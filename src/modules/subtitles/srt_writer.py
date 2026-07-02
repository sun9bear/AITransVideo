"""SRT writer — serializes list[SubtitleCue] to SRT-format strings.

Three module-level functions: write_zh_srt / write_en_srt / write_bilingual_srt.

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §T7, §5.3.1.

Contract (from plan §8):
  - Writer does NOT re-segment. It only serializes canonical cues as-is.
  - No calls to segment_text, assign_timing, or any segmenter/builder logic.
  - Trailing punctuation is stripped per-cue for display (剪映/CapCut style).
    Only affects the SRT output layer; cue.text is UNCHANGED.

SRT format conventions (aligned with EditorPackageWriter._write_srt_file):
  - Blocks joined with "\n\n"; final block followed by "\n" (not "\n\n").
  - Empty cues (no displayable text after strip) are skipped.
  - Bilingual order: en first (line 1), zh second (line 2) — matches existing
    EditorPackageWriter which does f"{en_text}\n{zh_text}".
  - Bilingual with empty/whitespace en_text: write zh-only content line
    (Option A — no blank second line).
  - Time format: HH:MM:SS,mmm (comma, not period). Hours zero-padded to >=2 digits.
  - Indices 1-based, sequential across non-skipped cues.
  - Metadata fields (cue_id, needs_review, review_reason, speaker_id, etc.)
    are NOT written to SRT output.
"""

from __future__ import annotations

from modules.subtitles.cue_models import SubtitleCue


def legacy_zh_en_alias_files_enabled(target_language: str | None) -> bool:
    """Alias-honesty gate for the legacy language-named SRT filenames.

    ``subtitles_zh.srt`` always holds the dub (TARGET) subtitle and
    ``subtitles_en.srt`` the SOURCE (== ``write_zh_srt``/``write_en_srt``
    output — the "zh"/"en" in those function names is the same legacy
    naming), so the filenames only tell the truth for the GA default
    en->zh pair. For any non-zh dub target (e.g. zh->en) the writers must
    stop emitting them and expose only the script-neutral
    ``subtitles_source/target.srt`` — a zh->en job used to ship a
    ``subtitles_en.srt`` full of Chinese (2026-07-02 prod report,
    job b07c29cf0652411ca0a7e0461648dc7b).

    Same discriminator as the cue pipeline's whisper char-DTW bypass
    (``cue_pipeline.build_subtitle_cues_for_blocks``): ``None`` == legacy
    en->zh jobs without a stamp → byte-identical default behavior.
    """
    return target_language is None or target_language in ("zh-CN", "zh")

# ---------------------------------------------------------------------------
# Trailing-punctuation strip for display
# ---------------------------------------------------------------------------

# Characters that should be removed from the tail of a subtitle cue for display.
# Single-char set: rstrip() naturally handles multi-char sequences like ——/…… by
# stripping one character at a time until none of these remain at the tail.
# Whitespace is also stripped (covered by the explicit " \t\n" addition in the call).
_TRAILING_PUNCT_CHARS = "，,。.；;：:！!？?、—…"


def _strip_trailing_subtitle_punct(text: str) -> str:
    """Strip trailing punctuation from a subtitle cue text for display.

    Both single-char punct (',', '.', etc.) and multi-char sequences
    ('——', '……') are handled by repeatedly stripping any char in
    _TRAILING_PUNCT_CHARS until none remain at the tail. This naturally
    handles e.g. '真的吗?!' -> '真的吗' (strips both '!' and '?').

    Whitespace is also stripped from the tail.

    Only applied at the SRT serialization layer. cue.text is never modified.
    """
    return text.rstrip(_TRAILING_PUNCT_CHARS + " \t\n")


def _format_srt_time(ms: int) -> str:
    """Format milliseconds as SRT time string HH:MM:SS,mmm.

    Uses comma between seconds and milliseconds per SRT spec (not period).
    Hours are zero-padded to at least 2 digits.

    Raises ValueError for negative input.
    """
    if ms < 0:
        raise ValueError(f"ms must be non-negative, got {ms}")
    hours, remainder = divmod(ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _clean(text: str) -> str:
    """Strip whitespace and replace internal newlines with a space.

    Defensive against data that survived validation without being fully
    normalized. Newlines in SRT content would split into extra display lines
    and corrupt the block structure.
    """
    text = text.strip()
    if "\n" in text:
        text = text.replace("\n", " ")
    return text


def _build_block(idx: int, start_ms: int, end_ms: int, content: str) -> str:
    """Build one SRT block string (no trailing newline — caller joins)."""
    time_line = f"{_format_srt_time(start_ms)} --> {_format_srt_time(end_ms)}"
    return f"{idx}\n{time_line}\n{content}"


def write_zh_srt(cues: list[SubtitleCue]) -> str:
    """Serialize cues to Chinese-only SRT string.

    Trailing punctuation is stripped from each cue's display text
    (剪映/CapCut style). cue.text itself is not modified.
    Empty/whitespace cues are skipped. Returns "" for an empty cue list.
    Does NOT re-segment or modify timing.
    """
    if not cues:
        return ""
    blocks: list[str] = []
    idx = 1
    for cue in cues:
        text = _strip_trailing_subtitle_punct(_clean(cue.text))
        if not text:
            continue
        blocks.append(_build_block(idx, cue.start_ms, cue.end_ms, text))
        idx += 1
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


def write_en_srt(cues: list[SubtitleCue]) -> str:
    """Serialize cues to English-only SRT string.

    Trailing punctuation is stripped from each cue's display text.
    Empty/whitespace en_text cues are skipped. Returns "" for an empty cue list.
    Does NOT re-segment or modify timing.
    """
    if not cues:
        return ""
    blocks: list[str] = []
    idx = 1
    for cue in cues:
        text = _strip_trailing_subtitle_punct(_clean(cue.en_text))
        if not text:
            continue
        blocks.append(_build_block(idx, cue.start_ms, cue.end_ms, text))
        idx += 1
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


def write_bilingual_srt(cues: list[SubtitleCue]) -> str:
    """Serialize cues to bilingual SRT string.

    Content order: en_text on line 1, zh (cue.text) on line 2.
    This matches EditorPackageWriter._write_srt_file bilingual convention.

    Trailing punctuation is stripped from each cue's display text.
    Empty en_text handling (Option A): if en_text is empty/whitespace, write
    zh-only content line (no blank second line). Cues with empty zh text are
    skipped entirely (same policy as write_zh_srt).

    Returns "" for an empty cue list.
    Does NOT re-segment or modify timing.
    """
    if not cues:
        return ""
    blocks: list[str] = []
    idx = 1
    for cue in cues:
        zh = _strip_trailing_subtitle_punct(_clean(cue.text))
        en = _strip_trailing_subtitle_punct(_clean(cue.en_text))
        if not zh:
            # No zh text — skip entirely (zh is the anchor for bilingual cues)
            continue
        if en:
            content = f"{en}\n{zh}"
        else:
            # Option A: en absent → zh-only line
            content = zh
        blocks.append(_build_block(idx, cue.start_ms, cue.end_ms, content))
        idx += 1
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"
