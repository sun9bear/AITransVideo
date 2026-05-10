"""Notification dispatch service.

Plan §16 — the only place that writes ``user_notifications`` rows.

Responsibilities:

- Resolve a recipe from ``notification_dispatch_map`` by event_type.
- Format ``title`` / ``body`` / ``action_url`` from a sanitized payload
  (allowlist; nothing internal-only).
- Insert a row honoring ``dedupe_key`` if provided.

Callers may use ``dispatch_event_sync`` for code paths that don't have
an async session in scope (those wrap the call in a one-off session).

Admin alerts (budget exceeded, provider failures) deliberately do NOT
flow through here — they go to email / webhook directly. Plan §16.5.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import UserNotification
from notification_dispatch_map import get_recipe

logger = logging.getLogger(__name__)


# Allowlist of payload keys we are willing to interpolate into title/body.
# Anything else is dropped to keep prompt-injection / accidental-leak
# surface area tiny. New events that need additional placeholders should
# add their keys here AND update the corresponding recipe.
_PAYLOAD_ALLOWLIST = {
    "display_name",
    "job_id",
    "plan_name",
    "trial_days",
    "trial_minutes",
    "amount",
    "topic",
    "summary",
}


def _sanitized_payload(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not raw:
        return {}
    out: dict[str, Any] = {}
    for key in _PAYLOAD_ALLOWLIST:
        if key in raw:
            value = raw[key]
            if value is None:
                out[key] = ""
            elif isinstance(value, (str, int, float, bool)):
                out[key] = value
            else:
                out[key] = str(value)
    return out


async def dispatch_event(
    db: AsyncSession,
    *,
    event_type: str,
    user_id: uuid.UUID | None,
    job_id: str | None = None,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    related_id: str | None = None,
    expires_at: datetime | None = None,
) -> UserNotification | None:
    """Insert one user_notifications row from an event.

    Returns the row or None if the event type is unknown or insertion
    was skipped due to a dedupe collision.
    """
    recipe = get_recipe(event_type)
    if not recipe:
        logger.debug("unknown notification event_type: %s", event_type)
        return None
    safe_payload = _sanitized_payload(payload)

    title_tmpl = recipe.get("title", "")
    body_tmpl = recipe.get("body", "")
    action_tmpl = recipe.get("action_url", "")
    try:
        title = title_tmpl.format(**safe_payload) if title_tmpl else event_type
    except (KeyError, IndexError):
        title = title_tmpl
    try:
        body = body_tmpl.format(**safe_payload) if body_tmpl else ""
    except (KeyError, IndexError):
        body = body_tmpl
    action_url = None
    if action_tmpl:
        try:
            action_payload = dict(safe_payload)
            action_payload.setdefault("job_id", job_id or "")
            action_url = action_tmpl.format(**action_payload)
        except (KeyError, IndexError):
            action_url = action_tmpl

    notif = UserNotification(
        scope=recipe["scope"],
        topic=recipe["topic"],
        user_id=user_id,
        job_id=job_id,
        title=title[:255],
        body=body,
        severity=recipe.get("severity", "info"),
        related_type=recipe.get("related_type"),
        related_id=related_id,
        artifact_key=recipe.get("artifact_key"),
        action_url=action_url,
        dedupe_key=dedupe_key,
        expires_at=expires_at,
        popup=bool(recipe.get("popup", False)),
    )
    db.add(notif)
    try:
        await db.flush()
    except IntegrityError:
        # dedupe collision — the unique partial index in migration 020
        # protected us from a duplicate row. Treat as a no-op.
        await db.rollback()
        return None
    return notif


async def list_for_user(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    include_archived: bool = False,
    limit: int = 50,
) -> list[UserNotification]:
    stmt = select(UserNotification).where(UserNotification.user_id == user_id)
    if not include_archived:
        stmt = stmt.where(UserNotification.archived_at.is_(None))
    stmt = stmt.order_by(UserNotification.created_at.desc()).limit(
        max(1, min(int(limit or 50), 200))
    )
    result = await db.execute(stmt)
    return list(result.scalars())


async def unread_count(db: AsyncSession, *, user_id: uuid.UUID) -> int:
    stmt = (
        select(UserNotification)
        .where(
            UserNotification.user_id == user_id,
            UserNotification.read_at.is_(None),
            UserNotification.archived_at.is_(None),
        )
    )
    result = await db.execute(stmt)
    return len(list(result.scalars()))


async def mark_read(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    ids: list[str],
    mark_all: bool,
) -> int:
    """Mark given notifications (or all unread) as read.

    Returns the number of rows updated.
    """
    if mark_all:
        stmt = select(UserNotification).where(
            UserNotification.user_id == user_id,
            UserNotification.read_at.is_(None),
        )
        result = await db.execute(stmt)
        rows = list(result.scalars())
    else:
        if not ids:
            return 0
        try:
            uids = [uuid.UUID(x) for x in ids]
        except ValueError:
            return 0
        stmt = select(UserNotification).where(
            UserNotification.user_id == user_id,
            UserNotification.id.in_(uids),
        )
        result = await db.execute(stmt)
        rows = list(result.scalars())
    now = datetime.now(timezone.utc)
    for row in rows:
        if row.read_at is None:
            row.read_at = now
    await db.flush()
    return len(rows)


async def archive(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    ids: list[str],
) -> int:
    if not ids:
        return 0
    try:
        uids = [uuid.UUID(x) for x in ids]
    except ValueError:
        return 0
    stmt = select(UserNotification).where(
        UserNotification.user_id == user_id,
        UserNotification.id.in_(uids),
    )
    result = await db.execute(stmt)
    rows = list(result.scalars())
    now = datetime.now(timezone.utc)
    for row in rows:
        if row.archived_at is None:
            row.archived_at = now
            if row.read_at is None:
                row.read_at = now
    await db.flush()
    return len(rows)
