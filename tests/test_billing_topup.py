"""CM-01 topup credit-pack purchase lane.

docs/plans/2026-07-02-commercialization-sprint-plan.md §2 CM-01.

Coverage map:
- pricing_schema: topup validator (prefix / plan-collision / dup / positive),
  price_usd_cents optional.
- billing_topup endpoints: packages listing (enabled gate, per-SKU rails,
  PayPal fail-closed without USD), order create (auth / enabled / SKU /
  provider gates, snapshot stamping, fake-provider happy path).
- settle_topup_paid on real aiosqlite: grant + ledger, replay probe,
  snapshot-fallback, both-missing → CRITICAL marker (never guess amounts),
  plan_code untouched.
- refund recall on real aiosqlite: topup refund revokes the bucket without
  touching plan; POISONING REGRESSION — refunding a plan order while a paid
  topup order exists must fall back to "free", never "topup_*".
- source wiring guards: billing.py dispatch + order_kind plan-lane filter +
  anti-cycle (billing_topup must not import billing).

Webhook-level idempotency (event dedup + terminal-state guard) is generic
_process_payment_event behavior already covered by test_billing.py /
test_billing_idempotency.py; the settle-side bucket probe here is the
belt-and-braces layer.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)

# Stub BEFORE importing billing/billing_topup (project convention: setdefault,
# never replace an already-imported module object).
import types  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
_fake_database.init_db = MagicMock()
sys.modules.setdefault("database", _fake_database)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


import billing  # noqa: E402
import billing_topup  # noqa: E402
import pricing_runtime  # noqa: E402
from billing_topup import (  # noqa: E402
    CreateTopupOrderRequest,
    create_topup_order,
    is_topup_order,
    list_topup_packages,
    settle_topup_paid,
)
from fastapi import HTTPException  # noqa: E402
from models import (  # noqa: E402
    AdminAuditLog,
    CreditsBucket,
    CreditsLedger,
    PaymentOrder,
    Subscription,
    User,
)
from pricing_schema import (  # noqa: E402
    PricingPayload,
    TopupConfig,
    TopupPackage,
    build_default_pricing_payload,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _default_payment_env(monkeypatch):
    monkeypatch.setenv("AVT_ENV", "dev")
    monkeypatch.delenv("AVT_ENABLE_FAKE_PAYMENT", raising=False)


# ---------------------------------------------------------------------------
# pricing fixtures
# ---------------------------------------------------------------------------


def _payload_with_topup(*, enabled: bool = True, usd_on_1000: int | None = None) -> PricingPayload:
    payload = build_default_pricing_payload()
    payload.topup.enabled = enabled
    if usd_on_1000 is not None:
        payload.topup.packages[0].price_usd_cents = usd_on_1000
    return payload


def _patch_pricing(monkeypatch, payload: PricingPayload) -> None:
    monkeypatch.setattr(
        pricing_runtime,
        "get_runtime_pricing",
        lambda force_reload=False: payload,
    )


def _make_user(*, plan_code="free", uid=None):
    return SimpleNamespace(
        id=uid or uuid.uuid4(),
        email="user@test.com",
        role="user",
        plan_code=plan_code,
    )


# ---------------------------------------------------------------------------
# real-sqlite harness (mirrors test_free_service_quota)
# ---------------------------------------------------------------------------

_TABLES = [
    User,
    PaymentOrder,
    CreditsBucket,
    CreditsLedger,
    Subscription,
    AdminAuditLog,
]


async def _make_sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        for model in _TABLES:
            await conn.run_sync(lambda s, t=model.__table__: t.create(s))
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _add_user(db: AsyncSession, *, plan_code="free") -> User:
    user = User(id=uuid.uuid4(), display_name="t", plan_code=plan_code)
    db.add(user)
    await db.flush()
    return user


def _topup_order_row(
    *,
    user_id,
    code="topup_1000",
    status="pending",
    credits_snapshot: int | None = 1000,
    paid_at=None,
) -> PaymentOrder:
    metadata = {"checkout_surface": "pc_web"}
    if credits_snapshot is not None:
        metadata["topup_credits_snapshot"] = credits_snapshot
    return PaymentOrder(
        id=uuid.uuid4(),
        user_id=user_id,
        provider="fake",
        order_kind="topup",
        target_plan_code=code,
        billing_period="one_time",
        amount_cny=3900,
        status=status,
        paid_at=paid_at,
        metadata_json=metadata,
    )


def _plan_order_row(*, user_id, plan="plus", paid_at=None) -> PaymentOrder:
    return PaymentOrder(
        id=uuid.uuid4(),
        user_id=user_id,
        provider="fake",
        order_kind="plan",
        target_plan_code=plan,
        billing_period="monthly",
        amount_cny=9900,
        status="paid",
        paid_at=paid_at,
        metadata_json={},
    )


# ---------------------------------------------------------------------------
# pricing_schema validator
# ---------------------------------------------------------------------------


class TestPricingSchemaTopup:
    def test_default_payload_validates_and_usd_defaults_none(self):
        payload = build_default_pricing_payload()
        assert payload.topup.enabled is False
        assert all(p.price_usd_cents is None for p in payload.topup.packages)

    def _payload_with_packages(self, packages):
        payload = build_default_pricing_payload()
        return payload.model_copy(update={"topup": TopupConfig(enabled=False, packages=packages)}).model_dump()

    def test_rejects_code_without_prefix(self):
        raw = self._payload_with_packages([TopupPackage(code="pack_1000", credits=1000, price_cny_fen=3900)])
        with pytest.raises(ValueError, match="must start with"):
            PricingPayload.model_validate(raw)

    def test_rejects_plan_code_collision_via_prefix_reservation(self):
        # A plan keyed by an existing topup code necessarily starts with
        # "topup_" → the prefix reservation subsumes the collision case.
        raw = build_default_pricing_payload().model_dump()
        raw["plans"]["topup_1000"] = raw["plans"]["plus"]
        with pytest.raises(ValueError, match="reserved for topup packages"):
            PricingPayload.model_validate(raw)

    def test_legacy_payload_without_topup_key_still_validates(self):
        # Adversarial review 2026-07-02 P1: a pre-topup pricing_runtime.json
        # (no "topup" key) must NOT fail whole-payload validation — that would
        # make pricing_runtime._load_from_file silently fall back to hardcoded
        # defaults, wiping admin-published plan prices. Missing key → inert.
        raw = build_default_pricing_payload().model_dump()
        del raw["topup"]
        payload = PricingPayload.model_validate(raw)
        assert payload.topup.enabled is False
        assert payload.topup.packages == []

    def test_rejects_plan_code_with_topup_prefix(self):
        # Adversarial review 2026-07-02 P2: provider adapters dispatch on the
        # code prefix, so a plan named "topup_*" would silently break that
        # plan's PayPal checkout — reject at config time.
        raw = build_default_pricing_payload().model_dump()
        raw["plans"]["topup_vip"] = raw["plans"]["plus"]
        with pytest.raises(ValueError, match="reserved for topup packages"):
            PricingPayload.model_validate(raw)

    def test_rejects_code_longer_than_16_chars(self):
        # CodeX CLI P2: PaymentOrder.target_plan_code / BillingInvoice.plan_code
        # are String(16) — an over-long SKU must fail at config time, not at
        # checkout flush on PostgreSQL.
        raw = self._payload_with_packages([TopupPackage(code="topup_12345678901", credits=1000, price_cny_fen=3900)])
        assert len("topup_12345678901") > 16
        with pytest.raises(ValueError, match="exceeds 16"):
            PricingPayload.model_validate(raw)

    def test_rejects_duplicate_codes(self):
        pkg = TopupPackage(code="topup_1000", credits=1000, price_cny_fen=3900)
        raw = self._payload_with_packages([pkg, pkg])
        with pytest.raises(ValueError, match="duplicate topup package code"):
            PricingPayload.model_validate(raw)

    def test_rejects_non_positive_credits_or_price(self):
        raw = self._payload_with_packages([TopupPackage(code="topup_bad", credits=0, price_cny_fen=3900)])
        with pytest.raises(ValueError, match="positive credits"):
            PricingPayload.model_validate(raw)


# ---------------------------------------------------------------------------
# GET /api/billing/topup/packages
# ---------------------------------------------------------------------------


class TestListPackages:
    def test_requires_login(self, monkeypatch):
        _patch_pricing(monkeypatch, _payload_with_topup())
        with pytest.raises(HTTPException) as exc:
            _run(list_topup_packages(user=None))
        assert exc.value.status_code == 401

    def test_disabled_returns_empty(self, monkeypatch):
        _patch_pricing(monkeypatch, _payload_with_topup(enabled=False))
        out = _run(list_topup_packages(user=_make_user()))
        assert out == {"enabled": False, "packages": []}

    def test_enabled_lists_packages_with_rails(self, monkeypatch):
        monkeypatch.setenv("AVT_ENABLE_FAKE_PAYMENT", "true")
        _patch_pricing(monkeypatch, _payload_with_topup(usd_on_1000=599))
        out = _run(list_topup_packages(user=_make_user()))
        assert out["enabled"] is True
        codes = [p["code"] for p in out["packages"]]
        assert codes == ["topup_1000", "topup_3000"]
        by_code = {p["code"]: p for p in out["packages"]}
        # fake is operational (env), wechat/paypal are not configured in tests.
        assert by_code["topup_1000"]["providers"] == ["fake"]
        assert by_code["topup_1000"]["price_usd_cents"] == 599
        assert by_code["topup_3000"]["price_usd_cents"] is None

    def test_paypal_rail_requires_usd_price(self, monkeypatch):
        _patch_pricing(monkeypatch, _payload_with_topup(usd_on_1000=599))
        monkeypatch.setattr(
            billing_topup,
            "is_provider_operational",
            lambda code: code in ("paypal", "fake"),
        )
        out = _run(list_topup_packages(user=_make_user()))
        by_code = {p["code"]: p for p in out["packages"]}
        # with USD → paypal offered; without USD → fail-closed hidden.
        assert "paypal" in by_code["topup_1000"]["providers"]
        assert "paypal" not in by_code["topup_3000"]["providers"]

    def test_inactive_packages_hidden(self, monkeypatch):
        payload = _payload_with_topup()
        payload.topup.packages[1].active = False
        _patch_pricing(monkeypatch, payload)
        out = _run(list_topup_packages(user=_make_user()))
        assert [p["code"] for p in out["packages"]] == ["topup_1000"]


# ---------------------------------------------------------------------------
# POST /api/billing/topup/orders
# ---------------------------------------------------------------------------


class TestCreateTopupOrder:
    def _req(self, code="topup_1000", provider="fake"):
        return CreateTopupOrderRequest(topup_code=code, provider=provider)

    def _create(self, db, user, *, code="topup_1000", provider="fake"):
        return create_topup_order(self._req(code=code, provider=provider), db=db, user=user, request=None)

    def test_requires_login(self, monkeypatch):
        _patch_pricing(monkeypatch, _payload_with_topup())
        with pytest.raises(HTTPException) as exc:
            _run(self._create(None, None))
        assert exc.value.status_code == 401

    def test_disabled_403(self, monkeypatch):
        _patch_pricing(monkeypatch, _payload_with_topup(enabled=False))
        with pytest.raises(HTTPException) as exc:
            _run(self._create(None, _make_user()))
        assert exc.value.status_code == 403

    def test_unknown_sku_400(self, monkeypatch):
        _patch_pricing(monkeypatch, _payload_with_topup())
        with pytest.raises(HTTPException) as exc:
            _run(self._create(None, _make_user(), code="topup_999"))
        assert exc.value.status_code == 400

    def test_inactive_sku_400(self, monkeypatch):
        payload = _payload_with_topup()
        payload.topup.packages[0].active = False
        _patch_pricing(monkeypatch, payload)
        with pytest.raises(HTTPException) as exc:
            _run(self._create(None, _make_user()))
        assert exc.value.status_code == 400

    def test_paddle_not_allowed_400(self, monkeypatch):
        _patch_pricing(monkeypatch, _payload_with_topup())
        with pytest.raises(HTTPException) as exc:
            _run(self._create(None, _make_user(), provider="paddle"))
        assert exc.value.status_code == 400
        assert "暂不支持点数充值" in exc.value.detail

    def test_non_operational_provider_501(self, monkeypatch):
        _patch_pricing(monkeypatch, _payload_with_topup())
        # wechatpay is allowed for topup but not configured in tests.
        with pytest.raises(HTTPException) as exc:
            _run(self._create(None, _make_user(), provider="wechatpay"))
        assert exc.value.status_code == 501

    def test_paypal_without_usd_price_400(self, monkeypatch):
        _patch_pricing(monkeypatch, _payload_with_topup(usd_on_1000=None))
        monkeypatch.setattr(billing_topup, "is_provider_operational", lambda code: True)
        with pytest.raises(HTTPException) as exc:
            _run(self._create(None, _make_user(), provider="paypal"))
        assert exc.value.status_code == 400
        assert "美元价格" in exc.value.detail

    def test_fake_provider_happy_path_snapshots_credits(self, monkeypatch):
        monkeypatch.setenv("AVT_ENABLE_FAKE_PAYMENT", "true")
        _patch_pricing(monkeypatch, _payload_with_topup())

        async def go():
            sm = await _make_sessionmaker()
            async with sm() as db:
                user = await _add_user(db)
                out = await self._create(db, user)
                row = (await db.execute(select(PaymentOrder))).scalars().one()
                return out, row

        out, row = _run(go())
        assert out["order_kind"] == "topup"
        assert out["topup_code"] == "topup_1000"
        assert out["credits"] == 1000
        assert out["amount_cny"] == 3900
        assert out["status"] == "pending"
        assert out["checkout_url"].startswith("/api/billing/fake-pay/")
        assert row.order_kind == "topup"
        assert row.target_plan_code == "topup_1000"
        assert row.billing_period == "one_time"
        assert row.metadata_json["topup_credits_snapshot"] == 1000


# ---------------------------------------------------------------------------
# settle_topup_paid (real sqlite)
# ---------------------------------------------------------------------------


class TestSettleTopupPaid:
    def test_grants_bucket_and_ledger_without_touching_plan(self):
        async def go():
            sm = await _make_sessionmaker()
            async with sm() as db:
                user = await _add_user(db, plan_code="free")
                order = _topup_order_row(user_id=user.id)
                db.add(order)
                await db.flush()
                updated = await settle_topup_paid(db, order=order)
                await db.commit()

                bucket = (await db.execute(select(CreditsBucket))).scalars().one()
                ledger = (await db.execute(select(CreditsLedger))).scalars().one()
                fresh_user = (await db.execute(select(User).where(User.id == user.id))).scalars().one()
                return updated, bucket, ledger, fresh_user, order

        updated, bucket, ledger, user, order = _run(go())
        assert updated is True
        assert bucket.bucket_type == "topup"
        assert bucket.granted == 1000 and bucket.remaining == 1000
        assert bucket.source_label == "topup_1000"
        assert str(bucket.related_order_id) == str(order.id)
        assert ledger.direction == "grant" and ledger.credits_delta == 1000
        assert ledger.reason_code == "topup_purchase"
        assert user.plan_code == "free"  # plan projection untouched
        assert order.metadata_json.get("topup_granted_bucket_id") == str(bucket.id)

    def test_replay_probe_skips_second_grant(self):
        async def go():
            sm = await _make_sessionmaker()
            async with sm() as db:
                user = await _add_user(db)
                order = _topup_order_row(user_id=user.id)
                db.add(order)
                await db.flush()
                first = await settle_topup_paid(db, order=order)
                second = await settle_topup_paid(db, order=order)
                await db.commit()
                buckets = (await db.execute(select(CreditsBucket))).scalars().all()
                return first, second, buckets

        first, second, buckets = _run(go())
        assert first is True and second is False
        assert len(buckets) == 1

    def test_missing_snapshot_falls_back_to_config_amount(self, monkeypatch):
        _patch_pricing(monkeypatch, _payload_with_topup(enabled=False))
        # enabled=False on purpose: settlement must NOT depend on the
        # purchase-path enabled gate (order predates the disable).

        async def go():
            sm = await _make_sessionmaker()
            async with sm() as db:
                user = await _add_user(db)
                order = _topup_order_row(user_id=user.id, credits_snapshot=None)
                db.add(order)
                await db.flush()
                updated = await settle_topup_paid(db, order=order)
                bucket = (await db.execute(select(CreditsBucket))).scalars().one_or_none()
                return updated, bucket

        updated, bucket = _run(go())
        assert updated is True
        assert bucket is not None and bucket.granted == 1000

    def test_snapshot_and_package_both_missing_marks_failure(self, monkeypatch):
        payload = _payload_with_topup()
        payload.topup.packages = []
        _patch_pricing(monkeypatch, payload)

        async def go():
            sm = await _make_sessionmaker()
            async with sm() as db:
                user = await _add_user(db)
                order = _topup_order_row(user_id=user.id, credits_snapshot=None)
                db.add(order)
                await db.flush()
                updated = await settle_topup_paid(db, order=order)
                buckets = (await db.execute(select(CreditsBucket))).scalars().all()
                return updated, buckets, order

        updated, buckets, order = _run(go())
        assert updated is False
        assert buckets == []  # never guess an amount
        assert order.metadata_json.get("topup_grant_failed") == "no_credit_amount"


# ---------------------------------------------------------------------------
# refund recall (real sqlite)
# ---------------------------------------------------------------------------


class TestRefundRecall:
    def test_topup_refund_revokes_bucket_keeps_plan(self):
        async def go():
            sm = await _make_sessionmaker()
            async with sm() as db:
                user = await _add_user(db, plan_code="plus")
                order = _topup_order_row(
                    user_id=user.id,
                    status="refunded",
                    paid_at=datetime.now(UTC),
                )
                db.add(order)
                await db.flush()
                await settle_topup_paid(db, order=order)
                await billing._recall_entitlements_for_refund(db, order=order, now=datetime.now(UTC))
                await db.commit()
                bucket = (await db.execute(select(CreditsBucket))).scalars().one()
                fresh_user = (await db.execute(select(User).where(User.id == user.id))).scalars().one()
                return bucket, fresh_user

        bucket, user = _run(go())
        assert bucket.remaining == 0  # revoked via related_order_id
        assert user.plan_code == "plus"  # plan projection untouched

    def test_poisoning_regression_plan_refund_ignores_paid_topup_order(self):
        """THE CM-01 red-line test: with a paid topup order on file, refunding
        the user's plan order must fall back to "free" — the topup SKU code
        must never be promoted into user.plan_code."""

        async def go():
            sm = await _make_sessionmaker()
            async with sm() as db:
                user = await _add_user(db, plan_code="plus")
                now = datetime.now(UTC)
                plan_order = _plan_order_row(user_id=user.id, paid_at=now)
                plan_order.status = "refunded"
                topup_order = _topup_order_row(
                    user_id=user.id,
                    status="paid",
                    paid_at=now,
                )
                db.add_all([plan_order, topup_order])
                await db.flush()
                await billing._recall_entitlements_for_refund(db, order=plan_order, now=now)
                await db.commit()
                fresh_user = (await db.execute(select(User).where(User.id == user.id))).scalars().one()
                return fresh_user

        user = _run(go())
        assert user.plan_code == "free"
        assert not user.plan_code.startswith("topup_")

    def test_plan_refund_still_falls_back_to_remaining_plan_order(self):
        """Existing behavior preserved: a real remaining paid plan order still
        wins the fallback (the order_kind filter only excludes topup)."""

        async def go():
            sm = await _make_sessionmaker()
            async with sm() as db:
                user = await _add_user(db, plan_code="pro")
                now = datetime.now(UTC)
                refunded = _plan_order_row(user_id=user.id, plan="pro", paid_at=now)
                refunded.status = "refunded"
                remaining = _plan_order_row(user_id=user.id, plan="plus", paid_at=now)
                topup_order = _topup_order_row(
                    user_id=user.id,
                    status="paid",
                    paid_at=now,
                )
                db.add_all([refunded, remaining, topup_order])
                await db.flush()
                await billing._recall_entitlements_for_refund(db, order=refunded, now=now)
                await db.commit()
                fresh_user = (await db.execute(select(User).where(User.id == user.id))).scalars().one()
                return fresh_user

        user = _run(go())
        assert user.plan_code == "plus"


# ---------------------------------------------------------------------------
# wiring guards (source-level)
# ---------------------------------------------------------------------------


class TestWiringGuards:
    def test_billing_dispatches_topup_before_subscription_upsert(self):
        import inspect

        src = inspect.getsource(billing._process_payment_event)
        assert "is_topup_order(order)" in src, "CM-01 regression: paid settle no longer dispatches topup orders"
        assert src.index("is_topup_order(order)") < src.index("upsert_active_subscription"), (
            "topup dispatch must run before the plan/subscription branch"
        )

    def test_refund_fallback_filters_plan_lane(self):
        import inspect

        src = inspect.getsource(billing._highest_remaining_paid_order_for_user)
        assert 'order_kind == "plan"' in src, (
            "CM-01 red-line regression: refund fallback no longer filters "
            "order_kind — a paid topup order could be promoted into "
            "user.plan_code"
        )

    def test_billing_topup_never_imports_billing(self):
        src = (Path(_GATEWAY_DIR) / "billing_topup.py").read_text(encoding="utf-8")
        assert "import billing\n" not in src and "from billing import" not in src, (
            "billing imports billing_topup for the settle hook; the reverse import would be a cycle"
        )

    def test_topup_rails_exclude_unpriced_providers(self):
        # Paddle charges by per-SKU dashboard price_id (none exist for topup)
        # and alipay direct is retired — offering either would 502 at click.
        assert "paddle" not in billing_topup.TOPUP_ALLOWED_PROVIDERS
        assert "alipay" not in billing_topup.TOPUP_ALLOWED_PROVIDERS
        assert "stripe" not in billing_topup.TOPUP_ALLOWED_PROVIDERS

    def test_is_topup_order_defaults_plan_for_legacy_rows(self):
        assert is_topup_order(SimpleNamespace(order_kind="topup")) is True
        assert is_topup_order(SimpleNamespace(order_kind="plan")) is False
        assert is_topup_order(SimpleNamespace(order_kind=None)) is False
        assert is_topup_order(SimpleNamespace()) is False
