"""Backfill terminal jobs whose quota/credit settlement was skipped.

Dry-run by default. Run inside the gateway container:

    python /opt/gateway/scripts/settle_terminal_credit_leaks.py --job-id job_x
    python /opt/gateway/scripts/settle_terminal_credit_leaks.py --job-id job_x --commit

Why this exists:
some request paths can observe a terminal upstream job after another path has
already mirrored ``jobs.status`` but before quota/credit settlement happened.
The runtime fix makes terminal settlement level-triggered and idempotent; this
script repairs rows that were already stranded before the fix.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

from credits_service import settle_job_credit_ledger, should_settle_job_credits  # noqa: E402
from database import async_session, init_db  # noqa: E402
from models import CreditsLedger, Job  # noqa: E402
from quota import TERMINAL_STATUSES, settle_job_quota  # noqa: E402

logger = logging.getLogger("settle_terminal_credit_leaks")

JOB_SETTLEMENT_REASONS = {
    "job_capture",
    "job_release",
    "capture_additional",
    "capture_overdraft",
    "capture_excess_release",
}


def _snapshot_int(snapshot: Any, key: str) -> int:
    if not isinstance(snapshot, dict):
        return 0
    try:
        return int(snapshot.get(key) or 0)
    except (TypeError, ValueError):
        return 0


async def _has_job_reserve(db, job_id: str) -> bool:
    result = await db.execute(
        select(CreditsLedger.id)
        .where(
            CreditsLedger.related_job_id == job_id,
            CreditsLedger.direction == "reserve",
            CreditsLedger.reason_code == "job_reserve",
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _has_job_settlement(db, job_id: str) -> bool:
    result = await db.execute(
        select(CreditsLedger.id)
        .where(
            CreditsLedger.related_job_id == job_id,
            CreditsLedger.direction.in_(["capture", "release"]),
            CreditsLedger.reason_code.in_(JOB_SETTLEMENT_REASONS),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _candidate_summary(db, job: Job) -> tuple[bool, bool, bool]:
    if not should_settle_job_credits(job):
        return False, False, False
    has_reserve = await _has_job_reserve(db, job.job_id)
    has_settlement = await _has_job_settlement(db, job.job_id)
    has_credit_intent = (
        _snapshot_int(job.metering_snapshot, "credits_estimated") > 0
        or has_reserve
    )
    needs_quota = job.quota_state == "reserved"
    needs_credit = has_credit_intent and not has_settlement
    return needs_quota, needs_credit, has_settlement


async def run(*, commit: bool, job_id: str | None, limit: int) -> int:
    async with async_session() as db:
        stmt = (
            select(Job)
            .where(Job.status.in_(sorted(TERMINAL_STATUSES)))
            .order_by(Job.completed_at.desc().nullslast(), Job.created_at.desc())
            .limit(limit)
        )
        if job_id:
            stmt = stmt.where(Job.job_id == job_id)
        result = await db.execute(stmt)
        jobs = list(result.scalars().all())

        repaired = 0
        for job in jobs:
            needs_quota, needs_credit, has_settlement = await _candidate_summary(db, job)
            if not needs_quota and not needs_credit:
                continue

            print(
                f"{'FIX' if commit else 'DRY'} job={job.job_id} "
                f"status={job.status} quota_state={job.quota_state} "
                f"needs_quota={needs_quota} needs_credit={needs_credit} "
                f"has_job_settlement={has_settlement}"
            )
            if not commit:
                continue

            try:
                await settle_job_quota(db, job, job.status)
                entries = await settle_job_credit_ledger(db, job, job.status)
                await db.commit()
                repaired += 1
                print(f"OK job={job.job_id} credit_entries={len(entries)}")
            except Exception:
                await db.rollback()
                logger.exception("failed to settle job=%s", job.job_id)

        if not commit:
            await db.rollback()
        return repaired


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="write repairs")
    parser.add_argument("--job-id", help="repair one job")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    init_db()
    repaired = asyncio.run(run(commit=args.commit, job_id=args.job_id, limit=args.limit))
    print(f"repaired={repaired} commit={args.commit}")


if __name__ == "__main__":
    main()
