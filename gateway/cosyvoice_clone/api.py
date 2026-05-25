"""Gateway CosyVoice clone endpoint（Phase 4.1 C.2）。

POST ``/api/voice/cosyvoice/clone``: 用户上传音色样本 + 显式授权 →
Gateway 转码 → 上传到 OSS / R2 / local stub → 调武汉 worker → 校验
worker 回包 ``target_model`` → 写 ``user_voices`` row → 返回 voice metadata。

**严格 fail-closed 顺序**（Codex 2026-05-25 二轮 review 修订后）：

::

    [无 cost 层]
    1. 认证（未登录 → 401）
    2. 授权（is_admin OR user.id in cosyvoice_clone_user_allowlist → 否则 403）
    3. feature flag（cosyvoice_clone_worker_enabled → 否则 503）
    4. uploader backend 配置（worker 已 enabled 时仍是 local_fs_stub → 503）
    5. consent 严格校验（confirmed == "true" literal / modal_version == v1）
    6. target_model 严格校验（∈ {flash, plus}，单值）
    7. source_segments JSON parse（无效 JSON → 400，**在读样本之前**）

    [DB 读 — 配额前置 gate]
    8. max_voices_per_user 配额检查（已满 → 409，**在读样本之前**）

    [本地 CPU / 几 ms]
    9. sample 读取 + sample_validator 5 维硬校验

    [本地 ffmpeg subprocess，~ 200 ms]
    10. audio_processor 转码（30s / 16k / mono / PCM 16-bit / WAV）

    [本地 / OSS 上传]
    11. SampleUploader 拿 short-TTL URL

    [跨境网络 + 付费 API ★]
    12. client.clone(WorkerCloneRequest) — **唯一付费触发点**

    [worker 回包契约校验]
    13. clone_resp.target_model == request.target_model（mismatch → 502 +
        bounded best-effort delete + 不写库）

    [DB 写入 + audit]
    14. user_voices row 落 + audit log

1-11 任一失败 → 不调 12 → 不扣费。
12 成功但 13 失败 → 不写 14；尝试 best-effort 删除已生成的 voice_id。

**plan §Phase 4.1 通过标准 锁死的字段**（Codex 2026-05-25 review）：

- ``user_voices`` 写入时 ``target_model`` / ``requires_worker`` /
  ``region_constraint`` / ``clone_worker_request_id`` /
  ``clone_provider_request_id`` 必须落库
- ``billing_sku`` 保持 None（待首次实账单回填）
- 单次 clone = 单 target_model（拒"复合 flash+plus"，前端 modal 一次
  提交对应 2 个 endpoint 调用）
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Reuse the same src/ injection pattern as mainland_voice_worker.py
for _candidate in [
    Path(__file__).resolve().parents[2] / "src",  # repo_root/src
    Path("/opt/aivideotrans/app/src"),                # Docker
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from admin_settings import _is_admin, load_settings
from auth import get_current_user
from config import settings as gw_settings
from cosyvoice_clone.audio_processor import (
    AudioProcessingError,
    AudioProcessingTimeoutError,
    normalize_sample_for_dashscope,
)
from cosyvoice_clone.sample_uploader import (
    IMPLEMENTED_BACKENDS,
    PRODUCTION_READY_BACKENDS,
    SampleUploader,
    build_sample_uploader_from_settings,
)
from cosyvoice_clone.sample_validator import (
    SampleValidationResult,
    validate_sample_bytes,
)
from database import get_db
from mainland_voice_worker import build_mainland_voice_worker_client
from models import User
from user_voice_service import add_user_voice, count_active_voices_for_user_and_provider

# Phase 1 worker contract（worker 端是部署到武汉的，但 client / dataclass
# 在 src/services/mainland_worker/ 共享）
from services.mainland_worker.client import (  # noqa: E402
    WorkerError,
    WorkerNetworkError,
    WorkerSignatureRejectedError,
)
from services.mainland_worker.types import (  # noqa: E402
    PLATFORM_DASHSCOPE_MAINLAND,
    PROVIDER_COSYVOICE_VOICE_CLONE,
    REGION_CONSTRAINT_MAINLAND_ONLY,
    TTS_PROVIDER_COSYVOICE,
    WORKER_PROVIDER_COSYVOICE,
    WORKER_REGION_CN_WUHAN,
    WorkerCloneConsent,
    WorkerCloneRequest,
    WorkerCloneSample,
    WorkerDeleteVoiceRequest,
)
from services.mainland_worker.worker.providers.base import ProviderError  # noqa: E402


logger = logging.getLogger(__name__)


# ---- 常量（Codex 2026-05-25 决策）----

CONSENT_MODAL_VERSION = "2026-05-25-v1"
ALLOWED_TARGET_MODELS = frozenset({
    "cosyvoice-v3.5-flash",
    "cosyvoice-v3.5-plus",
})
CLONE_API_MODEL = "voice-enrollment"   # plan §Phase 4.0a Observation Log
DEFAULT_SAMPLE_TTL_SECONDS = 3600       # 短 TTL signed URL


# ---- Router ----

router = APIRouter(
    prefix="/api/voice/cosyvoice",
    tags=["cosyvoice-clone"],
)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

def _check_authorized(user: User | None, allowlist: list[str]) -> User:
    """plan §Phase 4.1 §Schema + Backend 接通：
    ``authorized = is_admin(user) OR (user.id in cosyvoice_clone_user_allowlist)``。
    """
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthenticated", "message": "请先登录"},
        )
    if _is_admin(user):
        return user
    user_id_str = str(getattr(user, "id", "") or "")
    if user_id_str and user_id_str in allowlist:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "forbidden_not_in_allowlist",
            "message": "CosyVoice 克隆能力当前仅对受邀用户开放，您的账号未在 allowlist 中",
        },
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/clone")
async def cosyvoice_clone(
    target_model: str = Form(...),
    speaker_id: str = Form(...),
    speaker_name: str = Form(...),
    # Codex 2026-05-25 C.2 二轮 review fix #5：FastAPI ``bool = Form(...)``
    # 会把 ``"1" / "yes" / "on"`` 也解析为 True，对授权字段太宽松。改 str
    # 严格只接受 literal ``"true"``，其它一律 ``consent_required``。
    consent_voice_clone_confirmed: str = Form(...),
    consent_modal_version: str = Form(...),
    consent_confirmed_at: str = Form(...),
    sample: UploadFile = File(...),
    source_segments: str | None = Form(None),  # JSON array string
    source_job_id: str | None = Form(None),
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """``POST /api/voice/cosyvoice/clone`` — 用户显式触发的声音克隆。

    multipart/form-data：

    - ``sample``：音频文件（WAV PCM16 / MP3 / M4A，≤ 10 MB，3-60 秒）
    - ``target_model``：``cosyvoice-v3.5-flash`` 或 ``cosyvoice-v3.5-plus``
    - ``speaker_id`` / ``speaker_name``：UI 显示用
    - ``consent_voice_clone_confirmed`` / ``consent_modal_version`` /
      ``consent_confirmed_at``：用户授权三件套（plan §授权文案 v1）
    - ``source_segments`` (可选, JSON array)：样本来自任务 transcript 时的段号
    - ``source_job_id`` (可选)：样本来自某个 job
    """
    admin_settings = load_settings()

    # === Layer 1：认证 + 授权（无 cost）===
    auth_user = _check_authorized(user, admin_settings.cosyvoice_clone_user_allowlist)

    # === Layer 2：feature flag（无 cost）===
    if not admin_settings.cosyvoice_clone_worker_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "clone_feature_disabled",
                "message": "CosyVoice 克隆能力当前未启用",
            },
        )

    # === Layer 3 (NEW, fix #1 + 部署前项 #A)：sample uploader backend 配置（无 cost）===
    # Codex 2026-05-25 C.2 二轮 review：worker 已 enabled 但 uploader 仍是
    # local stub（file:// URL，DashScope 不可达）→ 直接 503，**不读样本 /
    # 不转码 / 不调付费 worker**。
    #
    # 部署前项 #A：``aliyun_oss`` 配置 schema 已开但工厂尚未实现 —— 早期
    # fail-closed，避免 transcode 完才报 500。具体两步：
    #   1) stub backend → ``sample_uploader_not_configured``
    #   2) 已配置 prod backend 但 factory 未实现 → ``sample_uploader_not_implemented``
    uploader_backend = getattr(gw_settings, "cosyvoice_sample_uploader", "local_fs_stub")
    if uploader_backend == "local_fs_stub":
        logger.error(
            "[cosyvoice_clone] refusing clone: worker enabled but sample uploader "
            "still LocalFsStubUploader (DashScope can't fetch file:// URLs). "
            "Set AVT_COSYVOICE_SAMPLE_UPLOADER=aliyun_oss in production env."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "sample_uploader_not_configured",
                "message": (
                    "CosyVoice 克隆功能不可用：sample 存储后端仍是本地 stub，"
                    "DashScope 无法访问 file:// URL，已拒绝请求。请联系管理员"
                    "配置真实 OSS 后端。"
                ),
            },
        )
    if uploader_backend not in PRODUCTION_READY_BACKENDS:
        # backend 在 KNOWN_BACKENDS 里但 factory 还没实现（当前 aliyun_oss
        # 处于此状态）。fail-closed —— 不要让请求走到转码 / 上传环节后才 500。
        logger.error(
            "[cosyvoice_clone] refusing clone: sample uploader backend %r is "
            "configured but factory implementation is missing (PROD_READY=%s, "
            "IMPLEMENTED=%s). Phase 4.1.x must ship AliyunOssUploader before live smoke.",
            uploader_backend,
            sorted(PRODUCTION_READY_BACKENDS),
            sorted(IMPLEMENTED_BACKENDS),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "sample_uploader_not_implemented",
                "message": (
                    f"CosyVoice 克隆功能不可用：sample 存储后端 {uploader_backend!r} "
                    f"尚未实现工厂层。请联系管理员核对 Phase 4.1.x 部署进度。"
                ),
            },
        )

    # === Layer 4 (UPDATED, fix #5)：consent 严格 literal "true" 校验（无 cost）===
    # FastAPI ``bool = Form(...)`` 会把 ``"1" / "yes" / "on"`` 也解析为 True；
    # 授权字段必须严格只接受 literal ``"true"``，避免前端意外发 ``"1"`` 就过授权关。
    if consent_voice_clone_confirmed != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "consent_required",
                "message": (
                    "必须勾选授权确认（consent.voice_clone_confirmed 必须是字符串 "
                    "\"true\"，严格大小写匹配）"
                ),
            },
        )
    if consent_modal_version != CONSENT_MODAL_VERSION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "consent_outdated",
                "message": (
                    f"授权文案版本不匹配：expected {CONSENT_MODAL_VERSION!r}, "
                    f"got {consent_modal_version!r}。请刷新页面看最新条款"
                ),
            },
        )
    if not consent_confirmed_at.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "consent_required", "message": "consent.confirmed_at 必填"},
        )

    # === Layer 5：target_model 严格校验（无 cost；防"复合 flash+plus"绕过）===
    if target_model not in ALLOWED_TARGET_MODELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_target_model",
                "message": (
                    f"target_model 必须是 {sorted(ALLOWED_TARGET_MODELS)} 之一，"
                    f"收到 {target_model!r}。前端 modal 提交 flash + plus 双 voice 时"
                    f"应该发起 2 次 endpoint 调用（plan §Phase 4.2 UI）"
                ),
            },
        )

    # === Layer 6 (REORDERED, fix #4)：source_segments JSON 解析提前 ===
    # Codex 2026-05-25 C.2 二轮 review：先解析 JSON，再读 sample / 转码 /
    # 上传。invalid JSON 不应触发任何 I/O。
    parsed_segments = _parse_source_segments(source_segments)

    # === Layer 7 (NEW, fix #3)：max_voices_per_user 配额检查（付费前 DB gate）===
    # Codex 2026-05-25 C.2 二轮 review：admin_settings.cosyvoice_clone_max_voices_per_user
    # 必须在 worker.clone 调用之前生效，否则灰度用户可反复触发付费。
    max_voices = int(admin_settings.cosyvoice_clone_max_voices_per_user or 0)
    if max_voices > 0:
        active_count = await count_active_voices_for_user_and_provider(
            db, user_id=auth_user.id, provider=PROVIDER_COSYVOICE_VOICE_CLONE,
        )
        if active_count >= max_voices:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "voice_quota_exceeded",
                    "message": (
                        f"已达账户克隆音色上限（{active_count}/{max_voices}），"
                        f"请删除部分已克隆音色后再试。"
                    ),
                    "current": active_count,
                    "limit": max_voices,
                },
            )

    # === Layer 8：sample 5 维硬校验（本地 CPU）===
    sample_bytes = await _read_upload_file(sample)
    validation = validate_sample_bytes(sample_bytes)
    if not validation.is_valid:
        # B 模块已经返了稳定 error_code，原样透传到前端
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": validation.error_code,
                "message": validation.error_message,
                "hints": list(validation.hints),
            },
        )

    # === Layer 9：audio_processor 转码（本地 ffmpeg）===
    try:
        transcoded = normalize_sample_for_dashscope(sample_bytes)
    except AudioProcessingTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except AudioProcessingError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    # === Layer 10：上传 sample 拿 short-TTL URL（本地 stub / 真实 OSS）===
    try:
        uploader: SampleUploader = build_sample_uploader_from_settings(gw_settings)
        sample_url = uploader.upload_and_sign(
            transcoded,
            filename_hint=sample.filename or "sample.wav",
            ttl_seconds=DEFAULT_SAMPLE_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — uploader 实现可能抛任意异常
        logger.exception("[cosyvoice_clone] sample upload failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "sample_upload_failed",
                "message": f"sample upload to storage failed: {exc}",
            },
        ) from exc

    # === Layer 11：调武汉 worker — **唯一付费 API 触发点**★ ===
    worker_client = build_mainland_voice_worker_client(gw_settings)
    if worker_client is None:
        # plan §Worker Degraded Mode：worker 未配置 / unavailable → 503，
        # 不静默切 MiniMax（CLAUDE.md 付费 API 硬约束）
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "worker_disabled",
                "message": "mainland voice worker disabled or misconfigured (admin to check)",
            },
        )

    sample_sha256 = _sha256_hex(transcoded)
    try:
        clone_resp = worker_client.clone(WorkerCloneRequest(
            job_id=(source_job_id or "").strip() or "no_job",
            user_id=str(auth_user.id),
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            target_model=target_model,
            sample=WorkerCloneSample(
                kind="download_url",
                url=sample_url,
                sha256=sample_sha256,
            ),
            source_segments=tuple(parsed_segments),
            consent=WorkerCloneConsent(
                voice_clone_confirmed=True,
                confirmed_at=consent_confirmed_at,
            ),
        ))
    except WorkerSignatureRejectedError as exc:
        # 签名错配——通常是 HMAC secret 配错。运维问题，不暴露细节给前端。
        logger.error("[cosyvoice_clone] worker HMAC rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "worker_auth_error", "message": "worker 拒绝签名，请联系管理员"},
        ) from exc
    except WorkerNetworkError as exc:
        logger.warning("[cosyvoice_clone] worker network error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "worker_unreachable", "message": str(exc)},
        ) from exc
    except WorkerError as exc:
        # 业务错误（4xx）：例如 consent_required（worker 端再校一遍）/
        # provider_error / sample_too_large 等。原样透传 error_code。
        logger.warning(
            "[cosyvoice_clone] worker returned business error: %s (code=%s, http=%d)",
            exc, exc.code, exc.http_status,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": f"worker_{exc.code}",
                "message": str(exc),
                "retryable": exc.retryable,
            },
        ) from exc
    except ProviderError as exc:  # pragma: no cover — should be wrapped by client
        logger.warning("[cosyvoice_clone] provider error leaked: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": f"provider_{exc.code}", "message": str(exc)},
        ) from exc

    # === Layer 12 (NEW, fix #2)：worker 回包 target_model 契约校验 ===
    # Codex 2026-05-25 C.2 二轮 review：CosyVoice voice_id ↔ model 绑定核心
    # 约束。若 worker / provider bug 返回的 voice 对不上请求的 target_model，
    # 写库后所有后续 TTS 都会失败。mismatch → 502 + bounded best-effort
    # delete + 拒写库。delete 仅试一次（永不重试），失败也不阻塞 502 返回；
    # 留 worker_request_id 供运维 reconcile。
    if clone_resp.target_model != target_model:
        logger.error(
            "[cosyvoice_clone] CRITICAL: worker target_model echo mismatch! "
            "requested=%r returned=%r voice_id=%s worker_request_id=%s "
            "provider_request_id=%s. Refusing to persist user_voices row.",
            target_model,
            clone_resp.target_model,
            clone_resp.voice_id,
            clone_resp.worker_request_id,
            clone_resp.provider_request_id,
        )
        _bounded_best_effort_delete(
            worker_client,
            voice_id=clone_resp.voice_id,
            job_id=(source_job_id or "").strip() or "no_job",
            user_id=str(auth_user.id),
            reason="target_model_mismatch_rollback",
        )
        try:
            worker_client.close()
        except Exception:  # pragma: no cover
            pass
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "worker_target_model_mismatch",
                "message": (
                    f"worker 返回的 voice 与请求 target_model 不一致："
                    f"requested={target_model!r}, got={clone_resp.target_model!r}。"
                    f"已发起 best-effort cleanup，请联系管理员核对 worker_request_id="
                    f"{clone_resp.worker_request_id!r}"
                ),
                "worker_request_id": clone_resp.worker_request_id,
            },
        )

    # === Layer 13：DB 写入 user_voices（plan §Phase 4.1 通过标准 必须落库的字段）===
    label = speaker_name or speaker_id or "我的克隆音色"
    try:
        await add_user_voice(
            db,
            user_id=auth_user.id,
            voice_id=clone_resp.voice_id,
            label=label,
            provider=PROVIDER_COSYVOICE_VOICE_CLONE,
            tts_provider=TTS_PROVIDER_COSYVOICE,
            platform=PLATFORM_DASHSCOPE_MAINLAND,
            source_speaker_id=speaker_id,
            source_job_id=source_job_id or None,
            source_speaker_name=speaker_name,
            clone_sample_seconds=(
                float(validation.duration_ms) / 1000.0
                if validation.duration_ms else None
            ),
            clone_sample_segment_ids=parsed_segments if parsed_segments else None,
            created_from="cosyvoice_clone_endpoint",
            # ---- Phase 4.1 worker dispatch + audit anchors（plan 必须落库的字段）----
            region_constraint=REGION_CONSTRAINT_MAINLAND_ONLY,
            requires_worker=True,
            target_model=target_model,
            worker_provider=WORKER_PROVIDER_COSYVOICE,
            worker_region=WORKER_REGION_CN_WUHAN,
            clone_api_model=CLONE_API_MODEL,
            # billing_sku 保持 None — Codex 2026-05-25 三轮决策：等首次实账单回填
            billing_sku=None,
            clone_provider_request_id=clone_resp.provider_request_id,
            clone_worker_request_id=clone_resp.worker_request_id,
        )
    except Exception as exc:  # noqa: BLE001
        # ⚠️ worker.clone 已经成功扣了 ¥0.01 但 DB 写失败：用户能在阿里云后台
        # 看到 voice_id 存在但 Gateway 看不到。这是 Phase 4.1 边界情况，需要
        # admin 后续手动同步或对账触发 reconcile。当前打 ERROR log + retryable
        # tombstone（plan §Voice Delete Flow 同款机制）。
        logger.error(
            "[cosyvoice_clone] CRITICAL: worker.clone succeeded (voice_id=%s, "
            "provider_request_id=%s) but DB write failed: %s. "
            "User has been charged but voice not in library.",
            clone_resp.voice_id,
            clone_resp.provider_request_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "db_write_failed_after_clone",
                "message": "克隆成功但本地音色库写入失败，请联系管理员核对",
                # 把 worker 返回的 ids 透回去，让支持工单能定位
                "voice_id": clone_resp.voice_id,
                "worker_request_id": clone_resp.worker_request_id,
            },
        ) from exc
    finally:
        try:
            worker_client.close()
        except Exception:  # pragma: no cover
            pass

    # === audit log（plan §账单观测与成本守卫 简版；完整 credits_ledger 集成留 G）===
    _log_clone_audit(
        user_id=str(auth_user.id),
        job_id=source_job_id or "",
        speaker_id=speaker_id,
        voice_id=clone_resp.voice_id,
        target_model=target_model,
        worker_request_id=clone_resp.worker_request_id,
        provider_request_id=clone_resp.provider_request_id,
        clone_sample_seconds=(
            float(validation.duration_ms) / 1000.0
            if validation.duration_ms else None
        ),
    )

    # === 成功响应 ===
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "ok": True,
            "voice_id": clone_resp.voice_id,
            "provider": PROVIDER_COSYVOICE_VOICE_CLONE,
            "tts_provider": TTS_PROVIDER_COSYVOICE,
            "target_model": target_model,
            "region_constraint": REGION_CONSTRAINT_MAINLAND_ONLY,
            "requires_worker": True,
            "platform": PLATFORM_DASHSCOPE_MAINLAND,
            "clone_api_model": CLONE_API_MODEL,
            "worker_request_id": clone_resp.worker_request_id,
            "provider_request_id": clone_resp.provider_request_id,
            "created_at": _utc_now_iso(),
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _read_upload_file(upload: UploadFile) -> bytes:
    """读 multipart UploadFile 全部 bytes。

    UploadFile 的 .read() 是 awaitable；返回前 await close 防文件句柄泄漏。
    """
    try:
        data = await upload.read()
    finally:
        try:
            await upload.close()
        except Exception:  # pragma: no cover
            pass
    return data


def _sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _bounded_best_effort_delete(
    worker_client: Any,
    *,
    voice_id: str,
    job_id: str,
    user_id: str,
    reason: str,
) -> None:
    """worker target_model 回包错配后的 bounded best-effort 清理（fix #2）。

    **永不重试，永不阻塞 502 返回。**

    - 用 worker client 现有 ``delete_voice()``（client retries 内置但
      `MAX_NETWORK_RETRIES=3`，对于 cleanup 来说 OK：客户已经收到 502，
      delete 失败也不影响业务）
    - delete 失败仅记 WARNING，让运维通过 ``worker_request_id`` 手动 reconcile
    """
    try:
        worker_client.delete_voice(
            voice_id,
            WorkerDeleteVoiceRequest(
                job_id=job_id,
                user_id=user_id,
                reason=reason,
            ),
        )
        logger.info(
            "[cosyvoice_clone] best-effort delete OK for mismatched voice_id=%s",
            voice_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[cosyvoice_clone] best-effort delete FAILED for voice_id=%s reason=%s exc=%s. "
            "Ops should manually reconcile via DashScope console.",
            voice_id, reason, exc,
        )


def _parse_source_segments(raw: str | None) -> list[int]:
    """``source_segments`` 可空 / 是 ``"[1, 2, 3]"`` 形态的 JSON。"""
    if not raw:
        return []
    raw = raw.strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_source_segments",
                "message": f"source_segments must be JSON array of int: {exc}",
            },
        ) from exc
    if not isinstance(parsed, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_source_segments", "message": "must be JSON array"},
        )
    try:
        return [int(x) for x in parsed]
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_source_segments",
                "message": f"source_segments items must be int: {exc}",
            },
        ) from exc


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_clone_audit(
    *,
    user_id: str,
    job_id: str,
    speaker_id: str,
    voice_id: str,
    target_model: str,
    worker_request_id: str,
    provider_request_id: str | None,
    clone_sample_seconds: float | None,
) -> None:
    """简版 audit log（plan §账单观测 完整 credits_ledger 集成留 G 收尾）。

    至少包含 plan §账单观测与成本守卫 list 的关键字段：worker_request_id
    必填 / provider_request_id nullable / billed_units / target_model /
    voice_id / user_id / job_id。
    """
    payload: dict[str, Any] = {
        "event_type": "cosyvoice_clone_request",
        "user_id": user_id,
        "job_id": job_id,
        "speaker_id": speaker_id,
        "voice_id": voice_id,
        "provider": PROVIDER_COSYVOICE_VOICE_CLONE,
        "clone_api_model": CLONE_API_MODEL,
        "billing_sku": None,
        "target_model": target_model,
        "worker_request_id": worker_request_id,
        "provider_request_id": provider_request_id,
        "billed_units": 1,
        "expected_cost_cny": 0.01,
        "clone_sample_seconds": clone_sample_seconds,
        "status": "succeeded",
        "created_at": _utc_now_iso(),
    }
    # 输出到 stdout（生产 docker logs 已经 bind-mount）；Phase 4.1 G 替换为
    # credits_ledger insert。注意 ``audit`` 关键字让运维 grep 容易。
    logger.info("[cosyvoice_clone_audit] %s", json.dumps(payload, ensure_ascii=False))
