"""Pan archive candidate scanner (Phase 8 §T8.1).

Plan 2026-05-13 §10. Daily 03:30 BJT (= 19:30 UTC) cron: scan all admin
users' jobs whose `updated_at < now - 30d` and aren't already covered by
an in-flight or completed backup at the current edit_generation; enqueue
`pan_backup` BackgroundTask for up to N candidates (default 5) per run.

## Candidate selection (spec §10)

  SELECT j.job_id, j.user_id, j.edit_generation
  FROM jobs j JOIN users u ON u.id = j.user_id
  WHERE u.role = 'admin'
    AND j.status = 'succeeded'
    AND j.updated_at < now() - interval '30 days'
    AND NOT EXISTS (
        SELECT 1 FROM backup_records br
        WHERE br.user_id = j.user_id
          AND br.job_id = j.job_id
          AND br.job_edit_generation = j.edit_generation
          AND br.status IN ('uploading', 'uploaded', 'restoring')
    )
    AND EXISTS (
        SELECT 1 FROM pan_credentials pc
        WHERE pc.user_id = j.user_id AND pc.status = 'active'
    )
  ORDER BY j.updated_at ASC   -- oldest first, deterministic
  LIMIT :max_per_run

## Idempotency

The NOT EXISTS guard means a candidate selected this tick won't appear
in the next tick (the enqueued backup_executor INSERTs a row with
status='uploading'). If executor fails before INSERT, the candidate
will re-surface next tick — backed off implicitly by the schedule
(daily) so we don't hammer.

## dry_run

`run_archive_scanner_tick(dry_run=True)` returns the candidate list
without enqueuing. Used by tests + admin diagnostic.
"""
from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import BackupRecord, Job, PanCredentials, User


logger = logging.getLogger(__name__)


async def run_archive_scanner_tick(
    db: AsyncSession,
    *,
    age_days: int = 30,
    max_per_run: int = 5,
    dry_run: bool = False,
) -> dict[str, Any]:
    """One tick of the archive scanner.

    Returns a dict:
      {'candidates': [{'job_id', 'user_id', 'edit_generation'}],
       'enqueued': int,
       'enqueued_task_ids': [str],
       'dry_run': bool}

    Failure to enqueue a single candidate is logged + counted in
    `failed_enqueue`; the tick continues for remaining candidates.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=age_days)

    # Subquery: any in-flight backup at this gen?
    in_flight_subq = (
        select(BackupRecord.id)
        .where(
            BackupRecord.user_id == Job.user_id,
            BackupRecord.job_id == Job.job_id,
            BackupRecord.job_edit_generation == Job.edit_generation,
            BackupRecord.status.in_(['uploading', 'uploaded', 'restoring']),
        )
    )
    # Subquery: does user have active pan credentials?
    has_creds_subq = (
        select(PanCredentials.id)
        .where(
            PanCredentials.user_id == Job.user_id,
            PanCredentials.status == 'active',
        )
    )

    stmt = (
        select(
            Job.job_id, Job.user_id, Job.edit_generation, Job.updated_at,
        )
        .join(User, User.id == Job.user_id)
        .where(
            User.role == 'admin',
            Job.status == 'succeeded',
            Job.updated_at < cutoff,
            ~exists(in_flight_subq),
            exists(has_creds_subq),
        )
        .order_by(Job.updated_at.asc())
        .limit(max_per_run)
    )

    rows = (await db.execute(stmt)).all()
    candidates: list[dict[str, Any]] = [
        {
            'job_id': r.job_id,
            'user_id': str(r.user_id),
            'edit_generation': r.edit_generation,
            'updated_at': r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]

    result: dict[str, Any] = {
        'candidates': candidates,
        'enqueued': 0,
        'enqueued_task_ids': [],
        'failed_enqueue': [],
        'dry_run': dry_run,
    }
    if dry_run or not candidates:
        logger.info(
            "pan_archive_scanner: tick found %d candidates%s",
            len(candidates), " (dry_run)" if dry_run else "",
        )
        return result

    # Enqueue background tasks. Per-candidate try/except so one bad row
    # doesn't kill the rest of the batch.
    import background_task_queue as queue
    for c in candidates:
        try:
            task_id, _ = await queue.create_task(
                db,
                job_id=c['job_id'],
                user_id=_uuid.UUID(c['user_id']),
                task_type='pan_backup',
                params={'user_id': c['user_id']},
            )
            await db.commit()
            result['enqueued'] += 1
            result['enqueued_task_ids'].append(task_id)
            logger.info(
                "pan_archive_scanner: enqueued backup task=%s job=%s user=%s",
                task_id, c['job_id'], c['user_id'],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "pan_archive_scanner: enqueue failed job=%s user=%s err=%s",
                c['job_id'], c['user_id'], exc,
            )
            result['failed_enqueue'].append({
                'job_id': c['job_id'],
                'user_id': c['user_id'],
                'error': str(exc)[:200],
            })
            # Don't commit failed rows — rollback the partial state.
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass

    return result
