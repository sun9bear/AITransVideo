from services.tts.cosyvoice_instruction_enhancer import (
    INSTRUCT_ENABLED,
    EnhancedVoiceResult,
    _INSTRUCT_CAPABLE_VOICES,
    enhance_voice_selection,
)


def test_static_gate_is_false() -> None:
    assert INSTRUCT_ENABLED is False


def test_enhancer_returns_enhanced_result() -> None:
    result = enhance_voice_selection("male", "middle", persona_style="serious")
    assert isinstance(result, EnhancedVoiceResult)


def test_instruction_always_none_in_b1() -> None:
    for gender, age, persona in [
        ("male", None, None),
        ("female", "middle", "warm"),
        ("male", "young", "energetic"),
        (None, None, None),
    ]:
        result = enhance_voice_selection(gender, age, persona)
        assert result.instruction is None


def test_instruct_supported_for_capable_voices() -> None:
    # longanyang is in _INSTRUCT_CAPABLE_VOICES and is the default male voice
    result = enhance_voice_selection("male")
    assert result.voice_id == "longanyang"
    assert result.instruct_supported is True


def test_instruct_not_supported_for_other_voices() -> None:
    # longanzhi_v3 is not in _INSTRUCT_CAPABLE_VOICES
    result = enhance_voice_selection("male", "middle", persona_style="serious")
    assert result.voice_id == "longanzhi_v3"
    assert result.instruct_supported is False


def test_enhancer_delegates_to_selector() -> None:
    result = enhance_voice_selection("female", "elderly")
    assert result.voice_id == "longlaoyi_v3"
    assert result.match_confidence == "medium"
    assert "base_age" in result.match_reason


def test_enhancer_match_score_propagated() -> None:
    result = enhance_voice_selection("male", "middle", persona_style="warm")
    assert result.match_score >= 0.80
    assert result.match_confidence == "high"


def test_enhancer_backup_voices_propagated() -> None:
    result = enhance_voice_selection("female", "middle", persona_style="warm")
    assert isinstance(result.backup_voices, tuple)
    assert result.voice_id not in result.backup_voices


def test_enhancer_childlike_routes_to_child_voice() -> None:
    result = enhance_voice_selection("male", "young", is_childlike=True)
    assert result.voice_id == "longhuhu_v3"
    assert result.instruct_supported is True  # longhuhu_v3 is instruct-capable


def test_instruct_capable_voices_set_not_empty() -> None:
    assert len(_INSTRUCT_CAPABLE_VOICES) >= 3
