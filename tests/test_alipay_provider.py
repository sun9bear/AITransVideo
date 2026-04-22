"""Tests for the live Alipay provider implementation."""
from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

import payment_provider_alipay as alipay_helper  # noqa: E402
from payment_provider_alipay import (  # noqa: E402
    AlipayConfig,
    build_checkout_url,
    detect_checkout_surface,
    format_amount_yuan,
    is_alipay_configured,
    is_alipay_live_ready,
    map_alipay_status,
    parse_alipay_notify,
    query_order_status,
    validate_alipay_notify_payload,
    validate_alipay_query_payload,
    verify_alipay_signature,
)
from payment_providers import AlipayProvider, CheckoutResult, NormalizedWebhookEvent  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def clean_alipay_env(monkeypatch):
    for var in (
        "AVT_ALIPAY_APP_ID",
        "AVT_ALIPAY_APP_PRIVATE_KEY",
        "AVT_ALIPAY_PUBLIC_KEY",
        "AVT_ALIPAY_NOTIFY_URL",
        "AVT_ALIPAY_RETURN_URL",
        "AVT_ALIPAY_GATEWAY_URL",
        "AVT_ALIPAY_SELLER_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def key_material():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return {"private_pem": private_pem, "public_pem": public_pem}


@pytest.fixture
def alipay_configured_env(monkeypatch, key_material):
    monkeypatch.setenv("AVT_ALIPAY_APP_ID", "test-app-id")
    monkeypatch.setenv("AVT_ALIPAY_APP_PRIVATE_KEY", key_material["private_pem"])
    monkeypatch.setenv("AVT_ALIPAY_PUBLIC_KEY", key_material["public_pem"])
    monkeypatch.setenv(
        "AVT_ALIPAY_NOTIFY_URL",
        "https://example.test/api/billing/webhooks/alipay",
    )
    monkeypatch.setenv(
        "AVT_ALIPAY_RETURN_URL",
        "https://example.test/settings/billing",
    )
    monkeypatch.setenv(
        "AVT_ALIPAY_GATEWAY_URL",
        "https://openapi.alipay.com/gateway.do",
    )
    monkeypatch.setenv("AVT_ALIPAY_SELLER_ID", "2088102177694100")
    return key_material


class TestStatusMapping:
    def test_known_statuses(self):
        assert map_alipay_status("TRADE_SUCCESS") == "paid"
        assert map_alipay_status("TRADE_FINISHED") == "paid"
        assert map_alipay_status("TRADE_CLOSED") == "cancelled"
        assert map_alipay_status("WAIT_BUYER_PAY") == "pending"

    def test_unknown_status_passthrough(self):
        assert map_alipay_status("SOMETHING_ELSE") == "SOMETHING_ELSE"


class TestOperationalGate:
    def test_non_operational_without_env(self, clean_alipay_env):
        assert is_alipay_configured() is False
        assert is_alipay_live_ready() is False
        assert AlipayProvider().operational is False

    def test_operational_with_complete_env(self, alipay_configured_env):
        assert is_alipay_configured() is True
        assert alipay_helper._ALIPAY_LIVE_READY is True
        assert is_alipay_live_ready() is True
        assert AlipayProvider().operational is True


class TestCheckoutSurface:
    def test_detects_mobile_user_agent(self):
        assert detect_checkout_surface(None, "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)") == "mobile_web"

    def test_defaults_to_pc(self):
        assert detect_checkout_surface(None, "Mozilla/5.0 (Windows NT 10.0; Win64; x64)") == "pc_web"

    def test_explicit_surface_wins(self):
        assert detect_checkout_surface("mobile_web", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)") == "mobile_web"


class TestCreateCheckout:
    def test_requires_config(self, clean_alipay_env):
        with pytest.raises(NotImplementedError):
            AlipayProvider().create_checkout(
                order_id="order-x",
                amount_cny=6900,
                target_plan_code="plus",
                billing_period="monthly",
            )

    def test_page_pay_checkout_is_signed(self, alipay_configured_env):
        provider = AlipayProvider()
        result = provider.create_checkout(
            order_id="order-xyz",
            amount_cny=6900,
            target_plan_code="plus",
            billing_period="monthly",
            checkout_surface="pc_web",
        )
        assert isinstance(result, CheckoutResult)
        assert result.provider_order_id is None

        parsed = urlparse(result.checkout_url)
        params = parse_qs(parsed.query)
        assert params["method"] == ["alipay.trade.page.pay"]
        assert params["sign_type"] == ["RSA2"]
        assert "sign" in params

        biz_content = json.loads(params["biz_content"][0])
        assert biz_content["out_trade_no"] == "order-xyz"
        assert biz_content["product_code"] == "FAST_INSTANT_TRADE_PAY"
        assert biz_content["total_amount"] == "69.00"

        signed_params = {
            key: values[0]
            for key, values in params.items()
            if key != "sign"
        }
        assert alipay_helper._verify_signature(
            alipay_configured_env["public_pem"],
            alipay_helper._canonicalize_params(signed_params),
            params["sign"][0],
        )

    def test_wap_checkout_uses_mobile_api(self, alipay_configured_env):
        config = AlipayConfig.from_env()
        assert config is not None

        url = build_checkout_url(
            config,
            order_id="order-mobile",
            amount_cny=19900,
            target_plan_code="pro",
            billing_period="annual",
            checkout_surface="mobile_web",
        )
        params = parse_qs(urlparse(url).query)
        biz_content = json.loads(params["biz_content"][0])
        assert params["method"] == ["alipay.trade.wap.pay"]
        assert biz_content["product_code"] == "QUICK_WAP_WAY"
        assert "quit_url" in biz_content

    def test_return_url_carries_order_context(self, alipay_configured_env):
        provider = AlipayProvider()
        result = provider.create_checkout(
            order_id="order-ctx",
            amount_cny=6900,
            target_plan_code="plus",
            billing_period="monthly",
        )
        params = parse_qs(urlparse(result.checkout_url).query)
        return_url = params["return_url"][0]
        return_params = parse_qs(urlparse(return_url).query)
        assert return_params["order_id"] == ["order-ctx"]
        assert return_params["provider"] == ["alipay"]
        assert return_params["status"] == ["processing"]

    def test_amount_formatter(self):
        assert format_amount_yuan(1) == "0.01"
        assert format_amount_yuan(6900) == "69.00"


class TestWebhookParsingAndVerification:
    def test_parse_form_notify(self):
        body = (
            b"notify_id=evt-1&out_trade_no=order-123"
            b"&trade_status=TRADE_SUCCESS&trade_no=trade-1"
        )
        parsed = parse_alipay_notify(body)
        assert parsed.provider_event_id == "evt-1"
        assert parsed.order_id == "order-123"
        assert parsed.trade_status == "TRADE_SUCCESS"

    def test_parse_json_notify_for_tests(self):
        body = json.dumps(
            {
                "notify_id": "evt-2",
                "out_trade_no": "order-456",
                "trade_status": "TRADE_CLOSED",
            }
        ).encode("utf-8")
        parsed = parse_alipay_notify(body)
        assert parsed.provider_event_id == "evt-2"
        assert parsed.order_id == "order-456"
        assert parsed.trade_status == "TRADE_CLOSED"

    def test_missing_order_id_raises(self):
        with pytest.raises(ValueError):
            parse_alipay_notify(b"trade_status=TRADE_SUCCESS")

    def test_signature_verification_passes_for_valid_notify(self, alipay_configured_env):
        config = AlipayConfig.from_env()
        assert config is not None

        payload = {
            "notify_id": "evt-3",
            "trade_no": "trade-3",
            "out_trade_no": "order-3",
            "trade_status": "TRADE_SUCCESS",
            "app_id": config.app_id,
            "total_amount": "69.00",
            "seller_id": "2088102177694100",
        }
        signed = {
            **payload,
            "sign_type": "RSA2",
        }
        signed["sign"] = alipay_helper._sign_with_private_key(
            alipay_configured_env["private_pem"],
            alipay_helper._canonicalize_params(payload),
        )
        body = urlencode(signed).encode("utf-8")
        assert verify_alipay_signature(config, body, {}) is True

    def test_signature_verification_fails_on_mutation(self, alipay_configured_env):
        config = AlipayConfig.from_env()
        assert config is not None
        payload = {
            "notify_id": "evt-4",
            "trade_no": "trade-4",
            "out_trade_no": "order-4",
            "trade_status": "TRADE_SUCCESS",
            "app_id": config.app_id,
            "total_amount": "69.00",
            "seller_id": "2088102177694100",
        }
        signed = {**payload, "sign_type": "RSA2"}
        signed["sign"] = alipay_helper._sign_with_private_key(
            alipay_configured_env["private_pem"],
            alipay_helper._canonicalize_params(payload),
        )
        signed["total_amount"] = "70.00"
        body = urlencode(signed).encode("utf-8")
        assert verify_alipay_signature(config, body, {}) is False

    def test_provider_parse_webhook_returns_normalized_event(self, alipay_configured_env):
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


class TestPayloadValidation:
    def test_validate_notify_payload(self, alipay_configured_env):
        config = AlipayConfig.from_env()
        assert config is not None
        payload = {
            "out_trade_no": "order-1",
            "app_id": config.app_id,
            "total_amount": "69.00",
            "seller_id": "2088102177694100",
        }
        validate_alipay_notify_payload(
            config,
            payload,
            order_id="order-1",
            amount_cny=6900,
        )

    def test_validate_notify_payload_rejects_mismatch(self, alipay_configured_env):
        config = AlipayConfig.from_env()
        assert config is not None
        payload = {
            "out_trade_no": "order-1",
            "app_id": config.app_id,
            "total_amount": "70.00",
            "seller_id": "2088102177694100",
        }
        with pytest.raises(ValueError):
            validate_alipay_notify_payload(
                config,
                payload,
                order_id="order-1",
                amount_cny=6900,
            )

    def test_validate_query_payload_does_not_require_app_id(self, alipay_configured_env):
        config = AlipayConfig.from_env()
        assert config is not None
        payload = {
            "out_trade_no": "order-1",
            "trade_no": "trade-1",
            "trade_status": "TRADE_SUCCESS",
            "total_amount": "69.00",
            "seller_id": "2088102177694100",
        }
        validate_alipay_query_payload(
            config,
            payload,
            order_id="order-1",
            amount_cny=6900,
        )


class TestQueryOrder:
    def test_query_order_status(self, monkeypatch, alipay_configured_env):
        captured = {}

        class DummyResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "alipay_trade_query_response": {
                        "code": "10000",
                        "out_trade_no": "order-1",
                        "trade_no": "trade-1",
                        "trade_status": "TRADE_SUCCESS",
                        "total_amount": "69.00",
                        "seller_id": "2088102177694100",
                    }
                }

        class DummyClient:
            def __init__(self, *args, **kwargs):
                del args, kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                del exc_type, exc, tb

            async def post(self, url, data):
                captured["url"] = url
                captured["data"] = data
                return DummyResponse()

        monkeypatch.setattr(alipay_helper.httpx, "AsyncClient", DummyClient)

        config = AlipayConfig.from_env()
        assert config is not None
        result = _run(query_order_status(config, order_id="order-1"))
        assert result is not None
        assert result.provider_order_id == "trade-1"
        assert result.provider_status == "TRADE_SUCCESS"
        assert captured["data"]["method"] == "alipay.trade.query"
        assert json.loads(captured["data"]["biz_content"]) == {"out_trade_no": "order-1"}
        assert "sign" in captured["data"]

    def test_query_order_status_returns_none_when_trade_missing(
        self, monkeypatch, alipay_configured_env
    ):
        class DummyResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "alipay_trade_query_response": {
                        "code": "40004",
                        "sub_code": "ACQ.TRADE_NOT_EXIST",
                    }
                }

        class DummyClient:
            def __init__(self, *args, **kwargs):
                del args, kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                del exc_type, exc, tb

            async def post(self, url, data):
                del url, data
                return DummyResponse()

        monkeypatch.setattr(alipay_helper.httpx, "AsyncClient", DummyClient)

        config = AlipayConfig.from_env()
        assert config is not None
        assert _run(query_order_status(config, order_id="order-1")) is None
