"""Tests for the Task 5 Alipay integration boundary.

Coverage:
1. Status mapping for known + unknown Alipay `trade_status` values.
2. `operational` gating when config is absent vs present.
3. `create_checkout` contract shape without a live network call, for both
   configured and unconfigured states.
4. `parse_webhook` / `parse_alipay_notify` contract for representative
   form-encoded and JSON payloads.
5. `verify_signature` behavior on the non-configured path (fails closed).

None of these tests require real Alipay credentials, a network connection,
or a cryptography library.
"""
from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

import pytest
from urllib.parse import parse_qs, urlparse


_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

import payment_provider_alipay as alipay_helper  # noqa: E402
import payment_providers  # noqa: E402
from payment_provider_alipay import (  # noqa: E402
    AlipayConfig,
    build_checkout_url,
    is_alipay_configured,
    is_alipay_live_ready,
    map_alipay_status,
    parse_alipay_notify,
    verify_alipay_signature,
)
from payment_providers import (  # noqa: E402
    AlipayProvider,
    CheckoutResult,
    NormalizedWebhookEvent,
    get_provider,
    is_provider_operational,
    list_providers,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ALL_ALIPAY_ENV_VARS = (
    "AVT_ALIPAY_APP_ID",
    "AVT_ALIPAY_APP_PRIVATE_KEY",
    "AVT_ALIPAY_PUBLIC_KEY",
    "AVT_ALIPAY_NOTIFY_URL",
    "AVT_ALIPAY_RETURN_URL",
    "AVT_ALIPAY_GATEWAY_URL",
)


@pytest.fixture
def clean_alipay_env(monkeypatch):
    """Remove every AVT_ALIPAY_* env var for the duration of the test."""
    for var in _ALL_ALIPAY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def alipay_configured_env(monkeypatch):
    """Set the minimum viable AVT_ALIPAY_* env vars."""
    monkeypatch.setenv("AVT_ALIPAY_APP_ID", "test-app-id")
    monkeypatch.setenv("AVT_ALIPAY_APP_PRIVATE_KEY", "test-private-key")
    monkeypatch.setenv("AVT_ALIPAY_PUBLIC_KEY", "test-alipay-public-key")
    monkeypatch.setenv(
        "AVT_ALIPAY_NOTIFY_URL", "https://example.test/api/billing/webhooks/alipay"
    )
    monkeypatch.setenv(
        "AVT_ALIPAY_RETURN_URL", "https://example.test/settings/billing?paid=1"
    )
    monkeypatch.setenv(
        "AVT_ALIPAY_GATEWAY_URL",
        "https://openapi-sandbox.dl.alipaydev.com/gateway.do",
    )
    yield


# ---------------------------------------------------------------------------
# 1. Status mapping
# ---------------------------------------------------------------------------


class TestStatusMapping:
    def test_trade_success_maps_to_paid(self):
        assert map_alipay_status("TRADE_SUCCESS") == "paid"

    def test_trade_finished_maps_to_paid(self):
        assert map_alipay_status("TRADE_FINISHED") == "paid"

    def test_trade_closed_maps_to_cancelled(self):
        assert map_alipay_status("TRADE_CLOSED") == "cancelled"

    def test_wait_buyer_pay_maps_to_pending(self):
        assert map_alipay_status("WAIT_BUYER_PAY") == "pending"

    def test_unknown_status_passes_through(self):
        assert map_alipay_status("SOMETHING_ELSE") == "SOMETHING_ELSE"

    def test_provider_instance_map_status_matches_helper(self, clean_alipay_env):
        provider = AlipayProvider()
        assert provider.map_status("TRADE_SUCCESS") == "paid"
        assert provider.map_status("TRADE_CLOSED") == "cancelled"


# ---------------------------------------------------------------------------
# 2. Operational / non-operational gating
# ---------------------------------------------------------------------------


class TestOperationalGate:
    """Alipay truthfulness gate (T5 minor revision).

    Env presence alone is NOT sufficient to report `operational = True`.
    The `_ALIPAY_LIVE_READY` flag in `payment_provider_alipay` must also be
    True, which happens only after the signed-checkout path and the verified-
    signature path are genuinely implemented. Until then the provider stays
    visible but non-operational, and checkout-config will not default users
    into it.
    """

    def test_non_operational_without_env(self, clean_alipay_env):
        assert is_alipay_configured() is False
        assert is_alipay_live_ready() is False
        provider = AlipayProvider()
        assert provider.operational is False

    def test_non_operational_with_partial_env(self, monkeypatch, clean_alipay_env):
        # Only one of the four required vars set.
        monkeypatch.setenv("AVT_ALIPAY_APP_ID", "x")
        assert AlipayConfig.from_env() is None
        assert is_alipay_live_ready() is False
        provider = AlipayProvider()
        assert provider.operational is False

    def test_env_alone_is_not_enough(self, alipay_configured_env):
        """Regression: a configured env MUST NOT flip Alipay to operational.

        The `_ALIPAY_LIVE_READY` module flag ships as False until the signed-
        checkout and signature-verification paths are actually implemented.
        This is the whole point of the T5 minor revision — env presence alone
        used to make Alipay the default provider even though the helper still
        returned unsigned URLs and fail-closed signatures.
        """
        assert is_alipay_configured() is True
        # But live-ready is False because the code-level flag hasn't flipped.
        assert alipay_helper._ALIPAY_LIVE_READY is False
        assert is_alipay_live_ready() is False
        provider = AlipayProvider()
        assert provider.operational is False

    def test_flipped_flag_plus_env_makes_it_operational(
        self, monkeypatch, alipay_configured_env
    ):
        """When the live-ready flag IS flipped AND env is present, the
        provider reports operational = True. This test exercises the path that
        unlocks once the real signing code ships, without requiring a file edit
        to prove the gate has both inputs wired correctly.
        """
        monkeypatch.setattr(alipay_helper, "_ALIPAY_LIVE_READY", True)
        assert is_alipay_live_ready() is True
        provider = AlipayProvider()
        assert provider.operational is True

    def test_flipped_flag_without_env_still_not_operational(
        self, monkeypatch, clean_alipay_env
    ):
        """Even with the live-ready flag True, missing env means not ready."""
        monkeypatch.setattr(alipay_helper, "_ALIPAY_LIVE_READY", True)
        assert is_alipay_live_ready() is False
        provider = AlipayProvider()
        assert provider.operational is False

    def test_registry_is_provider_operational_reflects_gate(
        self, monkeypatch, clean_alipay_env
    ):
        # Force the registry to rebuild so a fresh AlipayProvider is used.
        payment_providers._PROVIDERS = {}
        assert is_provider_operational("alipay") is False

    def test_registry_stays_non_operational_with_env_until_flag_flips(
        self, monkeypatch, alipay_configured_env
    ):
        """Registry-level view matches the updated gate: env alone is not
        enough, both env AND the code-level live-ready flag must agree.
        """
        payment_providers._PROVIDERS = {}
        # Env is set (alipay_configured_env fixture), but flag is still False.
        assert is_provider_operational("alipay") is False

        # Flip the flag → now it reports operational.
        monkeypatch.setattr(alipay_helper, "_ALIPAY_LIVE_READY", True)
        payment_providers._PROVIDERS = {}
        assert is_provider_operational("alipay") is True

        # And the registry still lists fake + alipay side-by-side.
        names = set(list_providers())
        assert "fake" in names
        assert "alipay" in names

    def test_fake_remains_default_safe_path_when_alipay_missing(
        self, clean_alipay_env, monkeypatch
    ):
        payment_providers._PROVIDERS = {}
        assert is_provider_operational("fake") is True
        assert is_provider_operational("alipay") is False

    def test_module_flag_ships_as_false_by_default(self):
        """Guard: the code-level live-ready flag must ship as False.

        If this assertion ever fails, someone edited the flag without also
        completing the signed-checkout + verified-signature implementations.
        That's the T5 minor revision's single invariant, so catch it here.
        """
        assert alipay_helper._ALIPAY_LIVE_READY is False


# ---------------------------------------------------------------------------
# 3. create_checkout contract (no live network)
# ---------------------------------------------------------------------------


class TestCreateCheckout:
    def test_non_operational_raises_not_implemented(self, clean_alipay_env):
        provider = AlipayProvider()
        with pytest.raises(NotImplementedError):
            provider.create_checkout(
                order_id="order-x",
                amount_cny=6900,
                target_plan_code="plus",
                billing_period="monthly",
            )

    def test_configured_returns_checkout_result_shape(self, alipay_configured_env):
        provider = AlipayProvider()
        result = provider.create_checkout(
            order_id="order-xyz",
            amount_cny=6900,
            target_plan_code="plus",
            billing_period="monthly",
        )
        assert isinstance(result, CheckoutResult)
        assert result.provider_order_id is None  # assigned on notify, not here
        assert result.checkout_url.startswith(
            "https://openapi-sandbox.dl.alipaydev.com/gateway.do?"
        )
        # Query string must include our order id as out_trade_no and the
        # yuan amount (6900 fen → 69.00 yuan).
        parsed = urlparse(result.checkout_url)
        params = parse_qs(parsed.query)
        assert params["out_trade_no"] == ["order-xyz"]
        assert params["total_amount"] == ["69.00"]
        assert params["app_id"] == ["test-app-id"]
        assert params["method"] == ["alipay.trade.page.pay"]
        # Must include both notify_url and return_url from config.
        assert "notify_url" in params and "return_url" in params

    def test_build_checkout_url_yuan_formatting(self, alipay_configured_env):
        config = AlipayConfig.from_env()
        assert config is not None
        url = build_checkout_url(
            config,
            order_id="o-1",
            amount_cny=29900,
            target_plan_code="pro",
            billing_period="monthly",
        )
        params = parse_qs(urlparse(url).query)
        assert params["total_amount"] == ["299.00"]


# ---------------------------------------------------------------------------
# 4. Webhook parsing contract
# ---------------------------------------------------------------------------


class TestParseWebhook:
    def test_form_encoded_payload(self):
        body = (
            b"notify_id=2024abc&out_trade_no=order-123"
            b"&trade_status=TRADE_SUCCESS&trade_no=2024041922001xxxxxx"
        )
        parsed = parse_alipay_notify(body)
        assert parsed.order_id == "order-123"
        assert parsed.trade_status == "TRADE_SUCCESS"
        assert parsed.provider_event_id == "2024abc"

    def test_json_payload_accepted_for_tests(self):
        import json

        body = json.dumps(
            {
                "notify_id": "evt-9",
                "out_trade_no": "order-456",
                "trade_status": "TRADE_CLOSED",
                "trade_no": "alipay-tn-1",
            }
        ).encode("utf-8")
        parsed = parse_alipay_notify(body)
        assert parsed.order_id == "order-456"
        assert parsed.trade_status == "TRADE_CLOSED"
        assert parsed.provider_event_id == "evt-9"

    def test_missing_out_trade_no_raises(self):
        body = b"trade_status=TRADE_SUCCESS"
        with pytest.raises(ValueError):
            parse_alipay_notify(body)

    def test_empty_body_raises(self):
        with pytest.raises(ValueError):
            parse_alipay_notify(b"")

    def test_invalid_json_raises(self):
        body = b"{not: json"
        with pytest.raises(ValueError):
            parse_alipay_notify(body)

    def test_fallback_provider_event_id_when_notify_id_missing(self):
        # If notify_id is absent but trade_no is present, use trade_no as the
        # provider event id so webhook dedup still has a stable key.
        body = b"out_trade_no=order-77&trade_status=TRADE_SUCCESS&trade_no=tn-xyz"
        parsed = parse_alipay_notify(body)
        assert parsed.provider_event_id == "tn-xyz"

    def test_provider_parse_webhook_returns_normalized_event(
        self, alipay_configured_env
    ):
        provider = AlipayProvider()
        body = (
            b"notify_id=evt-ok&out_trade_no=order-123"
            b"&trade_status=TRADE_SUCCESS&trade_no=tn-1"
        )
        event = provider.parse_webhook(body)
        assert isinstance(event, NormalizedWebhookEvent)
        assert event.provider_event_id == "evt-ok"
        assert event.order_id == "order-123"
        assert event.new_status == "paid"
        assert event.event_type == "payment.success"

    def test_provider_parse_webhook_cancelled_maps_event_type(
        self, alipay_configured_env
    ):
        provider = AlipayProvider()
        body = b"notify_id=evt-cx&out_trade_no=order-42&trade_status=TRADE_CLOSED"
        event = provider.parse_webhook(body)
        assert event.new_status == "cancelled"
        assert event.event_type == "payment.cancelled"


# ---------------------------------------------------------------------------
# 5. Signature verification (fail-closed contract)
# ---------------------------------------------------------------------------


class TestSignatureVerification:
    def test_fails_closed_without_config(self, clean_alipay_env):
        assert verify_alipay_signature(None, b"anything", {}) is False

    def test_fails_closed_even_when_config_present_until_rsa_lands(
        self, alipay_configured_env
    ):
        """Task 5 deliberately keeps RSA2 verification unimplemented.

        Until a live signing helper lands, `verify_alipay_signature` must
        return False even if config is fully populated. The settlement layer
        treats `signature_valid=False` as "record but don't settle", which is
        the safest default while the path is still a stub.
        """
        config = AlipayConfig.from_env()
        assert config is not None
        assert verify_alipay_signature(config, b"anything", {}) is False

    def test_provider_verify_signature_matches_helper(self, clean_alipay_env):
        provider = AlipayProvider()
        assert provider.verify_signature(b"x", {}) is False
