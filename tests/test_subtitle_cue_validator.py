"""Tests for SubtitleCueValidator (T6).

16 scenarios covering hard errors, review items, status determination,
BlockSummary aggregation, order stability, and edge cases.

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §8
"""

import pytest

from modules.subtitles.cue_models import SubtitleCue
from modules.subtitles.cue_validator import (
    BlockSpec,
    ValidationIssue,
    ValidationReport,
    validate_cues,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cue(
    cue_id: str,
    block_id: str,
    text: str,
    start_ms: int,
    end_ms: int,
    needs_review: bool = False,
    review_reason: str | None = None,
) -> SubtitleCue:
    return SubtitleCue(
        cue_id=cue_id,
        block_id=block_id,
        speaker_id="spk_1",
        speaker_name=None,
        text=text,
        en_text="",
        start_ms=start_ms,
        end_ms=end_ms,
        source="test",
        needs_review=needs_review,
        review_reason=review_reason,
    )


def _make_spec(
    block_id: str,
    merged_cn_text: str,
    start_ms: int,
    end_ms: int,
) -> BlockSpec:
    return BlockSpec(
        block_id=block_id,
        merged_cn_text=merged_cn_text,
        start_ms=start_ms,
        end_ms=end_ms,
    )


def _issues_by_code(report: ValidationReport, code: str) -> list[ValidationIssue]:
    return [i for i in report.issues if i.code == code]


# ---------------------------------------------------------------------------
# Scenario 1 — text_mismatch: partial cue coverage
# ---------------------------------------------------------------------------


def test_text_mismatch_partial_coverage() -> None:
    """Block has two sentences; cues only cover the first — text_mismatch."""
    spec = _make_spec("blk_01", "今天很好。明天也好。", 0, 5000)
    cue = _make_cue("blk_01_cue_01", "blk_01", "今天很好。", 0, 2500)

    report = validate_cues(block_specs=[spec], cues=[cue])

    assert report.validation_status == "failed"
    errors = _issues_by_code(report, "text_mismatch")
    assert len(errors) == 1
    assert errors[0].block_id == "blk_01"
    assert errors[0].cue_id is None  # block-level
    assert errors[0].severity == "error"


# ---------------------------------------------------------------------------
# Scenario 2 — timing_overlap: cue 2 starts before cue 1 ends
# ---------------------------------------------------------------------------


def test_timing_overlap_within_block() -> None:
    """Cue 1 [0,1500], cue 2 [1000,2000] → timing_overlap on cue 2."""
    spec = _make_spec("blk_02", "你好世界。", 0, 5000)
    cue1 = _make_cue("blk_02_cue_01", "blk_02", "你好", 0, 1500)
    cue2 = _make_cue("blk_02_cue_02", "blk_02", "世界。", 1000, 2000)

    report = validate_cues(block_specs=[spec], cues=[cue1, cue2])

    assert report.validation_status == "failed"
    errors = _issues_by_code(report, "timing_overlap")
    assert len(errors) == 1
    assert errors[0].cue_id == "blk_02_cue_02"  # later cue
    assert errors[0].severity == "error"


# ---------------------------------------------------------------------------
# Scenario 3 — timing_out_of_block: cue entirely after block end
# ---------------------------------------------------------------------------


def test_timing_out_of_block_after_end() -> None:
    """Block [0,5000], cue [5500,6000] — cue is entirely outside block."""
    spec = _make_spec("blk_03", "早上好。", 0, 5000)
    cue = _make_cue("blk_03_cue_01", "blk_03", "早上好。", 5500, 6000)

    report = validate_cues(block_specs=[spec], cues=[cue])

    assert report.validation_status == "failed"
    errors = _issues_by_code(report, "timing_out_of_block")
    assert len(errors) == 1
    assert errors[0].cue_id == "blk_03_cue_01"
    assert errors[0].severity == "error"


# ---------------------------------------------------------------------------
# Scenario 4 — timing_out_of_block: cue starts before block start
# ---------------------------------------------------------------------------


def test_timing_out_of_block_starts_before() -> None:
    """Block [1000,5000], cue [500,1500] — cue starts before block."""
    spec = _make_spec("blk_04", "很开心。", 1000, 5000)
    cue = _make_cue("blk_04_cue_01", "blk_04", "很开心。", 500, 1500)

    report = validate_cues(block_specs=[spec], cues=[cue])

    assert report.validation_status == "failed"
    errors = _issues_by_code(report, "timing_out_of_block")
    assert len(errors) == 1
    assert errors[0].cue_id == "blk_04_cue_01"
    assert errors[0].severity == "error"


# ---------------------------------------------------------------------------
# Scenario 5 — empty_cue: whitespace-only text (SubtitleCue strips in __post_init__)
# ---------------------------------------------------------------------------


def test_empty_cue_whitespace_becomes_empty() -> None:
    """Input text '   ' → post_init strips → text='' → empty_cue error."""
    spec = _make_spec("blk_05", "测试。", 0, 3000)
    # SubtitleCue.__post_init__ will strip "   " to ""
    cue = _make_cue("blk_05_cue_01", "blk_05", "   ", 0, 1500)
    assert cue.text == ""  # confirm post-init effect

    report = validate_cues(block_specs=[spec], cues=[cue])

    assert report.validation_status == "failed"
    errors = _issues_by_code(report, "empty_cue")
    assert len(errors) == 1
    assert errors[0].cue_id == "blk_05_cue_01"
    assert errors[0].severity == "error"


# ---------------------------------------------------------------------------
# Scenario 6 — unknown_block: cue block_id not in any BlockSpec
# ---------------------------------------------------------------------------


def test_unknown_block_cue_has_no_matching_spec() -> None:
    """Cue references block 'blk_99' which has no BlockSpec."""
    spec = _make_spec("blk_06", "测试。", 0, 3000)
    cue = _make_cue("blk_99_cue_01", "blk_99", "测试。", 0, 1500)

    report = validate_cues(block_specs=[spec], cues=[cue])

    assert report.validation_status == "failed"
    errors = _issues_by_code(report, "unknown_block")
    assert len(errors) == 1
    assert errors[0].block_id == "blk_99"
    assert errors[0].cue_id is None  # block-level
    assert errors[0].severity == "error"


# ---------------------------------------------------------------------------
# Scenario 7 — long_unbreakable_text review propagation
# ---------------------------------------------------------------------------


def test_long_unbreakable_text_review_propagated() -> None:
    """Cue with review_reason='long_unbreakable_text' → review issue surfaced."""
    spec = _make_spec("blk_07", "这是一段非常非常长的文本。", 0, 5000)
    cue = _make_cue(
        "blk_07_cue_01",
        "blk_07",
        "这是一段非常非常长的文本。",
        0,
        5000,
        needs_review=True,
        review_reason="long_unbreakable_text",
    )

    report = validate_cues(block_specs=[spec], cues=[cue])

    assert report.validation_status == "needs_review"
    review_issues = _issues_by_code(report, "long_unbreakable_text")
    assert len(review_issues) == 1
    assert review_issues[0].cue_id == "blk_07_cue_01"
    assert review_issues[0].severity == "review"


# ---------------------------------------------------------------------------
# Scenario 8 — unknown_mixed_token review propagation
# ---------------------------------------------------------------------------


def test_unknown_mixed_token_review_propagated() -> None:
    """Cue with review_reason='unknown_mixed_token' → review issue surfaced."""
    spec = _make_spec("blk_08", "参见http://example.com说明。", 0, 4000)
    cue = _make_cue(
        "blk_08_cue_01",
        "blk_08",
        "参见http://example.com说明。",
        0,
        4000,
        needs_review=True,
        review_reason="unknown_mixed_token",
    )

    report = validate_cues(block_specs=[spec], cues=[cue])

    assert report.validation_status == "needs_review"
    review_issues = _issues_by_code(report, "unknown_mixed_token")
    assert len(review_issues) == 1
    assert review_issues[0].cue_id == "blk_08_cue_01"
    assert review_issues[0].severity == "review"


# ---------------------------------------------------------------------------
# Scenario 9 — text_audio_may_need_review propagation-only
# ---------------------------------------------------------------------------


def test_text_audio_may_need_review_propagated() -> None:
    """Cue with review_reason='text_audio_may_need_review' → review issue surfaced.

    Validator does NOT infer this — it only propagates it from cue.review_reason.
    The caller (T9 OutputDispatcher) is responsible for setting this flag.
    """
    spec = _make_spec("blk_09", "修改后的文本。", 0, 3000)
    cue = _make_cue(
        "blk_09_cue_01",
        "blk_09",
        "修改后的文本。",
        0,
        3000,
        needs_review=True,
        review_reason="text_audio_may_need_review",
    )

    report = validate_cues(block_specs=[spec], cues=[cue])

    assert report.validation_status == "needs_review"
    review_issues = _issues_by_code(report, "text_audio_may_need_review")
    assert len(review_issues) == 1
    assert review_issues[0].cue_id == "blk_09_cue_01"
    assert review_issues[0].severity == "review"


# ---------------------------------------------------------------------------
# Scenario 10 — short_display_duration: cue duration < min_display_ms
# ---------------------------------------------------------------------------


def test_short_display_duration_below_threshold() -> None:
    """Cue duration 300ms < 500ms threshold → short_display_duration review."""
    spec = _make_spec("blk_10", "好的。", 0, 3000)
    cue = _make_cue("blk_10_cue_01", "blk_10", "好的。", 0, 300)

    report = validate_cues(block_specs=[spec], cues=[cue], min_display_ms=500)

    assert report.validation_status == "needs_review"
    review_issues = _issues_by_code(report, "short_display_duration")
    assert len(review_issues) == 1
    assert review_issues[0].cue_id == "blk_10_cue_01"
    assert review_issues[0].severity == "review"


# ---------------------------------------------------------------------------
# Scenario 11 — status=passed: no issues
# ---------------------------------------------------------------------------


def test_status_passed_no_issues() -> None:
    """Clean cue matching block → passed."""
    spec = _make_spec("blk_11", "今天天气不错。", 0, 4000)
    cue = _make_cue("blk_11_cue_01", "blk_11", "今天天气不错。", 0, 4000)

    report = validate_cues(block_specs=[spec], cues=[cue])

    assert report.validation_status == "passed"
    assert report.issues == []


# ---------------------------------------------------------------------------
# Scenario 12 — status=needs_review: only review issues, no errors
# ---------------------------------------------------------------------------


def test_status_needs_review_only_review_issues() -> None:
    """A short-duration cue with review_reason but no hard errors → needs_review."""
    spec = _make_spec("blk_12", "好。", 0, 3000)
    cue = _make_cue(
        "blk_12_cue_01",
        "blk_12",
        "好。",
        0,
        400,  # below 500ms threshold
        needs_review=True,
        review_reason="long_unbreakable_text",  # also has review_reason
    )

    report = validate_cues(block_specs=[spec], cues=[cue], min_display_ms=500)

    assert report.validation_status == "needs_review"
    assert all(i.severity == "review" for i in report.issues)


# ---------------------------------------------------------------------------
# Scenario 13 — status=failed: error + review issues mixed
# ---------------------------------------------------------------------------


def test_status_failed_overrides_review() -> None:
    """One error issue + one review issue → status is still 'failed'."""
    spec = _make_spec("blk_13", "第一句。第二句。", 0, 5000)
    # text_mismatch: cues only have first sentence
    # short_display_duration: 300ms < 500ms
    cue = _make_cue("blk_13_cue_01", "blk_13", "第一句。", 0, 300)

    report = validate_cues(block_specs=[spec], cues=[cue], min_display_ms=500)

    assert report.validation_status == "failed"
    error_issues = [i for i in report.issues if i.severity == "error"]
    review_issues = [i for i in report.issues if i.severity == "review"]
    assert len(error_issues) >= 1
    assert len(review_issues) >= 1


# ---------------------------------------------------------------------------
# Scenario 14 — BlockSummary aggregation across two blocks
# ---------------------------------------------------------------------------


def test_block_summary_aggregation_multi_block() -> None:
    """Two blocks with mixed issues — verify BlockSummary per-issue counts."""
    # Block A: 2 cues, timing overlap on second
    spec_a = _make_spec("blk_a", "你好世界。", 0, 5000)
    cue_a1 = _make_cue("blk_a_cue_01", "blk_a", "你好", 0, 1500)
    cue_a2 = _make_cue("blk_a_cue_02", "blk_a", "世界。", 1000, 2500)  # overlaps cue_a1

    # Block B: 2 cues, one short display duration, text matches
    spec_b = _make_spec("blk_b", "再见啊。", 3000, 8000)
    cue_b1 = _make_cue("blk_b_cue_01", "blk_b", "再见", 3000, 3200)  # 200ms < 500ms
    cue_b2 = _make_cue("blk_b_cue_02", "blk_b", "啊。", 3200, 5000)

    report = validate_cues(
        block_specs=[spec_a, spec_b],
        cues=[cue_a1, cue_a2, cue_b1, cue_b2],
        min_display_ms=500,
    )

    # Find summaries by block_id
    summary_map = {s.block_id: s for s in report.block_summaries}

    assert "blk_a" in summary_map
    assert "blk_b" in summary_map

    sa = summary_map["blk_a"]
    assert sa.cue_count == 2
    assert sa.timing_overlap_count == 1
    assert sa.short_display_duration_count == 0

    sb = summary_map["blk_b"]
    assert sb.cue_count == 2
    assert sb.timing_overlap_count == 0
    assert sb.short_display_duration_count == 1


# ---------------------------------------------------------------------------
# Scenario 15 — BlockSummary order stability
# ---------------------------------------------------------------------------


def test_block_summary_order_matches_input_block_specs() -> None:
    """BlockSummaries appear in same order as block_specs input."""
    spec_x = _make_spec("blk_x", "测试一。", 0, 3000)
    spec_y = _make_spec("blk_y", "测试二。", 3000, 6000)
    spec_z = _make_spec("blk_z", "测试三。", 6000, 9000)

    cue_x = _make_cue("blk_x_cue_01", "blk_x", "测试一。", 0, 3000)
    cue_y = _make_cue("blk_y_cue_01", "blk_y", "测试二。", 3000, 6000)
    cue_z = _make_cue("blk_z_cue_01", "blk_z", "测试三。", 6000, 9000)

    report = validate_cues(
        block_specs=[spec_x, spec_y, spec_z],
        cues=[cue_z, cue_x, cue_y],  # deliberately shuffled cues
    )

    ids = [s.block_id for s in report.block_summaries]
    assert ids == ["blk_x", "blk_y", "blk_z"]


# ---------------------------------------------------------------------------
# Scenario 16 — Empty inputs
# ---------------------------------------------------------------------------


def test_empty_inputs_returns_passed() -> None:
    """validate_cues(block_specs=[], cues=[]) → passed, no issues, empty summaries."""
    report = validate_cues(block_specs=[], cues=[])

    assert report.validation_status == "passed"
    assert report.issues == []
    assert report.block_summaries == []


# ---------------------------------------------------------------------------
# Additional edge: block with spec but no cues → text_mismatch if non-empty
# ---------------------------------------------------------------------------


def test_block_with_no_cues_non_empty_text_gives_mismatch() -> None:
    """A BlockSpec with non-empty merged_cn_text but zero cues → text_mismatch."""
    spec = _make_spec("blk_e", "有文字但没有cue。", 0, 4000)

    report = validate_cues(block_specs=[spec], cues=[])

    assert report.validation_status == "failed"
    errors = _issues_by_code(report, "text_mismatch")
    assert len(errors) == 1
    assert errors[0].block_id == "blk_e"
    assert errors[0].cue_id is None


# ---------------------------------------------------------------------------
# Additional edge: block with spec but no cues, empty text → passed
# ---------------------------------------------------------------------------


def test_block_with_no_cues_empty_text_passes() -> None:
    """A BlockSpec with empty merged_cn_text and zero cues → no text_mismatch."""
    spec = _make_spec("blk_f", "   ", 0, 4000)  # whitespace-only

    report = validate_cues(block_specs=[spec], cues=[])

    # normalize("") == normalize("   ") → both become "" → no mismatch
    assert report.validation_status == "passed"
    assert not _issues_by_code(report, "text_mismatch")
