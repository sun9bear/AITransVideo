"""USD price plumbing for the PayPal lane (plan 2026-06-26 §5 / §17 S1).

Proves the full data path is wired: pricing_schema.PlanConfig.price_usd_cents
-> PlanPrice/PlanDefinition USD fields -> _get_runtime_plans mapper ->
plan_catalog.get_price_usd -> /api/plans price_usd_cents. A USD price published
into the runtime payload must be readable by get_price_usd (the bridge S1 flags
as easy to miss), and a USD edit must be caught by the frozen-field detector.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

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

import pricing_runtime  # noqa: E402
from plan_catalog import (  # noqa: E402
    _build_plans_response,
    get_price,
    get_price_usd,
)
from pricing_schema import (  # noqa: E402
    build_default_pricing_payload,
    detect_frozen_field_changes,
)


# --- defaults (§5.1) ---------------------------------------------------------


def test_get_price_usd_returns_default_usd_cents():
    # Plus: $16.99 / $44.99 / $159.99
    assert get_price_usd("plus", "monthly") == 1699
    assert get_price_usd("plus", "quarterly") == 4499
    assert get_price_usd("plus", "annual") == 15999
    # Pro: $49.99 / $129.99 / $469.99
    assert get_price_usd("pro", "monthly") == 4999
    assert get_price_usd("pro", "quarterly") == 12999
    assert get_price_usd("pro", "annual") == 46999


def test_get_price_usd_none_for_free_and_unknown():
    assert get_price_usd("free", "monthly") is None
    assert get_price_usd("does-not-exist", "monthly") is None
    assert get_price_usd("plus", "weekly") is None


def test_cny_price_unchanged_by_usd_addition():
    # amount_cny stays the canonical ledger unit; USD is a parallel field.
    assert get_price("plus", "monthly") == 9900
    assert get_price("pro", "annual") == 299900


# --- bridge: a published runtime USD price is readable (S1) -------------------


def test_published_runtime_usd_is_read_by_get_price_usd(tmp_path, monkeypatch):
    runtime_file = tmp_path / "pricing_runtime.json"
    monkeypatch.setattr(pricing_runtime, "PRICING_RUNTIME_FILE", runtime_file)
    pricing_runtime.invalidate_runtime_pricing_cache()

    payload = build_default_pricing_payload()
    # Bump Plus monthly USD to $19.99; leave CNY untouched.
    payload.plans["plus"].price_usd_cents.monthly = 1999
    pricing_runtime.write_runtime_snapshot(payload)

    try:
        assert get_price_usd("plus", "monthly") == 1999
        # CNY must be unaffected by the USD-only edit.
        assert get_price("plus", "monthly") == 9900
    finally:
        pricing_runtime.invalidate_runtime_pricing_cache()


# --- /api/plans surfaces price_usd_cents -------------------------------------


def test_plans_response_includes_price_usd_cents():
    resp = _build_plans_response()
    by_code = {p["code"]: p for p in resp["plans"]}

    assert by_code["plus"]["price_usd_cents"] == {
        "monthly": 1699,
        "quarterly": 4499,
        "annual": 15999,
    }
    assert by_code["pro"]["price_usd_cents"]["annual"] == 46999
    # Free has no price at all → both CNY and USD payloads are None.
    assert by_code["free"]["price_cny_fen"] is None
    assert by_code["free"]["price_usd_cents"] is None


# --- frozen-field audit covers USD (M6) --------------------------------------


def test_detect_frozen_field_changes_flags_usd_edit():
    old = build_default_pricing_payload()
    new = build_default_pricing_payload()
    new.plans["plus"].price_usd_cents.monthly = 1799

    changes = detect_frozen_field_changes(old, new)
    assert "plans.plus.price_usd_cents" in changes
    # CNY path unchanged → not reported.
    assert "plans.plus.price_cny_fen" not in changes
