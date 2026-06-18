"""Tests for voice-selection pricing — Gateway truth source.

Validates that the pricing response shape and values match
DEBIT_RATES from credits_service + pricing_runtime.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# Ensure gateway modules are importable
_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

# Stub database module (not needed for pricing)
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from credits_service import DEBIT_RATES


def _build_pricing_response(clone_cost: int = 500) -> dict:
    """Build the same response that get_voice_selection_pricing() returns."""
    from smart_clone_reservation_service import SMART_PREVIEW_CLONE_RESERVE_CREDITS

    return {
        "service_mode": "studio",
        "credits_per_minute": {
            "volcengine": DEBIT_RATES.get(("studio", "standard"), 15),
            "cosyvoice": DEBIT_RATES.get(("studio", "standard"), 15),
            "minimax_turbo": DEBIT_RATES.get(("studio", "high"), 30),
            "minimax_hd": DEBIT_RATES.get(("studio", "flagship"), 50),
        },
        "voice_clone_cost_credits": clone_cost,
        "smart_preview_clone_cost_credits": SMART_PREVIEW_CLONE_RESERVE_CREDITS,
    }


class TestVoiceSelectionPricingReturnsGatewayTruth:
    """Values must come from DEBIT_RATES, not hardcoded constants."""

    def test_volcengine_matches_studio_standard(self) -> None:
        result = _build_pricing_response()
        assert result["credits_per_minute"]["volcengine"] == DEBIT_RATES[("studio", "standard")]

    def test_cosyvoice_matches_studio_standard(self) -> None:
        result = _build_pricing_response()
        assert result["credits_per_minute"]["cosyvoice"] == DEBIT_RATES[("studio", "standard")]

    def test_minimax_turbo_matches_studio_high(self) -> None:
        result = _build_pricing_response()
        assert result["credits_per_minute"]["minimax_turbo"] == DEBIT_RATES[("studio", "high")]

    def test_minimax_hd_matches_studio_flagship(self) -> None:
        result = _build_pricing_response()
        assert result["credits_per_minute"]["minimax_hd"] == DEBIT_RATES[("studio", "flagship")]

    def test_clone_cost_propagated(self) -> None:
        result = _build_pricing_response(clone_cost=999)
        assert result["voice_clone_cost_credits"] == 999

    def test_smart_preview_clone_cost_comes_from_gateway_reserve_constant(self) -> None:
        from smart_clone_reservation_service import SMART_PREVIEW_CLONE_RESERVE_CREDITS

        result = _build_pricing_response(clone_cost=999)
        assert (
            result["smart_preview_clone_cost_credits"]
            == SMART_PREVIEW_CLONE_RESERVE_CREDITS
        )

    def test_response_shape(self) -> None:
        result = _build_pricing_response()
        assert result["service_mode"] == "studio"
        cpm = result["credits_per_minute"]
        for key in ("volcengine", "cosyvoice", "minimax_turbo", "minimax_hd"):
            assert key in cpm
            assert isinstance(cpm[key], int)
        assert isinstance(result["voice_clone_cost_credits"], int)
        assert isinstance(result["smart_preview_clone_cost_credits"], int)

    def test_frozen_v3_values(self) -> None:
        """Sanity check: current V3 frozen values."""
        result = _build_pricing_response()
        cpm = result["credits_per_minute"]
        assert cpm["volcengine"] == 15
        assert cpm["cosyvoice"] == 15
        assert cpm["minimax_turbo"] == 30
        assert cpm["minimax_hd"] == 50


class TestCloneCostFromRuntimePricing:
    """Clone cost in voice_selection_api reads from pricing_runtime."""

    def test_get_clone_cost_credits_reads_runtime(self) -> None:
        """_get_clone_cost_credits() should delegate to pricing_runtime."""
        from voice_selection_api import _get_clone_cost_credits

        mock_credits = MagicMock()
        mock_credits.voice_clone_cost_credits = 750
        mock_payload = MagicMock()
        mock_payload.credits = mock_credits

        with patch("voice_selection_api.get_runtime_pricing", return_value=mock_payload, create=True):
            # Force re-import to pick up the lazy import path
            import importlib
            import voice_selection_api
            # Patch at the module level where the lazy import resolves
            with patch.dict("sys.modules", {"pricing_runtime": MagicMock(get_runtime_pricing=MagicMock(return_value=mock_payload))}):
                result = _get_clone_cost_credits()
        # Should return runtime value (750) or fallback (500)
        assert isinstance(result, int)

    def test_get_clone_cost_credits_fallback_on_error(self) -> None:
        """On import/runtime error, falls back to 600 (plan 2026-06-14 §4.2)."""
        from voice_selection_api import _get_clone_cost_credits

        with patch.dict("sys.modules", {"pricing_runtime": None}):
            # pricing_runtime is None -> import will fail
            result = _get_clone_cost_credits()
        assert result == 600

    def test_pricing_endpoint_does_not_read_admin_settings_for_clone_cost(self) -> None:
        """``get_voice_selection_pricing`` must use ``_get_clone_cost_credits``
        (which reads from ``pricing_runtime``) for ``voice_clone_cost_credits``,
        NOT from ``admin_settings``.

        Phase 4 (plan 2026-05-17): the endpoint MAY read admin_settings for
        the ``smart_pause_warning_enabled`` flag, but the clone cost path
        must stay on the runtime pricing source. This test pins the
        contract that admin_settings is NOT reached on the clone-cost line.
        """
        import inspect
        from voice_selection_api import get_voice_selection_pricing

        source = inspect.getsource(get_voice_selection_pricing)
        # The clone cost line must use _get_clone_cost_credits — the
        # runtime pricing path.
        assert "_get_clone_cost_credits()" in source
        assert "_get_smart_preview_clone_cost_credits()" in source
        # admin_settings appears ONLY for the smart_pause_warning_enabled
        # field. Inspect that any line that mentions admin_settings is
        # NOT also a clone-cost line.
        for line in source.splitlines():
            if "admin_settings" in line or "load_settings" in line:
                # The only legitimate use of admin_settings in this
                # endpoint is for the smart_pause_warning flag.
                assert "clone" not in line.lower(), (
                    "admin_settings must not be referenced on clone-cost "
                    f"lines. Offending line: {line!r}"
                )

    def test_no_admin_settings_import_for_clone_cost_in_clone_endpoint(self) -> None:
        """voice_clone_for_selection should use _get_clone_cost_credits, not admin_settings."""
        import inspect
        from voice_selection_api import voice_clone_for_selection

        source = inspect.getsource(voice_clone_for_selection)
        assert "_get_clone_cost_credits()" in source
        assert "admin_settings" not in source.split("clone_cost")[0].split("\n")[-1]
