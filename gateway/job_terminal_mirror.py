"""Mirror job terminal state from JSON store into Gateway PG.

Plan: docs/plans/2026-05-07-disk-relief-via-r2-publisher-and-ttl.md §4.5
(P1.1 fix from CodeX review #3)

Why this exists
---------------

Two callers need to bring Gateway PG ``Job`` rows into agreement with
the authoritative JSON store written by ``process_runner._finalize_process``:

1. ``intercept_list_jobs`` (gateway/job_intercept.py) — runs on every
   user list-jobs request. This is the legacy path; without anyone hitting
   the list page, jobs that finished in the background never propagate.

2. ``r2_artifact_sweeper.sweep_once`` — runs on a 300s loop and discovers
   newly-succeeded jobs from JSON store directly. Without this helper,
   a sweeper-driven sync would skip ``settle_job_quota`` and the user's
   reserved quota would never be released.

Both callers must produce identical side-effects, hence one helper.

Invariants
----------

- **purged is sticky.** If Gateway PG already wrote ``status=purged`` it
  means cleanup ran and removed disk artifacts; the JSON record is a
  ghost we deliberately do not resurrect. Returning False means "no
  change, do not commit".
- **Terminal settle is idempotent and compensating.** ``settle_job_quota``
  guards on ``quota_state ∈ {"none", "reserved"}``, while
  ``settle_job_credit_ledger`` guards on job-level capture/release rows.
  We call both for every observed terminal upstream state, not only on
  the non-terminal -> terminal edge, because other request paths can
  legitimately sync ``Job.status`` before settlement runs.
- **Mirror only mirror fields.** ``r2_artifacts``, ``display_name``,
  ``expires_at``, etc. are Gateway-owned. We never overwrite them here.
- **Caller commits.** This function mutates the SQLAlchemy session but
  does not call ``await db.commit()``. Callers know their batching
  strategy (intercept_list_jobs commits per-user; sweeper commits
  per-iteration).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from credits_service import settle_job_credit_ledger
from quota import TERMINAL_STATUSES, settle_job_quota
from cost_summary_backfill import backfill_smart_cost_summary

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from models import Job
    from storage.job_store_reader import JobJsonRecord

logger = logging.getLogger(__name__)


async def mirror_job_terminal_state(
    db: "AsyncSession",
    db_job: "Job",
    upstream: "JobJsonRecord",
) -> bool:
    """Sync a Gateway PG ``Job`` row from the corresponding JSON record.

    Returns True iff any field was updated (or quota was settled).
    Caller is responsible for ``await db.commit()``.

    Behavior matrix
    ---------------

    Old PG status   | Upstream status | Action
    ----------------|-----------------|------------------------------------
    purged          | (any)           | no-op (return False)
    queued / running| succeeded       | sync fields + settle_job_quota
    queued / running| failed          | sync fields + settle_job_quota
    succeeded       | succeeded       | sync fields + compensate missing settle
    (any non-purged)| (any)           | sync fields; settle iff upstream terminal
    """
    if db_job.status == "purged":
        return False

    changed = False
    old_status = db_job.status
    upstream_status = upstream.status or ""

    if upstream_status and upstream_status != old_status:
        db_job.status = upstream_status
        changed = True

    # Mirror the rest of the fields only when upstream actually has a
    # value. Don't clobber a good DB value with a transient upstream
    # omission (e.g. JSON write that hasn't filled the field yet).
    if upstream.current_stage and upstream.current_stage != db_job.current_stage:
        db_job.current_stage = upstream.current_stage
        changed = True
    if upstream.project_dir and upstream.project_dir != db_job.project_dir:
        db_job.project_dir = upstream.project_dir
        changed = True
    if upstream.completed_at and upstream.completed_at != db_job.completed_at:
        db_job.completed_at = upstream.completed_at
        changed = True
    # edit_generation drift fix (Day 2 follow-up). Without this, a job
    # whose JSON store records generation N+1 (because the user did an
    # editing/commit overwrite) but whose Gateway PG row is still at
    # generation N (because intercept_list_jobs didn't sync it before
    # this fix) will fail _run_publish's race-protection forever:
    # sweeper snapshots N+1, _run_publish sees PG=N, refuses to land
    # results. We saw this in production on the first Day-2 backfill
    # (plan 2026-05-07).
    #
    # Only mirror when upstream actually carries the value (None means
    # "field not present in payload" — see JobJsonRecord docstring).
    if (
        upstream.edit_generation is not None
        and upstream.edit_generation != (db_job.edit_generation or 0)
    ):
        db_job.edit_generation = upstream.edit_generation
        changed = True

    # Smart MVP P2 (plan §4.2 末段) — mirror smart_state BEFORE the settle
    # block below, so the F4 dispatcher in settle_job_credit_ledger sees
    # the latest credits_policy from the JSON store. Merge semantics
    # (last-write-wins per key) instead of full replace so partial
    # marker updates from earlier pipeline frames aren't clobbered by a
    # final-frame write that only carries one new field. None on
    # upstream.smart_state means "not present in upstream payload" — do
    # not clobber an existing DB value.
    if upstream.smart_state is not None:
        merged_smart_state = dict(getattr(db_job, "smart_state", None) or {})
        merged_smart_state.update(upstream.smart_state)
        # Compare against the existing dict to avoid spurious changed=True
        # on a no-op poll (mirror is level-triggered and runs frequently).
        if merged_smart_state != (getattr(db_job, "smart_state", None) or {}):
            db_job.smart_state = merged_smart_state
            changed = True

    # Terminal settlement is deliberately level-triggered rather than only
    # edge-triggered. ``GET /jobs/{id}`` notification polling, list-jobs, and
    # the R2 sweeper can observe the same terminal JSON record in different
    # orders; settlement must be safe to retry and able to repair a PG row that
    # already says "succeeded" but still has reserved quota/credits.
    if upstream_status in TERMINAL_STATUSES:
        if getattr(db_job, "is_anonymous_preview", False) is True:
            # APF P0 T8（AD-7/G2）：匿名预览 job 零结算不变量——跳过
            # settle_job_quota / settle_job_credit_ledger / cost backfill，
            # 但上方的状态字段镜像已照常完成（orphan reconciliation 与
            # cleanup 依赖终态可见）。G2 红线：禁止在函数入口整体
            # return，否则状态镜像一起丢失。
            logger.info(
                "mirror: anonymous preview job=%s terminal=%s — settlement bypassed",
                db_job.job_id, upstream_status,
            )
            return changed
        quota_state_before = getattr(db_job, "quota_state", None)
        try:
            await settle_job_quota(db, db_job, upstream_status)
            if getattr(db_job, "quota_state", None) != quota_state_before:
                changed = True
        except Exception as exc:
            # Don't propagate — quota glitches must not block status
            # mirroring. Worst case the user has a leaked reservation
            # the cleanup or admin tooling will eventually rectify.
            logger.warning(
                "mirror: settle_job_quota failed job=%s status=%s (%s)",
                db_job.job_id, upstream_status, exc,
            )
        # Track whether settle completed cleanly. Codex 第三十九轮 P1:
        # if settle raises, ``credit_entries`` would have stayed [] and
        # backfill below would write a misleading
        # ``pending_credits_charged=0`` plus stamp ``settled_at``,
        # making admin think the job settled at 0 credits. Skip
        # backfill on failure so the pre-settle ``pending_*=null``
        # state is preserved until a successful retry.
        settle_succeeded = False
        try:
            credit_entries = await settle_job_credit_ledger(db, db_job, upstream_status)
            if credit_entries:
                changed = True
            settle_succeeded = True
        except Exception as exc:
            logger.warning(
                "mirror: settle_job_credit_ledger failed job=%s status=%s (%s)",
                db_job.job_id, upstream_status, exc,
            )

        # Smart MVP Phase 2 (decision log §2): post-settle cost_summary
        # backfill replaces the two ``pending_*`` fields with real
        # values. Best-effort — backfill failure must NOT block the
        # mirror callback (plan §6.4 末段). Skips silently for
        # non-smart jobs / missing project_dir / missing file / failed
        # settle.
        try:
            await _backfill_smart_cost_summary_post_settle(
                db, db_job=db_job, settle_succeeded=settle_succeeded,
            )
        except Exception as exc:
            logger.warning(
                "mirror: cost_summary backfill failed job=%s (%s)",
                getattr(db_job, "job_id", "?"), exc,
            )

    return changed


async def _backfill_smart_cost_summary_post_settle(
    db: "AsyncSession",
    *,
    db_job: "Job",
    settle_succeeded: bool,
) -> None:
    """Internal Phase 2 backfill orchestrator (Codex 第三十九轮 P1):
    queries the CANONICAL persisted ``CreditsLedger`` for the job and
    delegates to the pure helper.

    Gates:
      - ``settle_succeeded=False`` (settle raised) → return early;
        leave ``pending_*=null`` so subsequent successful retry can
        backfill correctly.
      - ``service_mode != "smart"`` → return early.
      - ``project_dir`` missing → return early.

    Why query persisted ledger instead of trusting the per-call return:
      - Mirror is level-triggered; ``settle_job_credit_ledger`` has
        idempotency guards (``_has_existing_settlement``) that return
        ``[]`` on subsequent passes even though the job WAS charged.
        Using the empty return would write
        ``pending_credits_charged=0`` for already-settled jobs.
      - Historical jobs settled BEFORE this Phase 2 deploy have ledger
        rows but never went through the new backfill path. Querying
        persisted state lets us backfill them on the next level-
        triggered mirror pass.

    Quota lookup uses the same query shape as
    ``user_voice_api.internal_user_voice_quota``. Failure → quota_used
    stays None (fail-closed per Codex 第二十七轮 P0).
    """
    if not settle_succeeded:
        return  # Codex 39 P1: don't stamp settled_at on failed settle

    service_mode = getattr(db_job, "service_mode", None)
    if (service_mode or "").lower() != "smart":
        return
    project_dir = getattr(db_job, "project_dir", None)
    if not project_dir:
        return

    job_id = getattr(db_job, "job_id", None)
    user_id = getattr(db_job, "user_id", None)

    # Codex 39 P1: query CANONICAL persisted ledger by related_job_id.
    # Single source of truth regardless of (a) idempotent settle return
    # of [] or (b) historical jobs settled pre-Phase-2.
    persisted_entries: list = []
    try:
        from sqlalchemy import select
        from models import CreditsLedger

        result = await db.execute(
            select(CreditsLedger).where(
                CreditsLedger.related_job_id == job_id,
            )
        )
        persisted_entries = list(result.scalars().all())
    except Exception as exc:
        logger.warning(
            "backfill: persisted ledger query failed for job=%s (%s) — "
            "skipping backfill to avoid stamping incomplete settled_at",
            job_id, exc,
        )
        return

    quota_used: int | None = None
    if user_id is not None:
        try:
            from sqlalchemy import func, select
            from models import UserVoice

            result = await db.execute(
                select(func.count())
                .select_from(UserVoice)
                .where(
                    UserVoice.user_id == user_id,
                    UserVoice.expired_at.is_(None),
                )
            )
            quota_used = int(result.scalar() or 0)
        except Exception as exc:
            logger.warning(
                "backfill: quota lookup failed for user=%s job=%s (%s)",
                user_id, job_id, exc,
            )
            quota_used = None  # fail-closed: leave field null

    backfill_smart_cost_summary(
        service_mode=str(service_mode or ""),
        project_dir=str(project_dir),
        credit_entries=persisted_entries,
        quota_used=quota_used,
    )
