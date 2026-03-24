"""Daily cleanup of expired projects (older than 7 days)."""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

RETENTION_DAYS = 7
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60  # Run every 6 hours


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

        # Skip active jobs
        status = data.get("status", "")
        if status in ("running", "queued"):
            continue

        # Check age based on updated_at or created_at
        timestamp_str = data.get("updated_at") or data.get("created_at") or ""
        if not timestamp_str:
            continue

        try:
            job_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            if job_time.tzinfo is None:
                job_time = job_time.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        age_days = (now - job_time).total_seconds() / 86400
        if age_days < RETENTION_DAYS:
            continue

        job_id = data.get("job_id", job_file.stem)
        project_dir = data.get("project_dir")

        # Delete project directory
        if project_dir:
            project_path = Path(project_dir)
            if project_path.is_dir():
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
    """Background loop that runs cleanup periodically."""
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
