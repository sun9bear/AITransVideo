"""Cloudflare R2 S3-compatible client helpers (plan 2026-04-23).

Scope
-----
Thin wrapper around ``boto3.client("s3", ...)`` pointed at a Cloudflare R2
endpoint. Provides three helpers that ``backend_router`` composes:

- :func:`head_artifact`  — "is this key already in R2?"
- :func:`upload_artifact` — lazy upload when the first user requests a key
- :func:`generate_presigned_download_url` — sign a short-lived GET URL with
  ``ResponseContentDisposition`` set so the browser saves a friendly filename.

Design notes
------------
- **Lazy singleton client.** boto3 clients are documented thread-safe, so we
  build one on first use and keep it for the process lifetime. A module-level
  lock guards the creation race. We rebuild on demand if an explicit test
  reset hook is called (:func:`_reset_for_tests`).
- **Credentials come from settings.** We never read env vars directly here —
  ``gateway.config.settings`` already resolves ``R2_*`` (non-AVT prefix, per
  plan §4) into typed fields. Tests can monkeypatch the settings object.
- **No fallback logic lives here.** This module either returns a result or
  raises. The caller (``backend_router.resolve_download_target``) decides
  whether to downgrade to the local byte-passthrough.
- **Tight timeouts.** Connect timeout 10s; read/write timeout comes from
  ``settings.r2_upload_timeout_s`` (default 60s). The Gateway must not hold a
  user download request while R2 is degraded — fall through to local quickly.
- **No retries anywhere (``max_attempts=1``).** boto3's default retry (up to
  5 attempts on some error classes) is harmful for *every* op we run: HEAD /
  presign blow the fallback budget on slow R2, and upload retry buys only
  marginal reliability at the cost of user-perceived wait — if the first
  PUT fails, streaming the local bytes back to the browser is strictly
  better UX than making the user wait for a second R2 attempt. Fallback to
  local is the safety net; retries are not.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote as _urlquote

from config import settings

if TYPE_CHECKING:
    # boto3 is a runtime dep (see gateway/requirements.txt). Guarding the
    # type-only import keeps mypy happy even if a dev machine hasn't run
    # `pip install -r gateway/requirements.txt` yet.
    from botocore.client import BaseClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Client singleton
# ---------------------------------------------------------------------------

_client_lock = threading.Lock()
_client: "BaseClient | None" = None


def _reset_for_tests() -> None:
    """Drop the cached client. Used by pytest fixtures that monkeypatch
    credentials — the next ``_get_client()`` call rebuilds with new values.
    """
    global _client
    with _client_lock:
        _client = None


def _get_client() -> "BaseClient":
    """Return the process-wide boto3 s3 client, building it on first call.

    Raises:
        RuntimeError: if any required R2 credential is missing. The startup
            validator (``startup_checks.validate_r2_backend``) already
            prevents this by downgrading to ``local``, so hitting this
            branch means the caller bypassed the gate.
    """
    global _client
    if _client is not None:
        return _client

    # Import here rather than at module import time so that environments
    # without boto3 installed (e.g. partial test harnesses) can still import
    # the module for non-R2 code paths.
    import boto3  # type: ignore[import-untyped]
    from botocore.client import Config  # type: ignore[import-untyped]

    if not (settings.r2_endpoint and settings.r2_access_key_id and settings.r2_secret_access_key):
        raise RuntimeError(
            "r2_client called but R2 credentials are not configured. "
            "validate_r2_backend should have downgraded to local."
        )

    with _client_lock:
        # Double-checked lock — another thread may have raced us here.
        if _client is not None:
            return _client

        # Connect timeout is intentionally tight (10s). Read timeout is the
        # configured upload budget — HEAD / presign complete in ms so this
        # only matters for upload.
        #
        # ``max_attempts=1`` = total attempts = 1, i.e. **no retries**. This
        # is the single retry policy for every op (HEAD / PUT / presign) per
        # the module docstring. Fallback to local byte-passthrough is the
        # safety net; retrying R2 would only delay the user.
        config = Config(
            signature_version="s3v4",
            region_name="auto",
            connect_timeout=10,
            read_timeout=settings.r2_upload_timeout_s,
            retries={"max_attempts": 1, "mode": "standard"},
        )
        _client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            config=config,
        )
        return _client


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def head_artifact(key: str) -> bool:
    """Return ``True`` if ``key`` exists in the artifacts bucket, else ``False``.

    Returns ``False`` on a clean 404 so the caller can lazy-upload. Any other
    error (network / 5xx / auth) raises — the caller (``backend_router``)
    catches generic ``Exception`` and downgrades to the local byte path.
    """
    from botocore.exceptions import ClientError  # type: ignore[import-untyped]

    client = _get_client()
    try:
        client.head_object(Bucket=settings.r2_artifacts_bucket, Key=key)
        return True
    except ClientError as exc:
        # R2 returns 404 on missing keys. boto3 maps this to
        # error["Error"]["Code"] == "404" (not "NoSuchKey" for HEAD).
        code = exc.response.get("Error", {}).get("Code") if exc.response else None
        status = (
            exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if exc.response else None
        )
        if code in ("404", "NoSuchKey", "NotFound") or status == 404:
            return False
        raise


def upload_artifact(
    local_path: Path,
    key: str,
    content_type: str = "video/mp4",
) -> None:
    """Upload ``local_path`` to ``bucket/key`` via a single ``put_object`` call.

    We prefer ``put_object`` over ``upload_file`` / multipart because:
    - Current dubbed_video artifacts are <1 GB — well under the 5 GB single-
      part limit.
    - A single request is easier to time-box and to trace in logs.
    - Multipart adds complexity for negligible wins at this file size.

    ``content_type`` defaults to ``video/mp4`` to preserve the legacy
    lazy-upload call site (``backend_router._upload_with_lock``) that has
    only ever pushed ``publish.dubbed_video``. The proactive publisher
    (plan 2026-05-07 §4.4) derives the value per artifact_key via
    ``r2_publisher_lib.downloadable_keys.content_type_for`` so subtitles
    land as ``text/plain`` and zips as ``application/zip``.

    Raises the underlying boto3 exception on failure. Caller handles fallback.
    """
    client = _get_client()
    # We intentionally open the file in binary read mode (not read-all into
    # memory). boto3 streams it to R2, so a 500 MB file never inflates RSS.
    with local_path.open("rb") as fh:
        client.put_object(
            Bucket=settings.r2_artifacts_bucket,
            Key=key,
            Body=fh,
            ContentType=content_type,
        )


def generate_presigned_download_url(
    key: str,
    download_filename: str,
    content_type: str = "video/mp4",
) -> str:
    """Sign a short-lived GET URL for ``key``.

    The signed URL embeds ``ResponseContentDisposition=attachment; filename=...``
    so browsers save a stable filename even though the R2 object key is an
    opaque ``jobs/{job_id}/publish.dubbed_video`` path. Per the plan lock-in
    decision (§10 item 2), this is mandatory — without it the saved filename
    leaks the internal key structure and user-facing downloads look like
    ``publish.dubbed_video`` with no extension.

    ``content_type`` defaults to ``video/mp4`` for the legacy lazy-upload
    code path. The new registry-based download path (plan 2026-05-07 §4.7)
    passes the real content_type stored in the per-artifact registry so
    SRT files come back as ``text/plain`` and the browser picks the right
    Save-As behaviour.

    TTL is ``settings.r2_presigned_expires_s`` (default 120s = 2 min). Short
    on purpose: the browser follows the 302 immediately; a leaked URL expires
    before it can be meaningfully replayed.
    """
    client = _get_client()
    # RFC 6266 / 5987 UTF-8 friendly filename. boto3 passes this through to
    # the signed URL query param verbatim, so percent-encode non-ASCII and
    # quote bytes to avoid header-injection surprises.
    quoted_ascii = _ascii_fallback_filename(download_filename)
    quoted_utf8 = _urlquote(download_filename, safe="")
    content_disposition = (
        f'attachment; filename="{quoted_ascii}"; '
        f"filename*=UTF-8''{quoted_utf8}"
    )
    return client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.r2_artifacts_bucket,
            "Key": key,
            "ResponseContentDisposition": content_disposition,
            "ResponseContentType": content_type,
        },
        ExpiresIn=settings.r2_presigned_expires_s,
    )


def generate_presigned_stream_url(
    key: str,
    content_type: str = "video/mp4",
    expires_s: int | None = None,
) -> str:
    """Sign a long-lived GET URL for in-browser media streaming.

    Plan 2026-05-07 §11.3 C3 + CodeX P1/P2 follow-up (2026-05-12):
    ``<video src=...>`` and ``<audio src=...>`` players behave very
    differently from ``<a download>`` downloads:

    1. **TTL has to be long.** Players issue multiple Range requests
       over the full playback window — pause / resume / seek can
       re-fetch the same URL minutes apart. The 120s default from
       ``generate_presigned_download_url`` would 403 mid-playback on
       any video longer than 2 min. Stream presign defaults to
       ``settings.r2_stream_presigned_expires_s`` (1800s = 30 min) so
       a typical workspace play / pause / scrub session stays within
       a single signature window.

    2. **Disposition has to be inline.** With
       ``attachment; filename=...`` (the download helper) browsers
       try to save instead of play, breaking the in-page player.
       Stream presign omits filename and uses no
       ``Content-Disposition`` override; R2 then serves the object
       with whatever default header the bucket / object metadata has.

    3. **No filename param needed.** Stream URLs never reach the
       user's Save-As dialog; the only path that produces a saved
       filename is /download/{key}, which keeps using the download
       helper. Skipping ``ResponseContentDisposition`` here also
       removes one place where Save-As filenames could drift from
       the download path.

    Caller passes ``content_type`` from the registry entry so the
    response carries the right MIME (``video/mp4`` /
    ``audio/wav`` / ``image/jpeg``).
    """
    if expires_s is None:
        expires_s = getattr(settings, "r2_stream_presigned_expires_s", 1800)
    client = _get_client()
    return client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.r2_artifacts_bucket,
            "Key": key,
            "ResponseContentType": content_type,
        },
        ExpiresIn=expires_s,
    )


def _ascii_fallback_filename(name: str) -> str:
    """Best-effort ASCII rendering of ``name`` for the legacy ``filename=``
    directive. Non-ASCII characters become ``_`` so that user agents that
    ignore ``filename*=`` still save *something*, not an empty string.

    Any backslash or double-quote is stripped to prevent header breakage.
    """
    cleaned = []
    for ch in name:
        if ch in ('"', "\\", "\r", "\n"):
            continue
        if ord(ch) < 0x80:
            cleaned.append(ch)
        else:
            cleaned.append("_")
    result = "".join(cleaned).strip()
    return result or "download.mp4"
