"""Lightweight Free-user quota management.

State machine: none → reserved → committed | released

- reserve():  quota_used++, quota_state='reserved'  (at job creation)
- commit():   quota_state='committed'                (job succeeded)
- release():  quota_used--, quota_state='released'   (job failed/cancelled)

All transitions are guarded: only valid source states are accepted.
This prevents double-release, double-commit, and other inconsistencies.
"""
from __future__ import annotations

import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Job, User

logger = logging.getLogger(__name__)

# Terminal job statuses that should trigger quota settlement
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}

# Statuses that indicate failure/cancellation → release quota
RELEASE_STATUSES = {"failed", "cancelled"}


async def check_quota(db: AsyncSession, user: User) -> tuple[bool, int, int]:
    """Check if free user has remaining quota.

    Returns (has_quota, used, total).
    For non-free users, always returns (True, 0, 0).
    """
    plan = getattr(user, "plan_code", "free") or "free"
    if plan != "free":
        return True, 0, 0

    total = getattr(user, "free_jobs_quota_total", 5)
    used = getattr(user, "free_jobs_quota_used", 0)
    return used < total, used, total


async def reserve_quota(db: AsyncSession, user_id, job: Job) -> bool:
    """Pre-deduct 1 quota unit for a free user.  Returns True if reserved.

    Must be called within the same transaction as job creation.
    Only acts on free-plan users with quota_state='none'.
    """
    if job.quota_state != "none":
        logger.warning("reserve_quota: job %s already in state %s, skipping",
                       job.job_id, job.quota_state)
        return False

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        return False

    role = getattr(user, "role", "user") or "user"
    plan = getattr(user, "plan_code", "free") or "free"
    if role == "admin" or plan != "free":
        # Admins and non-free plans bypass free-quota counters.
        job.quota_state = "reserved"
        return True

    total = getattr(user, "free_jobs_quota_total", 5)
    used = getattr(user, "free_jobs_quota_used", 0)
    if used >= total:
        return False  # Quota exhausted — caller should return error

    user.free_jobs_quota_used = used + 1
    job.quota_state = "reserved"
    logger.info("Quota reserved for job %s: %d/%d → %d/%d",
                job.job_id, used, total, used + 1, total)
    return True


async def commit_quota(db: AsyncSession, job: Job) -> bool:
    """Mark quota as committed (job succeeded). No refund possible.

    Only transitions from 'reserved' → 'committed'.
    """
    if job.quota_state != "reserved":
        logger.debug("commit_quota: job %s in state %s, skipping",
                     job.job_id, job.quota_state)
        return False

    job.quota_state = "committed"
    logger.info("Quota committed for job %s", job.job_id)
    return True


async def release_quota(db: AsyncSession, job: Job) -> bool:
    """Release reserved quota (job failed/cancelled). Refund 1 unit to free user.

    Only transitions from 'reserved' → 'released'.
    """
    if job.quota_state != "reserved":
        logger.debug("release_quota: job %s in state %s, skipping",
                     job.job_id, job.quota_state)
        return False

    # Find the job's owner to refund
    result = await db.execute(select(User).where(User.id == job.user_id))
    user = result.scalar_one_or_none()
    if user is not None:
        plan = getattr(user, "plan_code", "free") or "free"
        if plan == "free":
            current_used = getattr(user, "free_jobs_quota_used", 0)
            if current_used > 0:
                user.free_jobs_quota_used = current_used - 1
                logger.info("Quota released for job %s: %d → %d",
                            job.job_id, current_used, current_used - 1)

    job.quota_state = "released"
    return True


async def settle_job_quota(db: AsyncSession, job: Job, terminal_status: str) -> None:
    """Settle quota based on terminal job status.

    Called during status sync when a job transitions to a terminal state.
    """
    if job.quota_state not in ("none", "reserved"):
        return  # Already settled

    if job.quota_state == "none":
        # Job was created before quota tracking — nothing to settle
        return

    if terminal_status == "succeeded":
        await commit_quota(db, job)
    elif terminal_status in RELEASE_STATUSES:
        await release_quota(db, job)
