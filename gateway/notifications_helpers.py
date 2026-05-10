"""Convenience helpers for gateway-side notification dispatch.

The notification system itself lives in ``notifications_service``; this
module is the safe glue layer used by hot paths (e.g.
``intercept_get_job``) where we want to dispatch a notification on a job
status transition without ever raising.

Contract:

- Helpers NEVER raise. Any exception is caught and logged.
- Helpers do not commit; the calling handler owns commit semantics.
- Helpers are idempotent at best-effort via ``dedupe_key``.

P1 limitation (Codex P2-2, 2026-05-08): the only wired-up trigger for
``maybe_dispatch_job_transition`` is ``intercept_get_job`` — i.e. the
gateway only emits a "task succeeded / failed" notification when the
user (or the frontend's polling hook) actually requests
``GET /job-api/jobs/{id}``. A user who never opens the page after
submitting a job will not see a notification.

This is acknowledged as a P1 MVP shortcut. The proper fix is to have
the pipeline / Job API call ``POST /internal/notifications/dispatch``
on terminal status transitions; the endpoint already exists. Migration
to that path is tracked as a follow-up — see plan §16.7 P2+ "true
event-driven dispatch".

Important corollaries:

- Notification creation is a *side effect* of a GET request. Callers
  hold a transactional commit (see ``intercept_get_job``); this module
  must stay safe-to-call within that transaction.
- Until the pipeline-side dispatch lands, treat the notification stream
  as "best-effort surface" rather than authoritative event log.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from notification_dispatch_map import (
    EVENT_ARTIFACT_JIANYING_DRAFT_READY,
    EVENT_ARTIFACT_MATERIALS_PACK_READY,
    EVENT_JOB_FAILED,
    EVENT_JOB_PUBLISHED,
    EVENT_JOB_SUCCEEDED,
)
from notifications_service import dispatch_event

logger = logging.getLogger(__name__)


_TERMINAL_TO_EVENT: dict[str, str] = {
    "succeeded": EVENT_JOB_SUCCEEDED,
    "failed": EVENT_JOB_FAILED,
}


async def maybe_dispatch_job_transition(
    db: AsyncSession,
    *,
    db_job: Any,
    upstream_status: str | None,
) -> None:
    """Dispatch a notification iff the gateway DB status differs from upstream
    AND upstream landed in a terminal state we care about.

    ``db_job`` is the gateway ``Job`` ORM row; ``upstream_status`` is the
    string from the Job API JSON payload. The function:

    1. Returns early if either side is missing.
    2. Returns early if status hasn't transitioned.
    3. Dispatches with a stable ``dedupe_key`` so a second poll for the
       same transition does not duplicate the notification.
    4. Leaves ``db_job.status`` untouched. Status mirroring and quota/credit
       settlement must go through ``mirror_job_terminal_state``; this helper is
       notification-only so it cannot create a terminal-status side path.

    NEVER raises. Any failure is logged at WARNING.
    """
    try:
        if db_job is None or db_job.user_id is None:
            return
        if upstream_status is None:
            return
        prev = (db_job.status or "").strip()
        new = (upstream_status or "").strip()
        if prev == new:
            return
        # We only react to terminal transitions for now. Adding more events
        # is a matter of growing _TERMINAL_TO_EVENT and the dispatch map.
        event_type = _TERMINAL_TO_EVENT.get(new)
        if event_type is None:
            return

        display_name = _resolve_display_name(db_job)
        # Dedupe key: per-job + event_type. A user re-triggering the job
        # via copy_as_new lands in a NEW Job row, so this is safe.
        dedupe_key = f"{event_type}:{getattr(db_job, 'job_id', db_job.id)}"
        await dispatch_event(
            db,
            event_type=event_type,
            user_id=db_job.user_id,
            job_id=getattr(db_job, "job_id", None),
            payload={
                "display_name": display_name,
                "job_id": getattr(db_job, "job_id", ""),
            },
            dedupe_key=dedupe_key,
        )
    except Exception as exc:
        # Never break the calling handler.
        logger.warning("notification dispatch helper failed: %s", exc)


async def dispatch_artifact_ready(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    job_id: str,
    artifact_kind: str,
    display_name: str,
) -> None:
    """Emit an artifact-ready notification.

    ``artifact_kind`` is one of ``"jianying_draft"`` / ``"materials_pack"``
    / ``"published_video"``. Anything else logs a debug and noops.
    """
    try:
        if artifact_kind == "jianying_draft":
            event = EVENT_ARTIFACT_JIANYING_DRAFT_READY
        elif artifact_kind == "materials_pack":
            event = EVENT_ARTIFACT_MATERIALS_PACK_READY
        elif artifact_kind == "published_video":
            event = EVENT_JOB_PUBLISHED
        else:
            logger.debug("unknown artifact_kind %s; skipping", artifact_kind)
            return
        dedupe_key = f"{event}:{job_id}"
        await dispatch_event(
            db,
            event_type=event,
            user_id=user_id,
            job_id=job_id,
            payload={"display_name": display_name, "job_id": job_id},
            dedupe_key=dedupe_key,
        )
    except Exception as exc:
        logger.warning("dispatch_artifact_ready failed: %s", exc)


def _resolve_display_name(db_job: Any) -> str:
    name = getattr(db_job, "display_name", None) or getattr(db_job, "title", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    job_id = getattr(db_job, "job_id", None) or ""
    return f"任务 {job_id[:8]}" if job_id else "任务"
