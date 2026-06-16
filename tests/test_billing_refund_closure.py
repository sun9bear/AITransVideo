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
from datetime import datetime, timezone
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
    ) == 990
    assert billing._extract_refund_amount_fen("wechatpay", {"transaction": {}}) is None
    assert billing._extract_refund_amount_fen(
        "paddle", {"data": {"totals": {"total": "abc"}}}
    ) is None
    assert billing._extract_refund_amount_fen("alipay", {}) is None


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
    session = _FakeSession([[user], [sub]])

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

def test_receive_webhook_wires_refund_binding_and_amount():
    src = inspect.getsource(billing.receive_webhook)
    assert "_resolve_refund_order_id" in src, "退款绑定未接入 receive_webhook"
    assert "refund_amount_fen=" in src, "退款金额未传入 _process_payment_event"
    # INVARIANT：绑定只对 refunded 事件做
    assert 'event.new_status == "refunded"' in src


def test_process_payment_event_partial_refund_guard():
    src = inspect.getsource(billing._process_payment_event)
    assert "refund_amount_fen is not None" in src
    assert "refund_amount_fen < order.amount_cny" in src, (
        "部分退款守卫缺失：已知退款金额小于订单金额时不得自动回收权益"
    )
    assert "partial_refunded" in src
    assert "_recall_entitlements_for_refund" in src
