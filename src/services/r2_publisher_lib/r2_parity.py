"""R2 完整副本守门 — Stage B cleanup parity gate.

Plan: docs/plans/2026-05-07-disk-relief-via-r2-publisher-and-ttl.md §5.1

Purpose
-------

Gateway 7-day TTL cleanup wants to ``shutil.rmtree(project_dir)`` to free
local disk. Before Stage B this happened unconditionally on the timer.
After Stage B, ``project_cleanup.cleanup_expired_projects`` calls
:func:`r2_parity_ok` first and **skips both rmtree AND status flip** when
the function returns False, so the on-disk artifacts stay reachable
until the sweeper has finished pushing them to R2.

What "parity OK" means
----------------------

The function is intentionally strict — false-positives here lead to
silent data loss (cleanup deletes the only on-disk copy of an artifact
the sweeper hadn't actually pushed). The judges are:

1. **Every expected key has a registry entry at the current generation.**
   Expected set = ``eager_push_keys_for(service_mode)`` plus
   ``editor.jianying_draft_zip`` iff the caller's
   ``has_jianying_draft`` argument is True (sourced from
   ``JobRecord.jianying_draft_zip_path`` in the JSON store).
2. **No entry is in ``failed`` state.** Failed means "publisher tried
   and got an exception" — the artifact is NOT in R2.
3. **State value is recognised.** Unknown states are refused defensively.
4. **For ``pushed`` / ``already_present`` entries, R2 HEAD must succeed.**
   Defends against the case where the registry says the object is in
   R2 but a lifecycle rule / manual delete removed it.
   (``skipped_missing`` skips HEAD because the entry's contract is
   "the artifact was never generated on disk either" — both sides are
   consistent at "doesn't exist".)

Anything ambiguous → False. Cleanup will skip the row and try again
on its next pass. The sweeper will fill registry holes; eventually
parity passes and cleanup proceeds. If parity stays false for weeks,
that's an operator alert, not a silent disk-fill.

What this function MUST NOT do (invariants from memory)
-------------------------------------------------------

- **Do NOT call settle helpers** (``settle_job_quota``,
  ``settle_job_credit_ledger``, etc.). Parity is a *consumer* of
  terminal state; ``mirror_job_terminal_state`` is the single entry
  for terminal side effects (see feedback_terminal_state_single_entry).
- **Do NOT redefine pricing / quota / billing semantics.** Anything
  about credits / cost / plan-code lookups belongs in
  ``gateway.credits_service`` / runtime pricing — never here.
- **Do NOT make naming / filename decisions.** Names belong to the
  producer-consumer contract (see feedback_r2_publisher_consumer_contract).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from services.r2_publisher_lib.downloadable_keys import eager_push_keys_for

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = logging.getLogger(__name__)


# States the parity gate accepts as "the artifact is in a defined,
# non-failing place". ``failed`` is explicitly NOT here — failures need
# to be re-attempted by the sweeper before cleanup is safe.
_OK_STATES: frozenset[str] = frozenset({
    "pushed",
    "already_present",
    "skipped_missing",
})


async def r2_parity_ok(
    db: "AsyncSession",
    job_id: str,
    *,
    has_jianying_draft: bool = False,
) -> bool:
    """Return True iff the Gateway is allowed to delete this job's
    local project_dir.

    Parameters
    ----------
    db
        Gateway DB session — used only for one ``SELECT Job WHERE
        job_id = ...`` query. We deliberately don't take the row
        lock; cleanup is single-writer and parity is read-only.
    job_id
        The job to check.
    has_jianying_draft
        Caller (cleanup loop) reads ``JobRecord.jianying_draft_zip_path``
        from the JSON store and passes True iff non-null. We never look
        at it here — the caller owns that signal so this module stays
        free of any JSON-store coupling.

    Returns
    -------
    bool
        True only if every expected artifact has a recognised, non-failed
        registry entry at the current ``edit_generation``, AND every
        pushable entry's R2 object actually exists per HEAD. False on
        any miss / exception — the caller treats False as "skip cleanup".
    """
    # Lazy imports keep this module light when the cleanup path is
    # disabled, and avoid pulling boto3 / SQLAlchemy at import time for
    # unit tests that monkeypatch them.
    from sqlalchemy import select
    from models import Job

    try:
        result = await db.execute(select(Job).where(Job.job_id == job_id))
        job = result.scalar_one_or_none()
    except Exception as exc:
        logger.warning(
            "r2_parity: job lookup failed job=%s (%s); refusing cleanup",
            job_id, exc,
        )
        return False

    if job is None:
        # Gateway PG doesn't know this row. Cleanup picked up something
        # off the disk that's not even tracked here — refuse to delete.
        return False

    expected: set[str] = set(eager_push_keys_for(job.service_mode))
    if has_jianying_draft:
        expected.add("editor.jianying_draft_zip")

    if not expected:
        # Defensive: every supported service_mode currently has at
        # least one eager key. Empty here means a config drift; refuse
        # cleanup so an operator notices.
        logger.warning(
            "r2_parity: empty expected key set for job=%s service_mode=%r",
            job_id, job.service_mode,
        )
        return False

    registry: list[dict] | None = job.r2_artifacts
    if not registry:
        # ``r2_artifacts IS NULL`` means the sweeper hasn't processed
        # this job yet (or an overwrite cleared it). Either way: not
        # safe to delete the on-disk copy.
        return False

    expected_gen = job.edit_generation or 0

    # Index registry by artifact_key for the matching generation only.
    # Older-generation entries don't count — an overwrite produced new
    # R2 objects under g{N+1}/ and the old ones are forensic only.
    entries_by_key: dict[str, dict] = {}
    for item in registry:
        if not isinstance(item, dict):
            continue
        if item.get("edit_generation") != expected_gen:
            continue
        key = item.get("artifact_key")
        if isinstance(key, str):
            entries_by_key[key] = item

    # Pass 1: every expected key must have an OK-state entry.
    for key in expected:
        entry = entries_by_key.get(key)
        if entry is None:
            logger.info(
                "r2_parity: missing entry for %s (gen=%d) job=%s",
                key, expected_gen, job_id,
            )
            return False
        state = entry.get("state")
        if state not in _OK_STATES:
            logger.info(
                "r2_parity: bad state %r for %s job=%s",
                state, key, job_id,
            )
            return False

    # Pass 2: HEAD R2 for entries that claim to have an object there.
    # ``skipped_missing`` is excluded — by construction those entries
    # never had an on-disk file to push, so R2 also doesn't have them
    # and that's the consistent state we want.
    try:
        from storage import r2_client
    except Exception as exc:
        logger.warning(
            "r2_parity: r2_client import failed (%s); refusing cleanup", exc,
        )
        return False

    for key in expected:
        entry = entries_by_key[key]
        if entry.get("state") not in ("pushed", "already_present"):
            continue
        r2_key = entry.get("r2_key")
        if not isinstance(r2_key, str) or not r2_key:
            logger.info(
                "r2_parity: missing r2_key in %s entry job=%s",
                key, job_id,
            )
            return False
        try:
            if not r2_client.head_artifact(r2_key):
                logger.warning(
                    "r2_parity: R2 HEAD miss for %s (key=%s) job=%s; "
                    "registry says pushed but object is gone",
                    key, r2_key, job_id,
                )
                return False
        except Exception as exc:
            logger.warning(
                "r2_parity: R2 HEAD raised for %s (key=%s) job=%s (%s); "
                "refusing cleanup",
                key, r2_key, job_id, exc,
            )
            return False

    return True
