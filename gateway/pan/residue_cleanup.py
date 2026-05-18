"""Pan backup residue cleanup executor (Phase 5b T5.12).

Forward-resolves backups that hit COMMIT POINT but didn't finish the
post-commit cleanup steps (rmtree project_dir / R2 delete / status='archived').
Called by pan_stale_reaper (Phase 8) when:

  - BackupRecord.status == 'uploaded'   (commit point passed)
  - Job.status == 'archiving'           (executor never reached step l)
  - BackupRecord.heartbeat_at is stale  (heartbeat loop died → executor died)

The reaper schedules this executor via the background task queue. The
cleanup is IDEMPOTENT — re-running on a job that's already 'archived'
is a no-op. The cleanup uses pg_try_advisory_lock (NOT pg_advisory_lock)
so if a real backup_executor is still alive on the same lock_key, the
cleanup yields rather than blocking.

Plan §10 stale_reaper forward-resolve branch.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import uuid as _uuid
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import desc, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


_REPO_SRC = Path(__file__).resolve().parent.parent.parent / "src"
for _candidate in (_REPO_SRC, Path("/opt/aivideotrans/app/src")):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))


logger = logging.getLogger(__name__)


# --- public entry ---


async def execute_pan_residue_cleanup(payload: dict) -> None:
    """Public entry. Payload: {'job_id': str, 'user_id': str(UUID),
    'provider': str?}."""
    from database import engine  # noqa: PLC0415

    await _execute_pan_residue_cleanup_impl(
        payload,
        engine=engine,
        rmtree_fn=shutil.rmtree,
        r2_delete_fn=_default_r2_delete,
    )


# --- impl with injection seams ---


async def _execute_pan_residue_cleanup_impl(
    payload: dict,
    *,
    engine: AsyncEngine,
    rmtree_fn: Callable[[Path], None],
    r2_delete_fn: Callable[[str], None],
) -> None:
    """Forward-resolve a stuck backup.

    Steps:
      1. pg_try_advisory_lock — fail fast if a real executor is alive
      2. Verify state: BackupRecord.status='uploaded' + Job.status='archiving'
         (if not, the state moved on while we waited; bail no-op)
      3. Idempotent rmtree project_dir (already-gone is fine)
      4. Idempotent R2 delete for each artifact in Job.r2_artifacts
      5. Set Job.status='archived' + Job.r2_artifacts=None
      6. Release lock
    """
    from models import Job, BackupRecord
    from gateway.pan.status_mutator import set_archive_status

    from gateway.pan._lock_keys import pan_lock_key

    job_id: str = payload['job_id']
    user_id: _uuid.UUID = _uuid.UUID(payload['user_id'])
    lock_key = pan_lock_key(user_id, job_id)  # stable across processes (CodeX P0-1)

    async with engine.connect() as conn:
        got_lock = await _try_advisory_lock(conn, lock_key)
        if not got_lock:
            logger.info(
                "pan_residue_cleanup: lock %s held by another worker, "
                "skipping job=%s", lock_key, job_id,
            )
            return

        try:
            # --- state verification ---
            async with conn.begin():
                job_row = (await conn.execute(
                    select(Job.status, Job.project_dir, Job.r2_artifacts)
                    .where(Job.user_id == user_id, Job.job_id == job_id)
                )).one_or_none()
                if job_row is None:
                    logger.info(
                        "pan_residue_cleanup: job %s not found, skipping",
                        job_id,
                    )
                    return
                if job_row.status != 'archiving':
                    logger.info(
                        "pan_residue_cleanup: job %s status=%r (not 'archiving'),"
                        " state moved on — no-op", job_id, job_row.status,
                    )
                    return

                # Must have a committed 'uploaded' BackupRecord — otherwise
                # this isn't a forward-resolve scenario.
                br_row = (await conn.execute(
                    select(BackupRecord.id, BackupRecord.status)
                    .where(
                        BackupRecord.user_id == user_id,
                        BackupRecord.job_id == job_id,
                        BackupRecord.status == 'uploaded',
                    ).order_by(desc(BackupRecord.created_at))
                    .limit(1)
                )).one_or_none()
                if br_row is None:
                    logger.info(
                        "pan_residue_cleanup: no 'uploaded' BackupRecord for "
                        "job=%s — not a forward-resolve case, skipping",
                        job_id,
                    )
                    return

                project_dir_str = job_row.project_dir
                r2_artifacts = job_row.r2_artifacts or []

            # --- idempotent rmtree (best effort) ---
            if project_dir_str:
                project_dir = Path(project_dir_str)
                if project_dir.exists():
                    try:
                        await asyncio.to_thread(rmtree_fn, project_dir)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "pan_residue_cleanup rmtree failed job=%s path=%s "
                            "err=%s — leaving residue for next pass",
                            job_id, project_dir, exc,
                        )

            # --- idempotent R2 delete ---
            for artifact in r2_artifacts:
                r2_key = artifact.get('r2_key') if isinstance(artifact, dict) else None
                if not r2_key:
                    continue
                try:
                    await asyncio.to_thread(r2_delete_fn, r2_key)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "pan_residue_cleanup R2 delete failed job=%s key=%s "
                        "err=%s — leaving residue for next pass",
                        job_id, r2_key, exc,
                    )

            # --- finalize Job.status='archived' + clear r2_artifacts ---
            try:
                async with conn.begin():
                    await set_archive_status(
                        user_id, job_id, 'archived', conn=conn,
                    )
                    await conn.execute(
                        update(Job)
                        .where(Job.user_id == user_id, Job.job_id == job_id)
                        .values(r2_artifacts=None)
                    )
                logger.info(
                    "pan_residue_cleanup: forward-resolved job=%s to 'archived'",
                    job_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "pan_residue_cleanup: finalize status='archived' failed "
                    "job=%s err=%s — will retry on next stale_reaper pass",
                    job_id, exc,
                )

        finally:
            await _release_advisory_lock(conn, lock_key)


# --- lock helpers (similar to backup_executor but TRY variant) ---


async def _try_advisory_lock(conn: AsyncConnection, key: int) -> bool:
    """pg_try_advisory_lock — non-blocking. Returns True if acquired.

    No-op on SQLite (returns True). Production PG always has it."""
    if conn.dialect.name != 'postgresql':
        return True
    result = await conn.execute(
        text("SELECT pg_try_advisory_lock(:k)"), {'k': key},
    )
    return bool(result.scalar())


async def _release_advisory_lock(conn: AsyncConnection, key: int) -> None:
    if conn.dialect.name == 'postgresql':
        await conn.execute(
            text("SELECT pg_advisory_unlock(:k)"), {'k': key},
        )


def _default_r2_delete(r2_key: str) -> None:
    """Production R2 delete via shared boto3 client (idempotent on 404)."""
    from config import settings  # noqa: PLC0415
    from gateway.storage.r2_client import _get_client  # noqa: PLC0415
    client = _get_client()
    client.delete_object(Bucket=settings.r2_artifacts_bucket, Key=r2_key)
