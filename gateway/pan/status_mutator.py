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
from pathlib import Path

# Make src/ importable so we can reuse services._file_lock (cross-platform
# reentrant lock). Mirrors the bootstrap in gateway/admin_settings.py:12-20.
for _candidate in [
    Path(__file__).resolve().parent.parent.parent / "src",   # local dev
    Path("/opt/aivideotrans/app/src"),                       # Docker container
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncConnection

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
