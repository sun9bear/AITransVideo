from services.tts.tts_strategy import VALID_PROVIDERS, get_fallback_provider, get_provider_rpm


def test_cosyvoice_has_no_fallback_provider() -> None:
    assert get_fallback_provider("cosyvoice", voice_clone_enabled=False) is None


def test_volcengine_is_valid_provider() -> None:
    assert "volcengine" in VALID_PROVIDERS


def test_volcengine_has_rpm_limit() -> None:
    rpm = get_provider_rpm("volcengine")
    assert rpm == 60


def test_volcengine_fallback_is_cosyvoice() -> None:
    assert get_fallback_provider("volcengine", voice_clone_enabled=False) == "cosyvoice"


# --- PR-E slice 3: fail-closed fallback (no CosyVoice for a non-zh target) ---

def test_zh_target_keeps_cosyvoice_fallback_byte_identical() -> None:
    # Default (None) and explicit zh-CN both keep the legacy CosyVoice fallback.
    assert get_fallback_provider("volcengine") == "cosyvoice"
    assert get_fallback_provider("volcengine", target_language="zh-CN") == "cosyvoice"
    assert get_fallback_provider("minimax", voice_clone_enabled=False) == "cosyvoice"
    assert (
        get_fallback_provider("minimax", voice_clone_enabled=False, target_language="zh-CN")
        == "cosyvoice"
    )


def test_non_zh_target_drops_cosyvoice_fallback() -> None:
    # CosyVoice is Chinese-only → never fall back to it for an en (or any non-zh) dub.
    assert get_fallback_provider("volcengine", target_language="en") is None
    assert (
        get_fallback_provider("minimax", voice_clone_enabled=False, target_language="en") is None
    )
    # Clone path is still no-fallback regardless of target.
    assert (
        get_fallback_provider("minimax", voice_clone_enabled=True, target_language="en") is None
    )
