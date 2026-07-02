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

def _make_output(
    tmp_path: Path,
    *,
    cues: list[SubtitleCue] | None = None,
    target_language: str | None = None,
) -> ProjectOutput:
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
    if target_language is not None:
        kwargs["target_language"] = target_language
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

    zh_c, en_c, bi_c = writer._write_srt(output_cues)[:3]  # PR-F 5-tuple
    zh_s, en_s, bi_s = writer._write_srt(output_segs)[:3]

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

    zh_path, _, _ = writer._write_srt(output)[:3]  # PR-F: _write_srt now returns a 5-tuple (+source/target)

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

    _, en_path, _ = writer._write_srt(output)[:3]  # PR-F: _write_srt now returns a 5-tuple (+source/target)

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

    _, _, bi_path = writer._write_srt(output)[:3]  # PR-F: _write_srt now returns a 5-tuple (+source/target)

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

    zh_path, _, _ = writer._write_srt(output)[:3]  # PR-F: _write_srt now returns a 5-tuple (+source/target)

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

    zh_path, en_path, bi_path = writer._write_srt(output)[:3]  # PR-F: _write_srt now returns a 5-tuple (+source/target)

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

    zh_path, en_path, _ = writer._write_srt(output)[:3]  # PR-F: _write_srt now returns a 5-tuple (+source/target)

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

    zh_path, _, _ = writer._write_srt(output)[:3]  # PR-F: _write_srt now returns a 5-tuple (+source/target)

    compat_path = Path(output.output_dir) / "output" / "subtitles.srt"
    assert compat_path.read_text(encoding="utf-8") == Path(zh_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# PR-F: script-neutral source/target SRT (additive; default byte-identical)
# ---------------------------------------------------------------------------

def test_prf_source_target_srt_written_for_canonical_path(tmp_path: Path) -> None:
    """Canonical cue path also writes subtitles_target.srt (== zh, the dub/TARGET) and
    subtitles_source.srt (== en, the SOURCE). Additive: the legacy zh/en files are
    unchanged, so the GA default stays byte-identical."""
    cue = _make_cue(text="目标语言字幕", en_text="Source language subtitle")
    output = _make_output(tmp_path, cues=[cue])
    writer = EditorPackageWriter()

    zh_path, en_path, _ = writer._write_srt(output)[:3]  # PR-F: _write_srt now returns a 5-tuple (+source/target)

    out_dir = Path(output.output_dir) / "output"
    target_path = out_dir / "subtitles_target.srt"
    source_path = out_dir / "subtitles_source.srt"
    assert target_path.exists() and source_path.exists()
    # target == zh file (cue.text), source == en file (cue.en_text)
    assert target_path.read_text(encoding="utf-8") == Path(zh_path).read_text(encoding="utf-8")
    assert source_path.read_text(encoding="utf-8") == Path(en_path).read_text(encoding="utf-8")


def test_prf_source_target_srt_written_for_segment_path(tmp_path: Path) -> None:
    """Segment fallback path (empty cues) also writes the script-neutral source/target
    SRT mirroring zh/en."""
    output = _make_output(tmp_path, cues=[])
    writer = EditorPackageWriter()

    zh_path, en_path, _ = writer._write_srt(output)[:3]  # PR-F: _write_srt now returns a 5-tuple (+source/target)

    out_dir = Path(output.output_dir) / "output"
    target_path = out_dir / "subtitles_target.srt"
    source_path = out_dir / "subtitles_source.srt"
    assert target_path.exists() and source_path.exists()
    assert target_path.read_text(encoding="utf-8") == Path(zh_path).read_text(encoding="utf-8")
    assert source_path.read_text(encoding="utf-8") == Path(en_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Alias honesty (2026-07-02): non-zh dub target suppresses the legacy
# subtitles_zh/en.srt filenames — a zh->en job used to ship subtitles_en.srt
# full of Chinese source text (prod job b07c29cf0652411ca0a7e0461648dc7b).
# ---------------------------------------------------------------------------

def test_zh_en_pair_suppresses_legacy_alias_files_canonical_path(tmp_path: Path) -> None:
    """target_language='en' (zh->en): no subtitles_zh/en.srt on disk; the returned
    zh/en role slots carry the script-neutral target/source paths with the correct
    language content (target=English cue.text, source=Chinese cue.en_text)."""
    cue = _make_cue(text="English dub line", en_text="中文源文台词")
    output = _make_output(tmp_path, cues=[cue], target_language="en")
    writer = EditorPackageWriter()

    zh_slot, en_slot, _, source_path, target_path = writer._write_srt(output)

    out_dir = Path(output.output_dir) / "output"
    assert not (out_dir / "subtitles_zh.srt").exists()
    assert not (out_dir / "subtitles_en.srt").exists()
    # Role slots redirect to the honestly-named neutral files.
    assert Path(zh_slot).name == "subtitles_target.srt"
    assert Path(en_slot).name == "subtitles_source.srt"
    assert zh_slot == target_path
    assert en_slot == source_path
    # Language content is correct: target=English, source=Chinese.
    assert "English dub line" in Path(target_path).read_text(encoding="utf-8")
    assert "中文源文台词" in Path(source_path).read_text(encoding="utf-8")
    # subtitles.srt compat copy stays the TARGET subtitle.
    compat = (out_dir / "subtitles.srt").read_text(encoding="utf-8")
    assert compat == Path(target_path).read_text(encoding="utf-8")


def test_zh_en_pair_suppresses_legacy_alias_files_segment_path(tmp_path: Path) -> None:
    """Segment fallback path honors the same alias gate."""
    output = _make_output(tmp_path, cues=[], target_language="en")
    writer = EditorPackageWriter()

    zh_slot, en_slot, _, source_path, target_path = writer._write_srt(output)

    out_dir = Path(output.output_dir) / "output"
    assert not (out_dir / "subtitles_zh.srt").exists()
    assert not (out_dir / "subtitles_en.srt").exists()
    assert Path(zh_slot).name == "subtitles_target.srt"
    assert Path(en_slot).name == "subtitles_source.srt"
    assert zh_slot == target_path and en_slot == source_path


def test_zh_en_pair_removes_stale_alias_files(tmp_path: Path) -> None:
    """A pre-fix run left wrong-language alias files on disk; a re-run (e.g.
    whisper regenerate / commit) must remove them, not leave stale lies behind."""
    cue = _make_cue(text="English dub line", en_text="中文源文台词")
    output = _make_output(tmp_path, cues=[cue], target_language="en")
    out_dir = Path(output.output_dir) / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "subtitles_zh.srt").write_text("stale 英文内容", encoding="utf-8")
    (out_dir / "subtitles_en.srt").write_text("stale 中文内容", encoding="utf-8")

    EditorPackageWriter()._write_srt(output)

    assert not (out_dir / "subtitles_zh.srt").exists()
    assert not (out_dir / "subtitles_en.srt").exists()


def test_default_pair_keeps_legacy_alias_files(tmp_path: Path) -> None:
    """GA default (no target_language / zh-CN): byte-identical legacy behavior —
    zh/en alias files written, role slots point at them."""
    cue = _make_cue(text="中文配音台词", en_text="English source line")
    for lang in (None, "zh-CN", "zh"):
        sub = tmp_path / (lang or "none")
        output = _make_output(sub, cues=[cue], target_language=lang)
        zh_slot, en_slot, *_ = EditorPackageWriter()._write_srt(output)
        out_dir = Path(output.output_dir) / "output"
        assert Path(zh_slot).name == "subtitles_zh.srt"
        assert Path(en_slot).name == "subtitles_en.srt"
        assert (out_dir / "subtitles_zh.srt").read_text(encoding="utf-8") == (
            out_dir / "subtitles_target.srt"
        ).read_text(encoding="utf-8")
        assert (out_dir / "subtitles_en.srt").read_text(encoding="utf-8") == (
            out_dir / "subtitles_source.srt"
        ).read_text(encoding="utf-8")
