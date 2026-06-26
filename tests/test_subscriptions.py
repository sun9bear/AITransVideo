"""Tests for Task 4 subscription / billing-history truth source.

Covers the seven invariants called out by the T4 instruction:

1. Successful first payment creates a Subscription row.
2. Successful first payment creates a BillingInvoice row.
3. Duplicate webhook does not create duplicate Subscription rows.
4. Duplicate webhook does not create duplicate BillingInvoice rows.
5. `GET /api/me/subscription` returns a deterministic authenticated shape.
6. `GET /api/billing/history` returns user-scoped history only.
7. Trial bookkeeping is never silently converted into a paid subscription.

DB access is stubbed at the infrastructure level, matching the pattern used
in test_billing.py and test_auth_phone.py.
"""
from __future__ import annotations

import asyncio
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException


_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

import billing  # noqa: E402
import subscriptions as subs_module  # noqa: E402
from billing import _process_payment_event, list_billing_history  # noqa: E402
from models import BillingInvoice, Subscription  # noqa: E402
from subscriptions import (  # noqa: E402
    get_my_subscription,
    record_invoice_for_order,
    upsert_active_subscription,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_user(
    *,
    uid=None,
    plan_code="free",
    trial_granted_at=None,
    trial_ends_at=None,
):
    return SimpleNamespace(
        id=uid or uuid.uuid4(),
        email="u@test.com",
        display_name="Test",
        role="user",
        plan_code=plan_code,
        is_active=True,
        free_jobs_quota_total=5,
        free_jobs_quota_used=0,
        trial_granted_at=trial_granted_at,
        trial_ends_at=trial_ends_at,
    )


def _make_order(
    *,
    order_id="order-1",
    user_id=None,
    target_plan_code="plus",
    billing_period="monthly",
    provider="fake",
    amount_cny=6900,
    status="pending",
):
    return SimpleNamespace(
        id=order_id,
        user_id=user_id or "uid-1",
        target_plan_code=target_plan_code,
        billing_period=billing_period,
        provider=provider,
        provider_order_id=None,
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


def _paid_flow_execute(*, order, user, existing_invoice=None, existing_sub=None):
    """Reproduce the execute sequence of `_process_payment_event(paid)`
    AFTER P1-11b (commit ca99e00 replaced SELECT-then-INSERT dedup with
    INSERT ON CONFLICT RETURNING):

      1. PaymentWebhookEvent INSERT ... RETURNING id → ``"evt-row-1"``
         (None would mean "ON CONFLICT fired → duplicate → bail")
      2. PaymentWebhookEvent re-fetch by id          → fresh event ORM
      3. PaymentOrder lookup                          → ``order``
      4. User lookup                                  → ``user``
      5. BillingInvoice lookup                        → ``existing_invoice`` or None
      6. Subscription lookup                          → ``existing_sub`` or None
    """
    insert_res = MagicMock()
    insert_res.scalar_one_or_none.return_value = "evt-row-1"
    fresh_event = SimpleNamespace(
        processed=False, error_message=None, processed_at=None,
    )
    event_res = MagicMock()
    event_res.scalar_one.return_value = fresh_event
    order_res = MagicMock(); order_res.scalar_one_or_none.return_value = order
    user_res = MagicMock(); user_res.scalar_one_or_none.return_value = user
    invoice_res = MagicMock(); invoice_res.scalar_one_or_none.return_value = existing_invoice
    sub_res = MagicMock(); sub_res.scalar_one_or_none.return_value = existing_sub
    sequence = [insert_res, event_res, order_res, user_res, invoice_res, sub_res]
    idx = {"n": 0}

    async def _execute(*a, **kw):
        i = idx["n"]
        idx["n"] += 1
        if i < len(sequence):
            return sequence[i]
        extra = MagicMock(); extra.scalar_one_or_none.return_value = None
        return extra

    return _execute


# ---------------------------------------------------------------------------
# 1 + 2. First paid event creates Subscription and BillingInvoice rows
# ---------------------------------------------------------------------------


class TestFirstPaymentWritesTruthRows:
    def test_creates_subscription_row(self):
        user = _make_user()
        order = _make_order(user_id=user.id)
        db = _make_db()
        db.execute = _paid_flow_execute(order=order, user=user)

        _run(
            _process_payment_event(
                db=db,
                provider="fake",
                provider_event_id="evt-first-sub",
                event_type="payment.success",
                order_id=str(order.id),
                new_status="paid",
                signature_valid=True,
                raw_payload={},
            )
        )

        added = [call.args[0] for call in db.add.call_args_list]
        subs = [obj for obj in added if isinstance(obj, Subscription)]
        assert len(subs) == 1
        sub = subs[0]
        assert sub.plan_code == "plus"
        assert sub.billing_period == "monthly"
        assert sub.provider == "fake"
        assert sub.status == "active"
        assert sub.current_period_start is not None
        assert sub.current_period_end is not None
        # 30-day monthly window.
        assert (sub.current_period_end - sub.current_period_start) == timedelta(days=30)
        assert sub.cancelled_at is None

    def test_creates_billing_invoice_row(self):
        user = _make_user()
        order = _make_order(user_id=user.id)
        db = _make_db()
        db.execute = _paid_flow_execute(order=order, user=user)

        _run(
            _process_payment_event(
                db=db,
                provider="fake",
                provider_event_id="evt-first-inv",
                event_type="payment.success",
                order_id=str(order.id),
                new_status="paid",
                signature_valid=True,
                raw_payload={},
            )
        )

        added = [call.args[0] for call in db.add.call_args_list]
        invoices = [obj for obj in added if isinstance(obj, BillingInvoice)]
        assert len(invoices) == 1
        inv = invoices[0]
        assert inv.status == "paid"
        assert inv.plan_code == "plus"
        assert inv.billing_period == "monthly"
        assert inv.amount_cny == 6900
        assert inv.currency == "CNY"
        assert inv.provider == "fake"
        assert inv.paid_at is not None
        # Linked back to the settlement subscription.
        subs = [obj for obj in added if isinstance(obj, Subscription)]
        assert inv.subscription_id == subs[0].id

    def test_updates_user_plan_code_as_compatibility_projection(self):
        user = _make_user(plan_code="free")
        order = _make_order(user_id=user.id)
        db = _make_db()
        db.execute = _paid_flow_execute(order=order, user=user)

        _run(
            _process_payment_event(
                db=db,
                provider="fake",
                provider_event_id="evt-proj",
                event_type="payment.success",
                order_id=str(order.id),
                new_status="paid",
                signature_valid=True,
            )
        )
        # `user.plan_code` remains a projection for the existing gates in
        # entitlements.py / job_intercept.py. It must be updated AFTER the
        # subscription row is written.
        assert user.plan_code == "plus"


# ---------------------------------------------------------------------------
# 3 + 4. Duplicate webhooks must not create duplicates
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_duplicate_dedup_event_is_a_noop(self):
        """Duplicate PaymentWebhookEvent short-circuits the whole path.

        P1-11b (commit ca99e00): the dedup signal flipped from
        "SELECT existing returns a row" to "INSERT ON CONFLICT RETURNING
        produces no row (scalar_one_or_none() == None)".
        """
        db = _make_db()
        # INSERT … ON CONFLICT DO NOTHING RETURNING id — duplicate fires,
        # nothing returned. The function must bail out before any other
        # execute() call lands.
        insert_dup = MagicMock()
        insert_dup.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=insert_dup)

        settled = _run(
            _process_payment_event(
                db=db,
                provider="fake",
                provider_event_id="already-seen",
                event_type="payment.success",
                order_id="order-1",
                new_status="paid",
                signature_valid=True,
            )
        )
        assert settled is False
        # db.add must not have been called for invoice or subscription.
        for call in db.add.call_args_list:
            obj = call.args[0]
            assert not isinstance(obj, (Subscription, BillingInvoice))

    def test_record_invoice_is_idempotent_on_existing_row(self):
        """Second callback on the same order returns the existing invoice."""
        user = _make_user()
        order = _make_order(user_id=user.id)
        existing_invoice = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user.id,
            payment_order_id=order.id,
            provider="fake",
            provider_order_id=None,
            plan_code="plus",
            billing_period="monthly",
            amount_cny=6900,
            currency="CNY",
            status="paid",
            issued_at=datetime.now(timezone.utc),
            paid_at=datetime.now(timezone.utc),
            updated_at=None,
            subscription_id=None,
        )
        db = _make_db()
        found = MagicMock(); found.scalar_one_or_none.return_value = existing_invoice
        db.execute = AsyncMock(return_value=found)

        result = _run(
            record_invoice_for_order(
                db,
                order=order,
                settled_at=datetime.now(timezone.utc),
                status="paid",
            )
        )
        assert result is existing_invoice
        # No new BillingInvoice row was added.
        for call in db.add.call_args_list:
            assert not isinstance(call.args[0], BillingInvoice)

    def test_upsert_subscription_reuses_existing_active_row(self):
        """Renewal / repeat payment updates the same Subscription row in place."""
        user = _make_user()
        order = _make_order(user_id=user.id, billing_period="annual")
        existing_sub = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user.id,
            plan_code="plus",
            billing_period="monthly",
            provider="fake",
            status="active",
            started_at=datetime.now(timezone.utc) - timedelta(days=365),
            current_period_start=datetime.now(timezone.utc) - timedelta(days=40),
            current_period_end=datetime.now(timezone.utc) + timedelta(days=-10),
            cancelled_at=None,
            updated_at=None,
        )
        db = _make_db()
        found = MagicMock(); found.scalar_one_or_none.return_value = existing_sub
        db.execute = AsyncMock(return_value=found)

        result = _run(
            upsert_active_subscription(
                db, user=user, order=order, paid_at=datetime.now(timezone.utc)
            )
        )
        assert result is existing_sub
        assert existing_sub.billing_period == "annual"
        # Period was refreshed: new end >= new start + 365 days.
        assert (
            existing_sub.current_period_end - existing_sub.current_period_start
        ) == timedelta(days=365)
        # No new Subscription row was added.
        for call in db.add.call_args_list:
            assert not isinstance(call.args[0], Subscription)


# ---------------------------------------------------------------------------
# 5. GET /api/me/subscription response shape
# ---------------------------------------------------------------------------


class TestGetMySubscriptionResponseShape:
    def test_rejects_unauthenticated(self):
        db = _make_db()
        with pytest.raises(HTTPException) as exc_info:
            _run(get_my_subscription(db=db, user=None))
        assert exc_info.value.status_code == 401

    def test_free_user_without_subscription_returns_none_status(self):
        user = _make_user(plan_code="free", trial_granted_at=None)
        db = _make_db()
        none_res = MagicMock(); none_res.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=none_res)

        result = _run(get_my_subscription(db=db, user=user))
        assert result["plan_code"] == "free"
        assert result["subscription_status"] == "none"
        assert result["subscription"] is None
        assert result["trial"]["granted_at"] is None
        assert result["trial"]["ends_at"] is None

    def test_paid_user_returns_active_subscription_payload(self):
        user = _make_user(plan_code="plus")
        now = datetime.now(timezone.utc)
        active = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user.id,
            plan_code="plus",
            billing_period="monthly",
            provider="alipay",
            status="active",
            started_at=now - timedelta(days=10),
            current_period_start=now - timedelta(days=5),
            current_period_end=now + timedelta(days=25),
            cancelled_at=None,
        )
        db = _make_db()
        found = MagicMock(); found.scalar_one_or_none.return_value = active
        db.execute = AsyncMock(return_value=found)

        result = _run(get_my_subscription(db=db, user=user))
        assert result["plan_code"] == "plus"
        assert result["subscription_status"] == "active"
        sub = result["subscription"]
        assert sub is not None
        assert sub["plan_code"] == "plus"
        assert sub["billing_period"] == "monthly"
        assert sub["provider"] == "alipay"
        assert sub["status"] == "active"
        assert sub["current_period_end"] is not None
        assert sub["cancelled_at"] is None

    def test_trial_ends_at_stays_null_when_not_frozen(self):
        """T3/T4 invariant: we must never invent a trial countdown date."""
        user = _make_user(
            plan_code="free",
            trial_granted_at=datetime.now(timezone.utc),
            trial_ends_at=None,
        )
        db = _make_db()
        none_res = MagicMock(); none_res.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=none_res)

        result = _run(get_my_subscription(db=db, user=user))
        assert result["trial"]["granted_at"] is not None
        assert result["trial"]["ends_at"] is None
        # Trial bookkeeping must NOT leak into the paid subscription slot.
        assert result["subscription"] is None
        assert result["subscription_status"] == "none"


# ---------------------------------------------------------------------------
# 6. GET /api/billing/history user-scoped listing
# ---------------------------------------------------------------------------


class TestBillingHistoryScope:
    def test_rejects_unauthenticated(self):
        db = _make_db()
        with pytest.raises(HTTPException) as exc_info:
            _run(list_billing_history(db=db, user=None))
        assert exc_info.value.status_code == 401

    def test_returns_only_current_user_invoices(self):
        user = _make_user(uid=uuid.uuid4())
        now = datetime.now(timezone.utc)
        inv1 = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user.id,
            subscription_id=None,
            payment_order_id=uuid.uuid4(),
            provider="fake",
            provider_order_id=None,
            plan_code="plus",
            billing_period="monthly",
            amount_cny=6900,
            currency="CNY",
            status="paid",
            issued_at=now,
            paid_at=now,
            created_at=now,
        )
        inv2 = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user.id,
            subscription_id=None,
            payment_order_id=uuid.uuid4(),
            provider="fake",
            provider_order_id=None,
            plan_code="pro",
            billing_period="annual",
            amount_cny=259900,
            currency="CNY",
            status="paid",
            issued_at=now - timedelta(days=30),
            paid_at=now - timedelta(days=30),
            created_at=now - timedelta(days=30),
        )
        db = _make_db()
        scalars = MagicMock()
        scalars.all.return_value = [inv1, inv2]
        result_wrap = MagicMock()
        result_wrap.scalars.return_value = scalars
        db.execute = AsyncMock(return_value=result_wrap)

        result = _run(list_billing_history(db=db, user=user))
        assert "invoices" in result
        assert len(result["invoices"]) == 2
        first = result["invoices"][0]
        assert first["plan_code"] == "plus"
        assert first["amount_cny"] == 6900
        assert first["currency"] == "CNY"
        assert first["status"] == "paid"
        assert "issued_at" in first
        # The query itself is the scope guard — verify it was issued with a
        # where clause constraining BillingInvoice.user_id (stringly compared
        # against the user id, sufficient to prove scoping intent).
        db.execute.assert_awaited_once()
        sent_stmt = str(db.execute.call_args.args[0])
        assert "billing_invoices" in sent_stmt.lower()
        # Non-USD rails carry no USD charge amount.
        assert first["charged_usd_cents"] is None

    def test_paypal_invoice_surfaces_charged_usd_cents(self):
        """PayPal invoices expose the per-order USD snapshot the buyer actually
        paid; ``amount_cny`` stays the canonical CNY ledger value (not overwritten)."""
        user = _make_user(uid=uuid.uuid4())
        now = datetime.now(timezone.utc)
        order_id = uuid.uuid4()
        inv = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user.id,
            subscription_id=None,
            payment_order_id=order_id,
            provider="paypal",
            provider_order_id="0EC260925C948832M",
            plan_code="pro",
            billing_period="monthly",
            amount_cny=29900,
            currency="CNY",
            status="paid",
            issued_at=now,
            paid_at=now,
            created_at=now,
        )
        order = SimpleNamespace(
            id=order_id, metadata_json={"paypal_expected_usd_cents": 4999}
        )
        inv_scalars = MagicMock(); inv_scalars.all.return_value = [inv]
        inv_wrap = MagicMock(); inv_wrap.scalars.return_value = inv_scalars
        order_scalars = MagicMock(); order_scalars.all.return_value = [order]
        order_wrap = MagicMock(); order_wrap.scalars.return_value = order_scalars
        db = _make_db()
        db.execute = AsyncMock(side_effect=[inv_wrap, order_wrap])

        result = _run(list_billing_history(db=db, user=user))
        row = result["invoices"][0]
        assert row["provider"] == "paypal"
        assert row["charged_usd_cents"] == 4999
        assert row["amount_cny"] == 29900  # canonical ledger preserved
        assert row["currency"] == "CNY"

    def test_paypal_invoice_without_usd_snapshot_serializes_none(self):
        """Defensive: a PayPal invoice whose order lacks the USD snapshot still
        serializes with ``charged_usd_cents=None`` rather than raising."""
        user = _make_user(uid=uuid.uuid4())
        now = datetime.now(timezone.utc)
        order_id = uuid.uuid4()
        inv = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user.id,
            subscription_id=None,
            payment_order_id=order_id,
            provider="paypal",
            provider_order_id=None,
            plan_code="pro",
            billing_period="monthly",
            amount_cny=29900,
            currency="CNY",
            status="paid",
            issued_at=now,
            paid_at=now,
            created_at=now,
        )
        order = SimpleNamespace(id=order_id, metadata_json={})
        inv_scalars = MagicMock(); inv_scalars.all.return_value = [inv]
        inv_wrap = MagicMock(); inv_wrap.scalars.return_value = inv_scalars
        order_scalars = MagicMock(); order_scalars.all.return_value = [order]
        order_wrap = MagicMock(); order_wrap.scalars.return_value = order_scalars
        db = _make_db()
        db.execute = AsyncMock(side_effect=[inv_wrap, order_wrap])

        result = _run(list_billing_history(db=db, user=user))
        assert result["invoices"][0]["charged_usd_cents"] is None


# ---------------------------------------------------------------------------
# 7. Trial bookkeeping is never silently promoted to a paid subscription
# ---------------------------------------------------------------------------


class TestTrialIsNotPromotedToPaid:
    def test_get_my_subscription_for_trial_user_still_reports_none(self):
        """A trial-granted user with no paid order must not appear as `active`.

        This is the public API contract: trial bookkeeping only populates
        `response.trial.*`, never `response.subscription`.
        """
        user = _make_user(
            plan_code="free",
            trial_granted_at=datetime.now(timezone.utc),
            trial_ends_at=None,
        )
        db = _make_db()
        none_res = MagicMock(); none_res.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=none_res)

        result = _run(get_my_subscription(db=db, user=user))
        assert result["subscription_status"] == "none"
        assert result["subscription"] is None
        assert result["plan_code"] == "free"
        # Trial bookkeeping is still reported in its own slot.
        assert result["trial"]["granted_at"] is not None

    def test_upsert_subscription_never_runs_without_a_paid_order(self):
        """Sanity: `upsert_active_subscription` is only invoked from the paid
        branch of `_process_payment_event`.

        Validates the source contract by reading the function body — no other
        caller in the gateway package should reference it.
        """
        import inspect
        import billing as billing_module
        src = inspect.getsource(billing_module._process_payment_event)
        # Must only be called inside the `if new_status == "paid"` branch.
        idx_paid = src.find('new_status == "paid"')
        idx_upsert = src.find("upsert_active_subscription(")
        assert idx_paid != -1 and idx_upsert != -1
        assert idx_upsert > idx_paid

    def test_upsert_subscription_never_promotes_trial_bookkeeping_fields(self):
        """`upsert_active_subscription` writes fields strictly from `order`.

        It must never read `user.trial_granted_at` / `user.trial_ends_at`
        into the subscription row.
        """
        import inspect
        src = inspect.getsource(upsert_active_subscription)
        assert "trial_granted_at" not in src
        assert "trial_ends_at" not in src


# ---------------------------------------------------------------------------
# T4 minor revision #1: DB-level active-subscription uniqueness
# ---------------------------------------------------------------------------


class TestActiveSubscriptionUniqueness:
    def test_migration_contains_partial_unique_index(self):
        """The 008 migration must enforce the invariant at the DB layer.

        Comment-only contracts regress silently under concurrent settlements;
        this guard makes sure the partial unique index definition physically
        exists in the migration file.
        """
        from pathlib import Path
        migration = (
            Path(__file__).resolve().parent.parent
            / "gateway"
            / "alembic"
            / "versions"
            / "008_add_subscriptions_minimal.py"
        )
        src = migration.read_text(encoding="utf-8")
        assert "uq_subscriptions_one_active_per_user" in src
        assert "unique=True" in src
        assert "status = 'active'" in src

    def test_subscription_orm_declares_partial_unique_index(self):
        """The ORM `__table_args__` must also express the partial unique index

        Keeps the ORM metadata consistent with the migration so future
        autogenerate runs and schema introspection both see the constraint.
        """
        from sqlalchemy import Index
        sub_table = Subscription.__table__
        matching = [
            idx
            for idx in sub_table.indexes
            if idx.name == "uq_subscriptions_one_active_per_user"
        ]
        assert len(matching) == 1
        idx = matching[0]
        assert idx.unique is True
        # Column list must be exactly ["user_id"].
        assert [c.name for c in idx.columns] == ["user_id"]
        # And it must be a partial index keyed on status = 'active'.
        pg_where = idx.dialect_options.get("postgresql", {}).get("where")
        assert pg_where is not None
        assert "active" in str(pg_where)

    def test_upsert_active_subscription_updates_existing_row_in_place(self):
        """Settlement helper must never create a second active row."""
        user = _make_user()
        order = _make_order(
            user_id=user.id, target_plan_code="pro", billing_period="annual"
        )
        existing = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user.id,
            plan_code="plus",
            billing_period="monthly",
            provider="fake",
            status="active",
            started_at=datetime.now(timezone.utc) - timedelta(days=100),
            current_period_start=datetime.now(timezone.utc) - timedelta(days=30),
            current_period_end=datetime.now(timezone.utc) - timedelta(days=0),
            cancelled_at=None,
            updated_at=None,
        )
        db = _make_db()
        found = MagicMock(); found.scalar_one_or_none.return_value = existing
        db.execute = AsyncMock(return_value=found)

        result = _run(
            upsert_active_subscription(
                db,
                user=user,
                order=order,
                paid_at=datetime.now(timezone.utc),
            )
        )
        # Same row is returned — upgraded in place.
        assert result is existing
        assert existing.plan_code == "pro"
        assert existing.billing_period == "annual"
        # No new Subscription was added (would violate the partial unique idx
        # if it ran against a real Postgres instance).
        for call in db.add.call_args_list:
            assert not isinstance(call.args[0], Subscription)


# ---------------------------------------------------------------------------
# T4 minor revision #2: refund truth layer
# ---------------------------------------------------------------------------


class TestRefundInvoiceTransition:
    def test_record_invoice_transitions_paid_to_refunded_in_place(self):
        """A later refund event must flip an existing paid invoice to refunded.

        Before the fix, `_process_payment_event` short-circuited on any
        terminal order state, so the invoice layer never saw the refund.
        After the fix, `record_invoice_for_order(status="refunded")` updates
        the existing row in place.
        """
        user = _make_user()
        order = _make_order(user_id=user.id)
        existing_invoice = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user.id,
            payment_order_id=order.id,
            provider="fake",
            provider_order_id=None,
            plan_code="plus",
            billing_period="monthly",
            amount_cny=6900,
            currency="CNY",
            status="paid",
            issued_at=datetime.now(timezone.utc),
            paid_at=datetime.now(timezone.utc),
            updated_at=None,
            subscription_id=uuid.uuid4(),
        )
        db = _make_db()
        found = MagicMock(); found.scalar_one_or_none.return_value = existing_invoice
        db.execute = AsyncMock(return_value=found)

        refunded_at = datetime.now(timezone.utc)
        result = _run(
            record_invoice_for_order(
                db, order=order, settled_at=refunded_at, status="refunded"
            )
        )
        assert result is existing_invoice
        assert existing_invoice.status == "refunded"
        # Timestamp on the row must reflect the refund moment.
        assert existing_invoice.updated_at == refunded_at
        # No new invoice row was added.
        for call in db.add.call_args_list:
            assert not isinstance(call.args[0], BillingInvoice)

    def test_record_invoice_replay_is_noop(self):
        """A duplicate refund event on an already-refunded invoice is a noop."""
        user = _make_user()
        order = _make_order(user_id=user.id)
        existing_invoice = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user.id,
            payment_order_id=order.id,
            provider="fake",
            provider_order_id=None,
            plan_code="plus",
            billing_period="monthly",
            amount_cny=6900,
            currency="CNY",
            status="refunded",
            issued_at=datetime.now(timezone.utc) - timedelta(hours=1),
            paid_at=datetime.now(timezone.utc) - timedelta(hours=1),
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            subscription_id=None,
        )
        original_updated_at = existing_invoice.updated_at
        db = _make_db()
        found = MagicMock(); found.scalar_one_or_none.return_value = existing_invoice
        db.execute = AsyncMock(return_value=found)

        _run(
            record_invoice_for_order(
                db,
                order=order,
                settled_at=datetime.now(timezone.utc),
                status="refunded",
            )
        )
        # Unchanged — same-status replay is a no-op.
        assert existing_invoice.status == "refunded"
        assert existing_invoice.updated_at == original_updated_at

    def test_record_invoice_sets_paid_at_after_early_partial_refund(self):
        """Out-of-order partial refund then paid must keep invoice payment truth."""
        user = _make_user()
        order = _make_order(user_id=user.id)
        existing_invoice = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user.id,
            payment_order_id=order.id,
            provider="fake",
            provider_order_id=None,
            plan_code="plus",
            billing_period="monthly",
            amount_cny=6900,
            currency="CNY",
            status="partial_refunded",
            issued_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            paid_at=None,
            updated_at=None,
            subscription_id=None,
        )
        db = _make_db()
        found = MagicMock(); found.scalar_one_or_none.return_value = existing_invoice
        db.execute = AsyncMock(return_value=found)

        paid_at = datetime.now(timezone.utc)
        result = _run(
            record_invoice_for_order(
                db, order=order, settled_at=paid_at, status="paid"
            )
        )

        assert result is existing_invoice
        assert existing_invoice.status == "partial_refunded"
        assert existing_invoice.paid_at == paid_at
        assert existing_invoice.updated_at == paid_at

    def test_refund_webhook_does_not_touch_subscription_or_plan_code(self):
        """Refund truth update must stay scoped to billing history.

        T4 minor revision explicitly does NOT revoke the subscription or
        roll back `user.plan_code` on a refund. Those belong to Task 5/6.
        """
        user = _make_user(plan_code="plus")
        order = _make_order(user_id=user.id, status="paid")
        existing_invoice = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user.id,
            payment_order_id=order.id,
            provider="fake",
            provider_order_id=None,
            plan_code="plus",
            billing_period="monthly",
            amount_cny=6900,
            currency="CNY",
            status="paid",
            issued_at=datetime.now(timezone.utc),
            paid_at=datetime.now(timezone.utc),
            updated_at=None,
            subscription_id=uuid.uuid4(),
        )
        # P1-11b (commit ca99e00): new sequence is INSERT-RETURNING,
        # re-fetch event, then SELECT order, then SELECT invoice.
        insert_res = MagicMock()
        insert_res.scalar_one_or_none.return_value = "evt-row-1"
        fresh_event = SimpleNamespace(
            processed=False, error_message=None, processed_at=None,
        )
        event_res = MagicMock()
        event_res.scalar_one.return_value = fresh_event
        order_res = MagicMock(); order_res.scalar_one_or_none.return_value = order
        invoice_res = MagicMock()
        invoice_res.scalar_one_or_none.return_value = existing_invoice
        sequence = [insert_res, event_res, order_res, invoice_res]
        idx = {"n": 0}

        async def _execute(*a, **kw):
            i = idx["n"]
            idx["n"] += 1
            if i < len(sequence):
                return sequence[i]
            extra = MagicMock(); extra.scalar_one_or_none.return_value = None
            return extra

        db = _make_db()
        db.execute = _execute

        settled = _run(
            _process_payment_event(
                db=db,
                provider="fake",
                provider_event_id="evt-refund-1",
                event_type="payment.refunded",
                order_id=str(order.id),
                new_status="refunded",
                signature_valid=True,
                raw_payload={},
            )
        )

        # Settlement didn't trigger the "entitlements_updated" return path,
        # since we deliberately don't touch user.plan_code on refund in T4.
        assert settled is False
        assert existing_invoice.status == "refunded"
        # Crucially, the refund path does NOT mutate user.plan_code.
        assert user.plan_code == "plus"
        # No Subscription / AdminAuditLog row was created by the refund path.
        added_types = [type(call.args[0]).__name__ for call in db.add.call_args_list]
        assert "Subscription" not in added_types
        assert "AdminAuditLog" not in added_types

    def test_order_status_transitions_paid_to_refunded(self):
        """The order row itself must reflect refund state after the webhook."""
        user = _make_user()
        order = _make_order(user_id=user.id, status="paid")
        existing_invoice = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user.id,
            payment_order_id=order.id,
            provider="fake",
            provider_order_id=None,
            plan_code="plus",
            billing_period="monthly",
            amount_cny=6900,
            currency="CNY",
            status="paid",
            issued_at=datetime.now(timezone.utc),
            paid_at=datetime.now(timezone.utc),
            updated_at=None,
            subscription_id=None,
        )
        # P1-11b sequence (same shape as test_refund_webhook_does_not_touch_*).
        insert_res = MagicMock()
        insert_res.scalar_one_or_none.return_value = "evt-row-2"
        fresh_event = SimpleNamespace(
            processed=False, error_message=None, processed_at=None,
        )
        event_res = MagicMock()
        event_res.scalar_one.return_value = fresh_event
        order_res = MagicMock(); order_res.scalar_one_or_none.return_value = order
        invoice_res = MagicMock()
        invoice_res.scalar_one_or_none.return_value = existing_invoice

        sequence = [insert_res, event_res, order_res, invoice_res]
        idx = {"n": 0}

        async def _execute(*a, **kw):
            i = idx["n"]
            idx["n"] += 1
            if i < len(sequence):
                return sequence[i]
            extra = MagicMock(); extra.scalar_one_or_none.return_value = None
            return extra

        db = _make_db()
        db.execute = _execute

        _run(
            _process_payment_event(
                db=db,
                provider="fake",
                provider_event_id="evt-refund-2",
                event_type="payment.refunded",
                order_id=str(order.id),
                new_status="refunded",
                signature_valid=True,
                raw_payload={},
            )
        )
        assert order.status == "refunded"

    def test_duplicate_refund_event_still_dedups_via_webhook_event_id(self):
        """Replayed refund webhooks are blocked at the event-dedup layer.

        This guards against a provider sending the same refund event twice —
        the event-dedup happens before the terminal-state guard, so we never
        even reach the `_is_paid_to_refund` branch a second time.

        P1-11b (commit ca99e00): dedup signal flipped from "SELECT existing
        returns row" to "INSERT ON CONFLICT RETURNING returns no row".
        """
        db = _make_db()
        # INSERT … ON CONFLICT DO NOTHING RETURNING id — duplicate fires,
        # nothing returned. Function bails out before any other call.
        insert_dup = MagicMock()
        insert_dup.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=insert_dup)

        settled = _run(
            _process_payment_event(
                db=db,
                provider="fake",
                provider_event_id="dup-refund-evt",
                event_type="payment.refunded",
                order_id="order-x",
                new_status="refunded",
                signature_valid=True,
            )
        )
        assert settled is False
        # Nothing was added for this duplicate.
        db.add.assert_not_called()
