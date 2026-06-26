"""PayPal billing integration (plan 2026-06-26): fact-gate, refund routing,
and the return endpoint (B1/B3/S4). DB is stubbed like test_billing.py.
"""
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_gateway_dir = str(
    __import__("pathlib").Path(__file__).resolve().parent.parent / "gateway"
)
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
_fake_database.init_db = MagicMock()
sys.modules.setdefault("database", _fake_database)

import billing  # noqa: E402
from billing import (  # noqa: E402
    _extract_refund_amount_fen,
    _is_refund_resource_event,
    _resolve_refund_order_id,
    _validate_paypal_event_against_order,
    paypal_return,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _paypal_env(monkeypatch):
    monkeypatch.setenv("AVT_PAYPAL_ENABLED", "true")
    monkeypatch.setenv("AVT_PAYPAL_CLIENT_ID", "cid")
    monkeypatch.setenv("AVT_PAYPAL_SECRET", "sec")
    monkeypatch.setenv("AVT_PAYPAL_WEBHOOK_ID", "WH-1")


def _order(status="pending", expected_usd_cents=1699, provider="paypal"):
    return SimpleNamespace(
        id="order-1",
        user_id="uid-1",
        provider=provider,
        provider_order_id="PAY-1",
        target_plan_code="plus",
        billing_period="monthly",
        amount_cny=9900,
        currency="CNY",
        status=status,
        metadata_json={"paypal_expected_usd_cents": expected_usd_cents},
        paid_at=None,
        updated_at=None,
    )


def _db_returning(order):
    db = AsyncMock()
    res = MagicMock()
    res.scalar_one_or_none.return_value = order
    db.execute = AsyncMock(return_value=res)
    db.commit = AsyncMock()
    return db


def _capture_envelope(event_type="PAYMENT.CAPTURE.COMPLETED", custom_id="order-1",
                      value="16.99", currency="USD"):
    return {
        "event_type": event_type,
        "resource": {
            "id": "CAP-1",
            "custom_id": custom_id,
            "amount": {"currency_code": currency, "value": value},
        },
    }


# --- refund routing ----------------------------------------------------------


def test_is_refund_resource_event_paypal():
    assert _is_refund_resource_event("paypal", "PAYMENT.CAPTURE.REFUNDED", {}) is True
    assert _is_refund_resource_event("paypal", "PAYMENT.CAPTURE.REVERSED", {}) is True  # S4
    assert _is_refund_resource_event("paypal", "PAYMENT.CAPTURE.COMPLETED", {}) is False


def test_extract_refund_amount_fen_paypal_is_none():
    # B1: PayPal refunds are USD — must NOT feed a value into the CNY-fen
    # partial-refund comparison. None → treated as full → clawback fires.
    env = _capture_envelope(event_type="PAYMENT.CAPTURE.REFUNDED")
    assert _extract_refund_amount_fen("paypal", env) is None


def test_resolve_refund_order_id_paypal_uses_custom_id():
    env = _capture_envelope(event_type="PAYMENT.CAPTURE.REFUNDED", custom_id="order-1")
    got = _run(_resolve_refund_order_id(AsyncMock(), provider_name="paypal", raw_payload=env))
    assert got == "order-1"


# --- fact gate ---------------------------------------------------------------


def test_validate_capture_passes_on_exact_usd():
    db = _db_returning(_order())
    ok = _run(_validate_paypal_event_against_order(
        db=db, order_id="order-1", payload=_capture_envelope()))
    assert ok is True


def test_validate_capture_rejects_custom_id_mismatch():
    db = _db_returning(_order())
    ok = _run(_validate_paypal_event_against_order(
        db=db, order_id="order-1", payload=_capture_envelope(custom_id="evil")))
    assert ok is False


def test_validate_capture_rejects_underpayment():
    db = _db_returning(_order(expected_usd_cents=1699))
    ok = _run(_validate_paypal_event_against_order(
        db=db, order_id="order-1", payload=_capture_envelope(value="16.98")))
    assert ok is False


def test_validate_refund_binds_currency_only_not_amount():
    # A refund's amount is the refund amount, not the original — gate on bind +
    # currency, NOT amount == snapshot (else every partial refund false-rejects).
    db = _db_returning(_order())
    env = _capture_envelope(event_type="PAYMENT.CAPTURE.REFUNDED", value="5.00")
    ok = _run(_validate_paypal_event_against_order(db=db, order_id="order-1", payload=env))
    assert ok is True


def test_validate_refund_rejects_non_usd():
    db = _db_returning(_order())
    env = _capture_envelope(event_type="PAYMENT.CAPTURE.REFUNDED", currency="EUR")
    ok = _run(_validate_paypal_event_against_order(db=db, order_id="order-1", payload=env))
    assert ok is False


def test_validate_orderless_event_is_safe_ack():
    db = _db_returning(None)  # order not found
    ok = _run(_validate_paypal_event_against_order(
        db=db, order_id="missing", payload=_capture_envelope()))
    assert ok is True  # recorded + no-op downstream


# --- return endpoint (B3) ----------------------------------------------------


def _location(resp):
    return resp.headers["location"]


def test_paypal_return_missing_order_id_errors():
    resp = _run(paypal_return(order_id="", db=AsyncMock()))
    assert resp.status_code == 303
    assert "status=error" in _location(resp)


def test_paypal_return_unknown_order_errors():
    resp = _run(paypal_return(order_id="nope", db=_db_returning(None)))
    assert "status=error" in _location(resp)


def test_paypal_return_non_paypal_order_errors():
    resp = _run(paypal_return(order_id="order-1", db=_db_returning(_order(provider="paddle"))))
    assert "status=error" in _location(resp)


def test_paypal_return_already_settled():
    resp = _run(paypal_return(order_id="order-1", db=_db_returning(_order(status="paid"))))
    assert "status=already_settled" in _location(resp)


def test_paypal_return_pending_triggers_capture(monkeypatch):
    refresh = AsyncMock()
    monkeypatch.setattr(billing, "_refresh_paypal_order", refresh)
    resp = _run(paypal_return(order_id="order-1", db=_db_returning(_order(status="pending"))))
    assert resp.status_code == 303
    assert "status=processing" in _location(resp)
    assert "order_id=order-1" in _location(resp)
    refresh.assert_awaited_once()


def test_paypal_return_capture_error_still_redirects(monkeypatch):
    # B3: never raise to the browser — the webhook/sweeper are the backstops.
    boom = AsyncMock(side_effect=RuntimeError("paypal down"))
    monkeypatch.setattr(billing, "_refresh_paypal_order", boom)
    resp = _run(paypal_return(order_id="order-1", db=_db_returning(_order(status="pending"))))
    assert resp.status_code == 303
    assert "status=processing" in _location(resp)
