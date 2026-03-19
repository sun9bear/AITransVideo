from dataclasses import dataclass
from pathlib import Path
import time

from core.exceptions import TTSError, TTSConfigurationError
from core.models import SemanticBlock
from services import config_loader
from services.tts_provider import OpenAICompatibleTTSProvider, RealTTSProviderConfig
from services.voice_clone import VoiceCloneConfig
from services.state_manager import utc_now_iso


DEFAULT_VOICE_VERIFICATION_SAMPLE_TEXT = "你好，这是一条 AutoDub 音色验证样例。"


class VoiceAssetVerificationError(Exception):
    """Base error for explicit voice asset verification."""


class VoiceAssetVerificationConfigurationError(VoiceAssetVerificationError):
    """Raised when verification cannot build a usable real TTS config."""


class VoiceAssetVerificationRuntimeError(VoiceAssetVerificationError):
    """Raised when the real TTS provider cannot produce a verification sample."""


@dataclass(slots=True)
class VoiceAssetVerificationResult:
    speaker_id: str
    voice_id: str
    sample_text: str
    output_path: str
    provider_name: str
    model_name: str | None
    verified_at: str
    config_source: str


class VoiceAssetVerifier:
    def __init__(
        self,
        *,
        config: RealTTSProviderConfig,
        verification_root: Path,
    ) -> None:
        self.config = config
        self.verification_root = verification_root

    @classmethod
    def from_env(
        cls,
        *,
        config_path: Path | None = None,
    ) -> "VoiceAssetVerifier":
        project_config = config_loader.load_project_local_config(config_path)
        tts_config = RealTTSProviderConfig.from_env(config_path=config_path)
        clone_config = VoiceCloneConfig.from_env(config_path=config_path)

        if not tts_config.provider_name.strip():
            tts_config.provider_name = "minimax_tts"
        if not tts_config.model_name and clone_config.model_name:
            tts_config.model_name = clone_config.model_name
            tts_config.model_name_source = clone_config.model_name_source
        if not tts_config.base_url and clone_config.base_url:
            tts_config.base_url = clone_config.base_url
            tts_config.base_url_source = clone_config.base_url_source
        if not tts_config.api_key and clone_config.resolved_api_key():
            tts_config.api_key = clone_config.resolved_api_key()
            tts_config.api_key_source = clone_config.api_key_source
        if not tts_config.enabled:
            tts_config.enabled = clone_config.enabled or tts_config.api_key is not None
        if not tts_config.api_protocol or tts_config.api_protocol == "audio_speech_v1":
            tts_config.api_protocol = "minimax_t2a_v2"
            tts_config.api_protocol_source = tts_config.api_protocol_source or "voice_asset_verification_default"

        verification_root_value, _ = config_loader.resolve_path_value(
            config=project_config,
            config_key_paths=(("paths", "voice_verification_root"),),
        )
        if verification_root_value is None:
            verification_root_value = str(project_config.path.parent / "voice_bank" / "verification_audio")
        verification_root = Path(verification_root_value).expanduser().resolve(strict=False)
        return cls(
            config=tts_config,
            verification_root=verification_root,
        )

    def verify_voice(
        self,
        *,
        speaker_id: str,
        voice_id: str,
        sample_text: str = DEFAULT_VOICE_VERIFICATION_SAMPLE_TEXT,
    ) -> VoiceAssetVerificationResult:
        normalized_speaker_id = _normalize_required_text(speaker_id, field_name="speaker_id")
        normalized_voice_id = _normalize_required_text(voice_id, field_name="voice_id")
        normalized_sample_text = _normalize_required_text(sample_text, field_name="sample_text")

        provider_config = self._build_verification_config(normalized_voice_id)
        output_dir = (self.verification_root / normalized_speaker_id).resolve(strict=False)
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            provider = OpenAICompatibleTTSProvider(output_dir=str(output_dir), config=provider_config)
        except TTSConfigurationError as exc:
            raise VoiceAssetVerificationConfigurationError(str(exc)) from exc

        block = SemanticBlock(
            block_id=f"voice_verify_{normalized_speaker_id}_{int(time.time() * 1000)}",
            speaker_id=normalized_speaker_id,
            speaker_name=None,
            original_srt_indices=[0],
            first_start_ms=0,
            last_end_ms=1_000,
            target_duration_ms=1_000,
            merged_cn_text=normalized_sample_text,
            merged_tts_cn_text=normalized_sample_text,
        )
        try:
            output_path = provider.synthesize(block)
        except TTSError as exc:
            raise VoiceAssetVerificationRuntimeError(str(exc)) from exc

        return VoiceAssetVerificationResult(
            speaker_id=normalized_speaker_id,
            voice_id=normalized_voice_id,
            sample_text=normalized_sample_text,
            output_path=output_path,
            provider_name=provider_config.provider_name,
            model_name=provider_config.model_name,
            verified_at=utc_now_iso(),
            config_source=str(provider_config.build_diagnostic_summary().get("config_source", "default")),
        )

    def _build_verification_config(self, voice_id: str) -> RealTTSProviderConfig:
        self.config.voice_id = voice_id
        self.config.voice_id_source = "voice_asset_verification_override"
        self.config.voice_registry_path = None
        self.config.voice_registry_path_source = "voice_asset_verification_override"
        if self.config.normalized_api_protocol() != "minimax_t2a_v2":
            raise VoiceAssetVerificationConfigurationError(
                "Voice asset verification currently requires MiniMax-compatible api_protocol=minimax_t2a_v2."
            )
        return self.config


def _normalize_required_text(raw_value: str | None, *, field_name: str) -> str:
    normalized = str(raw_value).strip() if raw_value is not None else ""
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized
