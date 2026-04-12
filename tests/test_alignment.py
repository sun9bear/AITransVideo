from pathlib import Path

from core.enums import BlockStatus
from core.exceptions import TTSError
from core.models import SemanticBlock
from modules.alignment.alignment_orchestrator import AlignmentConfig, AlignmentOrchestrator
from modules.alignment.dsp_stretcher import DSPStretcher
from modules.alignment.rewrite_engine import RewriteEngine
from services.audio_service import AudioService
from services.llm_service import MockLLMService
from services.tts_service import MockTTSConfig, MockTTSService


class StubbornLLMService:
    def rewrite_text(
        self,
        prompt: str,
        source_text: str,
        actual_duration_ms: int,
        target_duration_ms: int,
    ) -> str:
        del prompt, actual_duration_ms, target_duration_ms
        return source_text


class BrokenTTSService:
    def synthesize(self, block: SemanticBlock) -> str:
        del block
        raise TTSError("mock synthesis failure")


def _make_block(text: str, target_duration_ms: int) -> SemanticBlock:
    return SemanticBlock(
        block_id="block_test",
        speaker_id="spk_1",
        speaker_name="Alice",
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=target_duration_ms,
        target_duration_ms=target_duration_ms,
        merged_cn_text=text,
        cn_line_texts=[text],
    )


def _make_orchestrator(
    output_dir: Path,
    llm_service: object | None = None,
    config: AlignmentConfig | None = None,
) -> AlignmentOrchestrator:
    tts_service = MockTTSService(
        output_dir=str(output_dir),
        config=MockTTSConfig(ms_per_char=100, min_duration_ms=800),
    )
    audio_service = AudioService()
    rewrite_engine = RewriteEngine(llm_service=llm_service or MockLLMService())
    dsp_stretcher = DSPStretcher()
    return AlignmentOrchestrator(
        tts_service=tts_service,
        audio_service=audio_service,
        rewrite_engine=rewrite_engine,
        dsp_stretcher=dsp_stretcher,
        config=config or AlignmentConfig(ideal_threshold=0.05, dsp_threshold=0.20, max_retries=2),
    )


def test_alignment_succeeds_on_ideal_hit(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    block = _make_block("好" * 10, 1_000)

    processed = orchestrator.process_block(block)

    assert processed.status == BlockStatus.ALIGN_DONE.value
    assert processed.rewrite_count == 0
    assert processed.tts_audio_path == processed.aligned_audio_path
    assert processed.actual_audio_duration_ms == 1_000


def test_alignment_uses_unified_block_cn_text_for_spoken_text(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    block = SemanticBlock(
        block_id="block_tts_layer",
        speaker_id="spk_1",
        speaker_name="Alice",
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1_500,
        target_duration_ms=1_500,
        merged_cn_text="c" * 15,
        cn_line_texts=["placeholder"],
    )

    processed = orchestrator.process_block(block)

    assert processed.status == BlockStatus.ALIGN_DONE.value
    assert processed.rewrite_count == 0
    assert processed.actual_audio_duration_ms == 1_500
    assert processed.final_cn_lines == ["c" * 15]


def test_alignment_uses_dsp_for_small_diff(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    block = _make_block("好" * 12, 1_000)

    processed = orchestrator.process_block(block)

    assert processed.status == BlockStatus.ALIGN_DONE.value
    assert processed.rewrite_count == 0
    assert processed.aligned_audio_path is not None
    assert processed.tts_audio_path is not None
    assert processed.aligned_audio_path != processed.tts_audio_path
    assert processed.aligned_audio_path.endswith("_aligned.wav")
    assert processed.actual_audio_duration_ms == 1_000


def test_alignment_rewrites_for_large_diff_then_succeeds(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    block = _make_block("好" * 20, 1_000)

    processed = orchestrator.process_block(block)

    assert processed.status == BlockStatus.ALIGN_DONE.value
    assert processed.rewrite_count == 1
    assert len(processed.merged_cn_text) == 10
    assert processed.actual_audio_duration_ms == 1_000


def test_alignment_falls_back_to_dsp_after_retry_limit(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(
        tmp_path,
        llm_service=StubbornLLMService(),
        config=AlignmentConfig(ideal_threshold=0.05, dsp_threshold=0.20, max_retries=2),
    )
    block = _make_block("好" * 20, 1_000)

    processed = orchestrator.process_block(block)

    assert processed.status == BlockStatus.ALIGN_DONE_FALLBACK.value
    assert processed.rewrite_count == 2
    assert processed.aligned_audio_path is not None
    assert processed.aligned_audio_path.endswith("_aligned.wav")
    assert processed.actual_audio_duration_ms == 1_000


def test_alignment_marks_failed_on_exception(tmp_path: Path) -> None:
    audio_service = AudioService()
    rewrite_engine = RewriteEngine(llm_service=MockLLMService())
    orchestrator = AlignmentOrchestrator(
        tts_service=BrokenTTSService(),
        audio_service=audio_service,
        rewrite_engine=rewrite_engine,
        dsp_stretcher=DSPStretcher(),
        config=AlignmentConfig(),
    )
    block = _make_block("好" * 10, 1_000)

    processed = orchestrator.process_block(block)

    assert processed.status == BlockStatus.FAILED.value
    assert processed.tts_audio_path is None
    assert processed.aligned_audio_path is None
    assert processed.error_message == "mock synthesis failure"


def test_alignment_rerun_clears_previous_block_failure_state_on_success(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    block = _make_block("abcdefghij", 1_000)
    block.status = BlockStatus.FAILED.value
    block.error_message = "previous failure"
    block.error_type = "provider_timeout"

    processed = orchestrator.process_block(block)

    assert processed.status == BlockStatus.ALIGN_DONE.value
    assert processed.error_message is None
    assert processed.error_type is None
    assert processed.tts_audio_path is not None
    assert processed.aligned_audio_path == processed.tts_audio_path


def test_alignment_returns_matching_line_count_for_multi_line_block(tmp_path: Path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    block = SemanticBlock(
        block_id="block_multi",
        speaker_id="spk_1",
        speaker_name="Alice",
        original_srt_indices=[1, 2, 3],
        first_start_ms=0,
        last_end_ms=800,
        target_duration_ms=800,
        merged_cn_text="欢迎回来朋友们",
        cn_line_texts=["欢迎", "回来", "朋友们"],
    )

    processed = orchestrator.process_block(block)

    assert processed.status == BlockStatus.ALIGN_DONE.value
    assert len(processed.final_cn_lines) == len(block.cn_line_texts)
    assert "".join(processed.final_cn_lines) == processed.merged_cn_text
