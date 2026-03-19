from dataclasses import dataclass, field
from pathlib import Path

from core.enums import BlockStatus, StageStatus
from core.exceptions import WorkflowError
from core.models import SemanticBlock, summarize_block_text_layers
from core.retry import read_provider_retry_report, reset_provider_retry_report
from modules.alignment.alignment_orchestrator import AlignmentOrchestrator
from modules.workflow.restore_policy import build_cache_restore_audit
from modules.workflow.stage_helpers import (
    build_artifacts_payload,
    get_stage_payload_value,
    read_stage_payload,
    resolve_cache_execution_mode,
)
from services.cache_manager import CacheManager
from services.state_manager import StateManager
from services.tts_provider import build_tts_block_runtime_context, classify_tts_error


@dataclass(slots=True)
class AlignmentStageRunnerConfig:
    provider_name: str
    voice_name: str = "default"
    model_name: str | None = None
    provider_mode: str = "mock"
    version_context: dict[str, object] = field(default_factory=dict)
    fallback_applied: bool = False
    fallback_reason: str | None = None
    fallback_stage: str | None = None


class AlignmentStageRunner:
    """Execute block alignment while preserving cache restore behavior."""

    def __init__(
        self,
        alignment_orchestrator: AlignmentOrchestrator,
        state_manager: StateManager,
        cache_manager: CacheManager,
        config: AlignmentStageRunnerConfig,
    ) -> None:
        self.alignment_orchestrator = alignment_orchestrator
        self.state_manager = state_manager
        self.cache_manager = cache_manager
        self.config = config

    def run(self, blocks: list[SemanticBlock]) -> list[SemanticBlock]:
        stage_name = "alignment"
        previous_stage = self.state_manager.get_stage(stage_name)
        previous_payload = read_stage_payload(previous_stage) if previous_stage is not None else None
        source_input_hash = self._read_source_input_hash()
        audio_paths: list[str] = []
        block_summaries: list[dict[str, object]] = []
        block_hashes: list[str] = []
        reused_block_hashes: list[str] = []
        block_voice_audits: list[dict[str, object]] = []
        reset_provider_retry_report(self.alignment_orchestrator.tts_service)
        self.state_manager.set_stage(
            stage_name,
            StageStatus.RUNNING,
            self._build_provider_run_report(),
        )
        try:
            aligned_blocks: list[SemanticBlock] = []
            cache_hit_blocks = 0

            for block in blocks:
                block_runtime_context = build_tts_block_runtime_context(
                    self.alignment_orchestrator.tts_service,
                    block,
                    default_voice_name=self.config.voice_name,
                    default_version_context=self.config.version_context,
                )
                resolved_voice_name = str(block_runtime_context["voice_name"])
                resolved_voice_id = block_runtime_context["resolved_voice_id"]
                voice_resolution_source = block_runtime_context["voice_resolution_source"]
                resolved_version_context = dict(block_runtime_context["version_context"])
                block_voice_audits.append(
                    {
                        "block_id": block.block_id,
                        "speaker_id": block.speaker_id,
                        "voice_id": resolved_voice_id,
                        "voice_resolution_source": voice_resolution_source,
                    }
                )
                block_hash = self.cache_manager.build_tts_hash(
                    block,
                    provider_name=self.config.provider_name,
                    voice_name=resolved_voice_name,
                    model_name=self.config.model_name,
                    version_context=resolved_version_context,
                )
                block_hashes.append(block_hash)
                cached_entry = self.cache_manager.get_entry("tts_block", block_hash)
                if cached_entry is not None and self._restore_aligned_block(block, cached_entry.get("payload", {})):
                    cache_hit_blocks += 1
                    reused_block_hashes.append(block_hash)
                else:
                    block = self.alignment_orchestrator.process_block(block)
                    if block.status == BlockStatus.FAILED.value:
                        workflow_error = WorkflowError(block.error_message or f"Alignment failed for {block.block_id}.")
                        setattr(workflow_error, "error_type", block.error_type or "alignment_error")
                        setattr(
                            workflow_error,
                            "retry_candidate",
                            self._is_retry_candidate(block.error_type),
                        )
                        raise workflow_error
                    self.cache_manager.set_entry(
                        "tts_block",
                        block_hash,
                        {
                            "block_id": block.block_id,
                            "tts_audio_path": block.tts_audio_path,
                            "aligned_audio_path": block.aligned_audio_path,
                            "actual_audio_duration_ms": block.actual_audio_duration_ms,
                            "rewrite_count": block.rewrite_count,
                            "merged_cn_text": block.merged_cn_text,
                            "merged_literal_cn_text": block.merged_literal_cn_text,
                            "merged_tts_cn_text": block.merged_tts_cn_text,
                            "final_cn_lines": block.final_cn_lines,
                            "status": block.status,
                            "provider_name": self.config.provider_name,
                            "model_name": self.config.model_name,
                            "voice_name": resolved_voice_name,
                            "resolved_voice_id": resolved_voice_id,
                            "voice_resolution_source": voice_resolution_source,
                            "provider_mode": self.config.provider_mode,
                            "version_context": resolved_version_context,
                        },
                    )

                resolved_audio_path = block.aligned_audio_path or block.tts_audio_path
                if resolved_audio_path:
                    audio_paths.append(resolved_audio_path)
                block_summaries.append(
                    {
                        "block_id": block.block_id,
                        "status": block.status,
                        "audio_path": resolved_audio_path,
                    }
                )
                aligned_blocks.append(block)

            retry_report = read_provider_retry_report(self.alignment_orchestrator.tts_service)
            restore_audit = build_cache_restore_audit(
                stage_name=stage_name,
                previous_payload=previous_payload,
                source_input_hash=source_input_hash,
                context_fields={
                    "provider_name": self.config.provider_name,
                    "model_name": self.config.model_name,
                    "voice_name": self.config.voice_name,
                    "version_context": self.config.version_context,
                },
                total_units=len(blocks),
                cache_hits=cache_hit_blocks,
                artifact_paths=audio_paths,
                reused_artifacts=reused_block_hashes,
            )
            text_layer_summary = summarize_block_text_layers(aligned_blocks)
            payload = self._build_provider_run_report(
                execution_mode=resolve_cache_execution_mode(cache_hit_blocks, len(blocks)),
                retry_attempted=bool(retry_report["retry_attempted"]),
                retry_count=int(retry_report["retry_count"]),
                retry_candidate=retry_report["retry_candidate"],
            )
            voice_audit_summary = self._summarize_voice_audit(block_voice_audits)
            payload.update(
                {
                    **restore_audit,
                    "voice_name": self.config.voice_name,
                    "resolved_speaker_id": voice_audit_summary["resolved_speaker_id"],
                    "resolved_voice_id": voice_audit_summary["resolved_voice_id"],
                    "voice_resolution_source": voice_audit_summary["voice_resolution_source"],
                    "literal_text_layer_produced": text_layer_summary["literal_block_count"] > 0,
                    "tts_text_layer_produced": text_layer_summary["tts_block_count"] > 0,
                    "text_layer_summary": text_layer_summary,
                    "block_count": len(aligned_blocks),
                    "cache_hit_blocks": cache_hit_blocks,
                    "fallback_stage": self.config.fallback_stage,
                    "artifacts": build_artifacts_payload(
                        kind="aligned_audio",
                        file_paths=audio_paths,
                        extra={
                            "block_hashes": block_hashes,
                            "blocks": block_summaries,
                            "version_context": self.config.version_context,
                            "voice_resolution_summary": block_voice_audits,
                        },
                    ),
                }
            )
            self.state_manager.set_stage(
                stage_name,
                StageStatus.DONE,
                payload,
            )
            return aligned_blocks
        except Exception as exc:
            error_info = classify_tts_error(exc)
            if hasattr(exc, "error_type"):
                error_info["error_type"] = getattr(exc, "error_type")
            if hasattr(exc, "retry_candidate"):
                error_info["retry_candidate"] = getattr(exc, "retry_candidate")
            retry_report = read_provider_retry_report(self.alignment_orchestrator.tts_service)
            restore_audit = build_cache_restore_audit(
                stage_name=stage_name,
                previous_payload=previous_payload,
                source_input_hash=source_input_hash,
                context_fields={
                    "provider_name": self.config.provider_name,
                    "model_name": self.config.model_name,
                    "voice_name": self.config.voice_name,
                    "version_context": self.config.version_context,
                },
                total_units=len(blocks),
                cache_hits=len(reused_block_hashes),
                artifact_paths=audio_paths,
                reused_artifacts=reused_block_hashes,
            )
            payload = self._build_provider_run_report(
                error_type=str(error_info["error_type"]),
                retry_attempted=bool(retry_report["retry_attempted"]),
                retry_count=int(retry_report["retry_count"]),
                retry_candidate=retry_report["retry_candidate"]
                if retry_report["retry_candidate"] is not None
                else bool(error_info["retry_candidate"]),
                final_error_type=str(error_info["error_type"]),
                final_error_message=str(exc),
            )
            voice_audit_summary = self._summarize_voice_audit(block_voice_audits)
            payload.update(
                {
                    **restore_audit,
                    "voice_name": self.config.voice_name,
                    "resolved_speaker_id": voice_audit_summary["resolved_speaker_id"],
                    "resolved_voice_id": voice_audit_summary["resolved_voice_id"],
                    "voice_resolution_source": voice_audit_summary["voice_resolution_source"],
                    "literal_text_layer_produced": False,
                    "tts_text_layer_produced": False,
                    "text_layer_summary": {
                        "literal_block_count": 0,
                        "tts_block_count": 0,
                        "compat_block_count": 0,
                    },
                    "fallback_stage": self.config.fallback_stage,
                }
            )
            self.state_manager.set_stage(
                stage_name,
                StageStatus.FAILED,
                payload=payload,
                error_message=str(exc),
            )
            raise WorkflowError("Alignment stage failed.") from exc

    def _build_provider_run_report(
        self,
        *,
        execution_mode: str | None = None,
        error_type: str | None = None,
        retry_attempted: bool = False,
        retry_count: int = 0,
        retry_candidate: bool | None = None,
        final_error_type: str | None = None,
        final_error_message: str | None = None,
    ) -> dict[str, object]:
        return {
            "provider": self.config.provider_name,
            "provider_name": self.config.provider_name,
            "provider_mode": self.config.provider_mode,
            "model_name": self.config.model_name,
            "version_context": self.config.version_context,
            "execution_mode": execution_mode,
            "fallback_applied": self.config.fallback_applied,
            "fallback_reason": self.config.fallback_reason,
            "fallback_trigger": None,
            "fallback_from": None,
            "fallback_to": None,
            "retry_attempted": retry_attempted,
            "retry_count": retry_count,
            "error_type": error_type,
            "retry_candidate": retry_candidate,
            "final_error_type": final_error_type,
            "final_error_message": final_error_message,
            "resolved_speaker_id": None,
            "resolved_voice_id": None,
            "voice_resolution_source": None,
        }

    def _restore_aligned_block(self, block: SemanticBlock, payload: dict[str, object]) -> bool:
        aligned_audio_path = payload.get("aligned_audio_path")
        tts_audio_path = payload.get("tts_audio_path")
        candidate_audio_path = aligned_audio_path or tts_audio_path
        if not isinstance(candidate_audio_path, str) or not Path(candidate_audio_path).exists():
            return False

        block.tts_audio_path = tts_audio_path if isinstance(tts_audio_path, str) else None
        block.aligned_audio_path = aligned_audio_path if isinstance(aligned_audio_path, str) else block.tts_audio_path
        block.actual_audio_duration_ms = int(payload.get("actual_audio_duration_ms", block.target_duration_ms))
        block.rewrite_count = int(payload.get("rewrite_count", 0))
        block.status = str(payload.get("status", BlockStatus.ALIGN_DONE.value))
        block.merged_cn_text = str(payload.get("merged_cn_text", block.merged_cn_text))
        if "merged_literal_cn_text" in payload:
            block.merged_literal_cn_text = str(payload.get("merged_literal_cn_text", block.merged_literal_cn_text))
        if "merged_tts_cn_text" in payload:
            block.merged_tts_cn_text = str(payload.get("merged_tts_cn_text", block.merged_tts_cn_text))
        block.error_type = None
        final_cn_lines = payload.get("final_cn_lines", [])
        if isinstance(final_cn_lines, list) and all(isinstance(text, str) for text in final_cn_lines):
            block.final_cn_lines = final_cn_lines
        return True

    def _is_retry_candidate(self, error_type: str | None) -> bool:
        return error_type in {"provider_timeout", "provider_network_error"}

    def _read_source_input_hash(self) -> str | None:
        input_hash = get_stage_payload_value(self.state_manager, "ingestion", "input_hash")
        return input_hash if isinstance(input_hash, str) else None

    def _summarize_voice_audit(self, voice_audits: list[dict[str, object]]) -> dict[str, object]:
        speaker_ids = {
            speaker_id.strip()
            for speaker_id in (audit.get("speaker_id") for audit in voice_audits)
            if isinstance(speaker_id, str) and speaker_id.strip()
        }
        resolved_voice_ids = {
            voice_id.strip()
            for voice_id in (audit.get("voice_id") for audit in voice_audits)
            if isinstance(voice_id, str) and voice_id.strip()
        }
        resolution_sources = {
            source.strip()
            for source in (audit.get("voice_resolution_source") for audit in voice_audits)
            if isinstance(source, str) and source.strip()
        }
        return {
            "resolved_speaker_id": next(iter(speaker_ids)) if len(speaker_ids) == 1 else None,
            "resolved_voice_id": next(iter(resolved_voice_ids)) if len(resolved_voice_ids) == 1 else None,
            "voice_resolution_source": next(iter(resolution_sources)) if len(resolution_sources) == 1 else None,
        }
