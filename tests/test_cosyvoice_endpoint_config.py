import os

import pytest

from services.tts.cosyvoice_endpoint_config import (
    DEFAULT_OFFLINE_MODE,
    DEFAULT_RUNTIME_MODE,
    INTL_AVAILABLE_VOICES,
    INTL_WS_URL,
    MAINLAND_WS_URL,
    get_offline_endpoint_mode,
    get_runtime_endpoint_mode,
    get_ws_url,
    is_voice_available,
)


def test_default_runtime_mode_is_international() -> None:
    assert DEFAULT_RUNTIME_MODE == "international"


def test_default_offline_mode_is_mainland() -> None:
    assert DEFAULT_OFFLINE_MODE == "mainland"


def test_get_runtime_endpoint_mode_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", raising=False)
    assert get_runtime_endpoint_mode() == "international"


def test_get_offline_endpoint_mode_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COSYVOICE_OFFLINE_ENDPOINT_MODE", raising=False)
    assert get_offline_endpoint_mode() == "mainland"


def test_env_override_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "mainland")
    assert get_runtime_endpoint_mode() == "mainland"


def test_env_override_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSYVOICE_OFFLINE_ENDPOINT_MODE", "international")
    assert get_offline_endpoint_mode() == "international"


def test_env_alias_intl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "intl")
    assert get_runtime_endpoint_mode() == "international"


def test_get_ws_url_international() -> None:
    assert get_ws_url("international") == INTL_WS_URL


def test_get_ws_url_mainland() -> None:
    assert get_ws_url("mainland") == MAINLAND_WS_URL


def test_get_ws_url_intl_alias() -> None:
    assert get_ws_url("intl") == INTL_WS_URL


# --- Voice availability ---

def test_intl_available_voices_count() -> None:
    assert len(INTL_AVAILABLE_VOICES) == 10


def test_intl_known_voices() -> None:
    assert "longanyang" in INTL_AVAILABLE_VOICES
    assert "longanhuan" in INTL_AVAILABLE_VOICES
    assert "longhuhu_v3" in INTL_AVAILABLE_VOICES


def test_intl_unavailable_voices() -> None:
    assert "longyingjing_v3" not in INTL_AVAILABLE_VOICES
    assert "longxiaoxia_v3" not in INTL_AVAILABLE_VOICES
    assert "longcheng_v3" not in INTL_AVAILABLE_VOICES
    assert "longshuo_v3" not in INTL_AVAILABLE_VOICES


def test_is_voice_available_intl() -> None:
    assert is_voice_available("longanyang", "international") is True
    assert is_voice_available("longyingjing_v3", "international") is False


# --- API key selection ---

def test_intl_mode_uses_intl_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.tts.cosyvoice_endpoint_config import get_api_key_for_mode
    monkeypatch.setenv("DASHSCOPE_INTERNATIONAL_API_KEY", "sk-intl")
    monkeypatch.setenv("DASHSCOPE_MAINLAND_API_KEY", "sk-cn")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-generic")
    assert get_api_key_for_mode("international") == "sk-intl"


def test_mainland_mode_uses_mainland_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.tts.cosyvoice_endpoint_config import get_api_key_for_mode
    monkeypatch.setenv("DASHSCOPE_INTERNATIONAL_API_KEY", "sk-intl")
    monkeypatch.setenv("DASHSCOPE_MAINLAND_API_KEY", "sk-cn")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-generic")
    assert get_api_key_for_mode("mainland") == "sk-cn"


def test_missing_specific_key_falls_back_to_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.tts.cosyvoice_endpoint_config import get_api_key_for_mode
    monkeypatch.delenv("DASHSCOPE_INTERNATIONAL_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_MAINLAND_API_KEY", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-generic")
    assert get_api_key_for_mode("international") == "sk-generic"
    assert get_api_key_for_mode("mainland") == "sk-generic"


def test_no_key_at_all_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.tts.cosyvoice_endpoint_config import get_api_key_for_mode
    monkeypatch.delenv("DASHSCOPE_INTERNATIONAL_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_MAINLAND_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert get_api_key_for_mode("international") == ""
    assert get_api_key_for_mode("mainland") == ""


def test_is_voice_available_mainland_all() -> None:
    assert is_voice_available("longanyang", "mainland") is True
    assert is_voice_available("longyingjing_v3", "mainland") is True
    assert is_voice_available("longshuo_v3", "mainland") is True


# --- Admin settings persistence ---

def test_admin_settings_persistence_roundtrip(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.tts import cosyvoice_endpoint_config as cfg
    settings_file = tmp_path / "admin_settings.json"
    monkeypatch.setattr(cfg, "_ADMIN_SETTINGS_PATH", settings_file)
    monkeypatch.delenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", raising=False)
    monkeypatch.delenv("COSYVOICE_OFFLINE_ENDPOINT_MODE", raising=False)

    # Before persistence: defaults
    assert cfg.get_runtime_endpoint_mode() == "international"
    assert cfg.get_offline_endpoint_mode() == "mainland"

    # Persist changes
    cfg.set_runtime_endpoint_mode("mainland")
    cfg.set_offline_endpoint_mode("international")

    # After persistence: reads from file
    assert cfg.get_runtime_endpoint_mode() == "mainland"
    assert cfg.get_offline_endpoint_mode() == "international"


def test_env_overrides_admin_settings(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.tts import cosyvoice_endpoint_config as cfg
    settings_file = tmp_path / "admin_settings.json"
    monkeypatch.setattr(cfg, "_ADMIN_SETTINGS_PATH", settings_file)

    # Write mainland to file
    cfg.set_runtime_endpoint_mode("mainland")
    # But env says international
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "international")
    # Env wins
    assert cfg.get_runtime_endpoint_mode() == "international"


def test_admin_settings_preserves_other_fields(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from services.tts import cosyvoice_endpoint_config as cfg
    settings_file = tmp_path / "admin_settings.json"
    monkeypatch.setattr(cfg, "_ADMIN_SETTINGS_PATH", settings_file)

    # Pre-existing settings
    settings_file.write_text(json.dumps({"tts_provider": "minimax", "express_tts_provider": "cosyvoice"}))

    cfg.set_runtime_endpoint_mode("mainland")

    data = json.loads(settings_file.read_text())
    assert data["tts_provider"] == "minimax"
    assert data["express_tts_provider"] == "cosyvoice"
    assert data["cosyvoice_runtime_endpoint_mode"] == "mainland"
