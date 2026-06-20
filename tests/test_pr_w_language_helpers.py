"""PR-W: language-aware process.py helpers (plan 2026-06-13 v3, Phase 4/5).

Paired coverage (default pair unchanged + zh->en new behavior) for the
language-aware edit points landed in PR-W:

* ``_minimax_language_matches_target`` — voice-pool de-Chinese predicate;
* ``_count_source_words`` — script-aware probe unit counting;
* ``_select_probe_segments`` — CJK source yields candidates (no silent empty);
* failed-segment split-pattern dispatch by script family.

Pure stdlib — no network, no paid API, no provider catalog loads.
"""
from __future__ import annotations

from pipeline.process import (
    FAILED_SEGMENT_SEMANTIC_SPLIT_PATTERN,
    FAILED_SEGMENT_SOURCE_SPLIT_PATTERN,
    ProcessPipeline,
    _FAILED_SEGMENT_SPLIT_PATTERN_BY_SCRIPT,
    _minimax_language_matches_target,
)
from services.assemblyai.transcriber import TranscriptLine
from services.language_registry import SCRIPT_CJK, SCRIPT_LATIN


# ── _minimax_language_matches_target — de-Chinese predicate ─────────────────

def test_minimax_match_default_target_is_chinese_byte_identical() -> None:
    # zh-CN target accepts exactly the legacy Mandarin/Cantonese tags.
    assert _minimax_language_matches_target("中文-普通话", "zh-CN") is True
    assert _minimax_language_matches_target("中文-粤语", "zh-CN") is True
    assert _minimax_language_matches_target("英语", "zh-CN") is False
    assert _minimax_language_matches_target("日语", "zh-CN") is False


def test_minimax_match_english_target() -> None:
    assert _minimax_language_matches_target("英语", "en") is True
    assert _minimax_language_matches_target("中文-普通话", "en") is False


def test_minimax_match_unknown_target_is_false() -> None:
    assert _minimax_language_matches_target("英语", "fr") is False
    assert _minimax_language_matches_target("中文-普通话", "") is False


# ── _count_source_words — script-aware unit counting ───────────────────────

def test_count_source_words_latin_is_byte_identical() -> None:
    # Default (latin) behavior unchanged — word-like tokens.
    assert ProcessPipeline._count_source_words("Hello world, foo bar!") == 4
    assert ProcessPipeline._count_source_words("It's 2026 already") == 3
    assert ProcessPipeline._count_source_words("") == 0


def test_count_source_words_latin_on_chinese_is_near_zero() -> None:
    # The legacy Latin regex matches ~0 tokens in pure Chinese — this is the
    # silent-degradation that CJK-source mode fixes.
    assert ProcessPipeline._count_source_words("这是一段没有英文的中文文本") == 0


def test_count_source_words_cjk_counts_ideographs() -> None:
    assert ProcessPipeline._count_source_words("这是中文", source_script=SCRIPT_CJK) == 4
    # English names / numbers inside Chinese are not counted as CJK units, but
    # the ideograph count is still non-zero (probe candidate is selectable).
    # "我们聊聊…的" = 5 ideographs; the English tokens (OpenAI, GPT) are excluded.
    n = ProcessPipeline._count_source_words("我们聊聊 OpenAI 的 GPT-5", source_script=SCRIPT_CJK)
    assert n == 5


# ── _select_probe_segments — CJK source no longer collapses to empty ────────

def _zh_lines(n: int) -> list[TranscriptLine]:
    lines: list[TranscriptLine] = []
    for i in range(n):
        lines.append(
            TranscriptLine(
                index=i,
                start_ms=i * 6000,
                end_ms=i * 6000 + 5000,
                speaker_id="speaker_a",
                speaker_label="Speaker A",
                source_text="这是第" + str(i) + "段用于探针校准的中文访谈内容长度足够通过筛选阈值",
            )
        )
    return lines


def test_select_probe_segments_cjk_source_yields_candidates() -> None:
    lines = _zh_lines(6)
    picked = ProcessPipeline._select_probe_segments(lines, source_script=SCRIPT_CJK)
    assert picked, "CJK source should yield probe candidates when counted by char"


def test_select_probe_segments_latin_mode_on_chinese_is_empty() -> None:
    # Demonstrates the bug PR-W fixes: counting Chinese as Latin words → 0 →
    # no candidates pass the min-words threshold.
    lines = _zh_lines(6)
    picked = ProcessPipeline._select_probe_segments(lines, source_script=SCRIPT_LATIN)
    assert picked == []


# ── Failed-segment split pattern dispatch by script family ─────────────────

def test_split_pattern_map_default_is_byte_identical() -> None:
    assert _FAILED_SEGMENT_SPLIT_PATTERN_BY_SCRIPT[SCRIPT_CJK] is FAILED_SEGMENT_SEMANTIC_SPLIT_PATTERN
    assert _FAILED_SEGMENT_SPLIT_PATTERN_BY_SCRIPT[SCRIPT_LATIN] is FAILED_SEGMENT_SOURCE_SPLIT_PATTERN


def test_latin_pattern_splits_english_sentences() -> None:
    pipeline = ProcessPipeline()
    pieces = pipeline._split_text_for_failed_segment(
        "First sentence here. Second sentence follows. Third one ends.",
        _FAILED_SEGMENT_SPLIT_PATTERN_BY_SCRIPT[SCRIPT_LATIN],
    )
    assert pieces is not None and len(pieces) == 2


def test_cjk_pattern_does_not_split_english_text() -> None:
    # An English target run through the CJK (full-width punctuation) pattern would
    # fail to split — which is exactly why the dispatch must pick the Latin
    # pattern for an English target.
    pipeline = ProcessPipeline()
    pieces = pipeline._split_text_for_failed_segment(
        "First sentence here. Second sentence follows. Third one ends.",
        _FAILED_SEGMENT_SPLIT_PATTERN_BY_SCRIPT[SCRIPT_CJK],
    )
    assert pieces is None


def test_cjk_pattern_splits_chinese_sentences() -> None:
    pipeline = ProcessPipeline()
    pieces = pipeline._split_text_for_failed_segment(
        "第一句话在这里。第二句话紧随其后。第三句话到此结束。",
        _FAILED_SEGMENT_SPLIT_PATTERN_BY_SCRIPT[SCRIPT_CJK],
    )
    assert pieces is not None and len(pieces) == 2
