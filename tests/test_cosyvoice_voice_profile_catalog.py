from services.tts.cosyvoice_voice_profile_catalog import (
    VALID_DELIVERY_STYLES,
    VALID_MATURITY,
    VALID_PITCH_LEVELS,
    VALID_TEXTURE_TAGS,
    VALID_THREE_LEVELS,
    VoicePrimaryProfile,
    VoiceProfile,
    VoiceSecondaryProfile,
    get_voice_profile,
    list_profiled_voices,
    load_profiles_from_dict,
)


def _make_valid_profile(voice_id: str = "longanyang") -> VoiceProfile:
    return VoiceProfile(
        voice_id=voice_id,
        primary=VoicePrimaryProfile(
            pitch_level="mid", warmth="medium", authority="medium", intimacy="medium",
        ),
        secondary=VoiceSecondaryProfile(
            energy_level="medium", brightness="medium", maturity="adult",
            delivery_style="companion", texture_tags=("magnetic",), childlike=False,
        ),
        labeled_at="2026-03-30T12:00:00+00:00",
        labeled_by="manual",
    )


def test_valid_profile_has_no_errors() -> None:
    p = _make_valid_profile()
    assert p.validate() == []


def test_invalid_pitch_level() -> None:
    p = VoicePrimaryProfile(pitch_level="invalid", warmth="low", authority="low", intimacy="low")
    errors = p.validate()
    assert any("pitch_level" in e for e in errors)


def test_invalid_maturity() -> None:
    s = VoiceSecondaryProfile(
        energy_level="low", brightness="low", maturity="baby",
        delivery_style="narration", texture_tags=("soft",), childlike=False,
    )
    errors = s.validate()
    assert any("maturity" in e for e in errors)


def test_invalid_delivery_style() -> None:
    s = VoiceSecondaryProfile(
        energy_level="low", brightness="low", maturity="adult",
        delivery_style="singer", texture_tags=("soft",), childlike=False,
    )
    errors = s.validate()
    assert any("delivery_style" in e for e in errors)


def test_texture_tags_too_many() -> None:
    s = VoiceSecondaryProfile(
        energy_level="low", brightness="low", maturity="adult",
        delivery_style="narration", texture_tags=("soft", "crisp", "magnetic", "husky"), childlike=False,
    )
    errors = s.validate()
    assert any("texture_tags" in e for e in errors)


def test_texture_tags_empty() -> None:
    s = VoiceSecondaryProfile(
        energy_level="low", brightness="low", maturity="adult",
        delivery_style="narration", texture_tags=(), childlike=False,
    )
    errors = s.validate()
    assert any("texture_tags" in e for e in errors)


def test_load_profiles_from_dict() -> None:
    data = {
        "longanyang": {
            "primary": {"pitch_level": "mid", "warmth": "high", "authority": "low", "intimacy": "high"},
            "secondary": {
                "energy_level": "high", "brightness": "high", "maturity": "young",
                "delivery_style": "companion", "texture_tags": ["magnetic", "crisp"], "childlike": False,
            },
            "labeled_at": "2026-03-30T00:00:00+00:00",
            "labeled_by": "gemini-2.5-flash",
        }
    }
    count = load_profiles_from_dict(data)
    assert count == 1
    p = get_voice_profile("longanyang")
    assert p is not None
    assert p.primary.pitch_level == "mid"
    assert p.secondary.delivery_style == "companion"
    assert p.secondary.texture_tags == ("magnetic", "crisp")


def test_list_profiled_voices_after_load() -> None:
    voices = list_profiled_voices()
    assert "longanyang" in voices


def test_get_voice_profile_nonexistent() -> None:
    assert get_voice_profile("nonexistent_voice_xyz") is None


def test_childlike_string_false_not_truthy() -> None:
    """Ensure JSON string 'false' is parsed as False, not True."""
    data = {
        "test_voice": {
            "primary": {"pitch_level": "mid", "warmth": "low", "authority": "low", "intimacy": "low"},
            "secondary": {
                "energy_level": "low", "brightness": "low", "maturity": "adult",
                "delivery_style": "narration", "texture_tags": ["steady"],
                "childlike": "false",  # string, not bool
            },
            "labeled_at": "2026-01-01T00:00:00+00:00",
            "labeled_by": "test",
        }
    }
    load_profiles_from_dict(data)
    p = get_voice_profile("test_voice")
    assert p is not None
    assert p.secondary.childlike is False


def test_childlike_string_true_parsed_correctly() -> None:
    data = {
        "test_voice_2": {
            "primary": {"pitch_level": "high", "warmth": "high", "authority": "low", "intimacy": "high"},
            "secondary": {
                "energy_level": "high", "brightness": "high", "maturity": "child",
                "delivery_style": "storyteller", "texture_tags": ["airy"],
                "childlike": "true",
            },
            "labeled_at": "2026-01-01T00:00:00+00:00",
            "labeled_by": "test",
        }
    }
    load_profiles_from_dict(data)
    p = get_voice_profile("test_voice_2")
    assert p is not None
    assert p.secondary.childlike is True


def test_childlike_bool_true_preserved() -> None:
    data = {
        "test_voice_3": {
            "primary": {"pitch_level": "high", "warmth": "high", "authority": "low", "intimacy": "high"},
            "secondary": {
                "energy_level": "high", "brightness": "high", "maturity": "child",
                "delivery_style": "storyteller", "texture_tags": ["airy"],
                "childlike": True,
            },
            "labeled_at": "2026-01-01T00:00:00+00:00",
            "labeled_by": "test",
        }
    }
    load_profiles_from_dict(data)
    p = get_voice_profile("test_voice_3")
    assert p is not None
    assert p.secondary.childlike is True


def test_valid_enum_sets_not_empty() -> None:
    assert len(VALID_PITCH_LEVELS) == 3
    assert len(VALID_THREE_LEVELS) == 3
    assert len(VALID_MATURITY) == 4
    assert len(VALID_DELIVERY_STYLES) == 6
    assert len(VALID_TEXTURE_TAGS) == 6
