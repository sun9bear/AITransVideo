"""Process → Workflow convergence tests.

Verify that process pipeline output blocks use the canonical SemanticBlock
type, not a process-private dataclass, and that alignment fields flow
through correctly.
"""

from __future__ import annotations

from core.models import SemanticBlock


# ===================================================================
# SemanticBlock field availability
# ===================================================================


class TestSemanticBlockAlignmentFields:
    """SemanticBlock must carry alignment_method and needs_review."""

    def test_alignment_method_has_default(self):
        block = SemanticBlock(
            block_id="b001",
            speaker_id="speaker_a",
            speaker_name="Alice",
            original_srt_indices=[1],
            first_start_ms=0,
            last_end_ms=3000,
            target_duration_ms=2800,
            merged_cn_text="测试文本",
        )
        assert block.alignment_method == "direct"

    def test_needs_review_has_default(self):
        block = SemanticBlock(
            block_id="b001",
            speaker_id="speaker_a",
            speaker_name="Alice",
            original_srt_indices=[1],
            first_start_ms=0,
            last_end_ms=3000,
            target_duration_ms=2800,
            merged_cn_text="测试文本",
        )
        assert block.needs_review is False

    def test_alignment_fields_can_be_set_explicitly(self):
        block = SemanticBlock(
            block_id="b002",
            speaker_id="speaker_a",
            speaker_name="Alice",
            original_srt_indices=[2],
            first_start_ms=3000,
            last_end_ms=6000,
            target_duration_ms=2700,
            merged_cn_text="另一段测试",
            alignment_method="force_dsp",
            needs_review=True,
        )
        assert block.alignment_method == "force_dsp"
        assert block.needs_review is True


# ===================================================================
# Process output block type convergence
# ===================================================================


class TestProcessOutputBlockType:
    """process.py _build_process_output_blocks must return SemanticBlock."""

    def test_output_blocks_are_semantic_blocks(self):
        """Blocks in stage_outputs['aligned_blocks'] must be SemanticBlock instances."""
        from pipeline.process import ProcessPipeline
        from services.gemini.translator import DubbingSegment

        pipeline = ProcessPipeline()
        segments = [
            DubbingSegment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Alice",
                voice_id="voice_001",
                source_text="Hello world",
                cn_text="你好世界",
                tts_cn_text="你好世界",
                start_ms=0,
                end_ms=3000,
                target_duration_ms=2800,
                actual_duration_ms=2750,
                rewrite_count=0,
                tts_audio_path="/tmp/seg_001.wav",
                aligned_audio_path="/tmp/seg_001_aligned.wav",
                alignment_method="direct",
                needs_review=False,
            ),
        ]
        blocks = pipeline._build_process_output_blocks(segments)

        assert len(blocks) == 1
        assert isinstance(blocks[0], SemanticBlock), (
            f"Expected SemanticBlock, got {type(blocks[0]).__name__}"
        )

    def test_alignment_method_flows_through(self):
        from pipeline.process import ProcessPipeline
        from services.gemini.translator import DubbingSegment

        pipeline = ProcessPipeline()
        segments = [
            DubbingSegment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Alice",
                voice_id="voice_001",
                source_text="Test",
                cn_text="测试",
                tts_cn_text="测试",
                start_ms=0,
                end_ms=2000,
                target_duration_ms=1800,
                actual_duration_ms=1750,
                rewrite_count=0,
                alignment_method="force_dsp",
                needs_review=True,
            ),
        ]
        blocks = pipeline._build_process_output_blocks(segments)

        assert blocks[0].alignment_method == "force_dsp"
        assert blocks[0].needs_review is True


# ===================================================================
# OutputDispatcher segment_id fallback with SemanticBlock
# ===================================================================


class TestOutputDispatcherSegmentIdFallback:
    """OutputDispatcher must produce stable segment_id even when block
    has no segment_id attribute (SemanticBlock uses block_id instead)."""

    def test_fallback_index_used_when_no_segment_id(self):
        from modules.output.output_dispatcher import OutputDispatcher

        block = SemanticBlock(
            block_id="segment_001",
            speaker_id="speaker_a",
            speaker_name="Alice",
            original_srt_indices=[1],
            first_start_ms=0,
            last_end_ms=3000,
            target_duration_ms=2800,
            merged_cn_text="你好世界",
            tts_audio_path="/tmp/seg_001.wav",
            aligned_audio_path="/tmp/seg_001_aligned.wav",
            status="align_done",
            alignment_method="direct",
            needs_review=False,
        )

        # SemanticBlock has no segment_id attribute.
        # _resolve_segment_id should fall back to the provided index.
        resolved = OutputDispatcher._resolve_segment_id(block, fallback=42)
        assert resolved == 42

    def test_alignment_method_read_directly_from_semantic_block(self):
        from modules.output.output_dispatcher import OutputDispatcher

        block = SemanticBlock(
            block_id="segment_002",
            speaker_id="speaker_a",
            speaker_name="Alice",
            original_srt_indices=[2],
            first_start_ms=3000,
            last_end_ms=6000,
            target_duration_ms=2700,
            merged_cn_text="另一段",
            alignment_method="force_dsp",
            needs_review=True,
        )

        assert OutputDispatcher._resolve_alignment_method(block) == "force_dsp"
        assert OutputDispatcher._resolve_needs_review(block) is True
