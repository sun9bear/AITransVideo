import json
from pathlib import Path

from services.voice_registry import VoiceRegistry, VoiceResolver


def test_voice_registry_registers_cloned_voice(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))

    profile = registry.register_voice(
        "speaker_host",
        speaker_name="Host",
        voice_id="clone_host_001",
        voice_type="cloned",
        provider="minimax",
        label="Host Clone",
        created_at="2026-03-13T08:00:00+00:00",
        source_audio_path="D:/voices/host.wav",
        notes="primary cloned voice",
    )

    assert profile.speaker_id == "speaker_host"
    assert profile.speaker_name == "Host"
    assert len(profile.voices) == 1
    assert profile.voices[0].voice_id == "clone_host_001"
    assert profile.voices[0].voice_type == "cloned"
    assert profile.voices[0].provider == "minimax"
    assert profile.voices[0].source_audio_path == "D:/voices/host.wav"
    assert profile.voices[0].verification_status == "unverified"
    assert profile.voices[0].last_verification_success is None


def test_voice_registry_registers_builtin_voice(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))

    profile = registry.register_voice(
        "speaker_guest",
        speaker_name="Guest",
        voice_id="builtin_guest_001",
        voice_type="builtin",
        provider="minimax",
        label="Guest Builtin",
        created_at="2026-03-13T08:05:00+00:00",
    )

    assert profile.speaker_id == "speaker_guest"
    assert len(profile.voices) == 1
    assert profile.voices[0].voice_id == "builtin_guest_001"
    assert profile.voices[0].voice_type == "builtin"


def test_voice_registry_sets_default_voice(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    resolver = VoiceResolver(registry)
    registry.register_voice(
        "speaker_host",
        speaker_name="Host",
        voice_id="clone_host_001",
        voice_type="cloned",
        provider="minimax",
        label="Host Clone",
        created_at="2026-03-13T08:00:00+00:00",
    )

    profile = registry.set_default_voice("speaker_host", "clone_host_001")
    resolution = resolver.resolve("speaker_host")

    assert profile.default_voice_id == "clone_host_001"
    assert profile.default_voice_type == "cloned"
    assert resolution.status == "resolved"
    assert resolution.source == "speaker_default_cloned"
    assert resolution.voice_id == "clone_host_001"
    assert resolver.resolve_voice_id("speaker_host") == "clone_host_001"


def test_voice_registry_uses_speaker_id_as_unique_primary_key(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    registry.register_voice(
        "speaker_host",
        speaker_name="Host",
        voice_id="clone_host_001",
        voice_type="cloned",
        provider="minimax",
        label="Host Clone",
        created_at="2026-03-13T08:00:00+00:00",
    )
    profile = registry.register_voice(
        "speaker_host",
        speaker_name="Host Updated",
        voice_id="builtin_host_001",
        voice_type="builtin",
        provider="minimax",
        label="Host Builtin",
        created_at="2026-03-13T08:10:00+00:00",
    )
    loaded_payload = json.loads((tmp_path / "voice_registry.json").read_text(encoding="utf-8"))

    assert len(loaded_payload["speakers"]) == 1
    assert profile.speaker_name == "Host Updated"
    assert {voice.voice_id for voice in profile.voices} == {"clone_host_001", "builtin_host_001"}


def test_voice_resolver_priority_prefers_speaker_default_cloned_then_builtin(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    resolver = VoiceResolver(registry)
    registry.set_project_default_builtin_voice(
        voice_id="project_builtin_001",
        provider="minimax",
        label="Project Builtin",
        created_at="2026-03-13T08:15:00+00:00",
    )

    registry.register_voice(
        "speaker_clone",
        speaker_name="Clone Speaker",
        voice_id="clone_voice_001",
        voice_type="cloned",
        provider="minimax",
        label="Clone Voice",
        created_at="2026-03-13T08:16:00+00:00",
        set_default=True,
    )
    registry.register_voice(
        "speaker_builtin",
        speaker_name="Builtin Speaker",
        voice_id="builtin_voice_001",
        voice_type="builtin",
        provider="minimax",
        label="Builtin Voice",
        created_at="2026-03-13T08:17:00+00:00",
        set_default=True,
    )

    clone_resolution = resolver.resolve("speaker_clone")
    builtin_resolution = resolver.resolve("speaker_builtin")

    assert clone_resolution.source == "speaker_default_cloned"
    assert clone_resolution.voice_id == "clone_voice_001"
    assert builtin_resolution.source == "speaker_default_builtin"
    assert builtin_resolution.voice_id == "builtin_voice_001"


def test_voice_resolver_falls_back_to_project_default_builtin(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    resolver = VoiceResolver(registry)
    registry.set_project_default_builtin_voice(
        voice_id="project_builtin_001",
        provider="minimax",
        label="Project Builtin",
        created_at="2026-03-13T08:20:00+00:00",
    )

    resolution = resolver.resolve("speaker_without_binding")

    assert resolution.status == "resolved"
    assert resolution.source == "project_default_builtin"
    assert resolution.voice_id == "project_builtin_001"
    assert resolution.voice_type == "builtin"


def test_voice_resolver_returns_unresolved_when_no_binding_exists(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    resolver = VoiceResolver(registry)
    registry.register_voice(
        "speaker_host",
        speaker_name="Host",
        voice_id="clone_host_001",
        voice_type="cloned",
        provider="minimax",
        label="Host Clone",
        created_at="2026-03-13T08:25:00+00:00",
    )

    resolution = resolver.resolve("speaker_host")

    assert resolution.status == "unresolved"
    assert resolution.source == "unresolved"
    assert resolution.voice_id is None
    assert resolver.resolve_voice_id("speaker_host") is None


def test_voice_registry_round_trips_json_to_disk(tmp_path: Path) -> None:
    registry_path = tmp_path / "voice_registry.json"
    registry = VoiceRegistry(str(registry_path))
    registry.register_voice(
        "speaker_host",
        speaker_name="Host",
        voice_id="clone_host_001",
        voice_type="cloned",
        provider="minimax",
        label="Host Clone",
        created_at="2026-03-13T08:30:00+00:00",
        set_default=True,
    )
    registry.set_project_default_builtin_voice(
        voice_id="project_builtin_001",
        provider="minimax",
        label="Project Builtin",
        created_at="2026-03-13T08:31:00+00:00",
    )

    reloaded_registry = VoiceRegistry(str(registry_path))
    profile = reloaded_registry.get_speaker_profile("speaker_host")
    project_default = reloaded_registry.get_project_default_builtin_voice()
    raw_payload = json.loads(registry_path.read_text(encoding="utf-8"))

    assert profile is not None
    assert profile.default_voice_id == "clone_host_001"
    assert project_default is not None
    assert project_default.voice_id == "project_builtin_001"
    assert raw_payload["speakers"]["speaker_host"]["speaker_name"] == "Host"
    assert raw_payload["speakers"]["speaker_host"]["default_voice_type"] == "cloned"
    assert raw_payload["project_defaults"]["default_builtin_voice"]["voice_type"] == "builtin"


def test_voice_registry_records_voice_verification_status(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    registry.register_voice(
        "speaker_host",
        speaker_name="Host",
        voice_id="clone_host_001",
        voice_type="cloned",
        provider="minimax",
        label="Host Clone",
        created_at="2026-03-13T08:30:00+00:00",
        set_default=True,
    )

    profile = registry.record_voice_verification(
        "speaker_host",
        "clone_host_001",
        success=True,
        verified_at="2026-03-13T09:00:00+00:00",
        audio_path="D:/AutoVideoTrans/voice_bank/verification_audio/speaker_host/clone_host_001.wav",
    )

    assert profile.voices[0].verification_status == "verified"
    assert profile.voices[0].last_verified_at == "2026-03-13T09:00:00+00:00"
    assert profile.voices[0].last_verification_success is True
    assert profile.voices[0].last_verification_audio_path.endswith("clone_host_001.wav")


def test_voice_registry_records_failed_voice_verification_without_losing_voice(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    registry.register_voice(
        "speaker_host",
        speaker_name="Host",
        voice_id="clone_host_001",
        voice_type="cloned",
        provider="minimax",
        label="Host Clone",
        created_at="2026-03-13T08:30:00+00:00",
    )

    profile = registry.record_voice_verification(
        "speaker_host",
        "clone_host_001",
        success=False,
        error_message="provider timed out",
    )

    assert profile.voices[0].verification_status == "failed"
    assert profile.voices[0].last_verification_success is False
    assert profile.voices[0].last_verification_error == "provider timed out"


def test_voice_registry_set_default_voice_auto_registers_official_cosyvoice_builtin(
    tmp_path: Path,
) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    resolver = VoiceResolver(registry)
    registry.register_voice(
        "speaker_host",
        speaker_name="Host",
        voice_id="clone_host_001",
        voice_type="cloned",
        provider="minimax",
        label="Host Clone",
        created_at="2026-03-13T08:30:00+00:00",
    )

    profile = registry.set_default_voice("speaker_host", "longshu_v3")
    resolution = resolver.resolve("speaker_host", tts_provider="cosyvoice", platform="dashscope")

    assert profile.default_voice_id == "longshu_v3"
    assert profile.default_voice_type == "builtin"
    assert any(voice.voice_id == "longshu_v3" and voice.tts_provider == "cosyvoice" for voice in profile.voices)
    assert resolution.source == "speaker_default_builtin"
    assert resolution.voice_id == "longshu_v3"
