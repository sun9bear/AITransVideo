"""Unit tests for gateway/payment_provider_paypal.py (plan 2026-06-26).

Covers the USD-anchored fact gate (incl. the B1/B2 snapshot comparison and the
anti-underpayment lower bound), webhook parsing + capture-id idempotency key,
REVERSED→refunded (S4), and the HTTP flows (create/capture/verify/query) with a
mocked httpx client so no network is touched.
"""
from __future__ import annotations

import sys
import time
import types
from unittest.mock import MagicMock

import pytest

_gateway_dir = str(
    __import__("pathlib").Path(__file__).resolve().parent.parent / "gateway"
)
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
sys.modules.setdefault("database", _fake_database)

import payment_provider_paypal as p  # noqa: E402


def _config() -> p.PayPalConfig:
    return p.PayPalConfig(
        client_id="cid",
        secret="sec",
        api_base=p.SANDBOX_BASE,
        webhook_id="WH-1",
        return_url="https://aitrans.video/api/billing/paypal/return",
        cancel_url="https://aitrans.video/settings/billing?provider=paypal&status=cancelled",
    )


# --- pure helpers ------------------------------------------------------------


def test_usd_value_str():
    assert p._usd_value_str(1699) == "16.99"
    assert p._usd_value_str(4999) == "49.99"
    assert p._usd_value_str(15999) == "159.99"
    assert p._usd_value_str(100) == "1.00"


def test_amount_to_usd_cents():
    assert p._amount_to_usd_cents("16.99", "usd") == ("USD", 1699)
    assert p._amount_to_usd_cents("159.99", "USD") == ("USD", 15999)
    assert p._amount_to_usd_cents("bad", "USD") == ("USD", None)


def test_extract_related_capture_id():
    # supplementary_data path
    assert p._extract_related_capture_id(
        {"supplementary_data": {"related_ids": {"capture_id": "CAP-1"}}}
    ) == "CAP-1"
    # links rel=up path
    assert p._extract_related_capture_id(
        {"links": [{"rel": "up", "href": "https://api/v2/payments/captures/CAP-2"}]}
    ) == "CAP-2"
    # absent → empty (caller treats as unbound, never mis-settles)
    assert p._extract_related_capture_id({}) == ""
    assert p._extract_related_capture_id({"links": [{"rel": "self", "href": "x"}]}) == ""


def test_map_event_and_order_status():
    assert p.map_paypal_event_type("PAYMENT.CAPTURE.COMPLETED") == "paid"
    assert p.map_paypal_event_type("PAYMENT.CAPTURE.REFUNDED") == "refunded"
    assert p.map_paypal_event_type("PAYMENT.CAPTURE.REVERSED") == "refunded"  # S4
    assert p.map_paypal_event_type("PAYMENT.CAPTURE.DENIED") == "failed"
    assert p.map_paypal_event_type("CHECKOUT.ORDER.APPROVED") == "pending"
    assert p.map_paypal_order_status("COMPLETED") == "paid"
    assert p.map_paypal_order_status("APPROVED") == "approved"
    assert p.map_paypal_order_status("PAYER_ACTION_REQUIRED") == "pending"


# --- webhook parsing ---------------------------------------------------------


def test_parse_capture_webhook_uses_capture_id_as_event_id():
    raw = (
        b'{"id":"WH-xyz","event_type":"PAYMENT.CAPTURE.COMPLETED",'
        b'"resource":{"id":"CAP-1","status":"COMPLETED","custom_id":"order-123",'
        b'"amount":{"currency_code":"USD","value":"16.99"}}}'
    )
    parsed = p.parse_paypal_webhook(raw)
    # provider_event_id is the CAPTURE id, so return-path + webhook dedupe.
    assert parsed.provider_event_id == "CAP-1"
    assert parsed.event_type == "PAYMENT.CAPTURE.COMPLETED"
    assert parsed.order_id == "order-123"
    assert parsed.new_status == "paid"


def test_parse_reversed_webhook_is_refunded():
    raw = (
        b'{"id":"WH-2","event_type":"PAYMENT.CAPTURE.REVERSED",'
        b'"resource":{"id":"CAP-1","custom_id":"order-123",'
        b'"amount":{"currency_code":"USD","value":"16.99"}}}'
    )
    parsed = p.parse_paypal_webhook(raw)
    assert parsed.new_status == "refunded"
    assert parsed.order_id == "order-123"


def test_parse_empty_raises():
    with pytest.raises(ValueError):
        p.parse_paypal_webhook(b"")


# --- USD-anchored fact gate (B1/B2 + anti-underpayment) ----------------------


def _capture_resource(custom_id="order-123", currency="USD", value="16.99"):
    return {
        "id": "CAP-1",
        "custom_id": custom_id,
        "amount": {"currency_code": currency, "value": value},
    }


def test_validate_passes_on_exact_match():
    p.validate_paypal_webhook_payload(
        _config(), _capture_resource(), order_id="order-123", expected_usd_cents=1699
    )


def test_validate_custom_id_mismatch():
    with pytest.raises(ValueError, match="custom_id"):
        p.validate_paypal_webhook_payload(
            _config(), _capture_resource(custom_id="other"),
            order_id="order-123", expected_usd_cents=1699,
        )


def test_validate_currency_must_be_usd():
    with pytest.raises(ValueError, match="currency"):
        p.validate_paypal_webhook_payload(
            _config(), _capture_resource(currency="CNY"),
            order_id="order-123", expected_usd_cents=1699,
        )


def test_validate_rejects_underpayment():
    # captured $16.98 < expected $16.99 — the underpayment hole must be closed.
    with pytest.raises(ValueError, match="underpayment"):
        p.validate_paypal_webhook_payload(
            _config(), _capture_resource(value="16.98"),
            order_id="order-123", expected_usd_cents=1699,
        )


def test_validate_rejects_overpayment_beyond_tolerance():
    with pytest.raises(ValueError, match="overpayment"):
        p.validate_paypal_webhook_payload(
            _config(), _capture_resource(value="17.99"),
            order_id="order-123", expected_usd_cents=1699,
        )


def test_validate_requires_snapshot():
    with pytest.raises(ValueError, match="expected_usd_cents"):
        p.validate_paypal_webhook_payload(
            _config(), _capture_resource(),
            order_id="order-123", expected_usd_cents=None,
        )


# --- HTTP flows (mocked httpx) -----------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _FakeClient:
    def __init__(self, handler):
        self._handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, **kwargs):
        return self._handler("POST", url, kwargs)

    def get(self, url, **kwargs):
        return self._handler("GET", url, kwargs)


@pytest.fixture
def mock_http(monkeypatch):
    """Patch httpx.Client with a URL-dispatching fake; pre-seed the OAuth token."""
    cfg = _config()
    p._token_cache[(cfg.api_base, cfg.client_id)] = ("tok", time.monotonic() + 3600)

    def install(handler):
        monkeypatch.setattr(p.httpx, "Client", lambda *a, **k: _FakeClient(handler))

    yield install
    p._token_cache.clear()


def test_create_order_returns_payer_action_and_usd(mock_http):
    def handler(method, url, kwargs):
        assert method == "POST" and url.endswith("/v2/checkout/orders")
        body = kwargs["json"]
        assert body["intent"] == "CAPTURE"
        unit = body["purchase_units"][0]
        assert unit["custom_id"] == "order-123"
        assert unit["amount"] == {"currency_code": "USD", "value": "16.99"}
        ctx = body["payment_source"]["paypal"]["experience_context"]
        assert "order_id=order-123" in ctx["return_url"]
        assert ctx["user_action"] == "PAY_NOW"
        return _FakeResponse(201, {
            "id": "PAY-1",
            "status": "PAYER_ACTION_REQUIRED",
            "links": [
                {"rel": "self", "href": "https://api/self"},
                {"rel": "payer-action", "href": "https://paypal/approve?token=PAY-1"},
            ],
        })

    mock_http(handler)
    url, order_id, cents = p.create_order(
        _config(), order_id="order-123", target_plan_code="plus", billing_period="monthly"
    )
    assert url == "https://paypal/approve?token=PAY-1"
    assert order_id == "PAY-1"
    assert cents == 1699  # the snapshot stamped onto our order (B2)


def test_create_order_raises_without_payer_action_link(mock_http):
    def handler(method, url, kwargs):
        return _FakeResponse(201, {"id": "PAY-1", "status": "CREATED", "links": []})

    mock_http(handler)
    with pytest.raises(ValueError, match="payer-action"):
        p.create_order(
            _config(), order_id="o1", target_plan_code="plus", billing_period="monthly"
        )


def test_capture_order_extracts_capture(mock_http):
    def handler(method, url, kwargs):
        assert url.endswith("/v2/checkout/orders/PAY-1/capture")
        assert kwargs["headers"]["PayPal-Request-Id"] == "capture-order-123"
        return _FakeResponse(201, {
            "id": "PAY-1",
            "status": "COMPLETED",
            "purchase_units": [{
                "payments": {"captures": [{
                    "id": "CAP-1",
                    "status": "COMPLETED",
                    "custom_id": "order-123",
                    "amount": {"currency_code": "USD", "value": "16.99"},
                }]},
            }],
        })

    mock_http(handler)
    res = p.capture_order(_config(), paypal_order_id="PAY-1", order_id="order-123")
    assert res is not None
    assert res.capture_id == "CAP-1"
    assert res.capture_status == "COMPLETED"
    assert res.amount_usd_cents == 1699
    assert res.custom_id == "order-123"
    # the capture object feeds the fact-gate unchanged
    p.validate_paypal_webhook_payload(
        _config(), res.resource, order_id="order-123", expected_usd_cents=1699
    )


def test_verify_signature_success_and_failure(mock_http):
    headers = {
        "paypal-transmission-id": "t",
        "paypal-transmission-time": "2026-06-26T00:00:00Z",
        "paypal-transmission-sig": "s",
        "paypal-cert-url": "https://api.paypal.com/cert",
        "paypal-auth-algo": "SHA256withRSA",
    }
    raw = b'{"id":"WH-1","event_type":"PAYMENT.CAPTURE.COMPLETED"}'

    mock_http(lambda m, u, k: _FakeResponse(200, {"verification_status": "SUCCESS"}))
    assert p.verify_paypal_signature(_config(), raw, headers) is True

    mock_http(lambda m, u, k: _FakeResponse(200, {"verification_status": "FAILURE"}))
    assert p.verify_paypal_signature(_config(), raw, headers) is False


def test_verify_signature_missing_headers_is_false():
    # No HTTP at all — missing transmission headers fail closed.
    assert p.verify_paypal_signature(_config(), b"{}", {}) is False


def test_verify_signature_none_config_is_false():
    assert p.verify_paypal_signature(None, b"{}", {}) is False


def test_query_order_reads_status(mock_http):
    def handler(method, url, kwargs):
        assert method == "GET" and url.endswith("/v2/checkout/orders/PAY-1")
        return _FakeResponse(200, {
            "id": "PAY-1",
            "status": "APPROVED",
            "purchase_units": [{"payments": {"captures": []}}],
        })

    mock_http(handler)
    res = p.query_order(_config(), paypal_order_id="PAY-1")
    assert res is not None
    assert res.order_status == "APPROVED"
    assert p.map_paypal_order_status(res.order_status) == "approved"
