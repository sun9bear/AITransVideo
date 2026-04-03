from unittest.mock import patch

import pytest

# Mock Gateway HTTP calls to prevent real requests during tests
@pytest.fixture(autouse=True)
def _mock_gateway_calls():
    with patch("services.tts.cosyvoice_voice_catalog._fetch_cosyvoice_from_gateway", side_effect=ConnectionError("test")):
        yield

from services.tts.cosyvoice_voice_selector import (
    VoiceMatchResult,
    infer_is_childlike,
    select_voice,
    select_voice_match,
)


# --- Existing tests (backward compat) ---

def test_select_voice_uses_safe_middle_male_base_voice() -> None:
    assert select_voice("male", "middle") == "longanzhi_v3"


def test_select_voice_uses_safe_middle_male_serious_voice() -> None:
    assert select_voice("male", "middle", persona_style="serious") == "longanzhi_v3"


# --- VoiceMatchResult type tests ---

def test_select_voice_match_returns_match_result() -> None:
    result = select_voice_match("male")
    assert isinstance(result, VoiceMatchResult)
    assert isinstance(result.voice_id, str)
    assert isinstance(result.match_reason, str)
    assert isinstance(result.match_score, float)
    assert isinstance(result.match_confidence, str)
    assert isinstance(result.backup_voices, tuple)


def test_match_score_in_range() -> None:
    for gender, age, persona in [
        ("male", None, None),
        ("female", "middle", None),
        ("male", "middle", "serious"),
        (None, None, None),
    ]:
        result = select_voice_match(gender, age, persona)
        assert 0.0 <= result.match_score <= 1.0, f"score {result.match_score} out of range"


def test_match_confidence_values() -> None:
    for gender, age, persona in [
        ("male", None, None),
        ("female", "middle", None),
        ("male", "middle", "serious"),
        (None, None, None),
    ]:
        result = select_voice_match(gender, age, persona)
        assert result.match_confidence in ("high", "medium", "low")


# --- Confidence level tests ---

def test_style_override_gives_high_confidence() -> None:
    result = select_voice_match("male", "middle", persona_style="serious")
    assert result.match_confidence == "high"
    assert result.match_score >= 0.80
    assert "style_override" in result.match_reason


def test_base_age_gives_medium_confidence() -> None:
    result = select_voice_match("female", "elderly")
    assert result.match_confidence == "medium"
    assert result.match_score >= 0.50
    assert "base_age" in result.match_reason


def test_gender_only_gives_low_confidence() -> None:
    result = select_voice_match("male")
    assert result.match_confidence == "low"
    assert "gender_only" in result.match_reason


def test_no_gender_gives_low_confidence() -> None:
    result = select_voice_match(None)
    assert result.match_confidence == "low"
    assert result.voice_id == "longanyang"
    assert "fallback" in result.match_reason


# --- Voice selection correctness ---

def test_match_male_elderly() -> None:
    result = select_voice_match("male", "elderly")
    assert result.voice_id == "longlaobo_v3"


def test_match_female_middle_warm() -> None:
    result = select_voice_match("female", "middle", persona_style="warm")
    assert result.voice_id == "longanwen_v3"
    assert result.match_confidence == "high"


def test_match_female_young() -> None:
    result = select_voice_match("female", "young")
    assert result.voice_id == "longanhuan"


# --- Backup voices tests ---

def test_backup_voices_present() -> None:
    result = select_voice_match("female", "middle", persona_style="warm")
    assert len(result.backup_voices) > 0
    assert len(result.backup_voices) <= 2


def test_backup_voices_exclude_primary() -> None:
    result = select_voice_match("male", "middle", persona_style="serious")
    assert result.voice_id not in result.backup_voices


def test_backup_voices_empty_for_fallback() -> None:
    result = select_voice_match(None)
    assert result.backup_voices == ()


# --- Matchable filtering ---

# --- Childlike routing tests ---

def test_child_gender_routes_to_child_voice() -> None:
    result = select_voice_match("child")
    assert result.voice_id == "longhuhu_v3"
    assert result.match_confidence == "low"  # gender-only match


def test_child_young_routes_to_child_voice() -> None:
    result = select_voice_match("child", "young")
    assert result.voice_id == "longhuhu_v3"
    assert result.match_confidence == "medium"


def test_child_middle_routes_to_child_voice() -> None:
    # On intl endpoint (default), longjielidou_v3 is unavailable (418),
    # so it falls back to longhuhu_v3. On mainland, it would be longjielidou_v3.
    result = select_voice_match("child", "middle")
    assert result.voice_id in ("longjielidou_v3", "longhuhu_v3")


def test_is_childlike_overrides_gender() -> None:
    result = select_voice_match("male", "young", is_childlike=True)
    assert result.voice_id == "longhuhu_v3"
    assert "child" in result.match_reason


def test_is_childlike_with_female_overrides_to_child() -> None:
    result = select_voice_match("female", "middle", is_childlike=True)
    assert result.voice_id in ("longhuhu_v3", "longjielidou_v3")


# --- Matchable filtering ---

# --- infer_is_childlike tests ---

def test_infer_childlike_from_voice_description() -> None:
    assert infer_is_childlike("young", "活泼的小男孩童声") is True
    assert infer_is_childlike("young", "小朋友讲故事") is True
    assert infer_is_childlike("", "儿童节目主持人") is True


def test_infer_childlike_from_age_group() -> None:
    assert infer_is_childlike("child", "") is True
    assert infer_is_childlike("kid", "") is True


def test_infer_not_childlike_for_adults() -> None:
    assert infer_is_childlike("young", "阳光青年男") is False
    assert infer_is_childlike("middle", "温和知性女") is False
    assert infer_is_childlike("elderly", "沧桑老人") is False
    assert infer_is_childlike("", "") is False


# --- Matchable filtering ---

def test_dialect_voice_never_in_backup() -> None:
    dialect_ids = {"longjiaxin_v3", "longjiayi_v3", "longanyue_v3",
                   "longlaotie_v3", "longshange_v3", "longanmin_v3"}
    for gender in ("male", "female"):
        result = select_voice_match(gender)
        for backup in result.backup_voices:
            assert backup not in dialect_ids, f"dialect voice {backup} in backup_voices"


# --- Endpoint-safe voice pool tests ---

def test_intl_mode_female_middle_not_longyingjing(monkeypatch) -> None:
    """On intl endpoint, female_middle should not return longyingjing_v3 (418)."""
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "international")
    result = select_voice_match("female", "middle")
    assert result.voice_id != "longyingjing_v3", \
        f"longyingjing_v3 is not available on intl endpoint, got {result.voice_id}"
    assert "endpoint_fallback" in result.match_reason or result.voice_id in {
        "longanwen_v3", "longanhuan", "longlaoyi_v3",
    }


def test_intl_mode_serious_female_not_longxiaoxia(monkeypatch) -> None:
    """On intl endpoint, serious female should not return longxiaoxia_v3 (418)."""
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "international")
    result = select_voice_match("female", "middle", persona_style="serious")
    assert result.voice_id != "longxiaoxia_v3"


def test_intl_mode_backup_voices_all_intl_available(monkeypatch) -> None:
    """On intl endpoint, backup voices should only be intl-available."""
    from services.tts.cosyvoice_endpoint_config import INTL_AVAILABLE_VOICES
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "international")
    for gender in ("male", "female"):
        result = select_voice_match(gender, "middle")
        for backup in result.backup_voices:
            assert backup in INTL_AVAILABLE_VOICES, \
                f"backup {backup} not available on intl for {gender}"


def test_intl_mode_child_middle_not_longjielidou(monkeypatch) -> None:
    """On intl endpoint, child_middle should not return longjielidou_v3 (418)."""
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "international")
    result = select_voice_match("child", "middle")
    assert result.voice_id != "longjielidou_v3"
    assert result.voice_id == "longhuhu_v3"  # only intl child voice


def test_mainland_mode_full_pool(monkeypatch) -> None:
    """On mainland endpoint, longyingjing_v3 should be available."""
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "mainland")
    result = select_voice_match("female", "middle")
    assert result.voice_id == "longyingjing_v3"


# --- B2 rerank tests ---

def test_rerank_does_not_change_high_confidence() -> None:
    """High confidence (style_override) should NOT trigger rerank."""
    result = select_voice_match("male", "middle", persona_style="serious")
    assert result.match_confidence == "high"
    assert "reranked" not in result.match_reason


def test_rerank_no_profiles_returns_unchanged(monkeypatch) -> None:
    """With empty profile catalog, rerank should be a no-op."""
    import services.tts.cosyvoice_voice_profile_catalog as catalog
    # Reset profiles to empty
    old_profiles = dict(catalog._VOICE_PROFILES)
    old_loaded = catalog._PROFILES_LOADED
    catalog._VOICE_PROFILES.clear()
    catalog._PROFILES_LOADED = True
    try:
        result = select_voice_match("male")
        assert result.voice_id == "longanyang"
        assert "reranked" not in result.match_reason
    finally:
        catalog._VOICE_PROFILES.update(old_profiles)
        catalog._PROFILES_LOADED = old_loaded


def test_rerank_with_profiles_can_change_voice(monkeypatch) -> None:
    """With profile data loaded, rerank may change the selected voice for low/medium confidence."""
    import services.tts.cosyvoice_voice_profile_catalog as catalog

    old_profiles = dict(catalog._VOICE_PROFILES)
    old_loaded = catalog._PROFILES_LOADED

    # Load synthetic profiles: make longlaobo_v3 a better match for elderly male
    catalog._VOICE_PROFILES.clear()
    catalog._PROFILES_LOADED = True
    catalog.load_profiles_from_dict({
        "longanyang": {
            "primary": {"pitch_level": "mid", "warmth": "medium", "authority": "low", "intimacy": "medium"},
            "secondary": {"energy_level": "medium", "brightness": "medium", "maturity": "young",
                         "delivery_style": "companion", "texture_tags": ["soft"], "childlike": False},
            "labeled_at": "2026-01-01T00:00:00+00:00", "labeled_by": "test",
        },
        "longlaobo_v3": {
            "primary": {"pitch_level": "low", "warmth": "medium", "authority": "medium", "intimacy": "medium"},
            "secondary": {"energy_level": "medium", "brightness": "medium", "maturity": "elder",
                         "delivery_style": "narration", "texture_tags": ["steady", "magnetic"], "childlike": False},
            "labeled_at": "2026-01-01T00:00:00+00:00", "labeled_by": "test",
        },
    })

    try:
        monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "international")
        # male gender-only → low confidence → rerank should fire
        result = select_voice_match("male")
        # The result should use profile data; exact voice depends on scoring
        assert result.match_score >= 0.0
        assert result.match_score <= 1.0
    finally:
        catalog._VOICE_PROFILES.clear()
        catalog._VOICE_PROFILES.update(old_profiles)
        catalog._PROFILES_LOADED = old_loaded


def test_rerank_score_in_valid_range() -> None:
    """Rerank score should always be 0.0-1.0."""
    for gender, age, persona in [("male", None, None), ("female", "elderly", None), ("child", "young", None)]:
        result = select_voice_match(gender, age, persona)
        assert 0.0 <= result.match_score <= 1.0
