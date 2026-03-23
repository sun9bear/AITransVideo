from __future__ import annotations

from pathlib import Path

from core.exceptions import StateError
from services import config_loader
from services.review_state import (
    REVIEW_STATUS_PENDING,
    VOICE_REVIEW_STAGE,
    ReviewStateManager,
)
from services.voice_registry import SpeakerVoiceProfile, VoiceRegistry, VoiceResolver

from .config_helpers import _normalize_optional_text
from .output_entries import _resolve_review_state_path
from .utils import _coerce_float


def _build_voice_library_snapshot(
    *,
    project_root: Path,
    config_path: Path,
    project_dir: Path | None,
    transcript_items: list[dict[str, object]],
) -> dict[str, object]:
    registry_path = _resolve_voice_registry_path(project_root=project_root, config_path=config_path)
    snapshot: dict[str, object] = {
        "path": str(registry_path),
        "exists": registry_path.exists(),
        "load_error": None,
        "speaker_count": 0,
        "voice_count": 0,
        "builtin_voice_count": 0,
        "project_default_builtin_voice": None,
        "builtin_voice_options": [],
        "active_review": None,
        "current_project_speakers": [],
        "speakers": [],
    }

    registry = VoiceRegistry(str(registry_path))
    try:
        registry_data = registry.load()
    except StateError as exc:
        snapshot["load_error"] = str(exc)
        return snapshot

    resolver = VoiceResolver(registry)
    speakers_payload = registry_data.get("speakers", {})
    registry_speakers: list[dict[str, object]] = []
    builtin_voice_options: list[dict[str, object]] = []
    total_voice_count = 0

    if isinstance(speakers_payload, dict):
        for speaker_id in sorted(speakers_payload.keys(), key=str):
            speaker_payload = speakers_payload.get(speaker_id)
            if not isinstance(speaker_payload, dict):
                continue
            profile = SpeakerVoiceProfile.from_dict(str(speaker_id), speaker_payload)
            resolution = resolver.resolve(profile.speaker_id)
            serialized_voices = [_serialize_registry_voice(voice) for voice in profile.voices]
            total_voice_count += len(serialized_voices)
            registry_speakers.append(
                {
                    "speaker_id": profile.speaker_id,
                    "speaker_name": profile.speaker_name,
                    "default_voice_id": profile.default_voice_id,
                    "default_voice_type": profile.default_voice_type,
                    "resolution_source": resolution.source,
                    "voice_count": len(serialized_voices),
                    "voices": serialized_voices,
                }
            )
            for voice in profile.voices:
                if voice.voice_type != "builtin":
                    continue
                builtin_voice_options.append(
                    {
                        "voice_id": voice.voice_id,
                        "speaker_id": profile.speaker_id,
                        "speaker_name": profile.speaker_name,
                        "label": voice.label,
                        "provider": voice.provider,
                        "tts_provider": voice.tts_provider,
                        "platform": voice.platform,
                        "voice_type": voice.voice_type,
                        "created_at": voice.created_at,
                        "verification_status": voice.verification_status,
                    }
                )

    builtin_voice_options.sort(
        key=lambda item: (
            str(item.get("speaker_name") or item.get("speaker_id") or "").lower(),
            str(item.get("label") or "").lower(),
            str(item.get("voice_id") or "").lower(),
        )
    )
    project_default_builtin_voice = registry.get_project_default_builtin_voice()
    snapshot.update(
        {
            "speaker_count": len(registry_speakers),
            "voice_count": total_voice_count,
            "builtin_voice_count": len(builtin_voice_options),
            "project_default_builtin_voice": (
                project_default_builtin_voice.to_dict()
                if project_default_builtin_voice is not None
                else None
            ),
            "builtin_voice_options": builtin_voice_options,
            "active_review": _build_active_voice_review_snapshot(
                project_dir=project_dir,
                registry=registry,
                resolver=resolver,
            ),
            "current_project_speakers": _build_current_project_voice_bindings(
                transcript_items=transcript_items,
                registry=registry,
                resolver=resolver,
            ),
            "speakers": registry_speakers,
        }
    )
    return snapshot


def _resolve_voice_registry_path(*, project_root: Path, config_path: Path) -> Path:
    project_config = config_loader.load_project_local_config(config_path)
    resolved_path, _ = config_loader.resolve_path_value(
        env_keys=["AUTODUB_TTS_VOICE_REGISTRY_PATH"],
        config=project_config,
        config_key_paths=(
            ("voice_registry", "registry_path"),
            ("tts", "voice_registry_path"),
            ("paths", "voice_registry_path"),
        ),
    )
    if resolved_path is not None:
        return Path(resolved_path).expanduser().resolve(strict=False)
    return (project_root / "voice_registry.json").resolve(strict=False)


def _build_active_voice_review_snapshot(
    *,
    project_dir: Path | None,
    registry: VoiceRegistry,
    resolver: VoiceResolver,
) -> dict[str, object] | None:
    if project_dir is None:
        return None
    review_state_manager = ReviewStateManager(_resolve_review_state_path(project_dir))
    stage_payload = review_state_manager.get_stage(VOICE_REVIEW_STAGE)
    if not stage_payload or stage_payload.get("status") != REVIEW_STATUS_PENDING:
        return None
    payload = stage_payload.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    serialized_speakers: list[dict[str, object]] = []
    raw_speakers = payload.get("speakers", [])
    if isinstance(raw_speakers, list):
        for raw_speaker in raw_speakers:
            if not isinstance(raw_speaker, dict):
                continue
            speaker_id = _normalize_optional_text(raw_speaker.get("speaker_id"))
            if speaker_id is None:
                continue
            profile = registry.get_speaker_profile(speaker_id)
            resolution = resolver.resolve(speaker_id)
            serialized_speakers.append(
                {
                    "speaker_id": speaker_id,
                    "speaker_label": _normalize_optional_text(raw_speaker.get("speaker_label")),
                    "speaker_name": _normalize_optional_text(raw_speaker.get("speaker_name")) or speaker_id,
                    "voice_arg_name": _normalize_optional_text(raw_speaker.get("voice_arg_name")),
                    "sample_path": _normalize_optional_text(raw_speaker.get("sample_path")),
                    "sample_duration_s": _coerce_float(raw_speaker.get("sample_duration_s"), default=0.0),
                    "silence_ratio": _coerce_float(raw_speaker.get("silence_ratio"), default=0.0),
                    "default_voice_id": profile.default_voice_id if profile is not None else None,
                    "default_voice_type": profile.default_voice_type if profile is not None else None,
                    "resolved_status": resolution.status,
                    "resolved_source": resolution.source,
                    "resolved_voice_id": resolution.voice_id,
                    "resolved_voice_type": resolution.voice_type,
                    "resolved_label": resolution.label,
                    "available_voices": (
                        [_serialize_registry_voice(voice) for voice in profile.voices]
                        if profile is not None
                        else []
                    ),
                }
            )

    return {
        "stage": VOICE_REVIEW_STAGE,
        "status": stage_payload.get("status"),
        "message": _normalize_optional_text(payload.get("message"))
        or _normalize_optional_text(stage_payload.get("message"))
        or "",
        "reason": _normalize_optional_text(payload.get("reason")),
        "speakers": serialized_speakers,
    }


def _build_current_project_voice_bindings(
    *,
    transcript_items: list[dict[str, object]],
    registry: VoiceRegistry,
    resolver: VoiceResolver,
) -> list[dict[str, object]]:
    current_speakers: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in transcript_items:
        speaker_id = str(item.get("speaker_id") or "").strip()
        if not speaker_id or speaker_id in seen:
            continue
        seen.add(speaker_id)
        display_name = str(item.get("display_name") or "").strip() or speaker_id
        current_speakers.append((speaker_id, display_name))

    bindings: list[dict[str, object]] = []
    for speaker_id, display_name in current_speakers:
        profile = registry.get_speaker_profile(speaker_id)
        resolution = resolver.resolve(speaker_id)
        bindings.append(
            {
                "speaker_id": speaker_id,
                "display_name": display_name,
                "speaker_name": profile.speaker_name if profile is not None else None,
                "default_voice_id": profile.default_voice_id if profile is not None else None,
                "default_voice_type": profile.default_voice_type if profile is not None else None,
                "resolved_status": resolution.status,
                "resolved_source": resolution.source,
                "resolved_voice_id": resolution.voice_id,
                "resolved_voice_type": resolution.voice_type,
                "resolved_label": resolution.label,
                "available_voices": (
                    [_serialize_registry_voice(voice) for voice in profile.voices]
                    if profile is not None
                    else []
                ),
            }
        )
    return bindings


def _serialize_registry_voice(voice: object) -> dict[str, object]:
    return {
        "voice_id": getattr(voice, "voice_id", None),
        "voice_type": getattr(voice, "voice_type", None),
        "provider": getattr(voice, "provider", None),
        "tts_provider": getattr(voice, "tts_provider", None),
        "platform": getattr(voice, "platform", None),
        "label": getattr(voice, "label", None),
        "created_at": getattr(voice, "created_at", None),
        "source_audio_path": getattr(voice, "source_audio_path", None),
        "notes": getattr(voice, "notes", None),
        "verification_status": getattr(voice, "verification_status", None),
        "last_verified_at": getattr(voice, "last_verified_at", None),
        "last_verification_success": getattr(voice, "last_verification_success", None),
        "last_verification_audio_path": getattr(voice, "last_verification_audio_path", None),
        "last_verification_error": getattr(voice, "last_verification_error", None),
    }


def _find_builtin_voice_option(
    *,
    registry: VoiceRegistry,
    voice_id: str,
) -> dict[str, object] | None:
    registry_data = registry.load()
    speakers_payload = registry_data.get("speakers", {})
    if not isinstance(speakers_payload, dict):
        return None
    normalized_voice_id = str(voice_id).strip()
    if not normalized_voice_id:
        return None
    for speaker_id, speaker_payload in speakers_payload.items():
        if not isinstance(speaker_payload, dict):
            continue
        profile = SpeakerVoiceProfile.from_dict(str(speaker_id), speaker_payload)
        for voice in profile.voices:
            if voice.voice_type == "builtin" and voice.voice_id == normalized_voice_id:
                return {
                    "voice_id": voice.voice_id,
                    "provider": voice.provider,
                    "tts_provider": voice.tts_provider,
                    "platform": voice.platform,
                    "label": voice.label,
                    "created_at": voice.created_at,
                    "notes": voice.notes,
                }
    return None
