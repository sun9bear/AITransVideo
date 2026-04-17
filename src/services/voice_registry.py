from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from core.exceptions import StateError
from services._file_lock import file_lock
from services.state_manager import utc_now_iso
from services.tts.cosyvoice_voice_catalog import build_cosyvoice_v3_flash_builtin_voice_option


VOICE_TYPES = {"cloned", "builtin"}
VOICE_VERIFICATION_STATUSES = {"unverified", "verified", "failed"}


@dataclass(slots=True)
class VoiceRecord:
    voice_id: str
    voice_type: str
    provider: str
    label: str
    created_at: str
    tts_provider: str | None = None
    platform: str | None = None
    source_audio_path: str | None = None
    notes: str | None = None
    verification_status: str = "unverified"
    last_verified_at: str | None = None
    last_verification_success: bool | None = None
    last_verification_audio_path: str | None = None
    last_verification_error: str | None = None

    def __post_init__(self) -> None:
        self.voice_id = _normalize_required_text(self.voice_id, field_name="voice_id")
        self.voice_type = _normalize_voice_type(self.voice_type)
        self.provider = _normalize_required_text(self.provider, field_name="provider")
        self.tts_provider = _normalize_tts_provider(self.tts_provider, legacy_provider=self.provider)
        self.platform = _normalize_platform(
            self.platform,
            tts_provider=self.tts_provider,
            legacy_provider=self.provider,
        )
        self.label = _normalize_required_text(self.label, field_name="label")
        self.created_at = _normalize_required_text(self.created_at, field_name="created_at")
        self.source_audio_path = _normalize_optional_text(self.source_audio_path)
        self.notes = _normalize_optional_text(self.notes)
        self.verification_status = _normalize_voice_verification_status(self.verification_status)
        self.last_verified_at = _normalize_optional_text(self.last_verified_at)
        self.last_verification_audio_path = _normalize_optional_text(self.last_verification_audio_path)
        self.last_verification_error = _normalize_optional_text(self.last_verification_error)
        if self.last_verification_success is not None and not isinstance(self.last_verification_success, bool):
            raise ValueError("last_verification_success must be a bool when provided")

    def to_dict(self) -> dict[str, object]:
        return {
            "voice_id": self.voice_id,
            "voice_type": self.voice_type,
            "provider": self.provider,
            "tts_provider": self.tts_provider,
            "platform": self.platform,
            "label": self.label,
            "created_at": self.created_at,
            "source_audio_path": self.source_audio_path,
            "notes": self.notes,
            "verification_status": self.verification_status,
            "last_verified_at": self.last_verified_at,
            "last_verification_success": self.last_verification_success,
            "last_verification_audio_path": self.last_verification_audio_path,
            "last_verification_error": self.last_verification_error,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "VoiceRecord":
        return cls(
            voice_id=str(payload.get("voice_id", "")),
            voice_type=str(payload.get("voice_type", "")),
            provider=str(payload.get("provider", "")),
            tts_provider=_read_optional_string(payload, "tts_provider"),
            platform=_read_optional_string(payload, "platform"),
            label=str(payload.get("label", "")),
            created_at=str(payload.get("created_at", "")),
            source_audio_path=_read_optional_string(payload, "source_audio_path"),
            notes=_read_optional_string(payload, "notes"),
            verification_status=str(payload.get("verification_status", "unverified")),
            last_verified_at=_read_optional_string(payload, "last_verified_at"),
            last_verification_success=_read_optional_bool(payload, "last_verification_success"),
            last_verification_audio_path=_read_optional_string(payload, "last_verification_audio_path"),
            last_verification_error=_read_optional_string(payload, "last_verification_error"),
        )


@dataclass(slots=True)
class SpeakerVoiceProfile:
    speaker_id: str
    speaker_name: str | None = None
    default_voice_id: str | None = None
    default_voice_type: str | None = None
    voices: list[VoiceRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.speaker_id = _normalize_required_text(self.speaker_id, field_name="speaker_id")
        self.speaker_name = _normalize_optional_text(self.speaker_name)
        self.default_voice_id = _normalize_optional_text(self.default_voice_id)
        self.default_voice_type = (
            _normalize_voice_type(self.default_voice_type)
            if self.default_voice_type is not None
            else None
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "speaker_name": self.speaker_name,
            "default_voice_id": self.default_voice_id,
            "default_voice_type": self.default_voice_type,
            "voices": [voice.to_dict() for voice in self.voices],
        }

    @classmethod
    def from_dict(cls, speaker_id: str, payload: dict[str, Any]) -> "SpeakerVoiceProfile":
        voices_payload = payload.get("voices", [])
        if not isinstance(voices_payload, list):
            raise ValueError("voices must be a list")
        return cls(
            speaker_id=speaker_id,
            speaker_name=_read_optional_string(payload, "speaker_name"),
            default_voice_id=_read_optional_string(payload, "default_voice_id"),
            default_voice_type=_read_optional_string(payload, "default_voice_type"),
            voices=[
                VoiceRecord.from_dict(voice_payload)
                for voice_payload in voices_payload
                if isinstance(voice_payload, dict)
            ],
        )


@dataclass(slots=True)
class ProjectDefaultVoice:
    voice_id: str
    provider: str
    label: str
    created_at: str
    tts_provider: str | None = None
    platform: str | None = None
    notes: str | None = None
    voice_type: str = "builtin"

    def __post_init__(self) -> None:
        self.voice_id = _normalize_required_text(self.voice_id, field_name="voice_id")
        self.provider = _normalize_required_text(self.provider, field_name="provider")
        self.tts_provider = _normalize_tts_provider(self.tts_provider, legacy_provider=self.provider)
        self.platform = _normalize_platform(
            self.platform,
            tts_provider=self.tts_provider,
            legacy_provider=self.provider,
        )
        self.label = _normalize_required_text(self.label, field_name="label")
        self.created_at = _normalize_required_text(self.created_at, field_name="created_at")
        self.notes = _normalize_optional_text(self.notes)
        self.voice_type = _normalize_voice_type(self.voice_type)
        if self.voice_type != "builtin":
            raise ValueError("project default voice must use voice_type=builtin")

    def to_dict(self) -> dict[str, object]:
        return {
            "voice_id": self.voice_id,
            "voice_type": self.voice_type,
            "provider": self.provider,
            "tts_provider": self.tts_provider,
            "platform": self.platform,
            "label": self.label,
            "created_at": self.created_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProjectDefaultVoice":
        return cls(
            voice_id=str(payload.get("voice_id", "")),
            provider=str(payload.get("provider", "")),
            tts_provider=_read_optional_string(payload, "tts_provider"),
            platform=_read_optional_string(payload, "platform"),
            label=str(payload.get("label", "")),
            created_at=str(payload.get("created_at", "")),
            notes=_read_optional_string(payload, "notes"),
            voice_type=str(payload.get("voice_type", "builtin")),
        )


@dataclass(slots=True)
class VoiceResolution:
    speaker_id: str
    speaker_name: str | None
    status: str
    source: str
    voice_id: str | None = None
    voice_type: str | None = None
    provider: str | None = None
    tts_provider: str | None = None
    platform: str | None = None
    label: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved"


class VoiceRegistry:
    def __init__(self, registry_path: str) -> None:
        self.registry_path = Path(registry_path)

    def load(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return self._empty_registry()

        try:
            loaded = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"Failed to load voice registry file: {self.registry_path}") from exc
        return self._normalize_registry(loaded)

    def save(self, registry_data: dict[str, Any]) -> None:
        temp_path: Path | None = None
        try:
            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(registry_data, indent=2, sort_keys=True, ensure_ascii=False)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.registry_path.parent,
                prefix=f"{self.registry_path.stem}_",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_file.write(serialized)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            os.replace(temp_path, self.registry_path)
        except OSError as exc:
            raise StateError(f"Failed to save voice registry file: {self.registry_path}") from exc
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def get_speaker_profile(self, speaker_id: str) -> SpeakerVoiceProfile | None:
        normalized_speaker_id = _normalize_required_text(speaker_id, field_name="speaker_id")
        registry_data = self.load()
        speaker_payload = registry_data["speakers"].get(normalized_speaker_id)
        if not isinstance(speaker_payload, dict):
            return None
        return SpeakerVoiceProfile.from_dict(normalized_speaker_id, speaker_payload)

    def register_voice(
        self,
        speaker_id: str,
        *,
        speaker_name: str | None,
        voice_id: str,
        voice_type: str,
        provider: str,
        tts_provider: str | None = None,
        platform: str | None = None,
        label: str,
        created_at: str | None = None,
        source_audio_path: str | None = None,
        notes: str | None = None,
        set_default: bool = False,
    ) -> SpeakerVoiceProfile:
        normalized_speaker_id = _normalize_required_text(speaker_id, field_name="speaker_id")
        # T3.2: wrap load → modify → save so concurrent writers cannot lose
        # each other's updates. save() already uses atomic os.replace, but
        # that only guards the final write; without this lock, two threads
        # can both read-same-state then race on overwriting.
        with file_lock(self.registry_path):
            return self._register_voice_locked(
                normalized_speaker_id,
                speaker_name=speaker_name,
                voice_id=voice_id,
                voice_type=voice_type,
                provider=provider,
                tts_provider=tts_provider,
                platform=platform,
                label=label,
                created_at=created_at,
                source_audio_path=source_audio_path,
                notes=notes,
                set_default=set_default,
            )

    def _register_voice_locked(
        self,
        normalized_speaker_id: str,
        *,
        speaker_name: str | None,
        voice_id: str,
        voice_type: str,
        provider: str,
        tts_provider: str | None,
        platform: str | None,
        label: str,
        created_at: str | None,
        source_audio_path: str | None,
        notes: str | None,
        set_default: bool,
    ) -> SpeakerVoiceProfile:
        """Inner register-voice implementation that assumes caller already
        holds ``file_lock(self.registry_path)``. Exists so ``set_default_voice``
        can delegate without double-acquire (file_lock is reentrant anyway,
        but keeping the critical section shallow is clearer)."""
        registry_data = self.load()
        speakers = registry_data["speakers"]
        speaker_payload = speakers.setdefault(
            normalized_speaker_id,
            {
                "speaker_name": None,
                "default_voice_id": None,
                "default_voice_type": None,
                "voices": [],
            },
        )
        if not isinstance(speaker_payload, dict):
            raise StateError(f"Speaker registry entry is invalid for speaker_id={normalized_speaker_id}")

        voice_record = VoiceRecord(
            voice_id=voice_id,
            voice_type=voice_type,
            provider=provider,
            tts_provider=tts_provider,
            platform=platform,
            label=label,
            created_at=created_at or utc_now_iso(),
            source_audio_path=source_audio_path,
            notes=notes,
        )

        existing_voices = speaker_payload.setdefault("voices", [])
        if not isinstance(existing_voices, list):
            raise StateError(f"Speaker voices entry is invalid for speaker_id={normalized_speaker_id}")

        replaced = False
        for index, existing_voice in enumerate(existing_voices):
            if isinstance(existing_voice, dict) and str(existing_voice.get("voice_id", "")).strip() == voice_record.voice_id:
                preserved_verification = VoiceRecord.from_dict(existing_voice)
                voice_record.verification_status = preserved_verification.verification_status
                voice_record.last_verified_at = preserved_verification.last_verified_at
                voice_record.last_verification_success = preserved_verification.last_verification_success
                voice_record.last_verification_audio_path = preserved_verification.last_verification_audio_path
                voice_record.last_verification_error = preserved_verification.last_verification_error
                existing_voices[index] = voice_record.to_dict()
                replaced = True
                break
        if not replaced:
            existing_voices.append(voice_record.to_dict())

        normalized_speaker_name = _normalize_optional_text(speaker_name)
        if normalized_speaker_name is not None:
            speaker_payload["speaker_name"] = normalized_speaker_name
        speaker_payload.setdefault("default_voice_id", None)
        speaker_payload.setdefault("default_voice_type", None)

        if set_default:
            speaker_payload["default_voice_id"] = voice_record.voice_id
            speaker_payload["default_voice_type"] = voice_record.voice_type

        self.save(registry_data)
        return self.get_speaker_profile(normalized_speaker_id) or SpeakerVoiceProfile(speaker_id=normalized_speaker_id)

    def record_voice_verification(
        self,
        speaker_id: str,
        voice_id: str,
        *,
        success: bool,
        verified_at: str | None = None,
        audio_path: str | None = None,
        error_message: str | None = None,
    ) -> SpeakerVoiceProfile:
        normalized_speaker_id = _normalize_required_text(speaker_id, field_name="speaker_id")
        normalized_voice_id = _normalize_required_text(voice_id, field_name="voice_id")
        # T3.2: serialize load → modify → save against concurrent writers
        with file_lock(self.registry_path):
            registry_data = self.load()
            speaker_payload = registry_data["speakers"].get(normalized_speaker_id)
            if not isinstance(speaker_payload, dict):
                raise ValueError(f"Speaker not found for speaker_id={normalized_speaker_id}")

            voice_payload = self._find_voice_payload(speaker_payload, normalized_voice_id)
            if voice_payload is None:
                raise ValueError(
                    f"Voice not found for speaker_id={normalized_speaker_id} voice_id={normalized_voice_id}"
                )

            voice_payload["verification_status"] = "verified" if success else "failed"
            voice_payload["last_verified_at"] = _normalize_optional_text(verified_at) or utc_now_iso()
            voice_payload["last_verification_success"] = success
            voice_payload["last_verification_audio_path"] = _normalize_optional_text(audio_path)
            voice_payload["last_verification_error"] = None if success else _normalize_optional_text(error_message)
            self.save(registry_data)
            return self.get_speaker_profile(normalized_speaker_id) or SpeakerVoiceProfile(speaker_id=normalized_speaker_id)

    def set_default_voice(self, speaker_id: str, voice_id: str) -> SpeakerVoiceProfile:
        normalized_speaker_id = _normalize_required_text(speaker_id, field_name="speaker_id")
        normalized_voice_id = _normalize_required_text(voice_id, field_name="voice_id")
        # T3.2: serialize the whole operation, including the delegate to
        # register_voice on the "voice not found, fall back to catalog" path.
        # file_lock is reentrant so register_voice's own acquire is a no-op.
        with file_lock(self.registry_path):
            registry_data = self.load()
            speaker_payload = registry_data["speakers"].get(normalized_speaker_id)
            if not isinstance(speaker_payload, dict):
                raise ValueError(f"Speaker not found for speaker_id={normalized_speaker_id}")

            voice_payload = self._find_voice_payload(speaker_payload, normalized_voice_id)
            if voice_payload is None:
                catalog_voice = build_cosyvoice_v3_flash_builtin_voice_option(normalized_voice_id)
                if catalog_voice is None:
                    raise ValueError(
                        f"Voice not found for speaker_id={normalized_speaker_id} voice_id={normalized_voice_id}"
                    )
                return self.register_voice(
                    normalized_speaker_id,
                    speaker_name=_read_optional_string(speaker_payload, "speaker_name"),
                    voice_id=str(catalog_voice["voice_id"]),
                    voice_type="builtin",
                    provider=str(catalog_voice["provider"]),
                    tts_provider=_read_optional_string(catalog_voice, "tts_provider"),
                    platform=_read_optional_string(catalog_voice, "platform"),
                    label=str(catalog_voice["label"]),
                    created_at=_read_optional_string(catalog_voice, "created_at"),
                    notes=_read_optional_string(catalog_voice, "notes"),
                    set_default=True,
                )

            speaker_payload["default_voice_id"] = normalized_voice_id
            speaker_payload["default_voice_type"] = str(voice_payload.get("voice_type", "")).strip()
            self.save(registry_data)
            return self.get_speaker_profile(normalized_speaker_id) or SpeakerVoiceProfile(speaker_id=normalized_speaker_id)

    def set_project_default_builtin_voice(
        self,
        *,
        voice_id: str,
        provider: str,
        tts_provider: str | None = None,
        platform: str | None = None,
        label: str,
        created_at: str | None = None,
        notes: str | None = None,
    ) -> ProjectDefaultVoice:
        project_default_voice = ProjectDefaultVoice(
            voice_id=voice_id,
            provider=provider,
            tts_provider=tts_provider,
            platform=platform,
            label=label,
            created_at=created_at or utc_now_iso(),
            notes=notes,
        )
        # T3.2: serialize load → modify → save against concurrent writers
        with file_lock(self.registry_path):
            registry_data = self.load()
            registry_data["project_defaults"]["default_builtin_voice"] = project_default_voice.to_dict()
            self.save(registry_data)
        return project_default_voice

    def get_project_default_builtin_voice(self) -> ProjectDefaultVoice | None:
        registry_data = self.load()
        project_defaults = registry_data.get("project_defaults", {})
        if not isinstance(project_defaults, dict):
            return None
        default_voice_payload = project_defaults.get("default_builtin_voice")
        if not isinstance(default_voice_payload, dict):
            return None
        return ProjectDefaultVoice.from_dict(default_voice_payload)

    def _normalize_registry(self, registry_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(registry_data, dict):
            raise StateError("Voice registry JSON root must be an object.")

        speakers_payload = registry_data.get("speakers", {})
        if not isinstance(speakers_payload, dict):
            raise StateError("Voice registry speakers must be an object keyed by speaker_id.")

        normalized_speakers: dict[str, Any] = {}
        for speaker_id, speaker_payload in speakers_payload.items():
            if not isinstance(speaker_payload, dict):
                raise StateError(f"Voice registry speaker payload must be an object: {speaker_id}")
            profile = SpeakerVoiceProfile.from_dict(str(speaker_id), speaker_payload)
            normalized_speakers[profile.speaker_id] = profile.to_dict()

        project_defaults_payload = registry_data.get("project_defaults", {})
        if not isinstance(project_defaults_payload, dict):
            raise StateError("Voice registry project_defaults must be an object.")

        normalized_project_defaults: dict[str, Any] = {"default_builtin_voice": None}
        default_builtin_voice = project_defaults_payload.get("default_builtin_voice")
        if isinstance(default_builtin_voice, dict):
            normalized_project_defaults["default_builtin_voice"] = ProjectDefaultVoice.from_dict(
                default_builtin_voice
            ).to_dict()

        return {
            "speakers": normalized_speakers,
            "project_defaults": normalized_project_defaults,
        }

    def _empty_registry(self) -> dict[str, Any]:
        return {
            "speakers": {},
            "project_defaults": {"default_builtin_voice": None},
        }

    def _find_voice_payload(self, speaker_payload: dict[str, Any], voice_id: str) -> dict[str, Any] | None:
        voices_payload = speaker_payload.get("voices", [])
        if not isinstance(voices_payload, list):
            return None
        for voice_payload in voices_payload:
            if isinstance(voice_payload, dict) and str(voice_payload.get("voice_id", "")).strip() == voice_id:
                return voice_payload
        return None


class VoiceResolver:
    def __init__(self, registry: VoiceRegistry) -> None:
        self.registry = registry

    def resolve(
        self,
        speaker_id: str,
        *,
        tts_provider: str | None = None,
        platform: str | None = None,
    ) -> VoiceResolution:
        normalized_speaker_id = _normalize_required_text(speaker_id, field_name="speaker_id")
        normalized_tts_provider = _normalize_tts_provider(tts_provider)
        normalized_platform = _normalize_platform(platform, tts_provider=normalized_tts_provider)
        profile = self.registry.get_speaker_profile(normalized_speaker_id)
        if profile is not None:
            speaker_default_cloned = self._resolve_speaker_default(
                profile,
                expected_voice_type="cloned",
                tts_provider=normalized_tts_provider,
                platform=normalized_platform,
            )
            if speaker_default_cloned is not None:
                return self._build_resolved_result(
                    profile,
                    speaker_default_cloned,
                    source="speaker_default_cloned",
                )

            speaker_default_builtin = self._resolve_speaker_default(
                profile,
                expected_voice_type="builtin",
                tts_provider=normalized_tts_provider,
                platform=normalized_platform,
            )
            if speaker_default_builtin is not None:
                return self._build_resolved_result(
                    profile,
                    speaker_default_builtin,
                    source="speaker_default_builtin",
                )

        project_default_builtin = self.registry.get_project_default_builtin_voice()
        if (
            project_default_builtin is not None
            and self._is_voice_compatible(
                project_default_builtin.tts_provider,
                project_default_builtin.platform,
                tts_provider=normalized_tts_provider,
                platform=normalized_platform,
            )
        ):
            return VoiceResolution(
                speaker_id=normalized_speaker_id,
                speaker_name=profile.speaker_name if profile is not None else None,
                status="resolved",
                source="project_default_builtin",
                voice_id=project_default_builtin.voice_id,
                voice_type=project_default_builtin.voice_type,
                provider=project_default_builtin.provider,
                tts_provider=project_default_builtin.tts_provider,
                platform=project_default_builtin.platform,
                label=project_default_builtin.label,
            )

        return VoiceResolution(
            speaker_id=normalized_speaker_id,
            speaker_name=profile.speaker_name if profile is not None else None,
            status="unresolved",
            source="unresolved",
        )

    def resolve_voice_id(
        self,
        speaker_id: str,
        *,
        tts_provider: str | None = None,
        platform: str | None = None,
    ) -> str | None:
        return self.resolve(
            speaker_id,
            tts_provider=tts_provider,
            platform=platform,
        ).voice_id

    def _resolve_speaker_default(
        self,
        profile: SpeakerVoiceProfile,
        *,
        expected_voice_type: str,
        tts_provider: str | None,
        platform: str | None,
    ) -> VoiceRecord | None:
        if profile.default_voice_id is None or profile.default_voice_type != expected_voice_type:
            return None
        for voice in profile.voices:
            if (
                voice.voice_id == profile.default_voice_id
                and voice.voice_type == expected_voice_type
                and self._is_voice_compatible(
                    voice.tts_provider,
                    voice.platform,
                    tts_provider=tts_provider,
                    platform=platform,
                )
            ):
                return voice
        return None

    def _build_resolved_result(
        self,
        profile: SpeakerVoiceProfile,
        voice: VoiceRecord,
        *,
        source: str,
    ) -> VoiceResolution:
        return VoiceResolution(
            speaker_id=profile.speaker_id,
            speaker_name=profile.speaker_name,
            status="resolved",
            source=source,
            voice_id=voice.voice_id,
            voice_type=voice.voice_type,
            provider=voice.provider,
            tts_provider=voice.tts_provider,
            platform=voice.platform,
            label=voice.label,
        )

    def _is_voice_compatible(
        self,
        voice_tts_provider: str | None,
        voice_platform: str | None,
        *,
        tts_provider: str | None,
        platform: str | None,
    ) -> bool:
        if tts_provider is not None and voice_tts_provider != tts_provider:
            return False
        if platform is not None and voice_platform != platform:
            return False
        return True


def _normalize_required_text(raw_value: str | None, *, field_name: str) -> str:
    normalized = _normalize_optional_text(raw_value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return normalized


def _normalize_optional_text(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    normalized = str(raw_value).strip()
    return normalized or None


def _normalize_tts_provider(
    raw_value: str | None,
    *,
    legacy_provider: str | None = None,
) -> str | None:
    normalized = _normalize_optional_text(raw_value)
    if normalized is not None:
        return normalized.lower()
    inferred = _infer_legacy_tts_provider(legacy_provider)
    return inferred.lower() if inferred is not None else None


def _normalize_platform(
    raw_value: str | None,
    *,
    tts_provider: str | None,
    legacy_provider: str | None = None,
) -> str | None:
    normalized = _normalize_optional_text(raw_value)
    if normalized is not None:
        return normalized.lower()
    inferred_tts_provider = tts_provider or _infer_legacy_tts_provider(legacy_provider)
    if inferred_tts_provider == "minimax_tts":
        return "minimax_domestic"
    return None


def _infer_legacy_tts_provider(legacy_provider: str | None) -> str | None:
    normalized_legacy_provider = _normalize_optional_text(legacy_provider)
    if normalized_legacy_provider is None:
        return None
    normalized_legacy_provider = normalized_legacy_provider.lower()
    if normalized_legacy_provider == "minimax_voice_clone":
        return "minimax_tts"
    return normalized_legacy_provider


def _normalize_voice_type(raw_value: str | None) -> str:
    normalized = _normalize_required_text(raw_value, field_name="voice_type").lower()
    if normalized not in VOICE_TYPES:
        raise ValueError(f"Unsupported voice_type: {raw_value}")
    return normalized


def _normalize_voice_verification_status(raw_value: str | None) -> str:
    normalized = _normalize_required_text(raw_value, field_name="verification_status").lower()
    if normalized not in VOICE_VERIFICATION_STATUSES:
        raise ValueError(f"Unsupported verification_status: {raw_value}")
    return normalized


def _read_optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _read_optional_bool(payload: dict[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    return value if isinstance(value, bool) else None
