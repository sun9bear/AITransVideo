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
)


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
        # Should get childlike (0.07) + pitch (0.20) at minimum
        assert scored[0][1] >= W_CHILDLIKE + W_PITCH

    def test_weights_sum_to_one(self) -> None:
        """All weight constants should sum to 1.0."""
        total = W_AGE_EXACT + W_PERSONA_EXACT + W_PITCH + W_MATURITY + W_ENERGY + W_DELIVERY + W_CHILDLIKE + W_TEXTURE
        assert abs(total - 1.0) < 0.001


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
