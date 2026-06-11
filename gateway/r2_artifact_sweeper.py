"""Periodic sweeper that drives proactive R2 push for succeeded jobs.

Plan: docs/plans/2026-05-07-disk-relief-via-r2-publisher-and-ttl.md
§4.5-4.6 (sweeper data flow + implementation)

Design summary
--------------

1. **Source of truth = JSON store.** Gateway PG ``Job.status`` is
   updated by ``intercept_list_jobs`` only when a user actively loads
   the list, so jobs finished in the background never propagate. The
   sweeper reads ``services.jobs.store`` JSON files via the dedicated
   ``storage.job_store_reader`` module — no ``services.jobs.*`` import
   chain (CLAUDE.md / pydub isolation).

2. **Reverse-mirror on sight.** When the JSON record is ahead of PG,
   we call ``mirror_job_terminal_state`` to sync fields and settle
   quota. Sweeper iterations therefore double as the missing
   "no one opened the list page" sync path — we no longer rely on
   user traffic to release reservations.

3. **Two candidate predicates:**
     - ``r2_artifacts IS NULL``   → push the full eligible set
     - ``jianying_draft_zip_path != null AND registry lacks jianying entry``
       → delta-push just jianying

   Anything else is left alone. Editing-commit overwrites reset
   ``r2_artifacts`` back to NULL through ``_apply_editing_commit_gateway_side``,
   bringing the row into the first predicate.

4. **In-process executor.** ``background_task_queue.create_task`` only
   gets dispatched when a user POSTs to ``/api/jobs/.../tasks``. Adding
   a sweeper-driven row would not auto-launch. Instead we
   ``asyncio.create_task(_run_publish(...))`` directly inside the
   gateway event loop.

5. **Backoff.** A failed publish sets ``r2_push_retry_after = now + 5min``
   so the same job doesn't retry every loop. The same field is also set
   immediately when sweeper picks up a candidate (placeholder) so an
   in-flight task doesn't get re-entered if a second loop tick races.

6. **Two feature flags.** ``AVT_DOWNLOAD_REDIRECT_BACKEND`` already gates
   R2 downloads; we add ``AVT_R2_PROACTIVE_PUSH_ENABLED`` so we can
   ship the loop "off" and flip it on after the deploy. With both off,
   ``sweep_once`` returns 0 immediately — zero behavioral diff vs. v3.

Limits
------

- ``AVT_R2_SWEEPER_INTERVAL_S``                  default 300s
- ``AVT_R2_SWEEPER_BATCH_SIZE``                  default 5  (per pass)
- ``AVT_R2_SWEEPER_BACKFILL_RATE_PER_MIN``       default 2  (global)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update

# Make src/ importable so we can pull r2_publisher_lib (NOT services.jobs.*).
# Mirrors gateway/storage/backend_router.py:50-55.
for _candidate in [
    Path(__file__).resolve().parent.parent / "src",
    Path("/opt/aivideotrans/app/src"),
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from database import async_session
from models import Job
from storage.job_store_reader import (  # noqa: E402  (after sys.path tweak)
    JobJsonRecord,
    iter_succeeded_in_grace,
)
from job_terminal_mirror import mirror_job_terminal_state  # noqa: E402

logger = logging.getLogger(__name__)

# v4 final: 5min default (user 2026-05-08 decision §9). Knob still env-tunable
# so we can dial down in production if observed throughput is fine.
SWEEP_INTERVAL_S = int(os.environ.get("AVT_R2_SWEEPER_INTERVAL_S", "300"))
SWEEP_BATCH_SIZE = int(os.environ.get("AVT_R2_SWEEPER_BATCH_SIZE", "5"))
BACKFILL_RATE_PER_MIN = int(
    os.environ.get("AVT_R2_SWEEPER_BACKFILL_RATE_PER_MIN", "2")
)

# Tasks we placeholder-claim get a 5-minute lease so a second sweep tick
# during a slow PUT doesn't re-enter the same job. The same value is
# used for genuine retry backoff after a publisher failure.
LEASE_S = 300


def _is_enabled() -> bool:
    """Both gates must be set for the sweeper to do anything.

    Use ``storage.backend_router.is_r2_enabled()`` (which reads from
    ``settings``) instead of raw env so we respect the startup safety
    net applied by ``validate_r2_backend()`` when R2 credentials are
    missing.  The push flag is not downgraded at startup so it stays
    env-based.

    CodeX P2-4 (2026-05-18).
    """
    from storage.backend_router import is_r2_enabled
    if not is_r2_enabled():
        return False
    return (
        os.environ.get("AVT_R2_PROACTIVE_PUSH_ENABLED", "false").lower()
        == "true"
    )


def _registry_has_jianying(
    registry: list[dict] | None, edit_generation: int
) -> bool:
    if not registry:
        return False
    for item in registry:
        if (
            item.get("artifact_key") == "editor.jianying_draft_zip"
            and item.get("edit_generation") == edit_generation
        ):
            return True
    return False


def _classify_candidate(
    json_rec: JobJsonRecord, db_job: Job
) -> tuple[bool, frozenset[str] | None]:
    """Decide whether sweeper should publish for this job, and what subset.

    Returns
    -------
    (should_publish, push_keys)
        push_keys=None  → publisher pushes full eligible set
        push_keys=froz. → delta push (currently only used for jianying)
    """
    # APF P0 (AD-6): anonymous preview jobs run with service_mode="free" but
    # are stream-only — their teaser must NEVER be eager-pushed to R2 (it would
    # outlive the TTL-delete promise). The free-tier branch in
    # eager_push_keys_for would otherwise push them, since the sweeper keys off
    # service_mode alone. Short-circuit on the cross-cutting is_anonymous_preview
    # column (the minimal, contract-safe isolation point — service_mode stays
    # "free" so settlement / gating / watermark contracts are untouched).
    if getattr(db_job, "is_anonymous_preview", False) is True:
        return False, None

    expected_gen = db_job.edit_generation or 0

    # Primary trigger: never been published (or was reset by overwrite).
    if db_job.r2_artifacts is None:
        return True, None

    # Secondary trigger: jianying draft just got generated, registry
    # was already filled by an earlier full push and is missing this
    # specific entry.
    if (
        json_rec.jianying_draft_zip_path
        and not _registry_has_jianying(db_job.r2_artifacts, expected_gen)
    ):
        return True, frozenset({"editor.jianying_draft_zip"})

    return False, None


async def sweep_once(now: datetime | None = None) -> int:
    """One iteration. Returns the count of publish tasks dispatched."""
    if not _is_enabled():
        return 0
    now = now or datetime.now(timezone.utc)

    # Per-pass cap derived from the per-minute rate. With default
    # 300s interval and 2/min rate, that's 10/pass — but we also
    # bound by the configured batch size.
    rate_per_pass = max(1, BACKFILL_RATE_PER_MIN * SWEEP_INTERVAL_S // 60)
    batch_cap = min(SWEEP_BATCH_SIZE, rate_per_pass)

    enqueued = 0
    for json_rec in iter_succeeded_in_grace(now):
        if enqueued >= batch_cap:
            break
        try:
            should_dispatch = await _process_candidate(json_rec, now)
        except Exception:
            logger.exception(
                "sweeper iteration failed job=%s", json_rec.job_id
            )
            continue
        if should_dispatch:
            # P1-A (CodeX 4): pass JSON snapshot to _run_publish so the
            # eventual UPDATE can validate that the row hasn't been
            # overwrite-bumped while the in-flight publish was running.
            # CodeX P1-2 (2026-05-18): legacy JSON rows have edit_generation=None.
            # Default to 0 so dispatch/compare matches PG's server_default='0' for
            # legacy Job rows. Without this, sweeper loops forever taking lease but
            # never publishing for any pre-edit-generation task.
            asyncio.create_task(
                _run_publish(
                    json_rec.job_id,
                    expected_generation=(json_rec.edit_generation or 0),
                    jianying_draft_zip_path=json_rec.jianying_draft_zip_path,
                ),
                name=f"r2-publish-{json_rec.job_id}",
            )
            enqueued += 1

    return enqueued


async def _process_candidate(
    json_rec: JobJsonRecord, now: datetime
) -> bool:
    """Mirror PG state, classify, and (if needed) claim the lease.

    Returns True if the caller should now spawn ``_run_publish``.
    Always commits its session — caller dispatches outside of it.
    """
    async with async_session() as db:
        result = await db.execute(
            select(Job).where(Job.job_id == json_rec.job_id)
        )
        db_job = result.scalar_one_or_none()
        if db_job is None:
            # Gateway never received the create event (orphan in JSON store).
            # Don't try to push for someone we don't track.
            return False

        # Reverse-mirror PG from JSON store. Idempotent + settles quota
        # the first time this job crosses into a terminal state.
        await mirror_job_terminal_state(db, db_job, json_rec)

        # Honor an active backoff window.
        if db_job.r2_push_retry_after and db_job.r2_push_retry_after > now:
            await db.commit()
            return False

        should, _push_keys = _classify_candidate(json_rec, db_job)
        if not should:
            await db.commit()
            return False

        # Claim a lease so a second tick during the in-flight PUT
        # doesn't re-enter the same job. ``_run_publish`` overwrites
        # this field (NULL on success, now+5min on partial failure).
        db_job.r2_push_retry_after = now + timedelta(seconds=LEASE_S)
        await db.commit()
    return True


async def _run_publish(
    job_id: str,
    *,
    expected_generation: int,
    jianying_draft_zip_path: str | None,
) -> None:
    """In-process publish runner.

    Re-fetches the Job row to read service_mode / project_dir / display_name
    fresh — the sweep loop only kept the JSON record. ``expected_generation``
    is the generation the sweeper saw at lease time; we use it to detect
    an overwrite race (P1-A, CodeX 4):

      1. We refuse to RUN if the live row has already moved past
         ``expected_generation`` (saves the wasted PUT bytes; the
         next sweep tick will pick up the bumped generation).
      2. We refuse to COMMIT if the row moved during the in-flight
         publish (the new generation deserves a fresh push, not the
         old one's results merged in).

    Both refusals are silent and log at INFO — overwrite races are a
    legitimate path, not an error.
    """
    from services.r2_publisher_lib.r2_publisher import publish_artifacts

    # CodeX P1-2 (2026-05-18): legacy JSON rows have edit_generation=None.
    # Normalise once here so every downstream use — the early-return comparison,
    # the publish_artifacts call, the conditional UPDATE WHERE clause, and the
    # log format strings — all see an int rather than None.
    expected_generation = expected_generation or 0

    async with async_session() as db:
        result = await db.execute(select(Job).where(Job.job_id == job_id))
        job = result.scalar_one_or_none()
        if job is None or not job.project_dir:
            return
        # P1-A short-circuit: if the row already moved on, skip.
        live_generation = job.edit_generation or 0
        if live_generation != expected_generation:
            logger.info(
                "r2 publish: skip job=%s (lease saw gen=%d, live gen=%d — "
                "edit overwrite raced ahead)",
                job_id, expected_generation, live_generation,
            )
            return
        if job.status != "succeeded":
            logger.info(
                "r2 publish: skip job=%s (status=%s, expected succeeded — "
                "edit overwrite reset state)",
                job_id, job.status,
            )
            return
        service_mode = job.service_mode
        # Same precedence as gateway/job_intercept.py:_derive_download_filename.
        base_filename = (
            getattr(job, "display_name", None)
            or getattr(job, "title", None)
            or job_id
        )
        project_dir = Path(job.project_dir)
        existing_registry: list[dict] | None = job.r2_artifacts

    # Decide push mode based on what the sweeper saw + the live registry
    # at run-start. Re-classify here would race against editing commits,
    # but the eventual conditional UPDATE protects us either way.
    has_jianying = jianying_draft_zip_path is not None
    if existing_registry is not None and has_jianying and not _registry_has_jianying(
        existing_registry, expected_generation
    ):
        push_keys: frozenset[str] | None = frozenset(
            {"editor.jianying_draft_zip"}
        )
    else:
        push_keys = None

    pub = await asyncio.to_thread(
        publish_artifacts,
        job_id=job_id,
        service_mode=service_mode,
        edit_generation=expected_generation,
        project_dir=project_dir,
        base_filename=base_filename,
        has_jianying_draft=has_jianying,
        jianying_draft_zip_path=jianying_draft_zip_path,
        push_keys=push_keys,
    )

    new_entries = _merge_entries(
        existing_registry, push_keys, pub.entries, expected_generation
    )

    # P1-A conditional UPDATE: only land the result if the row's
    # ``edit_generation`` and ``status`` haven't moved during the
    # publish. Full pushes additionally require ``r2_artifacts IS NULL``
    # so we don't clobber a competing publisher's just-finished write.
    # (Delta pushes deliberately allow non-NULL — they're meant to
    # append to an existing registry.)
    where_clause = (
        (Job.job_id == job_id)
        & (Job.edit_generation == expected_generation)
        & (Job.status == "succeeded")
    )
    if push_keys is None:
        where_clause = where_clause & (Job.r2_artifacts.is_(None))

    async with async_session() as db:
        result = await db.execute(
            update(Job)
            .where(where_clause)
            .values(
                r2_artifacts=new_entries,
                r2_push_retry_after=(
                    None
                    if pub.all_ok
                    else datetime.now(timezone.utc)
                    + timedelta(minutes=5)
                ),
            )
        )
        await db.commit()
        rowcount = getattr(result, "rowcount", None)
        if rowcount == 0:
            logger.info(
                "r2 publish: drop result job=%s (gen=%d) — row moved during "
                "publish; next sweep will retry the live state",
                job_id, expected_generation,
            )


def _merge_entries(
    existing: list[dict] | None,
    push_keys: frozenset[str] | None,
    new_entries,
    edit_generation: int,
) -> list[dict]:
    """Combine new publisher output with prior registry state.

    Full push (``push_keys is None``): replace wholesale. The publisher
    always returns one entry per eligible key, so the new array is
    self-consistent with no stale state to carry over.

    Delta push (``push_keys`` non-empty, currently only jianying):
    keep entries from the prior registry that aren't in the touched
    key set OR that belong to a different ``edit_generation`` (so the
    history of older generations stays intact for forensic traces),
    and append the freshly-pushed ones.
    """
    new_dicts = [e.to_dict() for e in new_entries]
    if push_keys is None:
        return new_dicts

    keep: list[dict] = []
    if existing:
        for item in existing:
            item_key = item.get("artifact_key")
            item_gen = item.get("edit_generation")
            # If it's the same generation AND the publisher just touched
            # this key, drop the old entry (the new one replaces it).
            if item_gen == edit_generation and item_key in push_keys:
                continue
            keep.append(item)
    keep.extend(new_dicts)
    return keep


async def sweeper_loop() -> None:
    """Run sweep_once on a fixed cadence forever. Crashes are logged
    and the loop continues — operator alerts come from Uptime Kuma /
    log-based monitoring (Stage A non-goal: alerting, see plan §1.3).
    """
    logger.info(
        "r2_artifact_sweeper started "
        "(interval=%ss, batch=%d, rate=%d/min)",
        SWEEP_INTERVAL_S, SWEEP_BATCH_SIZE, BACKFILL_RATE_PER_MIN,
    )
    while True:
        try:
            n = await sweep_once()
            if n > 0:
                logger.info(
                    "r2_artifact_sweeper enqueued %d publish tasks", n
                )
        except Exception:
            logger.exception("r2_artifact_sweeper loop crashed; retrying")
        await asyncio.sleep(SWEEP_INTERVAL_S)
