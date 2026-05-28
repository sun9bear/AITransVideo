"""Phase 4.3a PR2-F — Express auto-clone 真实依赖装配 + 进入闸。

把 ``auto_clone.run_express_auto_clone`` 的注入式 client 用真实现装配：

- reservation：``services.express.reservation_client``（PR2-C internal endpoints）
- upload：``POST /api/internal/cosyvoice/express-sample-upload``（PR1-E1，multipart）
- worker：``MainlandWorkerClient.clone / delete_voice``
- register：``POST /api/internal/user-voices/register-smart``（PR1-E）
- sample：``VoiceSampleExtractor.extract_sample / validate_sample``

``maybe_run_express_auto_clone`` 是 process.py 的**唯一入口**，把进入闸
（admin 主开关 / worker env / consent / allowlist）收在这里，让 process.py
改动保持最小、orchestrator 逻辑不回流主流程（Codex PR2-F 边界）。

**默认安全**：admin 主开关默认 False → 立即 return None（no-op），Express
行为与 PR2 合入前**字节级一致**（DoD #8）。任何失败 → 不改 speaker_voices →
下游回 CosyVoice 预设音色，**绝不** MiniMax。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from services.express import reservation_client
from services.express.auto_clone import (
    CloneResult,
    ExpressAutoCloneClients,
    ExpressAutoCloneOutcome,
    RegisterResult,
    ReserveResult,
    SamplePrep,
    UploadResult,
    run_express_auto_clone,
)

logger = logging.getLogger(__name__)

TARGET_MODEL_DEFAULT = "cosyvoice-v3.5-flash"
_TEMPORARY_TTL_DAYS = 7
_UPLOAD_PATH = "/api/internal/cosyvoice/express-sample-upload"
_REGISTER_PATH = "/api/internal/user-voices/register-smart"
_UPLOAD_TIMEOUT_S = 30.0
_REGISTER_TIMEOUT_S = 15.0

# admin_settings keys（主 spec §7）
_K_ENABLED = "express_cosyvoice_auto_clone_enabled"
_K_ALLOWLIST = "express_cosyvoice_auto_clone_user_allowlist"
_K_MIN_RATIO = "express_cosyvoice_auto_clone_main_speaker_min_ratio"
_K_MIN_LINES = "express_cosyvoice_auto_clone_main_speaker_min_lines"
_K_TARGET_MODEL = "express_cosyvoice_auto_clone_target_model"
_K_SAMPLE_MAX_SECONDS = "express_cosyvoice_auto_clone_sample_max_seconds"


def _gateway_base() -> str:
    return os.environ.get("AVT_GATEWAY_URL", "http://127.0.0.1:8880").rstrip("/")


def _internal_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    key = os.environ.get("AVT_INTERNAL_API_KEY", "").strip()
    if key:
        headers["X-Internal-Key"] = key
    return headers


# ---------------------------------------------------------------------------
# HTTP 适配器：upload（multipart）+ register（JSON）
# ---------------------------------------------------------------------------


def _http_upload_sample(*, sample_path: str, user_id, job_id, speaker_id) -> UploadResult:
    """POST 样本到 PR1-E1 endpoint（multipart）→ 回 presigned URL + sha256。"""
    url = f"{_gateway_base()}{_UPLOAD_PATH}"
    try:
        with open(sample_path, "rb") as fh:
            files = {"sample": (Path(sample_path).name, fh, "audio/wav")}
            data = {
                "user_id": str(user_id),
                "job_id": str(job_id),
                "speaker_id": str(speaker_id),
            }
            resp = requests.post(
                url, headers=_internal_headers(), files=files, data=data,
                timeout=_UPLOAD_TIMEOUT_S,
            )
    except Exception as exc:  # noqa: BLE001 — 网络层错统一转 typed result
        logger.warning("express upload transport error: %s", type(exc).__name__)
        return UploadResult(ok=False, error="transport_error")
    if resp.status_code != 200:
        return UploadResult(ok=False, error=f"http_{resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        return UploadResult(ok=False, error="malformed_upload_response")
    if body.get("ok") and body.get("presigned_get_url") and body.get("sha256"):
        return UploadResult(
            ok=True,
            presigned_get_url=str(body["presigned_get_url"]),
            sha256=str(body["sha256"]),
        )
    return UploadResult(ok=False, error="malformed_upload_response")


def _http_register_smart(
    *, voice_id, speaker_id, job_id, user_id, target_model, temporary_expires_at
) -> RegisterResult:
    """POST register-smart（PR1-E）落 user_voices 临时音色行。

    payload 强制带 cosyvoice worker routing 自洽 8 字段（Codex PR2-F）：
    provider / tts_provider / platform / requires_worker / target_model /
    is_temporary / temporary_expires_at / created_from。否则
    lookup_clone_voice_routing_metadata 查不到 → TTS 回落预设。
    """
    url = f"{_gateway_base()}{_REGISTER_PATH}"
    headers = {"Content-Type": "application/json", **_internal_headers()}
    payload = {
        "user_id": str(user_id),
        "voice_id": str(voice_id),
        "label": f"express-clone-{speaker_id}",
        "provider": "cosyvoice_voice_clone",
        "tts_provider": "cosyvoice",
        "platform": "dashscope_mainland",
        "requires_worker": True,
        "target_model": str(target_model),
        "is_temporary": True,
        "temporary_expires_at": temporary_expires_at,
        "created_from": "express_auto",
        "source_speaker_id": str(speaker_id),
        "source_job_id": str(job_id),
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=_REGISTER_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("express register transport error: %s", type(exc).__name__)
        return RegisterResult(ok=False, detail=f"transport:{type(exc).__name__}")
    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError:
            return RegisterResult(ok=False, detail="malformed_register_response")
        if body.get("ok"):
            return RegisterResult(ok=True)
        return RegisterResult(ok=False, detail="register_not_ok")
    detail: str
    try:
        detail = str(resp.json())[:200]
    except ValueError:
        detail = f"http_{resp.status_code}"
    return RegisterResult(ok=False, detail=detail)


# ---------------------------------------------------------------------------
# 真实 deps 装配
# ---------------------------------------------------------------------------


def build_express_auto_clone_clients(
    *,
    user_id,
    job_id,
    project_dir,
    source_audio_path,
    target_model: str,
    sample_max_seconds: float,
    temporary_expires_at: str,
) -> ExpressAutoCloneClients:
    """装配 8 个真实 client callable。worker 由 ``build_client_from_env`` 构造
    （L2 gate 已确认 enabled，但仍 None-safe）。"""
    from services.mainland_worker.client_factory import build_client_from_env

    worker = build_client_from_env()

    def _prepare_sample(*, speaker_id, speaker_lines) -> SamplePrep | None:
        from services.voice.sample_extractor import (
            SampleExtractionError,
            VoiceSampleExtractor,
        )

        extractor = VoiceSampleExtractor()
        out_path = str(Path(project_dir) / "express_clone_samples" / f"{speaker_id}.wav")
        try:
            extractor.extract_sample(
                audio_path=str(source_audio_path),
                speaker_lines=speaker_lines,
                output_path=out_path,
                min_duration_s=10.0,
                max_duration_s=float(sample_max_seconds),
            )
            validation = extractor.validate_sample(out_path)
        except SampleExtractionError:
            return None
        except Exception:  # noqa: BLE001 — 抽样异常 → 视为不可 clone，回预设
            logger.exception("express sample extraction failed speaker=%s", speaker_id)
            return None
        duration_s = float(validation.get("duration_s") or 0.0)
        if duration_s < 10.0:
            return None
        return SamplePrep(sample_path=out_path, duration_s=duration_s, segment_ids=())

    def _reserve(*, user_id, job_id, speaker_id, target_model) -> ReserveResult:
        r = reservation_client.reserve(
            user_id=user_id, job_id=job_id, speaker_id=speaker_id, target_model=target_model
        )
        return ReserveResult(
            ok=r.ok, reservation_id=r.reservation_id, deny_reason=r.deny_reason, error=r.error
        )

    def _upload(*, sample_path, user_id, job_id, speaker_id) -> UploadResult:
        return _http_upload_sample(
            sample_path=sample_path, user_id=user_id, job_id=job_id, speaker_id=speaker_id
        )

    def _clone(*, sample_url, sample_sha256, speaker_id, job_id, user_id, consent_at) -> CloneResult:
        if worker is None:
            return CloneResult(ok=False, error="worker_not_configured")
        from services.mainland_worker.types import (
            WorkerCloneConsent,
            WorkerCloneRequest,
            WorkerCloneSample,
        )

        try:
            resp = worker.clone(
                WorkerCloneRequest(
                    job_id=str(job_id),
                    user_id=str(user_id),
                    speaker_id=str(speaker_id),
                    speaker_name=str(speaker_id),
                    target_model=str(target_model),
                    sample=WorkerCloneSample(
                        kind="download_url", url=str(sample_url), sha256=str(sample_sha256)
                    ),
                    source_segments=(),
                    consent=WorkerCloneConsent(
                        voice_clone_confirmed=True, confirmed_at=str(consent_at or "")
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 — worker 失败不重试（CLAUDE.md）
            logger.warning("express worker clone failed: %s", type(exc).__name__)
            return CloneResult(ok=False, error=type(exc).__name__)
        return CloneResult(
            ok=True,
            voice_id=resp.voice_id,
            worker_request_id=resp.worker_request_id,
            provider_request_id=resp.provider_request_id,
        )

    def _register(*, voice_id, speaker_id, job_id, user_id, sample_sha256, target_model) -> RegisterResult:
        return _http_register_smart(
            voice_id=voice_id,
            speaker_id=speaker_id,
            job_id=job_id,
            user_id=user_id,
            target_model=target_model,
            temporary_expires_at=temporary_expires_at,
        )

    def _delete_voice(voice_id, *, reason) -> bool:
        # best-effort 孤儿清理：rollback worker 自己刚创建的 voice（CLAUDE.md 允许）。
        if worker is None or not voice_id:
            return False
        from services.mainland_worker.types import WorkerDeleteVoiceRequest

        try:
            worker.delete_voice(
                str(voice_id),
                WorkerDeleteVoiceRequest(job_id=str(job_id), user_id=str(user_id), reason=str(reason)),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("express delete_voice failed: %s", type(exc).__name__)
            return False

    def _consume(reservation_id, *, voice_id) -> bool:
        return reservation_client.consume(reservation_id, voice_id=voice_id).ok

    def _release(reservation_id, *, reason) -> bool:
        return reservation_client.release(reservation_id, reason=reason).ok

    return ExpressAutoCloneClients(
        prepare_sample=_prepare_sample,
        reserve=_reserve,
        upload=_upload,
        clone=_clone,
        register=_register,
        delete_voice=_delete_voice,
        consume=_consume,
        release=_release,
    )


# ---------------------------------------------------------------------------
# 进入闸 + 单一入口
# ---------------------------------------------------------------------------


def _admin(key: str, default):
    from services.admin_settings import read_admin_setting

    return read_admin_setting(key, default=default)


def _has_consent(express_consent) -> bool:
    return bool(
        isinstance(express_consent, dict)
        and express_consent.get("auto_voice_clone") is True
        and express_consent.get("server_confirmed_at")
    )


def maybe_run_express_auto_clone(
    *,
    user_id,
    job_id,
    project_dir,
    source_audio_path,
    transcript_lines,
    speaker_voices: dict,
    speaker_routing: dict,
    express_consent,
) -> ExpressAutoCloneOutcome | None:
    """process.py 的唯一入口。进入闸全在这里：

    - identity（user_id / job_id 非空）
    - **L1 admin 主开关**（默认 False → no-op，Express 行为不变）
    - **L4 consent**（无 consent → skip，不调 auto_clone；前端 UI 留 PR3）
    - **L2 worker env**（武汉 worker 未启用 → skip）
    - **L3 allowlist**（fail-closed：空 / 非 list / 不含该 user → skip。空
      allowlist = 没人能用，等效 admin flag off；无 admin bypass）

    任一不过 → return None（**不构造 client、不调 run_express_auto_clone**），
    Express 走预设音色。全过则装配真实 deps 调编排，成功原地注入 routing。

    本函数**不抛**：任何意外 → log + return None（回预设）。
    """
    try:
        if not user_id or not job_id:
            return None
        # L1 admin 主开关（最重要：默认 False → Express 行为字节级不变）
        if not bool(_admin(_K_ENABLED, False)):
            return None
        # L4 consent（Codex PR2-F：无 consent 不调 auto_clone）
        if not _has_consent(express_consent):
            return None
        # L2 worker env
        from services.mainland_worker.client_factory import is_worker_enabled_in_env

        if not is_worker_enabled_in_env():
            return None
        # L3 allowlist —— **fail-closed canary gate**（与 PR1 availability +
        # 主 spec 一致）：空 allowlist = 没人能用（等效 admin flag off，双保险），
        # **不是**"全员放行"。非 list（malformed）也 fail-closed skip。
        # pipeline 只有 user_id、没有 user role，所以**不做 admin bypass**
        # （availability endpoint 才有 admin bypass）；admin 灰度冒烟须把自己的
        # user_id 显式加进 allowlist —— 与部署 SOP 一致。
        allowlist = _admin(_K_ALLOWLIST, [])
        if not isinstance(allowlist, list) or not allowlist:
            return None
        if str(user_id) not in {str(x) for x in allowlist}:
            return None

        target_model = str(_admin(_K_TARGET_MODEL, TARGET_MODEL_DEFAULT) or TARGET_MODEL_DEFAULT)
        try:
            min_ratio = float(_admin(_K_MIN_RATIO, 0.30))
        except (TypeError, ValueError):
            min_ratio = 0.30
        try:
            min_line_count = int(_admin(_K_MIN_LINES, 5))
        except (TypeError, ValueError):
            min_line_count = 5
        try:
            sample_max_seconds = float(_admin(_K_SAMPLE_MAX_SECONDS, 20))
        except (TypeError, ValueError):
            sample_max_seconds = 20.0

        temporary_expires_at = (
            datetime.now(timezone.utc) + timedelta(days=_TEMPORARY_TTL_DAYS)
        ).isoformat()

        clients = build_express_auto_clone_clients(
            user_id=user_id,
            job_id=job_id,
            project_dir=project_dir,
            source_audio_path=source_audio_path,
            target_model=target_model,
            sample_max_seconds=sample_max_seconds,
            temporary_expires_at=temporary_expires_at,
        )
        return run_express_auto_clone(
            user_id=user_id,
            job_id=job_id,
            project_dir=project_dir,
            transcript_lines=transcript_lines,
            speaker_voices=speaker_voices,
            speaker_routing=speaker_routing,
            express_consent=express_consent,
            clients=clients,
            target_model=target_model,
            min_ratio=min_ratio,
            min_line_count=min_line_count,
            admin_settings_snapshot={
                _K_ENABLED: True,
                _K_TARGET_MODEL: target_model,
                _K_MIN_RATIO: min_ratio,
            },
        )
    except Exception:  # noqa: BLE001 — 入口绝不炸 pipeline；回预设音色
        logger.exception("maybe_run_express_auto_clone failed (non-fatal; preset fallback)")
        return None


__all__ = [
    "build_express_auto_clone_clients",
    "maybe_run_express_auto_clone",
    "TARGET_MODEL_DEFAULT",
]
