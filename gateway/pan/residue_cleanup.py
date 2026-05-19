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

## Payload contract (CodeX P2)

  payload = {
    'job_id': str,
    'user_id': str(UUID),
    'backup_id': str(UUID),         # REQUIRED — which BackupRecord to clean
    'provider': str = 'baidu_pan',  # informational only
  }

Stale_reaper produces one task per stale BackupRecord row, so backup_id
is always available. Querying by (user_id, job_id, status='uploaded')
alone was sloppy — multiple BackupRecord rows can exist for one job
(failed prior attempts), and picking "latest" risks acting on the wrong
generation. backup_id + job_edit_generation match closes both holes.

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


# Phase 9 §T9.4 (CodeX 2026-05-19 P1b): pan JSONL emitter shared with
# backup_executor / restore_executor / auth (gateway/pan/_events.py).
from pan._events import emit_pan_event_safe as _emit_pan_event_safe  # noqa: E402


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
    from pan.status_mutator import set_archive_status

    from pan._lock_keys import pan_lock_key

    job_id: str = payload['job_id']
    user_id: _uuid.UUID = _uuid.UUID(payload['user_id'])
    backup_id_raw = payload.get('backup_id')
    if not backup_id_raw:
        raise ValueError(
            "pan_residue_cleanup payload missing required 'backup_id'. "
            "Stale_reaper must include the specific BackupRecord.id to "
            "clean — picking 'latest uploaded' by (user_id, job_id) is "
            "ambiguous when multiple BackupRecord rows exist (CodeX P2)."
        )
    backup_id: _uuid.UUID = _uuid.UUID(str(backup_id_raw))
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
            # --- state verification (CodeX P2: query by backup_id +
            # generation match instead of "latest uploaded") ---
            async with conn.begin():
                job_row = (await conn.execute(
                    select(
                        Job.status, Job.project_dir, Job.r2_artifacts,
                        Job.edit_generation,
                    ).where(Job.user_id == user_id, Job.job_id == job_id)
                )).one_or_none()
                if job_row is None:
                    logger.info(
                        "pan_residue_cleanup: job %s not found, skipping",
                        job_id,
                    )
                    return
                if job_row.status != 'archiving':
                    logger.info(
                        "pan_residue_cleanup: job %s status=%r (not "
                        "'archiving'), state moved on — no-op",
                        job_id, job_row.status,
                    )
                    return

                # Look up the SPECIFIC BackupRecord row stale_reaper
                # picked, not "latest uploaded". Multiple BackupRecord
                # rows can coexist for one job (failed prior attempts).
                br_row = (await conn.execute(
                    select(
                        BackupRecord.id, BackupRecord.status,
                        BackupRecord.job_edit_generation,
                    ).where(
                        BackupRecord.id == backup_id,
                        BackupRecord.user_id == user_id,
                        BackupRecord.job_id == job_id,
                    )
                )).one_or_none()
                if br_row is None:
                    logger.info(
                        "pan_residue_cleanup: BackupRecord %s not found "
                        "for user=%s job=%s — skipping",
                        backup_id, user_id, job_id,
                    )
                    return
                if br_row.status != 'uploaded':
                    logger.info(
                        "pan_residue_cleanup: BackupRecord %s status=%r "
                        "(not 'uploaded') — not a forward-resolve case, "
                        "skipping", backup_id, br_row.status,
                    )
                    return

                # Generation match. Plan §8: BackupRecord.job_edit_generation
                # must equal Job.edit_generation. If Job got edited past
                # the captured generation (rare but possible via admin
                # tooling), this BackupRecord is no longer the current
                # state — refuse to act on it.
                if br_row.job_edit_generation != job_row.edit_generation:
                    logger.info(
                        "pan_residue_cleanup: BackupRecord %s "
                        "job_edit_generation=%d but Job.edit_generation=%d "
                        "— generations diverged, skipping (manual review)",
                        backup_id, br_row.job_edit_generation,
                        job_row.edit_generation,
                    )
                    return

                project_dir_str = job_row.project_dir
                r2_artifacts = job_row.r2_artifacts or []

            # --- idempotent rmtree (best effort, tracked) ---
            # CodeX P0: gate rmtree behind the same safe-roots whitelist
            # used by TTL cleanup + backup_executor. The previous code had
            # NO safety check here — directly rmtree'd whatever Job.project_dir
            # pointed at (a poisoned row could have cascaded).
            from pan._safe_paths import verify_project_dir_safe
            rmtree_ok = True
            if project_dir_str:
                project_dir = Path(project_dir_str).resolve()
                try:
                    verify_project_dir_safe(project_dir)
                except RuntimeError as exc:
                    rmtree_ok = False
                    logger.error(
                        "pan_residue_cleanup REFUSING rmtree (unsafe path): "
                        "job=%s path=%s err=%s — leaving for manual review",
                        job_id, project_dir, exc,
                    )
                else:
                    if project_dir.exists():
                        try:
                            await asyncio.to_thread(rmtree_fn, project_dir)
                        except Exception as exc:  # noqa: BLE001
                            rmtree_ok = False
                            logger.warning(
                                "pan_residue_cleanup rmtree failed job=%s "
                                "path=%s err=%s — leaving for next pass",
                                job_id, project_dir, exc,
                            )

            # --- idempotent R2 delete (tracked) ---
            r2_failures: list[str] = []
            for artifact in r2_artifacts:
                r2_key = artifact.get('r2_key') if isinstance(artifact, dict) else None
                if not r2_key:
                    continue
                try:
                    await asyncio.to_thread(r2_delete_fn, r2_key)
                except Exception as exc:  # noqa: BLE001
                    r2_failures.append(r2_key)
                    logger.warning(
                        "pan_residue_cleanup R2 delete failed job=%s key=%s "
                        "err=%s — leaving for next pass",
                        job_id, r2_key, exc,
                    )

            # --- finalize (CONDITIONAL on cleanup OK) ---
            # CodeX P0-3: only finalize if both rmtree + all R2 deletes
            # succeeded. Otherwise Job stays at 'archiving' and r2_artifacts
            # remains intact for the next stale_reaper pass. Finalizing
            # with residue would destroy our way to find orphan R2 keys.
            if rmtree_ok and not r2_failures:
                try:
                    async with conn.begin():
                        await set_archive_status(
                            user_id, job_id, 'archived', conn=conn,
                        )
                        await conn.execute(
                            update(Job)
                            .where(
                                Job.user_id == user_id,
                                Job.job_id == job_id,
                            )
                            .values(r2_artifacts=None)
                        )
                    logger.info(
                        "pan_residue_cleanup: forward-resolved job=%s to 'archived'",
                        job_id,
                    )
                    # Phase 9 §T9.4: emit completed event only on
                    # successful finalize. Partial cleanup loops back
                    # for retry on the next stale_reaper Pass-2 forward
                    # pass — no event yet because residue isn't fully
                    # gone from the dashboard's perspective.
                    _emit_pan_event_safe(
                        job_id=job_id,
                        event_type='pan.residue_cleanup.completed',
                        message=(
                            f"pan residue cleanup completed: br={backup_id}"
                        ),
                        payload={
                            'user_id': str(user_id),
                            'backup_id': str(backup_id),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "pan_residue_cleanup: finalize status='archived' "
                        "failed job=%s err=%s — will retry on next pass",
                        job_id, exc,
                    )
            else:
                logger.info(
                    "pan_residue_cleanup: not finalizing job=%s — rmtree_ok=%s "
                    "r2_failures=%s — next stale_reaper pass will retry",
                    job_id, rmtree_ok, r2_failures,
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
    from storage.r2_client import _get_client  # noqa: PLC0415
    client = _get_client()
    client.delete_object(Bucket=settings.r2_artifacts_bucket, Key=r2_key)
