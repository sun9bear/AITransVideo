"""Status mutator for pan backup states (archiving / archived / restoring).

Plan 2026-05-13 §3.1 + §7. Writes Gateway PG Job.status + JSON store in
lockstep.

**Does NOT call mirror_job_terminal_state** because:
  - mirror is JSON → PG direction (we need the reverse here)
  - mirror handles credit settle on terminal states; archive is NOT credit-bearing
  - archive states are gateway-only, no upstream JSON writer

Atomicity: PG write goes through the caller's connection / transaction.
JSON write happens AFTER PG write inside its own file_lock (independent of
the PG txn). If the JSON write fails the PG write is NOT rolled back —
`backup_records.status` is source of truth, JSON mirror is best-effort.

CodeX C2 + C7 revisions:
  - Signature takes `conn: AsyncConnection` (not `db: AsyncSession`) so
    the executor's single-connection long-hold pattern (needed for
    `pg_advisory_lock`) is honored.
  - `services._file_lock` lives under src/ and needs the same sys.path
    bootstrap that admin_settings.py uses.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path

# Make src/ importable so we can reuse services._file_lock (cross-platform
# reentrant lock). Mirrors the bootstrap in gateway/admin_settings.py:12-20.
for _candidate in [
    Path(__file__).resolve().parent.parent.parent / "src",   # local dev
    Path("/opt/aivideotrans/app/src"),                       # Docker container
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from pan._lock_keys import pan_lock_key
from services._file_lock import file_lock

logger = logging.getLogger(__name__)


async def set_archive_status(
    user_id: _uuid.UUID,
    job_id: str,
    new_status: str,
    *,
    conn: AsyncConnection,
) -> None:
    """Write Job.status to PG (via caller's `conn`) and mirror to JSON store.

    Caller is responsible for transaction scope around `conn` — typically
    `async with conn.begin(): await set_archive_status(...)`.

    The PG UPDATE MUST hit exactly one row. If it hits zero (user_id/job_id
    doesn't match any row) we raise RuntimeError BEFORE touching the JSON
    mirror — otherwise a mismatched call could update a stale JSON file
    while PG was unchanged, creating a split-brain between source-of-truth
    (PG) and best-effort mirror (JSON).

    The JSON mirror is best-effort: if the JSON file is missing we skip
    (PG is authoritative for archive states); if the read/write fails we
    log a warning and return success (do NOT rollback PG).
    """
    # Local import so the module loads without database wired in (matters
    # for the contract test that checks no mirror import path exists).
    from models import Job

    result = await conn.execute(
        update(Job)
        .where(Job.user_id == user_id, Job.job_id == job_id)
        .values(status=new_status)
    )
    # Guard against silent 0-row UPDATE. job_id is unique so > 1 shouldn't
    # happen, but we accept >= 1 defensively — it would still mean the
    # intended row was updated.
    if result.rowcount == 0:
        raise RuntimeError(
            f"set_archive_status: no Job matched user_id={user_id} "
            f"job_id={job_id!r} — refusing to write JSON mirror."
        )

    jobs_dir = Path(os.environ.get(
        'AIVIDEOTRANS_JOBS_DIR', '/opt/aivideotrans/app/jobs'
    ))
    json_path = jobs_dir / f'{job_id}.json'
    if not json_path.exists():
        # JSON store optional for gateway-only states. PG is authoritative.
        return

    try:
        with file_lock(json_path):
            record = json.loads(json_path.read_text(encoding='utf-8'))
            record['status'] = new_status
            json_path.write_text(
                json.dumps(record, indent=2, ensure_ascii=False),
                encoding='utf-8',
            )
    except Exception as exc:  # noqa: BLE001
        # JSON mirror failure is best-effort — log and proceed.
        logger.warning(
            "set_archive_status JSON mirror failed for job=%s: %s",
            job_id, exc,
        )


# ---------------------------------------------------------------------------
# Controlled operator recovery (2026-05-26 postmortem P2b)
# ---------------------------------------------------------------------------

_DEFAULT_ROLLBACK_REASON = 'rollback_archive_attempt: operator-initiated abort'

# Job statuses rollback_archive_attempt is allowed to touch. Anything else
# ('restoring', 'running', 'archived', ...) is owned by other recovery paths
# (stale_reaper Pass 1 for restores, Pass 2 + residue_cleanup for
# post-commit archiving) and must be refused here.
_ROLLBACK_ELIGIBLE_JOB_STATUSES = ('archiving', 'succeeded')


async def _try_advisory_lock(conn: AsyncConnection, key: int) -> bool:
    """pg_try_advisory_lock — non-blocking probe. No-op on SQLite (True).

    Local copy of the helper in ``pan.stale_reaper`` — importing it from
    there would be a circular import (stale_reaper imports
    ``set_archive_status`` from this module at top level).
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


async def rollback_archive_attempt(
    user_id: _uuid.UUID,
    job_id: str,
    *,
    conn: AsyncConnection,
    reason: str | None = None,
) -> dict:
    """Controlled rollback of an aborted pan backup attempt (postmortem P2b).

    THE ONLY sanctioned way for an operator to abort an in-flight backup.
    Raw ``UPDATE jobs SET status=...`` / partial-state edits are banned by
    runbook: the 2026-06-02 deploy abort marked ``backup_records`` 'failed'
    by hand but never flipped ``Job.status`` back to 'succeeded', producing
    a state no stale_reaper pass matched (Job='archiving' + all BRs
    'failed') while ``admin_api.create_backup`` requires
    ``status == 'succeeded'`` — job_c31bd38126fd47ed8c2d3c1749c15ccf sat
    deadlocked in 'archiving' for 7 days (found 2026-06-10 on US prod).

    What it does, atomically and idempotently:
      1. Probe ``pg_try_advisory_lock(pan_lock_key(user, job))``. If held,
         an executor is STILL ALIVE → refuse (RuntimeError). This entry
         point must never bypass the per-job advisory lock.
      2. Refuse unless ``Job.status`` ∈ ('archiving', 'succeeded').
      3. Refuse if a BackupRecord at the CURRENT edit_generation is
         'uploaded' while Job='archiving' — the COMMIT POINT passed, local
         data may already be gone; that state belongs to stale_reaper
         Pass 2 + residue_cleanup (which flips to 'archived'), NOT to a
         rollback to 'succeeded'.
      4. In one txn: mark all 'uploading' BackupRecords for (user, job)
         'failed' (+ ``error_message``/``completed_at``), and if
         Job='archiving' flip it back to 'succeeded' via
         ``set_archive_status`` (PG + JSON mirror in lockstep).

    Already rolled back (Job='succeeded', no 'uploading' BRs) → no-op
    success with ``changed=False``. Never enqueues tasks, never calls any
    paid API, and — per the module-level rule — never touches
    mirror_job_terminal_state (archive states are not credit-bearing).

    Returns a summary dict for runbook/audit logging::

        {'job_id', 'status_before', 'status_after',
         'backup_records_failed', 'changed'}
    """
    from models import BackupRecord, Job

    lock_key = pan_lock_key(user_id, job_id)
    got = await _try_advisory_lock(conn, lock_key)
    if not got:
        raise RuntimeError(
            f"rollback_archive_attempt: advisory lock held for "
            f"user={user_id} job={job_id!r} — an executor is still active. "
            f"Refusing to roll back; wait for it to finish or die "
            f"(stale_reaper reaps dead ones within ~4h)."
        )
    # CodeX P1-3 pattern (see stale_reaper): the lock-probe SELECT
    # auto-begins a txn; flush it so the explicit begin() below works.
    # The advisory lock is session-scoped — commit does not release it.
    await conn.commit()
    try:
        job_row = (await conn.execute(
            select(Job.status, Job.edit_generation).where(
                Job.user_id == user_id, Job.job_id == job_id,
            )
        )).one_or_none()
        if job_row is None:
            raise RuntimeError(
                f"rollback_archive_attempt: no Job matched "
                f"user_id={user_id} job_id={job_id!r}"
            )
        if job_row.status not in _ROLLBACK_ELIGIBLE_JOB_STATUSES:
            raise RuntimeError(
                f"rollback_archive_attempt: Job status {job_row.status!r} is "
                f"not rollback-eligible (expected one of "
                f"{_ROLLBACK_ELIGIBLE_JOB_STATUSES}). Restores are owned by "
                f"stale_reaper Pass 1; archived jobs need a restore, not a "
                f"rollback."
            )

        uploaded_current_gen = (await conn.execute(
            select(BackupRecord.id).where(
                BackupRecord.user_id == user_id,
                BackupRecord.job_id == job_id,
                BackupRecord.job_edit_generation == job_row.edit_generation,
                BackupRecord.status == 'uploaded',
            ).limit(1)
        )).first()
        if job_row.status == 'archiving' and uploaded_current_gen is not None:
            raise RuntimeError(
                f"rollback_archive_attempt: job {job_id!r} passed the COMMIT "
                f"POINT (BackupRecord 'uploaded' at current generation "
                f"{job_row.edit_generation}) — local data may already be "
                f"cleaned up. Let stale_reaper Pass 2 / residue_cleanup "
                f"finalize it to 'archived' instead of rolling back."
            )
        await conn.commit()  # close the read txn before explicit begin()

        now = datetime.now(timezone.utc)
        async with conn.begin():
            result = await conn.execute(
                update(BackupRecord)
                .where(
                    BackupRecord.user_id == user_id,
                    BackupRecord.job_id == job_id,
                    BackupRecord.status == 'uploading',
                )
                .values(
                    status='failed',
                    error_message=reason or _DEFAULT_ROLLBACK_REASON,
                    completed_at=now,
                )
            )
            records_failed = int(result.rowcount or 0)
            flipped = False
            if job_row.status == 'archiving':
                await set_archive_status(
                    user_id, job_id, 'succeeded', conn=conn,
                )
                flipped = True

        summary = {
            'job_id': job_id,
            'status_before': job_row.status,
            'status_after': 'succeeded',
            'backup_records_failed': records_failed,
            'changed': flipped or records_failed > 0,
        }
        logger.info(
            "rollback_archive_attempt: user=%s job=%s before=%s "
            "records_failed=%d changed=%s",
            user_id, job_id, job_row.status, records_failed,
            summary['changed'],
        )
        return summary
    finally:
        await _release_advisory_lock(conn, lock_key)
        # Leave the connection outside any implicit txn so the caller can
        # open its own begin() afterwards.
        try:
            await conn.commit()
        except Exception:  # noqa: BLE001
            pass
