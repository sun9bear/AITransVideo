"""Pan stale reaper (Phase 8 §T8.3).

Plan 2026-05-13 §10. Every 30 min (`AVT_PAN_STALE_REAP_INTERVAL_MINUTES`)
scan two classes of stuck pan operations and reconcile them:

## Pass 1 — in-flight stuck (uploading / restoring + stale heartbeat)

  SELECT BackupRecord WHERE
    status IN ('uploading', 'restoring')
    AND (heartbeat_at IS NULL OR heartbeat_at < now() - 4h)

  For each: pg_try_advisory_lock(user_id, job_id).
  - lock acquired → executor is dead → reconcile:
      uploading  → BackupRecord.status='failed' + Job.status back to
                   'succeeded' (data is still in project_dir; no remote
                   tar yet, or partial — orphan_cleanup sweeps next)
      restoring  → BackupRecord.status='uploaded' (revert to recoverable
                   state) + Job.status back to 'archived' (tar still in
                   pan, project_dir might be in staging dir which the
                   restore executor's finally cleaned)
  - lock NOT acquired → real executor is still alive (PG advisory locks
                        are session-bound) → skip this row, try next tick

## Pass 2 — post-commit stuck (Job.archiving + BR.uploaded > 4h)

  Job hit COMMIT POINT (BackupRecord='uploaded') but the post-commit
  step l (set Job.status='archived') failed / executor died before it
  ran. Data is safe (tar in pan, 3-gate verified), just the Job status
  hasn't caught up.

  Forward-resolve: set Job.status='archived' + enqueue pan_residue_cleanup
  to retry rmtree project_dir + R2 delete (idempotent — they may have
  already happened). This is the same scenario residue_cleanup is
  designed for; it's just hooked here too because stale_reaper is the
  detection mechanism.

## Concurrency safety

pg_try_advisory_lock is session-level. Reaper opens one connection per
tick, takes per-row locks, releases each before moving on. If the real
executor is alive on the same lock, our try_lock returns False and we
move on — exactly what we want.

SQLite test environment: advisory lock is a no-op (returns True). Tests
exercise the reconciliation logic directly without needing the lock.
"""
from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from models import BackupRecord, Job

from pan._lock_keys import pan_lock_key
from pan.status_mutator import set_archive_status


logger = logging.getLogger(__name__)


async def _try_advisory_lock(conn: AsyncConnection, key: int) -> bool:
    """pg_try_advisory_lock — non-blocking. Returns True if acquired.

    No-op on SQLite (returns True). Production PG always has it.
    """
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


async def run_stale_reaper_tick(
    engine: AsyncEngine,
    *,
    stale_hours: int = 4,
    dry_run: bool = False,
) -> dict[str, Any]:
    """One tick. Returns:
      {'in_flight_reaped': int, 'in_flight_skipped_locked': int,
       'post_commit_forwarded': int, 'post_commit_skipped_locked': int,
       'residue_cleanup_enqueued': int,
       'dry_run': bool}
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=stale_hours)
    stats = {
        'in_flight_reaped': 0,
        'in_flight_skipped_locked': 0,
        'post_commit_forwarded': 0,
        'post_commit_skipped_locked': 0,
        'residue_cleanup_enqueued': 0,
        'dry_run': dry_run,
    }

    async with engine.connect() as conn:
        # --- Pass 1: in-flight stuck (uploading/restoring + stale heartbeat) ---
        in_flight_rows = (await conn.execute(
            select(
                BackupRecord.id, BackupRecord.user_id, BackupRecord.job_id,
                BackupRecord.job_edit_generation, BackupRecord.status,
            ).where(
                BackupRecord.status.in_(['uploading', 'restoring']),
                # heartbeat_at IS NULL OR heartbeat_at < cutoff
                (BackupRecord.heartbeat_at.is_(None))
                | (BackupRecord.heartbeat_at < cutoff),
            )
        )).all()
        # Close the implicit read txn so per-row reap can open fresh ones.
        # (SQLAlchemy AsyncConnection auto-begins on first execute; without
        # this, the per-row `async with conn.begin()` errors. PG READ
        # COMMITTED sees fresh per-statement state regardless; this commit
        # is a no-op there but unbreaks SQLite snapshot semantics.)
        await conn.commit()

        for row in in_flight_rows:
            lock_key = pan_lock_key(row.user_id, row.job_id)
            if dry_run:
                stats['in_flight_reaped'] += 1
                continue

            got = await _try_advisory_lock(conn, lock_key)
            if not got:
                stats['in_flight_skipped_locked'] += 1
                logger.info(
                    "pan_stale_reaper: skip in-flight br=%s (lock held)",
                    row.id,
                )
                continue
            try:
                if row.status == 'uploading':
                    await _reap_uploading(conn, row)
                elif row.status == 'restoring':
                    await _reap_restoring(conn, row)
                await conn.commit()
                stats['in_flight_reaped'] += 1
                logger.info(
                    "pan_stale_reaper: reaped %s br=%s user=%s job=%s",
                    row.status, row.id, row.user_id, row.job_id,
                )
            except Exception as exc:  # noqa: BLE001
                try:
                    await conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                logger.warning(
                    "pan_stale_reaper: reap failed br=%s err=%s",
                    row.id, exc,
                )
            finally:
                await _release_advisory_lock(conn, lock_key)

        # --- Pass 2: post-commit stuck (jobs.archiving + br.uploaded > 4h) ---
        # We use a generation-aware join: BackupRecord.job_edit_generation
        # must match Job.edit_generation (otherwise we'd risk forwarding
        # a job whose current state has moved on).
        post_commit_rows = (await conn.execute(
            select(
                Job.user_id, Job.job_id, BackupRecord.id.label('br_id'),
            ).select_from(
                Job.__table__.join(
                    BackupRecord.__table__,
                    (Job.user_id == BackupRecord.user_id)
                    & (Job.job_id == BackupRecord.job_id)
                    & (Job.edit_generation == BackupRecord.job_edit_generation)
                    & (BackupRecord.status == 'uploaded'),
                )
            ).where(
                Job.status == 'archiving',
                # 'completed_at' is when COMMIT POINT happened — if that
                # was > stale_hours ago and Job.status still 'archiving',
                # someone died between step i and step l.
                (BackupRecord.completed_at.is_(None))
                | (BackupRecord.completed_at < cutoff),
            )
        )).all()
        await conn.commit()  # same rationale as Pass 1 SELECT close

        for row in post_commit_rows:
            lock_key = pan_lock_key(row.user_id, row.job_id)
            if dry_run:
                stats['post_commit_forwarded'] += 1
                continue

            got = await _try_advisory_lock(conn, lock_key)
            if not got:
                stats['post_commit_skipped_locked'] += 1
                logger.info(
                    "pan_stale_reaper: skip post-commit job=%s (lock held)",
                    row.job_id,
                )
                continue
            try:
                # Forward-resolve to archived.
                async with conn.begin():
                    await set_archive_status(
                        row.user_id, row.job_id, 'archived', conn=conn,
                    )

                # Enqueue residue cleanup to retry rmtree + R2 delete.
                # Use a fresh session because queue.create_task expects
                # AsyncSession (not AsyncConnection).
                from sqlalchemy.ext.asyncio import (
                    AsyncSession as _AsyncSession, async_sessionmaker,
                )
                import background_task_queue as queue
                Session = async_sessionmaker(
                    engine, class_=_AsyncSession, expire_on_commit=False,
                )
                async with Session() as bg_db:
                    await queue.create_task(
                        bg_db,
                        job_id=row.job_id,
                        user_id=row.user_id,
                        task_type='pan_residue_cleanup',
                        params={
                            'user_id': str(row.user_id),
                            'backup_id': str(row.br_id),
                        },
                    )
                    await bg_db.commit()

                stats['post_commit_forwarded'] += 1
                stats['residue_cleanup_enqueued'] += 1
                logger.info(
                    "pan_stale_reaper: forward-resolved user=%s job=%s br=%s",
                    row.user_id, row.job_id, row.br_id,
                )
            except Exception as exc:  # noqa: BLE001
                try:
                    await conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                logger.warning(
                    "pan_stale_reaper: forward-resolve failed job=%s err=%s",
                    row.job_id, exc,
                )
            finally:
                await _release_advisory_lock(conn, lock_key)

    return stats


async def _reap_uploading(conn: AsyncConnection, row) -> None:
    """Pre-COMMIT-POINT executor died. backup_records → 'failed',
    Job.status → 'succeeded' (data still in project_dir)."""
    # set_archive_status uses its own short txn; we open an outer one.
    async with conn.begin():
        await set_archive_status(
            row.user_id, row.job_id, 'succeeded', conn=conn,
        )
        await conn.execute(
            update(BackupRecord)
            .where(BackupRecord.id == row.id)
            .values(
                status='failed',
                error_message='reaped: heartbeat stale (uploading phase)',
                completed_at=datetime.now(timezone.utc),
            )
        )


async def _reap_restoring(conn: AsyncConnection, row) -> None:
    """Restore executor died mid-download. backup_records → 'uploaded'
    (recoverable — tar still in pan), Job.status → 'archived' (revert
    to pre-restore state)."""
    async with conn.begin():
        await set_archive_status(
            row.user_id, row.job_id, 'archived', conn=conn,
        )
        await conn.execute(
            update(BackupRecord)
            .where(BackupRecord.id == row.id)
            .values(
                status='uploaded',
                error_message='reaped: heartbeat stale (restoring phase)',
            )
        )
