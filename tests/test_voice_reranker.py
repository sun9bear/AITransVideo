"""Tests for the shared voice reranker (combined_rerank + load_profiles)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services.tts.voice_reranker import (
    combined_rerank,
    load_profiles,
    resolve_age_bucket,
    score_to_confidence,
    MATURITY_MAP,
    GENDER_PITCH,
    PERSONA_TEXTURE,
    PERSONA_DELIVERY,
    PERSONA_ADJACENT,
    W_AGE_EXACT,
    W_PERSONA_EXACT,
    W_PITCH,
    W_MATURITY,
    W_ENERGY,
    W_DELIVERY,
    W_CHILDLIKE,
    W_TEXTURE,
    W_SPEED,
    W_SPEED_MIN,
    W_SPEED_MAX,
    W_SPEED_BASELINE_CPS,
    W_PERSONA_ADJACENT,
    compute_w_speed,
    is_voice_match_speed_dimension_enabled,
)


@pytest.fixture(autouse=True)
def _enable_speed_dimension(monkeypatch: pytest.MonkeyPatch):
    """CodeX P2-4: W_SPEED is gated behind admin flag (default OFF) for canary
    rollout. Existing tests assume the dimension is active — flip it ON for
    every test in this module so they exercise the real scoring math.
    The dedicated default-OFF behaviour gets its own test below.
    """
    from services.tts import voice_reranker as _vr
    monkeypatch.setattr(_vr, "is_voice_match_speed_dimension_enabled", lambda: True)


# ===================================================================
# resolve_age_bucket
# ===================================================================

class TestResolveAgeBucket:
    def test_young_aliases(self) -> None:
        assert resolve_age_bucket("young") == "young"
        assert resolve_age_bucket("Youth") == "young"

    def test_middle_aliases(self) -> None:
        assert resolve_age_bucket("middle") == "middle"
        assert resolve_age_bucket("adult") == "middle"
        assert resolve_age_bucket("MATURE") == "middle"

    def test_elderly_aliases(self) -> None:
        assert resolve_age_bucket("elderly") == "elderly"
        assert resolve_age_bucket("Old") == "elderly"
        assert resolve_age_bucket("senior") == "elderly"

    def test_empty_returns_empty(self) -> None:
        assert resolve_age_bucket(None) == ""
        assert resolve_age_bucket("") == ""

    def test_unknown_returns_empty(self) -> None:
        assert resolve_age_bucket("teenage") == ""


# ===================================================================
# score_to_confidence
# ===================================================================

class TestScoreToConfidence:
    def test_high(self) -> None:
        assert score_to_confidence(0.45) == "high"
        assert score_to_confidence(0.80) == "high"

    def test_medium(self) -> None:
        assert score_to_confidence(0.25) == "medium"
        assert score_to_confidence(0.44) == "medium"

    def test_low(self) -> None:
        assert score_to_confidence(0.10) == "low"
        assert score_to_confidence(0.0) == "low"


# ===================================================================
# combined_rerank — scoring logic
# ===================================================================

class TestCombinedRerank:
    def test_empty_candidates(self) -> None:
        assert combined_rerank([], {}, gender="male", age_bucket="", persona="", energy="") == []

    def test_single_candidate_no_profile(self) -> None:
        candidates = [{"voice_id": "v1", "gender": "male"}]
        scored = combined_rerank(candidates, {}, gender="male", age_bucket="", persona="", energy="")
        assert len(scored) == 1
        assert scored[0] == ("v1", 0.0)

    def test_age_exact_match_scores_highest(self) -> None:
        candidates = [
            {"voice_id": "v_young", "gender": "male", "age_group": "young"},
            {"voice_id": "v_old", "gender": "male", "age_group": "elderly"},
        ]
        scored = combined_rerank(candidates, {}, gender="male", age_bucket="young", persona="", energy="")
        assert scored[0][0] == "v_young"
        assert scored[0][1] >= W_AGE_EXACT

    def test_persona_exact_match(self) -> None:
        candidates = [
            {"voice_id": "v_warm", "gender": "female", "persona_style": "warm"},
            {"voice_id": "v_pro", "gender": "female", "persona_style": "professional"},
        ]
        scored = combined_rerank(candidates, {}, gender="female", age_bucket="", persona="warm", energy="")
        assert scored[0][0] == "v_warm"
        assert scored[0][1] >= W_PERSONA_EXACT

    def test_persona_adjacent_gives_partial_score(self) -> None:
        candidates = [
            {"voice_id": "v_serious", "gender": "male", "persona_style": "serious"},
            {"voice_id": "v_none", "gender": "male"},
        ]
        scored = combined_rerank(candidates, {}, gender="male", age_bucket="", persona="professional", energy="")
        # serious is adjacent to professional
        assert scored[0][0] == "v_serious"
        assert scored[0][1] > 0

    def test_profile_dimensions_score(self) -> None:
        candidates = [
            {"voice_id": "v_a", "gender": "female"},
            {"voice_id": "v_b", "gender": "female"},
        ]
        profiles = {
            "v_a": {
                "pitch_level": "low",       # mismatch for female
                "maturity": "elder",         # mismatch for young
                "childlike": True,           # mismatch
                "delivery_style": "narration",
                "energy_level": "high",
                "texture_tags": ["husky"],
            },
            "v_b": {
                "pitch_level": "high",       # match for female
                "maturity": "young",         # match
                "childlike": False,          # match
                "delivery_style": "companion",  # match for warm
                "energy_level": "medium",    # match
                "texture_tags": ["soft", "magnetic"],  # match for warm
            },
        }
        scored = combined_rerank(
            candidates, profiles,
            gender="female", age_bucket="young", persona="warm", energy="medium",
        )
        assert scored[0][0] == "v_b"
        assert scored[0][1] > scored[1][1]

    def test_sorted_descending(self) -> None:
        candidates = [
            {"voice_id": "v_low", "gender": "male"},
            {"voice_id": "v_high", "gender": "male", "age_group": "young", "persona_style": "warm"},
            {"voice_id": "v_mid", "gender": "male", "age_group": "young"},
        ]
        scored = combined_rerank(candidates, {}, gender="male", age_bucket="young", persona="warm", energy="")
        scores = [s for _, s in scored]
        assert scores == sorted(scores, reverse=True)

    def test_child_childlike_scoring(self) -> None:
        """Child speaker + childlike=True voice should score on childlike dimension."""
        candidates = [{"voice_id": "v_child", "gender": "child"}]
        profiles = {"v_child": {"childlike": True, "pitch_level": "high"}}
        scored = combined_rerank(
            candidates, profiles,
            gender="child", age_bucket="child", persona="", energy="",
        )
        # Should get childlike + pitch at minimum
        assert scored[0][1] >= W_CHILDLIKE + W_PITCH

    def test_weights_sum_to_one_with_speed(self) -> None:
        """After Task 2, catalog+profile+speed weights should still sum to ~1.0.

        Catalog picks one of {EXACT, ADJACENT} per category (age/persona),
        so the maximum-achievable score uses the EXACT branches plus all
        profile dims plus speed.
        """
        max_total = (
            W_AGE_EXACT + W_PERSONA_EXACT
            + W_PITCH + W_MATURITY + W_ENERGY
            + W_DELIVERY + W_CHILDLIKE + W_TEXTURE
            + W_SPEED
        )
        # Allow small drift (0.03) since W_SPEED tops catalog+profile max.
        # The score is used for ranking only — exact 1.0 is not required.
        assert 0.95 <= max_total <= 1.05

    # ===================================================================
    # Task 2: W_SPEED (target chars_per_second) scoring
    # ===================================================================

    def test_speed_dimension_disabled_when_target_none(self) -> None:
        """target=None → speed adds zero; only base dimensions contribute."""
        candidates = [
            {"voice_id": "slow", "gender": "male", "chars_per_second": 2.8},
            {"voice_id": "fast", "gender": "male", "chars_per_second": 5.0},
        ]
        scored = combined_rerank(
            candidates, {}, gender="male", age_bucket="", persona="", energy="",
            target_chars_per_second=None,
        )
        # Both score 0 on all dims (no age, persona, profile), so equal.
        assert scored[0][1] == scored[1][1] == 0.0

    def test_speed_exact_match_full_bonus_for_extreme_target(self) -> None:
        """Munger target (2.7 cps) → W_SPEED_MAX, voice cps==target → full bonus."""
        candidates = [{"voice_id": "perfect", "gender": "male", "chars_per_second": 2.7}]
        scored = combined_rerank(
            candidates, {}, gender="male", age_bucket="", persona="", energy="",
            target_chars_per_second=2.7,
        )
        assert scored[0][1] == pytest.approx(W_SPEED_MAX, abs=1e-6)

    def test_speed_normal_target_uses_minimum_weight(self) -> None:
        """Target near baseline (4.20 ±10%) should use W_SPEED_MIN."""
        candidates = [{"voice_id": "perfect_normal", "gender": "male", "chars_per_second": 4.20}]
        scored = combined_rerank(
            candidates, {}, gender="male", age_bucket="", persona="", energy="",
            target_chars_per_second=4.20,
        )
        # Voice exactly matches target → full speed bonus, but bounded by W_SPEED_MIN
        assert scored[0][1] == pytest.approx(W_SPEED_MIN, abs=1e-6)

    def test_speed_partial_match_proportional_extreme(self) -> None:
        """Munger target (extreme), voice 50% off cps → 0.5 × W_SPEED_MAX."""
        candidates = [{"voice_id": "half", "gender": "male", "chars_per_second": 4.05}]
        scored = combined_rerank(
            candidates, {}, gender="male", age_bucket="", persona="", energy="",
            target_chars_per_second=2.7,
        )
        # diff=1.35, ratio=1.35/2.7=0.5 → bonus = W_SPEED_MAX * 0.5 (extreme target)
        assert scored[0][1] == pytest.approx(W_SPEED_MAX * 0.5, abs=1e-6)

    def test_speed_diff_beyond_100pct_clamped(self) -> None:
        """Extreme mismatch clamps to 0 (no negative bonus)."""
        candidates = [{"voice_id": "extreme", "gender": "male", "chars_per_second": 10.0}]
        scored = combined_rerank(
            candidates, {}, gender="male", age_bucket="", persona="", energy="",
            target_chars_per_second=2.7,
        )
        # diff=7.3, ratio=2.7 → clamped to 1.0 → bonus=0
        assert scored[0][1] == pytest.approx(0.0, abs=1e-6)

    def test_speed_missing_voice_cps_no_penalty(self) -> None:
        """Voice without calibration data scores 0 on speed (no penalty)."""
        candidates = [
            {"voice_id": "calibrated", "gender": "male", "chars_per_second": 3.0},
            {"voice_id": "uncalibrated", "gender": "male"},  # no cps
        ]
        scored = combined_rerank(
            candidates, {}, gender="male", age_bucket="", persona="", energy="",
            target_chars_per_second=2.7,
        )
        scores = dict(scored)
        assert scores["uncalibrated"] == 0.0
        assert scores["calibrated"] > 0.0

    def test_speed_munger_scenario_promotes_slow_voice(self) -> None:
        """Realistic Munger scenario: slow storyteller voice beats mid voice
        when the other dimensions are tied.
        """
        candidates = [
            {
                "voice_id": "storyteller",
                "gender": "male",
                "age_group": "elderly",
                "persona_style": "warm",
                "chars_per_second": 3.04,
            },
            {
                "voice_id": "mid_elderly",
                "gender": "male",
                "age_group": "elderly",
                "persona_style": "warm",
                "chars_per_second": 4.2,
            },
        ]
        scored = combined_rerank(
            candidates, {}, gender="male", age_bucket="elderly", persona="warm", energy="",
            target_chars_per_second=2.7,
        )
        # Both get identical catalog-tag bonus; adaptive W_SPEED gives the
        # slower voice a much bigger boost (Munger scenario).
        assert scored[0][0] == "storyteller"


# ===================================================================
# Task 2: compute_w_speed (adaptive weight)
# ===================================================================

class TestAdaptiveWSpeed:
    def test_target_none_returns_zero(self) -> None:
        w, dev = compute_w_speed(None)
        assert w == 0.0
        assert dev == 0.0

    def test_target_zero_or_negative_returns_zero(self) -> None:
        assert compute_w_speed(0.0) == (0.0, 0.0)
        assert compute_w_speed(-1.0) == (0.0, 0.0)

    def test_normal_speaker_minimum_weight(self) -> None:
        """Within ±10% of baseline → W_SPEED_MIN."""
        w, dev = compute_w_speed(W_SPEED_BASELINE_CPS)  # exactly baseline
        assert w == pytest.approx(W_SPEED_MIN, abs=1e-6)
        assert dev == pytest.approx(0.0, abs=1e-6)

        w, dev = compute_w_speed(W_SPEED_BASELINE_CPS * 1.05)  # +5%
        assert w == pytest.approx(W_SPEED_MIN, abs=1e-6)
        assert dev == pytest.approx(0.05, abs=1e-6)

    def test_extreme_speaker_maximum_weight(self) -> None:
        """Beyond ±35% of baseline → W_SPEED_MAX."""
        w, dev = compute_w_speed(2.5)  # baseline 4.20, dev = 40.5%
        assert w == pytest.approx(W_SPEED_MAX, abs=1e-6)

        w, dev = compute_w_speed(7.0)  # dev = 66.7%
        assert w == pytest.approx(W_SPEED_MAX, abs=1e-6)

    def test_munger_scenario_hits_max(self) -> None:
        """Munger target=2.7 cps gives deviation 35.7%, just over the cap."""
        w, dev = compute_w_speed(2.7)
        assert dev == pytest.approx(0.357, abs=1e-3)
        assert w == pytest.approx(W_SPEED_MAX, abs=1e-6)

    def test_mid_band_linear_interpolation(self) -> None:
        """Deviation between 10% and 35% → linear ramp."""
        # deviation = 22.5% (midway) → expected weight = (MIN+MAX)/2 = 0.175
        target = W_SPEED_BASELINE_CPS * (1 - 0.225)
        w, dev = compute_w_speed(target)
        assert dev == pytest.approx(0.225, abs=1e-3)
        expected = W_SPEED_MIN + (W_SPEED_MAX - W_SPEED_MIN) * 0.5
        assert w == pytest.approx(expected, abs=1e-3)

    def test_mid_band_quarter_ramp(self) -> None:
        """deviation = 16.25% → quarter of the way from MIN to MAX."""
        target = W_SPEED_BASELINE_CPS * (1 + 0.1625)
        w, dev = compute_w_speed(target)
        assert dev == pytest.approx(0.1625, abs=1e-3)
        expected = W_SPEED_MIN + (W_SPEED_MAX - W_SPEED_MIN) * 0.25
        assert w == pytest.approx(expected, abs=1e-3)


# ===================================================================
# CodeX P2-4: admin gate (voice_match_speed_dimension_enabled)
# ===================================================================

class TestVoiceMatchSpeedDimensionGate:
    def test_disabled_by_default_when_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When flag is OFF, compute_w_speed returns (0, 0) regardless of input.

        This isolates the gate from the autouse fixture in this module: we
        explicitly re-patch the flag to False inside this test.
        """
        from services.tts import voice_reranker as _vr
        monkeypatch.setattr(_vr, "is_voice_match_speed_dimension_enabled", lambda: False)
        # Munger's extreme target — would normally trigger W_SPEED_MAX
        w, dev = compute_w_speed(2.7)
        assert w == 0.0
        assert dev == 0.0

    def test_combined_rerank_ignores_speed_when_gate_off(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify the gate stops W_SPEED bonus from entering combined_rerank."""
        from services.tts import voice_reranker as _vr
        monkeypatch.setattr(_vr, "is_voice_match_speed_dimension_enabled", lambda: False)
        candidates = [{"voice_id": "perfect", "gender": "male", "chars_per_second": 2.7}]
        scored = combined_rerank(
            candidates, {}, gender="male", age_bucket="", persona="", energy="",
            target_chars_per_second=2.7,
        )
        # No W_SPEED bonus → score is just the catalog/profile sum (0 here).
        assert scored[0][1] == pytest.approx(0.0, abs=1e-6)


# ===================================================================
# load_profiles — cache + fallback
# ===================================================================

class TestLoadProfiles:
    def test_gateway_failure_returns_empty(self) -> None:
        """When Gateway is down and no cache, return empty dict."""
        from services.tts.voice_reranker import _profile_cache
        _profile_cache.clear()
        with patch("services.tts.voice_reranker.requests.get", side_effect=ConnectionError("test")):
            result = load_profiles("test_provider")
        assert result == {}

    def test_includes_voices_with_any_profile_field(self) -> None:
        """Any reranker-relevant field should cause inclusion in profiles map."""
        import json
        from unittest.mock import MagicMock
        from services.tts.voice_reranker import _profile_cache
        _profile_cache.clear()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "voices": [
                {"voice_id": "v_pitch", "pitch_level": "high"},
                {"voice_id": "v_delivery", "delivery_style": "narration"},
                {"voice_id": "v_texture", "texture_tags": ["soft"]},
                {"voice_id": "v_energy", "energy_level": "medium"},
                {"voice_id": "v_empty"},  # no fields → excluded
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("services.tts.voice_reranker.requests.get", return_value=mock_resp):
            result = load_profiles("test_provider2")

        assert "v_pitch" in result
        assert "v_delivery" in result
        assert "v_texture" in result
        assert "v_energy" in result
        assert "v_empty" not in result
