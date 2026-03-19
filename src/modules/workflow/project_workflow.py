from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path

from core.artifact_index import ArtifactIndex
from core.enums import StageStatus
from core.exceptions import WorkflowError
from core.models import (
    SemanticBlock,
    SubtitleLine,
    summarize_block_text_layers,
    summarize_subtitle_text_layers,
)
from modules.alignment.alignment_orchestrator import AlignmentOrchestrator
from modules.chunking.semantic_block_builder import SemanticBlockBuilder
from modules.draft.caption_retiming import CaptionRetimer
from modules.draft.draft_writer import DraftWriteResult, DraftWriter
from modules.ingestion.providers import SubtitleSourceProvider
from modules.media_understanding.models import (
    AttributedTranscriptLine,
    MediaSource,
    MediaSourceKind,
    MediaUnderstandingResult,
    TranscriptLine,
    describe_authoritative_flow,
    is_authoritative_media_source_kind,
    uses_transcript_extraction_authoritative_path,
)
from modules.media_understanding.normalizer import AttributedTranscriptNormalizer
from modules.media_understanding.pipeline import MediaUnderstandingPipeline
from modules.media_understanding.providers import classify_media_understanding_error
from modules.translation.translator import TranslationPipeline
from modules.workflow.alignment_stage_runner import (
    AlignmentStageRunner,
    AlignmentStageRunnerConfig,
)
from modules.workflow.draft_stage_runner import DraftStageRunner, DraftStageRunnerConfig
from modules.workflow.project_builder import ProjectBuilder
from modules.workflow.project_shape_helpers import (
    build_canonical_source_info,
    build_core_media_artifact_entries,
    build_editor_artifact_entries,
)
from modules.workflow.restore_policy import build_always_rerun_audit
from modules.workflow.stage_helpers import build_artifacts_payload, get_stage_payload_value, read_stage_payload
from modules.workflow.translation_stage_runner import (
    TranslationStageRunner,
    TranslationStageRunnerConfig,
)
from modules.workflow.workflow_result import WorkflowBuildResult
from services.audio.source_audio_preparation import (
    SourceAudioPreparationRequest,
    SourceAudioPreparationService,
)
from services.cache_manager import CacheManager
from services.state_manager import StateManager


@dataclass(slots=True)
class ProjectWorkflowConfig:
    project_id: str
    translation_provider_name: str = "mock_translator"
    translation_target_language: str = "zh-CN"
    translation_model_name: str | None = None
    translation_provider_mode: str = "mock"
    translation_version_context: dict[str, object] = field(default_factory=dict)
    translation_fallback_applied: bool = False
    translation_fallback_reason: str | None = None
    translation_fallback_stage: str | None = None
    translation_runtime_fallback_enabled: bool = False
    translation_fallback_from: str | None = None
    translation_fallback_to: str | None = None
    tts_provider_name: str = "mock_tts"
    tts_voice_name: str = "default"
    tts_model_name: str | None = None
    tts_provider_mode: str = "mock"
    tts_version_context: dict[str, object] = field(default_factory=dict)
    tts_fallback_applied: bool = False
    tts_fallback_reason: str | None = None
    tts_fallback_stage: str | None = None


@dataclass(slots=True)
class ProjectWorkflowResult:
    project_id: str
    draft_dir: str
    draft_content_path: str
    draft_meta_info_path: str
    export_path: str | None
    block_count: int
    caption_count: int
    material_count: int
    stage_snapshot: dict[str, object] = field(default_factory=dict)


class ProjectWorkflow:
    """Run the end-to-end project workflow to an internal draft scaffold."""

    def __init__(
        self,
        subtitle_provider: SubtitleSourceProvider | None,
        translation_pipeline: TranslationPipeline,
        block_builder: SemanticBlockBuilder,
        alignment_orchestrator: AlignmentOrchestrator,
        caption_retimer: CaptionRetimer,
        draft_writer: DraftWriter,
        state_manager: StateManager,
        cache_manager: CacheManager,
        config: ProjectWorkflowConfig,
        media_understanding_pipeline: MediaUnderstandingPipeline | None = None,
        media_source: MediaSource | None = None,
        source_audio_preparation_service: SourceAudioPreparationService | None = None,
    ) -> None:
        self.subtitle_provider = subtitle_provider
        self.translation_pipeline = translation_pipeline
        self.block_builder = block_builder
        self.alignment_orchestrator = alignment_orchestrator
        self.caption_retimer = caption_retimer
        self.draft_writer = draft_writer
        self.state_manager = state_manager
        self.cache_manager = cache_manager
        self.config = config
        self.media_understanding_pipeline = media_understanding_pipeline
        self.media_source = media_source
        self.media_understanding_normalizer = AttributedTranscriptNormalizer()
        self.project_builder = ProjectBuilder()
        self.source_audio_preparation_service = source_audio_preparation_service or SourceAudioPreparationService()
        self.translation_stage_runner = TranslationStageRunner(
            translation_pipeline=self.translation_pipeline,
            state_manager=self.state_manager,
            cache_manager=self.cache_manager,
            config=TranslationStageRunnerConfig(
                provider_name=self.config.translation_provider_name,
                target_language=self.config.translation_target_language,
                model_name=self.config.translation_model_name,
                provider_mode=self.config.translation_provider_mode,
                version_context=self.config.translation_version_context,
                fallback_applied=self.config.translation_fallback_applied,
                fallback_reason=self.config.translation_fallback_reason,
                fallback_stage=self.config.translation_fallback_stage,
                runtime_fallback_enabled=self.config.translation_runtime_fallback_enabled,
                fallback_from=self.config.translation_fallback_from,
                fallback_to=self.config.translation_fallback_to,
            ),
        )
        self.alignment_stage_runner = AlignmentStageRunner(
            alignment_orchestrator=self.alignment_orchestrator,
            state_manager=self.state_manager,
            cache_manager=self.cache_manager,
            config=AlignmentStageRunnerConfig(
                provider_name=self.config.tts_provider_name,
                voice_name=self.config.tts_voice_name,
                model_name=self.config.tts_model_name,
                provider_mode=self.config.tts_provider_mode,
                version_context=self.config.tts_version_context,
                fallback_applied=self.config.tts_fallback_applied,
                fallback_reason=self.config.tts_fallback_reason,
                fallback_stage=self.config.tts_fallback_stage,
            ),
        )
        self.draft_stage_runner = DraftStageRunner(
            caption_retimer=self.caption_retimer,
            draft_writer=self.draft_writer,
            state_manager=self.state_manager,
            config=DraftStageRunnerConfig(project_id=self.config.project_id),
        )

    def run(self) -> ProjectWorkflowResult:
        build_result = self.run_build()
        return self.build_legacy_result(build_result)

    def build_legacy_result(self, build_result: WorkflowBuildResult) -> ProjectWorkflowResult:
        return self._build_legacy_result(build_result)

    def run_build(self) -> WorkflowBuildResult:
        self.state_manager.set_project(self.config.project_id)

        subtitle_lines = self._run_ingestion_stage()
        self._run_audio_preparation_stage()
        source_lines = self._run_media_understanding_stage(subtitle_lines)
        translated_lines = self._run_translation_stage(source_lines)
        blocks = self._run_chunking_stage(translated_lines)
        aligned_blocks = self._run_alignment_stage(blocks)
        self._apply_alignment_text_layers(translated_lines, aligned_blocks)
        self._run_draft_stage(translated_lines, aligned_blocks)
        stage_snapshot = self.state_manager.load().get("stages", {})
        artifact_index = self._build_artifact_index(stage_snapshot)
        return self.project_builder.build_result(
            project_id=self.config.project_id,
            source_info=self._build_source_info(stage_snapshot),
            artifact_index=artifact_index,
            stage_snapshot=stage_snapshot,
            stage_outputs={
                "translated_lines": translated_lines,
                "blocks": blocks,
                "aligned_blocks": aligned_blocks,
            },
        )
    def _build_legacy_result(self, build_result: WorkflowBuildResult) -> ProjectWorkflowResult:
        draft_payload = _read_stage_payload_from_snapshot(build_result.stage_snapshot, "draft")

        draft_dir = _require_non_empty_str(draft_payload, "draft_dir")
        draft_content_path = _require_non_empty_str(draft_payload, "draft_content_path")
        draft_meta_info_path = _require_non_empty_str(draft_payload, "draft_meta_info_path")
        export_path = draft_payload.get("export_path")
        if export_path is not None and not isinstance(export_path, str):
            raise WorkflowError("Draft stage payload contains invalid export_path.")

        return ProjectWorkflowResult(
            project_id=build_result.project_id,
            draft_dir=draft_dir,
            draft_content_path=draft_content_path,
            draft_meta_info_path=draft_meta_info_path,
            export_path=export_path,
            block_count=_read_int_payload_value(draft_payload, "block_count"),
            caption_count=_read_int_payload_value(draft_payload, "caption_count"),
            material_count=_read_int_payload_value(draft_payload, "material_count"),
            stage_snapshot=dict(build_result.stage_snapshot),
        )

    def _build_artifact_index(self, stage_snapshot: dict[str, object]) -> ArtifactIndex:
        audio_preparation_payload = _read_stage_payload_from_snapshot(stage_snapshot, "audio_preparation")
        artifact_entries = build_core_media_artifact_entries(
            source_original_audio=audio_preparation_payload.get("source_audio_path"),
            working_speech_for_asr=audio_preparation_payload.get("speech_audio_path"),
            working_ambient_audio=audio_preparation_payload.get("ambient_audio_path"),
        )

        if self.media_source is not None and isinstance(self.media_source.locator, str) and self.media_source.locator.strip():
            if self.media_source.kind == MediaSourceKind.LOCAL_AUDIO:
                artifact_entries.extend(
                    build_core_media_artifact_entries(
                        source_original_audio=self.media_source.locator,
                    )
                )
            elif self.media_source.kind == MediaSourceKind.LOCAL_VIDEO:
                artifact_entries.extend(
                    build_core_media_artifact_entries(
                        source_original_video=self.media_source.locator,
                    )
                )

        draft_payload = _read_stage_payload_from_snapshot(stage_snapshot, "draft")
        artifact_entries.extend(
            build_editor_artifact_entries(
                editor_draft_dir=draft_payload.get("draft_dir"),
                editor_draft_content=draft_payload.get("draft_content_path"),
                editor_draft_meta=draft_payload.get("draft_meta_info_path"),
                editor_material_dir=draft_payload.get("material_dir"),
                editor_export_json=draft_payload.get("export_path"),
            )
        )

        return self.project_builder.build_artifact_index(artifact_entries)

    def _build_source_info(self, stage_snapshot: dict[str, object]) -> dict[str, object]:
        media_payload = _read_stage_payload_from_snapshot(stage_snapshot, "media_understanding")
        source_kind = media_payload.get("source_kind")
        if not isinstance(source_kind, str) or not source_kind:
            source_kind = self.media_source.kind.value if self.media_source is not None else "ingested_subtitle_lines"

        source_path = media_payload.get("source_path")
        if isinstance(source_path, str) and source_path:
            resolved_source_path = source_path
        elif self.media_source is not None:
            resolved_source_path = self._read_media_source_path()
        else:
            resolved_source_path = None

        locator = (
            self.media_source.locator
            if self.media_source is not None and isinstance(self.media_source.locator, str) and self.media_source.locator.strip()
            else None
        )
        metadata = dict(self.media_source.metadata) if self.media_source is not None and self.media_source.metadata else None

        authoritative_input_used = media_payload.get("authoritative_input_used")
        authoritative_path_kind = media_payload.get("authoritative_path_kind")
        authoritative_flow = media_payload.get("authoritative_flow")
        input_hash = _read_input_hash(self.state_manager)
        return build_canonical_source_info(
            source_kind=source_kind,
            locator=locator,
            source_path=resolved_source_path if isinstance(resolved_source_path, str) else None,
            metadata=metadata,
            authoritative_input_used=(
                authoritative_input_used if isinstance(authoritative_input_used, bool) else None
            ),
            authoritative_path_kind=(
                authoritative_path_kind if isinstance(authoritative_path_kind, str) else None
            ),
            authoritative_flow=authoritative_flow if isinstance(authoritative_flow, str) else None,
            source_input_hash=input_hash if isinstance(input_hash, str) else None,
        )

    def _run_ingestion_stage(self) -> list[SubtitleLine]:
        stage_name = "ingestion"
        provider_name = type(self.subtitle_provider).__name__ if self.subtitle_provider is not None else "MediaSourceReference"
        self.state_manager.set_stage(stage_name, StageStatus.RUNNING, {"provider": provider_name})
        try:
            if self._uses_authoritative_media_source():
                assert self.media_source is not None
                input_hash = _build_media_source_hash(self.media_source)
                cache_hit = self.cache_manager.has_entry("media_source_input", input_hash)
                if not cache_hit:
                    self.cache_manager.set_entry(
                        "media_source_input",
                        input_hash,
                        {
                            "source_kind": self.media_source.kind.value,
                            "locator": self.media_source.locator,
                        },
                    )
                line_count = len(self.media_source.transcript_lines) or len(self.media_source.attributed_lines)
                restore_audit = build_always_rerun_audit(
                    source_input_hash=input_hash,
                    artifact_paths=[],
                    rerun_reason="media_source_reference_load_required",
                )
                self.state_manager.set_stage(
                    stage_name,
                    StageStatus.DONE,
                    {
                        "provider": provider_name,
                        "line_count": line_count,
                        "input_hash": input_hash,
                        "cache_hit": cache_hit,
                        "execution_mode": "source_reference",
                        "artifacts": build_artifacts_payload(
                            kind="media_source",
                            file_paths=[],
                            extra={
                                "source_kind": self.media_source.kind.value,
                                "locator": self.media_source.locator,
                            },
                        ),
                        **restore_audit,
                    },
                )
                return []

            if self.subtitle_provider is None:
                raise WorkflowError("subtitle_provider is required when authoritative media source is not configured.")
            subtitle_lines = self.subtitle_provider.load_subtitles()
            input_hash = self.cache_manager.build_input_hash(subtitle_lines)
            cache_hit = self.cache_manager.has_entry("subtitle_input", input_hash)
            if not cache_hit:
                self.cache_manager.set_entry("subtitle_input", input_hash, {"line_count": len(subtitle_lines)})
            restore_audit = build_always_rerun_audit(
                source_input_hash=input_hash,
                artifact_paths=[],
                rerun_reason="source_provider_load_required",
            )
            self.state_manager.set_stage(
                stage_name,
                StageStatus.DONE,
                {
                    "provider": type(self.subtitle_provider).__name__,
                    "line_count": len(subtitle_lines),
                    "input_hash": input_hash,
                    "cache_hit": cache_hit,
                    "execution_mode": "source_load",
                    "artifacts": build_artifacts_payload(
                        kind="subtitle_source",
                        file_paths=[],
                        extra={"source_provider": type(self.subtitle_provider).__name__},
                    ),
                    **restore_audit,
                },
            )
            return subtitle_lines
        except Exception as exc:
            self.state_manager.set_stage(stage_name, StageStatus.FAILED, error_message=str(exc))
            raise WorkflowError("Ingestion stage failed.") from exc

    def _run_audio_preparation_stage(self) -> None:
        if self.media_source is None or self.media_source.kind not in {MediaSourceKind.LOCAL_AUDIO, MediaSourceKind.LOCAL_VIDEO}:
            return

        stage_name = "audio_preparation"
        source_kind = self.media_source.kind.value
        source_path = self._read_media_source_path()
        base_payload = {
            "source_kind": source_kind,
            "source_audio_path": None,
            "speech_audio_path": None,
            "ambient_audio_path": None,
            "execution_mode": None,
            "reused_cache": False,
            "skipped": False,
        }
        self.state_manager.set_stage(stage_name, StageStatus.RUNNING, payload=base_payload)

        if self.media_source.kind == MediaSourceKind.LOCAL_VIDEO:
            self._complete_audio_preparation_stage(
                payload=base_payload,
                execution_mode="deferred_local_video",
                source_path=None,
                speech_audio_path=None,
                ambient_audio_path=None,
                reused_cache=False,
                skipped=True,
                rerun_reason="audio_preparation_deferred_local_video",
                extra={"source_kind": source_kind},
            )
            return

        if not isinstance(source_path, str) or not source_path.strip():
            self._complete_audio_preparation_stage(
                payload=base_payload,
                execution_mode="skipped_missing_source",
                source_path=None,
                speech_audio_path=None,
                ambient_audio_path=None,
                reused_cache=False,
                skipped=True,
                rerun_reason="audio_preparation_missing_source",
                extra={"source_kind": source_kind},
            )
            return

        source_audio_path = Path(source_path).expanduser().resolve(strict=False)
        if not source_audio_path.exists() or not source_audio_path.is_file():
            self._complete_audio_preparation_stage(
                payload=base_payload,
                execution_mode="skipped_missing_source",
                source_path=str(source_audio_path),
                speech_audio_path=None,
                ambient_audio_path=None,
                reused_cache=False,
                skipped=True,
                rerun_reason="audio_preparation_missing_source",
                extra={
                    "source_kind": source_kind,
                    "source_audio_path": str(source_audio_path),
                },
            )
            return

        try:
            preparation_result = self.source_audio_preparation_service.prepare(
                SourceAudioPreparationRequest(
                    project_dir=str(self._resolve_workflow_project_dir()),
                    source_audio_path=str(source_audio_path),
                )
            )
            self._complete_audio_preparation_stage(
                payload=base_payload,
                execution_mode="reuse_existing_artifacts" if preparation_result.reused_cache else "fresh_prepare",
                source_path=preparation_result.source_audio_path,
                speech_audio_path=preparation_result.speech_audio_path,
                ambient_audio_path=preparation_result.ambient_audio_path,
                reused_cache=preparation_result.reused_cache,
                skipped=False,
                rerun_reason="audio_preparation_local_audio",
                extra={
                    "source_kind": source_kind,
                    "source_audio_path": preparation_result.source_audio_path,
                },
            )
        except Exception as exc:
            self._complete_audio_preparation_stage(
                payload=base_payload,
                execution_mode="non_blocking_prepare_error",
                source_path=str(source_audio_path),
                speech_audio_path=None,
                ambient_audio_path=None,
                reused_cache=False,
                skipped=True,
                rerun_reason="audio_preparation_non_blocking_error",
                extra={
                    "source_kind": source_kind,
                    "source_audio_path": str(source_audio_path),
                    "error_type": type(exc).__name__,
                    "prepare_error": str(exc),
                },
            )

    def _complete_audio_preparation_stage(
        self,
        *,
        payload: dict[str, object],
        execution_mode: str,
        source_path: str | None,
        speech_audio_path: str | None,
        ambient_audio_path: str | None,
        reused_cache: bool,
        skipped: bool,
        rerun_reason: str,
        extra: dict[str, object],
    ) -> None:
        artifact_paths = [
            path
            for path in (speech_audio_path, ambient_audio_path)
            if isinstance(path, str) and path
        ]
        restore_audit = build_always_rerun_audit(
            source_input_hash=_read_input_hash(self.state_manager),
            artifact_paths=artifact_paths,
            rerun_reason=rerun_reason,
        )
        self.state_manager.set_stage(
            "audio_preparation",
            StageStatus.DONE,
            payload={
                **payload,
                **restore_audit,
                "source_audio_path": source_path,
                "speech_audio_path": speech_audio_path,
                "ambient_audio_path": ambient_audio_path,
                "execution_mode": execution_mode,
                "reused_cache": reused_cache,
                "skipped": skipped,
                "artifacts": build_artifacts_payload(
                    kind="source_audio_preparation",
                    file_paths=artifact_paths,
                    extra=extra,
                ),
            },
        )

    def _run_media_understanding_stage(self, subtitle_lines: list[SubtitleLine]) -> list[SubtitleLine]:
        stage_name = "media_understanding"
        initial_source_kind = self.media_source.kind.value if self.media_source is not None else "ingested_subtitle_lines"
        provider_audit = self._build_media_understanding_provider_audit()
        initial_authoritative_input_used = self._uses_authoritative_media_source()
        initial_authoritative_flow = describe_authoritative_flow(self.media_source.kind) if self.media_source else None
        initial_transcript_extraction_used = (
            uses_transcript_extraction_authoritative_path(self.media_source.kind) if self.media_source else False
        )
        self.state_manager.set_stage(
            stage_name,
            StageStatus.RUNNING,
            {
                "provider": str(provider_audit["provider_name"]),
                "provider_name": provider_audit["provider_name"],
                "provider_mode": provider_audit["provider_mode"],
                "extraction_provider_name": provider_audit["extraction_provider_name"],
                "extraction_provider_mode": provider_audit["extraction_provider_mode"],
                "extraction_version_context": provider_audit["extraction_version_context"],
                "source_kind": initial_source_kind,
                "source_path": self._read_media_source_path(),
                "execution_mode": None,
                "error_type": None,
                "authoritative_input_used": initial_authoritative_input_used,
                "authoritative_path_kind": initial_source_kind if initial_authoritative_input_used else None,
                "authoritative_flow": initial_authoritative_flow,
                "transcript_extraction_used": initial_transcript_extraction_used,
                "attributed_transcript_normalized": False,
                "subtitle_line_bridge_applied": False,
                "fallback_applied": provider_audit["fallback_applied"],
                "fallback_reason": provider_audit["fallback_reason"],
                "fallback_stage": provider_audit["fallback_stage"],
                "version_context": provider_audit["version_context"],
            },
        )
        try:
            result = self._resolve_media_understanding_result(subtitle_lines)
            source_kind = result.source.kind.value if result.source is not None else "ingested_subtitle_lines"
            rerun_reason = (
                "media_understanding_authoritative_run"
                if self._uses_authoritative_media_source()
                else "media_understanding_stage_placeholder"
            )
            restore_audit = build_always_rerun_audit(
                source_input_hash=_read_input_hash(self.state_manager),
                artifact_paths=[],
                rerun_reason=rerun_reason,
            )
            self.state_manager.set_stage(
                stage_name,
                StageStatus.DONE,
                {
                    "provider": str(provider_audit["provider_name"]),
                    "provider_name": provider_audit["provider_name"],
                    "provider_mode": provider_audit["provider_mode"],
                    "extraction_provider_name": provider_audit["extraction_provider_name"],
                    "extraction_provider_mode": provider_audit["extraction_provider_mode"],
                    "extraction_version_context": provider_audit["extraction_version_context"],
                    "execution_mode": result.execution_mode,
                    "source_kind": source_kind,
                    "source_path": self._read_media_source_path(result.source),
                    "error_type": None,
                    "authoritative_input_used": result.authoritative_input_used,
                    "authoritative_path_kind": result.authoritative_path_kind,
                    "authoritative_flow": result.authoritative_flow,
                    "transcript_extraction_used": result.transcript_extraction_used,
                    "attributed_transcript_normalized": result.attributed_transcript_normalized,
                    "subtitle_line_bridge_applied": result.subtitle_line_bridge_applied,
                    "fallback_applied": provider_audit["fallback_applied"],
                    "fallback_reason": provider_audit["fallback_reason"],
                    "fallback_stage": provider_audit["fallback_stage"],
                    "version_context": provider_audit["version_context"],
                    "line_count": len(result.subtitle_lines),
                    "attributed_line_count": len(result.attributed_lines),
                    "artifacts": build_artifacts_payload(
                        kind="attributed_transcript",
                        file_paths=[],
                        extra={
                            "provider": provider_audit["provider_name"],
                            "source_kind": source_kind,
                            "provider_mode": provider_audit["provider_mode"],
                            "extraction_provider_name": provider_audit["extraction_provider_name"],
                            "extraction_provider_mode": provider_audit["extraction_provider_mode"],
                            "extraction_version_context": provider_audit["extraction_version_context"],
                            "source_path": self._read_media_source_path(result.source),
                            "authoritative_input_used": result.authoritative_input_used,
                            "authoritative_path_kind": result.authoritative_path_kind,
                            "authoritative_flow": result.authoritative_flow,
                            "transcript_extraction_used": result.transcript_extraction_used,
                        },
                    ),
                    **restore_audit,
                },
            )
            return result.subtitle_lines
        except Exception as exc:
            error_info = classify_media_understanding_error(exc)
            self.state_manager.set_stage(
                stage_name,
                StageStatus.FAILED,
                payload={
                    "provider": str(provider_audit["provider_name"]),
                    "provider_name": provider_audit["provider_name"],
                    "provider_mode": provider_audit["provider_mode"],
                    "extraction_provider_name": provider_audit["extraction_provider_name"],
                    "extraction_provider_mode": provider_audit["extraction_provider_mode"],
                    "extraction_version_context": provider_audit["extraction_version_context"],
                    "source_kind": initial_source_kind,
                    "source_path": self._read_media_source_path(),
                    "execution_mode": "failed",
                    "error_type": error_info["error_type"],
                    "authoritative_input_used": initial_authoritative_input_used,
                    "authoritative_path_kind": initial_source_kind if initial_authoritative_input_used else None,
                    "authoritative_flow": initial_authoritative_flow,
                    "transcript_extraction_used": initial_transcript_extraction_used,
                    "attributed_transcript_normalized": False,
                    "subtitle_line_bridge_applied": False,
                    "fallback_applied": provider_audit["fallback_applied"],
                    "fallback_reason": provider_audit["fallback_reason"],
                    "fallback_stage": provider_audit["fallback_stage"],
                    "version_context": provider_audit["version_context"],
                },
                error_message=str(exc),
            )
            raise WorkflowError("Media understanding stage failed.") from exc

    def _run_translation_stage(self, subtitle_lines: list[SubtitleLine]) -> list[SubtitleLine]:
        return self.translation_stage_runner.run(subtitle_lines)

    def _run_chunking_stage(self, translated_lines: list[SubtitleLine]) -> list[SemanticBlock]:
        stage_name = "chunking"
        self.state_manager.set_stage(stage_name, StageStatus.RUNNING)
        try:
            blocks = self.block_builder.build(translated_lines)
            text_layer_summary = summarize_block_text_layers(blocks)
            restore_audit = build_always_rerun_audit(
                source_input_hash=_read_input_hash(self.state_manager),
                artifact_paths=[],
                rerun_reason="chunking_always_recomputed",
            )
            self.state_manager.set_stage(
                stage_name,
                StageStatus.DONE,
                {
                    "block_count": len(blocks),
                    "literal_text_layer_produced": text_layer_summary["literal_block_count"] > 0,
                    "tts_text_layer_produced": text_layer_summary["tts_block_count"] > 0,
                    "text_layer_summary": text_layer_summary,
                    "execution_mode": "fresh_build",
                    "artifacts": build_artifacts_payload(
                        kind="semantic_blocks",
                        file_paths=[],
                        extra={"block_ids": [block.block_id for block in blocks]},
                    ),
                    **restore_audit,
                },
            )
            return blocks
        except Exception as exc:
            self.state_manager.set_stage(stage_name, StageStatus.FAILED, error_message=str(exc))
            raise WorkflowError("Chunking stage failed.") from exc

    def _run_alignment_stage(self, blocks: list[SemanticBlock]) -> list[SemanticBlock]:
        return self.alignment_stage_runner.run(blocks)

    def _apply_alignment_text_layers(
        self,
        translated_lines: list[SubtitleLine],
        aligned_blocks: list[SemanticBlock],
    ) -> None:
        translated_line_map = {line.index: line for line in translated_lines}

        for block in aligned_blocks:
            resolved_line_texts: list[str] = []
            if block.final_cn_lines and len(block.final_cn_lines) == len(block.original_srt_indices):
                resolved_line_texts = [text.strip() for text in block.final_cn_lines]
            elif block.cn_line_texts and len(block.cn_line_texts) == len(block.original_srt_indices):
                resolved_line_texts = [text.strip() for text in block.cn_line_texts]

            if len(resolved_line_texts) != len(block.original_srt_indices):
                continue

            for line_index, spoken_text in zip(block.original_srt_indices, resolved_line_texts):
                target_line = translated_line_map.get(line_index)
                if target_line is None or not spoken_text:
                    continue
                target_line.tts_cn_text = spoken_text

        line_text_layer_summary = summarize_subtitle_text_layers(translated_lines)
        block_text_layer_summary = summarize_block_text_layers(aligned_blocks)
        alignment_stage = self.state_manager.get_stage("alignment")
        if alignment_stage is None:
            return

        updated_payload = dict(read_stage_payload(alignment_stage))
        updated_payload["literal_text_layer_produced"] = (
            block_text_layer_summary["literal_block_count"] > 0
            or line_text_layer_summary["literal_line_count"] > 0
        )
        updated_payload["tts_text_layer_produced"] = (
            block_text_layer_summary["tts_block_count"] > 0
            or line_text_layer_summary["tts_line_count"] > 0
        )
        updated_payload["text_layer_summary"] = {
            **block_text_layer_summary,
            **line_text_layer_summary,
        }
        self.state_manager.set_stage(
            "alignment",
            alignment_stage["status"],
            payload=updated_payload,
        )

    def _run_draft_stage(
        self,
        translated_lines: list[SubtitleLine],
        aligned_blocks: list[SemanticBlock],
    ) -> DraftWriteResult:
        return self.draft_stage_runner.run(translated_lines, aligned_blocks)

    def _resolve_media_understanding_result(
        self,
        subtitle_lines: list[SubtitleLine],
    ) -> MediaUnderstandingResult:
        if self._uses_authoritative_media_source() and self.media_understanding_pipeline is None:
            raise WorkflowError("media_understanding_pipeline is required for authoritative media source inputs.")
        if self.media_understanding_pipeline is not None and self.media_source is not None:
            return self.media_understanding_pipeline.run(self.media_source)
        if self.media_understanding_pipeline is not None:
            return self.media_understanding_pipeline.passthrough_subtitle_lines(subtitle_lines)

        attributed_lines = self.media_understanding_normalizer.from_subtitle_lines(subtitle_lines)
        return MediaUnderstandingResult(
            source=None,
            attributed_lines=attributed_lines,
            subtitle_lines=list(subtitle_lines),
            execution_mode="passthrough",
            authoritative_input_used=False,
            authoritative_path_kind=None,
            authoritative_flow=None,
            transcript_extraction_used=False,
            attributed_transcript_normalized=False,
            subtitle_line_bridge_applied=False,
        )

    def _uses_authoritative_media_source(self) -> bool:
        if self.media_source is None:
            return False
        return is_authoritative_media_source_kind(self.media_source.kind)

    def _build_media_understanding_provider_audit(self) -> dict[str, object]:
        if self.media_understanding_pipeline is None:
            return {
                "provider_name": "SubtitleLinePassthrough",
                "provider_mode": "passthrough",
                "extraction_provider_name": None,
                "extraction_provider_mode": None,
                "extraction_version_context": {},
                "fallback_applied": False,
                "fallback_reason": None,
                "fallback_stage": None,
                "version_context": {},
            }
        return self.media_understanding_pipeline.get_provider_audit()

    def _read_media_source_path(self, source: MediaSource | None = None) -> str | None:
        resolved_source = source if source is not None else self.media_source
        if resolved_source is None:
            return None
        return resolved_source.source_path()

    def _resolve_workflow_project_dir(self) -> Path:
        return self.draft_writer.output_root_dir / self.config.project_id


def _read_input_hash(state_manager: StateManager) -> str | None:
    input_hash = get_stage_payload_value(state_manager, "ingestion", "input_hash")
    return input_hash if isinstance(input_hash, str) else None


def _build_media_source_hash(source: MediaSource) -> str:
    payload: dict[str, object] = {
        "kind": source.kind.value,
        "locator": source.locator,
        "metadata": source.metadata,
    }

    if source.kind == MediaSourceKind.TRANSCRIPT:
        payload["transcript_lines"] = _serialize_transcript_lines(source.transcript_lines)
    elif source.kind == MediaSourceKind.ATTRIBUTED_TRANSCRIPT:
        payload["attributed_lines"] = _serialize_attributed_lines(source.attributed_lines)
    elif source.kind == MediaSourceKind.LOCAL_SRT:
        if not source.locator:
            raise WorkflowError("authoritative local SRT media source requires locator path.")
        srt_path = Path(source.locator)
        if not srt_path.exists():
            raise WorkflowError(f"authoritative local SRT media source not found: {source.locator}")
        payload["content"] = srt_path.read_text(encoding="utf-8-sig")
    elif source.kind in {MediaSourceKind.LOCAL_VIDEO, MediaSourceKind.LOCAL_AUDIO}:
        payload["source_path"] = source.source_path()
        if source.locator:
            source_path = Path(source.locator)
            if source_path.exists() and source_path.is_file():
                file_stat = source_path.stat()
                payload["file_size_bytes"] = file_stat.st_size
                payload["modified_time_ns"] = file_stat.st_mtime_ns

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _serialize_transcript_lines(lines: list[TranscriptLine]) -> list[dict[str, object]]:
    return [
        {
            "index": line.index,
            "start_ms": line.start_ms,
            "end_ms": line.end_ms,
            "source_text": line.source_text,
        }
        for line in lines
    ]


def _serialize_attributed_lines(lines: list[AttributedTranscriptLine]) -> list[dict[str, object]]:
    return [
        {
            "index": line.index,
            "start_ms": line.start_ms,
            "end_ms": line.end_ms,
            "speaker_id": line.speaker_id,
            "speaker_name": line.speaker_name,
            "source_text": line.source_text,
        }
        for line in lines
    ]


def _read_stage_payload_from_snapshot(stage_snapshot: dict[str, object], stage_name: str) -> dict[str, object]:
    stage = stage_snapshot.get(stage_name)
    if isinstance(stage, dict):
        return read_stage_payload(stage)
    return {}


def _require_non_empty_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value
    raise WorkflowError(f"Draft stage payload missing required field: {key}")


def _read_int_payload_value(payload: dict[str, object], key: str) -> int:
    value = payload.get(key, 0)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowError(f"Draft stage payload contains invalid integer field: {key}") from exc
