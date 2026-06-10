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
