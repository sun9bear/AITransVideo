"""Tests for the Paddle Billing provider (plan 2026-06-08, P1e / §12).

Covers the Paddle-specific logic: signature verification (valid / tampered /
wrong-secret / stale-ts replay / missing header), event & transaction status
mapping, the 3-gate webhook payload validation, transaction query (pending ->
paid flip; 404 -> None), checkout transaction creation (incl. R11 empty
checkout.url), refund (adjustment.created) safety, price-drift guard, and the
PaddleProvider adapter. Provider-agnostic settlement + idempotency are covered
by tests/test_billing*.py (Paddle reuses _process_payment_event unchanged).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
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
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

import payment_provider_paddle as paddle  # noqa: E402
from payment_provider_paddle import (  # noqa: E402
    PaddleConfig,
    check_price_drift,
    create_transaction,
    is_paddle_enabled,
    is_paddle_live_ready,
    map_paddle_event_type,
    map_paddle_transaction_status,
    parse_paddle_webhook,
    query_transaction,
    validate_paddle_webhook_payload,
    verify_paddle_signature,
)
from payment_providers import (  # noqa: E402
    CheckoutResult,
    NormalizedWebhookEvent,
    PaddleProvider,
    ProviderOrderQueryResult,
)

_SECRET = "pdl_ntfset_testsecret"
_PRICE_ENV_VALUES = {
    "AVT_PADDLE_PRICE_PLUS_M": "pri_plus_m",
    "AVT_PADDLE_PRICE_PLUS_Q": "pri_plus_q",
    "AVT_PADDLE_PRICE_PLUS_A": "pri_plus_a",
    "AVT_PADDLE_PRICE_PRO_M": "pri_pro_m",
    "AVT_PADDLE_PRICE_PRO_Q": "pri_pro_q",
    "AVT_PADDLE_PRICE_PRO_A": "pri_pro_a",
}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _sig_headers(secret: str, ts, body: bytes) -> dict[str, str]:
    digest = hmac.new(
        secret.encode(), f"{ts}:".encode() + body, hashlib.sha256
    ).hexdigest()
    return {"paddle-signature": f"ts={ts};h1={digest}"}


@pytest.fixture
def clean_paddle_env(monkeypatch):
    for var in (
        "AVT_PADDLE_ENABLED",
        "AVT_PADDLE_ENV",
        "AVT_PADDLE_API_KEY",
        "AVT_PADDLE_WEBHOOK_SECRET",
        "AVT_PADDLE_NOTIFY_URL",
        "AVT_PADDLE_CLIENT_TOKEN",
        "AVT_PADDLE_SIGNATURE_MAX_AGE_S",
        *_PRICE_ENV_VALUES,
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def paddle_configured_env(monkeypatch, clean_paddle_env):
    monkeypatch.setenv("AVT_PADDLE_ENABLED", "1")
    monkeypatch.setenv("AVT_PADDLE_ENV", "sandbox")
    monkeypatch.setenv("AVT_PADDLE_API_KEY", "pdl_sdbx_apikey_x")
    monkeypatch.setenv("AVT_PADDLE_WEBHOOK_SECRET", _SECRET)
    for var, value in _PRICE_ENV_VALUES.items():
        monkeypatch.setenv(var, value)
    return PaddleConfig.from_env()


def _txn_data(order_id="ord_1", txn_id="txn_1", price_id="pri_plus_m", currency="CNY"):
    return {
        "id": txn_id,
        "custom_data": {"order_id": order_id},
        "items": [{"price": {"id": price_id}, "quantity": 1}],
        "details": {"totals": {"currency_code": currency, "grand_total": "10890"}},
    }


def _webhook_envelope(event_type="transaction.completed", event_id="evt_1", **kw):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": "2026-06-09T00:00:00Z",
        "data": _txn_data(**kw),
    }


# --- status mapping (event filtering: non-settlement -> pending) ---


class TestStatusMapping:
    def test_settlement_events(self):
        assert map_paddle_event_type("transaction.completed") == "paid"
        assert map_paddle_event_type("transaction.paid") == "paid"

    def test_refund_is_adjustment_not_transaction_refunded(self):
        # Paddle Billing models refunds as adjustments (R7 correction).
        assert map_paddle_event_type("adjustment.created") == "refunded"
        assert map_paddle_event_type("transaction.refunded") == "pending"  # Classic-ism, n/a

    def test_non_settlement_events_are_pending(self):
        for ev in ("transaction.created", "transaction.updated", "transaction.ready"):
            assert map_paddle_event_type(ev) == "pending"

    def test_failed_and_cancelled(self):
        assert map_paddle_event_type("transaction.payment_failed") == "failed"
        assert map_paddle_event_type("transaction.canceled") == "cancelled"

    def test_transaction_status_tokens(self):
        assert map_paddle_transaction_status("completed") == "paid"
        assert map_paddle_transaction_status("paid") == "paid"
        assert map_paddle_transaction_status("billed") == "pending"  # F-A: billed != paid
        assert map_paddle_transaction_status("canceled") == "cancelled"
        assert map_paddle_transaction_status("ready") == "pending"
        assert map_paddle_transaction_status("draft") == "pending"


# --- operational gate (graceful degrade) ---


class TestOperationalGate:
    def test_not_operational_without_env(self, clean_paddle_env):
        assert is_paddle_enabled() is False
        assert is_paddle_live_ready() is False
        assert PaddleProvider().operational is False

    def test_operational_with_complete_env(self, paddle_configured_env):
        assert is_paddle_enabled() is True
        assert is_paddle_live_ready() is True
        assert PaddleProvider().operational is True

    def test_not_operational_when_disabled_flag(self, monkeypatch, paddle_configured_env):
        monkeypatch.setenv("AVT_PADDLE_ENABLED", "0")
        assert is_paddle_live_ready() is False

    def test_not_operational_when_a_price_missing(self, monkeypatch, paddle_configured_env):
        monkeypatch.delenv("AVT_PADDLE_PRICE_PRO_A")
        assert is_paddle_live_ready() is False

    def test_not_operational_when_secret_missing(self, monkeypatch, paddle_configured_env):
        monkeypatch.delenv("AVT_PADDLE_WEBHOOK_SECRET")
        assert PaddleConfig.from_env() is None
        assert is_paddle_live_ready() is False


# --- signature verification (verify BEFORE parse) ---


class TestSignatureVerification:
    def test_valid_signature(self, paddle_configured_env):
        cfg = paddle_configured_env
        body = b'{"event_id":"evt_1"}'
        assert verify_paddle_signature(cfg, body, _sig_headers(_SECRET, 1700, body), now=1700) is True

    def test_within_tolerance(self, paddle_configured_env):
        cfg = paddle_configured_env
        body = b"{}"
        assert verify_paddle_signature(cfg, body, _sig_headers(_SECRET, 1700, body), now=1700 + 100) is True

    def test_stale_timestamp_rejected_replay(self, paddle_configured_env):
        cfg = paddle_configured_env
        body = b"{}"
        assert verify_paddle_signature(cfg, body, _sig_headers(_SECRET, 1700, body), now=1700 + 99999) is False

    def test_tampered_body_rejected(self, paddle_configured_env):
        cfg = paddle_configured_env
        headers = _sig_headers(_SECRET, 1700, b'{"amount":"99"}')
        assert verify_paddle_signature(cfg, b'{"amount":"1"}', headers, now=1700) is False

    def test_wrong_secret_rejected(self, paddle_configured_env):
        cfg = paddle_configured_env
        body = b"{}"
        assert verify_paddle_signature(cfg, body, _sig_headers("wrong", 1700, body), now=1700) is False

    def test_missing_or_malformed_header(self, paddle_configured_env):
        cfg = paddle_configured_env
        assert verify_paddle_signature(cfg, b"{}", {}, now=1700) is False
        assert verify_paddle_signature(cfg, b"{}", {"paddle-signature": "garbage"}, now=1700) is False

    def test_no_config_rejected(self):
        assert verify_paddle_signature(None, b"{}", {"paddle-signature": "ts=1;h1=x"}, now=1) is False

    def test_header_case_insensitive(self, paddle_configured_env):
        cfg = paddle_configured_env
        body = b"{}"
        h = _sig_headers(_SECRET, 1700, body)["paddle-signature"]
        assert verify_paddle_signature(cfg, body, {"Paddle-Signature": h}, now=1700) is True

    def test_multiple_h1_accepts_any_match_for_rotation(self, paddle_configured_env):
        # Paddle emits multiple h1 during webhook-secret rotation; any match
        # must pass, all-wrong must still fail (review F-D).
        cfg = paddle_configured_env
        body = b"{}"
        good = hmac.new(_SECRET.encode(), b"1700:" + body, hashlib.sha256).hexdigest()
        ok = {"paddle-signature": f"ts=1700;h1=deadbeef;h1={good}"}
        assert verify_paddle_signature(cfg, body, ok, now=1700) is True
        bad = {"paddle-signature": "ts=1700;h1=deadbeef;h1=cafebabe"}
        assert verify_paddle_signature(cfg, body, bad, now=1700) is False


# --- webhook parsing ---


class TestWebhookParsing:
    def test_parse_transaction_completed(self):
        body = json.dumps(_webhook_envelope()).encode()
        parsed = parse_paddle_webhook(body)
        assert parsed.provider_event_id == "evt_1"
        assert parsed.event_type == "transaction.completed"
        assert parsed.order_id == "ord_1"
        assert parsed.transaction_id == "txn_1"
        assert parsed.new_status == "paid"

    def test_parse_adjustment_has_no_order_id(self):
        # adjustment.created carries no custom_data.order_id -> order_id "".
        env = {"event_id": "evt_adj", "event_type": "adjustment.created",
               "data": {"id": "adj_1", "transaction_id": "txn_1", "action": "refund"}}
        parsed = parse_paddle_webhook(json.dumps(env).encode())
        assert parsed.order_id == ""
        assert parsed.new_status == "refunded"

    def test_empty_body_raises(self):
        with pytest.raises(ValueError):
            parse_paddle_webhook(b"")

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError):
            parse_paddle_webhook(b"not json{")


# --- 3-gate payload validation ---


class TestPayloadValidation:
    def test_valid_payload(self, paddle_configured_env):
        validate_paddle_webhook_payload(
            paddle_configured_env, _txn_data(), order_id="ord_1",
            target_plan_code="plus", billing_period="monthly", provider_order_id="txn_1",
        )

    def test_order_id_mismatch(self, paddle_configured_env):
        with pytest.raises(ValueError):
            validate_paddle_webhook_payload(
                paddle_configured_env, _txn_data(order_id="ord_OTHER"), order_id="ord_1",
                target_plan_code="plus", billing_period="monthly",
            )

    def test_transaction_id_mismatch(self, paddle_configured_env):
        with pytest.raises(ValueError):
            validate_paddle_webhook_payload(
                paddle_configured_env, _txn_data(txn_id="txn_OTHER"), order_id="ord_1",
                target_plan_code="plus", billing_period="monthly", provider_order_id="txn_1",
            )

    def test_currency_mismatch(self, paddle_configured_env):
        with pytest.raises(ValueError):
            validate_paddle_webhook_payload(
                paddle_configured_env, _txn_data(currency="USD"), order_id="ord_1",
                target_plan_code="plus", billing_period="monthly",
            )

    def test_price_id_mismatch(self, paddle_configured_env):
        # transaction carries plus_m price but order claims pro/monthly
        with pytest.raises(ValueError):
            validate_paddle_webhook_payload(
                paddle_configured_env, _txn_data(price_id="pri_plus_m"), order_id="ord_1",
                target_plan_code="pro", billing_period="monthly", provider_order_id="txn_1",
            )

    def test_no_config(self):
        with pytest.raises(ValueError):
            validate_paddle_webhook_payload(
                None, _txn_data(), order_id="ord_1",
                target_plan_code="plus", billing_period="monthly",
            )


# --- checkout transaction creation (sync httpx.Client) ---


class _SyncResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise paddle.httpx.HTTPStatusError("err", request=MagicMock(), response=MagicMock())

    def json(self):
        return self._payload


def _patch_sync_client(monkeypatch, payload, status_code=200, capture=None):
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            if capture is not None:
                capture["url"] = url
                capture["json"] = json
                capture["headers"] = headers
            return _SyncResp(payload, status_code)

        def get(self, url, headers=None):
            if capture is not None:
                capture["url"] = url
            return _SyncResp(payload, status_code)

    monkeypatch.setattr(paddle.httpx, "Client", _Client)


def _patch_price_ok(monkeypatch, config):
    """Make _fetch_price return a plan_catalog-matching price for any mapped id,
    so create_transaction's R4 drift check passes in POST-path tests."""
    from plan_catalog import get_price

    id_to_kp = {pid: kp for kp, pid in config.price_map.items()}

    def fake_fetch(cfg, price_id):
        kp = id_to_kp.get(price_id)
        if kp is None:
            return None
        plan, period = kp
        return {
            "status": "active",
            "billing_cycle": None,
            "unit_price": {
                "amount": str(get_price(plan, period)),
                "currency_code": "CNY",
            },
        }

    monkeypatch.setattr(paddle, "_fetch_price", fake_fetch)


class TestCreateTransaction:
    def test_returns_checkout_url_and_txn_id(self, monkeypatch, paddle_configured_env):
        _patch_price_ok(monkeypatch, paddle_configured_env)
        capture = {}
        _patch_sync_client(
            monkeypatch,
            {"data": {"id": "txn_99", "checkout": {"url": "https://aitrans.video/paddle-checkout?_ptxn=txn_99"}}},
            capture=capture,
        )
        url, txn = create_transaction(
            paddle_configured_env, order_id="ord_5", target_plan_code="plus", billing_period="monthly"
        )
        assert txn == "txn_99"
        assert "_ptxn=txn_99" in url
        # request binds price + our order id
        assert capture["json"]["items"] == [{"price_id": "pri_plus_m", "quantity": 1}]
        assert capture["json"]["custom_data"] == {"order_id": "ord_5"}
        assert capture["headers"]["Authorization"].startswith("Bearer ")

    def test_empty_checkout_url_raises_r11(self, monkeypatch, paddle_configured_env):
        _patch_price_ok(monkeypatch, paddle_configured_env)
        _patch_sync_client(monkeypatch, {"data": {"id": "txn_1", "checkout": {"url": ""}}})
        with pytest.raises(ValueError):
            create_transaction(
                paddle_configured_env, order_id="o", target_plan_code="plus", billing_period="monthly"
            )

    def test_unmapped_price_raises(self, paddle_configured_env):
        with pytest.raises(ValueError):
            create_transaction(
                paddle_configured_env, order_id="o", target_plan_code="free", billing_period="monthly"
            )

    def test_refuses_on_price_drift(self, monkeypatch, paddle_configured_env):
        # R4: a drifted amount on the mapped price must block checkout creation.
        monkeypatch.setattr(
            paddle,
            "_fetch_price",
            lambda cfg, pid: {
                "status": "active",
                "billing_cycle": None,
                "unit_price": {"amount": "1", "currency_code": "CNY"},
            },
        )
        with pytest.raises(ValueError):
            create_transaction(
                paddle_configured_env, order_id="o", target_plan_code="plus", billing_period="monthly"
            )


# --- customer resolution (email prefill on checkout) ---


def _patch_customers_client(monkeypatch, *, get_payloads, post_payload=None, post_status=200):
    """Client mock for the /customers find-or-create flow. ``get_payloads`` is
    consumed per GET call (last one repeats); POST always returns the same."""
    state = {"gets": 0}
    calls: list[tuple[str, dict | None]] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, params=None):
            idx = min(state["gets"], len(get_payloads) - 1)
            state["gets"] += 1
            calls.append(("get", params))
            return _SyncResp(get_payloads[idx])

        def post(self, url, headers=None, json=None):
            calls.append(("post", json))
            return _SyncResp(post_payload or {}, post_status)

    monkeypatch.setattr(paddle.httpx, "Client", _Client)
    return calls


class TestResolveCustomer:
    def test_lookup_hit_returns_existing_id_without_create(
        self, monkeypatch, paddle_configured_env
    ):
        calls = _patch_customers_client(
            monkeypatch,
            get_payloads=[{"data": [{"id": "ctm_1", "status": "active"}]}],
        )
        assert (
            paddle.resolve_customer_id(paddle_configured_env, "user@example.com")
            == "ctm_1"
        )
        assert all(method != "post" for method, _ in calls)

    def test_lookup_miss_creates_customer(self, monkeypatch, paddle_configured_env):
        calls = _patch_customers_client(
            monkeypatch,
            get_payloads=[{"data": []}],
            post_payload={"data": {"id": "ctm_new"}},
        )
        assert (
            paddle.resolve_customer_id(paddle_configured_env, "user@example.com")
            == "ctm_new"
        )
        post_bodies = [body for method, body in calls if method == "post"]
        assert post_bodies == [{"email": "user@example.com"}]

    def test_create_conflict_falls_back_to_second_lookup(
        self, monkeypatch, paddle_configured_env
    ):
        # Create race: another request created the customer between our GET
        # and POST. 409 must NOT raise — re-lookup instead.
        _patch_customers_client(
            monkeypatch,
            get_payloads=[{"data": []}, {"data": [{"id": "ctm_2", "status": "active"}]}],
            post_status=409,
        )
        assert (
            paddle.resolve_customer_id(paddle_configured_env, "user@example.com")
            == "ctm_2"
        )

    def test_archived_customer_not_used(self, monkeypatch, paddle_configured_env):
        # Archived ctm_ ids can be rejected on the transaction; degrade to no
        # prefill (None) rather than attach one.
        _patch_customers_client(
            monkeypatch,
            get_payloads=[{"data": [{"id": "ctm_a", "status": "archived"}]}],
            post_status=409,
        )
        assert (
            paddle.resolve_customer_id(paddle_configured_env, "user@example.com")
            is None
        )

    def test_email_normalized_to_lowercase(self, monkeypatch, paddle_configured_env):
        # Paddle dedupes emails case-insensitively but ?email= is exact-match:
        # without normalization a mixed-case account email misses the GET,
        # 409s on POST, and misses the retry GET — no prefill, every checkout.
        calls = _patch_customers_client(
            monkeypatch,
            get_payloads=[{"data": []}],
            post_payload={"data": {"id": "ctm_lc"}},
        )
        assert (
            paddle.resolve_customer_id(paddle_configured_env, " User@Example.COM ")
            == "ctm_lc"
        )
        assert [p for m, p in calls if m == "get"] == [{"email": "user@example.com"}]
        assert [b for m, b in calls if m == "post"] == [{"email": "user@example.com"}]

    @pytest.mark.parametrize(
        "email",
        ["", "no-at-sign", "has space@example.com", "中文@example.com"],
    )
    def test_paddle_unsafe_email_skips_network(
        self, monkeypatch, paddle_configured_env, email
    ):
        class _Boom:
            def __init__(self, *a, **k):
                raise AssertionError("network must not be touched for unsafe email")

        monkeypatch.setattr(paddle.httpx, "Client", _Boom)
        assert paddle.resolve_customer_id(paddle_configured_env, email) is None

    def test_safe_wrapper_swallows_errors(self, monkeypatch, paddle_configured_env):
        def boom(config, email):
            raise RuntimeError("paddle down")

        monkeypatch.setattr(paddle, "resolve_customer_id", boom)
        assert (
            paddle._resolve_customer_id_safe(paddle_configured_env, "u@x.com") is None
        )
        assert paddle._resolve_customer_id_safe(paddle_configured_env, None) is None


class TestCreateTransactionCustomerEmail:
    def test_customer_email_attached_as_customer_id(
        self, monkeypatch, paddle_configured_env
    ):
        _patch_price_ok(monkeypatch, paddle_configured_env)
        monkeypatch.setattr(
            paddle,
            "_resolve_customer_id_safe",
            lambda cfg, email: "ctm_9" if email == "u@x.com" else None,
        )
        capture = {}
        _patch_sync_client(
            monkeypatch,
            {"data": {"id": "txn_1", "checkout": {"url": "https://x/p?_ptxn=txn_1"}}},
            capture=capture,
        )
        create_transaction(
            paddle_configured_env,
            order_id="o",
            target_plan_code="plus",
            billing_period="monthly",
            customer_email="u@x.com",
        )
        assert capture["json"]["customer_id"] == "ctm_9"

    def test_resolve_failure_never_blocks_checkout(
        self, monkeypatch, paddle_configured_env
    ):
        # Email prefill is a UX nicety: resolution failure must degrade to a
        # customer-less transaction, never a failed checkout.
        _patch_price_ok(monkeypatch, paddle_configured_env)

        def boom(config, email):
            raise RuntimeError("customers api down")

        monkeypatch.setattr(paddle, "resolve_customer_id", boom)
        capture = {}
        _patch_sync_client(
            monkeypatch,
            {"data": {"id": "txn_2", "checkout": {"url": "https://x/p?_ptxn=txn_2"}}},
            capture=capture,
        )
        url, txn = create_transaction(
            paddle_configured_env,
            order_id="o",
            target_plan_code="plus",
            billing_period="monthly",
            customer_email="u@x.com",
        )
        assert txn == "txn_2"
        assert "customer_id" not in capture["json"]

    def test_no_email_means_no_customer_field(
        self, monkeypatch, paddle_configured_env
    ):
        _patch_price_ok(monkeypatch, paddle_configured_env)
        capture = {}
        _patch_sync_client(
            monkeypatch,
            {"data": {"id": "txn_3", "checkout": {"url": "https://x/p?_ptxn=txn_3"}}},
            capture=capture,
        )
        create_transaction(
            paddle_configured_env,
            order_id="o",
            target_plan_code="plus",
            billing_period="monthly",
        )
        assert "customer_id" not in capture["json"]


def test_provider_create_checkout_passes_customer_email(
    monkeypatch, paddle_configured_env
):
    captured = {}

    def fake_create_transaction(
        config, *, order_id, target_plan_code, billing_period, customer_email=None
    ):
        captured["customer_email"] = customer_email
        return ("https://x/p?_ptxn=t", "t")

    monkeypatch.setattr(paddle, "create_transaction", fake_create_transaction)
    result = PaddleProvider().create_checkout(
        order_id="o",
        amount_cny=9900,
        target_plan_code="plus",
        billing_period="monthly",
        customer_email="u@x.com",
    )
    assert captured["customer_email"] == "u@x.com"
    assert result.provider_order_id == "t"


# --- transaction query (async httpx.AsyncClient) ---


def _patch_async_client(monkeypatch, payload, status_code=200):
    code = status_code  # avoid shadowing inside the class body namespace

    class _Resp:
        def __init__(self):
            self.status_code = code

        def raise_for_status(self):
            if code >= 400:
                raise paddle.httpx.HTTPStatusError("e", request=MagicMock(), response=MagicMock())

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr(paddle.httpx, "AsyncClient", _Client)


class TestQueryTransaction:
    def test_pending_to_paid_flip(self, monkeypatch, paddle_configured_env):
        _patch_async_client(monkeypatch, {"data": {"id": "txn_1", "status": "completed"}})
        result = _run(query_transaction(paddle_configured_env, transaction_id="txn_1"))
        assert result is not None
        assert result.provider_status == "completed"
        assert map_paddle_transaction_status(result.provider_status) == "paid"

    def test_404_returns_none(self, monkeypatch, paddle_configured_env):
        _patch_async_client(monkeypatch, {}, status_code=404)
        assert _run(query_transaction(paddle_configured_env, transaction_id="txn_x")) is None

    def test_no_transaction_id_returns_none(self, paddle_configured_env):
        assert _run(query_transaction(paddle_configured_env, transaction_id="")) is None


# --- price drift guard (R4) ---


class TestPriceDrift:
    def test_no_drift_when_all_match(self, monkeypatch, paddle_configured_env):
        from plan_catalog import get_price

        def fake_fetch(config, price_id):
            # map id back to (plan,period) via the configured price_map
            for (plan, period), pid in paddle_configured_env.price_map.items():
                if pid == price_id:
                    return {
                        "status": "active",
                        "billing_cycle": None,
                        "unit_price": {"amount": str(get_price(plan, period)), "currency_code": "CNY"},
                    }
            return None

        monkeypatch.setattr(paddle, "_fetch_price", fake_fetch)
        assert check_price_drift(paddle_configured_env) == []

    def test_drift_detected_on_amount_currency_recurring(self, monkeypatch, paddle_configured_env):
        def fake_fetch(config, price_id):
            return {
                "status": "active",
                "billing_cycle": {"interval": "month", "frequency": 1},  # recurring (wrong)
                "unit_price": {"amount": "1", "currency_code": "USD"},  # wrong amount + currency
            }

        monkeypatch.setattr(paddle, "_fetch_price", fake_fetch)
        problems = check_price_drift(paddle_configured_env)
        assert len(problems) >= 6  # every mapped price flags issues
        joined = " ".join(problems)
        assert "currency" in joined and "recurring" in joined

    def test_no_config(self):
        assert check_price_drift(None) == ["paddle config missing"]


# --- PaddleProvider adapter ---


class TestPaddleProvider:
    def test_create_checkout_requires_config(self, clean_paddle_env):
        with pytest.raises(NotImplementedError):
            PaddleProvider().create_checkout(
                order_id="o", amount_cny=9900, target_plan_code="plus", billing_period="monthly"
            )

    def test_create_checkout_returns_result(self, monkeypatch, paddle_configured_env):
        _patch_price_ok(monkeypatch, paddle_configured_env)
        _patch_sync_client(
            monkeypatch,
            {"data": {"id": "txn_7", "checkout": {"url": "https://aitrans.video/paddle-checkout?_ptxn=txn_7"}}},
        )
        result = PaddleProvider().create_checkout(
            order_id="o", amount_cny=9900, target_plan_code="plus", billing_period="monthly"
        )
        assert isinstance(result, CheckoutResult)
        assert result.provider_order_id == "txn_7"

    def test_parse_webhook_returns_normalized(self, paddle_configured_env):
        event = PaddleProvider().parse_webhook(json.dumps(_webhook_envelope()).encode())
        assert isinstance(event, NormalizedWebhookEvent)
        assert event.provider_event_id == "evt_1"
        assert event.order_id == "ord_1"
        assert event.new_status == "paid"

    def test_query_order_none_without_provider_order_id(self, paddle_configured_env):
        assert _run(PaddleProvider().query_order(order_id="o", provider_order_id=None)) is None

    def test_query_order_returns_result(self, monkeypatch, paddle_configured_env):
        _patch_async_client(monkeypatch, {"data": {"id": "txn_1", "status": "completed"}})
        result = _run(PaddleProvider().query_order(order_id="o", provider_order_id="txn_1"))
        assert isinstance(result, ProviderOrderQueryResult)
        assert result.provider_order_id == "txn_1"
        assert result.provider_status == "completed"

    def test_provider_verify_signature(self, paddle_configured_env):
        body = b'{"event_id":"evt_now"}'
        ts = str(int(time.time()))
        assert PaddleProvider().verify_signature(body, _sig_headers(_SECRET, ts, body)) is True
