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
      restoring  → CHECK project_dir.exists() (CodeX P0-2):
                     ⌐ exists (moved=True scenario from restore_executor
                       — os.replace succeeded but DB finalize failed)
                       → forward-resolve to Job.status='succeeded' +
                         BackupRecord.status='restored'. Blindly
                         rolling back would create the "data on disk
                         + DB says archived" stuck state that Phase 5
                         P1 specifically prevents.
                     ⌐ doesn't exist (move never happened — failure
                       was during download / extract / verify)
                       → BackupRecord.status='uploaded' (revert to
                         recoverable) + Job.status back to 'archived'.
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
            # CodeX P1-3: `SELECT pg_try_advisory_lock(...)` auto-begins
            # a transaction on PG. The reap helpers below open their
            # own `async with conn.begin()` and would error
            # "already in transaction". Flush the lock-acquire txn now;
            # the advisory lock itself is session-scoped (pg_try_advisory_lock
            # without _xact_), so commit doesn't release it.
            await conn.commit()
            try:
                if row.status == 'uploading':
                    await _reap_uploading(conn, row)
                    reap_label = 'rollback-uploading'
                elif row.status == 'restoring':
                    reap_label = await _reap_restoring(conn, row)
                else:
                    reap_label = 'unknown'
                stats['in_flight_reaped'] += 1
                logger.info(
                    "pan_stale_reaper: reaped %s br=%s user=%s job=%s mode=%s",
                    row.status, row.id, row.user_id, row.job_id, reap_label,
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
            # CodeX P1-3: flush the auto-begin txn from
            # `SELECT pg_try_advisory_lock(...)` before opening explicit
            # begin() blocks below.
            await conn.commit()

            # 2026-05-26 postmortem P0d v2 (Codex 2nd-round finding):
            # CRITICAL ORDERING — release the advisory lock BEFORE the
            # enqueue+launch of pan_residue_cleanup.
            #
            # The lock here is a PROOF probe: getting it shows no active
            # executor is currently holding it for this (user, job). Once
            # we have that proof, we don't need to KEEP the lock — the
            # subsequent residue_cleanup needs to acquire the same key on
            # its own connection. If we hold the lock through the
            # enqueue+launch path, residue_cleanup will start running
            # almost immediately (asyncio.create_task is fired inside
            # enqueue_pan_task), hit pg_try_advisory_lock against our
            # still-held key, fail, log "lock held by another worker",
            # return — and the dispatcher will silently mark the task
            # completed even though no actual cleanup ran.
            #
            # Safety after release: Job.status remains 'archiving'.
            # backup_executor only acquires the lock from inside its
            # critical section, which is gated by the HTTP enqueue
            # path that won't create a NEW pan_backup row for a job
            # already in 'archiving' state. The reconciler path is
            # gated separately via the pan feature flag (see commit
            # alongside this one). So the post-release window is safe.
            await _release_advisory_lock(conn, lock_key)

            try:
                # 2026-05-26 postmortem P0d (Codex 1st-round feedback):
                # PRE-FIX BUG — we used to call set_archive_status('archived')
                # HERE, then enqueue pan_residue_cleanup. But residue_cleanup
                # (pan/residue_cleanup.py:153) refuses to act unless
                # Job.status == 'archiving' — so the cleanup task immediately
                # no-op'd, leaving R2 artifacts orphaned and r2_artifacts
                # JSONB stale. Production 2026-05-26 had 3 archived jobs
                # with 1.98 GB of orphan R2 keys directly caused by this.
                #
                # POST-FIX CONTRACT — residue_cleanup owns the archived flip.
                # stale_reaper only enqueues; residue_cleanup verifies state,
                # cleans R2 + rmtree, then sets status='archived' +
                # r2_artifacts=NULL atomically. Job stays at 'archiving' until
                # cleanup actually succeeds — if residue_cleanup fails
                # partway, the next stale_reaper tick will re-enqueue (the
                # enqueue helper is idempotent on (user, job, task_type)).

                # Enqueue residue cleanup via the shared helper that
                # creates the BackgroundTask row AND launches the
                # executor (CodeX P0-1). Use a fresh session because
                # the helper expects AsyncSession (not the AsyncConnection
                # we hold the advisory lock on).
                from sqlalchemy.ext.asyncio import (
                    AsyncSession as _AsyncSession, async_sessionmaker,
                )
                from pan._enqueue import enqueue_pan_task
                Session = async_sessionmaker(
                    engine, class_=_AsyncSession, expire_on_commit=False,
                )
                async with Session() as bg_db:
                    await enqueue_pan_task(
                        bg_db,
                        user_id=row.user_id,
                        job_id=row.job_id,
                        task_type='pan_residue_cleanup',
                        extra_params={'backup_id': str(row.br_id)},
                    )

                stats['post_commit_forwarded'] += 1
                stats['residue_cleanup_enqueued'] += 1
                logger.info(
                    "pan_stale_reaper: enqueued residue_cleanup for "
                    "user=%s job=%s br=%s (status stays 'archiving' "
                    "until residue_cleanup finalizes)",
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

    return stats


async def _reap_uploading(conn: AsyncConnection, row) -> None:
    """Pre-COMMIT-POINT executor died. backup_records → 'failed',
    Job.status → 'succeeded' (data still in project_dir)."""
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


async def _reap_restoring(conn: AsyncConnection, row) -> str:
    """Restore executor died mid-flight. CodeX P0-2: must distinguish
    pre-move vs post-move failure:

      - post-move (os.replace already moved staging→project_dir, only
        DB finalize failed): project_dir exists with restored data.
        Forward-resolve: Job.status='succeeded' + BR.status='restored'.
        Blindly rolling back to 'archived' here would create the "data
        on disk + DB says archived" stuck state that next restore
        refuses (project_dir already exists), defeating Phase 5 P1's
        moved=True commit-point fix.
      - pre-move (download / extract / verify failed before os.replace):
        project_dir doesn't exist. Rollback: BR.status='uploaded' +
        Job.status='archived' (tar still in pan, retry possible).

    Returns a label for logging ('forward-resolve' / 'rollback').
    """
    # Look up project_dir to decide which branch.
    job_row = (await conn.execute(
        select(Job.project_dir).where(
            Job.user_id == row.user_id, Job.job_id == row.job_id,
        )
    )).one_or_none()

    project_exists = False
    if job_row is not None and job_row.project_dir:
        from pathlib import Path
        try:
            project_dir = Path(job_row.project_dir).resolve()
            project_exists = project_dir.exists() and project_dir.is_dir()
        except (OSError, RuntimeError):
            project_exists = False
    # commit the SELECT's auto-begin so the explicit begin() below works.
    await conn.commit()

    now = datetime.now(timezone.utc)
    if project_exists:
        # Post-move: data is on disk. Forward-resolve.
        async with conn.begin():
            await set_archive_status(
                row.user_id, row.job_id, 'succeeded', conn=conn,
            )
            await conn.execute(
                update(BackupRecord)
                .where(BackupRecord.id == row.id)
                .values(
                    status='restored',
                    error_message=(
                        'reaped: heartbeat stale post-move '
                        '(forward-resolved to succeeded/restored)'
                    ),
                    completed_at=now,
                )
            )
        return 'forward-resolve'

    # Pre-move: rollback as before.
    async with conn.begin():
        await set_archive_status(
            row.user_id, row.job_id, 'archived', conn=conn,
        )
        await conn.execute(
            update(BackupRecord)
            .where(BackupRecord.id == row.id)
            .values(
                status='uploaded',
                error_message='reaped: heartbeat stale (restoring, pre-move)',
            )
        )
    return 'rollback'
