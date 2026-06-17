"""APF P0 — anonymous preview API router (T7).

Three endpoints:

  POST /gateway/anonymous-preview/upload
      Accepts a raw binary upload, runs intake + admission, returns
      {preview_id, status, status_reason, mode}.

  GET /gateway/anonymous-preview/{preview_id}/status
      Returns {preview_status, stage, progress} for an existing record.
      Real-time job status is proxied from Job API (no local state beyond
      the record row).

  GET /gateway/anonymous-preview/{preview_id}/stream
      Gate check + httpx stream proxy to Job API publish.dubbed_video
      stream endpoint, rewriting Content-Disposition to "inline" and
      forwarding Range headers for seek support.  Does NOT use
      FileResponse (gateway cannot read the app container's filesystem,
      per AD-6 / F21).

Import constraints
------------------
* No ``services.jobs`` or ``src.pipeline`` (pydub guard — gateway has no pydub).
* No R2 references — preview artifacts are stream-only via Job API proxy.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from anonymous_preview_policy import (
    FreePreviewAdmissionResult,
    StreamGate,
    admit_for_free_preview,
    stream_gate_from_artifact_policy,
)
from anonymous_preview_probe import build_intake_probe_fn, teaser_dest_for
from anonymous_preview_prescreen import prescreen_filename
from anonymous_preview_quota import hash_scope_key, shanghai_today
from services.anonymous_preview_rate_limit import RateLimitCounterUnavailable
from anonymous_preview_record_store import RecordStoreError
from anonymous_preview_upload import UploadRejected, UploadTooLarge, handle_anonymous_upload, extract_client_ip
from anonymous_preview_intake_wiring import (
    ANON_PREVIEW_COUNTER_SCOPE,
    PER_SCOPE_PER_MODE_DAILY_CAP,
    express_subgate_key,
    peek_counter_keys,
    peek_mode_counter_keys,
    per_mode_cap_for_scope_key,
    resolve_express_global_cap,
    resolve_per_mode_caps,
    run_intake_and_save,
)
from anonymous_preview_limits import resolve_apf_limits
from anonymous_session import (
    AnonymousSessionContext,
    get_or_create_anonymous_session,
    require_anonymous_session,
)
from config import settings
from csrf import require_same_origin_state_change
from database import get_db
from internal_auth import internal_headers
from models import AnonymousPreviewRecord
from proxy import get_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/gateway/anonymous-preview",
    tags=["anonymous-preview"],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PREVIEW_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def _safe_preview_id(preview_id: str) -> Optional[str]:
    """Return preview_id if it passes a strict allowlist, else None."""
    if _PREVIEW_ID_RE.match(preview_id):
        return preview_id
    return None


def _redact_reason(reason: Optional[str]) -> Optional[str]:
    """Return a redacted reason string safe for clients (no internal detail)."""
    if not reason:
        return None
    _safe_codes = {
        "rate_limited", "rejected", "failed", "content_blocked",
        "needs_review", "storage_unavailable", "ready",
    }
    low = (reason or "").lower()
    for code in _safe_codes:
        if code in low:
            return code
    return "rejected"


async def _get_record_for_session(
    db: AsyncSession,
    preview_id: str,
    session_id_hash: str,
) -> Optional[AnonymousPreviewRecord]:
    """Fetch a record matching both preview_id and session_id (ownership check).

    存储侧的 ``session_id`` 不是 cookie 会话哈希本身：intake adapter 入库前
    用 wiring 的 HMAC hasher 又做了一层 ``hash_scope_key("sess:" + 会话哈希)``
    （privacy scope key，见 build_intake_config 的 hasher 包装）。查询侧必须
    用同一函数推导，否则 create/status/stream 恒 404 not_found
    （2026-06-11 冒烟发现）。
    """
    stored_session_key = hash_scope_key(
        f"sess:{session_id_hash}",
        secret=settings.anonymous_preview_hash_secret,
    )
    result = await db.execute(
        select(AnonymousPreviewRecord).where(
            AnonymousPreviewRecord.preview_id == preview_id,
            AnonymousPreviewRecord.session_id == stored_session_key,
        )
    )
    return result.scalar_one_or_none()


def _is_record_expired(record: AnonymousPreviewRecord) -> bool:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=__import__("datetime").timezone.utc)
    return expires <= now


def _get_admin_enabled() -> bool:
    """Read admin anonymous_free_preview_enabled — fail-closed on any error.

    plan 2026-06-12 §A 起仅供 **free lane** 语义的门使用（create 的
    free-mode 分支）；新 intake 的 master gate 走 ``_resolve_active_lane``。
    """
    try:
        from admin_settings import load_settings as _load
        return bool(_load().anonymous_free_preview_enabled)
    except Exception:
        return False


def _resolve_active_lane() -> Optional[str]:
    """当前活动 lane（"express"/"free"/None）。模块级薄包装便于测试注入。"""
    from anonymous_lane import resolve_anonymous_lane

    return resolve_anonymous_lane()


def _create_mode_gate(record_mode: str) -> Optional[JSONResponse]:
    """create 端按 record.mode 分流的开关门（plan 2026-06-12 §A/D2）。

    * ``free``：既有双门逐字节不变——``enable_free_tier`` env +
      ``anonymous_free_preview_enabled`` admin（AD-7：匿名 surface 不绕过
      free tier 总开关语义）。
    * ``express``：``anonymous_express_enabled`` admin（含 §E② mimo runtime
      防御纵深，经 ``express_lane_open``）；不查 free tier env——express
      不是 free tier。
    * 其它值：fail-closed 403（record.mode 只可能由 intake 写入合法值，
      未知值=数据损坏）。

    返回 None = 放行；JSONResponse = 调用方原样返回。
    """
    if record_mode == "free":
        if not getattr(settings, "enable_free_tier", False):
            return JSONResponse(status_code=403, content={"error": "free_disabled"})
        if not _get_admin_enabled():
            return JSONResponse(
                status_code=403, content={"error": "anonymous_preview_disabled"}
            )
        return None
    if record_mode == "express":
        from anonymous_lane import express_lane_open

        if not express_lane_open():
            return JSONResponse(
                status_code=403, content={"error": "anonymous_preview_disabled"}
            )
        return None
    logger.warning(
        "anon_create: unknown record mode %r — fail-closed", record_mode
    )
    return JSONResponse(
        status_code=403, content={"error": "anonymous_preview_disabled"}
    )


def _make_sync_intake_session():
    """Create a synchronous SQLAlchemy Session from the async engine's URL.

    Used only inside ``asyncio.to_thread`` for ``run_intake_and_save`` which
    requires a synchronous ``Session``. A fresh engine is created lazily so
    this module stays import-safe when the DB is not yet initialized.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from config import resolve_database_url
    url = resolve_database_url(settings)
    # Convert async postgresql+asyncpg:// → postgresql+psycopg2:// (sync driver)
    sync_url = url.replace("postgresql+asyncpg://", "postgresql://")
    if sync_url.startswith("postgresql+asyncpg://"):
        sync_url = "postgresql://" + sync_url[len("postgresql+asyncpg://"):]
    engine = create_engine(sync_url, pool_size=1, max_overflow=0)
    Session = sessionmaker(bind=engine)
    return Session()


# ---------------------------------------------------------------------------
# AD-8 body-before peek（/upload 与 §9 匿名分片 init 共享）
# ---------------------------------------------------------------------------

async def ad8_peek_precheck(
    db: AsyncSession, request: Request, limits, lane: str = "free"
) -> Optional[JSONResponse]:
    """Non-authoritative global + per-IP rate-limit pre-check（AD-8）。

    Body 读取/磁盘写入**之前**调用；超 cap 直接拒（瞬时磁盘 = 并发数×200MB
    防护）。SELECT-only，权威 try_acquire 仍在 run_intake_and_save →
    adapter._enforce_rate_limits（LaneAwareCounterStore 三层叠加）。返回
    None = 放行；返回 JSONResponse = 调用方原样返回（429 / 503 fail-closed）。

    (scope, scope_key) 必须与权威计数器写入形状逐字节一致：scope 列恒为
    ANON_PREVIEW_COUNTER_SCOPE（wiring 单实例 store），scope_key 是 adapter
    复合键 "global:{day}" / "ip:{hmac('ip:'+ip)}:{day}"。推导只许走
    peek_counter_keys / peek_mode_counter_keys——此前 peek 自行用
    scope='global'/'ip' + 裸值哈希，两个维度恒查 0 行、cap 预检恒放行
    （2026-06-11 bug ⑤）。

    plan 2026-06-12 §A/§B：``lane`` = resolver 结果，peek 按 lane 查对应
    计数——判定顺序与权威侧一致：总闸（legacy global，mode='free' 是历史
    key 形状的一部分、express intake 也累加它）→ express 子闸 →
    legacy per-ip → per-mode per-ip（1 次/日）。
    """
    client_ip_peek = extract_client_ip(request) or ""
    day_key_peek = shanghai_today()
    try:
        from sqlalchemy import text as _sa_text

        _global_key, _ip_key = peek_counter_keys(
            client_ip_peek,
            day_key_peek,
            secret=settings.anonymous_preview_hash_secret,
        )
        _express_key, _ip_mode_key = peek_mode_counter_keys(
            client_ip_peek,
            day_key_peek,
            lane,
            secret=settings.anonymous_preview_hash_secret,
        )
        _mode_sql = _sa_text(
            "SELECT count FROM anonymous_preview_daily_usage "
            "WHERE scope = :scope AND scope_key = :key "
            "  AND mode = :mode AND usage_date = :day"
        )
        _global_row = await db.execute(
            _sa_text(
                "SELECT count FROM anonymous_preview_daily_usage "
                "WHERE scope = :scope AND scope_key = :key "
                "  AND mode = 'free' AND usage_date = :day"
            ),
            {"scope": ANON_PREVIEW_COUNTER_SCOPE, "key": _global_key, "day": day_key_peek},
        )
        _global_count = int((_global_row.fetchone() or [0])[0])
        if _global_count >= limits.anonymous_preview_cap_global_per_day:
            logger.info(
                "anon_upload: AD-8 peek global cap reached count=%d cap=%d",
                _global_count,
                limits.anonymous_preview_cap_global_per_day,
            )
            return JSONResponse(status_code=429, content={"error": "preview_queue_full"})

        if lane == "express":
            _express_cap = int(resolve_express_global_cap())
            _express_row = await db.execute(
                _mode_sql,
                {
                    "scope": ANON_PREVIEW_COUNTER_SCOPE,
                    "key": _express_key,
                    "mode": "express",
                    "day": day_key_peek,
                },
            )
            _express_count = int((_express_row.fetchone() or [0])[0])
            if _express_count >= _express_cap:
                logger.info(
                    "anon_upload: AD-8 peek express subgate cap reached "
                    "count=%d cap=%d", _express_count, _express_cap,
                )
                return JSONResponse(
                    status_code=429, content={"error": "preview_queue_full"}
                )

        _ip_row = await db.execute(
            _sa_text(
                "SELECT count FROM anonymous_preview_daily_usage "
                "WHERE scope = :scope AND scope_key = :key "
                "  AND mode = 'free' AND usage_date = :day"
            ),
            {"scope": ANON_PREVIEW_COUNTER_SCOPE, "key": _ip_key, "day": day_key_peek},
        )
        _ip_count = int((_ip_row.fetchone() or [0])[0])
        if _ip_count >= limits.anonymous_preview_cap_per_ip:
            logger.info(
                "anon_upload: AD-8 peek ip cap reached count=%d cap=%d ip_key=%.16s",
                _ip_count,
                limits.anonymous_preview_cap_per_ip,
                _ip_key,
            )
            return JSONResponse(status_code=429, content={"error": "rate_limited"})

        _ip_mode_row = await db.execute(
            _mode_sql,
            {
                "scope": ANON_PREVIEW_COUNTER_SCOPE,
                "key": _ip_mode_key,
                "mode": lane,
                "day": day_key_peek,
            },
        )
        _ip_mode_count = int((_ip_mode_row.fetchone() or [0])[0])
        # per-mode ip cap：admin 旋钮（2026-06-13），与权威侧
        # LaneAwareCounterStore 共用 per_mode_cap_for_scope_key 推导。
        _ip_mode_cap = per_mode_cap_for_scope_key(_ip_mode_key, resolve_per_mode_caps())
        if _ip_mode_count >= _ip_mode_cap:
            logger.info(
                "anon_upload: AD-8 peek per-mode ip cap reached "
                "count=%d cap=%d lane=%s", _ip_mode_count, _ip_mode_cap, lane,
            )
            return JSONResponse(status_code=429, content={"error": "rate_limited"})
    except RateLimitCounterUnavailable:
        logger.warning("anon_upload: AD-8 peek rate-limit store unavailable — fail-closed")
        return JSONResponse(status_code=503, content={"error": "gate_unavailable"})
    except Exception as exc:
        logger.warning("anon_upload: AD-8 peek unexpected error — fail-closed: %s", exc)
        return JSONResponse(status_code=503, content={"error": "gate_unavailable"})
    return None


# ---------------------------------------------------------------------------
# POST /upload
# ---------------------------------------------------------------------------

@router.post("/upload")
async def anonymous_upload(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Accept a raw binary upload for anonymous preview.

    1. CSRF same-origin check
    2. Get-or-create anonymous session
    3. Stream to disk (cheap pre-checks inside handle_anonymous_upload)
    4. Build RequestFacts / UploadFacts → run_intake_and_save (in thread)
    5. admit_for_free_preview → merge admission info
    6. Return {preview_id, status, status_reason, mode}
    """
    # CSRF check
    try:
        require_same_origin_state_change(request)
    except Exception:
        return JSONResponse(status_code=403, content={"error": "csrf_origin_rejected"})

    # Session dependency (get-or-create)
    session_ctx = await get_or_create_anonymous_session(request, response, db)
    if isinstance(session_ctx, Response):
        return session_ctx

    assert isinstance(session_ctx, AnonymousSessionContext)

    # lane 单点解析（plan 2026-06-12 §A/D1）：本次请求内只 resolve 一次，
    # 同一值用于 master gate（admin_enabled）、record.mode 锁定、响应 mode。
    active_lane = _resolve_active_lane()
    admin_enabled = active_lane is not None

    # APF 限制旋钮（2026-06-11）：admin 热配置优先、env fallback。本次请求内
    # 只 resolve 一次，peek / upload / admission 用同一份快照保证一致。
    limits = resolve_apf_limits()

    # AD-8 body-before peek（抽出为 ad8_peek_precheck 与 §9 分片 init 共享；
    # fail-closed：DB 错 → 503 gate_unavailable）。lane 维度按 resolver 结果查。
    _peek_reject = await ad8_peek_precheck(
        db, request, limits, lane=active_lane or "free"
    )
    if _peek_reject is not None:
        return _peek_reject

    # Stream upload to disk
    upload_path: Optional[Path] = None
    try:
        upload_path, source_hash, size_bytes = await handle_anonymous_upload(
            request=request,
            session_hash=session_ctx.session_id_hash,
            flag_enabled=settings.enable_anonymous_preview,
            admin_enabled=admin_enabled,
            max_upload_bytes=limits.anonymous_preview_max_upload_bytes,
        )
    except UploadRejected as exc:
        logger.warning(
            "anon_upload_rejected reason=%s status=%d",
            exc.reason_code,
            exc.status_code,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.reason_code},
        )
    except UploadTooLarge:
        return JSONResponse(status_code=413, content={"error": "file_too_large"})
    except OSError as exc:
        logger.error("anon_upload: filesystem error: %s", exc)
        return JSONResponse(status_code=503, content={"error": "storage_error"})

    # Probe fn (T4) and prescreen fn (T5).
    # build_intake_probe_fn returns the SINGLE-arg adapter-contract callable
    # (the adapter calls probe_fn(upload)); never pass build_probe_fn raw —
    # its 2-arg signature TypeErrors into a fail-closed FAILED record.
    _probe_fn = build_intake_probe_fn(settings)

    def _prescreen_fn(probe_result) -> object:  # noqa: ANN001
        # T5: local-rules prescreen by filename (synchronous stdlib, no paid calls)
        filename = upload_path.name if upload_path else ""
        return prescreen_filename(filename)

    # Build adapter facts — field names from
    # src.services.anonymous_preview_backend_adapter.RequestFacts / UploadFacts
    from services.anonymous_preview_backend_adapter import RequestFacts, UploadFacts
    from services.anonymous_preview_intake import SourceType

    client_ip = extract_client_ip(request) or ""

    request_facts = RequestFacts(
        raw_session_id=session_ctx.session_id_hash,
        raw_ip=client_ip,
        raw_device_cookie=session_ctx.session_id_hash,  # AD-5: device key = avt_anon token
        source_type=SourceType.LOCAL_UPLOAD,
        is_free_user=True,
        day_key=shanghai_today(),
    )

    upload_facts = UploadFacts(
        file_name=upload_path.name if upload_path else "upload",
        byte_length=size_bytes,
        duration_seconds=0.0,  # probe_fn fills this in during handle_intake
        source_hash=source_hash,
        stored_path=upload_path,
    )

    # Run intake + save record (sync adapter → run in thread)
    try:
        def _run_sync() -> object:
            sync_db = _make_sync_intake_session()
            try:
                rec = run_intake_and_save(
                    db_session=sync_db,
                    request_facts=request_facts,
                    upload_facts=upload_facts,
                    probe_fn=_probe_fn,
                    prescreen_fn=_prescreen_fn,
                    # lane 锁定时机=intake（plan §A）：record.mode 一经写入，
                    # 后续 create 读 record.mode，不再重新 resolve。
                    mode=active_lane or "free",
                )
                # run_intake_and_save 契约（wiring docstring）："the caller
                # commits/rolls back"。store 内部只 flush；漏 commit → close()
                # 时整个事务（record + 配额计数）静默回滚，upload 返回 200 但
                # 记录不存在，/create 恒 404 not_found（2026-06-11 冒烟发现）。
                sync_db.commit()
                return rec
            except BaseException:
                sync_db.rollback()
                raise
            finally:
                sync_db.close()

        record = await asyncio.to_thread(_run_sync)
    except RecordStoreError as exc:
        logger.error("anon_intake: record store error: %s", exc)
        if upload_path and upload_path.exists():
            upload_path.unlink(missing_ok=True)
        return JSONResponse(status_code=503, content={"error": "storage_error"})
    except Exception as exc:
        logger.exception("anon_intake: unexpected error: %s", exc)
        if upload_path and upload_path.exists():
            upload_path.unlink(missing_ok=True)
        return JSONResponse(status_code=500, content={"error": "intake_failed"})

    # APF P0 T8b：把媒体路径持久化进 ORM audit（契约 record 保持 status-only，
    # 禁媒体字段；/create 需要 teaser 路径作为 Job API source_ref）。
    try:
        _row_result = await db.execute(
            select(AnonymousPreviewRecord).where(
                AnonymousPreviewRecord.preview_id == record.record_id
            )
        )
        _orm_row = _row_result.scalar_one_or_none()
        if _orm_row is None:
            # intake 刚返回了 record 却查不到行 = 持久化层断裂（如漏 commit）。
            # 静默跳过会让 200 带着死 preview_id 出门 → /create 恒 404；
            # 必须 fail-loud（与下方 except 分支同语义：503 + 清理媒体）。
            raise RuntimeError(
                f"anon_upload: record {record.record_id} not found in ORM "
                "after intake save — persistence broken"
            )
        if _orm_row is not None and upload_path is not None:
            _merged_audit = dict(_orm_row.audit or {})
            _merged_audit["stored_upload_path"] = str(upload_path)
            _merged_audit["teaser_path"] = str(teaser_dest_for(upload_path))
            # CodeX P1 修复：契约 PreviewRecord 字段名是 duration_seconds
            # (不是 teaser_duration_seconds)；ORM record 行不带 duration 列，
            # 故把契约真实 teaser 时长落进 audit，供 stream/后续 gate 读取。
            _merged_audit["teaser_duration_seconds"] = float(
                getattr(record, "duration_seconds", 0.0) or 0.0
            )
            _orm_row.audit = _merged_audit
            await db.commit()
    except Exception as exc:
        # 对抗审核 P1 修复：原先 warning 后返回 200 会造成"上传成功但
        # /create 永远 409 teaser_missing"的哑死局。改为显式 503 + 清理
        # 媒体文件，让用户重新上传（fail-loud 而非 fail-silent）。
        logger.error("anon_upload: audit path persist failed: %s", exc)
        if upload_path is not None:
            if upload_path.exists():
                upload_path.unlink(missing_ok=True)
            _t = teaser_dest_for(upload_path)
            if _t.exists():
                _t.unlink(missing_ok=True)
        return JSONResponse(status_code=503, content={"error": "storage_error"})

    # Admission (T6 thin adapter) — 用契约字段 duration_seconds（CodeX P1）。
    admission: Optional[FreePreviewAdmissionResult] = None
    try:
        teaser_dur = float(getattr(record, "duration_seconds", 0.0) or 0.0)
        # ApfLimits 字段与 settings 同名，policy 薄 adapter 直接消费
        admission = admit_for_free_preview(teaser_dur, limits)
    except Exception as exc:
        logger.warning("anon_upload: admit_for_free_preview error: %s", exc)

    admission_decision = None
    if admission is not None:
        d = admission.decision
        admission_decision = d.value if hasattr(d, "value") else str(d)

    record_status = record.status
    status_str = record_status.value if hasattr(record_status, "value") else str(record_status)

    out = JSONResponse(
        status_code=200,
        content={
            "preview_id": record.record_id,
            "status": status_str,
            "status_reason": _redact_reason(record.status_reason),
            "mode": getattr(record, "mode", None) or "free",
            "admission_decision": admission_decision,
        },
    )
    # FastAPI 不会把依赖注入 `response` 上的 header 合并进 handler 显式返回的
    # Response —— get_or_create_anonymous_session 设置的 avt_anon Set-Cookie
    # 必须手动搬运，否则匿名会话永远到不了客户端，/create 恒 401
    # anonymous_session_required（2026-06-11 e2e 冒烟发现，漏斗级 P0）。
    for _sc in response.headers.getlist("set-cookie"):
        out.headers.append("set-cookie", _sc)
    return out


# ---------------------------------------------------------------------------
# GET /limits  (APF 限制旋钮，2026-06-11)
# ---------------------------------------------------------------------------

@router.get("/limits")
async def anonymous_preview_limits() -> Response:
    """公开只读端点：当前生效的匿名预览限制（前端试用面板动态文案用）。

    无需 session / CSRF（GET 只读、只返回数字、无敏感信息）。仅 env flag
    gate：flag 关 → 404（与其他匿名预览端点一致）；admin 热开关**不** gate
    它——面板在 admin 临时熔断期间仍能渲染正确的提示文案。

    注意必须注册在 ``/{preview_id}/*`` 动态路由之前（字面路径优先）。
    """
    if not settings.enable_anonymous_preview:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    limits = resolve_apf_limits()
    # plan 2026-06-12 §G：lane 三态由 limits 下发（free/express/关闭），
    # 前端面板据此渲染，lane 切换无需重建镜像。master_open = env（已过）
    # AND 任一 lane 开。
    active_lane = _resolve_active_lane()
    return JSONResponse(
        status_code=200,
        content={
            "max_upload_mb": limits.anonymous_preview_max_upload_bytes // (1024 * 1024),
            "preview_seconds": limits.anonymous_preview_max_seconds,
            "active_lane": active_lane,
            "master_open": active_lane is not None,
        },
    )


# ---------------------------------------------------------------------------
# GET /{preview_id}/status
# ---------------------------------------------------------------------------

@router.get("/{preview_id}/status")
async def anonymous_preview_status(
    preview_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Return preview status for an existing record.

    For records that have a ``job_id``, proxies the live status from Job API
    and translates it to a preview-facing schema.  For records without a
    ``job_id``, returns the record status directly.
    """
    session_ctx = await require_anonymous_session(request, db)
    if isinstance(session_ctx, Response):
        return session_ctx

    assert isinstance(session_ctx, AnonymousSessionContext)

    safe_id = _safe_preview_id(preview_id)
    if safe_id is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})

    record = await _get_record_for_session(db, safe_id, session_ctx.session_id_hash)
    if record is None or _is_record_expired(record):
        return JSONResponse(status_code=404, content={"error": "not_found"})

    # No job yet → return record status directly
    if not record.job_id:
        return JSONResponse(
            status_code=200,
            content={
                "preview_id": record.preview_id,
                "preview_status": record.status,
                "stage": None,
                "progress": None,
                "mode": record.mode,
            },
        )

    # 对抗审核 P1 修复：__creating__ 哨兵值不要拿去查 Job API（必 404 →
    # 伪装成 pending）。显式返回 creating 阶段，前端与监控可识别。
    if record.job_id == "__creating__":
        return JSONResponse(
            status_code=200,
            content={
                "preview_id": record.preview_id,
                "preview_status": "processing",
                "stage": "creating",
                "progress": None,
                "mode": record.mode,
            },
        )

    # Proxy real-time status from Job API (no-state translation, no DB write)
    job_id = record.job_id
    upstream_url = f"{settings.job_api_upstream}/jobs/{job_id}"
    try:
        client = get_client()
        ih = internal_headers()
        upstream_resp = await client.get(upstream_url, headers=ih)
        if upstream_resp.status_code == 404:
            return JSONResponse(
                status_code=200,
                content={
                    "preview_id": record.preview_id,
                    "preview_status": "pending",
                    "stage": None,
                    "progress": None,
                    "mode": record.mode,
                },
            )
        if upstream_resp.status_code != 200:
            logger.warning(
                "anon_status: job_api returned %d for job_id=%s",
                upstream_resp.status_code, job_id,
            )
            return JSONResponse(
                status_code=200,
                content={
                    "preview_id": record.preview_id,
                    "preview_status": "unknown",
                    "stage": None,
                    "progress": None,
                    "mode": record.mode,
                },
            )
        job_data = upstream_resp.json()
        job_status = job_data.get("status", "unknown")
        _status_map = {
            "queued": "pending",
            "running": "processing",
            "succeeded": "ready",
            "failed": "failed",
            "cancelled": "failed",
        }
        preview_status = _status_map.get(job_status, job_status)
        return JSONResponse(
            status_code=200,
            content={
                "preview_id": record.preview_id,
                "preview_status": preview_status,
                "stage": job_data.get("current_stage"),
                "progress": job_data.get("progress"),
                "mode": record.mode,
            },
        )
    except Exception as exc:
        logger.warning("anon_status: job api proxy error: %s", exc)
        return JSONResponse(
            status_code=200,
            content={
                "preview_id": record.preview_id,
                "preview_status": "unknown",
                "stage": None,
                "progress": None,
                "mode": record.mode,
            },
        )


# ---------------------------------------------------------------------------
# GET /{preview_id}/stream
# ---------------------------------------------------------------------------

@router.get("/{preview_id}/stream")
async def anonymous_preview_stream(
    preview_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Stream-only proxy for the anonymous preview video (AD-6).

    Gate: record exists + session matches + TTL not expired + admin open +
    T6 stream_gate (stream_only_required) + job succeeded + job_id present.

    Proxies ``GET /jobs/{job_id}/stream/video`` from Job API with:
    - ``Content-Disposition: inline``  (NOT attachment)
    - Range header forwarded for seek / 206 partial-content support
    - No R2 redirect — local stream-only for anonymous previews
    """
    session_ctx = await require_anonymous_session(request, db)
    if isinstance(session_ctx, Response):
        return session_ctx

    assert isinstance(session_ctx, AnonymousSessionContext)

    safe_id = _safe_preview_id(preview_id)
    if safe_id is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})

    record = await _get_record_for_session(db, safe_id, session_ctx.session_id_hash)
    if record is None or _is_record_expired(record):
        return JSONResponse(status_code=404, content={"error": "not_found"})

    if not record.job_id:
        return JSONResponse(
            status_code=409,
            content={"error": "preview_not_ready", "detail": "no_job"},
        )

    # 生命周期不变量（plan 2026-06-12 §A，R2 #4）：stream 对 lane 开关
    # 零感知——原 admin 热开关 re-check 已移除，已建 record 凭存在性 +
    # env master flag 服务到 TTL（否则关 lane 会立刻杀旧 record 的回放，
    # 回滚语义就坏了）。紧急全停用 env enable_anonymous_preview。
    # 守卫：tests/test_anonymous_express_t1_lane_gates.py::
    # TestLifecycleSourceGuards::test_stream_handler_no_admin_lane_recheck。

    # T6 stream gate —— ORM record 行不带 duration 列，从 audit 读
    # upload 阶段落盘的契约 teaser 时长（CodeX P1）。
    try:
        _audit = dict(getattr(record, "audit", None) or {})
        teaser_dur = float(_audit.get("teaser_duration_seconds", 0.0) or 0.0)
        # APF 限制旋钮：admin 热值优先（字段与 settings 同名，policy 直接消费）
        admission = admit_for_free_preview(teaser_dur, resolve_apf_limits())
        gate: StreamGate = stream_gate_from_artifact_policy(admission.artifact_policy)
        if not gate.stream_only_required:
            logger.warning(
                "anon_stream: stream_gate stream_only_required=False preview_id=%s — fail-closed",
                safe_id,
            )
            return JSONResponse(status_code=403, content={"error": "stream_not_permitted"})
    except Exception as exc:
        logger.warning("anon_stream: stream gate error: %s", exc)
        return JSONResponse(status_code=403, content={"error": "stream_gate_error"})

    # Verify job is succeeded via Job API
    job_id = record.job_id
    try:
        client = get_client()
        ih = dict(internal_headers())
        status_resp = await client.get(
            f"{settings.job_api_upstream}/jobs/{job_id}",
            headers=ih,
        )
        if status_resp.status_code != 200:
            return JSONResponse(status_code=409, content={"error": "preview_not_ready"})
        job_data = status_resp.json()
        if job_data.get("status") != "succeeded":
            return JSONResponse(
                status_code=409,
                content={
                    "error": "preview_not_ready",
                    "job_status": job_data.get("status"),
                },
            )
    except Exception as exc:
        logger.warning("anon_stream: job status check error: %s", exc)
        return JSONResponse(status_code=502, content={"error": "upstream_error"})

    # Proxy stream via Job API — forward Range, rewrite Content-Disposition to inline
    stream_url = f"{settings.job_api_upstream}/jobs/{job_id}/stream/video"
    fwd_headers: dict[str, str] = dict(internal_headers())
    range_header = request.headers.get("range")
    if range_header:
        fwd_headers["range"] = range_header

    try:
        upstream_req = client.build_request("GET", stream_url, headers=fwd_headers)
        upstream_response = await client.send(upstream_req, stream=True)

        # Build safe response headers — only pass through known safe headers
        _PASSTHROUGH = frozenset({
            "content-type", "content-length", "accept-ranges",
            "content-range", "cache-control", "etag", "last-modified",
        })
        resp_headers: dict[str, str] = {
            k: v
            for k, v in upstream_response.headers.items()
            if k.lower() in _PASSTHROUGH
        }
        # Enforce inline disposition (stream-only, not download)
        resp_headers["Content-Disposition"] = "inline"

        async def _iter_body():
            # aclose() MUST be in finally: on client disconnect Starlette
            # abandons the generator without draining it, so an aclose() placed
            # after the loop never runs and the upstream TCP conn leaks until
            # timeout.
            try:
                async for chunk in upstream_response.aiter_bytes(chunk_size=65536):
                    yield chunk
            finally:
                await upstream_response.aclose()

        return StreamingResponse(
            _iter_body(),
            status_code=upstream_response.status_code,  # 200 or 206
            headers=resp_headers,
        )
    except Exception as exc:
        logger.warning("anon_stream: proxy error job_id=%s: %s", job_id, exc)
        return JSONResponse(status_code=502, content={"error": "stream_proxy_error"})


# ---------------------------------------------------------------------------
# POST /{preview_id}/create  (T8b)
# ---------------------------------------------------------------------------

_CREATING_SENTINEL = "__creating__"
_CREATE_RESERVATION_PREFIX = "creating_until:"
_CREATE_RESERVATION_TTL_SECONDS = 5 * 60
_SENTINEL_USER_EMAIL = "anonymous-preview@system"
_READY_STATUS = "ready_for_mode"  # PreviewStatus.READY_FOR_MODE.value（契约钉死）
_CREATE_CAPACITY_LOCK_KEY = int.from_bytes(
    hashlib.blake2b(b"anonymous_preview:create_capacity", digest_size=8).digest(),
    byteorder="big",
    signed=True,
)


def _create_reservation_claim_token(*, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    expires_epoch = int(now.timestamp()) + _CREATE_RESERVATION_TTL_SECONDS
    return f"{_CREATE_RESERVATION_PREFIX}{expires_epoch:010d}:{secrets.token_urlsafe(16)}"


def _create_reservation_cutoff_token(*, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{_CREATE_RESERVATION_PREFIX}{int(now.timestamp()):010d}:"


def _session_dialect_name(db: AsyncSession) -> Optional[str]:
    """Best-effort SQLAlchemy dialect name for PG-only lock helpers."""

    candidates = []
    get_bind = getattr(db, "get_bind", None)
    if callable(get_bind):
        try:
            candidates.append(get_bind())
        except Exception:
            pass

    bind = getattr(db, "bind", None)
    if bind is not None:
        candidates.append(bind)

    sync_session = getattr(db, "sync_session", None)
    sync_get_bind = getattr(sync_session, "get_bind", None)
    if callable(sync_get_bind):
        try:
            candidates.append(sync_get_bind())
        except Exception:
            pass

    for candidate in candidates:
        dialect = getattr(candidate, "dialect", None)
        name = getattr(dialect, "name", None)
        if name:
            return str(name)
    return None


async def _acquire_create_capacity_transaction_lock(db: AsyncSession) -> bool:
    """Serialize APF create capacity count + reservation on PostgreSQL.

    SQLite/fake sessions used by local tests intentionally no-op. Production
    PostgreSQL holds this transaction advisory lock only until the reservation
    helper commits, before any external Job API call is made.
    """

    if _session_dialect_name(db) != "postgresql":
        return False
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _CREATE_CAPACITY_LOCK_KEY},
    )
    return True


async def _reserve_create_capacity(
    db: AsyncSession,
    *,
    preview_id: str,
    max_in_flight: int,
    job_model: object,
    terminal_statuses: list[str],
) -> str:
    """Reserve one APF create slot before calling the external Job API.

    Returns ``reserved``, ``queue_full`` or ``already_created``.
    """

    from sqlalchemy import func, update as _sa_update

    if max_in_flight <= 0:
        return "queue_full"

    try:
        await _acquire_create_capacity_transaction_lock(db)
        now = datetime.now(timezone.utc)
        reservation_cutoff = _create_reservation_cutoff_token(now=now)

        job_count_result = await db.execute(
            select(func.count())
            .select_from(job_model)
            .where(
                job_model.is_anonymous_preview.is_(True),
                job_model.status.notin_(terminal_statuses),
            )
        )
        active_jobs = int(job_count_result.scalar() or 0)

        creating_count_result = await db.execute(
            select(func.count())
            .select_from(AnonymousPreviewRecord)
            .where(
                AnonymousPreviewRecord.job_id == _CREATING_SENTINEL,
                AnonymousPreviewRecord.claim_token_placeholder.like(
                    f"{_CREATE_RESERVATION_PREFIX}%"
                ),
                AnonymousPreviewRecord.claim_token_placeholder >= reservation_cutoff,
            )
        )
        active_reservations = int(creating_count_result.scalar() or 0)

        if active_jobs + active_reservations >= max_in_flight:
            await db.commit()
            return "queue_full"

        claim = await db.execute(
            _sa_update(AnonymousPreviewRecord)
            .where(
                AnonymousPreviewRecord.preview_id == preview_id,
                AnonymousPreviewRecord.job_id.is_(None),
            )
            .values(
                job_id=_CREATING_SENTINEL,
                claim_token_placeholder=_create_reservation_claim_token(now=now),
            )
            .returning(AnonymousPreviewRecord.preview_id)
        )
        won_claim = claim.first() is not None
        await db.commit()
        return "reserved" if won_claim else "already_created"
    except Exception:
        rollback = getattr(db, "rollback", None)
        if callable(rollback):
            try:
                maybe = rollback()
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception:
                logger.debug("anon_create: rollback after reserve failure failed", exc_info=True)
        raise

# 匿名 express payload 的 tts_provider 白名单（plan 2026-06-12 §D）。
# 唯一真源在 anonymous_lane.VALID_ANON_EXPRESS_TTS_PROVIDERS（CodeX 第三轮
# P2：lane 开关判定与 create 解析必须同一份白名单，否则 lane 开了 create
# 拒，preview 被锁死还烧配额）。语义对齐登录 express 解析但剔除 mimo。
from anonymous_lane import VALID_ANON_EXPRESS_TTS_PROVIDERS as _VALID_ANON_EXPRESS_TTS_PROVIDERS

# 重试可重入的 Job API 终态（plan §E：仅诚实失败可重入 create）。
_RETRYABLE_JOB_STATUSES = frozenset({"failed", "cancelled"})

# CodeX 外审 2026-06-12 P1：单个 preview 的重试次数上限（retry_chain 长度）。
# 没有上限时，一个 admitted preview 可以反复发起付费 express 管线（每次
# 重试 = 完整 转录+Gemini Pass1-3 成本）而不再过任何计数闸。
ANON_CREATE_MAX_RETRIES = 2


def _resolve_express_payload_tts_provider() -> Optional[str]:
    """express record 的 create payload tts_provider（plan §D）。

    admin ``express_tts_provider`` 经白名单解析；mimo / 未知值 / 读取异常
    → None（调用方 fail-closed 拒绝 create，不静默回落默认 provider——
    与登录 express 的"回落 cosyvoice"不同，匿名面宁可失败不猜）。
    """
    try:
        from admin_settings import load_settings

        provider = (load_settings().express_tts_provider or "").strip().lower()
    except Exception as exc:  # noqa: BLE001 — fail-closed
        logger.warning(
            "anon_create: express_tts_provider 读取失败 — fail-closed: %s", exc
        )
        return None
    if provider in _VALID_ANON_EXPRESS_TTS_PROVIDERS:
        return provider
    logger.warning(
        "anon_create: express_tts_provider=%r 不在匿名白名单 %s — 拒绝 create",
        provider, sorted(_VALID_ANON_EXPRESS_TTS_PROVIDERS),
    )
    return None


async def _job_is_terminal_failed(job_id: str) -> bool:
    """重试重入判定（plan §E + CodeX 外审 2026-06-12 P2 收紧）：仅当既有
    job 是 **Pass 3 诚实失败**（终态 failed/cancelled 且带
    ``smart_state.anon_pass3_failed`` marker）才允许 failed → 重新 create。

    P2 背景：终态判定若只看 status，内容合规拒绝 / 输入不支持 / 存储错误
    等确定性失败也会被同 payload 反复重提。marker 由 pipeline 在 artifact
    判定失败瞬间经 [SMART_STATE] 写入 JSON store（JobRecord.smart_state，
    GET /jobs/{id} 响应直接携带，无 mirror 滞后）。

    * 200 且 status ∈ {failed, cancelled} 且 marker 为 True → True
    * 404（job 已被清理，无法核验 marker）→ False（fail-closed；用户走
      重新上传）
    * 其它状态 / 任何异常 → False（fail-closed：409 already_created）
    """
    try:
        client = get_client()
        resp = await client.get(
            f"{settings.job_api_upstream}/jobs/{job_id}",
            headers=internal_headers(),
        )
        if resp.status_code != 200:
            return False
        payload = resp.json()
        if str(payload.get("status") or "") not in _RETRYABLE_JOB_STATUSES:
            return False
        smart_state = payload.get("smart_state") or {}
        return isinstance(smart_state, dict) and smart_state.get(
            "anon_pass3_failed"
        ) is True
    except Exception as exc:  # noqa: BLE001 — fail-closed
        logger.warning(
            "anon_create: retry 终态判定失败 job=%s — fail-closed: %s",
            job_id, exc,
        )
        return False


async def _reset_create_claim_to(
    db: "AsyncSession", preview_id: str, job_id_value: str
) -> None:
    """重试路径的抢占回退：__creating__ → 旧 failed job_id（保持可再重试）。"""
    from sqlalchemy import update as _sa_update

    try:
        await db.execute(
            _sa_update(AnonymousPreviewRecord)
            .where(
                AnonymousPreviewRecord.preview_id == preview_id,
                AnonymousPreviewRecord.job_id == _CREATING_SENTINEL,
            )
            .values(job_id=job_id_value)
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "anon_create: retry claim reset failed preview_id=%s: %s",
            preview_id, exc,
        )


async def _abort_create_claim(
    db: "AsyncSession", preview_id: str, retry_of: Optional[str]
) -> None:
    """claim 后失败路径的统一回退（CodeX 复审 2026-06-12 P2）。

    初次 create → job_id 复位 NULL（既有语义）；**重试** → 恢复旧 failed
    job_id——复位 NULL 会让下次 create 被当作初次创建，绕过重试守卫 /
    次数上限 / 子闸计费。
    """
    if retry_of is None:
        await _reset_create_claim(db, preview_id)
    else:
        await _reset_create_claim_to(db, preview_id, retry_of)


_USAGE_UPSERT_SQL = """
    INSERT INTO anonymous_preview_daily_usage
        (id, scope, scope_key, mode, usage_date, count,
         created_at, updated_at)
    VALUES
        (gen_random_uuid(), :scope, :key, :mode, :day, 1,
         now(), now())
    ON CONFLICT (scope, scope_key, mode, usage_date)
    DO UPDATE SET
        count = anonymous_preview_daily_usage.count + 1,
        updated_at = now()
    WHERE anonymous_preview_daily_usage.count < :cap
    RETURNING count
"""

_USAGE_DECREMENT_SQL = """
    UPDATE anonymous_preview_daily_usage
    SET count = GREATEST(count - 1, 0),
        updated_at = now()
    WHERE scope = :scope AND scope_key = :key
      AND mode = :mode AND usage_date = :day
"""


async def _try_acquire_usage_row(
    db: "AsyncSession", scope_key: str, mode: str, day: str, cap: int
) -> bool:
    """单行原子 increment-and-check（SQL 与 PgRateLimitCounterStore.try_acquire
    同构）。False = 打满或任何异常（fail-closed）。"""
    from sqlalchemy import text as _sa_text

    try:
        row = await db.execute(
            _sa_text(_USAGE_UPSERT_SQL),
            {
                "scope": ANON_PREVIEW_COUNTER_SCOPE,
                "key": scope_key,
                "mode": mode,
                "day": day,
                "cap": int(cap),
            },
        )
        return row.fetchone() is not None
    except Exception as exc:  # noqa: BLE001 — fail-closed
        logger.warning("anon_create: usage row acquire 失败 — fail-closed: %s", exc)
        return False


async def _decrement_usage_row(
    db: "AsyncSession", scope_key: str, mode: str, day: str
) -> None:
    """best-effort 回退（拒绝不落计数语义；floor 0）。"""
    from sqlalchemy import text as _sa_text

    try:
        await db.execute(
            _sa_text(_USAGE_DECREMENT_SQL),
            {
                "scope": ANON_PREVIEW_COUNTER_SCOPE,
                "key": scope_key,
                "mode": mode,
                "day": day,
            },
        )
    except Exception:  # noqa: BLE001 — 回退 best-effort
        logger.warning("anon_create: usage row decrement 失败（best-effort）")


async def _charge_retry_quota(
    db: "AsyncSession", record, record_mode: str
) -> Optional[JSONResponse]:
    """重试的配额重新计费（CodeX 外审 P1 + 复审 P2 第三条）。

    1. **per-mode 行重新取额**：若上次失败已退款（audit
       ``pass3_quota_refund == "done"``），把退掉的 per-IP/device/source
       行原子 re-acquire（cap=1）——否则重试成功后这些行仍处于退款态，
       同一匿名身份当日可再开一个 express 预览，违背 per-scope per-mode
       cap 本意。任一行打满（退款后的 slot 已被该身份的新上传用掉）→
       429。注：行沿用 intake 时落盘的 day（重试按钮典型同日；跨午夜
       边缘由 record TTL ≤ 24h 兜底）。
    2. **express 全局子闸计费**：每次重试 increment-and-check 一次——
       子闸语义 = express 付费管线启动数/日。
    3. 重新计费成功 → 清除退款幂等标记，下次再失败 mirror 可再次退款，
       账本闭环。

    返回 None = 放行；JSONResponse = 调用方回退抢占后原样返回。
    本函数不 commit——计费与后续 record 写回同事务落地；POST 失败路径的
    计费泄漏方向是少跑管线（安全侧）。
    """
    audit = dict(getattr(record, "audit", None) or {})
    recharge_rows: list[dict] = []
    if audit.get("pass3_quota_refund") == "done":
        recharge_rows = [
            r for r in (audit.get("quota_mode_rows") or []) if isinstance(r, dict)
        ]
    # per-mode 行重新取额用 admin 旋钮 cap（2026-06-13），按维度（ip/device/
    # source）各取——与 intake/peek 共用 per_mode_cap_for_scope_key 推导。
    _per_mode_caps = resolve_per_mode_caps()
    acquired: list[dict] = []
    for row in recharge_rows:
        scope_key = str(row.get("scope_key") or "")
        mode = str(row.get("mode") or "")
        day = str(row.get("day") or "")
        if not (scope_key and mode and day):
            continue
        if not await _try_acquire_usage_row(
            db, scope_key, mode, day,
            per_mode_cap_for_scope_key(scope_key, _per_mode_caps),
        ):
            for done in acquired:
                await _decrement_usage_row(
                    db, str(done["scope_key"]), str(done["mode"]), str(done["day"])
                )
            return JSONResponse(status_code=429, content={"error": "rate_limited"})
        acquired.append(row)

    if record_mode == "express":
        if not await _charge_express_retry_subgate(db):
            for done in acquired:
                await _decrement_usage_row(
                    db, str(done["scope_key"]), str(done["mode"]), str(done["day"])
                )
            return JSONResponse(
                status_code=429, content={"error": "preview_queue_full"}
            )

    if audit.get("pass3_quota_refund"):
        audit.pop("pass3_quota_refund", None)
        audit.pop("pass3_quota_refund_rows", None)
        record.audit = audit
    return None


async def _charge_express_retry_subgate(db: "AsyncSession") -> bool:
    """CodeX 外审 2026-06-12 P1：express 重试按全局子闸计费。

    intake 时子闸已为首次管线计 1；每次重试再原子 increment-and-check
    一次（SQL 形状与 PgRateLimitCounterStore.try_acquire 逐字节同构、
    key 经 express_subgate_key 单点推导）——子闸语义从「express intake
    数/日」收紧为「express 管线启动数/日」，cap 即日成本敞口上限。
    Pass 3 失败退款刻意不退本行（plan §E），重试在此重新扣减。

    返回 False = 子闸打满或任何异常（fail-closed，拒绝重试）。
    注：扣减成功后若下游 Job API POST 失败，本次扣减不回退——泄漏方向
    是少跑管线（安全侧），且 claim 回退后用户可明日再试。
    """
    from sqlalchemy import text as _sa_text

    try:
        cap = int(resolve_express_global_cap())
        day = shanghai_today()
        row = await db.execute(
            _sa_text(
                """
                INSERT INTO anonymous_preview_daily_usage
                    (id, scope, scope_key, mode, usage_date, count,
                     created_at, updated_at)
                VALUES
                    (gen_random_uuid(), :scope, :key, :mode, :day, 1,
                     now(), now())
                ON CONFLICT (scope, scope_key, mode, usage_date)
                DO UPDATE SET
                    count = anonymous_preview_daily_usage.count + 1,
                    updated_at = now()
                WHERE anonymous_preview_daily_usage.count < :cap
                RETURNING count
                """
            ),
            {
                "scope": ANON_PREVIEW_COUNTER_SCOPE,
                "key": express_subgate_key(day),
                "mode": "express",
                "day": day,
                "cap": cap,
            },
        )
        return row.fetchone() is not None
    except Exception as exc:  # noqa: BLE001 — fail-closed
        logger.warning(
            "anon_create: express retry 子闸计费失败 — fail-closed: %s", exc
        )
        return False


async def _reset_create_claim(db: "AsyncSession", preview_id: str) -> None:
    """create 下游失败时回滚原子抢占（job_id 复位 NULL，允许重试）。"""
    from sqlalchemy import update as _sa_update

    try:
        await db.execute(
            _sa_update(AnonymousPreviewRecord)
            .where(
                AnonymousPreviewRecord.preview_id == preview_id,
                AnonymousPreviewRecord.job_id == _CREATING_SENTINEL,
            )
            .values(job_id=None, claim_token_placeholder=None)
        )
        await db.commit()
    except Exception as exc:
        logger.error("anon_create: claim reset failed preview_id=%s: %s", preview_id, exc)


@router.post("/{preview_id}/create")
async def anonymous_preview_create(
    preview_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """匿名预览任务创建编排（plan AD-7/AD-8，T8b）。

    门序：CSRF → session → preview_id/record/TTL → F6 硬门(仅 READY 且未建过 job)
    → consent(strict) → 双门(free tier env + admin 热开关) → teaser 文件在场
    → 本地 ffprobe(免费) + T6 admission → in-flight gate → sentinel 用户
    → 原子抢占(job_id IS NULL) → payload 白名单 → POST Job API
    → PG Job 行(sentinel owner + is_anonymous_preview) → record 回写。
    全程任何 gate 失败 fail-closed；抢占后失败回滚抢占。
    """
    import secrets as _secrets
    from datetime import datetime, timezone as _tz

    from anonymous_consent import validate_anonymous_consent
    from anonymous_preview_payload_spec import validate_create_payload
    from anonymous_preview_probe import probe_source
    from models import Job, User
    from quota import TERMINAL_STATUSES

    # CSRF
    try:
        require_same_origin_state_change(request)
    except Exception:
        return JSONResponse(status_code=403, content={"error": "csrf_origin_rejected"})

    session_ctx = await require_anonymous_session(request, db)
    if isinstance(session_ctx, Response):
        return session_ctx
    assert isinstance(session_ctx, AnonymousSessionContext)

    safe_id = _safe_preview_id(preview_id)
    if safe_id is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})

    record = await _get_record_for_session(db, safe_id, session_ctx.session_id_hash)
    if record is None or _is_record_expired(record):
        return JSONResponse(status_code=404, content={"error": "not_found"})

    # F6 硬门：仅 READY_FOR_MODE；job_id 兼作防重放闸。
    # plan 2026-06-12 §E 重试扩展：既有 job 处于诚实失败终态（failed/
    # cancelled/已被清理）时允许 failed → 重新 create（复用 preview_id，
    # 无需重新上传）；进行中 / 成功 / 判定失败一律 409 fail-closed。
    retry_of: Optional[str] = None
    if record.job_id:
        if record.job_id == _CREATING_SENTINEL:
            return JSONResponse(status_code=409, content={"error": "already_created"})
        if not await _job_is_terminal_failed(record.job_id):
            return JSONResponse(status_code=409, content={"error": "already_created"})
        # CodeX P1：重试次数上限——retry_chain 已达上限即拒，付费管线
        # 启动次数有界（叠加下方 express 子闸计费双保险）。
        _prior_retries = len(list((record.audit or {}).get("retry_chain") or []))
        if _prior_retries >= ANON_CREATE_MAX_RETRIES:
            return JSONResponse(status_code=409, content={"error": "retry_exhausted"})
        retry_of = record.job_id
    if str(record.status) != _READY_STATUS:
        return JSONResponse(
            status_code=409,
            content={"error": "preview_not_ready", "status": _redact_reason(str(record.status))},
        )

    # consent（strict-bool 三件套；服务端盖权威时间戳）
    try:
        body = await request.json()
    except Exception:
        body = None
    consent_payload, consent_reason = validate_anonymous_consent(
        (body or {}).get("anonymous_consent") if isinstance(body, dict) else None
    )
    if consent_payload is None:
        return JSONResponse(
            status_code=403,
            content={"error": "consent_required", "reason": consent_reason},
        )
    consent_payload["server_confirmed_at"] = datetime.now(_tz.utc).isoformat()

    # 开关门按 record.mode 分流（plan 2026-06-12 §A/D2）：free 保持既有
    # 双门（enable_free_tier env + free admin flag）逐字节不变；express 查
    # anonymous_express_enabled（含 §E② mimo runtime 防御纵深）。create 是
    # 花钱动作，归新-intake 侧——lane 关掉后不再为旧 record 起新管线。
    record_mode = str(getattr(record, "mode", None) or "free")
    _mode_gate_reject = _create_mode_gate(record_mode)
    if _mode_gate_reject is not None:
        return _mode_gate_reject

    # plan 2026-06-12 §D：payload / PG mirror 按 record.mode 同步写。
    # express 的 tts_provider 在 claim 之前解析（白名单 + mimo 拒绝），
    # 解析失败提前 503，不消耗原子抢占。
    if record_mode == "express":
        _payload_tts_provider = _resolve_express_payload_tts_provider()
        if _payload_tts_provider is None:
            return JSONResponse(status_code=503, content={"error": "misconfigured"})
        _payload_service_mode = "express"
    else:
        _payload_service_mode = "free"
        _payload_tts_provider = "mimo"

    # teaser 文件在场（路径由 upload 阶段写入 audit）
    audit = dict(record.audit or {})
    teaser_path_raw = audit.get("teaser_path")
    teaser_path = Path(str(teaser_path_raw)) if teaser_path_raw else None
    if teaser_path is None or not teaser_path.is_file():
        return JSONResponse(status_code=409, content={"error": "teaser_missing"})

    # 本地 ffprobe（免费）→ T6 admission（决策值全部来自契约）
    probe = await asyncio.to_thread(probe_source, teaser_path)
    if not probe.get("ok") or not probe.get("duration_seconds"):
        return JSONResponse(status_code=409, content={"error": "teaser_unprobeable"})
    try:
        # APF 限制旋钮：admin 热值优先（字段与 settings 同名，policy 直接消费）
        admission = admit_for_free_preview(float(probe["duration_seconds"]), resolve_apf_limits())
        decision = admission.decision
        decision_str = decision.value if hasattr(decision, "value") else str(decision)
        if decision_str != "admitted":
            return JSONResponse(
                status_code=409,
                content={"error": "preview_not_admitted", "decision": decision_str},
            )
    except Exception as exc:
        logger.warning("anon_create: admission error: %s", exc)
        return JSONResponse(status_code=409, content={"error": "admission_error"})

    # in-flight gate（AD-8；fail-closed：admin 读失败按 0 容量拒绝）
    try:
        from admin_settings import load_settings as _load_admin

        max_in_flight = int(_load_admin().anonymous_preview_max_in_flight)
    except Exception:
        max_in_flight = 0
    # sentinel 系统用户（035 迁移插入；缺失=部署配置错误，fail-closed）
    sentinel_result = await db.execute(
        select(User).where(User.email == _SENTINEL_USER_EMAIL)
    )
    sentinel = sentinel_result.scalar_one_or_none()
    if sentinel is None:
        logger.error("anon_create: sentinel user missing (migration 035 not applied?)")
        return JSONResponse(status_code=503, content={"error": "misconfigured"})

    # 原子抢占：job_id IS NULL → __creating__（并发双 create 只有一个赢）。
    # 对抗审核 P1：用 RETURNING 判定胜出，不依赖 asyncpg 的 rowcount
    # （某些驱动/配置下 UPDATE 的 rowcount 不可靠 → 合法抢占被误判 409）。
    try:
        reservation = await _reserve_create_capacity(
            db,
            preview_id=safe_id,
            max_in_flight=max_in_flight,
            job_model=Job,
            terminal_statuses=list(TERMINAL_STATUSES),
        )
    except Exception as exc:
        logger.warning("anon_create: in-flight reservation failed: %s", exc)
        return JSONResponse(status_code=503, content={"error": "gate_unavailable"})
    if reservation == "queue_full":
        return JSONResponse(status_code=429, content={"error": "preview_queue_full"})
    if reservation == "already_created":
        return JSONResponse(status_code=409, content={"error": "already_created"})
    if reservation != "reserved":
        logger.error("anon_create: unexpected reservation state %s", reservation)
        return JSONResponse(status_code=503, content={"error": "gate_unavailable"})

    # CodeX 外审 2026-06-12 P1 + 复审 P2：重试配额重新计费——①退款过的
    # per-mode 行原子 re-acquire（否则重试成功后该身份当日可再开一个
    # express 预览）；②express 全局子闸 increment-and-check（付费管线
    # 启动数恒 ≤ cap）；③成功后清退款幂等标记（下次失败可再退款）。
    # 任一打满 → 抢占回退到旧 failed job_id（保留重试资格）+ 429。
    # 在 POST Job API 之前执行。
    if retry_of is not None:
        _retry_reject = await _charge_retry_quota(db, record, record_mode)
        if _retry_reject is not None:
            await _reset_create_claim_to(db, safe_id, retry_of)
            return _retry_reject

    # payload（白名单深度防御：违规字段=代码 bug，拒绝并回滚抢占）
    payload = {
        "job_type": "localize_video",
        # Job API 契约：source 是嵌套对象（api.py do_POST 读 payload["source"]
        # 的 type/value），扁平 source_type/source_ref 会 400（2026-06-11 冒烟）。
        "source": {"type": "local_video", "value": str(teaser_path)},
        # sentinel user_id（服务端注入，客户端不可达）：submit_job 只为带
        # user_id 的任务预填 workspace_dir/project_dir；缺省会走 legacy
        # stdout 捕获路径 → project_dir 被源路径污染（写一次门闩封死）→
        # stream/video 撞 "outside projects root" 400（2026-06-11 冒烟）。
        "user_id": str(sentinel.id),
        "output_target": "editor",
        # plan 2026-06-12 §D：mode 同步写（record.mode → payload → PG 行）。
        "service_mode": _payload_service_mode,
        "requires_review": False,
        # 防 clone 第一道防线：匿名任务（free 与 express 两个 lane）的
        # voice_strategy 恒为 preset_mapping，绝不出现 clone 字段。
        "voice_strategy": "preset_mapping",
        "tts_provider": _payload_tts_provider,
        "source_content_hash": record.source_hash,
        "anonymous_preview": True,
    }
    violations = validate_create_payload(payload)
    if violations:
        logger.error("anon_create: payload spec violations: %s", violations)
        await _abort_create_claim(db, safe_id, retry_of)
        return JSONResponse(status_code=500, content={"error": "payload_spec_violation"})

    # POST Job API
    try:
        client = get_client()
        create_resp = await client.post(
            f"{settings.job_api_upstream}/jobs",
            json=payload,
            headers=internal_headers(),
        )
        if create_resp.status_code not in (200, 201, 202):
            logger.error(
                "anon_create: job api returned %d", create_resp.status_code
            )
            await _abort_create_claim(db, safe_id, retry_of)
            return JSONResponse(status_code=502, content={"error": "job_create_failed"})
        job_id = str(create_resp.json().get("job_id") or "").strip()
        if not job_id:
            await _abort_create_claim(db, safe_id, retry_of)
            return JSONResponse(status_code=502, content={"error": "job_create_failed"})
    except Exception as exc:
        logger.error("anon_create: job api error: %s", exc)
        await _abort_create_claim(db, safe_id, retry_of)
        return JSONResponse(status_code=502, content={"error": "job_create_failed"})

    # Keep the capacity reservation visible until the PG Job row exists.
    try:
        db.add(
            Job(
                job_id=job_id,
                user_id=sentinel.id,
                source_type="local_video",
                source_ref=str(teaser_path),
                source_content_hash=record.source_hash,
                title="匿名预览",
                speakers="auto",
                status="queued",
                # §D：与 create payload 同值（record/payload/PG 三处一致）；
                # sentinel / plan_code_snapshot / role_snapshot 语义不变。
                service_mode=_payload_service_mode,
                tts_provider=_payload_tts_provider,
                requires_review=False,
                voice_clone_enabled=False,
                voice_strategy="preset_mapping",
                plan_code_snapshot="free",
                role_snapshot="user",
                is_anonymous_preview=True,
            )
        )
        await db.commit()
    except Exception as exc:
        logger.critical(
            "anon_create: PG Job row insert failed job=%s; record remains reserved "
            "for capacity accounting and needs manual reconciliation: %s",
            job_id,
            exc,
        )
        return JSONResponse(
            status_code=500,
            content={"error": "persist_failed"},
        )

    # PG Job row is now durable, so exposing the real job_id no longer creates
    # a capacity-accounting gap between the record and the Job mirror row.
    try:
        fresh = await _get_record_for_session(db, safe_id, session_ctx.session_id_hash)
        if fresh is not None:
            fresh.job_id = job_id
            fresh.claim_token_placeholder = _secrets.token_urlsafe(16)
            merged = dict(fresh.audit or {})
            merged["anonymous_consent"] = consent_payload
            if retry_of is not None:
                retry_chain = list(merged.get("retry_chain") or [])
                retry_chain.append(retry_of)
                merged["retry_chain"] = retry_chain
            fresh.audit = merged
        await db.commit()
    except Exception as exc:
        # job 已在 Job API 跑、record 仍是 __creating__：不回滚抢占（重试
        # 会双建任务）；status 端点的 creating 分支可见此态，留 TTL 清理。
        logger.critical(
            "anon_create: record 回写失败 job=%s — record 滞留 __creating__, "
            "需人工核对: %s", job_id, exc,
        )
        return JSONResponse(status_code=500, content={"error": "persist_failed"})

    return JSONResponse(
        status_code=202,
        content={"preview_id": safe_id, "status": "processing"},
    )
