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
