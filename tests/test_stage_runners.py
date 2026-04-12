from pathlib import Path

import pytest

from core.enums import StageStatus
from core.exceptions import TTSProviderTimeoutError, TranslationProviderUnavailableError, WorkflowError
from core.models import SemanticBlock, SubtitleLine
from modules.alignment.alignment_orchestrator import AlignmentConfig, AlignmentOrchestrator
from modules.alignment.dsp_stretcher import DSPStretcher
from modules.alignment.rewrite_engine import RewriteEngine
from modules.chunking.semantic_block_builder import SemanticBlockBuilder
from modules.draft.caption_retiming import CaptionRetimer
from modules.draft.draft_writer import DraftWriter
from modules.translation.router import TranslationChunkRouter, TranslationRouterConfig
from modules.translation.translator import MockTranslator, TranslationPipeline
from modules.workflow.alignment_stage_runner import AlignmentStageRunner, AlignmentStageRunnerConfig
from modules.workflow.draft_stage_runner import DraftStageRunner, DraftStageRunnerConfig
from modules.workflow.translation_stage_runner import (
    TranslationStageRunner,
    TranslationStageRunnerConfig,
)
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


class FakeRealTranslator:
    def translate_batch(self, lines: list[SubtitleLine]) -> list[str]:
        return [f"```text\nREAL:{line.en_text.strip()}\n```" for line in lines]

    def get_cache_context(self) -> dict[str, object]:
        return {"provider_mode": "real", "api_protocol": "chat_completions_v1"}


class UnavailableRealTranslator:
    def translate_batch(self, lines: list[SubtitleLine]) -> list[str]:
        del lines
        raise TranslationProviderUnavailableError("translation provider unavailable")


class RetryingAuditTranslator:
    def __init__(self) -> None:
        self._retry_report = {
            "retry_attempted": False,
            "retry_count": 0,
            "retry_candidate": None,
            "final_error_type": None,
            "final_error_message": None,
        }

    def translate_batch(self, lines: list[SubtitleLine]) -> list[str]:
        self._retry_report = {
            "retry_attempted": True,
            "retry_count": 2,
            "retry_candidate": True,
            "final_error_type": None,
            "final_error_message": None,
        }
        return [f"CN_RETRY:{line.en_text.strip()}" for line in lines]

    def get_retry_report(self) -> dict[str, object]:
        return dict(self._retry_report)

    def reset_retry_report(self) -> None:
        self._retry_report = {
            "retry_attempted": False,
            "retry_count": 0,
            "retry_candidate": None,
            "final_error_type": None,
            "final_error_message": None,
        }


class TimeoutingRealTTSProvider:
    def __init__(self) -> None:
        self._retry_report = {
            "retry_attempted": False,
            "retry_count": 0,
            "retry_candidate": None,
            "final_error_type": None,
            "final_error_message": None,
        }

    def synthesize(self, block: SemanticBlock) -> str:
        del block
        self._retry_report = {
            "retry_attempted": True,
            "retry_count": 2,
            "retry_candidate": True,
            "final_error_type": "provider_timeout",
            "final_error_message": "real tts provider timed out",
        }
        raise TTSProviderTimeoutError("real tts provider timed out")

    def get_retry_report(self) -> dict[str, object]:
        return dict(self._retry_report)

    def reset_retry_report(self) -> None:
        self._retry_report = {
            "retry_attempted": False,
            "retry_count": 0,
            "retry_candidate": None,
            "final_error_type": None,
            "final_error_message": None,
        }


class RetryingAuditTTSProvider:
    def __init__(self, output_dir: str) -> None:
        self.mock_tts = MockTTSService(output_dir=output_dir, config=MockTTSConfig(ms_per_char=80, min_duration_ms=800))
        self._retry_report = {
            "retry_attempted": False,
            "retry_count": 0,
            "retry_candidate": None,
            "final_error_type": None,
            "final_error_message": None,
        }

    def synthesize(self, block: SemanticBlock) -> str:
        self._retry_report = {
            "retry_attempted": True,
            "retry_count": 1,
            "retry_candidate": True,
            "final_error_type": None,
            "final_error_message": None,
        }
        return self.mock_tts.synthesize(block)

    def get_retry_report(self) -> dict[str, object]:
        return dict(self._retry_report)

    def reset_retry_report(self) -> None:
        self._retry_report = {
            "retry_attempted": False,
            "retry_count": 0,
            "retry_candidate": None,
            "final_error_type": None,
            "final_error_message": None,
        }


def _make_source_lines() -> list[SubtitleLine]:
    return [
        SubtitleLine(
            index=1,
            start_ms=0,
            end_ms=800,
            speaker_id="speaker_host",
            speaker_name="Host",
            en_text="Welcome back.",
            cn_text="",
        ),
        SubtitleLine(
            index=2,
            start_ms=900,
            end_ms=1_700,
            speaker_id="speaker_host",
            speaker_name="Host",
            en_text="We are assembling a draft scaffold.",
            cn_text="",
        ),
    ]


def test_translation_stage_runner_matches_existing_behavior(tmp_path: Path) -> None:
    state_manager = StateManager(str(tmp_path / "project_state.json"))
    cache_manager = CacheManager(str(tmp_path / "project_cache.json"))
    runner = TranslationStageRunner(
        translation_pipeline=TranslationPipeline(
            router=TranslationChunkRouter(TranslationRouterConfig(batch_size=2, max_batch_size=4)),
            translator=MockTranslator(),
        ),
        state_manager=state_manager,
        cache_manager=cache_manager,
        config=TranslationStageRunnerConfig(provider_name="mock_translator"),
    )

    translated_lines = runner.run(_make_source_lines())
    translation_stage = state_manager.get_stage("translation")

    assert [line.cn_text for line in translated_lines] == [
        "CN:Welcome back.",
        "CN:We are assembling a draft scaffold.",
    ]
    assert translation_stage is not None
    assert translation_stage["status"] == StageStatus.DONE.value
    assert translation_stage["payload"]["cache_hit_batches"] == 0
    assert translation_stage["payload"]["execution_mode"] == "fresh_run"
    assert EXPECTED_PROVIDER_REPORT_FIELDS.issubset(translation_stage["payload"])
    assert translation_stage["payload"]["provider_name"] == "mock_translator"
    assert translation_stage["payload"]["fallback_trigger"] is None
    assert translation_stage["payload"]["retry_attempted"] is False
    assert translation_stage["payload"]["retry_count"] == 0
    assert translation_stage["payload"]["error_type"] is None
    assert translation_stage["payload"]["retry_candidate"] is None
    assert translation_stage["payload"]["final_error_type"] is None
    assert translation_stage["payload"]["final_error_message"] is None
    assert translation_stage["payload"]["text_layer_summary"] == {
        "cn_line_count": 2,
    }
    assert translation_stage["payload"]["artifact_paths"] == []
    assert translation_stage["payload"]["reused_artifacts"] == []
    assert translation_stage["payload"]["restore_reason"] is None
    assert translation_stage["payload"]["rerun_reason"] == "translation_cache_miss"
    assert translation_stage["payload"]["source_input_hash"] is not None


def test_translation_stage_runner_supports_real_provider_context(tmp_path: Path) -> None:
    state_manager = StateManager(str(tmp_path / "project_state.json"))
    cache_manager = CacheManager(str(tmp_path / "project_cache.json"))
    runner = TranslationStageRunner(
        translation_pipeline=TranslationPipeline(
            router=TranslationChunkRouter(TranslationRouterConfig(batch_size=2, max_batch_size=4)),
            translator=FakeRealTranslator(),
        ),
        state_manager=state_manager,
        cache_manager=cache_manager,
        config=TranslationStageRunnerConfig(
            provider_name="openai_compatible",
            target_language="zh-CN",
            model_name="demo-model",
            provider_mode="real",
            version_context={"api_protocol": "chat_completions_v1"},
        ),
    )

    translated_lines = runner.run(_make_source_lines())
    translation_stage = state_manager.get_stage("translation")

    assert [line.cn_text for line in translated_lines] == [
        "REAL:Welcome back.",
        "REAL:We are assembling a draft scaffold.",
    ]
    assert [line.cn_text for line in translated_lines] == [
        "REAL:Welcome back.",
        "REAL:We are assembling a draft scaffold.",
    ]
    assert translation_stage is not None
    assert translation_stage["payload"]["provider_mode"] == "real"
    assert translation_stage["payload"]["model_name"] == "demo-model"
    assert translation_stage["payload"]["sanitizer_summary"]["sanitized_line_count"] == 2
    assert translation_stage["payload"]["sanitizer_summary"]["action_counts"]["strip_code_fence"] == 2
    assert translation_stage["payload"]["retry_attempted"] is False
    assert translation_stage["payload"]["retry_count"] == 0
    assert translation_stage["payload"]["final_error_type"] is None
    assert translation_stage["payload"]["final_error_message"] is None
    assert translation_stage["payload"]["rerun_reason"] == "translation_cache_miss"


def test_translation_stage_runner_records_retry_audit_fields_after_provider_recovery(tmp_path: Path) -> None:
    state_manager = StateManager(str(tmp_path / "project_state.json"))
    cache_manager = CacheManager(str(tmp_path / "project_cache.json"))
    runner = TranslationStageRunner(
        translation_pipeline=TranslationPipeline(
            router=TranslationChunkRouter(TranslationRouterConfig(batch_size=2, max_batch_size=4)),
            translator=RetryingAuditTranslator(),
        ),
        state_manager=state_manager,
        cache_manager=cache_manager,
        config=TranslationStageRunnerConfig(
            provider_name="openai_compatible",
            target_language="zh-CN",
            model_name="demo-model",
            provider_mode="real",
            version_context={"api_protocol": "chat_completions_v1"},
        ),
    )

    translated_lines = runner.run(_make_source_lines())
    translation_stage = state_manager.get_stage("translation")

    assert [line.cn_text for line in translated_lines] == [
        "CN_RETRY:Welcome back.",
        "CN_RETRY:We are assembling a draft scaffold.",
    ]
    assert [line.cn_text for line in translated_lines] == [
        "CN_RETRY:Welcome back.",
        "CN_RETRY:We are assembling a draft scaffold.",
    ]
    assert translation_stage is not None
    assert translation_stage["status"] == StageStatus.DONE.value
    assert translation_stage["payload"]["retry_attempted"] is True
    assert translation_stage["payload"]["retry_count"] == 2
    assert translation_stage["payload"]["retry_candidate"] is True
    assert translation_stage["payload"]["final_error_type"] is None
    assert translation_stage["payload"]["final_error_message"] is None
    assert translation_stage["payload"]["rerun_reason"] == "translation_cache_miss"


def test_translation_stage_runner_runtime_fallback_is_explicit_and_auditable(tmp_path: Path) -> None:
    state_manager = StateManager(str(tmp_path / "project_state.json"))
    cache_manager = CacheManager(str(tmp_path / "project_cache.json"))
    runner = TranslationStageRunner(
        translation_pipeline=TranslationPipeline(
            router=TranslationChunkRouter(TranslationRouterConfig(batch_size=2, max_batch_size=4)),
            translator=UnavailableRealTranslator(),
            fallback_translator=MockTranslator(),
        ),
        state_manager=state_manager,
        cache_manager=cache_manager,
        config=TranslationStageRunnerConfig(
            provider_name="openai_compatible",
            target_language="zh-CN",
            model_name="demo-model",
            provider_mode="real",
            version_context={"api_protocol": "chat_completions_v1"},
            runtime_fallback_enabled=True,
            fallback_from="openai_compatible",
            fallback_to="mock_translator",
        ),
    )

    translated_lines = runner.run(_make_source_lines())
    translation_stage = state_manager.get_stage("translation")

    assert [line.cn_text for line in translated_lines] == [
        "CN:Welcome back.",
        "CN:We are assembling a draft scaffold.",
    ]
    assert [line.cn_text for line in translated_lines] == [
        "CN:Welcome back.",
        "CN:We are assembling a draft scaffold.",
    ]
    assert translation_stage is not None
    assert translation_stage["payload"]["fallback_applied"] is True
    assert translation_stage["payload"]["fallback_trigger"] == "runtime_provider_unavailable"
    assert translation_stage["payload"]["fallback_from"] == "openai_compatible"
    assert translation_stage["payload"]["fallback_to"] == "mock_translator"
    assert translation_stage["payload"]["runtime_fallback_batches"] == 1
    assert translation_stage["payload"]["rerun_reason"] == "translation_cache_miss"


def test_alignment_stage_runner_matches_existing_behavior(tmp_path: Path) -> None:
    state_manager = StateManager(str(tmp_path / "project_state.json"))
    state_manager.set_project("alignment_runner_demo")
    state_manager.set_stage("ingestion", StageStatus.DONE, {"input_hash": "alignment-source-hash"})
    cache_manager = CacheManager(str(tmp_path / "project_cache.json"))
    translated_lines = [
        SubtitleLine(
            index=1,
            start_ms=0,
            end_ms=800,
            speaker_id="speaker_host",
            speaker_name="Host",
            en_text="Welcome back.",
            cn_text="CN:Welcome back.",
        ),
        SubtitleLine(
            index=2,
            start_ms=900,
            end_ms=1_700,
            speaker_id="speaker_host",
            speaker_name="Host",
            en_text="We are assembling a draft scaffold.",
            cn_text="CN:We are assembling a draft scaffold.",
        ),
    ]
    blocks = SemanticBlockBuilder().build(translated_lines)
    runner = AlignmentStageRunner(
        alignment_orchestrator=AlignmentOrchestrator(
            tts_service=MockTTSService(
                output_dir=str(tmp_path / "audio"),
                config=MockTTSConfig(ms_per_char=80, min_duration_ms=800),
            ),
            audio_service=AudioService(),
            rewrite_engine=RewriteEngine(llm_service=MockLLMService()),
            dsp_stretcher=DSPStretcher(),
            config=AlignmentConfig(),
        ),
        state_manager=state_manager,
        cache_manager=cache_manager,
        config=AlignmentStageRunnerConfig(provider_name="mock_tts", voice_name="default"),
    )

    aligned_blocks = runner.run(blocks)
    alignment_stage = state_manager.get_stage("alignment")

    assert len(aligned_blocks) == 1
    assert aligned_blocks[0].status in {"align_done", "align_done_fallback"}
    assert alignment_stage is not None
    assert alignment_stage["status"] == StageStatus.DONE.value
    assert alignment_stage["payload"]["artifacts"]["file_count"] >= 1
    assert EXPECTED_PROVIDER_REPORT_FIELDS.issubset(alignment_stage["payload"])
    assert alignment_stage["payload"]["provider_name"] == "mock_tts"
    assert alignment_stage["payload"]["fallback_trigger"] is None
    assert alignment_stage["payload"]["fallback_from"] is None
    assert alignment_stage["payload"]["fallback_to"] is None
    assert alignment_stage["payload"]["retry_attempted"] is False
    assert alignment_stage["payload"]["retry_count"] == 0
    assert alignment_stage["payload"]["error_type"] is None
    assert alignment_stage["payload"]["retry_candidate"] is None
    assert alignment_stage["payload"]["final_error_type"] is None
    assert alignment_stage["payload"]["final_error_message"] is None
    assert alignment_stage["payload"]["artifact_paths"]
    assert alignment_stage["payload"]["reused_artifacts"] == []
    assert alignment_stage["payload"]["restore_reason"] is None
    assert alignment_stage["payload"]["rerun_reason"] == "alignment_cache_miss"
    assert alignment_stage["payload"]["source_input_hash"] == "alignment-source-hash"


def test_alignment_stage_runner_records_retry_audit_fields_after_provider_recovery(tmp_path: Path) -> None:
    state_manager = StateManager(str(tmp_path / "project_state.json"))
    state_manager.set_project("alignment_runner_retry")
    state_manager.set_stage("ingestion", StageStatus.DONE, {"input_hash": "alignment-source-hash"})
    cache_manager = CacheManager(str(tmp_path / "project_cache.json"))
    blocks = SemanticBlockBuilder().build(
        [
            SubtitleLine(
                index=1,
                start_ms=0,
                end_ms=800,
                speaker_id="speaker_host",
                speaker_name="Host",
                en_text="Welcome back.",
                cn_text="CN:Welcome back.",
            )
        ]
    )
    runner = AlignmentStageRunner(
        alignment_orchestrator=AlignmentOrchestrator(
            tts_service=RetryingAuditTTSProvider(output_dir=str(tmp_path / "audio")),
            audio_service=AudioService(),
            rewrite_engine=RewriteEngine(llm_service=MockLLMService()),
            dsp_stretcher=DSPStretcher(),
            config=AlignmentConfig(),
        ),
        state_manager=state_manager,
        cache_manager=cache_manager,
        config=AlignmentStageRunnerConfig(
            provider_name="openai_compatible_tts",
            voice_name="alloy",
            model_name="tts-model",
            provider_mode="real",
            version_context={"api_protocol": "audio_speech_v1"},
        ),
    )

    aligned_blocks = runner.run(blocks)
    alignment_stage = state_manager.get_stage("alignment")

    assert len(aligned_blocks) == 1
    assert alignment_stage is not None
    assert alignment_stage["status"] == StageStatus.DONE.value
    assert alignment_stage["payload"]["retry_attempted"] is True
    assert alignment_stage["payload"]["retry_count"] == 1
    assert alignment_stage["payload"]["retry_candidate"] is True
    assert alignment_stage["payload"]["final_error_type"] is None
    assert alignment_stage["payload"]["final_error_message"] is None
    assert alignment_stage["payload"]["rerun_reason"] == "alignment_cache_miss"


def test_alignment_stage_runner_records_real_tts_failure_audit_fields(tmp_path: Path) -> None:
    state_manager = StateManager(str(tmp_path / "project_state.json"))
    state_manager.set_project("alignment_runner_failure")
    state_manager.set_stage("ingestion", StageStatus.DONE, {"input_hash": "alignment-source-hash"})
    cache_manager = CacheManager(str(tmp_path / "project_cache.json"))
    blocks = SemanticBlockBuilder().build(
        [
            SubtitleLine(
                index=1,
                start_ms=0,
                end_ms=800,
                speaker_id="speaker_host",
                speaker_name="Host",
                en_text="Welcome back.",
                cn_text="CN:Welcome back.",
            )
        ]
    )
    runner = AlignmentStageRunner(
        alignment_orchestrator=AlignmentOrchestrator(
            tts_service=TimeoutingRealTTSProvider(),
            audio_service=AudioService(),
            rewrite_engine=RewriteEngine(llm_service=MockLLMService()),
            dsp_stretcher=DSPStretcher(),
            config=AlignmentConfig(),
        ),
        state_manager=state_manager,
        cache_manager=cache_manager,
        config=AlignmentStageRunnerConfig(
            provider_name="openai_compatible_tts",
            voice_name="alloy",
            model_name="tts-model",
            provider_mode="real",
            version_context={"api_protocol": "audio_speech_v1"},
        ),
    )

    with pytest.raises(WorkflowError, match="Alignment stage failed"):
        runner.run(blocks)

    alignment_stage = state_manager.get_stage("alignment")

    assert alignment_stage is not None
    assert alignment_stage["status"] == StageStatus.FAILED.value
    assert EXPECTED_PROVIDER_REPORT_FIELDS.issubset(alignment_stage["payload"])
    assert alignment_stage["payload"]["error_type"] == "provider_timeout"
    assert alignment_stage["payload"]["retry_attempted"] is True
    assert alignment_stage["payload"]["retry_count"] == 2
    assert alignment_stage["payload"]["retry_candidate"] is True
    assert alignment_stage["payload"]["final_error_type"] == "provider_timeout"
    assert alignment_stage["payload"]["final_error_message"] == "real tts provider timed out"
    assert alignment_stage["payload"]["provider_name"] == "openai_compatible_tts"
    assert alignment_stage["payload"]["provider_mode"] == "real"
    assert alignment_stage["payload"]["model_name"] == "tts-model"
    assert alignment_stage["payload"]["version_context"] == {"api_protocol": "audio_speech_v1"}
    assert alignment_stage["payload"]["fallback_applied"] is False
    assert alignment_stage["payload"]["fallback_reason"] is None
    assert alignment_stage["payload"]["fallback_trigger"] is None
    assert alignment_stage["payload"]["fallback_from"] is None
    assert alignment_stage["payload"]["fallback_to"] is None
    assert alignment_stage["payload"]["artifact_paths"] == []
    assert alignment_stage["payload"]["reused_artifacts"] == []
    assert alignment_stage["payload"]["restore_reason"] is None
    assert alignment_stage["payload"]["rerun_reason"] == "alignment_cache_miss"
    assert alignment_stage["payload"]["source_input_hash"] == "alignment-source-hash"


def test_draft_stage_runner_matches_existing_behavior_and_resume(tmp_path: Path) -> None:
    state_manager = StateManager(str(tmp_path / "project_state.json"))
    state_manager.set_project("draft_runner_demo")
    state_manager.set_stage("ingestion", StageStatus.DONE, {"input_hash": "demo-input-hash"})
    source_audio_path = tmp_path / "aligned_block.wav"
    source_audio_path.write_bytes(b"RIFFdraft")

    translated_lines = [
        SubtitleLine(1, 0, 800, "speaker_host", "Host", "Welcome back.", "CN:Welcome"),
        SubtitleLine(2, 900, 1_700, "speaker_host", "Host", "Draft scaffold.", "back"),
    ]
    aligned_blocks = [
        SemanticBlock(
            block_id="block_0001",
            speaker_id="speaker_host",
            speaker_name="Host",
            original_srt_indices=[1, 2],
            first_start_ms=0,
            last_end_ms=1_700,
            target_duration_ms=1_700,
            merged_cn_text="CN:Welcomeback",
            actual_audio_duration_ms=1_700,
            aligned_audio_path=str(source_audio_path),
            final_cn_lines=["CN:Welcome", "back"],
        )
    ]
    runner = DraftStageRunner(
        caption_retimer=CaptionRetimer(),
        draft_writer=DraftWriter(output_root_dir=str(tmp_path / "output")),
        state_manager=state_manager,
        config=DraftStageRunnerConfig(project_id="draft_runner_demo"),
    )

    first_result = runner.run(translated_lines, aligned_blocks)
    first_stage = state_manager.get_stage("draft")
    second_result = runner.run(translated_lines, aligned_blocks)
    second_stage = state_manager.get_stage("draft")

    assert Path(first_result.draft_content_path).exists()
    assert first_stage is not None
    assert first_stage["status"] == StageStatus.DONE.value
    assert first_stage["payload"]["execution_mode"] == "fresh_write"
    assert first_stage["payload"]["restore_reason"] is None
    assert first_stage["payload"]["rerun_reason"] == "draft_stage_state_missing_or_not_done"
    assert first_stage["payload"]["reused_artifacts"] == []
    assert first_stage["payload"]["artifact_paths"]
    assert second_result.draft_content_path == first_result.draft_content_path
    assert second_stage is not None
    assert second_stage["payload"]["execution_mode"] == "reuse_existing_artifacts"
    assert second_stage["payload"]["skipped"] is True
    assert second_stage["payload"]["restore_reason"] == "existing_draft_artifacts_valid"
    assert second_stage["payload"]["rerun_reason"] is None
    assert second_stage["payload"]["reused_artifacts"] == second_stage["payload"]["artifact_paths"]
