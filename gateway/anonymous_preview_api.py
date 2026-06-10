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
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from anonymous_preview_policy import (
    FreePreviewAdmissionResult,
    StreamGate,
    admit_for_free_preview,
    stream_gate_from_artifact_policy,
)
from anonymous_preview_probe import build_probe_fn
from anonymous_preview_prescreen import prescreen_filename
from anonymous_preview_record_store import RecordStoreError
from anonymous_preview_upload import UploadRejected, UploadTooLarge, handle_anonymous_upload, extract_client_ip
from anonymous_preview_intake_wiring import run_intake_and_save
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
    """Fetch a record matching both preview_id and session_id (ownership check)."""
    result = await db.execute(
        select(AnonymousPreviewRecord).where(
            AnonymousPreviewRecord.preview_id == preview_id,
            AnonymousPreviewRecord.session_id == session_id_hash,
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
    """Read admin anonymous_free_preview_enabled — fail-closed on any error."""
    try:
        from admin_settings import load_settings as _load
        return bool(_load().anonymous_free_preview_enabled)
    except Exception:
        return False


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

    admin_enabled = _get_admin_enabled()

    # Stream upload to disk
    upload_path: Optional[Path] = None
    try:
        upload_path, source_hash, size_bytes = await handle_anonymous_upload(
            request=request,
            session_hash=session_ctx.session_id_hash,
            flag_enabled=settings.enable_anonymous_preview,
            admin_enabled=admin_enabled,
            max_upload_bytes=settings.anonymous_preview_max_upload_bytes,
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

    # Probe fn (T4) and prescreen fn (T5)
    _probe_fn = build_probe_fn(settings)

    def _prescreen_fn(probe_result) -> object:  # noqa: ANN001
        # T5: local-rules prescreen by filename (synchronous stdlib, no paid calls)
        filename = upload_path.name if upload_path else ""
        return prescreen_filename(filename)

    # Build adapter facts — field names from
    # src.services.anonymous_preview_backend_adapter.RequestFacts / UploadFacts
    from src.services.anonymous_preview_backend_adapter import RequestFacts, UploadFacts
    from src.services.anonymous_preview_intake import SourceType
    from anonymous_preview_quota import shanghai_today

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
                return run_intake_and_save(
                    db_session=sync_db,
                    request_facts=request_facts,
                    upload_facts=upload_facts,
                    probe_fn=_probe_fn,
                    prescreen_fn=_prescreen_fn,
                )
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
        from anonymous_preview_probe import teaser_dest_for

        _row_result = await db.execute(
            select(AnonymousPreviewRecord).where(
                AnonymousPreviewRecord.preview_id == record.record_id
            )
        )
        _orm_row = _row_result.scalar_one_or_none()
        if _orm_row is not None and upload_path is not None:
            _merged_audit = dict(_orm_row.audit or {})
            _merged_audit["stored_upload_path"] = str(upload_path)
            _merged_audit["teaser_path"] = str(teaser_dest_for(upload_path))
            _orm_row.audit = _merged_audit
            await db.commit()
    except Exception as exc:
        logger.warning("anon_upload: audit path persist failed: %s", exc)

    # Admission (T6 thin adapter) — use the record's teaser duration if available
    admission: Optional[FreePreviewAdmissionResult] = None
    try:
        teaser_dur = getattr(record, "teaser_duration_seconds", None) or 0.0
        admission = admit_for_free_preview(teaser_dur, settings)
    except Exception as exc:
        logger.warning("anon_upload: admit_for_free_preview error: %s", exc)

    admission_decision = None
    if admission is not None:
        d = admission.decision
        admission_decision = d.value if hasattr(d, "value") else str(d)

    record_status = record.status
    status_str = record_status.value if hasattr(record_status, "value") else str(record_status)

    return JSONResponse(
        status_code=200,
        content={
            "preview_id": record.record_id,
            "status": status_str,
            "status_reason": _redact_reason(record.status_reason),
            "mode": "free",
            "admission_decision": admission_decision,
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

    # Admin gate (re-check at stream time — hot-switch must take effect)
    if not _get_admin_enabled():
        return JSONResponse(status_code=403, content={"error": "anonymous_preview_disabled"})

    # T6 stream gate
    try:
        teaser_dur = getattr(record, "teaser_duration_seconds", 0.0) or 0.0
        admission = admit_for_free_preview(teaser_dur, settings)
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
            async for chunk in upstream_response.aiter_bytes(chunk_size=65536):
                yield chunk
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
_SENTINEL_USER_EMAIL = "anonymous-preview@system"
_READY_STATUS = "ready_for_mode"  # PreviewStatus.READY_FOR_MODE.value（契约钉死）


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
            .values(job_id=None)
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

    from sqlalchemy import func, update as _sa_update

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

    # F6 硬门：仅 READY_FOR_MODE 且未建过 job（job_id 兼作防重放闸）
    if record.job_id:
        return JSONResponse(status_code=409, content={"error": "already_created"})
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

    # 双门（AD-7）：匿名 surface 不绕过 free tier 总开关语义
    if not getattr(settings, "enable_free_tier", False):
        return JSONResponse(status_code=403, content={"error": "free_disabled"})
    if not _get_admin_enabled():
        return JSONResponse(status_code=403, content={"error": "anonymous_preview_disabled"})

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
        admission = admit_for_free_preview(float(probe["duration_seconds"]), settings)
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
    try:
        cnt_result = await db.execute(
            select(func.count())
            .select_from(Job)
            .where(
                Job.is_anonymous_preview.is_(True),
                Job.status.notin_(list(TERMINAL_STATUSES)),
            )
        )
        in_flight = int(cnt_result.scalar() or 0)
    except Exception as exc:
        logger.warning("anon_create: in-flight count failed: %s", exc)
        return JSONResponse(status_code=503, content={"error": "gate_unavailable"})
    if in_flight >= max_in_flight:
        return JSONResponse(status_code=429, content={"error": "preview_queue_full"})

    # sentinel 系统用户（035 迁移插入；缺失=部署配置错误，fail-closed）
    sentinel_result = await db.execute(
        select(User).where(User.email == _SENTINEL_USER_EMAIL)
    )
    sentinel = sentinel_result.scalar_one_or_none()
    if sentinel is None:
        logger.error("anon_create: sentinel user missing (migration 035 not applied?)")
        return JSONResponse(status_code=503, content={"error": "misconfigured"})

    # 原子抢占：job_id IS NULL → __creating__（并发双 create 只有一个赢）
    claim = await db.execute(
        _sa_update(AnonymousPreviewRecord)
        .where(
            AnonymousPreviewRecord.preview_id == safe_id,
            AnonymousPreviewRecord.job_id.is_(None),
        )
        .values(job_id=_CREATING_SENTINEL)
    )
    await db.commit()
    if getattr(claim, "rowcount", 0) != 1:
        return JSONResponse(status_code=409, content={"error": "already_created"})

    # payload（白名单深度防御：违规字段=代码 bug，拒绝并回滚抢占）
    payload = {
        "job_type": "localize_video",
        "source_type": "local_video",
        "source_ref": str(teaser_path),
        "output_target": "editor",
        "service_mode": "free",
        "requires_review": False,
        "voice_strategy": "preset_mapping",
        "tts_provider": "mimo",
        "source_content_hash": record.source_hash,
        "anonymous_preview": True,
    }
    violations = validate_create_payload(payload)
    if violations:
        logger.error("anon_create: payload spec violations: %s", violations)
        await _reset_create_claim(db, safe_id)
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
            await _reset_create_claim(db, safe_id)
            return JSONResponse(status_code=502, content={"error": "job_create_failed"})
        job_id = str(create_resp.json().get("job_id") or "").strip()
        if not job_id:
            await _reset_create_claim(db, safe_id)
            return JSONResponse(status_code=502, content={"error": "job_create_failed"})
    except Exception as exc:
        logger.error("anon_create: job api error: %s", exc)
        await _reset_create_claim(db, safe_id)
        return JSONResponse(status_code=502, content={"error": "job_create_failed"})

    # PG Job 行（sentinel owner + 标记列；mirror 的结算 bypass 靠它识别）
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
                service_mode="free",
                tts_provider="mimo",
                requires_review=False,
                voice_clone_enabled=False,
                voice_strategy="preset_mapping",
                plan_code_snapshot="free",
                role_snapshot="user",
                is_anonymous_preview=True,
            )
        )
        # record 回写：真实 job_id + claim token 占位 + consent 审计
        fresh = await _get_record_for_session(db, safe_id, session_ctx.session_id_hash)
        if fresh is not None:
            fresh.job_id = job_id
            fresh.claim_token_placeholder = _secrets.token_urlsafe(16)
            merged = dict(fresh.audit or {})
            merged["anonymous_consent"] = consent_payload
            fresh.audit = merged
        await db.commit()
    except Exception as exc:
        # Job API 已建任务但 PG 行/record 写失败：不回滚抢占（job 已存在，
        # 重放会双建）；记录错误，orphan reconciliation 与 status 端点
        # 仍可经 record.job_id==__creating__ 暴露异常态供排障。
        logger.error("anon_create: post-create persist failed job=%s: %s", job_id, exc)
        return JSONResponse(status_code=500, content={"error": "persist_failed"})

    return JSONResponse(
        status_code=202,
        content={"preview_id": safe_id, "status": "processing"},
    )
