"""Tests for Phase 6: Payment provider abstraction, billing routes, webhook processing.

Tests import real gateway modules with stubbed DB.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from billing import (  # noqa: E402
    create_order,
    get_order,
    fake_pay,
    receive_webhook,
    _process_payment_event,
    CreateOrderRequest,
    PLAN_PRICES_CNY,
    VALID_TARGET_PLANS,
    VALID_BILLING_PERIODS,
)
from payment_providers import (  # noqa: E402
    FakeProvider,
    StripeProvider,
    AlipayProvider,
    WechatPayProvider,
    get_provider,
    list_providers,
    is_provider_operational,
    NormalizedWebhookEvent,
    CheckoutResult,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_user(*, role="user", plan_code="free", uid=None):
    return SimpleNamespace(
        id=uid or str(uuid.uuid4()),
        email="user@test.com", display_name="Test",
        role=role, plan_code=plan_code,
        free_jobs_quota_total=5, free_jobs_quota_used=0,
    )


def _make_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


# ===================================================================
# Provider registry
# ===================================================================

class TestProviderRegistry:
    def test_list_providers_includes_all(self):
        names = list_providers()
        assert "fake" in names
        assert "stripe" in names
        assert "alipay" in names
        assert "wechatpay" in names

    def test_get_provider_returns_correct_type(self):
        assert isinstance(get_provider("fake"), FakeProvider)
        assert isinstance(get_provider("stripe"), StripeProvider)
        assert isinstance(get_provider("alipay"), AlipayProvider)
        assert isinstance(get_provider("wechatpay"), WechatPayProvider)

    def test_get_unknown_provider_raises(self):
        with pytest.raises(KeyError, match="Unknown"):
            get_provider("bitcoin")

    def test_fake_is_operational(self):
        assert is_provider_operational("fake") is True

    def test_stubs_are_not_operational(self):
        assert is_provider_operational("stripe") is False
        assert is_provider_operational("alipay") is False
        assert is_provider_operational("wechatpay") is False


# ===================================================================
# FakeProvider adapter
# ===================================================================

class TestFakeProvider:
    def test_create_checkout(self):
        p = FakeProvider()
        result = p.create_checkout(
            order_id="ord-123", amount_cny=6900,
            target_plan_code="plus", billing_period="monthly",
        )
        assert isinstance(result, CheckoutResult)
        assert "/api/billing/fake-pay/ord-123" in result.checkout_url
        assert result.provider_order_id is not None

    def test_verify_signature_always_true(self):
        assert FakeProvider().verify_signature(b"{}", {}) is True

    def test_parse_webhook(self):
        payload = json.dumps({
            "provider_event_id": "evt-1",
            "event_type": "payment.success",
            "order_id": "ord-1",
            "status": "paid",
        }).encode()
        event = FakeProvider().parse_webhook(payload)
        assert isinstance(event, NormalizedWebhookEvent)
        assert event.provider_event_id == "evt-1"
        assert event.new_status == "paid"
        assert event.order_id == "ord-1"

    def test_map_status_passthrough(self):
        p = FakeProvider()
        assert p.map_status("paid") == "paid"
        assert p.map_status("failed") == "failed"


# ===================================================================
# Stub providers — interface contracts
# ===================================================================

class TestStubProviders:
    def test_stripe_create_checkout_raises(self):
        with pytest.raises(NotImplementedError, match="stripe"):
            StripeProvider().create_checkout(
                order_id="x", amount_cny=100,
                target_plan_code="plus", billing_period="monthly",
            )

    def test_stripe_verify_signature_raises(self):
        with pytest.raises(NotImplementedError):
            StripeProvider().verify_signature(b"", {})

    def test_stripe_parse_webhook_raises(self):
        with pytest.raises(NotImplementedError):
            StripeProvider().parse_webhook(b"")

    def test_stripe_map_status_works(self):
        """Status mapping is defined even in stub — it's pure logic, no SDK needed."""
        p = StripeProvider()
        assert p.map_status("checkout.session.completed") == "paid"
        assert p.map_status("payment_intent.payment_failed") == "failed"
        assert p.map_status("charge.refunded") == "refunded"

    def test_alipay_map_status(self):
        p = AlipayProvider()
        assert p.map_status("TRADE_SUCCESS") == "paid"
        assert p.map_status("TRADE_CLOSED") == "cancelled"

    def test_wechatpay_map_status(self):
        p = WechatPayProvider()
        assert p.map_status("SUCCESS") == "paid"
        assert p.map_status("CLOSED") == "cancelled"
        assert p.map_status("REFUND") == "refunded"


# ===================================================================
# create_order — dispatched through provider
# ===================================================================

class TestCreateOrder:
    def _make_order_db(self):
        """DB mock that supports flush (assigns id) + commit + rollback."""
        db = _make_db()
        db.flush = AsyncMock()  # flush assigns order.id via default uuid4
        return db

    def test_fake_provider_creates_order(self):
        user = _make_user(plan_code="free")
        db = self._make_order_db()

        body = CreateOrderRequest(target_plan_code="plus", provider="fake")
        result = _run(create_order(body, db, user))

        assert result["status"] == "pending"
        assert result["checkout_url"] is not None
        assert "/fake-pay/" in result["checkout_url"]
        assert result["provider"] == "fake"
        # Only one commit — no double-commit from old code
        assert db.commit.await_count == 1

    def test_stub_provider_returns_501(self):
        from fastapi import HTTPException
        user = _make_user(plan_code="free")
        db = self._make_order_db()
        body = CreateOrderRequest(target_plan_code="plus", provider="stripe")
        with pytest.raises(HTTPException) as exc_info:
            _run(create_order(body, db, user))
        assert exc_info.value.status_code == 501

    def test_unknown_provider_returns_400(self):
        from fastapi import HTTPException
        user = _make_user(plan_code="free")
        db = self._make_order_db()
        body = CreateOrderRequest(target_plan_code="plus", provider="bitcoin")
        with pytest.raises(HTTPException) as exc_info:
            _run(create_order(body, db, user))
        assert exc_info.value.status_code == 400

    def test_reject_downgrade(self):
        from fastapi import HTTPException
        user = _make_user(plan_code="plus")
        db = self._make_order_db()
        body = CreateOrderRequest(target_plan_code="plus", provider="fake")
        with pytest.raises(HTTPException) as exc_info:
            _run(create_order(body, db, user))
        assert exc_info.value.status_code == 400

    def test_reject_unauthenticated(self):
        from fastapi import HTTPException
        db = self._make_order_db()
        body = CreateOrderRequest(target_plan_code="plus")
        with pytest.raises(HTTPException) as exc_info:
            _run(create_order(body, db, None))
        assert exc_info.value.status_code == 401

    def test_adapter_failure_rollbacks_no_dead_order(self):
        """If provider.create_checkout raises, the local order is rolled back."""
        from fastapi import HTTPException
        user = _make_user(plan_code="free")
        db = self._make_order_db()

        # Patch fake provider to raise on create_checkout
        with patch("billing.get_provider") as mock_get:
            broken_provider = MagicMock()
            broken_provider.operational = True
            broken_provider.create_checkout.side_effect = RuntimeError("网络超时")
            mock_get.return_value = broken_provider
            with patch("billing.is_provider_operational", return_value=True):
                with pytest.raises(HTTPException) as exc_info:
                    _run(create_order(
                        CreateOrderRequest(target_plan_code="plus", provider="fake"),
                        db, user,
                    ))
        assert exc_info.value.status_code == 502
        assert "checkout 失败" in exc_info.value.detail
        # DB must have been rolled back, not committed
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    def test_create_order_only_one_checkout_call(self):
        """Verify create_checkout is called exactly once (no double-create)."""
        user = _make_user(plan_code="free")
        db = self._make_order_db()

        with patch("billing.get_provider") as mock_get:
            fake = FakeProvider()
            fake.create_checkout = MagicMock(wraps=fake.create_checkout)
            mock_get.return_value = fake
            with patch("billing.is_provider_operational", return_value=True):
                _run(create_order(
                    CreateOrderRequest(target_plan_code="plus", provider="fake"),
                    db, user,
                ))
        assert fake.create_checkout.call_count == 1


# ===================================================================
# is_provider_operational — no side effects
# ===================================================================

class TestProviderOperationalCheck:
    def test_operational_check_has_no_side_effects(self):
        """is_provider_operational must not call create_checkout."""
        with patch.object(FakeProvider, "create_checkout", side_effect=AssertionError("should not be called")):
            # This must NOT raise — operational check reads attribute, doesn't call methods
            result = is_provider_operational("fake")
            assert result is True

    def test_stub_providers_not_operational(self):
        assert is_provider_operational("stripe") is False
        assert is_provider_operational("alipay") is False
        assert is_provider_operational("wechatpay") is False

    def test_unknown_provider_not_operational(self):
        assert is_provider_operational("bitcoin") is False


# ===================================================================
# receive_webhook — dispatched through provider adapter
# ===================================================================

class TestReceiveWebhook:
    def _make_request(self, body: dict) -> MagicMock:
        req = MagicMock()
        req.body = AsyncMock(return_value=json.dumps(body).encode())
        req.headers = {"content-type": "application/json"}
        return req

    def test_fake_provider_webhook_with_valid_signature(self):
        """Fake provider verify_signature returns True → event settles."""
        user = _make_user(plan_code="free", uid="uid-1")
        order = SimpleNamespace(
            id="order-1", user_id="uid-1", target_plan_code="plus",
            status="pending", paid_at=None, updated_at=None,
        )
        db = _make_db()
        none_result = MagicMock(); none_result.scalar_one_or_none.return_value = None
        order_result = MagicMock(); order_result.scalar_one_or_none.return_value = order
        user_result = MagicMock(); user_result.scalar_one_or_none.return_value = user

        call_n = {"n": 0}
        async def smart_execute(*a, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1: return none_result
            if call_n["n"] == 2: return order_result
            return user_result
        db.execute = smart_execute

        req = self._make_request({
            "provider_event_id": "fake-wh-1",
            "event_type": "payment.success",
            "order_id": "order-1",
            "status": "paid",
        })
        result = _run(receive_webhook("fake", req, db))
        assert result["settled"] is True
        assert user.plan_code == "plus"

    def test_stub_provider_webhook_does_not_settle(self):
        """Stub provider: verify_signature raises → signature_valid=False → no settlement."""
        db = _make_db()
        none_result = MagicMock(); none_result.scalar_one_or_none.return_value = None
        order = SimpleNamespace(
            id="order-1", user_id="uid-1", target_plan_code="plus",
            status="pending", paid_at=None, updated_at=None,
        )
        order_result = MagicMock(); order_result.scalar_one_or_none.return_value = order

        call_n = {"n": 0}
        async def smart_execute(*a, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1: return none_result
            return order_result
        db.execute = smart_execute

        req = self._make_request({
            "provider_event_id": "stripe-wh-1",
            "event_type": "payment.success",
            "order_id": "order-1",
            "status": "paid",
        })
        result = _run(receive_webhook("stripe", req, db))
        assert result["settled"] is False
        # Order status must NOT have changed
        assert order.status == "pending"

    def test_unknown_provider_webhook_returns_400(self):
        from fastapi import HTTPException
        req = self._make_request({"provider_event_id": "x"})
        db = _make_db()
        with pytest.raises(HTTPException) as exc_info:
            _run(receive_webhook("bitcoin", req, db))
        assert exc_info.value.status_code == 400


# ===================================================================
# _process_payment_event — core settlement (unchanged from Phase 5)
# ===================================================================

class TestProcessPaymentEvent:
    def test_verified_payment_upgrades_plan(self):
        user = _make_user(plan_code="free", uid="uid-1")
        order = SimpleNamespace(
            id="order-1", user_id="uid-1", target_plan_code="plus",
            status="pending", paid_at=None, updated_at=None,
        )
        db = _make_db()
        none_result = MagicMock(); none_result.scalar_one_or_none.return_value = None
        order_result = MagicMock(); order_result.scalar_one_or_none.return_value = order
        user_result = MagicMock(); user_result.scalar_one_or_none.return_value = user
        call_n = {"n": 0}
        async def smart_execute(*a, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1: return none_result
            if call_n["n"] == 2: return order_result
            return user_result
        db.execute = smart_execute

        settled = _run(_process_payment_event(
            db=db, provider="fake", provider_event_id="evt-1",
            event_type="payment.success", order_id="order-1",
            new_status="paid", signature_valid=True, raw_payload={},
        ))
        assert settled is True
        assert user.plan_code == "plus"
        assert order.status == "paid"

    def test_unverified_does_not_settle(self):
        user = _make_user(plan_code="free", uid="uid-1")
        order = SimpleNamespace(
            id="order-1", user_id="uid-1", target_plan_code="plus",
            status="pending", paid_at=None, updated_at=None,
        )
        db = _make_db()
        none_result = MagicMock(); none_result.scalar_one_or_none.return_value = None
        order_result = MagicMock(); order_result.scalar_one_or_none.return_value = order
        call_n = {"n": 0}
        async def smart_execute(*a, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1: return none_result
            return order_result
        db.execute = smart_execute

        settled = _run(_process_payment_event(
            db=db, provider="stripe", provider_event_id="evt-unverified",
            event_type="payment.success", order_id="order-1",
            new_status="paid", signature_valid=False,
        ))
        assert settled is False
        assert user.plan_code == "free"
        assert order.status == "pending"

    def test_duplicate_is_idempotent(self):
        db = _make_db()
        existing = SimpleNamespace(id="existing")
        found = MagicMock(); found.scalar_one_or_none.return_value = existing
        db.execute = AsyncMock(return_value=found)

        settled = _run(_process_payment_event(
            db=db, provider="fake", provider_event_id="dup-evt",
            event_type="payment.success", order_id="order-1",
            new_status="paid", signature_valid=True,
        ))
        assert settled is False
        db.commit.assert_not_awaited()

    def test_already_paid_not_reprocessed(self):
        order = SimpleNamespace(
            id="order-1", user_id="uid-1", target_plan_code="plus",
            status="paid", paid_at=datetime.now(timezone.utc), updated_at=None,
        )
        db = _make_db()
        none_result = MagicMock(); none_result.scalar_one_or_none.return_value = None
        order_result = MagicMock(); order_result.scalar_one_or_none.return_value = order
        call_n = {"n": 0}
        async def smart_execute(*a, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1: return none_result
            return order_result
        db.execute = smart_execute

        settled = _run(_process_payment_event(
            db=db, provider="fake", provider_event_id="evt-2",
            event_type="payment.success", order_id="order-1",
            new_status="paid", signature_valid=True,
        ))
        assert settled is False

    def test_failed_payment_does_not_upgrade(self):
        order = SimpleNamespace(
            id="order-1", user_id="uid-1", target_plan_code="plus",
            status="pending", paid_at=None, updated_at=None,
        )
        db = _make_db()
        none_result = MagicMock(); none_result.scalar_one_or_none.return_value = None
        order_result = MagicMock(); order_result.scalar_one_or_none.return_value = order
        call_n = {"n": 0}
        async def smart_execute(*a, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1: return none_result
            return order_result
        db.execute = smart_execute

        settled = _run(_process_payment_event(
            db=db, provider="fake", provider_event_id="evt-fail",
            event_type="payment.failed", order_id="order-1",
            new_status="failed", signature_valid=True,
        ))
        assert settled is False
        assert order.status == "failed"

    def test_payment_does_not_touch_job_snapshots(self):
        user = _make_user(plan_code="free", uid="uid-1")
        order = SimpleNamespace(
            id="order-1", user_id="uid-1", target_plan_code="plus",
            status="pending", paid_at=None, updated_at=None,
        )
        db = _make_db()
        none_result = MagicMock(); none_result.scalar_one_or_none.return_value = None
        order_result = MagicMock(); order_result.scalar_one_or_none.return_value = order
        user_result = MagicMock(); user_result.scalar_one_or_none.return_value = user
        call_n = {"n": 0}
        async def smart_execute(*a, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1: return none_result
            if call_n["n"] == 2: return order_result
            return user_result
        db.execute = smart_execute

        _run(_process_payment_event(
            db=db, provider="fake", provider_event_id="evt-snap",
            event_type="payment.success", order_id="order-1",
            new_status="paid", signature_valid=True,
        ))
        added_types = [type(call.args[0]).__name__ for call in db.add.call_args_list]
        assert "Job" not in str(added_types)

    def test_signature_valid_is_required_parameter(self):
        import inspect
        sig = inspect.signature(_process_payment_event)
        param = sig.parameters["signature_valid"]
        assert param.default is inspect.Parameter.empty


# ===================================================================
# Constants / prices
# ===================================================================

class TestConstants:
    def test_price_table_complete(self):
        for plan in ("plus", "pro"):
            for period in ("monthly", "quarterly", "annual"):
                assert (plan, period) in PLAN_PRICES_CNY
                assert PLAN_PRICES_CNY[(plan, period)] > 0

    def test_valid_target_plans(self):
        assert VALID_TARGET_PLANS == {"plus", "pro"}

    def test_valid_billing_periods(self):
        assert VALID_BILLING_PERIODS == {"monthly", "quarterly", "annual"}
