"""支付对账 sweeper + admin unsettled 端点（audit 2026-06-12 P1）。

覆盖面：
- reconcile_once 的编排语义：统计计数、单笔异常隔离、结算只经注入的
  refresh_fn（= billing 单一入口）
- 选单语句的窗口/状态/fake 排除（SQL 文本守卫）
- sweeper_loop 单轮崩溃续命（与 express_reservation_sweeper 同模式）
- admin 端点鉴权（401/403）与序列化 happy path
- main.py 接线守卫（lifespan create_task + shutdown 清单 + 路由注册）
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)

import billing_reconciliation as recon  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeSession:
    """最小 AsyncSession 替身：execute 按入队顺序吐结果。"""

    def __init__(self, results):
        self._results = list(results)
        self.executed = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        return _FakeResult(self._results.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_factory(rows):
    def factory():
        return _FakeSession([rows])

    return factory


def _order(status="pending", provider="wechatpay"):
    return SimpleNamespace(
        id="ord-" + status,
        user_id="u1",
        provider=provider,
        provider_order_id=None,
        status=status,
        amount_cny=990,
        target_plan_code="plus",
        billing_period="monthly",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        updated_at=None,
        paid_at=None,
    )


# ---------------------------------------------------------------------------
# reconcile_once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconcile_once_stats_and_per_order_isolation():
    """单笔 refresh 异常只计 errors，不中断其余订单；结算计 settled。"""
    settles = _order("pending")
    explodes = _order("created")
    stays = _order("pending")

    calls = []

    async def fake_refresh(*, db, order):
        calls.append(order)
        if order is settles:
            order.status = "paid"
        elif order is explodes:
            raise RuntimeError("provider query boom")
        # stays: no-op，保持 pending

    stats = await recon.reconcile_once(
        session_factory=_fake_factory([settles, explodes, stays]),
        refresh_fn=fake_refresh,
    )

    assert stats == {"scanned": 3, "settled": 1, "errors": 1}
    # 异常发生在第 2 笔，第 3 笔仍被处理（隔离语义）
    assert calls == [settles, explodes, stays]


@pytest.mark.asyncio
async def test_reconcile_once_empty_batch_is_noop():
    async def must_not_be_called(**kwargs):
        pytest.fail("空批次不应触发 provider 查询")

    stats = await recon.reconcile_once(
        session_factory=_fake_factory([]),
        refresh_fn=must_not_be_called,
    )
    assert stats == {"scanned": 0, "settled": 0, "errors": 0}


def test_candidate_stmt_window_and_exclusions():
    """选单语句必须：限定 created/pending、排除 fake、带上下两端时间窗。"""
    sql = str(recon._candidate_orders_stmt(datetime.now(timezone.utc)))
    assert "payment_orders.status IN" in sql
    assert "payment_orders.provider !=" in sql
    # 上下两端窗口（MIN_AGE 防与前端轮询打架；MAX_AGE 防滞留单无限打 provider）
    assert "payment_orders.created_at <=" in sql
    assert "payment_orders.created_at >=" in sql
    assert "LIMIT" in sql


# ---------------------------------------------------------------------------
# sweeper_loop 续命
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sweeper_loop_survives_tick_failure(monkeypatch):
    monkeypatch.setattr(recon, "INITIAL_DELAY_S", 0)
    monkeypatch.setattr(recon, "SWEEP_INTERVAL_S", 0)

    stop = asyncio.Event()
    ticks = {"n": 0}

    async def flaky_reconcile(**kwargs):
        ticks["n"] += 1
        if ticks["n"] == 1:
            raise RuntimeError("transient db boom")
        if ticks["n"] >= 3:
            stop.set()
        return {"scanned": 0, "settled": 0, "errors": 0}

    monkeypatch.setattr(recon, "reconcile_once", flaky_reconcile)
    await asyncio.wait_for(recon.sweeper_loop(stop_event=stop), timeout=5)
    assert ticks["n"] >= 3  # 第 1 轮崩溃后 loop 仍续命跑满后续轮次


# ---------------------------------------------------------------------------
# admin 端点
# ---------------------------------------------------------------------------

def _build_admin_app(user, db_results):
    from fastapi import FastAPI
    from admin_billing_api import router
    from auth import get_current_user
    from database import get_db

    app = FastAPI()
    app.include_router(router)

    async def override_user():
        return user

    async def override_db():
        yield _FakeSession(db_results)

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return app


def _event(signature_valid=False, error_message=None):
    return SimpleNamespace(
        id="evt-1",
        provider="paddle",
        provider_event_id="pe-1",
        event_type="transaction.completed",
        signature_valid=signature_valid,
        processed=True,
        error_message=error_message,
        received_at=datetime.now(timezone.utc),
    )


def test_unsettled_requires_login_and_admin():
    from fastapi.testclient import TestClient

    client = TestClient(_build_admin_app(None, [[], []]))
    assert client.get("/api/admin/billing/unsettled").status_code == 401

    client = TestClient(
        _build_admin_app(SimpleNamespace(role="user", id="u1"), [[], []])
    )
    assert client.get("/api/admin/billing/unsettled").status_code == 403


def test_unsettled_serializes_orders_and_events():
    from fastapi.testclient import TestClient

    order = _order("pending")
    event = _event(signature_valid=False, error_message="bad signature")
    client = TestClient(
        _build_admin_app(SimpleNamespace(role="admin", id="a1"), [[order], [event]])
    )
    resp = client.get("/api/admin/billing/unsettled")
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"] == {"pending_orders": 1, "suspect_webhook_events": 1}
    assert body["pending_orders"][0]["order_id"] == order.id
    assert body["pending_orders"][0]["amount_cny"] == 990
    assert body["suspect_webhook_events"][0]["signature_valid"] is False


def test_reconcile_endpoint_runs_reconcile_once(monkeypatch):
    from fastapi.testclient import TestClient
    import admin_billing_api  # noqa: F401

    async def fake_reconcile_once(**kwargs):
        return {"scanned": 2, "settled": 1, "errors": 0}

    monkeypatch.setattr(recon, "reconcile_once", fake_reconcile_once)
    client = TestClient(
        _build_admin_app(SimpleNamespace(role="admin", id="a1"), [])
    )
    resp = client.post("/api/admin/billing/reconcile")
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "stats": {"scanned": 2, "settled": 1, "errors": 0},
    }


# ---------------------------------------------------------------------------
# 接线守卫
# ---------------------------------------------------------------------------

def test_main_wiring_guards():
    src = (Path(_GATEWAY_DIR) / "main.py").read_text(encoding="utf-8")
    assert "from billing_reconciliation import sweeper_loop" in src, (
        "lifespan 未启动 billing_reconciliation sweeper"
    )
    assert '"billing_reconciliation_sweeper_task",' in src, (
        "shutdown 取消清单漏了 billing_reconciliation_sweeper_task（会 race engine.dispose）"
    )
    assert "from admin_billing_api import router as admin_billing_router" in src
    assert "app.include_router(admin_billing_router)" in src


def test_reconciliation_uses_single_settlement_entry():
    """结算单一入口守卫：模块只通过 billing._refresh_order_from_provider 结算，
    自己不 import 任何 provider 适配器、不直接调 _process_payment_event。"""
    src = (Path(_GATEWAY_DIR) / "billing_reconciliation.py").read_text(encoding="utf-8")
    assert "_refresh_order_from_provider" in src
    # 文档里提及 _process_payment_event 没问题，但不得出现实际调用。
    assert "_process_payment_event(" not in src, (
        "billing_reconciliation 不得绕过 _refresh_order_from_provider 直接结算"
    )
    for forbidden in (
        "payment_provider_wechat",
        "payment_provider_paddle",
        "payment_provider_alipay",
        "httpx",
        "requests",
    ):
        assert f"import {forbidden}" not in src, (
            f"billing_reconciliation 不得直接依赖 {forbidden}（渠道逻辑只在 billing 单一入口）"
        )
