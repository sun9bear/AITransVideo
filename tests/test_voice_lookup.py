import json
from pathlib import Path

import pytest

from services.voice.voice_lookup import VoiceLookupError, lookup_voice_ids


def test_lookup_voice_ids_matches_by_speaker_name(tmp_path: Path) -> None:
    registry_path = tmp_path / "voice_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "speakers": {
                    "speaker_a": {
                        "speaker_name": "Dan Koe",
                        "default_voice_id": "voice_dan_001",
                    },
                    "speaker_b": {
                        "speaker_name": "Interviewer",
                        "default_voice_id": "voice_host_001",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    resolved = lookup_voice_ids(
        {"speaker_a": "dan koe", "speaker_b": "INTERVIEWER"},
        str(registry_path),
    )

    assert resolved == {"speaker_a": "voice_dan_001", "speaker_b": "voice_host_001"}


def test_lookup_voice_ids_uses_fallback_when_name_not_found(tmp_path: Path) -> None:
    registry_path = tmp_path / "voice_registry.json"
    registry_path.write_text(json.dumps({"speakers": {}}), encoding="utf-8")

    resolved = lookup_voice_ids(
        {"speaker_a": "Dan Koe", "speaker_b": "Guest"},
        str(registry_path),
        fallback_voice_a="voice_a_fallback",
        fallback_voice_b="voice_b_fallback",
    )

    assert resolved == {"speaker_a": "voice_a_fallback", "speaker_b": "voice_b_fallback"}


def test_lookup_voice_ids_raises_when_name_not_found_and_no_fallback(tmp_path: Path) -> None:
    registry_path = tmp_path / "voice_registry.json"
    registry_path.write_text(json.dumps({"speakers": {}}), encoding="utf-8")

    with pytest.raises(VoiceLookupError, match="speaker_b"):
        lookup_voice_ids(
            {"speaker_a": "Dan Koe", "speaker_b": "Guest"},
            str(registry_path),
            fallback_voice_a="voice_a_fallback",
            fallback_voice_b=None,
        )
