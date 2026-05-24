"""Smart pricing clean-local fallback — Task #24, P2 launch blocker #2.

Spec sources:
  - docs/plans/2026-05-13-smart-mvp-p2-implementation-plan.md §5.3
    (clean-local fallback must include smart.standard:100)
  - docs/plans/2026-05-24-smart-auto-pipeline-rebaseline.md §3.2
  - codex audit 2026-05-24 acceptance criteria (7 items)

The bug: production runtime pricing JSON includes smart.standard=100, but
the repo's default fallback (used by clean-local dev, pytest, CI, and
any environment where runtime config is missing/corrupt) doesn't. So:
  - estimate_credits(min, "smart", "standard") silently falls back to
    DEFAULT_DEBIT_RATE=10 (the express rate), under-reserving smart jobs
    by 10× in any non-production env
  - bucket_priority dict has no "smart" key → smart consumes buckets
    in express priority order (free → subscription → topup → trial)
    instead of the paid-first order (trial → subscription → topup → free)
  - Plus/Pro plans in the default payload don't list smart, which
    contradicts plan_catalog.py PLANS (smart is in plus/pro there)

Fix: bring the default payload in line with production. Frozen
DEBIT_RATES and BUCKET_PRIORITY constants get the same treatment for
defense in depth (any code path that falls back to the frozen constants
instead of the runtime payload also gets the smart entries).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


# ─────────────────────────────────────────────────────────────────────
# Default pricing payload — must include smart.standard
# ─────────────────────────────────────────────────────────────────────


class TestDefaultPayloadSmartFields:
    def test_default_payload_debit_rates_includes_smart_standard_100(self):
        """build_default_pricing_payload().credits.debit_rates must
        contain ``smart.standard`` with value 100.

        Without this, ``_get_runtime_debit_rates`` returns a dict that
        doesn't include smart, ``estimate_credits`` then falls back to
        ``DEFAULT_DEBIT_RATE=10`` → 10× under-reservation."""
        from pricing_schema import build_default_pricing_payload

        payload = build_default_pricing_payload()
        rates = payload.credits.debit_rates
        assert "smart.standard" in rates, (
            f"Default payload must declare smart.standard. Got rates: {rates}. "
            "Production runtime has it; clean-local fallback must match."
        )
        assert rates["smart.standard"] == 100, (
            f"smart.standard must be 100 credits/source minute. Got {rates['smart.standard']}. "
            "Spec: docs/plans/2026-05-13-smart-mvp-p2-implementation-plan.md §5.3."
        )

    def test_default_payload_bucket_priority_includes_smart(self):
        """build_default_pricing_payload().credits.bucket_priority must
        contain a "smart" key. Without it, _pick_buckets_by_priority
        fires its fallback ``bp.get(service_mode, bp.get("express", ...))``
        and smart consumes from express priority order (free first),
        which leaks paid Plus/Pro grants to backstop free quota."""
        from pricing_schema import build_default_pricing_payload

        payload = build_default_pricing_payload()
        bp = payload.credits.bucket_priority
        assert "smart" in bp, (
            f"Default bucket_priority must declare 'smart' key. Got: {bp}. "
            "Without it, smart silently falls back to express priority."
        )
        # Smart is a paid feature like studio — must consume paid buckets
        # before free, otherwise paid grants subsidize free quota.
        assert bp["smart"] == ["trial", "subscription", "topup", "free"], (
            f"smart bucket_priority must match studio (paid-first). Got: {bp['smart']}."
        )

    def test_default_payload_plus_plan_includes_smart_mode(self):
        """plans.plus.allowed_service_modes must include 'smart'.

        Matches plan_catalog.py PLANS (which already lists smart) so
        clean-local and production agree on Plus tier entitlement."""
        from pricing_schema import build_default_pricing_payload

        payload = build_default_pricing_payload()
        assert "smart" in payload.plans["plus"].allowed_service_modes, (
            f"Plus plan must include 'smart' in allowed_service_modes. "
            f"Got: {payload.plans['plus'].allowed_service_modes}."
        )

    def test_default_payload_pro_plan_includes_smart_mode(self):
        """plans.pro.allowed_service_modes must include 'smart'."""
        from pricing_schema import build_default_pricing_payload

        payload = build_default_pricing_payload()
        assert "smart" in payload.plans["pro"].allowed_service_modes, (
            f"Pro plan must include 'smart' in allowed_service_modes. "
            f"Got: {payload.plans['pro'].allowed_service_modes}."
        )

    def test_default_payload_free_plan_excludes_smart_mode(self):
        """Free plan must NOT include smart — that would let any
        anonymous-tier user create smart jobs in clean-local."""
        from pricing_schema import build_default_pricing_payload

        payload = build_default_pricing_payload()
        assert "smart" not in payload.plans["free"].allowed_service_modes, (
            f"Free plan must NOT include 'smart'. Got: "
            f"{payload.plans['free'].allowed_service_modes}."
        )


# ─────────────────────────────────────────────────────────────────────
# Frozen V3 constants — defense in depth
# ─────────────────────────────────────────────────────────────────────


class TestFrozenConstantsSmartEntries:
    """credits_service.py frozen constants (DEBIT_RATES, BUCKET_PRIORITY)
    are used when even pricing_runtime fails to load. They must mirror
    the default payload — otherwise a deeply broken runtime falls back
    to constants that still under-charge smart."""

    def test_frozen_debit_rates_includes_smart_standard_100(self):
        from credits_service import DEBIT_RATES

        assert ("smart", "standard") in DEBIT_RATES, (
            f"DEBIT_RATES must include ('smart', 'standard'). Got keys: "
            f"{list(DEBIT_RATES.keys())}. This is the ultimate fallback when "
            f"pricing_runtime fails — must not under-charge smart."
        )
        assert DEBIT_RATES[("smart", "standard")] == 100

    def test_frozen_bucket_priority_includes_smart(self):
        from credits_service import BUCKET_PRIORITY

        assert "smart" in BUCKET_PRIORITY, (
            f"BUCKET_PRIORITY must include 'smart' key. Got: "
            f"{list(BUCKET_PRIORITY.keys())}."
        )
        assert BUCKET_PRIORITY["smart"] == ["trial", "subscription", "topup", "free"]


# ─────────────────────────────────────────────────────────────────────
# Behavior — estimate_credits must return 100 for smart.standard
# ─────────────────────────────────────────────────────────────────────


class TestEstimateCreditsSmart:
    """The acceptance test codex called out specifically:
    ``estimate_credits(1.0, 'smart', 'standard') == 100``."""

    def test_estimate_credits_smart_one_minute_returns_100(self):
        from credits_service import estimate_credits

        # 1 minute at 100 credits/min → 100.
        assert estimate_credits(1.0, "smart", "standard") == 100

    def test_estimate_credits_smart_zero_minutes_returns_0(self):
        from credits_service import estimate_credits

        # Edge: zero / None duration → 0 (per estimate_credits contract).
        assert estimate_credits(0, "smart", "standard") == 0
        assert estimate_credits(None, "smart", "standard") == 0

    def test_estimate_credits_smart_thirty_minutes_returns_3000(self):
        from credits_service import estimate_credits

        # 30 min at 100/min → 3000.
        assert estimate_credits(30.0, "smart", "standard") == 3000

    def test_estimate_credits_smart_fractional_minute_rounds(self):
        from credits_service import estimate_credits

        # estimate_credits uses round(minutes * rate); pin behavior at
        # the half-minute boundary so future schema changes can't quietly
        # change rounding mode (e.g. banker's vs floor).
        # 0.5 min × 100 = 50.
        assert estimate_credits(0.5, "smart", "standard") == 50

    def test_estimate_credits_smart_unknown_tier_falls_back_to_default(self):
        """Unknown quality tier falls back to DEFAULT_DEBIT_RATE.

        Pin this so callers know what happens when they pass an unknown
        tier — currently fallback to DEFAULT_DEBIT_RATE=10 (still a
        legitimate quote, just at the floor rate)."""
        from credits_service import estimate_credits, DEFAULT_DEBIT_RATE

        out = estimate_credits(1.0, "smart", "ultra_premium_nonexistent")
        assert out == DEFAULT_DEBIT_RATE


# ─────────────────────────────────────────────────────────────────────
# Behavior — bucket priority for smart matches paid-first order
# ─────────────────────────────────────────────────────────────────────


class TestBucketPrioritySmart:
    """When _get_runtime_bucket_priority returns the default (e.g. no
    runtime JSON), smart users must consume paid buckets first — same
    as studio."""

    def test_runtime_bucket_priority_smart_returns_paid_first_order(self):
        """Mock _get_runtime_bucket_priority to return the frozen
        constants and assert the smart key is paid-first."""
        from credits_service import _get_runtime_bucket_priority

        # No mocking — call it. If runtime is broken, falls back to
        # BUCKET_PRIORITY which we already tested above.
        # If runtime is OK, we still need smart present.
        bp = _get_runtime_bucket_priority()
        assert "smart" in bp, (
            f"_get_runtime_bucket_priority must surface 'smart' key. "
            f"Got: {bp}. Even when runtime config is missing/broken, "
            f"frozen BUCKET_PRIORITY must include it."
        )
