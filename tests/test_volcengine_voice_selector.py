"""Tests for VolcEngine B1 baseline voice selector (B4)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

# Mock HTTP calls to prevent real Gateway requests during tests
@pytest.fixture(autouse=True)
def _mock_gateway_calls():
    with patch("services.tts.volcengine_voice_selector._load_profiles", return_value={}), \
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
        """1.0 female + middle + professional → style override (high confidence)."""
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_1_0,
            gender="female",
            age_group="middle",
            persona_style="professional",
        )
        assert is_voice_in_resource(r.voice_id, RESOURCE_ID_1_0)
        assert r.match_confidence == "high"
        assert r.match_score >= 0.8
        assert "style_override" in r.match_reason

    def test_male_middle_serious(self) -> None:
        """1.0 male + middle + serious → style override."""
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_1_0,
            gender="male",
            age_group="middle",
            persona_style="serious",
        )
        assert is_voice_in_resource(r.voice_id, RESOURCE_ID_1_0)
        assert r.match_confidence == "high"

    def test_male_young_base_age(self) -> None:
        """1.0 male + young → base_age match."""
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_1_0,
            gender="male",
            age_group="young",
        )
        assert is_voice_in_resource(r.voice_id, RESOURCE_ID_1_0)
        assert r.match_confidence in ("high", "medium")
        assert r.match_score >= 0.5

    def test_female_gender_only(self) -> None:
        """1.0 female, no age → gender_only match."""
        r = select_volcengine_voice_match(
            resource_id=RESOURCE_ID_1_0,
            gender="female",
        )
        assert is_voice_in_resource(r.voice_id, RESOURCE_ID_1_0)
        assert "gender_only" in r.match_reason

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


# ===================================================================
# B2 Rerank — profile-based reranking
# ===================================================================

from services.tts.volcengine_voice_selector import _try_rerank_with_profiles


class TestRerank:
    """Test _try_rerank_with_profiles directly with mock profile data.

    These tests override the autouse _mock_gateway_calls fixture by
    patching _load_profiles to return controlled profile data, so the
    rerank scoring logic is truly exercised.
    """

    def test_rerank_no_profiles_returns_unchanged(self) -> None:
        """No profile data → B1 result unchanged."""
        # autouse fixture already returns {} from _load_profiles
        best, remaining = _try_rerank_with_profiles(
            "voice_a", ("voice_b", "voice_c"),
            speaker_gender="female", speaker_age="young",
        )
        assert best == "voice_a"
        assert remaining == ("voice_b", "voice_c")

    def test_rerank_with_profiles_can_promote_backup(self) -> None:
        """Backup scores higher on all 4 dimensions → gets promoted."""
        profiles = {
            # primary: no matching dimensions
            "voice_a": {
                "maturity": "elder",      # mismatch (speaker is young)
                "childlike": True,        # mismatch (speaker is not child)
                "pitch_level": "low",     # mismatch (female prefers mid/high)
                "texture_tags": ["husky"],  # mismatch (warm → soft/magnetic)
            },
            # backup: perfect match on all dimensions
            "voice_b": {
                "maturity": "young",      # match
                "childlike": False,       # match (not child)
                "pitch_level": "high",    # match (female)
                "texture_tags": ["soft", "magnetic"],  # match (warm)
            },
        }
        with patch("services.tts.volcengine_voice_selector._load_profiles", return_value=profiles):
            best, remaining = _try_rerank_with_profiles(
                "voice_a", ("voice_b",),
                speaker_gender="female", speaker_age="young",
                speaker_persona="warm", resource_id="seed-tts-1.0",
            )
        assert best == "voice_b", f"Expected voice_b to be promoted, got {best}"
        assert "voice_a" in remaining

    def test_rerank_keeps_primary_when_score_not_better(self) -> None:
        """Primary already matches well → stays primary."""
        profiles = {
            "voice_a": {
                "maturity": "young",
                "childlike": False,
                "pitch_level": "high",
                "texture_tags": ["soft"],
            },
            "voice_b": {
                "maturity": "elder",
                "childlike": True,
                "pitch_level": "low",
                "texture_tags": ["husky"],
            },
        }
        with patch("services.tts.volcengine_voice_selector._load_profiles", return_value=profiles):
            best, remaining = _try_rerank_with_profiles(
                "voice_a", ("voice_b",),
                speaker_gender="female", speaker_age="young",
                speaker_persona="warm", resource_id="seed-tts-1.0",
            )
        assert best == "voice_a"

    def test_rerank_maturity_and_childlike_scoring(self) -> None:
        """Child speaker: childlike=True and maturity=child should score highest."""
        profiles = {
            "voice_a": {
                "maturity": "adult",
                "childlike": False,
                "pitch_level": "high",
                "texture_tags": [],
            },
            "voice_b": {
                "maturity": "child",
                "childlike": True,
                "pitch_level": "high",
                "texture_tags": [],
            },
        }
        with patch("services.tts.volcengine_voice_selector._load_profiles", return_value=profiles):
            best, _ = _try_rerank_with_profiles(
                "voice_a", ("voice_b",),
                speaker_gender="child", speaker_age="child",
                resource_id="seed-tts-1.0",
            )
        # voice_b has maturity=child (0.30) + childlike=True (0.20) = 0.50
        # voice_a has maturity mismatch (0) + childlike mismatch (0) = 0.30 (pitch only)
        assert best == "voice_b"

    def test_rerank_remaining_backups_order(self) -> None:
        """After rerank, remaining backups are ordered by descending score."""
        profiles = {
            "voice_a": {"maturity": "young", "childlike": False, "pitch_level": "low", "texture_tags": []},
            "voice_b": {"maturity": "young", "childlike": False, "pitch_level": "high", "texture_tags": ["soft"]},
            "voice_c": {"maturity": "young", "childlike": False, "pitch_level": "high", "texture_tags": []},
        }
        with patch("services.tts.volcengine_voice_selector._load_profiles", return_value=profiles):
            best, remaining = _try_rerank_with_profiles(
                "voice_a", ("voice_b", "voice_c"),
                speaker_gender="female", speaker_age="young",
                speaker_persona="warm", resource_id="seed-tts-1.0",
            )
        # voice_b: maturity(0.30) + pitch(0.30) + texture(0.20) = 0.80
        # voice_c: maturity(0.30) + pitch(0.30) = 0.60
        # voice_a: maturity(0.30) + pitch mismatch = 0.30
        assert best == "voice_b"
        assert remaining == ("voice_c", "voice_a")
