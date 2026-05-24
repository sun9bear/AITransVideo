"""Smart kill switch / 灰度门禁 — Task #23 (P2 launch blocker #1).

Spec sources:
  - docs/plans/2026-05-13-smart-mvp-p2-implementation-plan.md §5.3
  - docs/plans/2026-05-24-smart-auto-pipeline-rebaseline.md §3.1
  - User-confirmed scope (2026-05-24):
      AVT_ENABLE_SMART_MODE default False, admin runtime toggle, single
      helper feeding create job gate + entitlements/discovery gate.

Design:
  ``gateway/entitlements.py::get_effective_allowed_service_modes(user)``
  is the single source of truth. It reads:
    1. Layer 1 — env var ``AVT_ENABLE_SMART_MODE`` (Settings.enable_smart_mode)
    2. Layer 2 — admin runtime toggle ``AdminSettings.smart_mode_enabled``
  Both must be True for smart to appear in the returned list. When either
  is False, smart is removed regardless of the user's plan_code carrying
  it. Admin users get the same logic — no auto-bypass.

  3 call sites must use this helper:
    - entitlements.py::get_entitlements admin branch
    - entitlements.py::get_entitlements regular user branch
    - job_intercept.py create-job service_mode validation
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


_GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


# ─────────────────────────────────────────────────────────────────────
# Helper-level tests — pure decision function
# ─────────────────────────────────────────────────────────────────────


def _fake_settings(*, enable_smart_mode: bool = False):
    """Build a minimal stand-in for gateway/config.py Settings.

    Only the `enable_smart_mode` attribute is exercised by the helper, so
    we don't have to construct a full pydantic Settings instance.
    """
    return SimpleNamespace(enable_smart_mode=enable_smart_mode)


def _fake_admin_settings(*, smart_mode_enabled: bool = False):
    """Minimal stand-in for AdminSettings."""
    return SimpleNamespace(smart_mode_enabled=smart_mode_enabled)


def _fake_user(*, role: str = "user", plan_code: str = "plus"):
    """Minimal stand-in for ``models.User``."""
    return SimpleNamespace(
        role=role,
        plan_code=plan_code,
        free_jobs_quota_total=0,
        free_jobs_quota_used=0,
        trial_started_at=None,
        trial_expires_at=None,
    )


def _patch_plan_gate_with_smart():
    """Patch ``get_effective_plan_gate`` so the plan-level base list
    always includes smart. Lets us test the kill switch independently
    of whether pricing_runtime currently exposes smart for plus/pro
    (which itself is Task #24, P2 launch blocker #2)."""
    return patch(
        "entitlements.get_effective_plan_gate",
        return_value={
            "max_duration_minutes": 45,
            "max_concurrent_jobs": 3,
            "allowed_service_modes": ["express", "studio", "smart"],
        },
    )


class TestEffectiveAllowedServiceModesHelper:
    """Pure helper — single source of truth feeding all 3 gates."""

    def test_smart_present_when_env_and_admin_both_enabled(self):
        """env=True AND admin=True AND plan contains smart → smart kept."""
        from entitlements import get_effective_allowed_service_modes

        user = _fake_user(plan_code="plus")
        with _patch_plan_gate_with_smart(), patch(
            "admin_settings.load_settings",
            return_value=_fake_admin_settings(smart_mode_enabled=True),
        ):
            modes = get_effective_allowed_service_modes(
                user, settings=_fake_settings(enable_smart_mode=True),
            )
        assert "smart" in modes

    def test_smart_removed_when_env_off(self):
        """env=False → smart removed even if admin toggle and plan allow it."""
        from entitlements import get_effective_allowed_service_modes

        user = _fake_user(plan_code="plus")
        with _patch_plan_gate_with_smart(), patch(
            "admin_settings.load_settings",
            return_value=_fake_admin_settings(smart_mode_enabled=True),
        ):
            modes = get_effective_allowed_service_modes(
                user, settings=_fake_settings(enable_smart_mode=False),
            )
        assert "smart" not in modes
        # Other modes must be preserved — kill switch only removes smart.
        assert "express" in modes or "studio" in modes

    def test_smart_removed_when_admin_off(self):
        """admin=False → smart removed even if env allows it."""
        from entitlements import get_effective_allowed_service_modes

        user = _fake_user(plan_code="plus")
        with _patch_plan_gate_with_smart(), patch(
            "admin_settings.load_settings",
            return_value=_fake_admin_settings(smart_mode_enabled=False),
        ):
            modes = get_effective_allowed_service_modes(
                user, settings=_fake_settings(enable_smart_mode=True),
            )
        assert "smart" not in modes

    def test_smart_removed_when_both_off(self):
        """Default state — neither layer enabled → smart never returned."""
        from entitlements import get_effective_allowed_service_modes

        user = _fake_user(plan_code="plus")
        with _patch_plan_gate_with_smart(), patch(
            "admin_settings.load_settings",
            return_value=_fake_admin_settings(smart_mode_enabled=False),
        ):
            modes = get_effective_allowed_service_modes(
                user, settings=_fake_settings(enable_smart_mode=False),
            )
        assert "smart" not in modes

    def test_admin_user_does_not_auto_bypass_kill_switch(self):
        """Codex F2 fix: admin role does NOT auto-grant smart.

        Without this, any admin would get smart even when env or admin
        toggle says off — violates the kill switch contract. Admins
        must go through the same gate."""
        from entitlements import get_effective_allowed_service_modes

        user = _fake_user(role="admin", plan_code="plus")
        with _patch_plan_gate_with_smart(), patch(
            "admin_settings.load_settings",
            return_value=_fake_admin_settings(smart_mode_enabled=False),
        ):
            modes = get_effective_allowed_service_modes(
                user, settings=_fake_settings(enable_smart_mode=True),
            )
        assert "smart" not in modes, (
            "Admin must not auto-bypass kill switch. Admin runtime toggle "
            "off ⇒ no admin can create smart jobs, otherwise the toggle "
            "is meaningless as an emergency stop."
        )

    def test_helper_preserves_other_modes_when_kill_switch_off(self):
        """Kill switch must only affect smart — express/studio kept intact."""
        from entitlements import get_effective_allowed_service_modes

        user = _fake_user(plan_code="plus")
        with _patch_plan_gate_with_smart(), patch(
            "admin_settings.load_settings",
            return_value=_fake_admin_settings(smart_mode_enabled=False),
        ):
            modes = get_effective_allowed_service_modes(
                user, settings=_fake_settings(enable_smart_mode=False),
            )
        # Plus contains express + studio + smart per plan_catalog.py;
        # kill switch must keep express/studio untouched.
        assert "express" in modes
        assert "studio" in modes

    def test_helper_settings_default_uses_global_settings(self):
        """When caller doesn't pass settings, helper should fall back
        to the module-level ``settings`` import (live config).

        This is the production call shape — entitlements API and
        job_intercept will both call without an explicit settings arg.
        """
        from entitlements import get_effective_allowed_service_modes

        user = _fake_user(plan_code="plus")
        # Both layers off → smart absent regardless of how settings is sourced.
        with _patch_plan_gate_with_smart(), patch(
            "admin_settings.load_settings",
            return_value=_fake_admin_settings(smart_mode_enabled=False),
        ), patch(
            "config.settings",
            _fake_settings(enable_smart_mode=False),
        ):
            modes = get_effective_allowed_service_modes(user)
        assert "smart" not in modes


# ─────────────────────────────────────────────────────────────────────
# Settings / AdminSettings field existence
# ─────────────────────────────────────────────────────────────────────


class TestKillSwitchFieldsExist:
    """The two kill switch fields must exist on Settings and AdminSettings.

    These pin the env var name + admin field name so downstream gates
    can read them without guessing."""

    def test_settings_has_enable_smart_mode_default_false(self):
        """GatewaySettings.enable_smart_mode must exist and default to False."""
        from config import GatewaySettings

        # Don't construct via env — directly verify the field metadata.
        fields = GatewaySettings.model_fields
        assert "enable_smart_mode" in fields, (
            "Settings must declare enable_smart_mode (env AVT_ENABLE_SMART_MODE). "
            "Without this field the kill switch has no env layer — only "
            "admin runtime toggle remains."
        )
        assert fields["enable_smart_mode"].default is False, (
            "AVT_ENABLE_SMART_MODE must default to False — Smart should be "
            "off until ops explicitly turns it on. Defaulting True would "
            "expose Plus/Pro entitlement to all users automatically."
        )

    def test_admin_settings_has_smart_mode_enabled_default_false(self):
        """AdminSettings.smart_mode_enabled must exist and default to False."""
        from admin_settings import AdminSettings

        fields = AdminSettings.model_fields
        assert "smart_mode_enabled" in fields, (
            "AdminSettings must declare smart_mode_enabled — admin "
            "runtime toggle (5-min hot-flip) for the kill switch."
        )
        assert fields["smart_mode_enabled"].default is False, (
            "AdminSettings.smart_mode_enabled must default to False so a "
            "fresh admin_settings.json (or a broken/missing one) leaves "
            "smart OFF rather than ON."
        )


# ─────────────────────────────────────────────────────────────────────
# Call-site contract tests — source-level pinning
# ─────────────────────────────────────────────────────────────────────


class TestKillSwitchCallSitesUseHelper:
    """All 3 gates must call ``get_effective_allowed_service_modes`` —
    NOT read ``plan_info['allowed_service_modes']`` directly. Source-level
    pinning so a future refactor that bypasses the helper fails immediately."""

    def test_entitlements_get_entitlements_uses_helper(self):
        """entitlements.py::get_entitlements must call the helper for
        both admin and regular user branches."""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "gateway" / "entitlements.py"
        )
        source = src.read_text(encoding="utf-8")

        # Must import or define the helper
        assert "get_effective_allowed_service_modes" in source, (
            "entitlements.py must define/use get_effective_allowed_service_modes — "
            "the single source of truth for kill switch decisions."
        )

        # The hardcoded admin list ["express", "studio", "smart"] must be GONE
        # (or only present inside a docstring/comment, not as live code).
        # Search for live code occurrences — a precise string match is fragile
        # so we sanity-check that the helper is actually CALLED.
        assert "get_effective_allowed_service_modes(" in source, (
            "entitlements.py must CALL get_effective_allowed_service_modes "
            "(not just import it). Both admin and regular user branches "
            "should derive allowed_service_modes from the helper."
        )

    def test_job_intercept_create_gate_uses_helper(self):
        """job_intercept.py create-job validation must call the helper
        instead of reading plan_info['allowed_service_modes'] directly.

        Without this, the kill switch is useless — Plus/Pro plans
        statically include smart, so admin toggle off cannot prevent
        smart job creation from those users."""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "gateway" / "job_intercept.py"
        )
        source = src.read_text(encoding="utf-8")

        assert "get_effective_allowed_service_modes" in source, (
            "job_intercept.py must use get_effective_allowed_service_modes "
            "in the create-job service_mode validation. Reading "
            "plan_info['allowed_service_modes'] directly bypasses both "
            "kill switch layers."
        )

        # Confirm the validation block calls the helper, not just imports it.
        assert "get_effective_allowed_service_modes(" in source
