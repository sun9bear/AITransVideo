from dataclasses import dataclass
import logging
from typing import Protocol

from src.utils.atomic_io import atomic_write_bytes, is_valid_output

from core.enums import BlockStatus
from core.exceptions import (
    TTSConfigurationError,
    TTSInvalidAudioPayloadError,
    TTSError,
    TTSOutputFileWriteError,
    TTSProviderOutputError,
    TTSProviderNetworkError,
    TTSProviderResponseFormatError,
    TTSProviderTimeoutError,
    TTSProviderUnavailableError,
)
from core.models import SemanticBlock
from modules.alignment.dsp_stretcher import DSPStretcher
from modules.alignment.rewrite_engine import RewriteEngine
from modules.alignment.text_distributor import distribute_text_by_weights
from services.tts_provider import TTSProvider


logger = logging.getLogger(__name__)

class AudioServiceProtocol(Protocol):
    def get_duration_ms(self, audio_path: str) -> int:
        """Read audio duration in milliseconds."""


@dataclass(slots=True)
class AlignmentConfig:
    ideal_threshold: float = 0.05
    dsp_threshold: float = 0.20
    max_retries: int = 2

    def __post_init__(self) -> None:
        if self.ideal_threshold < 0 or self.dsp_threshold < 0:
            raise ValueError("Thresholds must be non-negative.")
        if self.ideal_threshold > self.dsp_threshold:
            raise ValueError("ideal_threshold cannot exceed dsp_threshold.")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative.")


class AlignmentOrchestrator:
    def __init__(
        self,
        tts_service: TTSProvider,
        audio_service: AudioServiceProtocol,
        rewrite_engine: RewriteEngine,
        dsp_stretcher: DSPStretcher,
        config: AlignmentConfig | None = None,
    ) -> None:
        self.tts_service = tts_service
        self.audio_service = audio_service
        self.rewrite_engine = rewrite_engine
        self.dsp_stretcher = dsp_stretcher
        self.config = config or AlignmentConfig()

    def process_block(self, block: SemanticBlock) -> SemanticBlock:
        if block.target_duration_ms <= 0:
            return self._fail_block(block, "Target duration must be positive.")

        # Checkpoint: 如果 aligned_audio_path 已存在且有效，跳过 TTS + 对齐，直接返回
        if block.aligned_audio_path and is_valid_output(block.aligned_audio_path):
            logger.info(
                "Block %s 已有对齐音频，跳过 TTS（checkpoint）: %s",
                block.block_id,
                block.aligned_audio_path,
            )
            if block.status not in (
                BlockStatus.ALIGN_DONE.value,
                BlockStatus.ALIGN_DONE_FALLBACK.value,
            ):
                block.status = BlockStatus.ALIGN_DONE.value
            return block

        while True:
            try:
                selected_cn_text = block.get_preferred_cn_text_for_tts()
                tts_audio_path = self.tts_service.synthesize(block)
                block.tts_audio_path = tts_audio_path
                block.actual_audio_duration_ms = self.audio_service.get_duration_ms(tts_audio_path)
                block.status = BlockStatus.TTS_DONE.value
                block.error_message = None
                block.error_type = None

                diff_ratio = self._calculate_diff_ratio(
                    actual_duration_ms=block.actual_audio_duration_ms,
                    target_duration_ms=block.target_duration_ms,
                )

                if abs(diff_ratio) <= self.config.ideal_threshold:
                    block.aligned_audio_path = block.tts_audio_path
                    block.final_cn_lines = distribute_text_by_weights(selected_cn_text, block.cn_line_texts)
                    block.status = BlockStatus.ALIGN_DONE.value
                    return block

                if abs(diff_ratio) <= self.config.dsp_threshold:
                    block.aligned_audio_path = self.dsp_stretcher.fit_to_duration(
                        input_audio_path=tts_audio_path,
                        target_duration_ms=block.target_duration_ms,
                    )
                    block.actual_audio_duration_ms = block.target_duration_ms
                    block.final_cn_lines = distribute_text_by_weights(selected_cn_text, block.cn_line_texts)
                    block.status = BlockStatus.ALIGN_DONE.value
                    return block

                if block.rewrite_count >= self.config.max_retries:
                    block.aligned_audio_path = self.dsp_stretcher.fit_to_duration(
                        input_audio_path=tts_audio_path,
                        target_duration_ms=block.target_duration_ms,
                    )
                    block.actual_audio_duration_ms = block.target_duration_ms
                    block.final_cn_lines = distribute_text_by_weights(selected_cn_text, block.cn_line_texts)
                    block.status = BlockStatus.ALIGN_DONE_FALLBACK.value
                    return block

                rewritten_text = self.rewrite_engine.rewrite_for_duration(
                    text=selected_cn_text,
                    actual_duration_ms=block.actual_audio_duration_ms,
                    target_duration_ms=block.target_duration_ms,
                )
                block.merged_tts_cn_text = rewritten_text
                block.merged_cn_text = block.get_preferred_cn_text_for_tts()
                block.rewrite_count += 1
            except Exception as exc:
                return self._fail_block(block, str(exc), exc)

    def _calculate_diff_ratio(self, actual_duration_ms: int, target_duration_ms: int) -> float:
        return (actual_duration_ms - target_duration_ms) / target_duration_ms

    def _fail_block(
        self,
        block: SemanticBlock,
        error_message: str,
        exc: Exception | None = None,
    ) -> SemanticBlock:
        block.status = BlockStatus.FAILED.value
        block.error_message = error_message
        block.error_type = self._classify_error(exc)
        if exc is not None:
            logger.exception("Failed to process semantic block %s", block.block_id, exc_info=exc)
        else:
            logger.error("Failed to process semantic block %s: %s", block.block_id, error_message)
        return block

    def _classify_error(self, exc: Exception | None) -> str:
        if exc is None:
            return "alignment_error"
        if isinstance(exc, TTSConfigurationError):
            return "configuration_error"
        if isinstance(exc, TTSProviderTimeoutError):
            return "provider_timeout"
        if isinstance(exc, TTSProviderNetworkError):
            return "provider_network_error"
        if isinstance(exc, TTSProviderUnavailableError):
            return "provider_unavailable"
        if isinstance(exc, TTSProviderResponseFormatError):
            return "invalid_provider_response_format"
        if isinstance(exc, TTSInvalidAudioPayloadError):
            return "invalid_audio_payload"
        if isinstance(exc, TTSOutputFileWriteError):
            return "output_file_write_failure"
        if isinstance(exc, TTSProviderOutputError):
            return "invalid_provider_output"
        if isinstance(exc, TTSError):
            return "tts_error"
        return "alignment_error"
