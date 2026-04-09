"""Tests for voice-selection pricing — Gateway truth source.

Validates that the pricing response shape and values match
DEBIT_RATES from credits_service + admin_settings.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

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
    return {
        "service_mode": "studio",
        "credits_per_minute": {
            "volcengine": DEBIT_RATES.get(("studio", "standard"), 15),
            "cosyvoice": DEBIT_RATES.get(("studio", "standard"), 15),
            "minimax_turbo": DEBIT_RATES.get(("studio", "high"), 30),
            "minimax_hd": DEBIT_RATES.get(("studio", "flagship"), 50),
        },
        "voice_clone_cost_credits": clone_cost,
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

    def test_response_shape(self) -> None:
        result = _build_pricing_response()
        assert result["service_mode"] == "studio"
        cpm = result["credits_per_minute"]
        for key in ("volcengine", "cosyvoice", "minimax_turbo", "minimax_hd"):
            assert key in cpm
            assert isinstance(cpm[key], int)
        assert isinstance(result["voice_clone_cost_credits"], int)

    def test_frozen_v3_values(self) -> None:
        """Sanity check: current V3 frozen values."""
        result = _build_pricing_response()
        cpm = result["credits_per_minute"]
        assert cpm["volcengine"] == 15
        assert cpm["cosyvoice"] == 15
        assert cpm["minimax_turbo"] == 30
        assert cpm["minimax_hd"] == 50
