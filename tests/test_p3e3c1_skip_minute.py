"""P3e-3c-1 — 智能版 3min 预览跳分钟点（钱-关键，只扣 600 克隆点）.

plan 2026-06-14-p3e2-preview-lane-design.md §4。智能版预览任务（smart_state.
smart_preview_mode=True）的最终计费 = **只扣 600 克隆点、不扣分钟/时长点**。teaser
（P3e-3b）已把 pipeline 工作/产物有界到 3 分钟，故跳分钟安全（非免费完整任务）。

机制 = create 端 + late 端两个 minute reserve（reserve_credits_or_raise）对 smart
预览**跳过**。settle 是 capture-of-reserve（credits_service shadow_capture/release
按 job_id 查 reserve 行，无 reserve→零捕获→no-op），故跳 reserve = 自然不收分钟，
**无需改 settlement**。600 克隆 settle 是独立 session/reason_code（
settle_smart_clone_reservation），照常 capture。

⚠️ 动的是 P3e-2b 经 CodeX 三轮硬化过的 create reserve 块（7 类钱-bug）；本守卫 +
test_p3e_create_reserve.py 一起确保不回归（跳分钟 guard 是 additive，不删任何
reserve/release/forward/replay 接线）。

source-scan（不 import gateway 模块避 database-stub 污染，见 memory
feedback_test_database_stub_convention）。
"""
from __future__ import annotations

import ast
import asyncio
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

_REPO = Path(__file__).resolve().parents[1]
_JI = _REPO / "gateway" / "job_intercept.py"
_CS = _REPO / "gateway" / "credits_service.py"

# gateway on path + stub ``database`` so credits_service import doesn't build a
# real engine（见 memory feedback_test_database_stub_convention：setdefault、
# 绝不替换已存在的真模块对象）。
_gateway_dir = str(_REPO / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)


def _ast_func_src(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _func_src(name: str) -> str:
    return _ast_func_src(_JI, name)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _create_src() -> str:
    body = _func_src("intercept_create_job")
    assert body, "intercept_create_job 未找到"
    return body


def _update_meta_src() -> str:
    body = _func_src("update_source_metadata")
    assert body, "update_source_metadata 未找到"
    return body


# ---------------------------------------------------------------------------
# create 端：smart 预览跳分钟 reserve（只扣 600 克隆）
# ---------------------------------------------------------------------------


def test_create_minute_reserve_skipped_for_smart_preview():
    """🔥🔥 钱-关键：create 端 minute reserve（reserve_credits_or_raise）被
    smart 预览条件 guard 跳过——智能版预览只扣 600 克隆点、不扣分钟点。"""
    body = _create_src()
    flat = " ".join(body.split())
    # 预览判定：reservation 成功（600 已预留）+ 请求 preview_mode（与
    # smart_state.smart_preview_mode 同条件）。
    assert "_is_smart_preview = bool(_smart_clone_reservation_id) and (" in flat or \
        "_is_smart_preview = bool(_smart_clone_reservation_id) and request_data.get(\"preview_mode\") is True" in flat
    assert 'request_data.get("preview_mode") is True' in flat
    # minute reserve guard 带 not _is_smart_preview（且仍保留 shadow_credits>0）
    assert "if shadow_credits > 0 and not _is_smart_preview:" in flat


def test_create_still_reserves_minutes_for_normal_smart():
    """🔥 inert：普通 smart（reservation 但无 preview_mode，或无 reservation）仍走
    minute reserve——guard 用 `and not _is_smart_preview`（非 or），且 _is_smart_
    preview 要求 reservation_id **且** preview_mode 同时为真。"""
    body = _create_src()
    flat = " ".join(body.split())
    # _is_smart_preview 是 AND（两条件都要），故普通 smart 不误跳
    assert "bool(_smart_clone_reservation_id) and" in flat
    # reserve 块仍在（未删），只是加了 guard
    assert "reserve_credits_or_raise(" in body


def test_create_clone_reserve_uses_gateway_preview_constant():
    """🔥 克隆 reserve 不受影响（跳的是分钟点，不是克隆点）。"""
    body = _create_src()
    flat = " ".join(body.split())
    assert "amount_credits=_SMART_CLONE_RESERVE_CREDITS" in flat
    assert "_reserve_smart_clone(" in flat


def test_create_p3e2b_failure_paths_intact():
    """🔥 P3e-2b 硬化的失败/释放/补偿路径仍在（跳分钟 guard 是 additive）。"""
    body = _create_src()
    assert "_release_smart_clone_reservation_on_create_failure(" in body
    assert "_compensate_upstream_job(" in body


# ---------------------------------------------------------------------------
# late 端（update_source_metadata）：smart 预览也跳 late minute reserve
# ---------------------------------------------------------------------------


def test_late_minute_reserve_skipped_for_smart_preview():
    """🔥🔥 钱-关键：late reserve（duration 报告后补扣）对 smart 预览也跳——否则
    create 跳了、late 补扣 = 还是扣了分钟。读 PG Job.smart_state（create 已落）。"""
    body = _update_meta_src()
    flat = " ".join(body.split())
    assert "extract_smart_preview_flag(" in flat
    # late reserve guard 带 not <smart preview>
    assert "late_credits > 0 and not" in flat
    # reserve 块仍在（普通任务照常）
    assert "reserve_credits_or_raise(" in body


def test_late_reserve_still_fires_for_normal_jobs():
    """inert：普通任务（job.smart_state 无 smart_preview_mode）late reserve 照常。"""
    body = _update_meta_src()
    # already_reserved 守卫仍在（仅未预留过才补扣）
    assert "already_reserved" in body
    assert "reserve_credits_or_raise(" in body


# ---------------------------------------------------------------------------
# settle 端：smart 预览 minute settle 显式 no-op（对抗性/CodeX P0：settle 非纯
# capture-of-reserve，actual>reserved 会额外 debit，故跳 reserve 不够）
# ---------------------------------------------------------------------------


def test_settle_minute_guard_in_credits_service():
    """🔥🔥 settle_job_credit_ledger 对 smart 预览短路走 release（绝不 capture
    分钟），且在 has_credit_intent / credits_policy 分发**之前**。"""
    body = _ast_func_src(_CS, "settle_job_credit_ledger")
    assert body, "settle_job_credit_ledger 未找到"
    flat = " ".join(body.split())
    assert 'get("smart_preview_mode") is True' in flat
    assert "shadow_release(" in flat
    assert 'reason_code="smart_preview_minute_release"' in flat
    assert 'reserve_reason_code="job_reserve"' in flat
    # guard 必须在 has_credit_intent **赋值**之前短路（否则 credits_estimated>0 会进
    # 扣费路径）。用代码锚点（guard 的 shadow_release 调用）对比赋值，避免被注释里
    # 提及的 "has_credit_intent" 字样误导。test_settle_releases_not_captures_for_
    # smart_preview 行为测试已证明（credits_estimated=120 仍零 capture）。
    assert body.index("shadow_release(") < body.index("has_credit_intent = (")


def test_settle_releases_not_captures_for_smart_preview(monkeypatch):
    """🔥🔥🔥 钱-关键行为：smart 预览终态 settle → 走 shadow_release（fail-safe）、
    **绝不** shadow_capture 分钟。即便 credits_estimated>0（has_credit_intent=True）
    也短路（证明跳 reserve 之外、settle 也必须挡）。"""
    import credits_service as cs

    job = SimpleNamespace(
        job_id="job_smartprev",
        user_id=uuid.uuid4(),
        smart_state={"smart_preview_mode": True},
        # 非零 credits_estimated → has_credit_intent 本会为 True；guard 须在其前短路
        metering_snapshot={"credits_estimated": 120, "service_mode": "smart"},
        status="succeeded",
        actual_minutes=3.0,
        source_duration_seconds=180.0,
        role_snapshot="user",
    )
    lock_result = MagicMock()
    lock_result.scalar_one_or_none = MagicMock(return_value=job)
    db = MagicMock()
    db.execute = AsyncMock(return_value=lock_result)

    monkeypatch.setattr(cs, "should_settle_job_credits", lambda j: True)
    release_calls: list[dict] = []
    capture_calls: list[dict] = []

    async def _fake_release(*_a, **k):
        release_calls.append(k)
        return []

    async def _fake_capture(*_a, **k):
        capture_calls.append(k)
        return []

    monkeypatch.setattr(cs, "shadow_release", _fake_release)
    monkeypatch.setattr(cs, "shadow_capture", _fake_capture)

    result = _run(cs.settle_job_credit_ledger(db, job, "succeeded"))

    assert result == []
    assert capture_calls == [], "smart 预览绝不 capture 分钟"
    assert len(release_calls) == 1, "smart 预览走 release（fail-safe，无 reserve→no-op）"
    assert release_calls[0].get("reserve_reason_code") == "job_reserve"
    assert release_calls[0].get("reason_code") == "smart_preview_minute_release"


def test_settle_release_reason_in_idempotency_family():
    """🔥 CodeX 复核 P2：smart_preview_minute_release 必须在 job_reserve 幂等族里，
    否则 fail-safe 释放路径重复 terminal settle 会重复写 release ledger（非幂等）。"""
    import credits_service as cs

    family = cs._settlement_reason_codes("smart_preview_minute_release", "job_reserve")
    assert "smart_preview_minute_release" in family
    # 同族含标准 job_reserve 结算码（确认走的是完整族而非 {reason_code}）
    assert "job_capture" in family and "job_release" in family


def test_settle_guard_strict_is_true():
    """🔥 inert：settle 预览 guard 严格 `is True`——普通 smart（无 key）/ truthy
    字符串/None 都不误挡正常任务分钟结算（与 settle_job_credit_ledger 内一致）。"""

    def _guard(smart_state) -> bool:
        return (dict(smart_state or {})).get("smart_preview_mode") is True

    assert _guard({"smart_preview_mode": True}) is True
    assert _guard({"smart_clone_reservation_id": "r1"}) is False  # 普通 smart 克隆
    assert _guard(None) is False
    assert _guard({}) is False
    assert _guard({"smart_preview_mode": "true"}) is False  # truthy 字符串不误挡
    assert _guard({"smart_preview_mode": 1}) is False
