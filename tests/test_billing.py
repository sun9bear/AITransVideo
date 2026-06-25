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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
_fake_database.init_db = MagicMock()
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
    is_fake_payment_enabled,
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


def _make_order_ns(
    *,
    order_id="order-1",
    user_id="uid-1",
    target_plan_code="plus",
    billing_period="monthly",
    provider="fake",
    amount_cny=6900,
    status="pending",
    provider_order_id=None,
):
    """Mock PaymentOrder namespace enriched with Task 4 fields.

    Task 4 settlement writes a BillingInvoice row keyed on the order, so the
    order mock must expose `billing_period / provider / amount_cny / currency`
    for `upsert_invoice_for_paid_order` to read.
    """
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
    )


def _make_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.fixture(autouse=True)
def _default_payment_env(monkeypatch):
    monkeypatch.setenv("AVT_ENV", "dev")
    monkeypatch.delenv("AVT_ENABLE_FAKE_PAYMENT", raising=False)


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
    return {"private_key": private_pem, "public_key": public_pem}


def _paid_event_execute(*, order, user):
    """Build an `async db.execute` that replays the settlement path.

    Sequence of `db.execute` calls in `_process_payment_event` on a paid
    event AFTER P1-11b (commit a9b423c… replaced SELECT-then-INSERT
    with INSERT ON CONFLICT RETURNING):
      1. PaymentWebhookEvent INSERT ... RETURNING id → inserted id
         (None would mean ON CONFLICT fired = duplicate)
      2. PaymentWebhookEvent re-fetch by id          → fresh event ORM
      3. PaymentOrder lookup                          → ``order``
      4. User lookup                                  → ``user``
      5. BillingInvoice lookup                        → None
      6. Subscription lookup                          → None
    """
    insert_res = MagicMock()
    insert_res.scalar_one_or_none.return_value = "evt-row-1"
    fresh_event = SimpleNamespace(
        processed=False,
        error_message=None,
        processed_at=None,
    )
    event_res = MagicMock()
    event_res.scalar_one.return_value = fresh_event
    order_res = MagicMock(); order_res.scalar_one_or_none.return_value = order
    user_res = MagicMock(); user_res.scalar_one_or_none.return_value = user
    none_invoice = MagicMock(); none_invoice.scalar_one_or_none.return_value = None
    none_sub = MagicMock(); none_sub.scalar_one_or_none.return_value = None
    sequence = [insert_res, event_res, order_res, user_res, none_invoice, none_sub]
    idx = {"n": 0}

    async def _execute(*a, **kw):
        i = idx["n"]
        idx["n"] += 1
        if i < len(sequence):
            return sequence[i]
        # Any additional calls past the happy-path sequence return an empty row.
        extra = MagicMock(); extra.scalar_one_or_none.return_value = None
        return extra

    return _execute


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

    def test_fake_is_disabled_in_production_by_default(self, monkeypatch):
        monkeypatch.setenv("AVT_ENV", "production")
        monkeypatch.delenv("AVT_ENABLE_FAKE_PAYMENT", raising=False)

        assert is_fake_payment_enabled() is False
        assert is_provider_operational("fake") is False

    def test_fake_can_be_explicitly_enabled_in_production(self, monkeypatch):
        monkeypatch.setenv("AVT_ENV", "production")
        monkeypatch.setenv("AVT_ENABLE_FAKE_PAYMENT", "true")

        assert is_fake_payment_enabled() is True
        assert is_provider_operational("fake") is True

    def test_fake_is_disabled_for_unknown_environment(self, monkeypatch):
        monkeypatch.setenv("AVT_ENV", "prd")
        monkeypatch.setenv("AVT_ENABLE_FAKE_PAYMENT", "true")

        assert is_fake_payment_enabled() is False
        assert is_provider_operational("fake") is False

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

    def test_create_checkout_rejects_when_disabled(self, monkeypatch):
        monkeypatch.setenv("AVT_ENV", "production")
        monkeypatch.delenv("AVT_ENABLE_FAKE_PAYMENT", raising=False)

        with pytest.raises(RuntimeError, match="fake payment provider is disabled"):
            FakeProvider().create_checkout(
                order_id="ord-123", amount_cny=6900,
                target_plan_code="plus", billing_period="monthly",
            )

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

    def test_fake_provider_rejected_in_production_without_explicit_flag(self, monkeypatch):
        from fastapi import HTTPException
        user = _make_user(plan_code="free")
        db = self._make_order_db()

        monkeypatch.setenv("AVT_ENV", "production")
        monkeypatch.delenv("AVT_ENABLE_FAKE_PAYMENT", raising=False)

        body = CreateOrderRequest(target_plan_code="plus", provider="fake")
        with pytest.raises(HTTPException) as exc_info:
            _run(create_order(body, db, user))

        assert exc_info.value.status_code == 501
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

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
        order = _make_order_ns()
        db = _make_db()
        db.execute = _paid_event_execute(order=order, user=user)

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
        order = _make_order_ns()
        db = _make_db()
        db.execute = _paid_event_execute(order=order, user=user)

        settled = _run(_process_payment_event(
            db=db, provider="fake", provider_event_id="evt-1",
            event_type="payment.success", order_id="order-1",
            new_status="paid", signature_valid=True, raw_payload={},
        ))
        assert settled is True
        assert user.plan_code == "plus"
        assert order.status == "paid"
        # Task 4: a BillingInvoice and a Subscription row must have been added.
        added_types = {type(call.args[0]).__name__ for call in db.add.call_args_list}
        assert "BillingInvoice" in added_types
        assert "Subscription" in added_types

    def _make_smart_execute(self, *, order, fresh_event=None):
        """P1-11b mock helper: replay the new INSERT-then-fetch-event-then-
        SELECT sequence. Call 1 = INSERT RETURNING id (non-None to
        indicate "inserted, not duplicate"); call 2 = re-fetch event ORM;
        call 3+ = order / user / invoice / subscription — returns
        ``order_res`` for every subsequent call to mirror the original
        smart_execute's deliberately-loose semantics (the failed-payment
        and unverified paths short-circuit before drilling into User /
        Invoice fields, so we don't need a real User mock here).
        """
        if fresh_event is None:
            fresh_event = SimpleNamespace(
                processed=False, error_message=None, processed_at=None,
            )
        insert_res = MagicMock()
        insert_res.scalar_one_or_none.return_value = "evt-row-1"
        event_res = MagicMock()
        event_res.scalar_one.return_value = fresh_event
        order_res = MagicMock()
        order_res.scalar_one_or_none.return_value = order
        # call sequence:
        #   1: INSERT             → insert_res (id non-None = "inserted")
        #   2: re-fetch event ORM → event_res
        #   3+: order / user / invoice / subscription → order_res (loose mock,
        #       matching the legacy behaviour before P1-11b refactor)
        sequence = [insert_res, event_res]
        call_n = {"n": 0}

        async def smart_execute(*a, **kw):
            i = call_n["n"]
            call_n["n"] += 1
            if i < len(sequence):
                return sequence[i]
            return order_res
        return smart_execute

    def test_unverified_does_not_settle(self):
        user = _make_user(plan_code="free", uid="uid-1")
        order = SimpleNamespace(
            id="order-1", user_id="uid-1", target_plan_code="plus",
            status="pending", paid_at=None, updated_at=None,
        )
        db = _make_db()
        db.execute = self._make_smart_execute(order=order)

        settled = _run(_process_payment_event(
            db=db, provider="stripe", provider_event_id="evt-unverified",
            event_type="payment.success", order_id="order-1",
            new_status="paid", signature_valid=False,
        ))
        assert settled is False
        assert user.plan_code == "free"
        assert order.status == "pending"

    def test_duplicate_is_idempotent(self):
        """P1-11b: a duplicate webhook now manifests as INSERT ON CONFLICT
        DO NOTHING returning no row — scalar_one_or_none() == None."""
        db = _make_db()
        # First (and only expected) execute call: INSERT returns None
        # because the unique constraint fires (event already exists).
        insert_dup = MagicMock()
        insert_dup.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=insert_dup)

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
        db.execute = self._make_smart_execute(order=order)

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
        db.execute = self._make_smart_execute(order=order)

        settled = _run(_process_payment_event(
            db=db, provider="fake", provider_event_id="evt-fail",
            event_type="payment.failed", order_id="order-1",
            new_status="failed", signature_valid=True,
        ))
        assert settled is False
        assert order.status == "failed"

    def test_payment_does_not_touch_job_snapshots(self):
        user = _make_user(plan_code="free", uid="uid-1")
        order = _make_order_ns()
        db = _make_db()
        db.execute = _paid_event_execute(order=order, user=user)

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


# ===================================================================
# GET /api/billing/checkout-config — Task 5
# ===================================================================


class TestCheckoutConfig:
    """Gateway-owned provider availability endpoint.

    Keeps the frontend from reading `AVT_ALIPAY_*` env vars or hardcoding
    provider selection logic. `default_provider` is deterministic given the
    current registry state and alipay-config presence.
    """

    def _run_get_config(self, user):
        from billing import get_checkout_config
        return _run(get_checkout_config(user=user))

    def test_rejects_unauthenticated(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            self._run_get_config(user=None)
        assert exc_info.value.status_code == 401

    def test_fake_is_default_when_alipay_unconfigured(self, monkeypatch):
        # Scrub Alipay env to force non-operational state, then rebuild the
        # provider registry so the fresh AlipayProvider picks up the clean env.
        import payment_providers
        for var in (
            "AVT_ALIPAY_APP_ID",
            "AVT_ALIPAY_APP_PRIVATE_KEY",
            "AVT_ALIPAY_PUBLIC_KEY",
            "AVT_ALIPAY_NOTIFY_URL",
            "AVT_ALIPAY_RETURN_URL",
            "AVT_ALIPAY_GATEWAY_URL",
        ):
            monkeypatch.delenv(var, raising=False)
        payment_providers._PROVIDERS = {}

        user = _make_user(plan_code="free")
        result = self._run_get_config(user=user)
        assert result["default_provider"] == "fake"
        codes = {p["code"] for p in result["providers"]}
        assert "fake" in codes
        assert "alipay" in codes
        # Operational flags reflect the current environment.
        for entry in result["providers"]:
            if entry["code"] == "fake":
                assert entry["operational"] is True
            if entry["code"] == "alipay":
                assert entry["operational"] is False

    def test_fake_is_non_operational_in_production_checkout_config(self, monkeypatch):
        import payment_providers
        for var in (
            "AVT_ALIPAY_APP_ID",
            "AVT_ALIPAY_APP_PRIVATE_KEY",
            "AVT_ALIPAY_PUBLIC_KEY",
            "AVT_ALIPAY_NOTIFY_URL",
            "AVT_ALIPAY_RETURN_URL",
            "AVT_ALIPAY_GATEWAY_URL",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AVT_ENV", "production")
        monkeypatch.delenv("AVT_ENABLE_FAKE_PAYMENT", raising=False)
        payment_providers._PROVIDERS = {}

        user = _make_user(plan_code="free")
        result = self._run_get_config(user=user)
        fake_entry = next(p for p in result["providers"] if p["code"] == "fake")
        assert fake_entry["operational"] is False

    def test_alipay_becomes_default_when_env_is_complete(self, monkeypatch):
        import payment_providers

        _set_alipay_env(monkeypatch)
        payment_providers._PROVIDERS = {}

        user = _make_user(plan_code="free")
        result = self._run_get_config(user=user)
        assert result["default_provider"] == "alipay"
        alipay_entry = next(p for p in result["providers"] if p["code"] == "alipay")
        assert alipay_entry["operational"] is True

    def test_alipay_requires_return_url_to_be_operational(self, monkeypatch):
        import payment_providers

        _set_alipay_env(monkeypatch)
        monkeypatch.delenv("AVT_ALIPAY_RETURN_URL", raising=False)
        payment_providers._PROVIDERS = {}

        user = _make_user(plan_code="free")
        result = self._run_get_config(user=user)
        assert result["default_provider"] == "fake"
        alipay_entry = next(p for p in result["providers"] if p["code"] == "alipay")
        assert alipay_entry["operational"] is False

    def test_display_names_are_chinese_first(self, monkeypatch):
        import payment_providers
        for var in ("AVT_ALIPAY_APP_ID", "AVT_ALIPAY_APP_PRIVATE_KEY",
                    "AVT_ALIPAY_PUBLIC_KEY", "AVT_ALIPAY_NOTIFY_URL"):
            monkeypatch.delenv(var, raising=False)
        payment_providers._PROVIDERS = {}
        user = _make_user(plan_code="free")
        result = self._run_get_config(user=user)
        names = {p["code"]: p["display_name"] for p in result["providers"]}
        assert names["fake"] == "测试支付"
        assert names["alipay"] == "支付宝"
        assert names["wechatpay"] == "微信支付"

    def test_no_pricing_facts_leak_into_checkout_config(self, monkeypatch):
        import payment_providers
        payment_providers._PROVIDERS = {}
        user = _make_user(plan_code="free")
        result = self._run_get_config(user=user)
        # Strict shape lock — routing facts only, never prices. P3 (2026-06-10)
        # added recommended_provider + checkout_surface (surface-aware rail
        # recommendation); both are provider codes / surface tokens, not money.
        assert set(result.keys()) == {
            "default_provider",
            "recommended_provider",
            "checkout_surface",
            "providers",
        }
        assert isinstance(result["recommended_provider"], str)
        assert result["checkout_surface"] in {"pc_web", "mobile_web"}
        for entry in result["providers"]:
            assert "price" not in entry
            assert "amount_cny" not in entry
            assert "plan_code" not in entry

    @pytest.mark.parametrize(
        ("user_agent", "expected_surface"),
        [
            (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)",
                "mobile_web",
            ),
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "pc_web",
            ),
        ],
    )
    def test_wechatpay_is_recommended_on_mobile_and_desktop(
        self,
        monkeypatch,
        user_agent,
        expected_surface,
    ):
        from billing import get_checkout_config

        monkeypatch.setattr(
            "billing.list_providers",
            lambda: ["wechatpay", "paddle", "fake"],
        )
        monkeypatch.setattr(
            "billing.is_provider_operational",
            lambda code: code in {"wechatpay", "paddle"},
        )

        user = _make_user(plan_code="free")
        request = SimpleNamespace(headers={"user-agent": user_agent})
        result = _run(get_checkout_config(user=user, request=request))

        assert result["checkout_surface"] == expected_surface
        assert result["recommended_provider"] == "wechatpay"


# ===================================================================
# Alipay non-operational path (create_order rejection) — Task 5
# ===================================================================


class TestCreateOrderAlipayGate:
    """create_order must refuse to issue an order against a non-operational
    provider. This is the server-side guard behind any frontend CTA gating.
    """

    def _make_order_db(self):
        db = _make_db()
        db.flush = AsyncMock()
        return db

    def test_alipay_rejected_cleanly_when_unconfigured(self, monkeypatch):
        import payment_providers
        from fastapi import HTTPException
        for var in ("AVT_ALIPAY_APP_ID", "AVT_ALIPAY_APP_PRIVATE_KEY",
                    "AVT_ALIPAY_PUBLIC_KEY", "AVT_ALIPAY_NOTIFY_URL"):
            monkeypatch.delenv(var, raising=False)
        payment_providers._PROVIDERS = {}

        user = _make_user(plan_code="free")
        db = self._make_order_db()
        body = CreateOrderRequest(target_plan_code="plus", provider="alipay")
        with pytest.raises(HTTPException) as exc_info:
            _run(create_order(body, db, user))
        assert exc_info.value.status_code == 501
        # No order row should have been committed.
        db.commit.assert_not_awaited()

    def test_alipay_create_order_succeeds_when_env_is_complete(self, monkeypatch):
        import payment_providers

        _set_alipay_env(monkeypatch)
        payment_providers._PROVIDERS = {}

        user = _make_user(plan_code="free")
        db = self._make_order_db()
        request = SimpleNamespace(headers={"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"})
        body = CreateOrderRequest(target_plan_code="plus", provider="alipay")

        result = _run(create_order(body, db, user, request))
        assert result["provider"] == "alipay"
        assert result["checkout_surface"] == "mobile_web"
        assert "alipay.trade.wap.pay" in result["checkout_url"]
        db.commit.assert_awaited()


# ===================================================================
# GET /api/billing/fake-pay/{order_id} — browser handoff (T5 minor rev)
# ===================================================================


class TestFakePayBrowserRedirect:
    """The default fake checkout loop must be usable in a real browser.

    `FakeProvider.create_checkout` returns `/api/billing/fake-pay/{order_id}`
    as the checkout URL. The frontend hands off via `window.location.href`,
    which issues a GET. Prior to this revision the route only existed as POST,
    so the user hit 405 and dead-ended. The GET variant below must:

      1. settle the order via the same core logic as the POST path
      2. respond with a 303 redirect back to `/settings/billing`
      3. never surface a raw JSON body to the browser
    """

    def _make_db_with_order(self, order):
        none_a = MagicMock(); none_a.scalar_one_or_none.return_value = None
        order_res = MagicMock(); order_res.scalar_one_or_none.return_value = order

        calls = {"n": 0}

        async def execute(*args, **kwargs):
            calls["n"] += 1
            # First call: PaymentOrder lookup in fake-pay handler.
            # Then the _process_payment_event chain (dedup + order + user + invoice + sub).
            if calls["n"] == 1:
                return order_res
            if calls["n"] == 2:
                return none_a  # dedup check
            if calls["n"] == 3:
                return order_res  # order re-lookup inside _process_payment_event
            # Subsequent calls return None to keep the chain happy.
            empty = MagicMock(); empty.scalar_one_or_none.return_value = None
            return empty

        db = _make_db()
        db.execute = execute
        db.flush = AsyncMock()
        return db

    def test_get_handler_redirects_to_billing_on_success(self):
        from fastapi.responses import RedirectResponse
        from billing import fake_pay_browser

        order = _make_order_ns(status="pending")
        db = self._make_db_with_order(order)
        response = _run(fake_pay_browser("order-1", db))
        assert isinstance(response, RedirectResponse)
        assert response.status_code == 303
        assert response.headers["location"] == "/settings/billing?status=paid"

    def test_get_handler_never_returns_json_or_raises(self):
        """The browser path must not leak JSON or raise HTTPException.

        Programmatic callers go through the POST handler which still raises
        404/409 on not-found / already-terminal orders. The GET browser path
        absorbs those cases into a redirect with a status query param so the
        user always lands back inside the app.
        """
        from fastapi.responses import RedirectResponse
        from billing import fake_pay_browser

        # Case 1: order not found → redirect with error reason.
        db_none = _make_db()
        none_res = MagicMock(); none_res.scalar_one_or_none.return_value = None
        db_none.execute = AsyncMock(return_value=none_res)
        resp1 = _run(fake_pay_browser("ghost-order", db_none))
        assert isinstance(resp1, RedirectResponse)
        assert resp1.status_code == 303
        assert "order_not_found" in resp1.headers["location"]
        assert resp1.headers["location"].startswith("/settings/billing")

        # Case 2: order already paid → redirect with already_settled marker.
        db_paid = _make_db()
        order_paid = _make_order_ns(status="paid")
        paid_res = MagicMock(); paid_res.scalar_one_or_none.return_value = order_paid
        db_paid.execute = AsyncMock(return_value=paid_res)
        resp2 = _run(fake_pay_browser("order-1", db_paid))
        assert isinstance(resp2, RedirectResponse)
        assert resp2.status_code == 303
        assert "already_settled" in resp2.headers["location"]

    def test_fake_pay_routes_disabled_in_production_without_explicit_flag(self, monkeypatch):
        from fastapi import HTTPException
        from fastapi.responses import RedirectResponse
        from billing import fake_pay_browser

        monkeypatch.setenv("AVT_ENV", "production")
        monkeypatch.delenv("AVT_ENABLE_FAKE_PAYMENT", raising=False)

        post_db = _make_db()
        with pytest.raises(HTTPException) as exc_info:
            _run(fake_pay("order-1", post_db))
        assert exc_info.value.status_code == 403
        post_db.execute.assert_not_called()

        get_db = _make_db()
        response = _run(fake_pay_browser("order-1", get_db))
        assert isinstance(response, RedirectResponse)
        assert response.status_code == 303
        assert response.headers["location"] == (
            "/settings/billing?status=error&reason=fake_payment_disabled"
        )
        get_db.execute.assert_not_called()

    def test_post_handler_still_returns_json_for_programmatic_callers(self):
        """POST path preserves its original JSON contract.

        Tests and scripts that drive the fake-pay flow programmatically should
        keep getting `{"ok": True, "settled": ..., "order_id": ...}`.
        """
        from billing import fake_pay

        order = _make_order_ns(status="pending")
        db = self._make_db_with_order(order)
        result = _run(fake_pay("order-1", db))
        assert result["ok"] is True
        assert result["order_id"] == "order-1"
        assert "settled" in result

    def test_fake_provider_checkout_url_matches_get_route(self):
        """The URL FakeProvider returns must match the GET endpoint path.

        If anyone ever changes `FakeProvider.create_checkout` to return a
        different URL shape, the frontend handoff would break again. Lock the
        contract with a simple string match.
        """
        fake = FakeProvider()
        result = fake.create_checkout(
            order_id="order-xyz",
            amount_cny=6900,
            target_plan_code="plus",
            billing_period="monthly",
        )
        assert result.checkout_url == "/api/billing/fake-pay/order-xyz"
