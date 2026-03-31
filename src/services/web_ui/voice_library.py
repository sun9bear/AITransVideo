from __future__ import annotations

import os
from pathlib import Path

from core.exceptions import StateError
from services import config_loader
from services.review_state import (
    REVIEW_STATUS_PENDING,
    VOICE_REVIEW_STAGE,
    ReviewStateManager,
)
from services.tts import cosyvoice_provider
from services.tts.cosyvoice_voice_catalog import (
    COSYVOICE_PLATFORM,
    COSYVOICE_TTS_PROVIDER,
    COSYVOICE_V3_FLASH_MODEL,
    build_cosyvoice_v3_flash_builtin_voice_option,
    list_cosyvoice_v3_flash_builtin_voice_options,
)
from services.tts.tts_strategy import get_tts_provider
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
        "builtin_voice_runtime_context": None,
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
    builtin_voice_runtime_context = _build_builtin_voice_runtime_context(config_path=config_path)
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
    if get_tts_provider(config_path) == "cosyvoice":
        builtin_voice_options = _merge_builtin_voice_options(
            builtin_voice_options,
            list_cosyvoice_v3_flash_builtin_voice_options(),
        )
    builtin_voice_options = [
        _annotate_builtin_voice_option(
            option,
            runtime_context=builtin_voice_runtime_context,
        )
        for option in builtin_voice_options
    ]
    project_default_builtin_voice = registry.get_project_default_builtin_voice()
    snapshot.update(
        {
            "speaker_count": len(registry_speakers),
            "voice_count": total_voice_count,
            "builtin_voice_count": len(builtin_voice_options),
            "builtin_voice_runtime_context": builtin_voice_runtime_context,
            "project_default_builtin_voice": (
                _annotate_builtin_voice_option(
                    project_default_builtin_voice.to_dict(),
                    runtime_context=builtin_voice_runtime_context,
                )
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
    config_path: Path | None = None,
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
                option = {
                    "voice_id": voice.voice_id,
                    "provider": voice.provider,
                    "tts_provider": voice.tts_provider,
                    "platform": voice.platform,
                    "label": voice.label,
                    "created_at": voice.created_at,
                    "notes": voice.notes,
                }
                if config_path is not None:
                    return _annotate_builtin_voice_option(
                        option,
                        runtime_context=_build_builtin_voice_runtime_context(config_path=config_path),
                    )
                return option
    option = build_cosyvoice_v3_flash_builtin_voice_option(normalized_voice_id)
    if option is None or config_path is None:
        return option
    return _annotate_builtin_voice_option(
        option,
        runtime_context=_build_builtin_voice_runtime_context(config_path=config_path),
    )


def _assert_builtin_voice_selection_allowed(option: dict[str, object] | None) -> None:
    if option is None:
        return
    if str(option.get("compatibility_status") or "").strip().lower() != "incompatible":
        return
    voice_id = str(option.get("voice_id") or "").strip() or "(unknown)"
    reason = str(option.get("compatibility_reason") or "").strip() or "incompatible_with_current_runtime"
    raise ValueError(f"builtin voice_id={voice_id} is incompatible with current TTS runtime: {reason}")


def _merge_builtin_voice_options(
    existing_options: list[dict[str, object]],
    catalog_options: list[dict[str, object]],
) -> list[dict[str, object]]:
    merged_options = list(existing_options)
    seen_voice_ids = {
        str(option.get("voice_id") or "").strip()
        for option in existing_options
        if str(option.get("voice_id") or "").strip()
    }
    for option in catalog_options:
        voice_id = str(option.get("voice_id") or "").strip()
        if not voice_id or voice_id in seen_voice_ids:
            continue
        merged_options.append(option)
        seen_voice_ids.add(voice_id)
    merged_options.sort(
        key=lambda item: (
            str(item.get("speaker_name") or item.get("speaker_id") or "").lower(),
            str(item.get("label") or "").lower(),
            str(item.get("voice_id") or "").lower(),
        )
    )
    return merged_options


def _build_builtin_voice_runtime_context(*, config_path: Path) -> dict[str, object]:
    active_provider = get_tts_provider(config_path)
    runtime_context: dict[str, object] = {
        "active_provider": active_provider,
        "active_model": None,
        "deployment_mode": None,
        "ws_url": None,
        "ws_url_source": None,
    }
    if active_provider != "cosyvoice":
        return runtime_context

    explicit_ws_url = os.environ.get("DASHSCOPE_WS_URL", "").strip()
    runtime_context.update(
        {
            "active_model": cosyvoice_provider.DEFAULT_MODEL,
            "deployment_mode": cosyvoice_provider._resolve_deployment_mode(),
            "ws_url": cosyvoice_provider._resolve_ws_url(),
            "ws_url_source": "env_override" if explicit_ws_url else "deployment_mode_default",
        }
    )
    return runtime_context


def _annotate_builtin_voice_option(
    option: dict[str, object],
    *,
    runtime_context: dict[str, object],
) -> dict[str, object]:
    annotated = dict(option)
    compatibility_status, compatibility_reason = _resolve_builtin_voice_compatibility(
        annotated,
        runtime_context=runtime_context,
    )
    annotated["compatibility_status"] = compatibility_status
    annotated["compatibility_reason"] = compatibility_reason
    return annotated


def _resolve_builtin_voice_compatibility(
    option: dict[str, object],
    *,
    runtime_context: dict[str, object],
) -> tuple[str, str]:
    active_provider = str(runtime_context.get("active_provider") or "").strip().lower()
    voice_tts_provider = str(option.get("tts_provider") or "").strip().lower()
    voice_platform = str(option.get("platform") or "").strip().lower()
    catalog_model = str(option.get("catalog_model") or "").strip().lower()

    if active_provider == "cosyvoice":
        active_model = str(runtime_context.get("active_model") or "").strip().lower()
        if voice_tts_provider and voice_tts_provider != COSYVOICE_TTS_PROVIDER:
            return (
                "incompatible",
                f"builtin voice uses tts_provider={voice_tts_provider}, current provider is cosyvoice",
            )
        if voice_platform and voice_platform != COSYVOICE_PLATFORM:
            return (
                "incompatible",
                f"builtin voice uses platform={voice_platform}, current platform is {COSYVOICE_PLATFORM}",
            )
        if catalog_model and catalog_model != active_model:
            return (
                "incompatible",
                f"voice catalog targets {catalog_model}, current model is {active_model}",
            )
        return ("compatible", "compatible_with_current_cosyvoice_runtime")

    if active_provider == "minimax":
        if voice_tts_provider in {"", "minimax_tts"}:
            return ("compatible", "compatible_with_current_minimax_runtime")
        return (
            "incompatible",
            f"builtin voice uses tts_provider={voice_tts_provider or 'unknown'}, current provider is minimax",
        )

    if active_provider == "mimo":
        return ("incompatible", "mimo_runtime_does_not_support_builtin_voice_ids")

    return ("unknown", "runtime_provider_unknown")
