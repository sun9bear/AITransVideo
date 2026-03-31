from services.tts.tts_strategy import get_fallback_provider


def test_cosyvoice_has_no_fallback_provider() -> None:
    assert get_fallback_provider("cosyvoice", voice_clone_enabled=False) is None
