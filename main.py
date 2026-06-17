import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import pprint
import sys


PROJECT_ROOT = Path(__file__).resolve().parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from modules.alignment.alignment_orchestrator import AlignmentConfig, AlignmentOrchestrator
from modules.alignment.dsp_stretcher import DSPStretcher
from modules.alignment.rewrite_engine import RewriteEngine
from modules.chunking.semantic_block_builder import SemanticBlockBuilder
from modules.draft.caption_retiming import CaptionRetimer
from modules.draft.draft_writer import DraftWriter
from modules.ingestion.intake import AuthoritativeIntakeBuilder, AuthoritativeIntakeRequest
from modules.ingestion.providers import MemorySubtitleProvider
from modules.ingestion.models import SubtitleSeed
from modules.media_understanding.models import MediaSource, MediaSourceKind
from modules.media_understanding.pipeline import MediaUnderstandingPipeline
from modules.media_understanding.providers import (
    MediaUnderstandingProviderSelectionConfig,
    TranscriptExtractionProviderSelectionConfig,
)
from modules.translation.providers import (
    RealTranslationProviderConfig,
    TranslationProviderSelectionConfig,
    resolve_translation_provider,
)
from modules.translation.router import TranslationChunkRouter, TranslationRouterConfig
from modules.translation.translator import MockTranslator, TranslationPipeline
from modules.workflow.project_workflow import ProjectWorkflow, ProjectWorkflowConfig
from modules.output import OutputBundleResult, OutputDispatcher, OutputRequest
from pipeline.process import ProcessConfig, ProcessPipeline
from core.exceptions import (
    MediaUnderstandingInvalidSourcePathError,
    MediaUnderstandingTranscriptExtractionModelError,
    MediaUnderstandingTranscriptExtractionNoResultError,
    MediaUnderstandingTranscriptExtractionRuntimeError,
    MediaUnderstandingTranscriptExtractionUnavailableError,
    MediaUnderstandingUnsupportedSourceKindError,
    StateError,
    TTSConfigurationError,
    TranslationConfigurationError,
    PublishError,
    WorkflowError,
)
from core.enums import OutputTarget
from services.audio_service import AudioService
from services.cache_manager import CacheManager
from services import config_loader
from services.control_panel import CONTROL_PANEL_DEFAULT_PORT, run_control_panel_server
from services.jobs import (
    JOB_API_DEFAULT_HOST,
    JOB_API_DEFAULT_PORT,
    build_default_job_service,
    build_job_api_server,
)
from services.llm_service import MockLLMService
from services.project_state_summary import build_project_state_summary, build_stage_execution_summary
from services.source_context_summary import build_source_context_summary
from services.state_manager import StateManager
from services.voice_asset import (
    DEFAULT_VOICE_VERIFICATION_SAMPLE_TEXT,
    VoiceAssetVerificationConfigurationError,
    VoiceAssetVerificationResult,
    VoiceAssetVerificationRuntimeError,
    VoiceAssetVerifier,
)
from services.tts_provider import RealTTSProviderConfig, TTSProviderSelectionConfig, resolve_tts_provider
from services.tts_service import MockTTSConfig, MockTTSService
# web_ui standalone server removed in Phase 4 (8876 deprecation).
# Only helpers still used by Job API are retained in services.web_ui.
from services.voice_clone import (
    MiniMaxVoiceCloneClient,
    VoiceCloneAPIError,
    VoiceCloneConfig,
    VoiceCloneConfigurationError,
    VoiceCloneInputError,
    VoiceCloneResponseFormatError,
    VoiceCloneUploadError,
)
from services.voice_registry import SpeakerVoiceProfile, VoiceRegistry, VoiceResolver

DEFAULT_DEMO_OUTPUT_ROOT = PROJECT_ROOT / "demo_output" / "sprint_4b_demo"
LOCAL_AUDIO_DEMO_ROOT = PROJECT_ROOT / "demo_output" / "local_audio_real_asr_demo"
LOCAL_VIDEO_DEMO_ROOT = PROJECT_ROOT / "demo_output" / "local_video_authoritative_demo"
DEFAULT_VOICE_REGISTRY_PATH = PROJECT_ROOT / "voice_registry.json"
ALLOWED_PROVIDER_MODES = ("mock", "real")
SUPPORTED_LOCAL_AUDIO_EXTENSIONS = (".wav", ".wave")
SUPPORTED_LOCAL_VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".avi", ".webm")
VOICE_REGISTRY_PROVIDER_NAME = "minimax_tts"


@dataclass(frozen=True, slots=True)
class LocalAudioDemoArgs:
    local_audio_path: Path
    translation_mode: str = "mock"
    tts_mode: str = "mock"
    output_target: OutputTarget = OutputTarget.EDITOR


@dataclass(frozen=True, slots=True)
class LocalVideoDemoArgs:
    local_video_path: Path
    translation_mode: str = "mock"
    tts_mode: str = "mock"
    output_target: OutputTarget = OutputTarget.EDITOR


@dataclass(frozen=True, slots=True)
class VoiceRegistryCommandArgs:
    action: str
    speaker_id: str
    speaker_name: str | None = None
    voice_id: str | None = None


@dataclass(frozen=True, slots=True)
class VoiceCloneCommandArgs:
    action: str
    speaker_id: str
    speaker_name: str
    source_audio_path: Path


@dataclass(frozen=True, slots=True)
class ControlPanelCommandArgs:
    port: int = CONTROL_PANEL_DEFAULT_PORT


@dataclass(frozen=True, slots=True)
class JobAPICommandArgs:
    host: str = JOB_API_DEFAULT_HOST
    port: int = JOB_API_DEFAULT_PORT


def parse_process_args(argv: list[str]) -> ProcessConfig:
    parser = argparse.ArgumentParser(
        prog="python main.py process",
        description="视频来源 → 全自动生成配音素材包",
    )
    # Legacy positional: youtube_url (optional when --source-type/--source-ref used)
    parser.add_argument(
        "youtube_url",
        nargs="?",
        default="",
        help="YouTube 视频 URL（旧入口；新入口请用 --source-type/--source-ref）",
    )
    # New explicit source parameters (must be used as a pair)
    parser.add_argument(
        "--source-type",
        default=None,
        choices=["youtube_url", "local_video", "local_audio"],
        help="来源类型：youtube_url | local_video | local_audio（须与 --source-ref 一起使用）",
    )
    parser.add_argument(
        "--source-ref",
        default=None,
        help="来源引用：YouTube URL 或本地文件路径（须与 --source-type 一起使用）",
    )
    parser.add_argument(
        "--voice-a",
        required=False,
        default=None,
        help="Speaker A 的 MiniMax voice_id（可选，缺失时自动克隆）",
    )
    parser.add_argument(
        "--voice-b",
        default=None,
        help="Speaker B 的 MiniMax voice_id（可选，缺失时自动克隆）",
    )
    parser.add_argument("--speaker-a", default="Speaker A", help="说话人显示名称")
    parser.add_argument("--speaker-b", default="Speaker B", help="Speaker B 的显示名称")
    parser.add_argument("--speakers", default="auto", help="说话人数量：1 | 2 | auto（默认 auto）")
    parser.add_argument("--project-dir", default=None, help="指定项目目录")
    parser.add_argument(
        "--resume-from",
        default=None,
        help="断点续跑起始 Stage；当前仅支持 'alignment'，由 commit copy_as_new / overwrite 触发。",
    )
    parser.add_argument("--skip-review", action="store_true", help="跳过Gemini说话人审核步骤")
    parser.add_argument("--wait-for-review", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--transcription-method", default="assemblyai", choices=["assemblyai", "gemini"], help="转录方案：assemblyai（默认）| gemini")
    parser.add_argument("--job-id", default=None, help=argparse.SUPPRESS)
    parsed = parser.parse_args(argv[2:])

    # Validate: --source-type and --source-ref must be used as a pair
    has_st = parsed.source_type is not None
    has_sr = parsed.source_ref is not None
    if has_st != has_sr:
        missing = "--source-ref" if has_st else "--source-type"
        parser.error(f"--source-type 和 --source-ref 必须一起使用（缺少 {missing}）")

    source_type = (parsed.source_type or "").strip()
    source_ref = (parsed.source_ref or "").strip()
    youtube_url = (parsed.youtube_url or "").strip()

    return ProcessConfig(
        youtube_url=youtube_url,
        source_type=source_type,
        source_ref=source_ref,
        voice_a=parsed.voice_a,
        voice_b=parsed.voice_b,
        speaker_a_name=parsed.speaker_a,
        speaker_b_name=parsed.speaker_b,
        speakers=parsed.speakers,
        project_dir=parsed.project_dir,
        resume_from=parsed.resume_from,
        skip_review=parsed.skip_review,
        wait_for_review=parsed.wait_for_review,
        transcription_method=parsed.transcription_method,
        job_id=parsed.job_id,
    )


def _resolve_translation_binding(mode: str | None = None):
    selection = TranslationProviderSelectionConfig.from_env()
    if mode is not None:
        selection.mode = _normalize_provider_mode(mode)
    return resolve_translation_provider(selection, mock_provider=MockTranslator())


def _resolve_tts_binding(output_root: Path, mode: str | None = None):
    selection = TTSProviderSelectionConfig.from_env()
    if mode is not None:
        selection.mode = _normalize_provider_mode(mode)
    mock_tts_service = MockTTSService(
        output_dir=str(output_root / "audio"),
        config=MockTTSConfig(ms_per_char=100, min_duration_ms=800),
    )
    return resolve_tts_provider(
        selection,
        mock_provider=mock_tts_service,
        output_dir=str(output_root / "audio"),
    )


def _build_project_workflow(
    *,
    output_root: Path,
    project_id: str,
    subtitle_provider: MemorySubtitleProvider | None,
    translation_mode: str | None = None,
    tts_mode: str | None = None,
    media_understanding_pipeline: MediaUnderstandingPipeline | None = None,
    media_source: MediaSource | None = None,
) -> tuple[ProjectWorkflow, StateManager, CacheManager]:
    state_manager = StateManager(str(output_root / "project_state.json"))
    cache_manager = CacheManager(str(output_root / "project_cache.json"))
    translation_binding = _resolve_translation_binding(mode=translation_mode)
    translation_pipeline = TranslationPipeline(
        router=TranslationChunkRouter(
            TranslationRouterConfig(batch_size=2, max_batch_size=4, max_chars_per_batch=80)
        ),
        translator=translation_binding.provider,
        fallback_translator=translation_binding.fallback_provider,
    )
    tts_binding = _resolve_tts_binding(output_root, mode=tts_mode)
    workflow = ProjectWorkflow(
        subtitle_provider=subtitle_provider,
        translation_pipeline=translation_pipeline,
        block_builder=SemanticBlockBuilder(),
        alignment_orchestrator=AlignmentOrchestrator(
            tts_service=tts_binding.provider,
            audio_service=AudioService(),
            rewrite_engine=RewriteEngine(llm_service=MockLLMService()),
            dsp_stretcher=DSPStretcher(),
            config=AlignmentConfig(ideal_threshold=0.05, dsp_threshold=0.20, max_retries=2),
        ),
        caption_retimer=CaptionRetimer(),
        draft_writer=DraftWriter(output_root_dir=str(output_root)),
        state_manager=state_manager,
        cache_manager=cache_manager,
        config=ProjectWorkflowConfig(
            project_id=project_id,
            translation_provider_name=translation_binding.provider_name,
            translation_target_language=translation_binding.target_language,
            translation_model_name=translation_binding.model_name,
            translation_provider_mode=translation_binding.mode,
            translation_version_context=translation_binding.version_context,
            translation_fallback_applied=translation_binding.fallback_applied,
            translation_fallback_reason=translation_binding.fallback_reason,
            translation_fallback_stage=translation_binding.fallback_stage,
            translation_runtime_fallback_enabled=translation_binding.runtime_fallback_enabled,
            translation_fallback_from=translation_binding.fallback_from,
            translation_fallback_to=translation_binding.fallback_to,
            tts_provider_name=tts_binding.provider_name,
            tts_voice_name=tts_binding.voice_name,
            tts_model_name=tts_binding.model_name,
            tts_provider_mode=tts_binding.mode,
            tts_version_context=tts_binding.version_context,
            tts_fallback_applied=tts_binding.fallback_applied,
            tts_fallback_reason=tts_binding.fallback_reason,
            tts_fallback_stage=tts_binding.fallback_stage,
        ),
        media_understanding_pipeline=media_understanding_pipeline,
        media_source=media_source,
    )
    return workflow, state_manager, cache_manager


def build_demo_workflow(output_root: Path) -> tuple[ProjectWorkflow, StateManager, CacheManager]:
    subtitle_provider = MemorySubtitleProvider(
        [
            SubtitleSeed(
                index=1,
                start_ms=0,
                end_ms=900,
                en_text="Welcome back to the show.",
                speaker_id="speaker_host",
                speaker_name="Host",
            ),
            SubtitleSeed(
                index=2,
                start_ms=1_000,
                end_ms=1_900,
                en_text="Today we assemble a Jianying draft scaffold.",
                speaker_id="speaker_host",
                speaker_name="Host",
            ),
        ]
    )
    return _build_project_workflow(
        output_root=output_root,
        project_id="demo_sprint_4b",
        subtitle_provider=subtitle_provider,
    )


def build_local_audio_demo_workflow(
    output_root: Path,
    local_audio_path: Path,
    *,
    translation_mode: str = "mock",
    tts_mode: str = "mock",
) -> tuple[ProjectWorkflow, StateManager, CacheManager]:
    media_source = AuthoritativeIntakeBuilder().build(
        AuthoritativeIntakeRequest(
            kind=MediaSourceKind.LOCAL_AUDIO,
            locator=str(local_audio_path),
        )
    )
    transcript_extraction_selection = TranscriptExtractionProviderSelectionConfig.from_env()
    media_understanding_pipeline = MediaUnderstandingPipeline.from_selection(
        MediaUnderstandingProviderSelectionConfig.for_transcript_extraction(
            transcript_extraction_selection
        )
    )
    return _build_project_workflow(
        output_root=output_root,
        project_id="local_audio_real_asr_demo",
        subtitle_provider=None,
        translation_mode=translation_mode,
        tts_mode=tts_mode,
        media_understanding_pipeline=media_understanding_pipeline,
        media_source=media_source,
    )


def build_local_video_demo_workflow(
    output_root: Path,
    local_video_path: Path,
    *,
    translation_mode: str = "mock",
    tts_mode: str = "mock",
) -> tuple[ProjectWorkflow, StateManager, CacheManager]:
    media_source = AuthoritativeIntakeBuilder().build(
        AuthoritativeIntakeRequest(
            kind=MediaSourceKind.LOCAL_VIDEO,
            locator=str(local_video_path),
        )
    )
    transcript_extraction_selection = TranscriptExtractionProviderSelectionConfig.from_env()
    media_understanding_pipeline = MediaUnderstandingPipeline.from_selection(
        MediaUnderstandingProviderSelectionConfig.for_transcript_extraction(
            transcript_extraction_selection
        )
    )
    return _build_project_workflow(
        output_root=output_root,
        project_id="local_video_authoritative_demo",
        subtitle_provider=None,
        translation_mode=translation_mode,
        tts_mode=tts_mode,
        media_understanding_pipeline=media_understanding_pipeline,
        media_source=media_source,
    )


def _run_workflow_build_and_dispatch(
    workflow: ProjectWorkflow,
    *,
    output_target: OutputTarget = OutputTarget.EDITOR,
) -> tuple[object, OutputBundleResult, OutputRequest]:
    build_result = workflow.run_build()
    output_request = OutputRequest(targets=[output_target])
    output_bundle = OutputDispatcher().dispatch(
        build_result.localized_project,
        build_result.artifact_index,
        output_request,
    )
    return workflow.build_legacy_result(build_result), output_bundle, output_request


def build_run_summary(
    result,
    state_manager: StateManager,
    cache_manager: CacheManager,
    *,
    run_context: dict[str, object] | None = None,
    output_bundle: OutputBundleResult | None = None,
    output_request: OutputRequest | None = None,
) -> dict[str, object]:
    draft_content = json.loads(Path(result.draft_content_path).read_text(encoding="utf-8"))
    draft_meta = json.loads(Path(result.draft_meta_info_path).read_text(encoding="utf-8"))
    export_json = json.loads(Path(result.export_path).read_text(encoding="utf-8")) if result.export_path else {}
    project_state_snapshot = state_manager.load()
    stage_snapshot = result.stage_snapshot or project_state_snapshot.get("stages", {})
    if not project_state_snapshot.get("stages") and isinstance(stage_snapshot, dict) and stage_snapshot:
        project_state_snapshot = {
            **project_state_snapshot,
            "stages": dict(stage_snapshot),
        }
    project_state_summary = build_project_state_summary(project_state_snapshot)
    stage_execution_summary = build_stage_execution_summary(stage_snapshot)
    cache_snapshot = cache_manager.load()
    media_payload = stage_snapshot.get("media_understanding", {}).get("payload", {})
    translation_payload = stage_snapshot.get("translation", {}).get("payload", {})
    alignment_payload = stage_snapshot.get("alignment", {}).get("payload", {})
    draft_payload = stage_snapshot.get("draft", {}).get("payload", {})
    summarized_tts_voice_id = _summarize_tts_voice_id(alignment_payload)
    tts_voice_resolution_source = _read_tts_voice_resolution_source(alignment_payload)
    tts_primary_speaker_id = _read_primary_tts_speaker_id(alignment_payload)
    output_summary = _build_output_summary(
        result=result,
        output_bundle=output_bundle,
        output_request=output_request,
    )
    source_context = build_source_context_summary(
        manifest_path=output_summary.get("manifest_path"),
        run_context=run_context,
        stage_snapshot=stage_snapshot,
    )

    return {
        "success": True,
        "draft_dir": result.draft_dir,
        "draft_content_path": result.draft_content_path,
        "draft_meta_info_path": result.draft_meta_info_path,
        "export_path": result.export_path,
        "run_context": dict(run_context or {}),
        "draft_summary": draft_meta["summary"],
        "timeline_summary": {
            "duration_ms": draft_content["timeline"]["duration_ms"],
            "audio_items": len(draft_content["timeline"]["audio_tracks"][0]["items"]),
            "caption_items": len(draft_content["timeline"]["caption_tracks"][0]["items"]),
        },
        "export_summary": {
            "export_target": export_json.get("export_target"),
            "audio_tracks": len(export_json.get("timeline", {}).get("audio_tracks", [])),
            "caption_tracks": len(export_json.get("timeline", {}).get("caption_tracks", [])),
            "audio_materials": len(export_json.get("materials", {}).get("audio_materials", [])),
        },
        "provider_mode_summary": {
            "source_kind": media_payload.get("source_kind"),
            "media_understanding_provider": media_payload.get("provider_name"),
            "media_understanding_mode": media_payload.get("provider_mode"),
            "media_extraction_provider": media_payload.get("extraction_provider_name"),
            "media_execution_mode": media_payload.get("execution_mode"),
            "media_authoritative_input_used": media_payload.get("authoritative_input_used"),
            "media_authoritative_path_kind": media_payload.get("authoritative_path_kind"),
            "media_authoritative_flow": media_payload.get("authoritative_flow"),
            "media_transcript_extraction_used": media_payload.get("transcript_extraction_used"),
            "translation_mode": translation_payload.get("provider_mode"),
            "literal_text_layer_produced": translation_payload.get("literal_text_layer_produced"),
            "tts_mode": alignment_payload.get("provider_mode"),
            "tts_provider": alignment_payload.get("provider_name"),
            "tts_text_layer_produced": alignment_payload.get("tts_text_layer_produced"),
            "tts_primary_speaker_id": tts_primary_speaker_id,
            "tts_voice_id": summarized_tts_voice_id,
            "tts_voice_resolution_source": tts_voice_resolution_source,
            "translation_execution_mode": translation_payload.get("execution_mode"),
            "alignment_execution_mode": alignment_payload.get("execution_mode"),
            "draft_execution_mode": draft_payload.get("execution_mode"),
        },
        "source_context": source_context,
        "output_summary": output_summary,
        "result_summary": {
            "status": "success",
            "source_kind": media_payload.get("source_kind"),
            "input_path": (run_context or {}).get("input_path"),
            "output_root": (run_context or {}).get("output_root"),
            "draft_path": result.draft_dir,
            "requested_output_targets": [target.value for target in output_request.expanded_targets()] if output_request else [],
            "media_provider": media_payload.get("provider_name"),
            "extraction_provider": media_payload.get("extraction_provider_name"),
            "authoritative_input_used": media_payload.get("authoritative_input_used"),
            "authoritative_path_kind": media_payload.get("authoritative_path_kind"),
            "authoritative_flow": media_payload.get("authoritative_flow"),
            "translation_mode": translation_payload.get("provider_mode"),
            "tts_mode": alignment_payload.get("provider_mode"),
            "tts_provider": alignment_payload.get("provider_name"),
            "tts_primary_speaker_id": tts_primary_speaker_id,
            "tts_voice_id": summarized_tts_voice_id,
            "tts_voice_resolution_source": tts_voice_resolution_source,
            "manifest_path": output_summary["manifest_path"],
            "editor_dubbed_audio_path": output_summary["editor_dubbed_audio_path"],
            "editor_subtitles_path": output_summary["editor_subtitles_path"],
            "publish_dubbed_video_path": output_summary["publish_dubbed_video_path"],
            "source_context": source_context,
        },
        "stage_execution_summary": stage_execution_summary,
        "project_state_summary": project_state_summary,
        "project_state_snapshot": project_state_snapshot,
        "cache_snapshot": {
            "metrics": cache_snapshot.get("metrics"),
            "last_lookup": cache_snapshot.get("last_lookup"),
        },
    }

def _build_output_summary(
    *,
    result,
    output_bundle: OutputBundleResult | None,
    output_request: OutputRequest | None,
) -> dict[str, object]:
    editor_result = output_bundle.editor_result if output_bundle is not None else None
    publish_result = output_bundle.publish_result if output_bundle is not None else None
    return {
        "targets": [target.value for target in output_request.expanded_targets()] if output_request else [],
        "manifest_path": output_bundle.manifest_path if output_bundle is not None else None,
        "editor_draft_dir": result.draft_dir,
        "editor_export_path": result.export_path,
        "editor_dubbed_audio_path": editor_result.dubbed_audio_path if editor_result is not None else None,
        "editor_subtitles_path": editor_result.subtitles_path if editor_result is not None else None,
        "editor_segments_dir": editor_result.segments_dir if editor_result is not None else None,
        "editor_alignment_report_path": (
            editor_result.alignment_report_path if editor_result is not None else None
        ),
        "editor_needs_review_count": editor_result.needs_review_count if editor_result is not None else None,
        "publish_dubbed_video_path": publish_result.dubbed_video_path if publish_result is not None else None,
        "publish_original_video_path": publish_result.original_video_path if publish_result is not None else None,
        "publish_dubbed_audio_path": publish_result.dubbed_audio_path if publish_result is not None else None,
    }


def _print_run_summary(summary: dict[str, object]) -> None:
    if summary.get("run_context"):
        print("Run context:")
        pprint.pp(summary["run_context"], sort_dicts=False)
    print(f"Draft scaffold written to: {summary['draft_dir']}")
    print("Key files:")
    print(f" - {summary['draft_content_path']}")
    print(f" - {summary['draft_meta_info_path']}")
    if summary.get("export_path"):
        print(f" - {summary['export_path']}")
    print("Draft summary:")
    pprint.pp(summary["draft_summary"], sort_dicts=False)
    print("Timeline summary:")
    pprint.pp(summary["timeline_summary"], sort_dicts=False)
    print("Export summary:")
    pprint.pp(summary["export_summary"], sort_dicts=False)
    print("Provider mode summary:")
    pprint.pp(summary["provider_mode_summary"], sort_dicts=False)
    print("Source context:")
    pprint.pp(summary.get("source_context"), sort_dicts=False)
    print("Output summary:")
    pprint.pp(summary["output_summary"], sort_dicts=False)
    output_summary = summary.get("output_summary", {})
    if isinstance(output_summary, dict):
        print("Output artifacts:")
        artifact_lines = [
            ("Manifest", output_summary.get("manifest_path")),
            ("Editor draft", output_summary.get("editor_draft_dir")),
            ("Editor export", output_summary.get("editor_export_path")),
            ("Editor dubbed audio", output_summary.get("editor_dubbed_audio_path")),
            ("Editor subtitles", output_summary.get("editor_subtitles_path")),
            ("Editor segments", output_summary.get("editor_segments_dir")),
            ("Alignment report", output_summary.get("editor_alignment_report_path")),
            ("Publish dubbed video", output_summary.get("publish_dubbed_video_path")),
        ]
        for label, value in artifact_lines:
            if isinstance(value, str) and value.strip():
                print(f" - {label}: {value}")
    print("Run result summary:")
    pprint.pp(summary["result_summary"], sort_dicts=False)
    print("Stage execution summary:")
    pprint.pp(summary["stage_execution_summary"], sort_dicts=False)
    print("Project state summary:")
    pprint.pp(summary.get("project_state_summary"), sort_dicts=False)
    print("Project state snapshot:")
    pprint.pp(summary["project_state_snapshot"], sort_dicts=False)
    print("Cache snapshot:")
    pprint.pp(summary["cache_snapshot"], sort_dicts=False)


def run_demo_pipeline() -> None:
    output_root = DEFAULT_DEMO_OUTPUT_ROOT
    workflow, state_manager, cache_manager = build_demo_workflow(output_root)
    result, output_bundle, output_request = _run_workflow_build_and_dispatch(workflow)
    summary = build_run_summary(
        result,
        state_manager,
        cache_manager,
        run_context={
            "demo_kind": "default_mock_demo",
            "input_path": None,
            "output_root": str(output_root),
            "translation_mode": "mock_or_env_selection",
            "tts_mode": "mock_or_env_selection",
            "output_target": OutputTarget.EDITOR.value,
        },
        output_bundle=output_bundle,
        output_request=output_request,
    )
    _print_run_summary(summary)


def run_local_audio_demo_pipeline(
    local_audio_path: Path,
    *,
    translation_mode: str = "mock",
    tts_mode: str = "mock",
    output_target: OutputTarget = OutputTarget.EDITOR,
) -> None:
    normalized_translation_mode = _normalize_provider_mode(translation_mode)
    normalized_tts_mode = _normalize_provider_mode(tts_mode)
    validated_audio_path = _validate_local_audio_demo_input_path(local_audio_path)
    if output_target in {OutputTarget.PUBLISH, OutputTarget.BOTH}:
        raise SystemExit(
            "local-audio-demo failed: publish output requires a source video. "
            "Use --output editor for local audio inputs."
        )
    output_root = build_local_audio_demo_output_root(
        validated_audio_path,
        translation_mode=normalized_translation_mode,
        tts_mode=normalized_tts_mode,
    )

    try:
        workflow, state_manager, cache_manager = build_local_audio_demo_workflow(
            output_root=output_root,
            local_audio_path=validated_audio_path,
            translation_mode=normalized_translation_mode,
            tts_mode=normalized_tts_mode,
        )
        result, output_bundle, output_request = _run_workflow_build_and_dispatch(
            workflow,
            output_target=output_target,
        )
    except Exception as exc:
        raise SystemExit(_format_local_audio_demo_error(exc, validated_audio_path)) from exc

    summary = build_run_summary(
        result,
        state_manager,
        cache_manager,
        run_context={
            "demo_kind": "real_local_audio_asr_demo",
            "input_path": str(validated_audio_path),
            "output_root": str(output_root),
            "translation_mode": normalized_translation_mode,
            "tts_mode": normalized_tts_mode,
            "output_target": output_target.value,
        },
        output_bundle=output_bundle,
        output_request=output_request,
    )
    _print_run_summary(summary)


def run_local_video_demo_pipeline(
    local_video_path: Path,
    *,
    translation_mode: str = "mock",
    tts_mode: str = "mock",
    output_target: OutputTarget = OutputTarget.EDITOR,
) -> None:
    normalized_translation_mode = _normalize_provider_mode(translation_mode)
    normalized_tts_mode = _normalize_provider_mode(tts_mode)
    validated_video_path = _validate_local_video_demo_input_path(local_video_path)
    output_root = build_local_video_demo_output_root(
        validated_video_path,
        translation_mode=normalized_translation_mode,
        tts_mode=normalized_tts_mode,
    )

    try:
        workflow, state_manager, cache_manager = build_local_video_demo_workflow(
            output_root=output_root,
            local_video_path=validated_video_path,
            translation_mode=normalized_translation_mode,
            tts_mode=normalized_tts_mode,
        )
        result, output_bundle, output_request = _run_workflow_build_and_dispatch(
            workflow,
            output_target=output_target,
        )
    except Exception as exc:
        raise SystemExit(_format_local_video_demo_error(exc, validated_video_path)) from exc

    summary = build_run_summary(
        result,
        state_manager,
        cache_manager,
        run_context={
            "demo_kind": "local_video_authoritative_demo",
            "input_path": str(validated_video_path),
            "output_root": str(output_root),
            "translation_mode": normalized_translation_mode,
            "tts_mode": normalized_tts_mode,
            "output_target": output_target.value,
        },
        output_bundle=output_bundle,
        output_request=output_request,
    )
    _print_run_summary(summary)


def run_voice_registry_command(argv: list[str]) -> None:
    parsed_args = parse_voice_registry_args(argv)
    registry_path = _resolve_voice_registry_path()
    registry = VoiceRegistry(str(registry_path))
    resolver = VoiceResolver(registry)
    try:
        if parsed_args.action == "show":
            profile = registry.get_speaker_profile(parsed_args.speaker_id)
            if profile is None:
                raise SystemExit(
                    f"voice-registry failed: speaker not found: {parsed_args.speaker_id}. "
                    f"Registry path: {registry_path}"
                )
            resolution = resolver.resolve(
                parsed_args.speaker_id,
                tts_provider=_resolve_voice_registry_tts_provider(),
                platform=_resolve_voice_registry_platform(),
            )
            print(f"Voice registry path: {registry_path}")
            print("Speaker profile:")
            pprint.pp(_build_voice_registry_show_payload(profile, resolution), sort_dicts=False)
            return

        if parsed_args.action in {"register-builtin", "register-cloned"}:
            assert parsed_args.speaker_name is not None
            assert parsed_args.voice_id is not None
            voice_type = "builtin" if parsed_args.action == "register-builtin" else "cloned"
            label_suffix = "Builtin" if voice_type == "builtin" else "Cloned"
            profile = registry.register_voice(
                parsed_args.speaker_id,
                speaker_name=parsed_args.speaker_name,
                voice_id=parsed_args.voice_id,
                voice_type=voice_type,
                provider=_resolve_voice_registry_provider_name(),
                tts_provider=_resolve_voice_registry_tts_provider(),
                platform=_resolve_voice_registry_platform(),
                label=f"{parsed_args.speaker_name} {label_suffix}",
            )
            print(
                f"Voice registry updated: registered {voice_type} voice "
                f"{parsed_args.voice_id} for speaker_id={parsed_args.speaker_id}. "
                f"Registry path: {registry_path}"
            )
            pprint.pp(_serialize_speaker_voice_profile(profile), sort_dicts=False)
            return

        if parsed_args.action == "set-default":
            assert parsed_args.voice_id is not None
            profile = registry.set_default_voice(parsed_args.speaker_id, parsed_args.voice_id)
            print(
                f"Voice registry updated: set default voice {parsed_args.voice_id} "
                f"for speaker_id={parsed_args.speaker_id}. Registry path: {registry_path}"
            )
            pprint.pp(_serialize_speaker_voice_profile(profile), sort_dicts=False)
            return
    except (StateError, ValueError) as exc:
        raise SystemExit(f"voice-registry failed: {exc}. Registry path: {registry_path}") from exc

    raise SystemExit(_build_voice_registry_usage(f"Unsupported voice-registry action: {parsed_args.action}"))


def run_voice_clone_command(argv: list[str]) -> None:
    parsed_args = parse_voice_clone_args(argv)
    registry_path = _resolve_voice_registry_path()
    registry = VoiceRegistry(str(registry_path))
    clone_config = VoiceCloneConfig.from_env()
    clone_config_summary = clone_config.build_diagnostic_summary()
    verification_result: VoiceAssetVerificationResult | None = None

    if parsed_args.action != "create":
        raise SystemExit(_build_voice_clone_usage(f"Unsupported voice-clone action: {parsed_args.action}"))

    try:
        clone_client = MiniMaxVoiceCloneClient(clone_config)
        clone_result = clone_client.create_voice_clone(
            speaker_id=parsed_args.speaker_id,
            speaker_name=parsed_args.speaker_name,
            source_audio_path=parsed_args.source_audio_path,
        )
        profile = registry.register_voice(
            clone_result.speaker_id,
            speaker_name=clone_result.speaker_name,
            voice_id=clone_result.voice_id,
            voice_type="cloned",
            provider=clone_result.provider_name,
            tts_provider=_resolve_voice_registry_tts_provider(),
            platform=_resolve_voice_registry_platform(),
            label=f"{clone_result.speaker_name} Cloned",
            source_audio_path=clone_result.source_audio_path,
            notes=f"uploaded_file_id={clone_result.uploaded_file_id}",
            set_default=True,
        )
    except VoiceCloneInputError as exc:
        raise SystemExit(
            "voice-clone failed: input file problem. "
            f"Details: {exc}"
        ) from exc
    except VoiceCloneConfigurationError as exc:
        raise SystemExit(
            "voice-clone failed: clone configuration problem. "
            f"Details: {exc}. "
            f"Config summary: {clone_config_summary}"
        ) from exc
    except VoiceCloneUploadError as exc:
        raise SystemExit(
            "voice-clone failed: upload API failure. "
            f"Details: {exc}"
        ) from exc
    except VoiceCloneAPIError as exc:
        raise SystemExit(
            "voice-clone failed: clone API failure. "
            f"Details: {exc}"
        ) from exc
    except VoiceCloneResponseFormatError as exc:
        raise SystemExit(
            "voice-clone failed: provider response format problem. "
            f"Details: {exc}"
        ) from exc
    except (StateError, ValueError) as exc:
        raise SystemExit(
            "voice-clone failed: registry write failure. "
            f"Details: {exc}. Registry path: {registry_path}"
        ) from exc

    try:
        verification_result = _verify_cloned_voice_asset(
            clone_result=clone_result,
            config_path=Path(clone_config.config_path) if clone_config.config_path else None,
        )
    except (VoiceAssetVerificationConfigurationError, VoiceAssetVerificationRuntimeError) as exc:
        try:
            profile = registry.record_voice_verification(
                clone_result.speaker_id,
                clone_result.voice_id,
                success=False,
                error_message=str(exc),
            )
        except Exception:
            profile = registry.get_speaker_profile(clone_result.speaker_id) or SpeakerVoiceProfile(
                speaker_id=clone_result.speaker_id
            )
        print("Voice clone succeeded, but verification failed:")
        pprint.pp(
            {
                "speaker_id": clone_result.speaker_id,
                "speaker_name": clone_result.speaker_name,
                "voice_id": clone_result.voice_id,
                "set_as_default": True,
                "registry_path": str(registry_path),
                "provider_name": clone_result.provider_name,
                "model_name": clone_result.model_name,
                "source_audio_path": clone_result.source_audio_path,
                "uploaded_file_id": clone_result.uploaded_file_id,
                "verification_status": "failed",
                "verification_sample_text": DEFAULT_VOICE_VERIFICATION_SAMPLE_TEXT,
            },
            sort_dicts=False,
        )
        print("Updated speaker profile:")
        pprint.pp(_serialize_speaker_voice_profile(profile), sort_dicts=False)
        raise SystemExit(
            "voice-clone failed: clone completed but verification failed. "
            f"Details: {exc}. Registry path: {registry_path}"
        ) from exc
    try:
        profile = registry.record_voice_verification(
            clone_result.speaker_id,
            clone_result.voice_id,
            success=True,
            verified_at=verification_result.verified_at,
            audio_path=verification_result.output_path,
        )
    except (StateError, ValueError) as exc:
        raise SystemExit(
            "voice-clone failed: registry write failure after verification. "
            f"Details: {exc}. Registry path: {registry_path}"
        ) from exc

    print("Voice clone succeeded:")
    pprint.pp(
        {
            "speaker_id": clone_result.speaker_id,
            "speaker_name": clone_result.speaker_name,
            "voice_id": clone_result.voice_id,
            "set_as_default": True,
            "registry_path": str(registry_path),
            "provider_name": clone_result.provider_name,
            "model_name": clone_result.model_name,
            "source_audio_path": clone_result.source_audio_path,
            "uploaded_file_id": clone_result.uploaded_file_id,
            "verification_status": "verified",
            "verification_sample_text": verification_result.sample_text if verification_result else None,
            "verification_audio_path": verification_result.output_path if verification_result else None,
            "verification_verified_at": verification_result.verified_at if verification_result else None,
        },
        sort_dicts=False,
    )
    print("Updated speaker profile:")
    pprint.pp(_serialize_speaker_voice_profile(profile), sort_dicts=False)


def run_control_panel_command(argv: list[str]) -> None:
    parsed_args = parse_control_panel_args(argv)
    try:
        run_control_panel_server(port=parsed_args.port)
    except OSError as exc:
        raise SystemExit(
            "control-panel failed: could not start local server. "
            f"Details: {exc}"
        ) from exc


def run_job_api_command(argv: list[str]) -> None:
    parsed_args = parse_job_api_args(argv)
    # Attach persistent rotating file log early so all job-api log lines
    # land in runtime_logs/jobapi.app.log (survives container recreate).
    # Fail-safe: missing/unwritable directory is printed and ignored.
    try:
        from utils.rotating_log import attach_rotating_file_log
        attach_rotating_file_log("jobapi.app.log")
    except Exception as _exc:  # noqa: BLE001
        print(f"[job-api] WARNING: rotating log attach failed: {_exc}", flush=True)
    try:
        service = build_default_job_service(project_root=PROJECT_ROOT)

        # Post-build wiring — idle-cancel callback, segment TTS caller,
        # cleanup background thread. The SAME helper is called from
        # ``scripts/run_remote_workbench_service.py`` so the container
        # entry path stays in lock-step. See services.jobs.runtime_wiring.
        from services.jobs.runtime_wiring import apply_runtime_wiring

        apply_runtime_wiring(service)

        server = build_job_api_server(
            service=service,
            host=parsed_args.host,
            port=parsed_args.port,
        )
        server_url = f"http://{parsed_args.host}:{parsed_args.port}"
        print(f"AIVideoTrans Job API started at {server_url}")
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Job API...")
    except OSError as exc:
        error_details = str(exc)
        if getattr(exc, "winerror", None) == 10048 or "Address already in use" in error_details:
            raise SystemExit(
                "job-api failed: port "
                f"{parsed_args.port} is already in use. "
                "Close the old process or use a different port, for example: "
                f"python main.py job-api {parsed_args.port + 1}"
            ) from exc
        raise SystemExit(
            "job-api failed: could not start local server. "
            f"Details: {exc}"
        ) from exc
    finally:
        server = locals().get("server")
        if server is not None:
            server.server_close()


def run_process_command(argv: list[str]) -> None:
    """Keep the legacy process command stable while workflow dispatch converges."""

    config = parse_process_args(argv)

    try:
        result = ProcessPipeline().run(config)
    except Exception as exc:
        raise SystemExit(f"process failed: {exc}") from exc

    if result.status == "waiting_for_review":
        return

    project_dir = Path(result.project_dir).resolve(strict=False)

    def _display_path(path_value: str) -> str:
        resolved_path = Path(path_value).resolve(strict=False)
        for base_path in (project_dir, PROJECT_ROOT):
            try:
                return str(resolved_path.relative_to(base_path))
            except ValueError:
                continue
        return str(resolved_path)

    def _display_project_dir(path_value: str) -> str:
        resolved_path = Path(path_value).resolve(strict=False)
        try:
            return str(resolved_path.relative_to(PROJECT_ROOT))
        except ValueError:
            return str(resolved_path)

    print("═══════════════════════════════════════════════")
    print("  AIVideoTrans 处理完成")
    print("═══════════════════════════════════════════════")
    print(f"  项目目录：{_display_project_dir(str(project_dir))}")
    print(f"  总段数：{result.total_segments}")
    print(f"  需要手工检查：{result.needs_review_count} 段")
    print("")
    print("  输出文件：")
    print(f"  → 完整配音：{_display_path(result.dubbed_audio_path)}")
    print(f"  → 分段音频：{_display_path(result.segments_dir)}")
    print(f"  → 中文字幕：{_display_path(result.subtitles_path)}")
    print(f"  → 对齐报告：{_display_path(result.alignment_report_path)}")
    print(f"  → 背景声报告：{_display_path(result.background_sounds_path)}")
    print("")
    print("  下一步：")
    print("  1. 在剪映中导入原视频（video/original.mp4）")
    print("  2. 静音原视频音轨")
    print("  3. 导入 output/dubbed_audio_complete.wav")
    print("  4. 导入 output/subtitles.srt")
    print("  5. 精修发布！")
    print("═══════════════════════════════════════════════")


def build_local_audio_demo_output_root(
    local_audio_path: Path,
    *,
    translation_mode: str = "mock",
    tts_mode: str = "mock",
) -> Path:
    normalized_translation_mode = _normalize_provider_mode(translation_mode)
    normalized_tts_mode = _normalize_provider_mode(tts_mode)
    source_slug = _slugify_path_component(local_audio_path.stem)
    return (
        LOCAL_AUDIO_DEMO_ROOT
        / source_slug
        / f"translation_{normalized_translation_mode}__tts_{normalized_tts_mode}"
    )


def build_local_video_demo_output_root(
    local_video_path: Path,
    *,
    translation_mode: str = "mock",
    tts_mode: str = "mock",
) -> Path:
    normalized_translation_mode = _normalize_provider_mode(translation_mode)
    normalized_tts_mode = _normalize_provider_mode(tts_mode)
    source_slug = _slugify_path_component(local_video_path.stem)
    return (
        LOCAL_VIDEO_DEMO_ROOT
        / source_slug
        / f"translation_{normalized_translation_mode}__tts_{normalized_tts_mode}"
    )


def _normalize_provider_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized not in ALLOWED_PROVIDER_MODES:
        allowed_modes = ", ".join(ALLOWED_PROVIDER_MODES)
        raise ValueError(f"Unsupported provider mode: {mode}. Allowed values: {allowed_modes}.")
    return normalized


def _normalize_output_target(value: str) -> OutputTarget:
    normalized = value.strip().lower()
    try:
        return OutputTarget(normalized)
    except ValueError as exc:
        raise ValueError(
            f"Unsupported output target: {value}. Allowed values: editor, publish, both."
        ) from exc


def _parse_demo_output_args(
    tokens: list[str],
    *,
    usage_builder,
) -> tuple[list[str], OutputTarget]:
    positional_tokens: list[str] = []
    output_target = OutputTarget.EDITOR
    index = 0

    while index < len(tokens):
        token = tokens[index]
        if token == "--output":
            if index + 1 >= len(tokens):
                raise SystemExit(usage_builder("Missing value for --output."))
            try:
                output_target = _normalize_output_target(tokens[index + 1])
            except ValueError as exc:
                raise SystemExit(usage_builder(str(exc))) from exc
            index += 2
            continue
        if token.startswith("--"):
            raise SystemExit(usage_builder(f"Unsupported option: {token}"))
        positional_tokens.append(token)
        index += 1

    return positional_tokens, output_target


def parse_local_audio_demo_args(argv: list[str]) -> LocalAudioDemoArgs:
    if len(argv) < 3:
        raise SystemExit(
            _build_local_audio_demo_usage(
                "Missing required argument: <local_audio_path>."
            )
        )

    local_audio_path = Path(argv[2])
    remaining_args, output_target = _parse_demo_output_args(
        argv[3:],
        usage_builder=_build_local_audio_demo_usage,
    )
    try:
        translation_mode = _normalize_provider_mode(remaining_args[0]) if len(remaining_args) >= 1 else "mock"
        tts_mode = _normalize_provider_mode(remaining_args[1]) if len(remaining_args) >= 2 else "mock"
    except ValueError as exc:
        raise SystemExit(_build_local_audio_demo_usage(str(exc))) from exc

    if len(remaining_args) > 2:
        raise SystemExit(
            _build_local_audio_demo_usage("Too many arguments for local-audio-demo.")
        )

    return LocalAudioDemoArgs(
        local_audio_path=local_audio_path,
        translation_mode=translation_mode,
        tts_mode=tts_mode,
        output_target=output_target,
    )


def parse_local_video_demo_args(argv: list[str]) -> LocalVideoDemoArgs:
    if len(argv) < 3:
        raise SystemExit(
            _build_local_video_demo_usage(
                "Missing required argument: <local_video_path>."
            )
        )

    local_video_path = Path(argv[2])
    remaining_args, output_target = _parse_demo_output_args(
        argv[3:],
        usage_builder=_build_local_video_demo_usage,
    )
    try:
        translation_mode = _normalize_provider_mode(remaining_args[0]) if len(remaining_args) >= 1 else "mock"
        tts_mode = _normalize_provider_mode(remaining_args[1]) if len(remaining_args) >= 2 else "mock"
    except ValueError as exc:
        raise SystemExit(_build_local_video_demo_usage(str(exc))) from exc

    if len(remaining_args) > 2:
        raise SystemExit(
            _build_local_video_demo_usage("Too many arguments for local-video-demo.")
        )

    return LocalVideoDemoArgs(
        local_video_path=local_video_path,
        translation_mode=translation_mode,
        tts_mode=tts_mode,
        output_target=output_target,
    )


def parse_voice_registry_args(argv: list[str]) -> VoiceRegistryCommandArgs:
    if len(argv) < 3:
        raise SystemExit(_build_voice_registry_usage("Missing required voice-registry action."))

    action = argv[2].strip().lower()
    if action == "show":
        if len(argv) != 4:
            raise SystemExit(_build_voice_registry_usage("show requires: <speaker_id>"))
        return VoiceRegistryCommandArgs(action=action, speaker_id=argv[3])

    if action in {"register-builtin", "register-cloned"}:
        if len(argv) != 6:
            raise SystemExit(
                _build_voice_registry_usage(
                    f"{action} requires: <speaker_id> <speaker_name> <voice_id>"
                )
            )
        return VoiceRegistryCommandArgs(
            action=action,
            speaker_id=argv[3],
            speaker_name=argv[4],
            voice_id=argv[5],
        )

    if action == "set-default":
        if len(argv) != 5:
            raise SystemExit(_build_voice_registry_usage("set-default requires: <speaker_id> <voice_id>"))
        return VoiceRegistryCommandArgs(
            action=action,
            speaker_id=argv[3],
            voice_id=argv[4],
        )

    raise SystemExit(_build_voice_registry_usage(f"Unsupported voice-registry action: {action}"))


def parse_voice_clone_args(argv: list[str]) -> VoiceCloneCommandArgs:
    if len(argv) < 3:
        raise SystemExit(_build_voice_clone_usage("Missing required voice-clone action."))

    action = argv[2].strip().lower()
    if action == "create":
        if len(argv) != 6:
            raise SystemExit(
                _build_voice_clone_usage(
                    "create requires: <speaker_id> <speaker_name> <source_audio_path>"
                )
            )
        return VoiceCloneCommandArgs(
            action=action,
            speaker_id=argv[3],
            speaker_name=argv[4],
            source_audio_path=Path(argv[5]),
        )
    raise SystemExit(_build_voice_clone_usage(f"Unsupported voice-clone action: {action}"))


def parse_control_panel_args(argv: list[str]) -> ControlPanelCommandArgs:
    if len(argv) == 2:
        return ControlPanelCommandArgs()
    if len(argv) != 3:
        raise SystemExit(
            _build_control_panel_usage("control-panel accepts at most one optional <port> argument.")
        )

    raw_port = argv[2].strip()
    if not raw_port.isdigit():
        raise SystemExit(_build_control_panel_usage(f"Port must be a positive integer. Got: {argv[2]}"))
    resolved_port = int(raw_port)
    if resolved_port <= 0 or resolved_port > 65535:
        raise SystemExit(_build_control_panel_usage(f"Port must be between 1 and 65535. Got: {resolved_port}"))
    return ControlPanelCommandArgs(port=resolved_port)


def parse_job_api_args(argv: list[str]) -> JobAPICommandArgs:
    if len(argv) == 2:
        return JobAPICommandArgs()
    if len(argv) > 3:
        raise SystemExit(
            _build_job_api_usage("job-api accepts at most one optional <port> argument.")
        )

    try:
        resolved_port = int(argv[2])
    except ValueError as exc:
        raise SystemExit(_build_job_api_usage("job-api <port> must be an integer.")) from exc

    if resolved_port <= 0:
        raise SystemExit(_build_job_api_usage("job-api <port> must be positive."))
    return JobAPICommandArgs(port=resolved_port)


def _validate_local_audio_demo_input_path(local_audio_path: Path) -> Path:
    resolved_path = local_audio_path.expanduser().resolve(strict=False)
    if not resolved_path.exists():
        raise SystemExit(f"local-audio-demo failed: input file not found: {resolved_path}")
    if not resolved_path.is_file():
        raise SystemExit(f"local-audio-demo failed: input path must point to a file: {resolved_path}")
    if resolved_path.suffix.lower() not in SUPPORTED_LOCAL_AUDIO_EXTENSIONS:
        supported_extensions = ", ".join(SUPPORTED_LOCAL_AUDIO_EXTENSIONS)
        raise SystemExit(
            "local-audio-demo failed: unsupported input format for the current real local ASR demo. "
            f"Expected one of: {supported_extensions}. Got: {resolved_path.suffix or '<no extension>'}"
        )
    return resolved_path


def _validate_local_video_demo_input_path(local_video_path: Path) -> Path:
    resolved_path = local_video_path.expanduser().resolve(strict=False)
    if not resolved_path.exists():
        raise SystemExit(f"local-video-demo failed: input file not found: {resolved_path}")
    if not resolved_path.is_file():
        raise SystemExit(f"local-video-demo failed: input path must point to a file: {resolved_path}")
    if resolved_path.suffix.lower() not in SUPPORTED_LOCAL_VIDEO_EXTENSIONS:
        supported_extensions = ", ".join(SUPPORTED_LOCAL_VIDEO_EXTENSIONS)
        raise SystemExit(
            "local-video-demo failed: unsupported input format for the current local_video authoritative demo. "
            f"Expected one of: {supported_extensions}. Got: {resolved_path.suffix or '<no extension>'}"
        )
    return resolved_path


def _build_local_audio_demo_usage(error_message: str | None = None) -> str:
    usage_lines = [
        "Usage: python main.py local-audio-demo <local_audio_path> [translation_mode] [tts_mode] [--output editor|publish|both]",
        "Required:",
        "  <local_audio_path>  Local WAV/WAVE file for the real local ASR demo.",
        "Optional:",
        "  translation_mode    mock (default) | real",
        "  tts_mode            mock (default) | real",
        "  --output            editor (default) | publish | both",
        "Notes:",
        "  local-audio-demo currently supports editor output only because publish requires a source video.",
        "  tts_mode=real reuses the existing real TTS provider selection/config path.",
        "  Real TTS reads config with priority: env -> persisted env -> autodub.local.json -> defaults.",
    ]
    if error_message:
        usage_lines.append(f"Error: {error_message}")
    return "\n".join(usage_lines)


def _build_local_video_demo_usage(error_message: str | None = None) -> str:
    usage_lines = [
        "Usage: python main.py local-video-demo <local_video_path> [translation_mode] [tts_mode] [--output editor|publish|both]",
        "Required:",
        "  <local_video_path>  Local video file for the formal authoritative-input demo.",
        "Optional:",
        "  translation_mode    mock (default) | real",
        "  tts_mode            mock (default) | real",
        "  --output            editor (default) | publish | both",
        "Notes:",
        "  local_video currently reuses the existing media_understanding transcript-extraction boundary.",
        "  The current local_video extractor is still expected to fail clearly unless a local_video-capable path is connected.",
    ]
    if error_message:
        usage_lines.append(f"Error: {error_message}")
    return "\n".join(usage_lines)


def _build_main_usage(error_message: str | None = None) -> str:
    usage_lines = [
        "Usage:",
        "  python main.py",
        "  python main.py process <youtube_url> [--voice-a <voice_id>] [--voice-b <voice_id>] [--speaker-a <name>] [--speaker-b <name>] [--speakers auto|1|2] [--project-dir <path>] [--resume-from <stage>]",
        f"  python main.py control-panel [port]  # default port: {CONTROL_PANEL_DEFAULT_PORT}",
        f"  python main.py job-api [port]  # default port: {JOB_API_DEFAULT_PORT}",
        "  python main.py local-audio-demo <local_audio_path> [translation_mode] [tts_mode] [--output editor|publish|both]",
        "  python main.py local-video-demo <local_video_path> [translation_mode] [tts_mode] [--output editor|publish|both]",
        "  python main.py voice-registry show <speaker_id>",
        "  python main.py voice-registry register-builtin <speaker_id> <speaker_name> <voice_id>",
        "  python main.py voice-registry register-cloned <speaker_id> <speaker_name> <voice_id>",
        "  python main.py voice-registry set-default <speaker_id> <voice_id>",
        "  python main.py voice-clone create <speaker_id> <speaker_name> <source_audio_path>",
    ]
    if error_message:
        usage_lines.append(f"Error: {error_message}")
    return "\n".join(usage_lines)


def _build_control_panel_usage(error_message: str | None = None) -> str:
    usage_lines = [
        "Usage: python main.py control-panel [port]",
        "Notes:",
        "  Starts the lightweight local config + voice-registry control panel.",
        "  Default host is 127.0.0.1.",
        f"  Default port is {CONTROL_PANEL_DEFAULT_PORT}.",
        f"  Local config fallback: {PROJECT_ROOT / 'autodub.local.json'}",
    ]
    if error_message:
        usage_lines.append(f"Error: {error_message}")
    return "\n".join(usage_lines)


def _build_job_api_usage(error_message: str | None = None) -> str:
    usage_lines = [
        "Usage: python main.py job-api [port]",
        "Notes:",
        "  Starts the minimal A1 HTTP job API for process-backed YouTube localization jobs.",
        f"  Default host is {JOB_API_DEFAULT_HOST}.",
        f"  Default port is {JOB_API_DEFAULT_PORT}.",
        "  A1 currently accepts only source.type=youtube_url and output_target=editor.",
        "  A1 enforces single-active-job semantics on this local store.",
        "  This is a local integration surface, not a production-grade public service.",
        f"  Jobs are stored under: {PROJECT_ROOT / 'jobs'}",
    ]
    if error_message:
        usage_lines.append(f"Error: {error_message}")
    return "\n".join(usage_lines)


def _build_voice_registry_usage(error_message: str | None = None) -> str:
    usage_lines = [
        "Usage: python main.py voice-registry <action> ...",
        "Actions:",
        "  show <speaker_id>",
        "  register-builtin <speaker_id> <speaker_name> <voice_id>",
        "  register-cloned <speaker_id> <speaker_name> <voice_id>",
        "  set-default <speaker_id> <voice_id>",
        "Notes:",
        "  speaker_id is the stable key; speaker_name is display-only metadata.",
        "  Quote <speaker_name> when it contains spaces.",
        f"  Local config fallback: {PROJECT_ROOT / 'autodub.local.json'}",
        f"  Registry path: AUTODUB_TTS_VOICE_REGISTRY_PATH or default {DEFAULT_VOICE_REGISTRY_PATH}",
    ]
    if error_message:
        usage_lines.append(f"Error: {error_message}")
    return "\n".join(usage_lines)


def _build_voice_clone_usage(error_message: str | None = None) -> str:
    usage_lines = [
        "Usage: python main.py voice-clone <action> ...",
        "Actions:",
        "  create <speaker_id> <speaker_name> <source_audio_path>",
        "Notes:",
        "  This is an explicit command path and does not auto-run in workflow.",
        "  Clone path uses MiniMax upload (/v1/files/upload) then voice_clone (/v1/voice_clone).",
        f"  On success it now runs a minimal verification TTS using fixed sample text: {DEFAULT_VOICE_VERIFICATION_SAMPLE_TEXT}",
        "  Required config can come from env or autodub.local.json.",
        "  Required fields: AUTODUB_TTS_CLONE_ENABLED=true,",
        "    AUTODUB_TTS_CLONE_BASE_URL (or AUTODUB_TTS_BASE_URL),",
        "    AUTODUB_TTS_CLONE_API_KEY (or AUTODUB_TTS_API_KEY).",
        "  Verification reuses the existing real TTS provider boundary; for MiniMax it expects api_protocol=minimax_t2a_v2.",
        "  Optional preview field: AUTODUB_TTS_CLONE_MODEL_NAME (or AUTODUB_TTS_MODEL_NAME).",
        f"  Local config fallback: {PROJECT_ROOT / 'autodub.local.json'}",
        f"  Registry path: AUTODUB_TTS_VOICE_REGISTRY_PATH or default {DEFAULT_VOICE_REGISTRY_PATH}",
    ]
    if error_message:
        usage_lines.append(f"Error: {error_message}")
    return "\n".join(usage_lines)


def _slugify_path_component(raw_value: str) -> str:
    slug = "".join(character.lower() if character.isalnum() else "_" for character in raw_value)
    normalized = "_".join(part for part in slug.split("_") if part)
    return normalized or "audio_input"


def _summarize_tts_voice_id(alignment_payload: dict[str, object]) -> str | None:
    resolved_voice_id = alignment_payload.get("resolved_voice_id")
    if isinstance(resolved_voice_id, str):
        stripped_resolved_voice_id = resolved_voice_id.strip()
        if stripped_resolved_voice_id:
            return _truncate_identifier(stripped_resolved_voice_id)
    version_context = alignment_payload.get("version_context", {})
    if not isinstance(version_context, dict):
        return None
    voice_id = version_context.get("voice_id")
    if not isinstance(voice_id, str):
        return None
    stripped = voice_id.strip()
    if not stripped:
        return None
    return _truncate_identifier(stripped)


def _read_tts_voice_resolution_source(alignment_payload: dict[str, object]) -> str | None:
    voice_resolution_source = alignment_payload.get("voice_resolution_source")
    if not isinstance(voice_resolution_source, str):
        return None
    stripped = voice_resolution_source.strip()
    return stripped or None


def _read_primary_tts_speaker_id(alignment_payload: dict[str, object]) -> str | None:
    resolved_speaker_id = alignment_payload.get("resolved_speaker_id")
    if isinstance(resolved_speaker_id, str):
        stripped_resolved_speaker_id = resolved_speaker_id.strip()
        if stripped_resolved_speaker_id:
            return stripped_resolved_speaker_id

    artifacts = alignment_payload.get("artifacts", {})
    if not isinstance(artifacts, dict):
        return None
    voice_resolution_summary = artifacts.get("voice_resolution_summary", [])
    if not isinstance(voice_resolution_summary, list):
        return None
    speaker_ids = {
        speaker_id.strip()
        for speaker_id in (entry.get("speaker_id") for entry in voice_resolution_summary if isinstance(entry, dict))
        if isinstance(speaker_id, str) and speaker_id.strip()
    }
    return next(iter(speaker_ids)) if len(speaker_ids) == 1 else None


def _truncate_identifier(value: str, head: int = 18, tail: int = 6) -> str:
    if len(value) <= head + tail + 3:
        return value
    return f"{value[:head]}...{value[-tail:]}"


def _find_exception_in_chain(exc: BaseException, target_types: tuple[type[BaseException], ...]) -> BaseException | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, target_types):
            return current
        seen.add(id(current))
        next_exc = current.__cause__ or current.__context__
        current = next_exc if isinstance(next_exc, BaseException) else None
    return None


def _format_local_audio_demo_error(exc: Exception, local_audio_path: Path) -> str:
    if isinstance(exc, SystemExit):
        return str(exc)

    publish_error = _find_exception_in_chain(exc, (PublishError,))
    if publish_error is not None:
        return (
            "local-audio-demo failed: publish output is unavailable for local audio input because no source video "
            f"is present. Details: {publish_error}"
        )

    invalid_source_error = _find_exception_in_chain(exc, (MediaUnderstandingInvalidSourcePathError,))
    if invalid_source_error is not None:
        return f"local-audio-demo failed: invalid local audio input path. Details: {invalid_source_error}"

    no_result_error = _find_exception_in_chain(exc, (MediaUnderstandingTranscriptExtractionNoResultError,))
    if no_result_error is not None:
        return (
            "local-audio-demo failed: real local ASR returned no recognizable speech. "
            "Check the installed recognizer, input language, and whether the audio contains usable speech. "
            f"Input: {local_audio_path}. Details: {no_result_error}"
        )

    extraction_model_error = _find_exception_in_chain(exc, (MediaUnderstandingTranscriptExtractionModelError,))
    if extraction_model_error is not None:
        return (
            "local-audio-demo failed: the real local ASR recognizer/config is not available in the current environment. "
            f"Details: {extraction_model_error}"
        )

    extraction_unavailable_error = _find_exception_in_chain(
        exc,
        (MediaUnderstandingTranscriptExtractionUnavailableError,),
    )
    if extraction_unavailable_error is not None:
        extraction_unavailable_text = str(extraction_unavailable_error)
        if "command_transcript_extraction" in extraction_unavailable_text:
            return (
                "local-audio-demo failed: the configured transcript extraction stub is not connected in the current "
                f"environment. Details: {extraction_unavailable_error}"
            )
        return (
            "local-audio-demo failed: the real local ASR backend is unavailable in the current environment. "
            f"Details: {extraction_unavailable_error}"
        )

    extraction_runtime_error = _find_exception_in_chain(
        exc,
        (MediaUnderstandingTranscriptExtractionRuntimeError,),
    )
    if extraction_runtime_error is not None:
        return (
            "local-audio-demo failed: the real local ASR runtime could not process this audio input. "
            f"Input: {local_audio_path}. Details: {extraction_runtime_error}"
        )

    translation_config_error = _find_exception_in_chain(exc, (TranslationConfigurationError,))
    if translation_config_error is not None:
        translation_config_summary = RealTranslationProviderConfig.from_env().build_diagnostic_summary()
        return (
            "local-audio-demo failed: real translation mode is not configured correctly. "
            f"Details: {translation_config_error}. "
            f"Config summary: {translation_config_summary}"
        )

    tts_config_error = _find_exception_in_chain(exc, (TTSConfigurationError,))
    if tts_config_error is not None:
        tts_config_summary = RealTTSProviderConfig.from_env().build_diagnostic_summary()
        if "No TTS voice could be resolved" in str(tts_config_error):
            return (
                "local-audio-demo failed: no TTS voice could be resolved for the current speaker. "
                "Register a speaker voice with `main.py voice-registry ...`, or set AUTODUB_TTS_VOICE_ID "
                "as the final fallback. "
                f"Details: {tts_config_error}. "
                f"Config summary: {tts_config_summary}"
            )
        return (
            "local-audio-demo failed: real TTS mode is not configured correctly. "
            "Check AUTODUB_TTS_ENABLED, AUTODUB_TTS_MODEL_NAME, AUTODUB_TTS_BASE_URL, "
            "AUTODUB_TTS_VOICE_REGISTRY_PATH or AUTODUB_TTS_VOICE_ID, and the configured "
            "AUTODUB_TTS_API_KEY env wiring. "
            f"Details: {tts_config_error}. "
            f"Config summary: {tts_config_summary}"
        )

    unresolved_voice_marker = "No TTS voice could be resolved"
    if unresolved_voice_marker in str(exc) or unresolved_voice_marker in str(exc.__cause__ or ""):
        return (
            "local-audio-demo failed: no TTS voice could be resolved for the current speaker. "
            "Register a speaker voice with `main.py voice-registry ...`, or set AUTODUB_TTS_VOICE_ID "
            "as the final fallback. "
            f"Details: {exc.__cause__ or exc}"
        )

    if _find_exception_in_chain(exc, (WorkflowError,)) is not None:
        return f"local-audio-demo failed: workflow execution did not complete. Root cause: {exc.__cause__ or exc}"

    return f"local-audio-demo failed: {exc}"


def _format_local_video_demo_error(exc: Exception, local_video_path: Path) -> str:
    if isinstance(exc, SystemExit):
        return str(exc)

    publish_error = _find_exception_in_chain(exc, (PublishError,))
    if publish_error is not None:
        return f"local-video-demo failed: publish output did not complete. Details: {publish_error}"

    invalid_source_error = _find_exception_in_chain(exc, (MediaUnderstandingInvalidSourcePathError,))
    if invalid_source_error is not None:
        return f"local-video-demo failed: invalid local video input path. Details: {invalid_source_error}"

    unsupported_source_error = _find_exception_in_chain(exc, (MediaUnderstandingUnsupportedSourceKindError,))
    if unsupported_source_error is not None:
        return (
            "local-video-demo failed: local_video authoritative input is wired, but the current transcript "
            "extraction path is not connected in this sprint. "
            f"Input: {local_video_path}. Details: {unsupported_source_error}"
        )

    extraction_unavailable_error = _find_exception_in_chain(
        exc,
        (MediaUnderstandingTranscriptExtractionUnavailableError,),
    )
    if extraction_unavailable_error is not None:
        return (
            "local-video-demo failed: local_video authoritative input is wired, but the current transcript "
            "extractor is not connected in this sprint. "
            f"Input: {local_video_path}. Details: {extraction_unavailable_error}"
        )

    extraction_model_error = _find_exception_in_chain(exc, (MediaUnderstandingTranscriptExtractionModelError,))
    if extraction_model_error is not None:
        return (
            "local-video-demo failed: the configured local_video transcript extractor/config is not available "
            f"in the current environment. Details: {extraction_model_error}"
        )

    extraction_runtime_error = _find_exception_in_chain(
        exc,
        (MediaUnderstandingTranscriptExtractionRuntimeError,),
    )
    if extraction_runtime_error is not None:
        return (
            "local-video-demo failed: the current local_video transcript extractor could not process this input. "
            f"Input: {local_video_path}. Details: {extraction_runtime_error}"
        )

    translation_config_error = _find_exception_in_chain(exc, (TranslationConfigurationError,))
    if translation_config_error is not None:
        translation_config_summary = RealTranslationProviderConfig.from_env().build_diagnostic_summary()
        return (
            "local-video-demo failed: real translation mode is not configured correctly. "
            f"Details: {translation_config_error}. "
            f"Config summary: {translation_config_summary}"
        )

    tts_config_error = _find_exception_in_chain(exc, (TTSConfigurationError,))
    if tts_config_error is not None:
        tts_config_summary = RealTTSProviderConfig.from_env().build_diagnostic_summary()
        if "No TTS voice could be resolved" in str(tts_config_error):
            return (
                "local-video-demo failed: no TTS voice could be resolved for the current speaker. "
                "Register a speaker voice with `main.py voice-registry ...`, or set AUTODUB_TTS_VOICE_ID "
                "as the final fallback. "
                f"Details: {tts_config_error}. "
                f"Config summary: {tts_config_summary}"
            )
        return (
            "local-video-demo failed: real TTS mode is not configured correctly. "
            "Check AUTODUB_TTS_ENABLED, AUTODUB_TTS_MODEL_NAME, AUTODUB_TTS_BASE_URL, "
            "AUTODUB_TTS_VOICE_REGISTRY_PATH or AUTODUB_TTS_VOICE_ID, and the configured "
            "AUTODUB_TTS_API_KEY env wiring. "
            f"Details: {tts_config_error}. "
            f"Config summary: {tts_config_summary}"
        )

    unresolved_voice_marker = "No TTS voice could be resolved"
    if unresolved_voice_marker in str(exc) or unresolved_voice_marker in str(exc.__cause__ or ""):
        return (
            "local-video-demo failed: no TTS voice could be resolved for the current speaker. "
            "Register a speaker voice with `main.py voice-registry ...`, or set AUTODUB_TTS_VOICE_ID "
            "as the final fallback. "
            f"Details: {exc.__cause__ or exc}"
        )

    if _find_exception_in_chain(exc, (WorkflowError,)) is not None:
        return f"local-video-demo failed: workflow execution did not complete. Root cause: {exc.__cause__ or exc}"

    return f"local-video-demo failed: {exc}"


def _resolve_voice_registry_path() -> Path:
    config = config_loader.load_project_local_config()
    configured_path, _ = config_loader.resolve_path_value(
        env_keys=["AUTODUB_TTS_VOICE_REGISTRY_PATH"],
        config=config,
        config_key_paths=(
            ("voice_registry", "registry_path"),
            ("tts", "voice_registry_path"),
            ("paths", "voice_registry_path"),
        ),
    )
    if configured_path and configured_path.strip():
        return Path(configured_path.strip()).expanduser().resolve(strict=False)
    return DEFAULT_VOICE_REGISTRY_PATH


def _resolve_voice_registry_provider_name() -> str:
    config = config_loader.load_project_local_config()
    provider_name, _ = config_loader.resolve_text_value(
        env_keys=["AUTODUB_TTS_PROVIDER_NAME"],
        config=config,
        config_key_paths=(
            ("voice_registry", "provider_name"),
            ("tts", "provider_name"),
        ),
    )
    normalized_provider_name = (provider_name or VOICE_REGISTRY_PROVIDER_NAME).strip()
    return normalized_provider_name or VOICE_REGISTRY_PROVIDER_NAME


def _resolve_voice_registry_tts_provider() -> str:
    config = config_loader.load_project_local_config()
    tts_provider, _ = config_loader.resolve_text_value(
        env_keys=["AUTODUB_TTS_TTS_PROVIDER", "AUTODUB_TTS_PROVIDER_NAME"],
        config=config,
        config_key_paths=(
            ("tts", "tts_provider"),
            ("voice_registry", "tts_provider"),
            ("tts", "provider_name"),
            ("voice_registry", "provider_name"),
        ),
    )
    normalized_tts_provider = (tts_provider or VOICE_REGISTRY_PROVIDER_NAME).strip().lower()
    return normalized_tts_provider or VOICE_REGISTRY_PROVIDER_NAME


def _resolve_voice_registry_platform() -> str | None:
    config = config_loader.load_project_local_config()
    platform, _ = config_loader.resolve_text_value(
        env_keys=["AUTODUB_TTS_PLATFORM"],
        config=config,
        config_key_paths=(
            ("tts", "platform"),
            ("voice_registry", "platform"),
        ),
    )
    normalized_platform = platform.strip().lower() if isinstance(platform, str) else ""
    if normalized_platform:
        return normalized_platform
    if _resolve_voice_registry_tts_provider() == "minimax_tts":
        return "minimax_domestic"
    return None


def _verify_cloned_voice_asset(
    *,
    clone_result,
    config_path: Path | None = None,
) -> VoiceAssetVerificationResult:
    verifier = VoiceAssetVerifier.from_env(config_path=config_path)
    return verifier.verify_voice(
        speaker_id=clone_result.speaker_id,
        voice_id=clone_result.voice_id,
        sample_text=DEFAULT_VOICE_VERIFICATION_SAMPLE_TEXT,
    )


def _serialize_speaker_voice_profile(profile: SpeakerVoiceProfile) -> dict[str, object]:
    return {
        "speaker_id": profile.speaker_id,
        "speaker_name": profile.speaker_name,
        "default_voice_id": profile.default_voice_id,
        "default_voice_type": profile.default_voice_type,
        "voices": [
            {
                "voice_id": voice.voice_id,
                "voice_type": voice.voice_type,
                "provider": voice.provider,
                "tts_provider": voice.tts_provider,
                "platform": voice.platform,
                "label": voice.label,
                "created_at": voice.created_at,
                "source_audio_path": voice.source_audio_path,
                "notes": voice.notes,
                "verification_status": voice.verification_status,
                "last_verified_at": voice.last_verified_at,
                "last_verification_success": voice.last_verification_success,
                "last_verification_audio_path": voice.last_verification_audio_path,
                "last_verification_error": voice.last_verification_error,
            }
            for voice in profile.voices
        ],
    }


def _build_voice_registry_show_payload(
    profile: SpeakerVoiceProfile,
    resolution,
) -> dict[str, object]:
    payload = _serialize_speaker_voice_profile(profile)
    payload["resolution"] = {
        "status": resolution.status,
        "source": resolution.source,
        "voice_id": resolution.voice_id,
        "voice_type": resolution.voice_type,
        "provider": resolution.provider,
        "tts_provider": resolution.tts_provider,
        "platform": resolution.platform,
        "label": resolution.label,
    }
    return payload


def main() -> None:
    if len(sys.argv) == 1:
        run_demo_pipeline()
        return

    command = sys.argv[1].strip().lower()
    if command in {"-h", "--help", "help"}:
        raise SystemExit(_build_main_usage())
    if command == "control-panel":
        run_control_panel_command(sys.argv)
        return
    if command == "job-api":
        run_job_api_command(sys.argv)
        return
    # Legacy compatibility boundary: do not route this command through dispatcher yet.
    if command == "process":
        run_process_command(sys.argv)
        return
    if command == "local-audio-demo":
        parsed_args = parse_local_audio_demo_args(sys.argv)
        run_local_audio_demo_pipeline(
            parsed_args.local_audio_path,
            translation_mode=parsed_args.translation_mode,
            tts_mode=parsed_args.tts_mode,
            output_target=parsed_args.output_target,
        )
        return
    if command == "local-video-demo":
        parsed_args = parse_local_video_demo_args(sys.argv)
        run_local_video_demo_pipeline(
            parsed_args.local_video_path,
            translation_mode=parsed_args.translation_mode,
            tts_mode=parsed_args.tts_mode,
            output_target=parsed_args.output_target,
        )
        return
    if command == "voice-registry":
        run_voice_registry_command(sys.argv)
        return
    if command == "voice-clone":
        run_voice_clone_command(sys.argv)
        return

    raise SystemExit(_build_main_usage(f"Unsupported command: {sys.argv[1]}"))


if __name__ == "__main__":
    main()
