from __future__ import annotations

import json
from pathlib import Path


class VoiceLookupError(Exception):
    pass


def lookup_voice_ids(
    speaker_names: dict[str, str],
    voice_registry_path: str,
    fallback_voice_a: str | None = None,
    fallback_voice_b: str | None = None,
) -> dict[str, str]:
    registry_path = Path(voice_registry_path).expanduser().resolve(strict=False)
    registry_payload: dict[str, object] = {}
    if registry_path.exists():
        try:
            loaded_payload = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VoiceLookupError(f"Failed to load voice registry: {registry_path}") from exc
        if not isinstance(loaded_payload, dict):
            raise VoiceLookupError("voice_registry.json must contain a top-level JSON object.")
        registry_payload = loaded_payload

    speakers_section = registry_payload.get("speakers", {})
    if speakers_section is None:
        speakers_section = {}
    if not isinstance(speakers_section, dict):
        raise VoiceLookupError("voice_registry.json speakers section must be a JSON object.")

    registry_by_name: dict[str, str] = {}
    for speaker_payload in speakers_section.values():
        if not isinstance(speaker_payload, dict):
            continue
        speaker_name = _normalize_name(speaker_payload.get("speaker_name"))
        default_voice_id = _normalize_voice_id(speaker_payload.get("default_voice_id"))
        if speaker_name is None or default_voice_id is None:
            continue
        registry_by_name[speaker_name] = default_voice_id

    resolved_voice_ids: dict[str, str] = {}
    for speaker_id, speaker_name in speaker_names.items():
        normalized_name = _normalize_name(speaker_name)
        matched_voice_id = registry_by_name.get(normalized_name or "")
        if matched_voice_id is None:
            matched_voice_id = _fallback_voice_id(
                speaker_id=speaker_id,
                fallback_voice_a=fallback_voice_a,
                fallback_voice_b=fallback_voice_b,
            )
        if matched_voice_id is None:
            raise VoiceLookupError(
                f"Missing voice_id for {speaker_id} ({speaker_name}). "
                "Pass --voice-b or register this speaker in voice_registry.json."
            )
        resolved_voice_ids[speaker_id] = matched_voice_id

    return resolved_voice_ids


def _normalize_name(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().casefold()
    return normalized or None


def _normalize_voice_id(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _fallback_voice_id(
    *,
    speaker_id: str,
    fallback_voice_a: str | None,
    fallback_voice_b: str | None,
) -> str | None:
    normalized_speaker_id = speaker_id.strip().lower()
    if normalized_speaker_id == "speaker_a":
        return _normalize_voice_id(fallback_voice_a)
    if normalized_speaker_id == "speaker_b":
        return _normalize_voice_id(fallback_voice_b)
    return None
