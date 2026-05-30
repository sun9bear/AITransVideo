"""Phase 2a Task 4 — free_service_quota: day-key + reserve/consume/release.

``shanghai_day_key`` is a pure fn (no DB). The reserve/consume/release state
machine, idempotency, daily-cap counting, and inline-expire run on in-memory
aiosqlite (mirrors test_phase43a_pr2b_reservation_service). True PG ``FOR UPDATE``
concurrency is left to a real-PG test; sqlite covers the logical paths.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Column, MetaData, Table
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


from models import FreeServiceDailyUsage  # noqa: E402
import free_service_quota as q  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_USER = uuid.UUID("00000000-0000-0000-0000-0000000000c4")
_UNKNOWN = uuid.UUID("00000000-0000-0000-0000-0000000000fe")
_USER2 = uuid.UUID("00000000-0000-0000-0000-0000000000c5")

# minimal users stub — reserve does SELECT users.id FOR UPDATE. id column uses
# PG_UUID(as_uuid=True) (same as models.User.id) so the bind processor matches.
_users_md = MetaData()
_users_stub = Table(
    "users", _users_md, Column("id", PG_UUID(as_uuid=True), primary_key=True)
)


async def _make_sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda s: _users_stub.create(s))
        await conn.run_sync(lambda s: FreeServiceDailyUsage.__table__.create(s))
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sm() as db:
        await db.execute(_users_stub.insert().values(id=_USER))
        await db.commit()
    return sm


# ---------------------------------------------------------------------------
# day-key (pure)
# ---------------------------------------------------------------------------

def test_shanghai_day_key_rolls_over_at_shanghai_midnight():
    # 2026-05-29 23:30 UTC == 2026-05-30 07:30 Asia/Shanghai (+8) -> next SH day
    assert q.shanghai_day_key(datetime(2026, 5, 29, 23, 30, tzinfo=timezone.utc)) == "2026-05-30"
    # 2026-05-29 10:00 UTC == 2026-05-29 18:00 SH -> same SH day
    assert q.shanghai_day_key(datetime(2026, 5, 29, 10, 0, tzinfo=timezone.utc)) == "2026-05-29"


def test_shanghai_day_key_naive_treated_as_utc():
    assert q.shanghai_day_key(datetime(2026, 5, 29, 23, 30)) == "2026-05-30"


# ---------------------------------------------------------------------------
# reserve: cap / idempotency / unknown-user / next-day reset
# ---------------------------------------------------------------------------

def test_reserve_admits_then_cap_blocks_second_same_day():
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            o1 = await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29", idempotency_key="k1", daily_cap=1
            )
            assert o1.status == "reserved" and not o1.idempotent_hit
        async with sm() as db:
            o2 = await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29", idempotency_key="k2", daily_cap=1
            )
            assert o2.status == "denied" and o2.deny_reason == "daily_cap_exceeded"

    _run(go())


def test_reserve_is_idempotent_on_same_key():
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            o1 = await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29", idempotency_key="kX", daily_cap=1
            )
        async with sm() as db:
            o2 = await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29", idempotency_key="kX", daily_cap=1
            )
        assert o2.status == "reserved" and o2.idempotent_hit and o2.row_id == o1.row_id

    _run(go())


def test_reserve_unknown_user_fails_closed():
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            o = await q.reserve_free_daily(
                db, user_id=_UNKNOWN, usage_date="2026-05-29", idempotency_key="k", daily_cap=1
            )
            assert o.status == "user_not_found"

    _run(go())


def test_reserve_next_shanghai_day_resets_cap():
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29", idempotency_key="d1", daily_cap=1
            )
        async with sm() as db:
            o = await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-30", idempotency_key="d2", daily_cap=1
            )
            assert o.status == "reserved"  # different SH day -> fresh cap

    _run(go())


# ---------------------------------------------------------------------------
# consume / release / inline-expire
# ---------------------------------------------------------------------------

def test_consumed_still_counts_toward_cap():
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29", idempotency_key="c1", daily_cap=1
            )
        async with sm() as db:
            con = await q.consume_free_daily(db, user_id=_USER, idempotency_key="c1", job_id="job_c1")
            assert con.ok and con.status == "consumed"
        async with sm() as db:
            o = await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29", idempotency_key="c2", daily_cap=1
            )
            assert o.status == "denied"  # consumed occupies the daily slot

    _run(go())


def test_release_frees_the_daily_slot():
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29", idempotency_key="r1", daily_cap=1
            )
        async with sm() as db:
            rel = await q.release_free_daily(db, user_id=_USER, idempotency_key="r1", reason="upstream_failed")
            assert rel.ok and rel.status == "released"
        async with sm() as db:
            o = await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29", idempotency_key="r2", daily_cap=1
            )
            assert o.status == "reserved"  # released slot is free again

    _run(go())


def test_release_refuses_to_release_consumed():
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29", idempotency_key="x1", daily_cap=1
            )
        async with sm() as db:
            await q.consume_free_daily(db, user_id=_USER, idempotency_key="x1", job_id="job_x1")
        async with sm() as db:
            rel = await q.release_free_daily(db, user_id=_USER, idempotency_key="x1", reason="late")
            assert not rel.ok and rel.conflict_reason == "already_consumed"

    _run(go())


def test_reserve_inline_expires_stale_reserved():
    async def go():
        sm = await _make_sessionmaker()
        # ttl_minutes=-1 -> expires_at strictly in the PAST, so it is
        # deterministically stale on the next reserve. (ttl_minutes=0 made
        # expires_at == now, which was clock-tick flaky under fast runs: when the
        # next reserve landed in the same tick, expires_at < now was False.)
        async with sm() as db:
            await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29",
                idempotency_key="s1", daily_cap=1, ttl_minutes=-1,
            )
        async with sm() as db:
            o = await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29", idempotency_key="s2", daily_cap=1
            )
            assert o.status == "reserved"  # stale reserved inline-expired -> slot free

    _run(go())


# ---------------------------------------------------------------------------
# idempotency invariants (CodeX P1 user-scoping / P2 consumed-retry)
# ---------------------------------------------------------------------------

def test_reserve_idempotency_isolated_per_user():
    """Two users sharing the SAME idempotency key must not collide (CodeX P1):
    user B's reserve must not hit user A's row, and must get B's own slot."""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            await db.execute(_users_stub.insert().values(id=_USER2))
            await db.commit()
        async with sm() as db:
            a = await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29", idempotency_key="shared", daily_cap=1
            )
            assert a.status == "reserved" and not a.idempotent_hit
        async with sm() as db:
            b = await q.reserve_free_daily(
                db, user_id=_USER2, usage_date="2026-05-29", idempotency_key="shared", daily_cap=1
            )
            # B gets its OWN slot — not an idempotent hit on A's row.
            assert b.status == "reserved" and not b.idempotent_hit
            assert b.row_id != a.row_id

    _run(go())


def test_reserve_idempotent_after_consume_not_over_cap():
    """Same user + same key, AFTER the row is consumed, returns an idempotent hit
    (NOT daily_cap_exceeded) — supports network-timeout retries (CodeX P2)."""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            r = await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29", idempotency_key="kc", daily_cap=1
            )
        async with sm() as db:
            await q.consume_free_daily(db, user_id=_USER, idempotency_key="kc", job_id="job_kc")
        async with sm() as db:
            retry = await q.reserve_free_daily(
                db, user_id=_USER, usage_date="2026-05-29", idempotency_key="kc", daily_cap=1
            )
            assert retry.status == "reserved" and retry.idempotent_hit  # NOT denied
            assert retry.row_id == r.row_id  # same row, no new insert

    _run(go())


# ---------------------------------------------------------------------------
# migration 034 chain + model columns (AST/ORM guards, mirror pr2a)
# ---------------------------------------------------------------------------

def test_migration_034_revision_chain():
    import ast

    mig = (
        Path(__file__).resolve().parents[1]
        / "gateway" / "alembic" / "versions" / "034_free_service_daily_usage.py"
    )
    assert mig.exists(), f"migration 034 missing: {mig}"
    rev: dict[str, object] = {}
    for node in ast.walk(ast.parse(mig.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in ("revision", "down_revision")
            and isinstance(node.value, ast.Constant)
        ):
            rev[node.target.id] = node.value.value
    assert rev.get("revision") == "034_free_service_daily_usage"
    assert rev.get("down_revision") == "033_user_voice_cleanup_tracking"


def test_free_service_daily_usage_model_columns():
    cols = set(FreeServiceDailyUsage.__table__.columns.keys())
    assert {
        "id", "user_id", "usage_date", "create_idempotency_key", "job_id",
        "status", "created_at", "updated_at", "expires_at", "released_reason",
    } <= cols
    assert FreeServiceDailyUsage.__tablename__ == "free_service_daily_usage"
