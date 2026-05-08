"""Admin presence tracking for in-product human chat routing.

Plan 2026-05-08 (L1 follow-up §"管理员/运营/客服只要登录，就显示在线"):

- ``record_heartbeat(db, user_id)`` — UPSERT a row in
  ``support_admin_presence`` with current timestamp; preserves status
  if the row already existed.
- ``set_status(db, user_id, status)`` — explicitly set
  online / paused / offline.
- ``count_online(db, threshold_seconds)`` — number of admins whose
  status is ``online`` AND ``last_heartbeat_at > now - threshold``.
  ``paused`` admins are NOT counted (the whole point of the toggle).
- ``is_anyone_online(db, threshold_seconds)`` — bool wrapper.

Status semantics:
- ``online``  — admin is at the keyboard. New tickets route to them.
- ``paused``  — admin is logged in but doesn't want to be interrupted.
                Heartbeat keeps flowing (so we know they're alive),
                but they're excluded from online_count. New tickets
                route to WeChat QR fallback.
- ``offline`` — explicit "stop counting me". Frontend stops sending
                heartbeats. Equivalent to closed-tab state.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import SupportAdminPresence

logger = logging.getLogger(__name__)


PresenceStatus = Literal["online", "paused", "offline"]
_VALID_STATUSES: set[str] = {"online", "paused", "offline"}


async def record_heartbeat(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> SupportAdminPresence:
    """Refresh ``last_heartbeat_at`` for an admin (status untouched).

    Idempotent: callable many times per request without side-effects
    beyond timestamp updates. Returns the (possibly newly-created) row.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(SupportAdminPresence)
        .values(
            user_id=user_id,
            status="online",
            last_heartbeat_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[SupportAdminPresence.user_id],
            # Preserve existing status (don't flip paused → online on heartbeat).
            set_={
                "last_heartbeat_at": now,
                "updated_at": now,
            },
        )
        .returning(SupportAdminPresence)
    )
    result = await db.execute(stmt)
    return result.scalar_one()


async def set_status(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    status: PresenceStatus,
) -> SupportAdminPresence:
    """Explicitly set the admin's presence status.

    Also updates ``last_heartbeat_at`` so a transition from ``offline``
    back to ``online`` doesn't suddenly look stale.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"invalid presence status: {status!r}")
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(SupportAdminPresence)
        .values(
            user_id=user_id,
            status=status,
            last_heartbeat_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[SupportAdminPresence.user_id],
            set_={
                "status": status,
                "last_heartbeat_at": now,
                "updated_at": now,
            },
        )
        .returning(SupportAdminPresence)
    )
    result = await db.execute(stmt)
    return result.scalar_one()


async def get_my_presence(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> SupportAdminPresence | None:
    stmt = select(SupportAdminPresence).where(
        SupportAdminPresence.user_id == user_id
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def count_online(
    db: AsyncSession,
    *,
    threshold_seconds: int,
) -> int:
    """How many admins are actively online (status='online' + fresh heartbeat).

    ``paused`` admins are NOT counted.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(1, int(threshold_seconds)))
    stmt = (
        select(func.count())
        .select_from(SupportAdminPresence)
        .where(
            SupportAdminPresence.status == "online",
            SupportAdminPresence.last_heartbeat_at > cutoff,
        )
    )
    result = await db.execute(stmt)
    return int(result.scalar() or 0)


async def is_anyone_online(
    db: AsyncSession,
    *,
    threshold_seconds: int,
) -> bool:
    return (await count_online(db, threshold_seconds=threshold_seconds)) > 0


async def list_recent(
    db: AsyncSession,
    *,
    limit: int = 20,
) -> list[SupportAdminPresence]:
    """Recent admin presence rows for the admin dashboard. Ordered by
    last_heartbeat desc."""
    stmt = (
        select(SupportAdminPresence)
        .order_by(SupportAdminPresence.last_heartbeat_at.desc())
        .limit(max(1, min(int(limit or 20), 100)))
    )
    result = await db.execute(stmt)
    return list(result.scalars())
