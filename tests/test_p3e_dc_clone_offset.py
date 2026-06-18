"""P3e D-C / D-A — smart 克隆 600 抵扣进分钟点（max(600, 分钟×100)），钱-关键.

plan 2026-06-15-smart-clone-600-minute-offset-plan.md §4。

- C_own：单任务正式 full smart（自有克隆 600）→ 分钟 capture=max(0,分钟×100−600)，
  合计 = max(600,分钟×100)。读权威 CloneBillingEvent（不依赖克隆 settle 顺序）。
- C_carryover：预览→完整 convert 的 600 single-use 结转（防双扣 + 防越权 + 幂等）。
- 默认 inert：非 smart / 无克隆 / 无 marker → 原样不抵。

STYLE B（真 in-memory aiosqlite，真账本）—— 不 stub sys.modules['database']
（smart_clone_reservation_service / models / credits_service import 无 module-level
engine；stub 会污染其它 gateway 测试，见 memory feedback_test_database_stub_convention）。
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import Column, MetaData, Table, select
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

_REPO = Path(__file__).resolve().parents[1]
_gateway = str(_REPO / "gateway")
if _gateway not in sys.path:
    sys.path.insert(0, _gateway)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


from models import (  # noqa: E402
    CloneBillingEvent,
    CreditsBucket,
    CreditsLedger,
    SmartCloneReservation,
)
import credits_service as cs  # noqa: E402

_CS = _REPO / "gateway" / "credits_service.py"

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

_users_md = MetaData()
_users_stub = Table("users", _users_md, Column("id", PG_UUID(as_uuid=True), primary_key=True))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _make_sessionmaker(*, bucket_remaining: int = 0) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda s: _users_stub.create(s))
        await conn.run_sync(lambda s: SmartCloneReservation.__table__.create(s))
        await conn.run_sync(lambda s: CloneBillingEvent.__table__.create(s))
        await conn.run_sync(lambda s: CreditsBucket.__table__.create(s))
        await conn.run_sync(lambda s: CreditsLedger.__table__.create(s))
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sm() as db:
        await db.execute(_users_stub.insert().values(id=_USER))
        await db.execute(_users_stub.insert().values(id=_OTHER))
        if bucket_remaining > 0:
            db.add(CreditsBucket(
                id=uuid.uuid4(), user_id=_USER, bucket_type="free",
                granted=bucket_remaining, remaining=bucket_remaining, reserved=0,
            ))
        await db.commit()
    return sm


async def _seed_clone(
    db, *, task_id: str, user_id=_USER, amount: int = 600,
    status: str = "reserved", chargeable: bool = True, carryover=None,
):
    """Seed a SmartCloneReservation (+ chargeable CloneBillingEvent) for a task.

    Returns the reservation id (uuid).
    """
    now = datetime.now(timezone.utc)
    rid = uuid.uuid4()
    db.add(SmartCloneReservation(
        id=rid, user_id=user_id, task_id=task_id, purpose="smart_clone_minimax_600",
        amount_credits=amount, status=status, created_at=now, updated_at=now,
        expires_at=now + timedelta(minutes=60), carryover_applied_to_task_id=carryover,
    ))
    if chargeable is not None:
        db.add(CloneBillingEvent(
            id=uuid.uuid4(), task_id=task_id, reservation_id=rid,
            provider="minimax", voice_id="mm_v1", chargeable=chargeable,
        ))
    await db.commit()
    return rid


def _job(smart_state=None):
    return SimpleNamespace(smart_state=smart_state)


async def _reservation_row(db, rid):
    return (
        await db.execute(select(SmartCloneReservation).where(SmartCloneReservation.id == rid))
    ).scalar_one_or_none()


# ===========================================================================
# C_own — 单任务正式 full smart：克隆 600 抵扣进分钟
# ===========================================================================


def test_c_own_long_task_deducts_600():
    """🔥🔥 长任务（10min=1000）+ 克隆 600 → 分钟 capture=400（合计 1000=max(600,1000)）。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            await _seed_clone(db, task_id="job_full1")
            adjusted, audit = await cs._smart_clone_minute_offset(
                db, job=_job(), job_id="job_full1", user_id=_USER,
                service_mode="smart", actual_credits=1000,
            )
            assert adjusted == 400
            assert audit == {}  # C_own 不产生 carryover audit
    _run(go())


def test_c_own_short_task_floors_at_zero():
    """🔥🔥 短任务（3min=300）+ 克隆 600 → 分钟 capture=0（合计 600=max(600,300)）。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            await _seed_clone(db, task_id="job_short")
            adjusted, _ = await cs._smart_clone_minute_offset(
                db, job=_job(), job_id="job_short", user_id=_USER,
                service_mode="smart", actual_credits=300,
            )
            assert adjusted == 0
    _run(go())


def test_c_own_boundary_six_min():
    """🔥 6min=600 边界 → 分钟 capture=0（两段相等、连续）。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            await _seed_clone(db, task_id="job_6")
            adjusted, _ = await cs._smart_clone_minute_offset(
                db, job=_job(), job_id="job_6", user_id=_USER,
                service_mode="smart", actual_credits=600,
            )
            assert adjusted == 0
    _run(go())


def test_c_own_no_chargeable_event_full_minutes():
    """🔥🔥 克隆失败/回预设（无 chargeable event）→ C_own=0 → 分钟全额（用户不为白克隆买单）。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            # reservation 存在但 billing event chargeable=False（不计费）
            await _seed_clone(db, task_id="job_nocharge", chargeable=False)
            adjusted, _ = await cs._smart_clone_minute_offset(
                db, job=_job(), job_id="job_nocharge", user_id=_USER,
                service_mode="smart", actual_credits=1000,
            )
            assert adjusted == 1000
    _run(go())


def test_c_own_no_clone_inert():
    """🔥 无任何克隆（普通 smart 未勾选克隆）→ C_own=0 → 分钟全额。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            adjusted, audit = await cs._smart_clone_minute_offset(
                db, job=_job(), job_id="job_plain", user_id=_USER,
                service_mode="smart", actual_credits=1000,
            )
            assert adjusted == 1000 and audit == {}
    _run(go())


def test_non_smart_inert():
    """🔥 express/studio/free → 原样返回（不进抵扣）。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            # 即便有克隆行（不该发生），非 smart 也不抵
            await _seed_clone(db, task_id="job_studio")
            for mode in ("studio", "express", "free"):
                adjusted, audit = await cs._smart_clone_minute_offset(
                    db, job=_job(), job_id="job_studio", user_id=_USER,
                    service_mode=mode, actual_credits=1000,
                )
                assert adjusted == 1000 and audit == {}
    _run(go())


def test_zero_actual_credits_inert():
    """边界：actual_credits<=0（分钟未知）→ 原样返回（抵扣前返回，不越界）。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            await _seed_clone(db, task_id="job_zero")
            adjusted, _ = await cs._smart_clone_minute_offset(
                db, job=_job(), job_id="job_zero", user_id=_USER,
                service_mode="smart", actual_credits=0,
            )
            assert adjusted == 0
    _run(go())


# ===========================================================================
# C_carryover — 预览→完整 convert single-use 结转
# ===========================================================================


def test_carryover_first_convert_deducts_and_marks():
    """🔥🔥 首个完整任务：消费预览 600 结转 → 分钟 capture=400 + audit；预览行被标记。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            prid = await _seed_clone(db, task_id="job_preview", status="captured")
            f_state = {"preview_clone_offset_reservation_id": str(prid)}
            adjusted, audit = await cs._smart_clone_minute_offset(
                db, job=_job(f_state), job_id="job_full_F", user_id=_USER,
                service_mode="smart", actual_credits=1000,
            )
            assert adjusted == 400
            assert audit["clone_carryover_applied_credits"] == 600
            assert audit["clone_carryover_source_job_id"] == "job_preview"
            await db.commit()
            row = await _reservation_row(db, prid)
            assert row.carryover_applied_to_task_id == "job_full_F"
    _run(go())


def test_carryover_second_convert_no_double_deduct():
    """🔥🔥 同一预览第二个完整任务：结转已被首个消费 → C_carryover=0 → 全额分钟。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            prid = await _seed_clone(
                db, task_id="job_preview2", status="captured",
                carryover="job_first_F",  # 已被首个 F 消费
            )
            f_state = {"preview_clone_offset_reservation_id": str(prid)}
            adjusted, audit = await cs._smart_clone_minute_offset(
                db, job=_job(f_state), job_id="job_second_F", user_id=_USER,
                service_mode="smart", actual_credits=1000,
            )
            assert adjusted == 1000 and audit == {}
            await db.commit()
            row = await _reservation_row(db, prid)
            # 不被改写成第二个 F
            assert row.carryover_applied_to_task_id == "job_first_F"
    _run(go())


def test_carryover_idempotent_same_f_replay():
    """🔥🔥 F 自身 settle 重放（applied==F）→ 幂等仍抵 600，不报错不双改。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            prid = await _seed_clone(
                db, task_id="job_preview3", status="captured",
                carryover="job_F_self",
            )
            f_state = {"preview_clone_offset_reservation_id": str(prid)}
            adjusted, audit = await cs._smart_clone_minute_offset(
                db, job=_job(f_state), job_id="job_F_self", user_id=_USER,
                service_mode="smart", actual_credits=1000,
            )
            assert adjusted == 400
            assert audit["clone_carryover_applied_credits"] == 600
    _run(go())


def test_carryover_overreach_other_user_rejected():
    """🔥🔥 防越权：marker 指向**他人**的 captured reservation → C_carryover=0、不消费。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            prid = await _seed_clone(
                db, task_id="job_preview_other", user_id=_OTHER, status="captured",
            )
            f_state = {"preview_clone_offset_reservation_id": str(prid)}
            adjusted, audit = await cs._smart_clone_minute_offset(
                db, job=_job(f_state), job_id="job_thief_F", user_id=_USER,
                service_mode="smart", actual_credits=1000,
            )
            assert adjusted == 1000 and audit == {}
            await db.commit()
            row = await _reservation_row(db, prid)
            assert row.carryover_applied_to_task_id is None  # 未被越权消费
    _run(go())


def test_carryover_not_captured_rejected():
    """🔥 marker 指向**未 captured**（reserved/released）的 reservation → 不抵、不消费。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            prid = await _seed_clone(db, task_id="job_prev_resv", status="reserved")
            f_state = {"preview_clone_offset_reservation_id": str(prid)}
            adjusted, _ = await cs._smart_clone_minute_offset(
                db, job=_job(f_state), job_id="job_F2", user_id=_USER,
                service_mode="smart", actual_credits=1000,
            )
            assert adjusted == 1000
            await db.commit()
            row = await _reservation_row(db, prid)
            assert row.carryover_applied_to_task_id is None
    _run(go())


def test_carryover_captured_but_no_chargeable_event_rejected():
    """🔥 captured 但无 chargeable event（不一致 ledger）→ defense-in-depth 不抵、不消费。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            prid = await _seed_clone(
                db, task_id="job_prev_inconsistent", status="captured", chargeable=None,
            )
            f_state = {"preview_clone_offset_reservation_id": str(prid)}
            adjusted, _ = await cs._smart_clone_minute_offset(
                db, job=_job(f_state), job_id="job_F3", user_id=_USER,
                service_mode="smart", actual_credits=1000,
            )
            assert adjusted == 1000
            await db.commit()
            row = await _reservation_row(db, prid)
            assert row.carryover_applied_to_task_id is None
    _run(go())


def test_carryover_malformed_marker_inert():
    """边界：marker 非合法 uuid → 不抵（不抛）。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            f_state = {"preview_clone_offset_reservation_id": "not-a-uuid"}
            adjusted, _ = await cs._smart_clone_minute_offset(
                db, job=_job(f_state), job_id="job_F4", user_id=_USER,
                service_mode="smart", actual_credits=1000,
            )
            assert adjusted == 1000
    _run(go())


# ===========================================================================
# End-to-end 真账本：offset → shadow_capture 真扣（合计 max(600,分钟×100)）
# ===========================================================================


def test_e2e_offset_then_capture_real_bucket():
    """🔥🔥🔥 真账本：reserve 1000 → offset(克隆600)→400 → shadow_capture(400)：
    remaining 真扣 400、reserved 清 0（合计 = 400 分钟 + 600 克隆 = 1000 = max(600,1000)）。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=2000)
        async with sm() as db:
            await _seed_clone(db, task_id="job_e2e")
            # 分钟 reserve 1000
            await cs.reserve_credits_or_raise(
                db, user_id=_USER, job_id="job_e2e", estimated_credits=1000,
                service_mode="smart",
            )
            await db.commit()
            adjusted, _ = await cs._smart_clone_minute_offset(
                db, job=_job(), job_id="job_e2e", user_id=_USER,
                service_mode="smart", actual_credits=1000,
            )
            assert adjusted == 400
            await cs.shadow_capture(
                db, user_id=_USER, job_id="job_e2e", actual_credits=adjusted,
                service_mode="smart", reason_code="job_capture",
                reserve_reason_code="job_reserve",
            )
            await db.commit()
            b = (await db.execute(select(CreditsBucket).where(CreditsBucket.user_id == _USER))).scalar_one()
            # 分钟侧：reserve 1000 → capture 400 + release 600 超额 → remaining 2000-400=1600, reserved 0
            assert b.remaining == 1600
            assert b.reserved == 0
    _run(go())


# ===========================================================================
# 结构守卫：offset 接入 settle 两分支、在 shadow_capture 之前
# ===========================================================================


def _ast_func_src(name: str) -> str:
    src = _CS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def test_offset_wired_in_legacy_succeeded_branch():
    """🔥 settle_job_credit_ledger succeeded 分支：offset 在 shadow_capture 之前调。"""
    body = _ast_func_src("settle_job_credit_ledger")
    assert body
    assert "_smart_clone_minute_offset(" in body
    assert body.index("_smart_clone_minute_offset(") < body.rindex("shadow_capture(")


def test_offset_wired_in_capture_full_branch():
    """🔥 _settle_smart_job_credit_ledger capture_full 分支：offset 在 shadow_capture 之前调。"""
    body = _ast_func_src("_settle_smart_job_credit_ledger")
    assert body
    assert "_smart_clone_minute_offset(" in body
    assert body.index("_smart_clone_minute_offset(") < body.index("smart_capture_full")


# ===========================================================================
# settle 集成（STYLE A mock）：offset 的 adjusted 真流入 shadow_capture
# ===========================================================================


def _mock_settle_db(job):
    lock_result = MagicMock()
    lock_result.scalar_one_or_none = MagicMock(return_value=job)
    db = MagicMock()
    db.execute = AsyncMock(return_value=lock_result)
    return db


def test_settle_succeeded_passes_adjusted_to_capture(monkeypatch):
    """🔥🔥 settle succeeded：offset 返回 400 → shadow_capture 收到 actual_credits=400。"""
    job = SimpleNamespace(
        job_id="job_wire1", user_id=_USER, smart_state=None,
        metering_snapshot={"credits_estimated": 1000, "service_mode": "smart"},
        status="succeeded", actual_minutes=10.0, source_duration_seconds=600.0,
        service_mode="smart", tts_provider="minimax", tts_model="speech-2.8-turbo",
        role_snapshot="user",
    )
    db = _mock_settle_db(job)
    monkeypatch.setattr(cs, "should_settle_job_credits", lambda j: True)

    async def _fake_offset(_db, **k):
        return 400, {}
    monkeypatch.setattr(cs, "_smart_clone_minute_offset", _fake_offset)
    monkeypatch.setattr(cs, "ensure_credit_buckets_for_user", AsyncMock())

    captured = {}

    async def _fake_capture(_db, **k):
        captured.update(k)
        return []
    monkeypatch.setattr(cs, "shadow_capture", _fake_capture)

    _run(cs.settle_job_credit_ledger(db, job, "succeeded"))
    assert captured.get("actual_credits") == 400
    assert captured.get("reason_code") == "job_capture"


# ===========================================================================
# CodeX 复审 P1：carryover 减免真落进 smart_cost_summary.json（可审计）
# ===========================================================================


def test_cost_summary_backfill_stamps_carryover(tmp_path):
    """🔥🔥 CodeX P1：convert 600 结转减免写进 cost summary 的 breakdown（可审计），
    不只停在 metering_snapshot。否则 convert F 的低 pending_credits_charged 看着像漏扣。"""
    from cost_summary_backfill import backfill_smart_cost_summary

    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "smart_cost_summary.json").write_text(
        json.dumps({"service_mode": "smart", "pending_credits_charged": None}),
        encoding="utf-8",
    )
    ok = backfill_smart_cost_summary(
        service_mode="smart", project_dir=str(tmp_path), credit_entries=[],
        quota_used=None, carryover_applied_credits=600,
        carryover_source_job_id="job_preview",
    )
    assert ok is True
    written = json.loads((audit / "smart_cost_summary.json").read_text(encoding="utf-8"))
    bd = written["cost_breakdown_internal_only"]
    assert bd["clone_carryover_applied_credits"] == 600
    assert bd["clone_carryover_source_job_id"] == "job_preview"


def test_cost_summary_backfill_omits_carryover_when_none(tmp_path):
    """🔥 inert：非 convert（carryover=None）→ 不写 carryover 字段（单任务/普通 smart 不受扰）。"""
    from cost_summary_backfill import backfill_smart_cost_summary

    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "smart_cost_summary.json").write_text(
        json.dumps({"service_mode": "smart"}), encoding="utf-8",
    )
    backfill_smart_cost_summary(
        service_mode="smart", project_dir=str(tmp_path), credit_entries=[],
        quota_used=None,
    )
    written = json.loads((audit / "smart_cost_summary.json").read_text(encoding="utf-8"))
    bd = written.get("cost_breakdown_internal_only", {})
    assert "clone_carryover_applied_credits" not in bd
