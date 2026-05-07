"""Daily cleanup of expired projects.

Retention rule (plan 2026-04-18 §5.3):

- If ``jobs/<id>.json`` has a ``expires_at`` field (populated by migration 015
  + the Gateway at create/copy time), that is the authoritative expiry.
- Otherwise the legacy fallback applies: delete when
  ``COALESCE(updated_at, created_at) + RETENTION_DAYS`` is in the past.

Active statuses are never deleted here:

- ``queued`` / ``running`` — live workers, never touch.
- ``waiting_for_review`` — user-owned session, only user / admin can cancel.
- ``editing`` — user-owned session; idle-cancel is handled by
  :mod:`src.services.web_ui.editing_idle_scanner`, NOT this module.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

RETENTION_DAYS = 7
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60  # Run every 6 hours

# Statuses that must never be deleted by the TTL-based cleanup. ``editing`` is
# here even though it's also time-based — the per-job idle timeout (24h since
# editing_touched_at) is enforced by editing_idle_scanner, not this file, so
# that the two mechanisms don't race.
# Mirrors src/services/jobs/models.ACTIVE_JOB_STATUSES minus the expected
# no-worker-is-fine distinction of ``waiting_for_review`` — we skip both.
_CLEANUP_PROTECTED_STATUSES = frozenset(
    {"queued", "running", "waiting_for_review", "editing"}
)

# 2026-04-21 regression guard: the original code did a bare
# ``shutil.rmtree(Path(project_dir))`` with no path validation, which
# would have been catastrophic if ``project_dir`` was ever polluted (e.g.
# the ``/s`` bug from 2026-04-20). Limit rm to descendants of known
# project roots. Mirrors the allowlist on the Gateway side
# (``gateway/project_cleanup.py``).
_SAFE_PROJECT_ROOTS: tuple[Path, ...] = (
    Path("/opt/aivideotrans/app/projects"),
    Path("/opt/aivideotrans/data/projects"),
)


def _is_safe_project_dir(path: Path) -> bool:
    """True iff ``path`` resolves to a strict descendant of one of the
    safe roots. Refuses empty paths, filesystem root, and paths that
    traverse outside a root via ``..`` / symlink."""
    if not path or str(path) in ("", "/"):
        return False
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    if resolved == Path("/") or str(resolved) == "":
        return False
    for root in _SAFE_PROJECT_ROOTS:
        try:
            resolved_root = root.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if resolved == resolved_root:
            return False  # never nuke the whole projects/ root
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            continue
        return True
    return False


def _parse_iso_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _resolve_expires_at(data: dict, now: datetime) -> datetime | None:
    """Decide when a job is expired. Returns the expiry timestamp, or None
    if we can't compute one (in which case the caller should skip deletion).

    Priority: explicit ``expires_at`` > legacy ``updated_at/created_at + 7d``.
    """
    explicit = data.get("expires_at")
    if explicit:
        parsed = _parse_iso_utc(explicit)
        if parsed is not None:
            return parsed
        # Malformed expires_at — fall through to legacy rule rather than
        # silently never delete.
    timestamp_str = data.get("updated_at") or data.get("created_at") or ""
    if not timestamp_str:
        return None
    parsed = _parse_iso_utc(timestamp_str)
    if parsed is None:
        return None
    return parsed + timedelta(days=RETENTION_DAYS)


def cleanup_expired_projects(*, deleted_job_ids_out: list[str] | None = None) -> dict[str, list[str]]:
    """Remove projects and job files older than RETENTION_DAYS. Returns summary.

    If deleted_job_ids_out is provided, appends deleted job_ids for external cleanup (e.g. PostgreSQL).
    """
    jobs_dir = Path(os.environ.get("AIVIDEOTRANS_JOBS_DIR", "/opt/aivideotrans/app/jobs"))
    now = datetime.now(timezone.utc)
    deleted_jobs: list[str] = []
    deleted_projects: list[str] = []
    errors: list[str] = []

    if not jobs_dir.is_dir():
        return {"deleted_jobs": [], "deleted_projects": [], "errors": []}

    for job_file in jobs_dir.glob("*.json"):
        if job_file.name.endswith(".events.jsonl"):
            continue
        try:
            data = json.loads(job_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        # Skip any status the TTL cleaner must not touch. Explicit set rather
        # than "not in terminal statuses" so that unknown future statuses
        # default to "leave alone" instead of "delete aggressively".
        status = str(data.get("status", "")).strip().lower()
        if status in _CLEANUP_PROTECTED_STATUSES:
            continue
        if str(data.get("role_snapshot", "")).strip().lower() == "admin":
            continue

        expires_at = _resolve_expires_at(data, now)
        if expires_at is None or now < expires_at:
            continue

        job_id = data.get("job_id", job_file.stem)
        project_dir = data.get("project_dir")

        # Delete project directory — only if it passes the safety
        # whitelist. Untrusted / poisoned paths trigger a warning and
        # fall through to the JSON/events-file unlink path; the
        # Gateway-side cleanup will mark the DB row 'purged' on the
        # next pass so we don't get stuck in a loop.
        if project_dir:
            project_path = Path(project_dir)
            if not _is_safe_project_dir(project_path):
                errors.append(
                    f"refusing to rmtree unsafe path {project_path!r} "
                    f"for job {job_id} (not under {_SAFE_PROJECT_ROOTS})"
                )
            elif project_path.is_dir():
                try:
                    shutil.rmtree(project_path, ignore_errors=True)
                    deleted_projects.append(str(project_path))
                except Exception as e:
                    errors.append(f"Failed to delete project {project_path}: {e}")

        # Delete job files
        events_file = jobs_dir / f"{job_id}.events.jsonl"
        try:
            job_file.unlink(missing_ok=True)
            events_file.unlink(missing_ok=True)
            deleted_jobs.append(job_id)
            if deleted_job_ids_out is not None:
                deleted_job_ids_out.append(job_id)
        except Exception as e:
            errors.append(f"Failed to delete job file {job_id}: {e}")

    return {
        "deleted_jobs": deleted_jobs,
        "deleted_projects": deleted_projects,
        "errors": errors,
    }


def _cleanup_loop() -> None:
    """Background loop that runs TTL cleanup + editing-idle scanner."""
    # Late import so Phase 0 can land this file without circular-import risk
    # from Phase 1 modules (the scanner module itself has no Phase 1 deps).
    # P0-6 (audit 2026-05-07): pythonpath in container is .../src, not project
    # root, so 'src.' prefix is wrong — would raise ImportError and silently
    # kill this daemon thread before the editing-idle scanner ever ran.
    from services.web_ui import editing_idle_scanner

    while True:
        try:
            time.sleep(CLEANUP_INTERVAL_SECONDS)
            result = cleanup_expired_projects()
            if result["deleted_jobs"]:
                logger.info(
                    "Cleanup: deleted %d expired jobs, %d project dirs",
                    len(result["deleted_jobs"]),
                    len(result["deleted_projects"]),
                )
            if result["errors"]:
                for err in result["errors"]:
                    logger.warning("Cleanup error: %s", err)

            # Post-edit idle scanner (D24). Phase 0 callback is a no-op; Phase
            # 1 T1-1 swaps ``registered_cancel_callback`` at startup to the
            # real editing/cancel handler. Until then this is detection-only.
            scan_result = editing_idle_scanner.scan_editing_idle(
                datetime.now(timezone.utc),
                editing_idle_scanner.registered_cancel_callback,
            )
            if scan_result["candidates"]:
                logger.info(
                    "editing idle scan: candidates=%d cancelled=%d failed=%d",
                    len(scan_result["candidates"]),
                    len(scan_result["cancelled"]),
                    len(scan_result["failed"]),
                )
        except Exception:
            logger.exception("Cleanup loop error")


def start_cleanup_thread() -> threading.Thread:
    """Start the background cleanup thread. Call once at server startup."""
    # Run once immediately
    try:
        result = cleanup_expired_projects()
        if result["deleted_jobs"]:
            logger.info(
                "Startup cleanup: deleted %d expired jobs, %d project dirs",
                len(result["deleted_jobs"]),
                len(result["deleted_projects"]),
            )
    except Exception:
        logger.exception("Startup cleanup failed")

    thread = threading.Thread(target=_cleanup_loop, daemon=True, name="project-cleanup")
    thread.start()
    return thread
