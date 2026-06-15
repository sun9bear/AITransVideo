"""Phase 2a free tier — daily free-service quota: per-job ledger + atomic reserve.

Mirrors ``gateway/express_reservation_service.py``. The free tier allows N
(default 1) free-service jobs per user per **Asia/Shanghai natural day**. This is
a SEPARATE ledger from ``users.free_jobs_quota_*`` (the free *plan* total) — a
free-service job MUST NOT consume that legacy quota (CodeX plan review).

Atomicity: ``reserve_free_daily`` locks the ``users`` row (``SELECT ... FOR
UPDATE``) to serialize a user's concurrent free creates (PG); inline-expires
stale reserved rows; counts active(reserved|consumed) rows for
``(user, usage_date)``; inserts a reserved row when under cap. A partial-unique
index on ``(user_id, create_idempotency_key)`` (``WHERE status='reserved'``) is
the idempotency fail-safe 2nd defense.

Idempotency invariants (CodeX review):
- **Scoped by user_id** (P1): every lookup/transition filters ``user_id`` so two
  users sharing a client-supplied key never collide / mutate each other's rows.
- **active = reserved|consumed** (P2): a retry after the row is consumed is an
  idempotent hit (returns the existing row), NOT a cap-exceeded 403.

PG-only FOR UPDATE blocking is left to a real-PG test; state machine /
idempotency / counting / inline-expire run on in-memory sqlite.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import FreeServiceDailyUsage, User

RESERVED = "reserved"
CONSUMED = "consumed"
RELEASED = "released"
EXPIRED = "expired"

FREE_DAILY_CAP = 1
RESERVE_TTL_MINUTES = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def shanghai_day_key(now: datetime | None = None) -> str:
    """Asia/Shanghai natural-day key ``YYYY-MM-DD`` (free quota resets per SH day).

    Mirrors ``user_voice_service`` tz handling: ``ZoneInfo("Asia/Shanghai")`` when
    available, fixed UTC+8 fallback. Naive datetimes are treated as UTC. Pure —
    testable without a DB.
    """
    dt = now or _now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        dt = dt.astimezone(ZoneInfo("Asia/Shanghai"))
    except Exception:
        dt = dt.astimezone(timezone(timedelta(hours=8)))
    return dt.strftime("%Y-%m-%d")


def shanghai_day_start_utc(now: datetime | None = None) -> datetime:
    """Asia/Shanghai 当日 00:00 对应的 **UTC** 时刻（用于 ``created_at >= 日界``
    范围查询，配合 ``shanghai_day_key`` 的 per-SH-day 语义）.

    与 ``shanghai_day_key`` 同 tz 处理（ZoneInfo 优先、UTC+8 兜底；naive 当 UTC）。
    取本地午夜后转回 UTC（aware datetime）。Pure —— 无 DB，可测。
    """
    dt = now or _now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Shanghai")
    except Exception:
        tz = timezone(timedelta(hours=8))
    local = dt.astimezone(tz)
    local_midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


@dataclass(frozen=True)
class FreeDailyOutcome:
    """``reserve_free_daily`` result. ``status`` drives the caller:

    - ``"reserved"``        → admitted (new or idempotent hit)
    - ``"denied"``          → daily cap reached (``deny_reason``)
    - ``"user_not_found"``  → ``users`` row missing → fail-closed
    """

    status: str
    row_id: str | None = None
    deny_reason: str | None = None
    idempotent_hit: bool = False


@dataclass(frozen=True)
class FreeTransitionOutcome:
    ok: bool
    status: str
    conflict_reason: str | None = None


async def _expire_stale_for_user(db: AsyncSession, user_id, usage_date, *, now) -> int:
    """Inline-expire the user's stale reserved rows for ``usage_date`` (don't rely
    on a sweeper). Does NOT commit — the caller's transaction commits."""
    result = await db.execute(
        update(FreeServiceDailyUsage)
        .where(
            FreeServiceDailyUsage.user_id == user_id,
            FreeServiceDailyUsage.usage_date == usage_date,
            FreeServiceDailyUsage.status == RESERVED,
            FreeServiceDailyUsage.expires_at < now,
        )
        .values(status=EXPIRED, released_reason="ttl_expired", updated_at=now)
    )
    return int(result.rowcount or 0)


async def _count_active_for_day(db: AsyncSession, user_id, usage_date) -> int:
    """Active (reserved|consumed) row count for ``(user, usage_date)`` — daily cap."""
    result = await db.execute(
        select(func.count())
        .select_from(FreeServiceDailyUsage)
        .where(
            FreeServiceDailyUsage.user_id == user_id,
            FreeServiceDailyUsage.usage_date == usage_date,
            FreeServiceDailyUsage.status.in_((RESERVED, CONSUMED)),
        )
    )
    return int(result.scalar() or 0)


async def _find_active_by_key(db: AsyncSession, user_id, idempotency_key: str):
    """The user's active (reserved|consumed) row for ``idempotency_key``.

    Scoped by ``user_id`` (CodeX P1 — never collide across users) and matches
    reserved|consumed (CodeX P2 — a consumed row is an idempotent hit, not
    over-cap). At most one active row exists per ``(user, key)`` since a row
    transitions reserved → consumed in place.
    """
    return (
        await db.execute(
            select(FreeServiceDailyUsage).where(
                FreeServiceDailyUsage.user_id == user_id,
                FreeServiceDailyUsage.create_idempotency_key == idempotency_key,
                FreeServiceDailyUsage.status.in_((RESERVED, CONSUMED)),
            )
        )
    ).scalar_one_or_none()


async def reserve_free_daily(
    db: AsyncSession,
    *,
    user_id: object,
    usage_date: str,
    idempotency_key: str,
    daily_cap: int = FREE_DAILY_CAP,
    ttl_minutes: int = RESERVE_TTL_MINUTES,
) -> FreeDailyOutcome:
    """Atomically reserve one free-service slot for ``(user, usage_date)``.

    Single transaction: lock users row → inline-expire stale → idempotency →
    cap check → INSERT. PG serializes concurrent same-user reserves via the
    users-row FOR UPDATE; the partial-unique on ``(user_id, create_idempotency_key)``
    is the fail-safe 2nd defense (IntegrityError → re-read existing).
    """
    now = _now()
    user_pk = (
        await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    if user_pk is None:
        return FreeDailyOutcome(status="user_not_found")

    await _expire_stale_for_user(db, user_id, usage_date, now=now)

    existing = await _find_active_by_key(db, user_id, idempotency_key)
    if existing is not None:
        await db.commit()  # persist inline-expire
        return FreeDailyOutcome(status="reserved", row_id=str(existing.id), idempotent_hit=True)

    active = await _count_active_for_day(db, user_id, usage_date)
    if active >= daily_cap:
        await db.commit()  # persist inline-expire even when denying
        return FreeDailyOutcome(status="denied", deny_reason="daily_cap_exceeded")

    row = FreeServiceDailyUsage(
        id=uuid.uuid4(),
        user_id=user_id,
        usage_date=usage_date,
        create_idempotency_key=idempotency_key,
        status=RESERVED,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(minutes=int(ttl_minutes)),
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing2 = await _find_active_by_key(db, user_id, idempotency_key)
        if existing2 is not None:
            return FreeDailyOutcome(
                status="reserved", row_id=str(existing2.id), idempotent_hit=True
            )
        raise
    return FreeDailyOutcome(status="reserved", row_id=str(row.id))


async def consume_free_daily(
    db: AsyncSession, *, user_id: object, idempotency_key: str, job_id: str
) -> FreeTransitionOutcome:
    """reserved → consumed (upstream accepted the job). Idempotent on re-consume.
    Scoped by ``user_id`` (CodeX P1)."""
    row = await _find_active_by_key(db, user_id, idempotency_key)
    if row is None:
        return FreeTransitionOutcome(False, "missing", "reservation_not_found")
    if row.status == CONSUMED:
        return FreeTransitionOutcome(True, CONSUMED)  # idempotent
    row.status = CONSUMED
    row.job_id = job_id
    row.updated_at = _now()
    await db.commit()
    return FreeTransitionOutcome(True, CONSUMED)


async def release_free_daily(
    db: AsyncSession, *, user_id: object, idempotency_key: str, reason: str
) -> FreeTransitionOutcome:
    """reserved → released (upstream failed / rollback). Idempotent (no active row
    → ``"absent"``); refuses to release an already-consumed row. Scoped by
    ``user_id`` (CodeX P1)."""
    row = await _find_active_by_key(db, user_id, idempotency_key)
    if row is None:
        return FreeTransitionOutcome(True, "absent")  # nothing active — idempotent
    if row.status == CONSUMED:
        return FreeTransitionOutcome(False, CONSUMED, "already_consumed")
    row.status = RELEASED
    row.released_reason = (reason or "")[:64]
    row.updated_at = _now()
    await db.commit()
    return FreeTransitionOutcome(True, RELEASED)


__all__ = [
    "shanghai_day_key",
    "shanghai_day_start_utc",
    "reserve_free_daily",
    "consume_free_daily",
    "release_free_daily",
    "FreeDailyOutcome",
    "FreeTransitionOutcome",
    "FREE_DAILY_CAP",
    "RESERVE_TTL_MINUTES",
    "RESERVED",
    "CONSUMED",
    "RELEASED",
    "EXPIRED",
]
