from pathlib import Path
import wave

import pytest

from core.enums import StageStatus
from core.exceptions import WorkflowError
from modules.alignment.alignment_orchestrator import AlignmentConfig, AlignmentOrchestrator
from modules.alignment.dsp_stretcher import DSPStretcher
from modules.alignment.rewrite_engine import RewriteEngine
from modules.chunking.semantic_block_builder import SemanticBlockBuilder
from modules.draft.caption_retiming import CaptionRetimer
from modules.draft.draft_writer import DraftWriter
from modules.ingestion.models import SubtitleSeed
from modules.ingestion.providers import MemorySubtitleProvider
from modules.media_understanding.models import (
    MediaSource,
    MediaSourceKind,
    TranscriptExtractionRequest,
    TranscriptExtractionResult,
    TranscriptLine,
)
from modules.media_understanding.pipeline import MediaUnderstandingPipeline
from modules.media_understanding.providers import (
    LocalSRTProvider,
    LocalTranscriptProvider,
    MediaUnderstandingProviderSelectionConfig,
    SystemSpeechLocalASRTranscriptExtractionProvider,
)
from modules.translation.router import TranslationChunkRouter, TranslationRouterConfig
from modules.translation.translator import MockTranslator, TranslationPipeline
import modules.workflow.project_workflow as project_workflow_module
from modules.workflow.project_workflow import ProjectWorkflow, ProjectWorkflowConfig
from modules.workflow.workflow_result import WorkflowBuildResult
from services.audio_service import AudioService
from services.cache_manager import CacheManager
from services.llm_service import MockLLMService
from services.state_manager import StateManager
from services.tts_service import MockTTSConfig, MockTTSService


EXPECTED_PROVIDER_REPORT_FIELDS = {
    "provider_name",
    "provider_mode",
    "model_name",
    "version_context",
    "execution_mode",
    "fallback_applied",
    "fallback_reason",
    "fallback_trigger",
    "fallback_from",
    "fallback_to",
    "retry_attempted",
    "retry_count",
    "error_type",
    "retry_candidate",
    "final_error_type",
    "final_error_message",
    "reused_artifacts",
    "artifact_paths",
    "restore_reason",
    "rerun_reason",
    "source_input_hash",
}

EXPECTED_MEDIA_AUDIT_FIELDS = {
    "provider_name",
    "provider_mode",
    "extraction_provider_name",
    "extraction_provider_mode",
    "extraction_version_context",
    "source_kind",
    "source_path",
    "execution_mode",
    "error_type",
    "authoritative_input_used",
    "authoritative_path_kind",
    "authoritative_flow",
    "transcript_extraction_used",
    "attributed_transcript_normalized",
    "subtitle_line_bridge_applied",
    "fallback_applied",
    "fallback_reason",
    "fallback_stage",
    "version_context",
}


def _write_dummy_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x00\x00" * 1600)


class FakeRealTranslator:
    def translate_batch(self, lines: list) -> list[str]:
        return [f"REAL_CN:{line.en_text.strip()}" for line in lines]

    def get_cache_context(self) -> dict[str, object]:
        return {
            "provider_mode": "real",
            "api_protocol": "chat_completions_v1",
            "provider_variant": "fake_real_translation_v1",
        }


class FakeRealTTSService:
    def __init__(self, output_dir: str) -> None:
        self.mock_tts = MockTTSService(
            output_dir=output_dir,
            config=MockTTSConfig(ms_per_char=80, min_duration_ms=800),
        )

    def synthesize(self, block) -> str:
        return self.mock_tts.synthesize(block)

    def get_cache_context(self) -> dict[str, object]:
        return {
            "provider_mode": "real",
            "api_protocol": "audio_speech_v1",
            "provider_variant": "fake_real_tts_v1",
            "audio_format": "wav",
        }


def _build_workflow(
    tmp_path: Path,
    translation_kind: str = "mock",
    tts_kind: str = "mock",
    workflow_tag: str | None = None,
    subtitle_seeds: list[SubtitleSeed] | None = None,
    translation_audit_overrides: dict[str, object] | None = None,
    alignment_audit_overrides: dict[str, object] | None = None,
    media_source: MediaSource | None = None,
    media_understanding_pipeline: MediaUnderstandingPipeline | None = None,
) -> tuple[ProjectWorkflow, Path, dict[str, dict[str, object]]]:
    normalized_workflow_tag = workflow_tag or f"{translation_kind}_{tts_kind}"
    output_root = tmp_path / f"workflow_output_{normalized_workflow_tag}"
    subtitle_seed_items = subtitle_seeds or [
        SubtitleSeed(
            index=1,
            start_ms=0,
            end_ms=800,
            en_text="Welcome back.",
            speaker_id="speaker_host",
            speaker_name="Host",
        ),
        SubtitleSeed(
            index=2,
            start_ms=900,
            end_ms=1_700,
            en_text="We are assembling a draft scaffold.",
            speaker_id="speaker_host",
            speaker_name="Host",
        ),
    ]
    subtitle_provider = None if media_source is not None else MemorySubtitleProvider(subtitle_seed_items)
    translation_provider, translation_audit = _build_translation_runtime(translation_kind)
    if translation_audit_overrides:
        translation_audit.update(translation_audit_overrides)
    translation_pipeline = TranslationPipeline(
        router=TranslationChunkRouter(
            TranslationRouterConfig(batch_size=2, max_batch_size=4, max_chars_per_batch=100)
        ),
        translator=translation_provider,
    )
    tts_service, alignment_audit = _build_tts_runtime(tts_kind, output_root)
    if alignment_audit_overrides:
        alignment_audit.update(alignment_audit_overrides)
    workflow = ProjectWorkflow(
        subtitle_provider=subtitle_provider,
        translation_pipeline=translation_pipeline,
        block_builder=SemanticBlockBuilder(),
        alignment_orchestrator=AlignmentOrchestrator(
            tts_service=tts_service,
            audio_service=AudioService(),
            rewrite_engine=RewriteEngine(llm_service=MockLLMService()),
            dsp_stretcher=DSPStretcher(),
            config=AlignmentConfig(),
        ),
        caption_retimer=CaptionRetimer(),
        draft_writer=DraftWriter(output_root_dir=str(output_root)),
        state_manager=StateManager(str(output_root / "project_state.json")),
        cache_manager=CacheManager(str(output_root / "project_cache.json")),
        config=ProjectWorkflowConfig(
            project_id=f"workflow_{normalized_workflow_tag}",
            translation_provider_name=str(translation_audit["provider_name"]),
            translation_target_language="zh-CN",
            translation_model_name=_read_optional_text(translation_audit, "model_name"),
            translation_provider_mode=str(translation_audit["provider_mode"]),
            translation_version_context=dict(translation_audit["version_context"]),
            translation_fallback_applied=bool(translation_audit["fallback_applied"]),
            translation_fallback_reason=_read_optional_text(translation_audit, "fallback_reason"),
            translation_fallback_stage=_read_optional_text(translation_audit, "fallback_stage"),
            translation_runtime_fallback_enabled=bool(translation_audit["runtime_fallback_enabled"]),
            translation_fallback_from=_read_optional_text(translation_audit, "fallback_from"),
            translation_fallback_to=_read_optional_text(translation_audit, "fallback_to"),
            tts_provider_name=str(alignment_audit["provider_name"]),
            tts_voice_name=str(alignment_audit["voice_name"]),
            tts_model_name=_read_optional_text(alignment_audit, "model_name"),
            tts_provider_mode=str(alignment_audit["provider_mode"]),
            tts_version_context=dict(alignment_audit["version_context"]),
            tts_fallback_applied=bool(alignment_audit["fallback_applied"]),
            tts_fallback_reason=_read_optional_text(alignment_audit, "fallback_reason"),
            tts_fallback_stage=_read_optional_text(alignment_audit, "fallback_stage"),
        ),
        media_understanding_pipeline=media_understanding_pipeline,
        media_source=media_source,
    )
    return workflow, output_root, {"translation": translation_audit, "alignment": alignment_audit}


def _build_translation_runtime(kind: str) -> tuple[object, dict[str, object]]:
    if kind == "mock":
        provider = MockTranslator()
        return provider, {
            "provider_name": "mock_translator",
            "provider_mode": "mock",
            "model_name": None,
            "version_context": provider.get_cache_context(),
            "fallback_applied": False,
            "fallback_reason": None,
            "fallback_stage": None,
            "runtime_fallback_enabled": False,
            "fallback_from": None,
            "fallback_to": None,
        }
    if kind == "real":
        provider = FakeRealTranslator()
        return provider, {
            "provider_name": "openai_compatible",
            "provider_mode": "real",
            "model_name": "fake-translation-model",
            "version_context": provider.get_cache_context(),
            "fallback_applied": False,
            "fallback_reason": None,
            "fallback_stage": None,
            "runtime_fallback_enabled": False,
            "fallback_from": None,
            "fallback_to": None,
        }
    raise ValueError(f"Unsupported translation_kind: {kind}")


def _build_tts_runtime(kind: str, output_root: Path) -> tuple[object, dict[str, object]]:
    if kind == "mock":
        provider = MockTTSService(
            output_dir=str(output_root / "audio"),
            config=MockTTSConfig(ms_per_char=80, min_duration_ms=800),
        )
        return provider, {
            "provider_name": "mock_tts",
            "provider_mode": "mock",
            "model_name": None,
            "voice_name": "default",
            "version_context": provider.get_cache_context(),
            "fallback_applied": False,
            "fallback_reason": None,
            "fallback_stage": None,
        }
    if kind == "real":
        provider = FakeRealTTSService(output_dir=str(output_root / "audio"))
        return provider, {
            "provider_name": "openai_compatible_tts",
            "provider_mode": "real",
            "model_name": "fake-tts-model",
            "voice_name": "alloy",
            "version_context": provider.get_cache_context(),
            "fallback_applied": False,
            "fallback_reason": None,
            "fallback_stage": None,
        }
    raise ValueError(f"Unsupported tts_kind: {kind}")


def _read_optional_text(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _assert_provider_run_report(
    payload: dict[str, object],
    expected: dict[str, object],
    *,
    execution_mode: str,
    stage_name: str,
) -> None:
    assert EXPECTED_PROVIDER_REPORT_FIELDS.issubset(payload)
    assert payload["provider_name"] == expected["provider_name"]
    assert payload["provider_mode"] == expected["provider_mode"]
    assert payload["model_name"] == expected["model_name"]
    assert payload["version_context"] == expected["version_context"]
    assert payload["execution_mode"] == execution_mode
    assert payload["fallback_applied"] == expected["fallback_applied"]
    assert payload["fallback_reason"] == expected["fallback_reason"]
    assert payload["fallback_trigger"] is None
    assert payload["fallback_from"] is None
    assert payload["fallback_to"] is None
    assert payload["retry_attempted"] is False
    assert payload["retry_count"] == 0
    assert payload["error_type"] is None
    assert payload["retry_candidate"] is None
    assert payload["final_error_type"] is None
    assert payload["final_error_message"] is None
    assert isinstance(payload["artifact_paths"], list)
    assert isinstance(payload["reused_artifacts"], list)
    assert payload["source_input_hash"] is not None
    if execution_mode == "fresh_run":
        assert payload["restore_reason"] is None
        assert payload["rerun_reason"] == f"{stage_name}_cache_miss"
    if execution_mode == "cache_restore_full":
        assert payload["restore_reason"] == f"{stage_name}_cache_restore_full"
        assert payload["rerun_reason"] is None


def _read_stage_payload(stage_snapshot: dict[str, object], stage_name: str) -> dict[str, object]:
    stage = stage_snapshot[stage_name]
    payload = stage.get("payload", {})
    if isinstance(payload, dict):
        return payload
    return {}


def test_project_workflow_runs_to_draft_scaffold(tmp_path: Path) -> None:
    workflow, output_root, expected = _build_workflow(tmp_path, translation_kind="mock", tts_kind="mock")

    result = workflow.run()
    state_manager = StateManager(str(output_root / "project_state.json"))
    draft_stage = state_manager.get_stage("draft")
    translation_stage = state_manager.get_stage("translation")
    alignment_stage = state_manager.get_stage("alignment")
    media_stage = state_manager.get_stage("media_understanding")

    assert Path(result.draft_content_path).exists()
    assert Path(result.draft_meta_info_path).exists()
    assert result.export_path is not None
    assert Path(result.export_path).exists()
    assert Path(result.draft_dir, "materials").exists()
    assert result.block_count == 1
    assert result.caption_count == 2
    assert result.material_count == 1
    assert draft_stage is not None
    assert draft_stage["status"] == StageStatus.DONE.value
    assert draft_stage["payload"]["draft_dir"] == result.draft_dir
    assert draft_stage["payload"]["export_path"] == result.export_path
    assert draft_stage["payload"]["draft_content_path"] == result.draft_content_path
    assert draft_stage["payload"]["draft_meta_info_path"] == result.draft_meta_info_path
    assert draft_stage["payload"]["artifacts"]["file_count"] == 3
    assert draft_stage["payload"]["rerun_reason"] == "draft_stage_state_missing_or_not_done"
    assert draft_stage["payload"]["restore_reason"] is None
    assert draft_stage["payload"]["artifact_paths"]
    assert draft_stage["payload"]["reused_artifacts"] == []
    assert translation_stage is not None
    assert alignment_stage is not None
    assert media_stage is not None
    assert media_stage["status"] == StageStatus.DONE.value
    assert EXPECTED_MEDIA_AUDIT_FIELDS.issubset(media_stage["payload"])
    assert media_stage["payload"]["provider_name"] == "SubtitleLinePassthrough"
    assert media_stage["payload"]["provider_mode"] == "passthrough"
    assert media_stage["payload"]["extraction_version_context"] == {}
    assert media_stage["payload"]["execution_mode"] == "passthrough"
    assert media_stage["payload"]["error_type"] is None
    assert media_stage["payload"]["fallback_applied"] is False
    assert media_stage["payload"]["fallback_reason"] is None
    assert media_stage["payload"]["fallback_stage"] is None
    assert media_stage["payload"]["source_kind"] == "ingested_subtitle_lines"
    assert media_stage["payload"]["source_path"] is None
    assert media_stage["payload"]["authoritative_input_used"] is False
    assert media_stage["payload"]["authoritative_path_kind"] is None
    assert media_stage["payload"]["authoritative_flow"] is None
    assert media_stage["payload"]["transcript_extraction_used"] is False
    assert media_stage["payload"]["attributed_transcript_normalized"] is False
    assert media_stage["payload"]["subtitle_line_bridge_applied"] is False
    assert media_stage["payload"]["line_count"] == 2
    assert media_stage["payload"]["attributed_line_count"] == 2
    assert media_stage["payload"]["rerun_reason"] == "media_understanding_stage_placeholder"
    _assert_provider_run_report(
        translation_stage["payload"],
        expected["translation"],
        execution_mode="fresh_run",
        stage_name="translation",
    )
    _assert_provider_run_report(
        alignment_stage["payload"],
        expected["alignment"],
        execution_mode="fresh_run",
        stage_name="alignment",
    )
    assert translation_stage["payload"]["literal_text_layer_produced"] is True
    assert translation_stage["payload"]["tts_text_layer_produced"] is False
    assert alignment_stage["payload"]["artifacts"]["file_count"] >= 1
    ingestion_stage = state_manager.get_stage("ingestion")
    chunking_stage = state_manager.get_stage("chunking")
    assert ingestion_stage is not None
    assert chunking_stage is not None
    assert chunking_stage["payload"]["literal_text_layer_produced"] is True
    assert alignment_stage["payload"]["literal_text_layer_produced"] is True
    assert alignment_stage["payload"]["tts_text_layer_produced"] is True
    assert alignment_stage["payload"]["text_layer_summary"]["literal_block_count"] == 1
    assert alignment_stage["payload"]["text_layer_summary"]["tts_block_count"] == 1
    assert alignment_stage["payload"]["text_layer_summary"]["tts_line_count"] == 2
    assert ingestion_stage["payload"]["rerun_reason"] == "source_provider_load_required"
    assert chunking_stage["payload"]["rerun_reason"] == "chunking_always_recomputed"
    assert result.stage_snapshot["draft"]["status"] == StageStatus.DONE.value


def test_project_workflow_run_build_returns_canonical_build_result(tmp_path: Path) -> None:
    workflow, _, _ = _build_workflow(tmp_path, translation_kind="mock", tts_kind="mock")

    result = workflow.run_build()

    assert isinstance(result, WorkflowBuildResult)
    assert result.project_id == workflow.config.project_id
    assert result.localized_project.project_id == workflow.config.project_id
    assert result.localized_project.source_info["source_kind"] == "ingested_subtitle_lines"
    assert len(result.localized_project.captions) == 2
    assert len(result.localized_project.semantic_blocks) == 1
    assert len(result.localized_project.aligned_blocks) == 1
    assert result.artifact_index is result.localized_project.artifacts
    assert Path(result.artifact_index.require("editor.draft_content")).exists()
    assert Path(result.artifact_index.require("editor.draft_meta")).exists()
    assert Path(result.artifact_index.require("editor.export_json")).exists()
    assert result.stage_snapshot["draft"]["status"] == StageStatus.DONE.value


def test_project_workflow_run_build_uses_shared_shape_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _, _ = _build_workflow(tmp_path, translation_kind="mock", tts_kind="mock")
    captured: dict[str, int] = {
        "source_info_calls": 0,
        "core_artifact_calls": 0,
        "editor_artifact_calls": 0,
    }
    real_build_canonical_source_info = project_workflow_module.build_canonical_source_info
    real_build_core_media_artifact_entries = project_workflow_module.build_core_media_artifact_entries
    real_build_editor_artifact_entries = project_workflow_module.build_editor_artifact_entries

    def recording_build_canonical_source_info(**kwargs):
        captured["source_info_calls"] += 1
        captured["source_kind"] = kwargs["source_kind"]
        return real_build_canonical_source_info(**kwargs)

    def recording_build_core_media_artifact_entries(**kwargs):
        captured["core_artifact_calls"] += 1
        return real_build_core_media_artifact_entries(**kwargs)

    def recording_build_editor_artifact_entries(**kwargs):
        captured["editor_artifact_calls"] += 1
        return real_build_editor_artifact_entries(**kwargs)

    monkeypatch.setattr(
        project_workflow_module,
        "build_canonical_source_info",
        recording_build_canonical_source_info,
    )
    monkeypatch.setattr(
        project_workflow_module,
        "build_core_media_artifact_entries",
        recording_build_core_media_artifact_entries,
    )
    monkeypatch.setattr(
        project_workflow_module,
        "build_editor_artifact_entries",
        recording_build_editor_artifact_entries,
    )

    result = workflow.run_build()

    assert result.localized_project.source_info["source_kind"] == "ingested_subtitle_lines"
    assert captured["source_info_calls"] == 1
    assert captured["core_artifact_calls"] == 1
    assert captured["editor_artifact_calls"] == 1
    assert captured["source_kind"] == "ingested_subtitle_lines"


def test_project_workflow_runs_through_authoritative_media_understanding_transcript_path(tmp_path: Path) -> None:
    workflow, output_root, _ = _build_workflow(
        tmp_path,
        workflow_tag="authoritative_transcript",
        media_source=MediaSource(
            kind=MediaSourceKind.TRANSCRIPT,
            transcript_lines=[
                TranscriptLine(index=1, start_ms=0, end_ms=800, source_text="Welcome back."),
                TranscriptLine(index=2, start_ms=900, end_ms=1_700, source_text="We are assembling a draft scaffold."),
            ],
        ),
        media_understanding_pipeline=MediaUnderstandingPipeline(LocalTranscriptProvider()),
    )

    result = workflow.run()
    state_manager = StateManager(str(output_root / "project_state.json"))
    ingestion_stage = state_manager.get_stage("ingestion")
    media_stage = state_manager.get_stage("media_understanding")
    translation_stage = state_manager.get_stage("translation")

    assert Path(result.draft_content_path).exists()
    assert ingestion_stage is not None
    assert ingestion_stage["payload"]["execution_mode"] == "source_reference"
    assert ingestion_stage["payload"]["artifacts"]["kind"] == "media_source"
    assert ingestion_stage["payload"]["rerun_reason"] == "media_source_reference_load_required"
    assert media_stage is not None
    assert EXPECTED_MEDIA_AUDIT_FIELDS.issubset(media_stage["payload"])
    assert media_stage["payload"]["provider_name"] == "local_transcript"
    assert media_stage["payload"]["provider_mode"] == "local_transcript"
    assert media_stage["payload"]["extraction_version_context"] == {}
    assert media_stage["payload"]["execution_mode"] == "provider_run"
    assert media_stage["payload"]["source_kind"] == MediaSourceKind.TRANSCRIPT.value
    assert media_stage["payload"]["source_path"] is None
    assert media_stage["payload"]["error_type"] is None
    assert media_stage["payload"]["authoritative_input_used"] is True
    assert media_stage["payload"]["authoritative_path_kind"] == MediaSourceKind.TRANSCRIPT.value
    assert media_stage["payload"]["authoritative_flow"] == (
        "transcript -> attributed_transcript -> subtitle_line_bridge"
    )
    assert media_stage["payload"]["transcript_extraction_used"] is False
    assert media_stage["payload"]["attributed_transcript_normalized"] is True
    assert media_stage["payload"]["subtitle_line_bridge_applied"] is True
    assert media_stage["payload"]["fallback_applied"] is False
    assert media_stage["payload"]["rerun_reason"] == "media_understanding_authoritative_run"
    assert translation_stage is not None
    assert translation_stage["status"] == StageStatus.DONE.value


def test_project_workflow_runs_through_authoritative_media_understanding_srt_path(tmp_path: Path) -> None:
    srt_path = tmp_path / "authoritative.srt"
    srt_path.write_text(
        "1\n"
        "00:00:00,000 --> 00:00:00,800\n"
        "Welcome back.\n\n"
        "2\n"
        "00:00:00,900 --> 00:00:01,700\n"
        "We are assembling a draft scaffold.\n",
        encoding="utf-8",
    )
    workflow, output_root, _ = _build_workflow(
        tmp_path,
        workflow_tag="authoritative_srt",
        media_source=MediaSource(kind=MediaSourceKind.LOCAL_SRT, locator=str(srt_path)),
        media_understanding_pipeline=MediaUnderstandingPipeline(LocalSRTProvider()),
    )

    result = workflow.run()
    state_manager = StateManager(str(output_root / "project_state.json"))
    ingestion_stage = state_manager.get_stage("ingestion")
    media_stage = state_manager.get_stage("media_understanding")

    assert Path(result.draft_content_path).exists()
    assert ingestion_stage is not None
    assert ingestion_stage["payload"]["execution_mode"] == "source_reference"
    assert ingestion_stage["payload"]["artifacts"]["kind"] == "media_source"
    assert media_stage is not None
    assert EXPECTED_MEDIA_AUDIT_FIELDS.issubset(media_stage["payload"])
    assert media_stage["payload"]["provider_name"] == "local_srt"
    assert media_stage["payload"]["provider_mode"] == "local_srt"
    assert media_stage["payload"]["extraction_version_context"] == {}
    assert media_stage["payload"]["execution_mode"] == "provider_run"
    assert media_stage["payload"]["source_kind"] == MediaSourceKind.LOCAL_SRT.value
    assert media_stage["payload"]["source_path"] == str(srt_path)
    assert media_stage["payload"]["error_type"] is None
    assert media_stage["payload"]["authoritative_input_used"] is True
    assert media_stage["payload"]["authoritative_path_kind"] == MediaSourceKind.LOCAL_SRT.value
    assert media_stage["payload"]["authoritative_flow"] == (
        "local_srt -> attributed_transcript -> subtitle_line_bridge"
    )
    assert media_stage["payload"]["transcript_extraction_used"] is False
    assert media_stage["payload"]["attributed_transcript_normalized"] is True
    assert media_stage["payload"]["subtitle_line_bridge_applied"] is True
    assert media_stage["payload"]["fallback_applied"] is False


def test_project_workflow_runs_through_authoritative_local_audio_asr_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "authoritative_audio.wav"
    _write_dummy_wav(audio_path)

    def fake_backend(
        self: SystemSpeechLocalASRTranscriptExtractionProvider,
        request: TranscriptExtractionRequest,
    ) -> TranscriptExtractionResult:
        del self
        return TranscriptExtractionResult(
            request=request,
            transcript_lines=[
                TranscriptLine(index=1, start_ms=0, end_ms=800, source_text="Welcome back."),
                TranscriptLine(index=2, start_ms=900, end_ms=1_700, source_text="We are assembling a draft scaffold."),
            ],
            provider_name="system_speech_local_asr",
            provider_mode="real",
            version_context={
                "provider_variant": "system_speech_local_asr_v1",
                "model_name": "system_speech_dictation",
                "language": "en-US",
                "task": "transcribe",
            },
        )

    monkeypatch.setattr(SystemSpeechLocalASRTranscriptExtractionProvider, "_run_backend", fake_backend)
    workflow, output_root, _ = _build_workflow(
        tmp_path,
        workflow_tag="authoritative_local_audio_asr",
        media_source=MediaSource(kind=MediaSourceKind.LOCAL_AUDIO, locator=str(audio_path)),
        media_understanding_pipeline=MediaUnderstandingPipeline.from_selection(
            MediaUnderstandingProviderSelectionConfig(mode="local_asr")
        ),
    )

    result = workflow.run()
    state_manager = StateManager(str(output_root / "project_state.json"))
    media_stage = state_manager.get_stage("media_understanding")
    translation_stage = state_manager.get_stage("translation")
    alignment_stage = state_manager.get_stage("alignment")

    assert Path(result.draft_content_path).exists()
    assert result.block_count == 1
    assert result.caption_count == 2
    assert result.material_count == 1
    assert media_stage is not None
    assert EXPECTED_MEDIA_AUDIT_FIELDS.issubset(media_stage["payload"])
    assert media_stage["payload"]["provider_name"] == "transcript_extraction_adapter"
    assert media_stage["payload"]["provider_mode"] == "transcript_extraction"
    assert media_stage["payload"]["extraction_provider_name"] == "system_speech_local_asr"
    assert media_stage["payload"]["extraction_provider_mode"] == "local_asr"
    assert media_stage["payload"]["extraction_version_context"]["provider_variant"] == "system_speech_local_asr_v1"
    assert media_stage["payload"]["source_kind"] == MediaSourceKind.LOCAL_AUDIO.value
    assert media_stage["payload"]["execution_mode"] == "provider_run"
    assert media_stage["payload"]["error_type"] is None
    assert media_stage["payload"]["authoritative_input_used"] is True
    assert media_stage["payload"]["authoritative_path_kind"] == MediaSourceKind.LOCAL_AUDIO.value
    assert media_stage["payload"]["authoritative_flow"] == (
        "local_audio -> transcript_extraction -> attributed_transcript -> subtitle_line_bridge"
    )
    assert media_stage["payload"]["transcript_extraction_used"] is True
    assert media_stage["payload"]["attributed_transcript_normalized"] is True
    assert media_stage["payload"]["subtitle_line_bridge_applied"] is True
    assert translation_stage is not None
    assert translation_stage["status"] == StageStatus.DONE.value
    assert translation_stage["payload"]["provider_mode"] == "mock"
    assert alignment_stage is not None
    assert alignment_stage["status"] == StageStatus.DONE.value
    assert alignment_stage["payload"]["provider_mode"] == "mock"


def test_project_workflow_run_build_registers_local_audio_source_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "authoritative_audio_build.wav"
    _write_dummy_wav(audio_path)

    def fake_backend(
        self: SystemSpeechLocalASRTranscriptExtractionProvider,
        request: TranscriptExtractionRequest,
    ) -> TranscriptExtractionResult:
        del self
        return TranscriptExtractionResult(
            request=request,
            transcript_lines=[
                TranscriptLine(index=1, start_ms=0, end_ms=800, source_text="Welcome back."),
                TranscriptLine(index=2, start_ms=900, end_ms=1_700, source_text="We are assembling a draft scaffold."),
            ],
            provider_name="system_speech_local_asr",
            provider_mode="real",
            version_context={
                "provider_variant": "system_speech_local_asr_v1",
                "model_name": "system_speech_dictation",
                "language": "en-US",
                "task": "transcribe",
            },
        )

    monkeypatch.setattr(SystemSpeechLocalASRTranscriptExtractionProvider, "_run_backend", fake_backend)
    workflow, _, _ = _build_workflow(
        tmp_path,
        workflow_tag="authoritative_local_audio_build",
        media_source=MediaSource(kind=MediaSourceKind.LOCAL_AUDIO, locator=str(audio_path)),
        media_understanding_pipeline=MediaUnderstandingPipeline.from_selection(
            MediaUnderstandingProviderSelectionConfig(mode="local_asr")
        ),
    )

    result = workflow.run_build()

    audio_preparation_stage = result.stage_snapshot["audio_preparation"]

    assert audio_preparation_stage["status"] == StageStatus.DONE.value
    assert audio_preparation_stage["payload"]["execution_mode"] == "fresh_prepare"
    assert Path(audio_preparation_stage["payload"]["speech_audio_path"]).exists()
    assert Path(audio_preparation_stage["payload"]["ambient_audio_path"]).exists()
    assert result.localized_project.source_info["source_kind"] == MediaSourceKind.LOCAL_AUDIO.value
    assert result.localized_project.source_info["source_path"] == str(audio_path)
    assert result.artifact_index.require("source.original_audio") == str(audio_path)
    assert Path(result.artifact_index.require("working.speech_for_asr")).name == "speech_for_asr.wav"
    assert Path(result.artifact_index.require("working.ambient_audio")).name == "ambient.wav"


def test_project_workflow_runs_through_authoritative_local_audio_asr_path_with_real_translation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "authoritative_audio_real_translation.wav"
    _write_dummy_wav(audio_path)

    def fake_backend(
        self: SystemSpeechLocalASRTranscriptExtractionProvider,
        request: TranscriptExtractionRequest,
    ) -> TranscriptExtractionResult:
        del self
        return TranscriptExtractionResult(
            request=request,
            transcript_lines=[
                TranscriptLine(index=1, start_ms=0, end_ms=800, source_text="Welcome back."),
                TranscriptLine(index=2, start_ms=900, end_ms=1_700, source_text="We are assembling a draft scaffold."),
            ],
            provider_name="system_speech_local_asr",
            provider_mode="real",
            version_context={
                "provider_variant": "system_speech_local_asr_v1",
                "model_name": "system_speech_dictation",
                "language": "en-US",
                "task": "transcribe",
            },
        )

    monkeypatch.setattr(SystemSpeechLocalASRTranscriptExtractionProvider, "_run_backend", fake_backend)
    workflow, output_root, _ = _build_workflow(
        tmp_path,
        translation_kind="real",
        tts_kind="mock",
        workflow_tag="audio_asr_real_tr",
        media_source=MediaSource(kind=MediaSourceKind.LOCAL_AUDIO, locator=str(audio_path)),
        media_understanding_pipeline=MediaUnderstandingPipeline.from_selection(
            MediaUnderstandingProviderSelectionConfig(mode="local_asr")
        ),
    )

    result = workflow.run()
    state_manager = StateManager(str(output_root / "project_state.json"))
    media_stage = state_manager.get_stage("media_understanding")
    translation_stage = state_manager.get_stage("translation")
    alignment_stage = state_manager.get_stage("alignment")

    assert Path(result.draft_content_path).exists()
    assert result.block_count == 1
    assert result.caption_count == 2
    assert result.material_count == 1
    assert media_stage is not None
    assert media_stage["payload"]["provider_name"] == "transcript_extraction_adapter"
    assert media_stage["payload"]["extraction_provider_name"] == "system_speech_local_asr"
    assert media_stage["payload"]["source_kind"] == MediaSourceKind.LOCAL_AUDIO.value
    assert media_stage["payload"]["authoritative_input_used"] is True
    assert media_stage["payload"]["authoritative_path_kind"] == MediaSourceKind.LOCAL_AUDIO.value
    assert media_stage["payload"]["transcript_extraction_used"] is True
    assert translation_stage is not None
    assert translation_stage["status"] == StageStatus.DONE.value
    assert translation_stage["payload"]["provider_name"] == "openai_compatible"
    assert translation_stage["payload"]["provider_mode"] == "real"
    assert translation_stage["payload"]["model_name"] == "fake-translation-model"
    assert alignment_stage is not None
    assert alignment_stage["status"] == StageStatus.DONE.value
    assert alignment_stage["payload"]["provider_mode"] == "mock"


def test_project_workflow_records_local_video_media_understanding_failure_audit(tmp_path: Path) -> None:
    video_path = tmp_path / "authoritative_video.mp4"
    video_path.write_bytes(b"fake-video")
    workflow, output_root, _ = _build_workflow(
        tmp_path,
        workflow_tag="authoritative_local_video",
        media_source=MediaSource(kind=MediaSourceKind.LOCAL_VIDEO, locator=str(video_path)),
        media_understanding_pipeline=MediaUnderstandingPipeline.from_selection(
            MediaUnderstandingProviderSelectionConfig(mode="multimodal_skeleton")
        ),
    )

    with pytest.raises(WorkflowError, match="Media understanding stage failed"):
        workflow.run()

    state_manager = StateManager(str(output_root / "project_state.json"))
    audio_preparation_stage = state_manager.get_stage("audio_preparation")
    media_stage = state_manager.get_stage("media_understanding")

    assert audio_preparation_stage is not None
    assert audio_preparation_stage["status"] == StageStatus.DONE.value
    assert audio_preparation_stage["payload"]["execution_mode"] == "deferred_local_video"
    assert audio_preparation_stage["payload"]["skipped"] is True
    assert media_stage is not None
    assert media_stage["status"] == StageStatus.FAILED.value
    assert EXPECTED_MEDIA_AUDIT_FIELDS.issubset(media_stage["payload"])
    assert media_stage["payload"]["provider_name"] == "transcript_extraction_adapter"
    assert media_stage["payload"]["provider_mode"] == "transcript_extraction"
    assert media_stage["payload"]["extraction_provider_name"] == "gemini_like_multimodal_extraction"
    assert media_stage["payload"]["extraction_provider_mode"] == "multimodal_skeleton"
    assert media_stage["payload"]["extraction_version_context"]["provider_variant"] == (
        "future_multimodal_transcript_extraction_v1"
    )
    assert media_stage["payload"]["source_kind"] == MediaSourceKind.LOCAL_VIDEO.value
    assert media_stage["payload"]["source_path"] == str(video_path)
    assert media_stage["payload"]["execution_mode"] == "failed"
    assert media_stage["payload"]["error_type"] == "transcript_extraction_unavailable"
    assert media_stage["payload"]["authoritative_input_used"] is True
    assert media_stage["payload"]["authoritative_path_kind"] == MediaSourceKind.LOCAL_VIDEO.value
    assert media_stage["payload"]["authoritative_flow"] == (
        "local_video -> transcript_extraction -> attributed_transcript -> subtitle_line_bridge"
    )
    assert media_stage["payload"]["transcript_extraction_used"] is True
    assert media_stage["payload"]["attributed_transcript_normalized"] is False
    assert media_stage["payload"]["subtitle_line_bridge_applied"] is False


def test_project_workflow_records_local_audio_invalid_path_failure_audit(tmp_path: Path) -> None:
    audio_path = tmp_path / "missing_audio.wav"
    workflow, output_root, _ = _build_workflow(
        tmp_path,
        workflow_tag="authoritative_local_audio_missing",
        media_source=MediaSource(kind=MediaSourceKind.LOCAL_AUDIO, locator=str(audio_path)),
        media_understanding_pipeline=MediaUnderstandingPipeline.from_selection(
            MediaUnderstandingProviderSelectionConfig(mode="multimodal_skeleton")
        ),
    )

    with pytest.raises(WorkflowError, match="Media understanding stage failed"):
        workflow.run()

    state_manager = StateManager(str(output_root / "project_state.json"))
    audio_preparation_stage = state_manager.get_stage("audio_preparation")
    media_stage = state_manager.get_stage("media_understanding")

    assert audio_preparation_stage is not None
    assert audio_preparation_stage["status"] == StageStatus.DONE.value
    assert audio_preparation_stage["payload"]["execution_mode"] == "skipped_missing_source"
    assert audio_preparation_stage["payload"]["skipped"] is True
    assert media_stage is not None
    assert media_stage["status"] == StageStatus.FAILED.value
    assert media_stage["payload"]["provider_name"] == "transcript_extraction_adapter"
    assert media_stage["payload"]["provider_mode"] == "transcript_extraction"
    assert media_stage["payload"]["extraction_provider_name"] == "gemini_like_multimodal_extraction"
    assert media_stage["payload"]["extraction_provider_mode"] == "multimodal_skeleton"
    assert media_stage["payload"]["extraction_version_context"]["provider_variant"] == (
        "future_multimodal_transcript_extraction_v1"
    )
    assert media_stage["payload"]["source_kind"] == MediaSourceKind.LOCAL_AUDIO.value
    assert media_stage["payload"]["source_path"] == str(audio_path)
    assert media_stage["payload"]["execution_mode"] == "failed"
    assert media_stage["payload"]["error_type"] == "invalid_source_path"
    assert media_stage["payload"]["authoritative_input_used"] is True
    assert media_stage["payload"]["authoritative_path_kind"] == MediaSourceKind.LOCAL_AUDIO.value
    assert media_stage["payload"]["authoritative_flow"] == (
        "local_audio -> transcript_extraction -> attributed_transcript -> subtitle_line_bridge"
    )
    assert media_stage["payload"]["transcript_extraction_used"] is True
    assert media_stage["payload"]["attributed_transcript_normalized"] is False
    assert media_stage["payload"]["subtitle_line_bridge_applied"] is False


def test_project_workflow_media_understanding_failure_does_not_create_downstream_stage_state(tmp_path: Path) -> None:
    video_path = tmp_path / "authoritative_video.mp4"
    video_path.write_bytes(b"fake-video")
    workflow, output_root, _ = _build_workflow(
        tmp_path,
        workflow_tag="authoritative_local_video_downstream_guard",
        media_source=MediaSource(kind=MediaSourceKind.LOCAL_VIDEO, locator=str(video_path)),
        media_understanding_pipeline=MediaUnderstandingPipeline.from_selection(
            MediaUnderstandingProviderSelectionConfig(mode="multimodal_skeleton")
        ),
    )

    with pytest.raises(WorkflowError, match="Media understanding stage failed"):
        workflow.run()

    state_manager = StateManager(str(output_root / "project_state.json"))

    assert state_manager.get_stage("ingestion") is not None
    assert state_manager.get_stage("media_understanding") is not None
    assert state_manager.get_stage("translation") is None
    assert state_manager.get_stage("chunking") is None
    assert state_manager.get_stage("alignment") is None
    assert state_manager.get_stage("draft") is None


def test_project_workflow_supports_lightweight_draft_resume(tmp_path: Path) -> None:
    workflow, output_root, _ = _build_workflow(tmp_path, translation_kind="mock", tts_kind="mock")
    first_result = workflow.run()

    resumed_workflow, _, _ = _build_workflow(tmp_path, translation_kind="mock", tts_kind="mock")
    second_result = resumed_workflow.run()
    state_manager = StateManager(str(output_root / "project_state.json"))
    state = state_manager.load()

    assert first_result.draft_content_path == second_result.draft_content_path
    assert state["stages"]["translation"]["payload"]["cache_hit_batches"] >= 1
    assert state["stages"]["alignment"]["payload"]["cache_hit_blocks"] >= 1
    assert state["stages"]["draft"]["payload"]["execution_mode"] == "reuse_existing_artifacts"
    assert state["stages"]["draft"]["payload"]["skipped"] is True
    assert state["stages"]["draft"]["payload"]["restore_reason"] == "existing_draft_artifacts_valid"
    assert state["stages"]["draft"]["payload"]["rerun_reason"] is None
    assert state["stages"]["draft"]["payload"]["reused_artifacts"] == state["stages"]["draft"]["payload"]["artifact_paths"]


@pytest.mark.parametrize(
    ("translation_kind", "tts_kind"),
    [
        ("mock", "mock"),
        ("real", "mock"),
        ("mock", "real"),
        ("real", "real"),
    ],
    ids=["mock_mock", "real_mock", "mock_real", "real_real"],
)
def test_project_workflow_covers_provider_mode_combinations(
    tmp_path: Path,
    translation_kind: str,
    tts_kind: str,
) -> None:
    workflow, _, expected = _build_workflow(tmp_path, translation_kind=translation_kind, tts_kind=tts_kind)

    result = workflow.run()
    translation_payload = _read_stage_payload(result.stage_snapshot, "translation")
    alignment_payload = _read_stage_payload(result.stage_snapshot, "alignment")
    draft_payload = _read_stage_payload(result.stage_snapshot, "draft")

    assert Path(result.draft_content_path).exists()
    _assert_provider_run_report(
        translation_payload,
        expected["translation"],
        execution_mode="fresh_run",
        stage_name="translation",
    )
    _assert_provider_run_report(
        alignment_payload,
        expected["alignment"],
        execution_mode="fresh_run",
        stage_name="alignment",
    )
    assert draft_payload["execution_mode"] == "fresh_write"
    assert draft_payload["skipped"] is False


@pytest.mark.parametrize(
    ("translation_kind", "tts_kind"),
    [
        ("mock", "mock"),
        ("real", "mock"),
        ("mock", "real"),
        ("real", "real"),
    ],
    ids=["mock_mock", "real_mock", "mock_real", "real_real"],
)
def test_project_workflow_cache_execution_modes_remain_consistent_across_provider_modes(
    tmp_path: Path,
    translation_kind: str,
    tts_kind: str,
) -> None:
    first_workflow, _, expected = _build_workflow(tmp_path, translation_kind=translation_kind, tts_kind=tts_kind)
    first_result = first_workflow.run()

    second_workflow, _, _ = _build_workflow(tmp_path, translation_kind=translation_kind, tts_kind=tts_kind)
    second_result = second_workflow.run()

    first_translation_payload = _read_stage_payload(first_result.stage_snapshot, "translation")
    first_alignment_payload = _read_stage_payload(first_result.stage_snapshot, "alignment")
    second_translation_payload = _read_stage_payload(second_result.stage_snapshot, "translation")
    second_alignment_payload = _read_stage_payload(second_result.stage_snapshot, "alignment")
    second_draft_payload = _read_stage_payload(second_result.stage_snapshot, "draft")

    _assert_provider_run_report(
        first_translation_payload,
        expected["translation"],
        execution_mode="fresh_run",
        stage_name="translation",
    )
    _assert_provider_run_report(
        first_alignment_payload,
        expected["alignment"],
        execution_mode="fresh_run",
        stage_name="alignment",
    )
    _assert_provider_run_report(
        second_translation_payload,
        expected["translation"],
        execution_mode="cache_restore_full",
        stage_name="translation",
    )
    _assert_provider_run_report(
        second_alignment_payload,
        expected["alignment"],
        execution_mode="cache_restore_full",
        stage_name="alignment",
    )
    assert second_translation_payload["cache_hit_batches"] >= 1
    assert second_alignment_payload["cache_hit_blocks"] >= 1
    assert second_draft_payload["execution_mode"] == "reuse_existing_artifacts"
    assert second_draft_payload["skipped"] is True
    assert second_draft_payload["restore_reason"] == "existing_draft_artifacts_valid"
    assert second_draft_payload["rerun_reason"] is None


def test_project_workflow_reruns_downstream_stages_when_input_hash_changes(tmp_path: Path) -> None:
    first_workflow, _, _ = _build_workflow(tmp_path, workflow_tag="input_change")
    first_result = first_workflow.run()

    updated_workflow, _, _ = _build_workflow(
        tmp_path,
        workflow_tag="input_change",
        subtitle_seeds=[
            SubtitleSeed(
                index=1,
                start_ms=0,
                end_ms=800,
                en_text="Welcome back.",
                speaker_id="speaker_host",
                speaker_name="Host",
            ),
            SubtitleSeed(
                index=2,
                start_ms=900,
                end_ms=1_700,
                en_text="We are assembling a revised draft scaffold.",
                speaker_id="speaker_host",
                speaker_name="Host",
            ),
        ],
    )
    second_result = updated_workflow.run()

    first_translation_payload = _read_stage_payload(first_result.stage_snapshot, "translation")
    second_translation_payload = _read_stage_payload(second_result.stage_snapshot, "translation")
    second_alignment_payload = _read_stage_payload(second_result.stage_snapshot, "alignment")
    second_draft_payload = _read_stage_payload(second_result.stage_snapshot, "draft")

    assert first_translation_payload["source_input_hash"] != second_translation_payload["source_input_hash"]
    assert second_translation_payload["execution_mode"] == "fresh_run"
    assert second_translation_payload["rerun_reason"] == "source_input_hash_changed"
    assert second_alignment_payload["execution_mode"] == "fresh_run"
    assert second_alignment_payload["rerun_reason"] == "source_input_hash_changed"
    assert second_draft_payload["execution_mode"] == "fresh_write"
    assert second_draft_payload["rerun_reason"] == "source_input_hash_changed"


def test_project_workflow_invalidates_translation_cache_when_provider_context_changes(tmp_path: Path) -> None:
    first_workflow, _, _ = _build_workflow(
        tmp_path,
        translation_kind="real",
        tts_kind="mock",
        workflow_tag="translation_context_change",
        translation_audit_overrides={
            "model_name": "fake-translation-model-v1",
            "version_context": {"api_protocol": "chat_completions_v1", "provider_variant": "variant_a"},
        },
    )
    first_workflow.run()

    second_workflow, _, _ = _build_workflow(
        tmp_path,
        translation_kind="real",
        tts_kind="mock",
        workflow_tag="translation_context_change",
        translation_audit_overrides={
            "model_name": "fake-translation-model-v2",
            "version_context": {"api_protocol": "chat_completions_v1", "provider_variant": "variant_b"},
        },
    )
    second_result = second_workflow.run()

    translation_payload = _read_stage_payload(second_result.stage_snapshot, "translation")
    alignment_payload = _read_stage_payload(second_result.stage_snapshot, "alignment")
    draft_payload = _read_stage_payload(second_result.stage_snapshot, "draft")

    assert translation_payload["execution_mode"] == "fresh_run"
    assert translation_payload["rerun_reason"] == "provider_context_changed"
    assert translation_payload["cache_hit_batches"] == 0
    assert alignment_payload["execution_mode"] == "cache_restore_full"
    assert draft_payload["execution_mode"] == "fresh_write"
    assert draft_payload["rerun_reason"] == "upstream_translation_not_reusable"


def test_project_workflow_invalidates_alignment_cache_when_tts_context_changes(tmp_path: Path) -> None:
    first_workflow, _, _ = _build_workflow(
        tmp_path,
        translation_kind="mock",
        tts_kind="real",
        workflow_tag="tts_context_change",
        alignment_audit_overrides={
            "model_name": "fake-tts-model-v1",
            "version_context": {"api_protocol": "audio_speech_v1", "provider_variant": "variant_a"},
        },
    )
    first_workflow.run()

    second_workflow, _, _ = _build_workflow(
        tmp_path,
        translation_kind="mock",
        tts_kind="real",
        workflow_tag="tts_context_change",
        alignment_audit_overrides={
            "model_name": "fake-tts-model-v2",
            "version_context": {"api_protocol": "audio_speech_v1", "provider_variant": "variant_b"},
        },
    )
    second_result = second_workflow.run()

    translation_payload = _read_stage_payload(second_result.stage_snapshot, "translation")
    alignment_payload = _read_stage_payload(second_result.stage_snapshot, "alignment")
    draft_payload = _read_stage_payload(second_result.stage_snapshot, "draft")

    assert translation_payload["execution_mode"] == "cache_restore_full"
    assert alignment_payload["execution_mode"] == "fresh_run"
    assert alignment_payload["rerun_reason"] == "provider_context_changed"
    assert alignment_payload["cache_hit_blocks"] == 0
    assert draft_payload["execution_mode"] == "fresh_write"
    assert draft_payload["rerun_reason"] == "upstream_alignment_not_reusable"
