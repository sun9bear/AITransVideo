"""APF P0 — anonymous video upload handler (AD-8).

Handles the "read body AFTER cheap pre-checks" pattern for anonymous
preview uploads.  Responsibilities:

1. **Cheap pre-checks before reading body** (flag off / admin off /
   no session / Content-Length over limit → immediate 4xx, zero bytes
   read).
2. **Stream to disk** under ``uploads/anonymous/{session_hash}/
   {upload_id}_{safe_name}`` with a hard byte truncation and a
   streaming sha256 so we never hold the full file in memory.
3. **Return** ``(local_path, source_hash, size_bytes)`` on success.
4. **Clean up** any partial file on any non-success exit (exception,
   size exceeded mid-stream, etc.).

CSRF protection is handled at the router layer (T7 dependency), not
here.  IP extraction is done via the same trusted-proxy helper used by
``gateway/auth_phone.py``.

Import constraints
------------------
* No ``services.jobs`` or ``src.pipeline`` import — gateway container
  does not have pydub (see CLAUDE.md).
* No ``fastapi`` import — this is a pure async helper; the router wires
  FastAPI types.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path sanitisation (mirrors gateway/upload.py)
# ---------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9_\-]")
_UNSAFE_FILENAME_CHARS = re.compile(r"[^a-zA-Z0-9_\-.]")


def _safe_segment(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return "anon"
    result = _UNSAFE_CHARS.sub("_", cleaned)
    while ".." in result:
        result = result.replace("..", "_")
    return result[:64]  # cap segment length


def _sanitize_filename(filename: str) -> str:
    cleaned = filename.strip()
    if not cleaned:
        return "unnamed"
    result = _UNSAFE_FILENAME_CHARS.sub("_", cleaned)
    while ".." in result:
        result = result.replace("..", "_")
    return result[:128]  # cap filename length


# ---------------------------------------------------------------------------
# Project-root resolver (mirrors gateway/upload.py)
# ---------------------------------------------------------------------------

def _resolve_project_root() -> Path:
    return Path(
        os.environ.get("AIVIDEOTRANS_PROJECTS_DIR", "")
        or os.environ.get("AIVIDEOTRANS_PROJECT_ROOT", "")
        or "/opt/aivideotrans/app"
    ).resolve(strict=False)


# ---------------------------------------------------------------------------
# IP extraction (trusted-proxy pattern from auth_phone.py:147-190)
# ---------------------------------------------------------------------------

_TRUSTED_PROXIES_ENV = "AVT_TRUSTED_PROXIES"
_DEFAULT_TRUSTED_PROXIES = frozenset({"127.0.0.1", "::1", "localhost"})


def _trusted_proxies() -> frozenset:
    raw = os.environ.get(_TRUSTED_PROXIES_ENV, "").strip()
    if not raw:
        return _DEFAULT_TRUSTED_PROXIES
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def extract_client_ip(request: object) -> Optional[str]:
    """Resolve the requester IP with a trusted-proxy boundary.

    Mirrors ``gateway/auth_phone.py:_client_ip`` exactly:
    * Only trust CF-Connecting-IP / X-Forwarded-For / X-Real-IP when
      the immediate socket peer is in the trusted allowlist.
    * Otherwise fall back to the socket peer.

    ``request`` is typed as ``object`` so callers can pass any
    Request-like object without coupling this module to FastAPI.
    """
    # Duck-type: works with starlette Request and test fakes alike.
    client = getattr(request, "client", None)
    socket_peer: Optional[str] = client.host if client is not None else None
    headers = getattr(request, "headers", {})
    trusted = _trusted_proxies()
    if socket_peer and socket_peer in trusted:
        cf = headers.get("cf-connecting-ip")
        if cf:
            return cf.strip() or socket_peer
        fwd = headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip() or socket_peer
        real = headers.get("x-real-ip")
        if real:
            return real.strip() or socket_peer
    return socket_peer


# ---------------------------------------------------------------------------
# Pre-check exceptions (structured, not HTTPException — router maps them)
# ---------------------------------------------------------------------------

class UploadRejected(Exception):
    """Raised by pre-checks before any body is read.

    ``status_code`` mirrors the HTTP status the router should return.
    ``reason_code`` is a short ASCII string for structured log / metric.
    """

    def __init__(self, status_code: int, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.reason_code = reason_code
        self.detail = detail


class UploadTooLarge(Exception):
    """Raised mid-stream when the body exceeds the byte limit.

    Callers must delete any partial file on catching this.
    """

    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"upload exceeded {limit_bytes} bytes")
        self.limit_bytes = limit_bytes


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

async def handle_anonymous_upload(
    *,
    request: object,
    session_hash: Optional[str],
    flag_enabled: bool,
    admin_enabled: bool,
    max_upload_bytes: int,
) -> Tuple[Path, str, int]:
    """Receive a raw binary body upload for anonymous preview.

    Parameters
    ----------
    request:
        A Starlette/FastAPI ``Request``-compatible object.  Must expose
        ``.headers``, ``.client``, and async ``.body()`` / ``stream()``.
    session_hash:
        The HMAC-hashed anonymous session token (used as directory
        segment to isolate uploads per session).  ``None`` → 401.
    flag_enabled:
        ``settings.enable_anonymous_preview`` value.  ``False`` → 404.
    admin_enabled:
        ``admin_settings.anonymous_free_preview_enabled`` value.  ``False`` → 404.
    max_upload_bytes:
        ``settings.anonymous_preview_max_upload_bytes``.

    Returns
    -------
    ``(local_path, source_hash_hex, size_bytes)``

    Raises
    ------
    ``UploadRejected``
        On any cheap pre-check failure (flag off / no session /
        Content-Length over limit).
    ``UploadTooLarge``
        If the body exceeds ``max_upload_bytes`` mid-stream.  Callers
        must delete any partial file.
    ``OSError``
        On filesystem write failure.
    """
    # ------------------------------------------------------------------
    # Cheap pre-checks — no body read yet
    # ------------------------------------------------------------------
    if not flag_enabled or not admin_enabled:
        raise UploadRejected(
            status_code=404,
            reason_code="flag_disabled",
            detail="Anonymous preview is not available",
        )

    if not session_hash:
        raise UploadRejected(
            status_code=401,
            reason_code="session_missing",
            detail="Anonymous session required",
        )

    headers = getattr(request, "headers", {})
    raw_cl = headers.get("content-length")
    if raw_cl is not None:
        try:
            cl = int(raw_cl)
        except (ValueError, TypeError):
            cl = 0
        if cl > max_upload_bytes:
            logger.warning(
                "anon_upload_rejected reason=content_length_exceeded "
                "content_length=%d limit=%d session_hash=%.8s",
                cl,
                max_upload_bytes,
                session_hash,
            )
            raise UploadRejected(
                status_code=413,
                reason_code="content_length_exceeded",
                detail=f"File too large (max {max_upload_bytes // (1024 * 1024)} MB)",
            )

    # ------------------------------------------------------------------
    # Build output path: uploads/anonymous/{session_hash}/{upload_id}_{name}
    # ------------------------------------------------------------------
    # Derive a filename from Content-Disposition or fall back.
    filename = _get_filename_from_headers(headers) or "upload.mp4"
    upload_id = uuid.uuid4().hex[:12]
    project_root = _resolve_project_root()

    session_seg = _safe_segment(session_hash)
    safe_name = _sanitize_filename(filename)
    output_path = (
        project_root / "uploads" / "anonymous" / session_seg
        / f"{upload_id}_{safe_name}"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Stream body to disk with hard truncation + streaming sha256
    # ------------------------------------------------------------------
    digest = hashlib.sha256()
    bytes_written = 0
    partial_file_created = False

    try:
        with open(output_path, "wb") as out:
            partial_file_created = True
            # Starlette exposes .stream() as an async generator of bytes.
            # Fall back to single .body() read for test fakes that don't
            # implement an async generator.
            stream_fn = getattr(request, "stream", None)
            if stream_fn is not None and asyncio.iscoroutinefunction(
                getattr(stream_fn, "__call__", None)
            ):
                # stream() is a sync method returning an async generator
                async for chunk in request.stream():  # type: ignore[union-attr]
                    bytes_written += len(chunk)
                    if bytes_written > max_upload_bytes:
                        raise UploadTooLarge(max_upload_bytes)
                    digest.update(chunk)
                    await asyncio.to_thread(out.write, chunk)
            else:
                # Fallback: read body as bytes (test fakes / coroutine)
                body_fn = getattr(request, "body", None)
                if asyncio.iscoroutinefunction(body_fn):
                    body: bytes = await request.body()  # type: ignore[union-attr]
                else:
                    body = request.body  # type: ignore[assignment]
                    if callable(body):
                        body = body()

                if len(body) > max_upload_bytes:
                    raise UploadTooLarge(max_upload_bytes)
                bytes_written = len(body)
                digest.update(body)
                await asyncio.to_thread(out.write, body)

    except UploadTooLarge:
        _safe_delete(output_path)
        logger.warning(
            "anon_upload_rejected reason=body_exceeded limit=%d "
            "bytes_read=%d session_hash=%.8s",
            max_upload_bytes,
            bytes_written,
            session_hash,
        )
        raise
    except Exception:
        if partial_file_created:
            _safe_delete(output_path)
        raise

    if bytes_written == 0:
        _safe_delete(output_path)
        raise UploadRejected(
            status_code=400,
            reason_code="empty_body",
            detail="Uploaded file is empty",
        )

    source_hash = digest.hexdigest()
    logger.info(
        "anon_upload_ok path=%s size=%d hash=%.16s session_hash=%.8s",
        output_path,
        bytes_written,
        source_hash,
        session_hash,
    )
    return output_path, source_hash, bytes_written


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_filename_from_headers(headers: object) -> Optional[str]:
    """Try to extract a filename from Content-Disposition."""
    cd = headers.get("content-disposition", "") if hasattr(headers, "get") else ""
    if not cd:
        return None
    # filename="foo.mp4" or filename*=UTF-8''foo.mp4
    m = re.search(r'filename\*?=["\']?([^"\';\r\n]+)["\']?', cd)
    if m:
        name = m.group(1).strip()
        if name:
            return name
    return None


def _safe_delete(path: Path) -> None:
    """Delete ``path`` without raising — best-effort cleanup."""
    try:
        path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.debug("anon_upload: failed to delete partial file %s", path)
