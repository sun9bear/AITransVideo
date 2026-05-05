"""Tests for SemanticBlock.tts_input_cn_text + cue-pipeline drift detection
(Phase B of 2026-05-04-subtitle-audio-sync-plan).

When a SemanticBlock's ``merged_cn_text`` differs from
``tts_input_cn_text`` (the joined text that produced its current
audio), cue generation must:
  1. NOT silently emit timestamps from mismatched audio
  2. Emit a ``text_audio_drift`` validation issue so downstream consumers
     (Phase C whisper alignment, future UI badges) can react

Cue pipeline behavior under drift:
  - In Phase B (this commit): emit the issue, but otherwise produce cues
    via the existing proportional layout (current behavior).
  - In Phase C: skip whisper alignment for drift blocks, fall through
    to proportional layout.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# B1: dataclass field
# ---------------------------------------------------------------------------


def test_semantic_block_has_tts_input_cn_text_default_empty():
    """Default empty until the block-builder fills it from segment data.

    Empty must mean "unknown" downstream; the cue-pipeline sync check
    treats empty as in-sync (legacy backfill rule, mirrors the segment
    side from Phase A)."""
    from core.models import SemanticBlock
    b = SemanticBlock(
        block_id="b1", speaker_id="A", speaker_name="A",
        original_srt_indices=[1], first_start_ms=0, last_end_ms=1000,
        target_duration_ms=1000, merged_cn_text="hi",
    )
    assert b.tts_input_cn_text == ""


def test_semantic_block_strips_whitespace_in_post_init():
    """Mirror the existing post_init treatment of merged_cn_text — strip
    whitespace so comparison with merged_cn_text doesn't trip on stray
    leading/trailing spaces."""
    from core.models import SemanticBlock
    b = SemanticBlock(
        block_id="b1", speaker_id="A", speaker_name="A",
        original_srt_indices=[1], first_start_ms=0, last_end_ms=1000,
        target_duration_ms=1000,
        merged_cn_text="  hi  ",
        tts_input_cn_text="  hi  ",
    )
    assert b.merged_cn_text == "hi"
    assert b.tts_input_cn_text == "hi"


# ---------------------------------------------------------------------------
# B2: pipeline _build_blocks populates field from segments
# ---------------------------------------------------------------------------


def _make_segment(
    sid: int,
    cn_text: str,
    *,
    tts_input_cn_text: str | None = None,
    short_merge_absorbed_segment_ids: str = "",
):
    """Minimal DubbingSegment factory for block-builder tests."""
    from services.gemini.translator import DubbingSegment
    return DubbingSegment(
        segment_id=sid,
        speaker_id="A", display_name="A", voice_id="v",
        start_ms=sid * 1000, end_ms=(sid + 1) * 1000,
        target_duration_ms=1000,
        source_text=f"src{sid}",
        cn_text=cn_text,
        tts_input_cn_text=tts_input_cn_text if tts_input_cn_text is not None else cn_text,
        short_merge_absorbed_segment_ids=short_merge_absorbed_segment_ids,
    )


def test_build_blocks_one_to_one_propagates_tts_input_cn_text():
    """Single-segment block: block.tts_input_cn_text == segment.tts_input_cn_text."""
    from pipeline.process import ProcessPipeline
    seg = _make_segment(1, "你好", tts_input_cn_text="你好")
    blocks = ProcessPipeline._build_process_output_blocks(
        ProcessPipeline.__new__(ProcessPipeline), [seg]
    )
    assert len(blocks) == 1
    assert blocks[0].merged_cn_text == "你好"
    assert blocks[0].tts_input_cn_text == "你好"


def test_build_blocks_drift_segment_propagates_drift_to_block():
    """Segment with cn_text != tts_input_cn_text (drift) makes the block
    inherit the drift state — block.merged_cn_text is the new text,
    block.tts_input_cn_text is the audio's original text."""
    from pipeline.process import ProcessPipeline
    seg = _make_segment(1, "用户改后的新文本", tts_input_cn_text="原始合成文本")
    blocks = ProcessPipeline._build_process_output_blocks(
        ProcessPipeline.__new__(ProcessPipeline), [seg]
    )
    assert blocks[0].merged_cn_text == "用户改后的新文本"
    assert blocks[0].tts_input_cn_text == "原始合成文本"


def test_build_blocks_legacy_segment_with_empty_tts_input_backfills_from_cn():
    """Defense-in-depth: even after Phase A's load-time backfill, if a
    segment somehow lands with tts_input_cn_text="" and cn_text non-empty
    (e.g. fresh dataclass construction in tests, manual API usage), the
    block builder treats it as in-sync rather than triggering false drift
    detection."""
    from pipeline.process import ProcessPipeline
    seg = _make_segment(1, "正常文本", tts_input_cn_text="")
    blocks = ProcessPipeline._build_process_output_blocks(
        ProcessPipeline.__new__(ProcessPipeline), [seg]
    )
    assert blocks[0].merged_cn_text == "正常文本"
    assert blocks[0].tts_input_cn_text == "正常文本"  # backfilled


def test_short_merge_join_includes_tts_input_cn_text_in_parallel():
    """When short_merge collapses multiple segments into one base, both
    base.cn_text AND base.tts_input_cn_text get joined the same way.

    Two-segment merge case:
      seg_1: cn='A', tts_input='A'
      seg_2: cn='B', tts_input='B'  (both in sync)
    After merge:
      base.cn_text == "A B"
      base.tts_input_cn_text == "A B"  (also synced)
    """
    from pipeline.process import ProcessPipeline
    seg_1 = _make_segment(1, "A", tts_input_cn_text="A")
    seg_2 = _make_segment(2, "B", tts_input_cn_text="B")
    base = ProcessPipeline._materialize_short_merge_group([seg_1, seg_2])
    assert base.cn_text == "A B"
    assert base.tts_input_cn_text == "A B"


def test_short_merge_preserves_drift_when_one_member_is_drift():
    """If one of the merged members has cn_text != tts_input_cn_text, the
    merged base inherits that drift: base.cn_text uses the new texts but
    base.tts_input_cn_text reflects the old (audio's) texts. Cue pipeline
    will then detect drift on the resulting block."""
    from pipeline.process import ProcessPipeline
    seg_1 = _make_segment(1, "A_new", tts_input_cn_text="A_old")  # drift
    seg_2 = _make_segment(2, "B", tts_input_cn_text="B")          # sync
    base = ProcessPipeline._materialize_short_merge_group([seg_1, seg_2])
    assert base.cn_text == "A_new B"
    assert base.tts_input_cn_text == "A_old B"
    # The assert that matters downstream:
    assert base.cn_text != base.tts_input_cn_text


def test_short_merge_single_segment_returns_unchanged():
    """A 'group' of one is a no-op and tts_input_cn_text passes through."""
    from pipeline.process import ProcessPipeline
    seg = _make_segment(1, "X", tts_input_cn_text="X")
    base = ProcessPipeline._materialize_short_merge_group([seg])
    assert base is seg  # same instance
    assert base.tts_input_cn_text == "X"


# ---------------------------------------------------------------------------
# B3: cue pipeline drift detection + validation issue
# ---------------------------------------------------------------------------


def _make_block_spec(
    block_id: str,
    merged_cn_text: str,
    *,
    tts_input_cn_text: str | None = None,
    start_ms: int = 0,
    end_ms: int = 5000,
):
    """BlockSpec factory; defaults tts_input_cn_text to merged_cn_text (sync)."""
    from modules.subtitles.cue_validator import BlockSpec
    return BlockSpec(
        block_id=block_id,
        merged_cn_text=merged_cn_text,
        start_ms=start_ms,
        end_ms=end_ms,
        tts_input_cn_text=(
            tts_input_cn_text if tts_input_cn_text is not None else merged_cn_text
        ),
    )


def _make_cue(
    cue_id: str, block_id: str, text: str, start_ms: int, end_ms: int,
):
    """SubtitleCue factory."""
    from modules.subtitles.cue_models import SubtitleCue
    return SubtitleCue(
        cue_id=cue_id,
        block_id=block_id,
        speaker_id="A",
        speaker_name="A",
        text=text,
        en_text="",
        start_ms=start_ms,
        end_ms=end_ms,
        source="block",
        needs_review=False,
        review_reason=None,
    )


def test_validator_emits_drift_issue_when_tts_input_differs_from_merged():
    """A BlockSpec where tts_input_cn_text != merged_cn_text emits a
    text_audio_drift issue. Severity is 'review' (informational) — drift
    flips status to needs_review but doesn't fail validation, since the
    cue pipeline still produces usable cues via the proportional layout
    fallback."""
    from modules.subtitles.cue_validator import validate_cues

    drift_spec = _make_block_spec(
        "b1",
        merged_cn_text="新版文字",
        tts_input_cn_text="原版文字",
        start_ms=0, end_ms=2000,
    )
    sync_spec = _make_block_spec(
        "b2",
        merged_cn_text="同步的文字",
        start_ms=2000, end_ms=4000,
    )
    cues = [
        _make_cue("b1_c1", "b1", "新版文字", 0, 2000),
        _make_cue("b2_c1", "b2", "同步的文字", 2000, 4000),
    ]
    report = validate_cues(block_specs=[drift_spec, sync_spec], cues=cues)

    drift_issues = [i for i in report.issues if i.code == "text_audio_drift"]
    assert len(drift_issues) == 1
    assert drift_issues[0].block_id == "b1"
    assert drift_issues[0].severity == "review"

    # Validation status flips to needs_review (would have been "passed"
    # without drift) — the drift IS surfaced but not as a hard error.
    assert report.validation_status == "needs_review"


def test_validator_no_drift_issue_when_fields_match():
    """No drift issue emitted when merged_cn_text == tts_input_cn_text
    (post-strip-and-normalize comparison)."""
    from modules.subtitles.cue_validator import validate_cues

    spec = _make_block_spec("b1", merged_cn_text="同步文本")
    cues = [_make_cue("b1_c1", "b1", "同步文本", 0, 2000)]
    report = validate_cues(block_specs=[spec], cues=cues)

    drift_issues = [i for i in report.issues if i.code == "text_audio_drift"]
    assert drift_issues == []
    assert report.validation_status == "passed"


def test_validator_treats_empty_tts_input_as_in_sync():
    """A BlockSpec with empty tts_input_cn_text (legacy case before Phase
    A backfill ran) is treated as in-sync — no false drift flag.

    This is an extra safety net: Phase A's load-time backfill SHOULD have
    populated the field, but layered defense is cheap and keeps cue
    pipeline robust across version skews."""
    from modules.subtitles.cue_validator import validate_cues

    spec = _make_block_spec("b1", merged_cn_text="文本", tts_input_cn_text="")
    cues = [_make_cue("b1_c1", "b1", "文本", 0, 2000)]
    report = validate_cues(block_specs=[spec], cues=cues)

    drift_issues = [i for i in report.issues if i.code == "text_audio_drift"]
    assert drift_issues == []


def test_block_summary_exposes_text_audio_drift_flag():
    """BlockSummary carries a per-block text_audio_drift bool so the
    quality report can surface it directly without re-walking issues."""
    from modules.subtitles.cue_validator import validate_cues

    drift_spec = _make_block_spec(
        "b1", merged_cn_text="新", tts_input_cn_text="原",
        start_ms=0, end_ms=2000,
    )
    sync_spec = _make_block_spec(
        "b2", merged_cn_text="同步",
        start_ms=2000, end_ms=4000,
    )
    cues = [
        _make_cue("b1_c1", "b1", "新", 0, 2000),
        _make_cue("b2_c1", "b2", "同步", 2000, 4000),
    ]
    report = validate_cues(block_specs=[drift_spec, sync_spec], cues=cues)

    by_id = {s.block_id: s for s in report.block_summaries}
    assert by_id["b1"].text_audio_drift is True
    assert by_id["b2"].text_audio_drift is False


def test_cue_pipeline_emits_drift_issue_for_drift_block():
    """End-to-end: build_subtitle_cues_for_blocks passes block.tts_input_cn_text
    through to the validator's BlockSpec and a drift block surfaces in the
    final ValidationReport."""
    from core.models import SemanticBlock, SubtitleLine
    from modules.subtitles.cue_pipeline import build_subtitle_cues_for_blocks

    drift_block = SemanticBlock(
        block_id="b1", speaker_id="A", speaker_name="A",
        original_srt_indices=[1], first_start_ms=0, last_end_ms=2000,
        target_duration_ms=2000,
        merged_cn_text="新版文字",
        tts_input_cn_text="原版文字",
    )
    sync_block = SemanticBlock(
        block_id="b2", speaker_id="A", speaker_name="A",
        original_srt_indices=[2], first_start_ms=2000, last_end_ms=4000,
        target_duration_ms=2000,
        merged_cn_text="同步的文字",
        tts_input_cn_text="同步的文字",
    )
    lines = [
        SubtitleLine(index=1, start_ms=0, end_ms=2000, speaker_id="A",
                     speaker_name="A", en_text="x", cn_text="新版文字"),
        SubtitleLine(index=2, start_ms=2000, end_ms=4000, speaker_id="A",
                     speaker_name="A", en_text="y", cn_text="同步的文字"),
    ]
    result = build_subtitle_cues_for_blocks([drift_block, sync_block], lines)

    drift_issues = [i for i in result.report.issues if i.code == "text_audio_drift"]
    assert len(drift_issues) == 1
    assert drift_issues[0].block_id == "b1"

    # And block-summary surfaces it without re-walking issues.
    by_id = {s.block_id: s for s in result.report.block_summaries}
    assert by_id["b1"].text_audio_drift is True
    assert by_id["b2"].text_audio_drift is False


# ---------------------------------------------------------------------------
# B4: subtitle_quality_report.json exposes per-block drift + count
# ---------------------------------------------------------------------------


def test_quality_report_json_exposes_text_audio_drift_per_block(tmp_path):
    """Per-block summaries in the serialized JSON include text_audio_drift
    so the UI / downstream tooling can render "audio out of date" badges
    without re-deriving from the issues list."""
    import json
    from modules.output.output_dispatcher import OutputDispatcher
    from modules.subtitles.cue_validator import (
        BlockSummary, ValidationReport,
    )

    drift_summary = BlockSummary(
        block_id="b1",
        cue_count=2,
        text_mismatch=False,
        timing_overlap_count=0,
        timing_out_of_block_count=0,
        empty_cue_count=0,
        long_unbreakable_count=0,
        unknown_mixed_token_count=0,
        short_display_duration_count=0,
        text_audio_drift=True,
    )
    sync_summary = BlockSummary(
        block_id="b2",
        cue_count=2,
        text_mismatch=False,
        timing_overlap_count=0,
        timing_out_of_block_count=0,
        empty_cue_count=0,
        long_unbreakable_count=0,
        unknown_mixed_token_count=0,
        short_display_duration_count=0,
        text_audio_drift=False,
    )
    report = ValidationReport(
        validation_status="needs_review",
        issues=[],
        block_summaries=[drift_summary, sync_summary],
    )

    out = tmp_path / "subtitle_quality_report.json"
    OutputDispatcher._write_quality_report_json(out, "proj_test", report, [])
    data = json.loads(out.read_text(encoding="utf-8"))

    by_id = {s["block_id"]: s for s in data["block_summaries"]}
    assert by_id["b1"]["text_audio_drift"] is True
    assert by_id["b2"]["text_audio_drift"] is False


def test_quality_report_json_exposes_aggregate_drift_count(tmp_path):
    """Top-level ``text_audio_drift_count`` surfaces "how many blocks have
    drift" at a glance, so dashboards / UI summary cards don't have to
    iterate block_summaries themselves."""
    import json
    from modules.output.output_dispatcher import OutputDispatcher
    from modules.subtitles.cue_validator import (
        BlockSummary, ValidationReport,
    )

    summaries = [
        BlockSummary(
            block_id=f"b{i}",
            cue_count=1,
            text_mismatch=False,
            timing_overlap_count=0,
            timing_out_of_block_count=0,
            empty_cue_count=0,
            long_unbreakable_count=0,
            unknown_mixed_token_count=0,
            short_display_duration_count=0,
            text_audio_drift=(i in (1, 3)),  # 2 of 4 blocks drift
        ) for i in range(4)
    ]
    report = ValidationReport(
        validation_status="needs_review",
        issues=[],
        block_summaries=summaries,
    )

    out = tmp_path / "subtitle_quality_report.json"
    OutputDispatcher._write_quality_report_json(out, "proj_test", report, [])
    data = json.loads(out.read_text(encoding="utf-8"))

    assert data["text_audio_drift_count"] == 2
    assert data["validation_status"] == "needs_review"


# ---------------------------------------------------------------------------
# Phase C C3: cue_pipeline integration with feature flag + 5 fallback paths
# ---------------------------------------------------------------------------
#
# CodeX-required regression classes (all covered below):
#   - flag off
#   - sync block + audio + flag on → whisper used
#   - drift block (skip)
#   - no aligned_audio_path (skip)
#   - whisper subprocess failure (fallback)
#   - DTW empty result (fallback)
#   - short_merge / no aligned audio (fallback)


def _make_block(
    block_id: str,
    cn_text: str,
    *,
    tts_input_cn_text: str | None = None,
    aligned_audio_path: str | None = None,
    first_start_ms: int = 0,
    last_end_ms: int = 5000,
    target_duration_ms: int = 5000,
):
    from core.models import SemanticBlock
    return SemanticBlock(
        block_id=block_id, speaker_id="A", speaker_name="A",
        original_srt_indices=[1],
        first_start_ms=first_start_ms, last_end_ms=last_end_ms,
        target_duration_ms=target_duration_ms,
        merged_cn_text=cn_text,
        tts_input_cn_text=(tts_input_cn_text if tts_input_cn_text is not None else cn_text),
        aligned_audio_path=aligned_audio_path,
    )


def _make_subtitle_lines(*texts):
    from core.models import SubtitleLine
    return [
        SubtitleLine(index=i + 1, start_ms=i * 5000, end_ms=(i + 1) * 5000,
                     speaker_id="A", speaker_name="A",
                     en_text=f"en{i}", cn_text=t)
        for i, t in enumerate(texts)
    ]


def test_c3_flag_off_skips_whisper_alignment(monkeypatch, tmp_path):
    """When AVT_WHISPER_ALIGN_ENABLED is unset / != "1", whisper is NEVER
    invoked. All cues come from the proportional path — source tag does
    NOT contain "whisper"."""
    monkeypatch.delenv("AVT_WHISPER_ALIGN_ENABLED", raising=False)
    from modules.subtitles.cue_pipeline import build_subtitle_cues_for_blocks

    # Whisper must not be called — set up a sentinel that would crash if it were
    def _explode(*a, **kw):
        raise AssertionError("whisper should not be called when flag is off")
    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._run_whisper_cached", _explode,
    )

    audio = tmp_path / "seg.wav"
    audio.write_bytes(b"fake-wav")
    block = _make_block("b1", "你好。", aligned_audio_path=str(audio))
    result = build_subtitle_cues_for_blocks([block], _make_subtitle_lines("你好。"))
    assert all("whisper" not in c.source.lower() for c in result.cues)


def test_c3_flag_on_sync_block_uses_whisper(monkeypatch, tmp_path):
    """Flag on + drift==False + audio path exists → whisper subprocess
    invoked, char_times feed into cue construction, cues carry the
    whisper-aligned source tag."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    from modules.subtitles.cue_pipeline import build_subtitle_cues_for_blocks

    fake_words = [
        {"start_ms": 100, "end_ms": 800, "text": "你好"},
    ]
    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        lambda *a, **kw: fake_words,
    )

    audio = tmp_path / "seg.wav"
    audio.write_bytes(b"fake-wav")
    # 1-cue text "你好" — no segmenter split
    block = _make_block("b1", "你好",
                        aligned_audio_path=str(audio),
                        first_start_ms=10_000, last_end_ms=11_000,
                        target_duration_ms=1_000)
    result = build_subtitle_cues_for_blocks([block], _make_subtitle_lines("你好"))
    assert len(result.cues) == 1
    assert "whisper" in result.cues[0].source.lower()
    # Local 100..800ms + global offset 10_000 → 10_100..10_800
    assert result.cues[0].start_ms == 10_100
    assert result.cues[0].end_ms == 10_800


def test_c3_drift_block_skips_whisper_uses_proportional(monkeypatch, tmp_path):
    """Drift block (cn_text != tts_input_cn_text per normalize) MUST skip
    whisper and use proportional path — Phase B's drift detection gate
    is consumed here exactly as CodeX guardrail #4 requires."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    from modules.subtitles.cue_pipeline import build_subtitle_cues_for_blocks

    def _explode(*a, **kw):
        raise AssertionError("whisper should not be called for drift block")
    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._run_whisper_cached", _explode,
    )

    audio = tmp_path / "seg.wav"
    audio.write_bytes(b"fake-wav")
    drift_block = _make_block(
        "b1", "新版文字",  # current cn_text
        tts_input_cn_text="原版文字",  # what audio was made from
        aligned_audio_path=str(audio),
    )
    result = build_subtitle_cues_for_blocks(
        [drift_block], _make_subtitle_lines("新版文字"),
    )
    # Cues built — but NOT via whisper path
    assert all("whisper" not in c.source.lower() for c in result.cues)


def test_c3_block_without_aligned_audio_skips_whisper(monkeypatch, tmp_path):
    """A SemanticBlock with aligned_audio_path=None (e.g. short_merge
    base where audio is freshly cleared) MUST skip whisper. Falls
    through to proportional path."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    from modules.subtitles.cue_pipeline import build_subtitle_cues_for_blocks

    def _explode(*a, **kw):
        raise AssertionError("whisper should not be called when no audio")
    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._run_whisper_cached", _explode,
    )

    block = _make_block("b1", "你好。", aligned_audio_path=None)
    result = build_subtitle_cues_for_blocks([block], _make_subtitle_lines("你好。"))
    assert all("whisper" not in c.source.lower() for c in result.cues)


def test_c3_block_with_missing_audio_file_skips_whisper(monkeypatch, tmp_path):
    """aligned_audio_path is set but the file is missing on disk
    (deleted, copy_as_new mid-flight, etc.) → skip whisper, fall back."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    from modules.subtitles.cue_pipeline import build_subtitle_cues_for_blocks

    def _explode(*a, **kw):
        raise AssertionError("whisper should not be called when audio missing")
    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._run_whisper_cached", _explode,
    )

    block = _make_block(
        "b1", "你好。", aligned_audio_path=str(tmp_path / "does_not_exist.wav"),
    )
    result = build_subtitle_cues_for_blocks([block], _make_subtitle_lines("你好。"))
    assert all("whisper" not in c.source.lower() for c in result.cues)


def test_c3_whisper_subprocess_failure_falls_back_to_proportional(monkeypatch, tmp_path):
    """Whisper subprocess raises (model load failed, OOM, timeout) →
    cue_pipeline catches and falls back to proportional. PUBLISH MUST
    NOT FAIL just because whisper had a bad day — CodeX guardrail #5."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    from modules.subtitles.cue_pipeline import build_subtitle_cues_for_blocks

    def _fail(*a, **kw):
        raise RuntimeError("whisper subprocess failed (rc=1): cuda OOM")
    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._run_whisper_cached", _fail,
    )

    audio = tmp_path / "seg.wav"
    audio.write_bytes(b"fake-wav")
    block = _make_block("b1", "你好。", aligned_audio_path=str(audio))
    result = build_subtitle_cues_for_blocks([block], _make_subtitle_lines("你好。"))
    # Cues exist (publish proceeds) but via proportional path
    assert len(result.cues) >= 1
    assert all("whisper" not in c.source.lower() for c in result.cues)


def test_c3_dtw_empty_result_falls_back_to_proportional(monkeypatch, tmp_path):
    """Whisper succeeded but DTW returned [] (cn_text and whisper
    transcript too disjoint to align). Cue pipeline falls back."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    from modules.subtitles.cue_pipeline import build_subtitle_cues_for_blocks

    # Wrong-language whisper output → DTW disjoint → empty char_times
    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        lambda *a, **kw: [{"start_ms": 0, "end_ms": 1000,
                            "text": "totally different english words here"}],
    )

    audio = tmp_path / "seg.wav"
    audio.write_bytes(b"fake-wav")
    block = _make_block("b1", "你好世界",
                        aligned_audio_path=str(audio),
                        first_start_ms=0, last_end_ms=1000,
                        target_duration_ms=1000)
    result = build_subtitle_cues_for_blocks([block], _make_subtitle_lines("你好世界"))
    assert len(result.cues) >= 1
    assert all("whisper" not in c.source.lower() for c in result.cues)


def test_c3_dtw_raising_exception_falls_back_to_proportional(monkeypatch, tmp_path):
    """CodeX P1: Whisper succeeds but DTW raises (e.g. unexpected input
    shape, bug in normalize). Cue pipeline MUST catch this and fall back
    to proportional — publish must never fail because of whisper/DTW
    trouble. CodeX guardrail #5."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    from modules.subtitles.cue_pipeline import build_subtitle_cues_for_blocks

    # Whisper returns "valid" words (caller can't pre-validate)
    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        lambda *a, **kw: [{"start_ms": 0, "end_ms": 1000, "text": "你好"}],
    )

    # DTW raises some unexpected exception
    def _explode(*a, **kw):
        raise KeyError("synthetic dtw bug")
    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._align_chars_to_words", _explode,
    )

    audio = tmp_path / "seg.wav"
    audio.write_bytes(b"fake")
    block = _make_block(
        "b1", "你好",
        aligned_audio_path=str(audio),
        first_start_ms=0, last_end_ms=1000, target_duration_ms=1000,
    )
    result = build_subtitle_cues_for_blocks([block], _make_subtitle_lines("你好"))
    # Cues exist (publish proceeded) AND came via proportional path.
    assert len(result.cues) >= 1
    assert all("whisper" not in c.source.lower() for c in result.cues)


def test_c3_helper_raising_exception_falls_back_to_proportional(monkeypatch, tmp_path):
    """Same guardrail: build_cues_with_char_times raising must not crash
    publish. (The helper itself returns [] for known anomalies; this
    covers any unforeseen exception path.)"""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    from modules.subtitles.cue_pipeline import build_subtitle_cues_for_blocks

    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        lambda *a, **kw: [{"start_ms": 0, "end_ms": 1000, "text": "你好"}],
    )
    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._align_chars_to_words",
        lambda *a, **kw: [{"start_ms": 0, "end_ms": 1000, "text": "你好"}],
    )

    def _explode(*a, **kw):
        raise RuntimeError("synthetic helper bug")
    # build_cues_with_char_times is imported at module top into cue_pipeline's
    # namespace, so the patch target IS cue_pipeline (not cue_builder).
    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline.build_cues_with_char_times", _explode,
    )

    audio = tmp_path / "seg.wav"
    audio.write_bytes(b"fake")
    block = _make_block(
        "b1", "你好",
        aligned_audio_path=str(audio),
        first_start_ms=0, last_end_ms=1000, target_duration_ms=1000,
    )
    result = build_subtitle_cues_for_blocks([block], _make_subtitle_lines("你好"))
    assert len(result.cues) >= 1
    assert all("whisper" not in c.source.lower() for c in result.cues)


def test_c3_drift_check_uses_normalize_consistent_with_phase_b(monkeypatch, tmp_path):
    """The drift gate must use cue_models.normalize() — same as the
    Phase B validator — so drift decisions in C3 match what
    subtitle_quality_report.json says. CodeX guardrail #4."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    from modules.subtitles.cue_pipeline import build_subtitle_cues_for_blocks

    # Trailing whitespace difference — normalize() collapses these to equal,
    # so the drift gate should NOT trigger; whisper SHOULD run.
    fake_words = [{"start_ms": 0, "end_ms": 800, "text": "你好"}]
    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        lambda *a, **kw: fake_words,
    )

    audio = tmp_path / "seg.wav"
    audio.write_bytes(b"fake-wav")
    block = _make_block("b1", "你好",
                        tts_input_cn_text="  你好  ",  # only whitespace diff
                        aligned_audio_path=str(audio),
                        first_start_ms=0, last_end_ms=1000,
                        target_duration_ms=1000)
    result = build_subtitle_cues_for_blocks([block], _make_subtitle_lines("你好"))
    # Treated as in-sync via normalize → whisper runs
    assert any("whisper" in c.source.lower() for c in result.cues)
