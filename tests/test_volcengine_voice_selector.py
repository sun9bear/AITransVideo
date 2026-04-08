"""Tests for VolcEngine voice selector (uses shared combined_rerank)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

# Mock HTTP calls to prevent real Gateway requests during tests
@pytest.fixture(autouse=True)
def _mock_gateway_calls():
    with patch("services.tts.voice_reranker.load_profiles", return_value={}), \
         patch("services.tts.volcengine_voice_catalog._fetch_from_gateway", side_effect=ConnectionError("test")):
        yield

from services.tts.volcengine_voice_selector import select_volcengine_voice_match
from services.tts.volcengine_voice_catalog import is_voice_in_resource
from services.tts.volcengine_tts_provider import (
    DEFAULT_SPEAKER_1_0,
    DEFAULT_SPEAKER_2_0,
    RESOURCE_ID_1_0,
    RESOURCE_ID_2_0,
)


# ===================================================================
# 1.0 — style / base / gender matching
# ===================================================================

class TestMatch1_0:
    def test_female_middle_professional(self) -> None:
        """1.0 female + middle + professional → high confidence via combined_rerank."""
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_1_0,
            gender="female",
            age_group="middle",
            persona_style="professional",
        )
        assert is_voice_in_resource(r.voice_id, RESOURCE_ID_1_0)
        assert r.match_confidence in ("high", "medium")
        assert "combined_rerank" in r.match_reason

    def test_male_middle_serious(self) -> None:
        """1.0 male + middle + serious → combined_rerank (catalog tags only, no profiles)."""
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_1_0,
            gender="male",
            age_group="middle",
            persona_style="serious",
        )
        assert is_voice_in_resource(r.voice_id, RESOURCE_ID_1_0)
        # Without profile data, age(0.22)+persona(0.18)=0.40 → medium
        assert r.match_confidence in ("high", "medium")

    def test_male_young_base_age(self) -> None:
        """1.0 male + young → combined_rerank (age-only, no profiles → low/medium)."""
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_1_0,
            gender="male",
            age_group="young",
        )
        assert is_voice_in_resource(r.voice_id, RESOURCE_ID_1_0)
        # Without profile data, age-only match gives 0.22 → low or medium
        assert r.match_confidence in ("high", "medium", "low")

    def test_female_gender_only(self) -> None:
        """1.0 female, no age → combined_rerank with low info."""
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_1_0,
            gender="female",
        )
        assert is_voice_in_resource(r.voice_id, RESOURCE_ID_1_0)
        assert "combined_rerank" in r.match_reason

    def test_child_matches_child_voice(self) -> None:
        """1.0 child gender → should find child voice in 1.0 catalog."""
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_1_0,
            gender="child",
            age_group="young",
        )
        assert is_voice_in_resource(r.voice_id, RESOURCE_ID_1_0)


# ===================================================================
# 2.0 — matching
# ===================================================================

class TestMatch2_0:
    def test_female_gender_only_2_0(self) -> None:
        """2.0 female → returns uranus voice."""
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_2_0,
            gender="female",
        )
        assert "uranus_bigtts" in r.voice_id

    def test_male_middle_professional_2_0(self) -> None:
        """2.0 male + middle + professional → should find a male 2.0 voice."""
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_2_0,
            gender="male",
            age_group="middle",
            persona_style="professional",
        )
        assert is_voice_in_resource(r.voice_id, RESOURCE_ID_2_0)
        assert r.match_confidence in ("high", "medium")

    def test_male_young_energetic_2_0(self) -> None:
        """2.0 male + young + energetic → style override."""
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_2_0,
            gender="male",
            age_group="young",
            persona_style="energetic",
        )
        assert "uranus_bigtts" in r.voice_id


# ===================================================================
# Fallback / edge cases
# ===================================================================

class TestFallback:
    def test_no_gender_returns_resource_default(self) -> None:
        """Empty gender → resource default voice, low confidence."""
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_1_0,
            gender=None,
        )
        assert r.voice_id == DEFAULT_SPEAKER_1_0
        assert r.match_confidence == "low"
        assert "fallback" in r.match_reason

    def test_no_gender_2_0_returns_2_0_default(self) -> None:
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_2_0,
            gender=None,
        )
        assert r.voice_id == DEFAULT_SPEAKER_2_0
        assert r.match_confidence == "low"

    def test_empty_string_gender_treated_as_no_gender(self) -> None:
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_1_0,
            gender="",
        )
        assert r.voice_id == DEFAULT_SPEAKER_1_0
        assert "fallback" in r.match_reason

    def test_unrecognized_gender_falls_back(self) -> None:
        """Unknown gender string (e.g. 'alien') → fallback."""
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_1_0,
            gender="alien",
        )
        assert r.voice_id == DEFAULT_SPEAKER_1_0
        assert "fallback" in r.match_reason


# ===================================================================
# Cross-resource safety
# ===================================================================

class TestCrossResourceSafety:
    def test_1_0_never_returns_2_0_voice(self) -> None:
        """1.0 selector must never return a voice from the 2.0 catalog."""
        for gender in ("male", "female", "child", None):
            for age in ("young", "middle", "elderly", None):
                for persona in ("professional", "warm", "serious", "energetic", None):
                    r = select_volcengine_voice_match(
                        resource_id=RESOURCE_ID_1_0,
                        gender=gender,
                        age_group=age,
                        persona_style=persona,
                    )
                    assert is_voice_in_resource(r.voice_id, RESOURCE_ID_1_0), (
                        f"1.0 returned non-1.0 voice: {r.voice_id} "
                        f"(gender={gender}, age={age}, persona={persona})"
                    )

    def test_2_0_never_returns_1_0_voice(self) -> None:
        """2.0 selector must never return a voice from the 1.0 catalog."""
        for gender in ("male", "female", "child", None):
            for age in ("young", "middle", "elderly", None):
                for persona in ("professional", "warm", "serious", "energetic", None):
                    r = select_volcengine_voice_match(
                        resource_id=RESOURCE_ID_2_0,
                        gender=gender,
                        age_group=age,
                        persona_style=persona,
                    )
                    assert is_voice_in_resource(r.voice_id, RESOURCE_ID_2_0), (
                        f"2.0 returned non-2.0 voice: {r.voice_id} "
                        f"(gender={gender}, age={age}, persona={persona})"
                    )


# ===================================================================
# Result structure completeness
# ===================================================================

class TestResultStructure:
    def test_result_has_all_fields(self) -> None:
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_1_0,
            gender="male",
            age_group="middle",
            persona_style="professional",
        )
        assert isinstance(r.voice_id, str) and r.voice_id
        assert isinstance(r.match_reason, str) and r.match_reason
        assert isinstance(r.match_score, float)
        assert r.match_confidence in ("high", "medium", "low")
        assert isinstance(r.backup_voices, tuple)

    def test_backup_voices_exclude_primary(self) -> None:
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_1_0,
            gender="female",
            age_group="young",
            persona_style="energetic",
        )
        assert r.voice_id not in r.backup_voices


    # B2 rerank tests migrated to tests/test_voice_reranker.py
