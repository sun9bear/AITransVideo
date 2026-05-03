"""Background-thread runner for on-demand Jianying draft generation (Task K3).

Idempotent triggers:
- idle    -> start thread, set running, return running
- running -> reject (409 caller's responsibility)
- succeeded -> return existing zip path (no re-run)
- failed -> clear error, start new thread, return running

Threading: uses daemon threads. Process restarts will leave running
rows orphaned — see reap_stale().

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md §11.6
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modules.output.jianying.jianying_draft_backend import JianyingDraftBackend
    from services.jobs.store import JobStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class JianyingNotAllowedError(Exception):
    """Raised by trigger() when caller's request violates a precondition.

    Carries a ``reason`` field for the API layer to map to an HTTP status.

    Reason codes:
    - ``service_mode_not_studio`` — job.service_mode != "studio"  -> 403
    - ``job_not_succeeded``       — job.status != "succeeded"     -> 409 / 422
    - ``job_not_found``           — job_id does not exist         -> 404
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


class JianyingEngineUnavailable(Exception):
    """Raised when the pyJianYingDraft engine is not available (not installed).

    Re-exported here so the API layer only needs to import from
    services.jobs.jianying_draft_runner, keeping the optional
    modules.output.jianying dependency out of api.py imports.
    """


class JianyingInvalidDraftRoot(ValueError):
    """Raised when user_draft_root fails validation in trigger().

    K12 (API endpoint) catches this and returns HTTP 400.

    Validation rules (applied in trigger() before the job state machine):
    - Length must be <= 500 characters.
    - Must not be empty after stripping surrounding whitespace.
    - Must not contain null bytes (\\0).
    - Must not begin with a URL scheme (http://, https://, ftp://).
    """


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _validate_user_draft_root(value: str) -> str:
    """Validate and normalise user_draft_root.

    Returns the stripped value on success.
    Raises JianyingInvalidDraftRoot on any validation failure.

    Rules:
    - Strip surrounding whitespace first.
    - Empty after strip → rejected.
    - Length > 500 → rejected.
    - Contains null byte (\\0) → rejected (security).
    - Starts with http://, https://, or ftp:// → rejected (user pasted a URL).
    """
    stripped = value.strip()
    if not stripped:
        raise JianyingInvalidDraftRoot(
            "user_draft_root must not be empty after stripping whitespace."
        )
    if len(stripped) > 500:
        raise JianyingInvalidDraftRoot(
            f"user_draft_root is too long ({len(stripped)} chars, max 500)."
        )
    if "\0" in stripped:
        raise JianyingInvalidDraftRoot(
            "user_draft_root must not contain null bytes."
        )
    lower = stripped.lower()
    for scheme in ("http://", "https://", "ftp://"):
        if lower.startswith(scheme):
            raise JianyingInvalidDraftRoot(
                f"user_draft_root looks like a URL ({stripped[:40]!r}); "
                "please provide a local filesystem path instead."
            )
    return stripped


class JianyingDraftRunner:
    """Background-thread runner for on-demand Jianying draft generation.

    Idempotent triggers:
    - idle    -> start thread, set running, return running
    - running -> reject (409 caller's responsibility)
    - succeeded -> return existing zip path (no re-run)
    - failed -> clear error, start new thread, return running

    Threading: uses daemon threads. Process restarts will leave running
    rows orphaned — see reap_stale().
    """

    STALE_THRESHOLD_SECONDS = 1800  # 30 minutes

    def __init__(
        self,
        *,
        store: "JobStore",
        backend: "JianyingDraftBackend | None" = None,
    ) -> None:
        self._store = store
        self._backend = backend  # may be None — lazy default inside _run_in_background

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def trigger(self, job_id: str, *, user_draft_root: str | None = None) -> dict:
        """Idempotent trigger. Returns response dict per plan §11.2.2.

        Args:
            job_id: The job to generate a draft for.
            user_draft_root: Optional absolute path to the user's local Jianying
                drafts directory (e.g. ``"F:\\剪映缓存\\草稿\\JianyingPro Drafts"``
                or ``"~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"``).
                When set, material paths in the generated JSON will be absolute
                rather than relative. If the cached artifact was built with a
                different root, the draft is regenerated.

        Raises:
            KeyError: if job_id is not found. Caller maps to 404.
            JianyingNotAllowedError: if preconditions are not met. Caller
                maps to 403/409 based on ``error.reason``.
            JianyingInvalidDraftRoot: if user_draft_root fails validation.
                Caller (K12) maps to 400.
        """
        # Validate user_draft_root early (before touching the store)
        if user_draft_root is not None:
            user_draft_root = _validate_user_draft_root(user_draft_root)

        job = self._store.require_job(job_id)  # raises KeyError if not found

        # Gate 1: must be a studio job
        if job.service_mode != "studio":
            raise JianyingNotAllowedError(
                "service_mode_not_studio",
                f"Jianying draft is only available for Studio mode jobs (got {job.service_mode!r}).",
            )

        # Gate 2: overall job must be succeeded
        if job.status != "succeeded":
            raise JianyingNotAllowedError(
                "job_not_succeeded",
                f"Jianying draft can only be triggered for succeeded jobs (got {job.status!r}).",
            )

        jd_status = job.jianying_draft_status

        # Already running — reject to avoid duplicate threads
        if jd_status == "running":
            return {
                "status": "running",
                "started_at": job.jianying_draft_started_at,
                "message": "still in progress",
            }

        # Already succeeded — return existing artifact unless user_draft_root differs
        if jd_status == "succeeded":
            cached_root = getattr(job, "jianying_draft_user_root", None)
            if user_draft_root and user_draft_root != cached_root:
                # User changed the drafts root — fall through and regenerate
                pass
            else:
                return {
                    "status": "succeeded",
                    "completed_at": job.jianying_draft_completed_at,
                    "draft_zip_path": job.jianying_draft_zip_path,
                    "artifact_key": "editor.jianying_draft_zip",
                    "_idempotent": True,
                }

        # idle, failed, or succeeded-with-different-root — transition to running and spawn thread
        job.jianying_draft_status = "running"
        job.jianying_draft_started_at = _utc_now_iso()
        job.jianying_draft_completed_at = None
        job.jianying_draft_error = None
        self._store.save_job(job)

        threading.Thread(
            target=self._run_in_background,
            args=(job_id, user_draft_root),
            daemon=True,
            name=f"jianying-draft-{job_id}",
        ).start()

        return {
            "status": "running",
            "started_at": job.jianying_draft_started_at,
        }

    def get_status(self, job_id: str) -> dict:
        """Return current jianying_draft_* fields as a dict for the API.

        Raises:
            KeyError: if job_id is not found.
        """
        job = self._store.require_job(job_id)
        result: dict = {
            "status": job.jianying_draft_status,
            "started_at": job.jianying_draft_started_at,
            "completed_at": job.jianying_draft_completed_at,
            "error": job.jianying_draft_error,
            "artifact_key": (
                "editor.jianying_draft_zip"
                if job.jianying_draft_status == "succeeded"
                else None
            ),
            "draft_zip_path": job.jianying_draft_zip_path,
        }
        return result

    def reap_stale(self, now: datetime | None = None) -> int:
        """Scan all jobs with jianying_draft_status='running' that started more
        than STALE_THRESHOLD_SECONDS ago, mark them as failed.

        Returns count reaped. Called at Job API startup.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        threshold = now - timedelta(seconds=self.STALE_THRESHOLD_SECONDS)
        reaped = 0
        for job in self._store.list_jobs():
            if job.jianying_draft_status != "running":
                continue
            if not job.jianying_draft_started_at:
                continue
            try:
                started = datetime.fromisoformat(job.jianying_draft_started_at)
            except ValueError:
                logger.warning(
                    "reap_stale: corrupt jianying_draft_started_at for job %s: %r",
                    job.job_id,
                    job.jianying_draft_started_at,
                )
                continue  # corrupt timestamp, skip
            # Ensure both datetimes are timezone-aware for comparison
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if started < threshold:
                job.jianying_draft_status = "failed"
                job.jianying_draft_error = (
                    "Process restart while generation was in progress; "
                    "marked stale by startup reaper. Trigger again to retry."
                )
                job.jianying_draft_completed_at = _utc_now_iso()
                self._store.save_job(job)
                logger.warning(
                    "reap_stale: marked jianying_draft as failed for job %s "
                    "(started_at=%s, threshold=%s)",
                    job.job_id,
                    job.jianying_draft_started_at,
                    threshold.isoformat(),
                )
                reaped += 1
        return reaped

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_in_background(self, job_id: str, user_draft_root: str | None = None) -> None:
        """Execute draft generation in a background thread.

        Updates JobRecord status on success or failure. All exceptions are
        caught — the thread must never die silently without recording the error.

        Args:
            job_id: Job to generate a draft for.
            user_draft_root: Passed through to _build_jianying_request so
                absolute material paths can be embedded in the draft JSON.
        """
        try:
            job = self._store.require_job(job_id)
            request = self._build_jianying_request(job, user_draft_root=user_draft_root)

            # Lazy-import backend to avoid pulling in pyJianYingDraft at module
            # import time (optional dependency).
            if self._backend is not None:
                backend = self._backend
            else:
                from modules.output.jianying.jianying_draft_backend import (
                    JianyingDraftBackend,
                )

                backend = JianyingDraftBackend()

            result = backend.write(request)

            # validation_status: ok / skipped_no_engine / skipped_missing_input / failed
            if result.validation_status == "ok":
                job.jianying_draft_status = "succeeded"
                job.jianying_draft_zip_path = result.draft_zip_path
                job.jianying_draft_completed_at = _utc_now_iso()
                job.jianying_draft_error = None
                job.jianying_draft_user_root = user_draft_root
                logger.info(
                    "Jianying draft succeeded for job %s: %s",
                    job_id,
                    result.draft_zip_path,
                )
            else:
                # skipped_no_engine / skipped_missing_input / failed all
                # map to status=failed so the user sees an error and can retry.
                job.jianying_draft_status = "failed"
                job.jianying_draft_error = (
                    f"backend returned {result.validation_status}: "
                    f"see {result.compatibility_report_path}"
                )
                job.jianying_draft_completed_at = _utc_now_iso()
                logger.warning(
                    "Jianying draft non-ok for job %s: validation_status=%s",
                    job_id,
                    result.validation_status,
                )

            self._store.save_job(job)

        except Exception as exc:  # noqa: BLE001
            logger.exception("Jianying draft generation failed for %s", job_id)
            try:
                job = self._store.require_job(job_id)
                job.jianying_draft_status = "failed"
                job.jianying_draft_error = f"{type(exc).__name__}: {exc}"
                job.jianying_draft_completed_at = _utc_now_iso()
                self._store.save_job(job)
            except Exception:
                logger.exception(
                    "Failed to record jianying error for job %s — store unreachable",
                    job_id,
                )

    def _build_jianying_request(self, job, *, user_draft_root: str | None = None) -> "object":
        """Construct JianyingDraftRequest from JobRecord.

        Reads manifest.json from {project_dir}/manifest.json to resolve
        artifact paths. Raises if project_dir is missing or manifest is absent.

        Args:
            job: JobRecord instance.
            user_draft_root: Optional absolute path to the user's local Jianying
                drafts directory. When set, material paths in the generated JSON
                will be absolute rather than relative.

        Returns a JianyingDraftRequest instance.
        """
        from modules.output.jianying.jianying_draft_models import JianyingDraftRequest

        if not job.project_dir:
            raise ValueError(f"job {job.job_id} has no project_dir — cannot build JianyingDraftRequest")

        project_dir = Path(job.project_dir)
        manifest_path = project_dir / "manifest.json"

        if not manifest_path.exists():
            raise FileNotFoundError(
                f"manifest.json not found at {manifest_path} for job {job.job_id}"
            )

        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifact_index: dict[str, str] = manifest_data.get("artifact_index", {})

        source_video_path = artifact_index.get("source.original_video", "")
        dubbed_audio_path = artifact_index.get("editor.dubbed_audio_complete", "")
        subtitle_path = artifact_index.get("editor.subtitles", "")
        ambient_audio_path = artifact_index.get("editor.ambient_audio") or None

        # Use display_name if available; fall back to job_id as project title
        project_title = job.display_name or job.job_id

        return JianyingDraftRequest(
            project_id=job.job_id,
            project_title=project_title,
            source_video_path=source_video_path,
            dubbed_audio_path=dubbed_audio_path,
            subtitle_path=subtitle_path,
            output_dir=str(project_dir),
            ambient_audio_path=ambient_audio_path,
            width=1920,
            height=1080,
            user_draft_root=user_draft_root,
        )
