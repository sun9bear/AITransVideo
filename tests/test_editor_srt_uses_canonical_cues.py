"""Tests for T8: EditorPackageWriter routing between canonical-cue SRT path
and legacy segment-based fallback path.

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md (T8, Phase 1a).

Coverage:
1.  subtitle_cues empty -> fallback (segment) path used
2.  subtitle_cues non-empty -> canonical path used
3.  Filenames identical between both paths
4.  zh SRT content from cues matches write_zh_srt output
5.  en SRT content from cues matches write_en_srt output
6.  Bilingual SRT content from cues matches write_bilingual_srt output
7.  _build_subtitle_slices NOT called when cues are present
8.  Segments + cues both present -> cues take precedence
9.  Single-cue block -> one SRT entry
10. Cross-hour timing (end_ms > 3_600_000) -> correct SRT time
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from modules.output.editor.editor_package_models import AlignedSegment, ProjectOutput
from modules.output.editor.editor_package_writer import EditorPackageWriter
from modules.subtitles.cue_models import SubtitleCue
from modules.subtitles.srt_writer import (
    write_bilingual_srt,
    write_en_srt,
    write_zh_srt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_output(tmp_path: Path, *, cues: list[SubtitleCue] | None = None) -> ProjectOutput:
    """Build a minimal ProjectOutput with one segment, optionally with cues."""
    # Create a tiny but syntactically valid WAV (RIFF header + minimal data).
    # EditorPackageWriter._copy_segment_files checks file existence; tests
    # that don't reach that code path skip or mock it.
    tmp_path.mkdir(parents=True, exist_ok=True)
    segment_wav = tmp_path / "seg.wav"
    segment_wav.write_bytes(
        b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
        b"@\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
    )
    segment = AlignedSegment(
        segment_id=1,
        speaker_id="speaker_a",
        display_name="Speaker A",
        start_ms=0,
        end_ms=2_000,
        cn_text="你好世界。",
        en_text="Hello world.",
        aligned_audio_path=str(segment_wav),
        actual_duration_ms=2_000,
        alignment_method="direct",
        needs_review=False,
    )
    kwargs: dict = dict(
        project_id="test_project",
        youtube_url="",
        video_title="Test Video",
        total_duration_ms=4_000,
        segments=[segment],
        output_dir=str(tmp_path),
    )
    if cues is not None:
        kwargs["subtitle_cues"] = cues
    return ProjectOutput(**kwargs)


def _make_cue(
    *,
    cue_id: str = "blk001_c01",
    block_id: str = "blk001",
    speaker_id: str = "speaker_a",
    text: str = "你好世界",
    en_text: str = "Hello world",
    start_ms: int = 0,
    end_ms: int = 2_000,
) -> SubtitleCue:
    return SubtitleCue(
        cue_id=cue_id,
        block_id=block_id,
        speaker_id=speaker_id,
        speaker_name="Speaker A",
        text=text,
        en_text=en_text,
        start_ms=start_ms,
        end_ms=end_ms,
        source="semantic_block_v2",
    )


def _call_write_srt_only(writer: EditorPackageWriter, output: ProjectOutput, tmp_path: Path) -> tuple[str, str, str]:
    """Call only _write_srt (not the full write() which requires ffmpeg)."""
    output_root = tmp_path / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    return writer._write_srt(output)


# ---------------------------------------------------------------------------
# Test 1: empty subtitle_cues -> fallback (segment) path
# ---------------------------------------------------------------------------

def test_empty_cues_uses_segment_fallback_path(tmp_path: Path) -> None:
    """When subtitle_cues is empty (default), _write_srt_from_segments is called."""
    output = _make_output(tmp_path, cues=[])
    writer = EditorPackageWriter()

    with patch.object(writer, "_write_srt_from_segments", wraps=writer._write_srt_from_segments) as mock_seg:
        with patch.object(writer, "_write_srt_from_canonical_cues", wraps=writer._write_srt_from_canonical_cues) as mock_cue:
            writer._write_srt(output)

    mock_seg.assert_called_once()
    mock_cue.assert_not_called()


def test_no_cues_field_uses_segment_fallback_path(tmp_path: Path) -> None:
    """When subtitle_cues is not passed (defaults to []), fallback path is used."""
    # Construct without passing subtitle_cues at all — relies on default_factory=list
    output = ProjectOutput(
        project_id="test_project",
        youtube_url="",
        video_title="Test Video",
        total_duration_ms=2_000,
        segments=[],
        output_dir=str(tmp_path),
    )
    writer = EditorPackageWriter()

    with patch.object(writer, "_write_srt_from_segments", wraps=writer._write_srt_from_segments) as mock_seg:
        with patch.object(writer, "_write_srt_from_canonical_cues", wraps=writer._write_srt_from_canonical_cues) as mock_cue:
            writer._write_srt(output)

    mock_seg.assert_called_once()
    mock_cue.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: non-empty subtitle_cues -> canonical path
# ---------------------------------------------------------------------------

def test_non_empty_cues_uses_canonical_path(tmp_path: Path) -> None:
    """When subtitle_cues is non-empty, _write_srt_from_canonical_cues is called."""
    cues = [_make_cue()]
    output = _make_output(tmp_path, cues=cues)
    writer = EditorPackageWriter()

    with patch.object(writer, "_write_srt_from_canonical_cues", wraps=writer._write_srt_from_canonical_cues) as mock_cue:
        with patch.object(writer, "_write_srt_from_segments", wraps=writer._write_srt_from_segments) as mock_seg:
            writer._write_srt(output)

    mock_cue.assert_called_once()
    mock_seg.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: filenames identical between both paths
# ---------------------------------------------------------------------------

def test_filenames_identical_between_paths(tmp_path: Path) -> None:
    """Both canonical and segment paths produce SRT files with identical names."""
    cues = [_make_cue()]
    output_cues = _make_output(tmp_path / "with_cues", cues=cues)
    output_segs = _make_output(tmp_path / "with_segs", cues=[])
    writer = EditorPackageWriter()

    zh_c, en_c, bi_c = writer._write_srt(output_cues)
    zh_s, en_s, bi_s = writer._write_srt(output_segs)

    assert Path(zh_c).name == Path(zh_s).name == "subtitles_zh.srt"
    assert Path(en_c).name == Path(en_s).name == "subtitles_en.srt"
    assert Path(bi_c).name == Path(bi_s).name == "subtitles_bilingual.srt"


# ---------------------------------------------------------------------------
# Test 4: zh SRT from cues matches write_zh_srt output
# ---------------------------------------------------------------------------

def test_zh_srt_content_matches_write_zh_srt(tmp_path: Path) -> None:
    """zh SRT file content matches what write_zh_srt would produce."""
    cue = _make_cue(text="你好世界", start_ms=0, end_ms=2_000)
    output = _make_output(tmp_path, cues=[cue])
    writer = EditorPackageWriter()

    zh_path, _, _ = writer._write_srt(output)

    actual_content = Path(zh_path).read_text(encoding="utf-8")
    expected_content = write_zh_srt([cue])
    assert actual_content == expected_content


# ---------------------------------------------------------------------------
# Test 5: en SRT from cues matches write_en_srt output
# ---------------------------------------------------------------------------

def test_en_srt_content_matches_write_en_srt(tmp_path: Path) -> None:
    """en SRT file content matches what write_en_srt would produce."""
    cue = _make_cue(en_text="Hello world", start_ms=500, end_ms=2_500)
    output = _make_output(tmp_path, cues=[cue])
    writer = EditorPackageWriter()

    _, en_path, _ = writer._write_srt(output)

    actual_content = Path(en_path).read_text(encoding="utf-8")
    expected_content = write_en_srt([cue])
    assert actual_content == expected_content


# ---------------------------------------------------------------------------
# Test 6: Bilingual SRT from cues matches write_bilingual_srt output
# ---------------------------------------------------------------------------

def test_bilingual_srt_content_matches_write_bilingual_srt(tmp_path: Path) -> None:
    """Bilingual SRT file content matches what write_bilingual_srt would produce."""
    cue = _make_cue(text="你好世界", en_text="Hello world", start_ms=100, end_ms=2_100)
    output = _make_output(tmp_path, cues=[cue])
    writer = EditorPackageWriter()

    _, _, bi_path = writer._write_srt(output)

    actual_content = Path(bi_path).read_text(encoding="utf-8")
    expected_content = write_bilingual_srt([cue])
    assert actual_content == expected_content


# ---------------------------------------------------------------------------
# Test 7: _build_subtitle_slices NOT called when cues are present
# ---------------------------------------------------------------------------

def test_build_subtitle_slices_not_called_when_cues_present(tmp_path: Path) -> None:
    """When cues are non-empty, _build_subtitle_slices is never invoked."""
    cues = [_make_cue()]
    output = _make_output(tmp_path, cues=cues)
    writer = EditorPackageWriter()

    with patch.object(writer, "_build_subtitle_slices") as mock_slices:
        writer._write_srt(output)

    mock_slices.assert_not_called()


# ---------------------------------------------------------------------------
# Test 8: Segments + cues both present -> cues take precedence
# ---------------------------------------------------------------------------

def test_cues_take_precedence_when_both_present(tmp_path: Path) -> None:
    """Even with segments populated, non-empty cues take precedence."""
    cues = [_make_cue(text="仅出现在字幕cue中", en_text="Only in cue")]
    output = _make_output(tmp_path, cues=cues)
    # The output already has a segment with cn_text="你好世界。"
    assert len(output.segments) == 1
    assert output.segments[0].cn_text == "你好世界。"
    writer = EditorPackageWriter()

    zh_path, _, _ = writer._write_srt(output)

    content = Path(zh_path).read_text(encoding="utf-8")
    # Cue text should appear
    assert "仅出现在字幕cue中" in content
    # Segment cn_text should NOT appear (cue path was taken)
    assert "你好世界" not in content


# ---------------------------------------------------------------------------
# Test 9: Single-cue block -> one SRT entry
# ---------------------------------------------------------------------------

def test_single_cue_produces_one_srt_entry(tmp_path: Path) -> None:
    """A single SubtitleCue produces exactly one numbered block in each SRT."""
    cue = _make_cue(text="单句测试", en_text="Single sentence", start_ms=1_000, end_ms=3_000)
    output = _make_output(tmp_path, cues=[cue])
    writer = EditorPackageWriter()

    zh_path, en_path, bi_path = writer._write_srt(output)

    zh_content = Path(zh_path).read_text(encoding="utf-8")
    en_content = Path(en_path).read_text(encoding="utf-8")
    bi_content = Path(bi_path).read_text(encoding="utf-8")

    # SRT index 1 should appear; index 2 should not
    assert zh_content.startswith("1\n")
    assert "2\n" not in zh_content
    assert en_content.startswith("1\n")
    assert "2\n" not in en_content
    assert bi_content.startswith("1\n")
    assert "2\n" not in bi_content

    # Text content check
    assert "单句测试" in zh_content
    assert "Single sentence" in en_content
    assert "Single sentence" in bi_content
    assert "单句测试" in bi_content


# ---------------------------------------------------------------------------
# Test 10: Cross-hour timing (end_ms > 3_600_000)
# ---------------------------------------------------------------------------

def test_cross_hour_timing_produces_correct_srt_time(tmp_path: Path) -> None:
    """Cue with end_ms > 3_600_000 produces correct HH:MM:SS,mmm in SRT."""
    # 1h 2m 3.456s = 3_723_456 ms
    cue = _make_cue(
        text="跨小时测试",
        en_text="Cross-hour test",
        start_ms=3_600_000,   # exactly 01:00:00,000
        end_ms=3_723_456,     # 01:02:03,456
    )
    output = _make_output(tmp_path, cues=[cue])
    writer = EditorPackageWriter()

    zh_path, en_path, _ = writer._write_srt(output)

    zh_content = Path(zh_path).read_text(encoding="utf-8")
    en_content = Path(en_path).read_text(encoding="utf-8")

    assert "01:00:00,000 --> 01:02:03,456" in zh_content
    assert "01:00:00,000 --> 01:02:03,456" in en_content


# ---------------------------------------------------------------------------
# Additional regression: compat subtitles.srt written for cue path too
# ---------------------------------------------------------------------------

def test_compat_subtitles_srt_written_for_canonical_path(tmp_path: Path) -> None:
    """subtitles.srt (zh copy) is also written when canonical cue path is taken."""
    cue = _make_cue(text="兼容性测试", en_text="Compat test")
    output = _make_output(tmp_path, cues=[cue])
    writer = EditorPackageWriter()

    writer._write_srt(output)

    compat_path = Path(output.output_dir) / "output" / "subtitles.srt"
    assert compat_path.exists()
    assert "兼容性测试" in compat_path.read_text(encoding="utf-8")


def test_compat_subtitles_srt_matches_zh_for_canonical_path(tmp_path: Path) -> None:
    """subtitles.srt is a copy of subtitles_zh.srt for the canonical cue path."""
    cue = _make_cue(text="内容一致性", en_text="Content consistency")
    output = _make_output(tmp_path, cues=[cue])
    writer = EditorPackageWriter()

    zh_path, _, _ = writer._write_srt(output)

    compat_path = Path(output.output_dir) / "output" / "subtitles.srt"
    assert compat_path.read_text(encoding="utf-8") == Path(zh_path).read_text(encoding="utf-8")
