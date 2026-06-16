"""R7 退款闭环（audit 2026-06-12 P1 / Codex 复核「退款链路三处断裂」）。

三处断裂的回归覆盖：
1. 绑定断裂——微信 REFUND.* / Paddle adjustment 事件不携带我们的 order_id，
   现在由 billing._resolve_refund_order_id 按 provider_order_id 反查；
2. 幂等键撞车——微信 refund resource 携带与原支付相同的 transaction_id，
   parse 层必须改用 refund_id/out_refund_no 作 provider_event_id；
3. 权益不回收——refunded 结算现在回收订阅 / plan 投影 / 本单 credits，
   部分退款（金额可知且小于订单额）不自动回收留人工复核。
"""
from __future__ import annotations

import inspect
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)

import billing  # noqa: E402
import payment_provider_paddle as paddle  # noqa: E402
import payment_provider_wechat as wechat  # noqa: E402
import subscriptions  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one(self):
        return self._rows[0]

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        rows = self._rows

        class _S:
            def all(self):
                return list(rows)

        return _S()


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.executed = []
        self.added = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        return _FakeResult(self._results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def flush(self):
        pass


def _order(**overrides):
    base = dict(
        id="ord-1",
        user_id="u1",
        provider="wechatpay",
        provider_order_id="AVT_abc123",
        status="paid",
        amount_cny=990,
        target_plan_code="plus",
        billing_period="monthly",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 微信 REFUND.* 解析
# ---------------------------------------------------------------------------

def _wechat_envelope(event_type: str) -> bytes:
    return json.dumps({
        "id": "EV-refund-1",
        "event_type": event_type,
        "resource": {"ciphertext": "x", "nonce": "n"},
    }).encode("utf-8")


def _refund_resource(refund_status="SUCCESS"):
    return {
        "out_trade_no": "AVT_abc123",
        "transaction_id": "4200001234",
        "refund_id": "50300001234",
        "out_refund_no": "REF_1",
        "refund_status": refund_status,
        "amount": {"total": 990, "refund": 990},
    }


def test_wechat_refund_success_parses_refunded(monkeypatch):
    resource = _refund_resource("SUCCESS")
    monkeypatch.setattr(wechat, "decrypt_resource", lambda config, res: resource)
    parsed = wechat.parse_wechat_webhook(
        SimpleNamespace(), _wechat_envelope("REFUND.SUCCESS")
    )
    assert parsed.new_status == "refunded"
    assert parsed.out_trade_no == "AVT_abc123"
    # order_id 留空，绑定交给 billing 层反查 provider_order_id
    assert parsed.order_id == ""
    # 幂等键必须避开 transaction_id（与原支付结算事件撞唯一键会被当重复丢弃）
    assert parsed.provider_event_id == "50300001234"
    assert parsed.provider_event_id != resource["transaction_id"]


def test_wechat_refund_non_success_stays_pending(monkeypatch):
    resource = _refund_resource("ABNORMAL")
    monkeypatch.setattr(wechat, "decrypt_resource", lambda config, res: resource)
    parsed = wechat.parse_wechat_webhook(
        SimpleNamespace(), _wechat_envelope("REFUND.ABNORMAL")
    )
    assert parsed.new_status == "pending"


def test_wechat_payment_event_unchanged(monkeypatch):
    """回归：支付结算事件仍用 transaction_id 作幂等键、trade_state 定状态。"""
    txn = {
        "out_trade_no": "AVT_abc123",
        "transaction_id": "4200001234",
        "trade_state": "SUCCESS",
        "attach": "ord-1",
    }
    monkeypatch.setattr(wechat, "decrypt_resource", lambda config, res: txn)
    parsed = wechat.parse_wechat_webhook(
        SimpleNamespace(), _wechat_envelope("TRANSACTION.SUCCESS")
    )
    assert parsed.new_status == "paid"
    assert parsed.provider_event_id == "4200001234"
    assert parsed.order_id == "ord-1"


# ---------------------------------------------------------------------------
# Paddle adjustment 解析
# ---------------------------------------------------------------------------

def _paddle_adjustment(
    action="refund", status="approved", event_type="adjustment.created"
) -> bytes:
    return json.dumps({
        "event_id": "evt_adj_1",
        "event_type": event_type,
        "data": {
            "id": "adj_1",
            "transaction_id": "txn_1",
            "action": action,
            "status": status,
            "totals": {"total": "990", "currency_code": "CNY"},
        },
    }).encode("utf-8")


@pytest.mark.parametrize("event_type", ["adjustment.created", "adjustment.updated"])
@pytest.mark.parametrize(
    ("action", "status", "expected"),
    [
        ("refund", "approved", "refunded"),
        ("chargeback", "approved", "refunded"),
        ("refund", "pending_approval", "pending"),
        ("refund", "rejected", "pending"),
        ("credit", "approved", "pending"),
    ],
)
def test_paddle_adjustment_status_gating(action, status, expected, event_type):
    """created/updated 必须走同一门控——live 环境人工审批的退款先发
    created(pending_approval)、审批后由 updated 携带 approved（Codex P1）。"""
    parsed = paddle.parse_paddle_webhook(
        _paddle_adjustment(action, status, event_type)
    )
    assert parsed.new_status == expected
    assert parsed.provider_event_id == "evt_adj_1"
    assert parsed.order_id == ""  # adjustment 无 custom_data，绑定走反查


def test_paddle_transaction_event_unchanged():
    body = json.dumps({
        "event_id": "evt_txn_1",
        "event_type": "transaction.completed",
        "data": {"id": "txn_1", "custom_data": {"order_id": "ord-1"}},
    }).encode("utf-8")
    parsed = paddle.parse_paddle_webhook(body)
    assert parsed.new_status == "paid"
    assert parsed.order_id == "ord-1"


# ---------------------------------------------------------------------------
# 反查绑定
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_refund_order_id_paddle():
    order = _order(provider="paddle", provider_order_id="txn_1")
    session = _FakeSession([[order]])
    oid = await billing._resolve_refund_order_id(
        session,
        provider_name="paddle",
        raw_payload={"data": {"transaction_id": "txn_1"}},
    )
    assert oid == "ord-1"


@pytest.mark.asyncio
async def test_resolve_refund_order_id_wechat():
    order = _order()
    session = _FakeSession([[order]])
    oid = await billing._resolve_refund_order_id(
        session,
        provider_name="wechatpay",
        raw_payload={"transaction": {"out_trade_no": "AVT_abc123"}},
    )
    assert oid == "ord-1"


@pytest.mark.asyncio
async def test_resolve_refund_order_id_no_token_skips_db():
    session = _FakeSession([])  # 队列为空：一旦查库会 IndexError
    oid = await billing._resolve_refund_order_id(
        session, provider_name="paddle", raw_payload={"data": {}}
    )
    assert oid == ""
    assert session.executed == []


# ---------------------------------------------------------------------------
# 退款 fact-gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_paddle_refund_gate_pass_and_txn_mismatch():
    order = _order(provider="paddle", provider_order_id="txn_1")
    ok_payload = {
        "event_type": "adjustment.created",
        "data": {"transaction_id": "txn_1", "action": "refund", "status": "approved"},
    }
    assert await billing._validate_paddle_event_against_order(
        db=_FakeSession([[order]]), order_id="ord-1", payload=ok_payload
    ) is True

    bad_payload = {
        "event_type": "adjustment.created",
        "data": {"transaction_id": "txn_OTHER", "action": "refund", "status": "approved"},
    }
    assert await billing._validate_paddle_event_against_order(
        db=_FakeSession([[order]]), order_id="ord-1", payload=bad_payload
    ) is False


@pytest.mark.asyncio
async def test_wechat_refund_gate_mirrors_payment_gate(monkeypatch):
    """退款 gate 不得比支付 gate 松（Codex P2）：mchid 硬门 + out_trade_no
    绑定 + SUCCESS + 原单总额 fail-closed（缺失即拒）。"""
    monkeypatch.setattr(
        wechat.WechatPayConfig, "from_env",
        classmethod(lambda cls: SimpleNamespace(mchid="MCH1", appid="")),
    )
    order = _order()

    def payload(total=990, refund_status="SUCCESS",
                out_trade_no="AVT_abc123", mchid="MCH1"):
        body = {
            "out_trade_no": out_trade_no,
            "refund_status": refund_status,
            "mchid": mchid,
        }
        if total is not None:
            body["amount"] = {"total": total, "refund": 990}
        return {"event_type": "REFUND.SUCCESS", "transaction": body}

    async def gate(p):
        return await billing._validate_wechat_event_against_order(
            db=_FakeSession([[order]]), order_id="ord-1", payload=p
        )

    assert await gate(payload()) is True
    # mchid 不符（商户号与 AiPlay 共用，转发回调必须拒）→ 拒
    assert await gate(payload(mchid="MCH_OTHER")) is False
    # 原单总额不符 → 拒
    assert await gate(payload(total=100)) is False
    # 金额缺失 → 拒（fail-closed，不再放行）
    assert await gate(payload(total=None)) is False
    # refund_status 非 SUCCESS → 拒
    assert await gate(payload(refund_status="CLOSED")) is False
    # out_trade_no 不符 → 拒
    assert await gate(payload(out_trade_no="AVT_other")) is False


@pytest.mark.asyncio
async def test_paddle_refund_gate_accepts_adjustment_updated():
    """审批通过走 adjustment.updated——validator 必须与 created 同门控。"""
    order = _order(provider="paddle", provider_order_id="txn_1")
    payload = {
        "event_type": "adjustment.updated",
        "data": {"transaction_id": "txn_1", "action": "refund", "status": "approved"},
    }
    assert await billing._validate_paddle_event_against_order(
        db=_FakeSession([[order]]), order_id="ord-1", payload=payload
    ) is True


# ---------------------------------------------------------------------------
# 退款金额提取
# ---------------------------------------------------------------------------

def test_extract_refund_amount_fen():
    assert billing._extract_refund_amount_fen(
        "wechatpay", {"transaction": {"amount": {"total": 990, "refund": 500}}}
    ) == 500
    assert billing._extract_refund_amount_fen(
        "paddle", {"data": {"totals": {"total": "990"}}}
    ) is None
    assert billing._extract_refund_amount_fen("wechatpay", {"transaction": {}}) is None
    assert billing._extract_refund_amount_fen(
        "paddle", {"data": {"totals": {"total": "abc"}}}
    ) is None
    assert billing._extract_refund_amount_fen("alipay", {}) is None


def test_extract_paddle_refund_amount_prefers_subtotal_before_tax():
    assert billing._extract_refund_amount_fen(
        "paddle",
        {
            "data": {
                "totals": {
                    "subtotal": "600",
                    "tax": "480",
                    "total": "1080",
                    "currency_code": "CNY",
                }
            }
        },
    ) == 600


# ---------------------------------------------------------------------------
# 权益回收
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recall_downgrades_matching_plan(monkeypatch):
    user = SimpleNamespace(id="u1", plan_code="plus")
    sub = SimpleNamespace(
        user_id="u1", plan_code="plus", status="active",
        cancelled_at=None, updated_at=None,
    )
    session = _FakeSession([[user], [sub], []])

    revoke_calls = {}

    async def fake_revoke(db, *, user_id, related_order_id, reason_code="refund_revoke"):
        revoke_calls["args"] = (user_id, related_order_id)
        return 1

    import credits_service
    monkeypatch.setattr(credits_service, "revoke_buckets_for_order", fake_revoke)

    now = datetime.now(timezone.utc)
    await billing._recall_entitlements_for_refund(session, order=_order(), now=now)

    assert user.plan_code == "free"
    assert sub.status == "cancelled"
    assert sub.cancelled_at == now
    assert revoke_calls["args"] == ("u1", "ord-1")
    audit = [a for a in session.added if getattr(a, "action", "") == "payment_refund_downgrade"]
    assert len(audit) == 1
    assert audit[0].old_value == "plus" and audit[0].new_value == "free"


@pytest.mark.asyncio
async def test_recall_keeps_same_plan_when_another_paid_order_remains(monkeypatch):
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    user = SimpleNamespace(id="u1", plan_code="plus")
    sub = SimpleNamespace(
        user_id="u1", plan_code="plus", status="active",
        cancelled_at=None, updated_at=None,
    )
    other_paid_order = SimpleNamespace(
        id="ord-2",
        target_plan_code="plus",
        billing_period="monthly",
        paid_at=now - timedelta(days=1),
    )
    session = _FakeSession([[user], [sub], [other_paid_order]])
    revoke_calls = {}

    async def fake_revoke(db, *, user_id, related_order_id, reason_code="refund_revoke"):
        revoke_calls["args"] = (user_id, related_order_id)
        return 1

    import credits_service
    monkeypatch.setattr(credits_service, "revoke_buckets_for_order", fake_revoke)

    await billing._recall_entitlements_for_refund(
        session, order=_order(id="ord-1", target_plan_code="plus"),
        now=now,
    )

    assert user.plan_code == "plus"
    assert sub.status == "active"
    assert sub.cancelled_at is None
    assert [a for a in session.added if getattr(a, "action", "") == "payment_refund_downgrade"] == []
    assert revoke_calls["args"] == ("u1", "ord-1")


@pytest.mark.asyncio
async def test_recall_restores_same_plan_subscription_period_from_remaining_order(monkeypatch):
    monthly_paid_at = datetime(2026, 1, 10, tzinfo=timezone.utc)
    annual_paid_at = datetime(2026, 1, 12, tzinfo=timezone.utc)
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    user = SimpleNamespace(id="u1", plan_code="plus")
    sub = SimpleNamespace(
        user_id="u1", plan_code="plus", billing_period="annual",
        provider="wechatpay", status="active", cancelled_at=None,
        updated_at=None, current_period_start=annual_paid_at,
        current_period_end=annual_paid_at + timedelta(days=365),
    )
    remaining_monthly_order = _order(
        id="ord-plus-monthly",
        provider="paddle",
        target_plan_code="plus",
        status="paid",
        billing_period="monthly",
        paid_at=monthly_paid_at,
    )
    session = _FakeSession([[user], [sub], [remaining_monthly_order]])

    async def fake_revoke(db, *, user_id, related_order_id, reason_code="refund_revoke"):
        return 1

    import credits_service
    monkeypatch.setattr(credits_service, "revoke_buckets_for_order", fake_revoke)

    await billing._recall_entitlements_for_refund(
        session,
        order=_order(
            id="ord-plus-annual",
            target_plan_code="plus",
            billing_period="annual",
            paid_at=annual_paid_at,
        ),
        now=now,
    )

    assert user.plan_code == "plus"
    assert sub.status == "active"
    assert sub.plan_code == "plus"
    assert sub.billing_period == "monthly"
    assert sub.provider == "paddle"
    assert sub.cancelled_at is None
    assert sub.updated_at == now
    assert sub.current_period_start == monthly_paid_at
    assert sub.current_period_end == monthly_paid_at + timedelta(days=30)
    assert [a for a in session.added if getattr(a, "action", "") == "payment_refund_downgrade"] == []


@pytest.mark.asyncio
async def test_recall_restores_lower_paid_plan_when_refunded_upgrade(monkeypatch):
    plus_paid_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    pro_period_end = datetime(2027, 1, 1, tzinfo=timezone.utc)
    user = SimpleNamespace(id="u1", plan_code="pro")
    sub = SimpleNamespace(
        user_id="u1", plan_code="pro", billing_period="monthly",
        provider="wechatpay", status="active", cancelled_at=None, updated_at=None,
        current_period_start=datetime(2026, 2, 1, tzinfo=timezone.utc),
        current_period_end=pro_period_end,
    )
    lower_paid_order = _order(
        id="ord-plus",
        target_plan_code="plus",
        status="paid",
        billing_period="monthly",
        paid_at=plus_paid_at,
    )
    session = _FakeSession([[user], [sub], [lower_paid_order]])

    async def fake_revoke(db, *, user_id, related_order_id, reason_code="refund_revoke"):
        return 1

    import credits_service
    monkeypatch.setattr(credits_service, "revoke_buckets_for_order", fake_revoke)

    await billing._recall_entitlements_for_refund(
        session,
        order=_order(id="ord-pro", target_plan_code="pro"),
        now=now,
    )

    assert user.plan_code == "plus"
    assert sub.status == "active"
    assert sub.plan_code == "plus"
    assert sub.cancelled_at is None
    assert sub.updated_at == now
    assert sub.current_period_start == plus_paid_at
    assert sub.current_period_end == plus_paid_at + timedelta(days=30)
    audit = [a for a in session.added if getattr(a, "action", "") == "payment_refund_downgrade"]
    assert len(audit) == 1
    assert audit[0].old_value == "pro" and audit[0].new_value == "plus"


@pytest.mark.asyncio
async def test_recall_ignores_expired_lower_paid_plan_when_refunded_upgrade(monkeypatch):
    now = datetime(2026, 3, 15, tzinfo=timezone.utc)
    expired_paid_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    user = SimpleNamespace(id="u1", plan_code="pro")
    sub = SimpleNamespace(
        user_id="u1", plan_code="pro", billing_period="annual",
        provider="wechatpay", status="active", cancelled_at=None, updated_at=None,
        current_period_start=datetime(2026, 2, 1, tzinfo=timezone.utc),
        current_period_end=datetime(2027, 2, 1, tzinfo=timezone.utc),
    )
    expired_order = _order(
        id="ord-plus-expired",
        target_plan_code="plus",
        status="paid",
        billing_period="monthly",
        paid_at=expired_paid_at,
    )
    session = _FakeSession([[user], [sub], [expired_order]])

    async def fake_revoke(db, *, user_id, related_order_id, reason_code="refund_revoke"):
        return 1

    import credits_service
    monkeypatch.setattr(credits_service, "revoke_buckets_for_order", fake_revoke)

    await billing._recall_entitlements_for_refund(
        session,
        order=_order(id="ord-pro", target_plan_code="pro"),
        now=now,
    )

    assert user.plan_code == "free"
    assert sub.status == "cancelled"
    assert sub.cancelled_at == now
    audit = [a for a in session.added if getattr(a, "action", "") == "payment_refund_downgrade"]
    assert len(audit) == 1
    assert audit[0].old_value == "pro" and audit[0].new_value == "free"


@pytest.mark.asyncio
async def test_refund_recall_locks_user_and_subscription_before_mutation(monkeypatch):
    user = SimpleNamespace(id="u1", plan_code="plus")
    sub = SimpleNamespace(
        user_id="u1", plan_code="plus", status="active",
        cancelled_at=None, updated_at=None,
    )
    session = _FakeSession([[user], [sub], []])

    async def fake_revoke(db, *, user_id, related_order_id, reason_code="refund_revoke"):
        return 1

    import credits_service
    monkeypatch.setattr(credits_service, "revoke_buckets_for_order", fake_revoke)

    await billing._recall_entitlements_for_refund(
        session, order=_order(target_plan_code="plus"),
        now=datetime.now(timezone.utc),
    )

    user_stmt = session.executed[0]
    sub_stmt = session.executed[1]
    assert getattr(user_stmt, "_for_update_arg", None) is not None
    assert user_stmt.get_execution_options().get("populate_existing") is True
    assert getattr(sub_stmt, "_for_update_arg", None) is not None
    assert sub_stmt.get_execution_options().get("populate_existing") is True


@pytest.mark.asyncio
async def test_paid_settlement_locks_user_row_before_subscription_upsert(monkeypatch):
    order = _order(id="ord-paid", status="pending", target_plan_code="plus")
    user = SimpleNamespace(id="u1", plan_code="free")
    event = SimpleNamespace(processed=False, error_message=None, processed_at=None)
    session = _FakeSession([["evt-row-1"], [event], [order], [user]])

    async def fake_record_invoice_for_order(db, *, order, settled_at, status):
        return SimpleNamespace(subscription_id=None)

    async def fake_upsert_active_subscription(db, *, user, order, paid_at):
        return SimpleNamespace(id="sub-1", current_period_end=None)

    async def fake_ensure_subscription_bucket(*args, **kwargs):
        return None

    monkeypatch.setattr(
        billing, "record_invoice_for_order", fake_record_invoice_for_order
    )
    monkeypatch.setattr(
        billing, "upsert_active_subscription", fake_upsert_active_subscription
    )
    import credits_service
    monkeypatch.setattr(
        credits_service, "ensure_subscription_bucket", fake_ensure_subscription_bucket
    )

    await billing._process_payment_event(
        db=session,
        provider="wechatpay",
        provider_event_id="evt-paid-lock",
        event_type="PAY.SUCCESS",
        order_id=order.id,
        new_status="paid",
        signature_valid=True,
        raw_payload={},
    )

    user_stmt = session.executed[3]
    assert getattr(user_stmt, "_for_update_arg", None) is not None
    assert user_stmt.get_execution_options().get("populate_existing") is True


def test_upsert_active_subscription_locks_active_subscription_row():
    src = inspect.getsource(subscriptions.upsert_active_subscription)
    assert ".with_for_update()" in src
    assert "populate_existing=True" in src


@pytest.mark.asyncio
async def test_recall_keeps_newer_plan(monkeypatch):
    """退款单的计划是 plus，但用户已升到 pro：订阅与 plan 投影都不动，
    只回收本单 credits。"""
    user = SimpleNamespace(id="u1", plan_code="pro")
    sub = SimpleNamespace(
        user_id="u1", plan_code="pro", status="active",
        cancelled_at=None, updated_at=None,
    )
    session = _FakeSession([[user], [sub]])

    async def fake_revoke(db, *, user_id, related_order_id, reason_code="refund_revoke"):
        return 0

    import credits_service
    monkeypatch.setattr(credits_service, "revoke_buckets_for_order", fake_revoke)

    await billing._recall_entitlements_for_refund(
        session, order=_order(target_plan_code="plus"),
        now=datetime.now(timezone.utc),
    )

    assert user.plan_code == "pro"
    assert sub.status == "active"
    assert session.added == []


@pytest.mark.asyncio
async def test_partial_refund_keeps_order_non_terminal_for_later_full_refund(monkeypatch):
    order = _order(status="paid", amount_cny=990)
    event = SimpleNamespace(processed=False, error_message=None, processed_at=None)
    session = _FakeSession([["evt-row-1"], [event], [order]])
    invoice_statuses: list[str] = []
    recalls: list[str] = []

    async def fake_record_invoice_for_order(db, *, order, settled_at, status):
        invoice_statuses.append(status)
        return SimpleNamespace(status=status)

    async def fake_recall_entitlements(db, *, order, now):
        recalls.append(order.id)

    monkeypatch.setattr(
        billing, "record_invoice_for_order", fake_record_invoice_for_order
    )
    monkeypatch.setattr(
        billing, "_recall_entitlements_for_refund", fake_recall_entitlements
    )

    settled = await billing._process_payment_event(
        db=session,
        provider="wechatpay",
        provider_event_id="evt-partial-refund",
        event_type="REFUND.SUCCESS",
        order_id=order.id,
        new_status="refunded",
        signature_valid=True,
        raw_payload={},
        refund_amount_fen=500,
    )

    assert settled is False
    assert order.status == "partial_refunded"
    assert invoice_statuses == ["partial_refunded"]
    assert recalls == []
    assert event.processed is True


# ---------------------------------------------------------------------------
# 接线守卫
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_paid_event_after_early_partial_refund_grants_remaining_entitlement(monkeypatch):
    order = _order(status="pending", amount_cny=990, paid_at=None)
    early_refund_event = SimpleNamespace(
        processed=False, error_message=None, processed_at=None
    )
    early_refund_session = _FakeSession([["evt-row-early"], [early_refund_event], [order]])
    invoice_statuses: list[str] = []
    recalls: list[str] = []

    async def fake_record_invoice_for_order(db, *, order, settled_at, status):
        invoice_statuses.append(status)
        return SimpleNamespace(status=status, subscription_id=None)

    async def fake_recall_entitlements(db, *, order, now):
        recalls.append(order.id)

    monkeypatch.setattr(
        billing, "record_invoice_for_order", fake_record_invoice_for_order
    )
    monkeypatch.setattr(
        billing, "_recall_entitlements_for_refund", fake_recall_entitlements
    )

    await billing._process_payment_event(
        db=early_refund_session,
        provider="wechatpay",
        provider_event_id="evt-early-partial-refund",
        event_type="REFUND.SUCCESS",
        order_id=order.id,
        new_status="refunded",
        signature_valid=True,
        raw_payload={},
        refund_amount_fen=500,
    )

    assert order.status == "partial_refunded"
    assert order.paid_at is None
    assert invoice_statuses == ["partial_refunded"]
    assert recalls == []

    user = SimpleNamespace(id="u1", plan_code="free")
    paid_event = SimpleNamespace(processed=False, error_message=None, processed_at=None)
    paid_session = _FakeSession([["evt-row-paid"], [paid_event], [order], [user]])
    upserts: list[str] = []

    async def fake_upsert_active_subscription(db, *, user, order, paid_at):
        upserts.append(order.id)
        return SimpleNamespace(id="sub-1", current_period_end=None)

    async def fake_ensure_subscription_bucket(*args, **kwargs):
        return None

    monkeypatch.setattr(
        billing, "upsert_active_subscription", fake_upsert_active_subscription
    )
    import credits_service
    monkeypatch.setattr(
        credits_service, "ensure_subscription_bucket", fake_ensure_subscription_bucket
    )

    settled = await billing._process_payment_event(
        db=paid_session,
        provider="wechatpay",
        provider_event_id="evt-paid-after-partial",
        event_type="TRANSACTION.SUCCESS",
        order_id=order.id,
        new_status="paid",
        signature_valid=True,
        raw_payload={},
    )

    assert settled is True
    assert order.status == "partial_refunded"
    assert order.paid_at is not None
    assert user.plan_code == "plus"
    assert upserts == [order.id]
    assert invoice_statuses == ["partial_refunded", "paid"]


@pytest.mark.asyncio
async def test_cumulative_partial_refunds_recall_when_total_reaches_order_amount(monkeypatch):
    order = _order(status="paid", amount_cny=990, metadata_json={})
    invoice_statuses: list[str] = []
    recalls: list[str] = []

    async def fake_record_invoice_for_order(db, *, order, settled_at, status):
        invoice_statuses.append(status)
        return SimpleNamespace(status=status)

    async def fake_recall_entitlements(db, *, order, now):
        recalls.append(order.id)

    monkeypatch.setattr(
        billing, "record_invoice_for_order", fake_record_invoice_for_order
    )
    monkeypatch.setattr(
        billing, "_recall_entitlements_for_refund", fake_recall_entitlements
    )

    first_event = SimpleNamespace(processed=False, error_message=None, processed_at=None)
    first_session = _FakeSession([["evt-row-first"], [first_event], [order]])
    await billing._process_payment_event(
        db=first_session,
        provider="wechatpay",
        provider_event_id="evt-refund-500",
        event_type="REFUND.SUCCESS",
        order_id=order.id,
        new_status="refunded",
        signature_valid=True,
        raw_payload={},
        refund_amount_fen=500,
    )

    assert order.status == "partial_refunded"
    assert order.metadata_json["refund_amount_fen_total"] == 500
    assert invoice_statuses == ["partial_refunded"]
    assert recalls == []

    second_event = SimpleNamespace(processed=False, error_message=None, processed_at=None)
    second_session = _FakeSession([["evt-row-second"], [second_event], [order]])
    await billing._process_payment_event(
        db=second_session,
        provider="wechatpay",
        provider_event_id="evt-refund-490",
        event_type="REFUND.SUCCESS",
        order_id=order.id,
        new_status="refunded",
        signature_valid=True,
        raw_payload={},
        refund_amount_fen=490,
    )

    assert order.status == "refunded"
    assert order.metadata_json["refund_amount_fen_total"] == 990
    assert invoice_statuses == ["partial_refunded", "refunded"]
    assert recalls == [order.id]


def test_receive_webhook_wires_refund_binding_and_amount():
    src = inspect.getsource(billing.receive_webhook)
    assert "_resolve_refund_order_id" in src, "退款绑定未接入 receive_webhook"
    assert "refund_amount_fen=" in src, "退款金额未传入 _process_payment_event"
    # INVARIANT：绑定只对 refunded 事件做
    assert 'event.new_status == "refunded"' in src


class _FakeRequest:
    def __init__(self, body: bytes):
        self._body = body
        self.headers = {"content-length": str(len(body))}

    async def body(self):
        return self._body


@pytest.mark.asyncio
async def test_receive_webhook_binds_and_skips_pending_refund_without_order_id(monkeypatch):
    raw_payload = {
        "event_type": "adjustment.created",
        "data": {
            "transaction_id": "txn_1",
            "action": "refund",
            "status": "pending_approval",
        },
    }
    event = SimpleNamespace(
        provider_event_id="evt_pending_refund",
        event_type="adjustment.created",
        order_id="",
        new_status="pending",
        raw_payload=raw_payload,
    )
    provider = SimpleNamespace(
        verify_signature=lambda raw_body, headers: True,
        parse_webhook=lambda raw_body: event,
    )
    resolve_calls: list[dict] = []

    async def fake_resolve(db, *, provider_name, raw_payload):
        resolve_calls.append({
            "provider_name": provider_name,
            "raw_payload": raw_payload,
        })
        return "ord-1"

    async def fake_validate(**kwargs):
        raise AssertionError("pending refund resources should skip provider validation")

    async def fake_process(**kwargs):
        raise AssertionError("pending refund resources must not process an order event")

    monkeypatch.setattr(billing, "get_provider", lambda name: provider)
    monkeypatch.setattr(billing, "_resolve_refund_order_id", fake_resolve)
    monkeypatch.setattr(billing, "_validate_paddle_event_against_order", fake_validate)
    monkeypatch.setattr(billing, "_process_payment_event", fake_process)

    response = await billing.receive_webhook(
        "paddle",
        _FakeRequest(b"{}"),
        _FakeSession([]),
    )

    assert response == {"ok": True, "settled": False}
    assert resolve_calls == [{
        "provider_name": "paddle",
        "raw_payload": raw_payload,
    }]


def test_process_payment_event_partial_refund_guard():
    src = inspect.getsource(billing._process_payment_event)
    assert "refund_amount_fen is not None" in src
    assert "refund_amount_fen < order.amount_cny" in src, (
        "部分退款守卫缺失：已知退款金额小于订单金额时不得自动回收权益"
    )
    assert "partial_refunded" in src
    assert "_recall_entitlements_for_refund" in src


def test_process_payment_event_locked_order_query_refreshes_identity_map():
    src = inspect.getsource(billing._process_payment_event)
    assert ".with_for_update()" in src
    assert "populate_existing=True" in src
