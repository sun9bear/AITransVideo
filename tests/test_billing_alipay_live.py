"""Focused tests for the live Alipay billing path."""
from __future__ import annotations

import asyncio
import sys
import types
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

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

from billing import get_order, receive_webhook  # noqa: E402
from payment_providers import ProviderOrderQueryResult  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_user(*, role="user", plan_code="free", uid=None):
    return SimpleNamespace(
        id=uid or str(uuid.uuid4()),
        email="user@test.com",
        display_name="Test",
        role=role,
        plan_code=plan_code,
        free_jobs_quota_total=5,
        free_jobs_quota_used=0,
    )


def _make_order_ns(
    *,
    order_id="order-1",
    user_id="uid-1",
    target_plan_code="plus",
    billing_period="monthly",
    provider="alipay",
    amount_cny=6900,
    status="pending",
    provider_order_id=None,
):
    return SimpleNamespace(
        id=order_id,
        user_id=user_id,
        target_plan_code=target_plan_code,
        billing_period=billing_period,
        provider=provider,
        provider_order_id=provider_order_id,
        amount_cny=amount_cny,
        currency="CNY",
        status=status,
        paid_at=None,
        updated_at=None,
        created_at=None,
    )


def _make_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _paid_event_execute(*, order, user):
    none_a = MagicMock()
    none_a.scalar_one_or_none.return_value = None
    order_res = MagicMock()
    order_res.scalar_one_or_none.return_value = order
    user_res = MagicMock()
    user_res.scalar_one_or_none.return_value = user
    none_invoice = MagicMock()
    none_invoice.scalar_one_or_none.return_value = None
    none_sub = MagicMock()
    none_sub.scalar_one_or_none.return_value = None
    sequence = [none_a, order_res, user_res, none_invoice, none_sub]
    idx = {"n": 0}

    async def _execute(*args, **kwargs):
        del args, kwargs
        i = idx["n"]
        idx["n"] += 1
        if i < len(sequence):
            return sequence[i]
        extra = MagicMock()
        extra.scalar_one_or_none.return_value = None
        return extra

    return _execute


def _set_alipay_env(monkeypatch):
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
    monkeypatch.setenv("AVT_ALIPAY_APP_ID", "app-id")
    monkeypatch.setenv("AVT_ALIPAY_APP_PRIVATE_KEY", private_pem)
    monkeypatch.setenv("AVT_ALIPAY_PUBLIC_KEY", public_pem)
    monkeypatch.setenv(
        "AVT_ALIPAY_NOTIFY_URL",
        "https://example.test/api/billing/webhooks/alipay",
    )
    monkeypatch.setenv(
        "AVT_ALIPAY_RETURN_URL",
        "https://example.test/settings/billing",
    )
    monkeypatch.setenv("AVT_ALIPAY_SELLER_ID", "2088102177694100")
    return {"private_key": private_pem, "public_key": public_pem}


class TestBillingAlipayLive:
    def test_get_order_refreshes_pending_alipay_order(self, monkeypatch):
        import payment_providers

        _set_alipay_env(monkeypatch)
        payment_providers._PROVIDERS = {}
        order = _make_order_ns(provider="alipay", status="pending", provider_order_id=None)
        user = _make_user(plan_code="free", uid="uid-1")
        db = _make_db()

        initial_order = MagicMock()
        initial_order.scalar_one_or_none.return_value = order
        base_execute = _paid_event_execute(order=order, user=user)
        calls = {"n": 0}

        async def execute(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return initial_order
            return await base_execute(*args, **kwargs)

        db.execute = execute

        provider = MagicMock()
        provider.query_order = AsyncMock(
            return_value=ProviderOrderQueryResult(
                provider_event_id="alipay_query_trade-1_TRADE_SUCCESS",
                provider_order_id="trade-1",
                provider_status="TRADE_SUCCESS",
                raw_payload={
                    "out_trade_no": "order-1",
                    "trade_no": "trade-1",
                    "trade_status": "TRADE_SUCCESS",
                    "total_amount": "69.00",
                    "seller_id": "2088102177694100",
                },
            )
        )
        provider.map_status.return_value = "paid"

        with patch("billing.get_provider", return_value=provider):
            result = _run(get_order("order-1", db, user, True))

        assert result["status"] == "paid"
        assert result["provider_order_id"] == "trade-1"
        assert order.provider_order_id == "trade-1"

    def test_alipay_webhook_returns_plain_success(self, monkeypatch):
        import payment_provider_alipay
        import payment_providers
        from payment_provider_alipay import AlipayConfig

        keys = _set_alipay_env(monkeypatch)
        payment_providers._PROVIDERS = {}
        config = AlipayConfig.from_env()
        assert config is not None

        payload = {
            "notify_id": "evt-alipay-1",
            "trade_no": "trade-1",
            "out_trade_no": "order-1",
            "trade_status": "TRADE_SUCCESS",
            "app_id": config.app_id,
            "total_amount": "69.00",
            "seller_id": "2088102177694100",
        }
        signed_payload = dict(payload)
        signed_payload["sign_type"] = "RSA2"
        signed_payload["sign"] = payment_provider_alipay._sign_with_private_key(
            keys["private_key"],
            payment_provider_alipay._canonicalize_params(payload),
        )
        raw_body = urlencode(signed_payload).encode("utf-8")

        request = MagicMock()
        request.body = AsyncMock(return_value=raw_body)
        request.headers = {"content-type": "application/x-www-form-urlencoded"}

        order = _make_order_ns(provider="alipay", status="pending", provider_order_id=None)
        user = _make_user(plan_code="free", uid="uid-1")
        db = _make_db()

        validation_order = MagicMock()
        validation_order.scalar_one_or_none.return_value = order
        base_execute = _paid_event_execute(order=order, user=user)
        calls = {"n": 0}

        async def execute(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return validation_order
            return await base_execute(*args, **kwargs)

        db.execute = execute

        response = _run(receive_webhook("alipay", request, db))
        assert response.body == b"success"
        assert order.status == "paid"
        assert order.provider_order_id == "trade-1"
