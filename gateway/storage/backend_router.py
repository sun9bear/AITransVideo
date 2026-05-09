"""Download-backend routing (plan 2026-04-23 §3).

Scope
-----
Decides whether a given artifact download should be served by:

- **R2 redirect** — ``settings.download_redirect_backend == "r2"`` AND the
  object is (or can be made) present in R2. Returns a 302 target URL.
- **Local passthrough** — anything else. Returns ``None`` and the caller
  keeps the existing Gateway -> Job API byte-streaming path.

This module is the single place where "is R2 really going to serve this
request?" is decided. Keeping that decision here — rather than scattered
through ``job_intercept.py`` — means the fallback contract is testable in
isolation and the R2 error-handling rule (see below) only lives in one
function.

Fallback contract (user constraint, non-negotiable)
--------------------------------------------------
Any exception raised by R2 client helpers (HEAD / upload / presign) **must**
result in a ``None`` return from this router so the Gateway falls back to
local byte-passthrough. The user never sees an error just because R2 is
degraded. We log at WARNING (not ERROR) because the user-visible path still
works; CRITICAL is reserved for startup-time misconfiguration.

Phase 2 scope
-------------
The only artifact key wired through this router is ``publish.dubbed_video``.
Any other key ignored here (caller's responsibility to limit the surface).
"""

from __future__ import annotations

import logging
import hashlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from config import settings

if TYPE_CHECKING:
    # Avoid a hard dep from the router module onto the Gateway DB session.
    # The router itself never touches the DB — callers pass in the pre-
    # resolved local file path.
    pass

# Make src/ importable (same pattern as gateway/job_intercept.py) so we can
# call the shared file-lock helper that protects concurrent lazy uploads.
for _candidate in [
    Path(__file__).resolve().parent.parent.parent / "src",
    Path("/opt/aivideotrans/app/src"),
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_r2_enabled() -> bool:
    """Cheap gate for the caller: "should I even try the R2 path?"

    The effective backend is written back to ``settings`` at startup by
    ``validate_r2_backend`` (see ``startup_checks.py``), so reading the
    setting at request time is always safe — it already reflects the
    post-downgrade truth.
    """
    return settings.download_redirect_backend == "r2"


def r2_key_for(
    job_id: str,
    artifact_key: str,
    *,
    local_path: Path | None = None,
    edit_generation: int | None = None,
) -> str:
    """Compose the canonical R2 object key for a (job, artifact) pair.

    Two shapes (plan 2026-05-07 §4.4):

    - ``edit_generation is None`` (legacy lazy-upload path):
      ``jobs/{job_id}/{artifact_key}{suffix}``
      — Used by ``_upload_with_lock`` so existing R2 objects stay
        addressable. Only ``publish.dubbed_video`` ever takes this path,
        and the download intercept (``_resolve_r2_redirect``) restricts it
        to ``edit_generation == 0`` so an overwrite cannot be served from
        a stale object.

    - ``edit_generation is int`` (proactive publisher path, plan §4.4):
      ``jobs/{job_id}/g{N}/{artifact_key}{suffix}``
      — Used by ``r2_publisher_lib.r2_publisher.publish_artifacts`` so each
        edit generation lands on a physically distinct R2 key. Overwrites
        cannot HEAD-hit the previous generation's object.

    The two shapes are deliberately disjoint — a publish.dubbed_video
    pushed via the legacy path under shape A and the same artifact pushed
    by the publisher under shape B coexist as separate R2 objects until
    R2 lifecycle policy eventually retires the older one.
    """
    suffix = local_path.suffix if local_path is not None else ""
    if edit_generation is None:
        base = f"jobs/{job_id}/{artifact_key}"
    else:
        base = f"jobs/{job_id}/g{edit_generation}/{artifact_key}"
    if suffix and not artifact_key.endswith(suffix):
        return f"{base}{suffix}"
    return base


def resolve_download_target(
    *,
    job_id: str,
    artifact_key: str,
    local_path: Path,
    download_filename: str,
) -> str | None:
    """Resolve the eventual 302 target URL for an R2-backed download.

    Parameters
    ----------
    job_id
        Gateway-level job identifier. Used only to compose the R2 key.
    artifact_key
        Artifact slug (e.g. ``"publish.dubbed_video"``). Used in the R2 key.
    local_path
        Already-resolved absolute path to the artifact on the Gateway
        container's filesystem. Caller is responsible for making sure it
        exists — if the file is missing we return ``None`` and the caller
        passes through to Job API (which then returns its own 404).
    download_filename
        User-facing filename embedded in the presigned URL's
        ``ResponseContentDisposition`` header so the browser saves a
        recognisable name.

    Returns
    -------
    str | None
        Signed URL on success; ``None`` to indicate "fall back to local
        byte-passthrough". Never raises.

    Flow
    ----
    1. If R2 is not enabled in settings → ``None`` (cheap guard).
    2. HEAD the object.
       - Present: skip to step 4.
       - Missing (404) AND local file exists: lazy-upload (guarded by a
         per-key file lock so concurrent first-downloads don't each push
         the same bytes) → step 4.
       - Missing AND local file also missing: ``None`` (Job API will 404).
    3. Anything else (network / auth / 5xx) → ``None`` (fallback).
    4. Sign the URL and return it.
    """
    if not is_r2_enabled():
        return None

    # Import lazily so the non-R2 code path never imports boto3. This keeps
    # the default container (no AVT_DOWNLOAD_REDIRECT_BACKEND=r2) cheaper at
    # cold-start and means a missing boto3 wheel does not break the
    # "local" deployment.
    try:
        from storage import r2_client
    except Exception as exc:  # pragma: no cover - unreachable in prod
        logger.warning(
            "r2 backend enabled but r2_client import failed (%s); falling back to local",
            exc,
        )
        return None

    key = r2_key_for(job_id, artifact_key, local_path=local_path)

    try:
        present = r2_client.head_artifact(key)
    except Exception as exc:
        logger.warning(
            "r2 HEAD failed for job=%s key=%s (%s); falling back to local",
            job_id, key, exc,
        )
        return None

    if not present:
        if not local_path.exists():
            # Nothing to upload and nothing to redirect to — let the Job
            # API respond with its normal 404 so the user-visible error
            # message is consistent with the non-R2 deployment.
            logger.info(
                "r2 miss and local artifact missing for job=%s key=%s; falling through",
                job_id, key,
            )
            return None
        if not _upload_with_lock(local_path, key, job_id):
            return None

    try:
        url = r2_client.generate_presigned_download_url(key, download_filename)
    except Exception as exc:
        logger.warning(
            "r2 presign failed for job=%s key=%s (%s); falling back to local",
            job_id, key, exc,
        )
        return None

    return url


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _upload_with_lock(local_path: Path, key: str, job_id: str) -> bool:
    """Upload ``local_path`` to R2 under a per-key lock.

    Returns True on success, False on any failure (caller falls back to
    local). The file lock lives beside the artifact file on disk; the
    shared ``services._file_lock.file_lock`` helper already handles cross-
    process reentrancy (see ``src/services/_file_lock.py``).

    The lock scope is per-key, not global, so two different jobs uploading
    at the same time do not serialize on each other. A second request for
    the same key will block on this lock, and by the time it enters the
    critical section the first request's HEAD-miss-then-upload will
    already have completed — a second HEAD at that point returns True and
    the second request skips the upload. Re-checking HEAD inside the lock
    is cheap and avoids a double-push.
    """
    try:
        from services._file_lock import file_lock
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "file_lock import failed (%s); skipping r2 upload for job=%s",
            exc, job_id,
        )
        return False

    try:
        from storage import r2_client
    except Exception:  # pragma: no cover
        return False

    try:
        with file_lock(_lock_path_for_key(key)):
            # Re-check under the lock. Another request may have uploaded
            # the same key while we were blocked on the lock.
            try:
                if r2_client.head_artifact(key):
                    return True
            except Exception as exc:
                logger.warning(
                    "r2 HEAD (post-lock) failed for job=%s key=%s (%s); falling back to local",
                    job_id, key, exc,
                )
                return False

            logger.info("r2 lazy upload starting: job=%s key=%s bytes=%d",
                        job_id, key, local_path.stat().st_size)
            r2_client.upload_artifact(local_path, key)
            logger.info("r2 lazy upload complete: job=%s key=%s", job_id, key)
            return True
    except Exception as exc:
        logger.warning(
            "r2 upload failed for job=%s key=%s (%s); falling back to local",
            job_id, key, exc,
        )
        return False


def _lock_path_for_key(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return Path(settings.jobs_dir) / "_r2_upload_locks" / digest
