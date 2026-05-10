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
- **Quota settle is idempotent.** ``settle_job_quota`` (gateway/quota.py:131)
  guards on ``quota_state ∈ {"none", "reserved"}``; calling it twice
  for the same job is safe. We still gate on
  ``upstream_status in TERMINAL_STATUSES and old_status not in TERMINAL_STATUSES``
  to avoid redundant work, not because it would be incorrect.
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

from quota import TERMINAL_STATUSES, settle_job_quota

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
    succeeded       | succeeded       | sync fields if differ; no settle
    (any non-purged)| (any)           | sync fields; settle iff entering terminal
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

    # Quota settlement on terminal entry. settle_job_quota itself guards
    # against double-settling (quota.py:131 checks quota_state). The
    # entry-condition check below is just to avoid the function call
    # overhead for the common steady-state mirror.
    if upstream_status in TERMINAL_STATUSES and old_status not in TERMINAL_STATUSES:
        try:
            await settle_job_quota(db, db_job, upstream_status)
        except Exception as exc:
            # Don't propagate — quota glitches must not block status
            # mirroring. Worst case the user has a leaked reservation
            # the cleanup or admin tooling will eventually rectify.
            logger.warning(
                "mirror: settle_job_quota failed job=%s status=%s (%s)",
                db_job.job_id, upstream_status, exc,
            )

    return changed
