from pathlib import Path
import re

import pytest

from services.voice.auto_clone import AutoCloneError, AutoVoiceCloner
import services.voice.auto_clone as auto_clone_module


def test_auto_voice_cloner_clone_voice_returns_voice_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_path = tmp_path / "sample.wav"
    sample_path.write_bytes(b"sample")

    class FakeCloneClient:
        def __init__(self, config):
            assert config.api_key == "test-key"

        def create_voice_clone(self, *, speaker_id: str, speaker_name: str, source_audio_path: Path):
            assert speaker_name == "Sam Altman"
            assert speaker_id.startswith("speaker_")
            assert source_audio_path == sample_path
            return type("CloneResult", (), {"voice_id": "moss_audio_demo"})()

    monkeypatch.setattr(auto_clone_module, "MiniMaxVoiceCloneClient", FakeCloneClient)

    voice_id = AutoVoiceCloner("test-key").clone_voice(str(sample_path), "Sam Altman")

    assert voice_id == "moss_audio_demo"


def test_auto_voice_cloner_clone_voice_uses_ascii_safe_speaker_id_for_non_ascii_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_path = tmp_path / "sample.wav"
    sample_path.write_bytes(b"sample")
    observed: dict[str, str] = {}

    class FakeCloneClient:
        def __init__(self, config):
            assert config.api_key == "test-key"

        def create_voice_clone(self, *, speaker_id: str, speaker_name: str, source_audio_path: Path):
            observed["speaker_id"] = speaker_id
            assert speaker_name == "沃伦·巴菲特"
            assert source_audio_path == sample_path
            return type("CloneResult", (), {"voice_id": "moss_audio_demo"})()

    monkeypatch.setattr(auto_clone_module, "MiniMaxVoiceCloneClient", FakeCloneClient)

    voice_id = AutoVoiceCloner("test-key").clone_voice(str(sample_path), "沃伦·巴菲特")

    assert voice_id == "moss_audio_demo"
    assert observed["speaker_id"].isascii()
    assert re.fullmatch(r"speaker_[a-z0-9_]+", observed["speaker_id"])


def test_auto_voice_cloner_passes_retry_and_timeout_settings_to_clone_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_path = tmp_path / "sample.wav"
    sample_path.write_bytes(b"sample")

    class FakeCloneClient:
        def __init__(self, config):
            assert config.api_key == "test-key"
            assert config.base_url == "https://clone.example"
            assert config.timeout_seconds == 240.0
            assert config.max_retries == 4
            assert config.retry_backoff_seconds == 1.5

        def create_voice_clone(self, *, speaker_id: str, speaker_name: str, source_audio_path: Path):
            del speaker_id, speaker_name
            assert source_audio_path == sample_path
            return type("CloneResult", (), {"voice_id": "retry_ready_voice"})()

    monkeypatch.setattr(auto_clone_module, "MiniMaxVoiceCloneClient", FakeCloneClient)

    voice_id = AutoVoiceCloner(
        "test-key",
        "https://clone.example",
        timeout_seconds=240.0,
        max_retries=4,
        retry_backoff_seconds=1.5,
    ).clone_voice(str(sample_path), "Sam Altman")

    assert voice_id == "retry_ready_voice"


def test_auto_voice_cloner_clone_voice_raises_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_path = tmp_path / "sample.wav"
    sample_path.write_bytes(b"sample")

    class FakeCloneClient:
        def __init__(self, config):
            del config

        def create_voice_clone(self, *, speaker_id: str, speaker_name: str, source_audio_path: Path):
            del speaker_id, speaker_name, source_audio_path
            raise RuntimeError("provider down")

    monkeypatch.setattr(auto_clone_module, "MiniMaxVoiceCloneClient", FakeCloneClient)

    with pytest.raises(AutoCloneError, match="自动克隆失败"):
        AutoVoiceCloner("test-key").clone_voice(str(sample_path), "Sam Altman")


def test_auto_voice_cloner_register_voice_uses_voice_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "voice_registry.json"
    registry_path.write_text('{"speakers": {}}', encoding="utf-8")
    observed: dict[str, object] = {}

    class FakeVoiceRegistry:
        def __init__(self, path: str):
            observed["path"] = path

        def load(self) -> dict[str, object]:
            return {"speakers": {}}

        def register_voice(self, speaker_id: str, **kwargs):
            observed["speaker_id"] = speaker_id
            observed["kwargs"] = kwargs

    monkeypatch.setattr(auto_clone_module, "VoiceRegistry", FakeVoiceRegistry)

    AutoVoiceCloner("test-key").register_voice(
        voice_id="moss_audio_demo",
        speaker_name="Sam Altman",
        sample_path=str(tmp_path / "sample.wav"),
        voice_registry_path=str(registry_path),
    )

    assert observed["path"] == str(registry_path)
    assert str(observed["speaker_id"]).startswith("speaker_")
    assert observed["kwargs"]["speaker_name"] == "Sam Altman"
    assert observed["kwargs"]["voice_id"] == "moss_audio_demo"
    assert observed["kwargs"]["provider"] == "minimax_voice_clone"
    assert observed["kwargs"]["set_default"] is True


def test_auto_voice_cloner_wait_until_ready_returns_true_when_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"count": 0}

    def fake_probe(voice_id: str) -> bool:
        assert voice_id == "moss_audio_demo"
        attempts["count"] += 1
        return attempts["count"] >= 2

    cloner = AutoVoiceCloner("test-key")
    monkeypatch.setattr(cloner, "_probe_voice_ready", fake_probe)
    monkeypatch.setattr(auto_clone_module.time, "sleep", lambda seconds: None)

    assert cloner.wait_until_ready("moss_audio_demo", max_wait_seconds=30, poll_interval_seconds=10) is True
    assert attempts["count"] == 2


def test_auto_voice_cloner_wait_until_ready_returns_false_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"count": 0}

    def fake_probe(voice_id: str) -> bool:
        assert voice_id == "moss_audio_demo"
        attempts["count"] += 1
        return False

    cloner = AutoVoiceCloner("test-key")
    monkeypatch.setattr(cloner, "_probe_voice_ready", fake_probe)
    monkeypatch.setattr(auto_clone_module.time, "sleep", lambda seconds: None)

    assert cloner.wait_until_ready("moss_audio_demo", max_wait_seconds=30, poll_interval_seconds=10) is False
    assert attempts["count"] == 4
