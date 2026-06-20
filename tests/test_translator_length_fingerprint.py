"""PR-CD slice 2: per-pair length budget + fingerprint freeze (plan v3 §2.5/F).

The default pair en->zh-CN must keep the exact legacy length numbers AND a
byte-identical translation fingerprint (a drifted fingerprint would invalidate
every existing en->zh checkpoint and force paid re-translation). zh-CN->en uses
the 0.55 ratio + CJK source counting. No network.
"""
from __future__ import annotations

from services.assemblyai.transcriber import TranscriptLine
from services.gemini.translator import (
    GeminiTranslator,
    _build_groups,
    _count_source_words,
    _estimate_dynamic_target_chars,
)


def _translator() -> GeminiTranslator:
    return GeminiTranslator(api_key="test-key")


def _line(text: str, start_ms: int = 0, end_ms: int = 5000) -> TranscriptLine:
    return TranscriptLine(
        index=1, start_ms=start_ms, end_ms=end_ms,
        speaker_id="speaker_a", speaker_label="Speaker A", source_text=text,
    )


# ── source counting ─────────────────────────────────────────────────────────

def test_count_source_words_latin_byte_identical() -> None:
    assert _count_source_words("one two three four") == 4
    assert _count_source_words("one two three four", "latin") == 4


def test_count_source_words_cjk_counts_ideographs() -> None:
    assert _count_source_words("这是中文测试内容", "cjk") == 8
    assert _count_source_words("这是中文测试内容") == 0  # latin default sees no words


# ── dynamic target chars ratio ──────────────────────────────────────────────

def test_estimate_target_chars_default_ratio_1_8() -> None:
    n = _estimate_dynamic_target_chars(target_duration_ms=5000, density_factor=1.0, source_word_count=10)
    assert n == round(10 * 1.8)  # 18 — byte-identical legacy


def test_estimate_target_chars_zh_en_ratio_0_55() -> None:
    n = _estimate_dynamic_target_chars(
        target_duration_ms=5000, density_factor=1.0, source_word_count=100, ratio=0.55
    )
    assert n == round(100 * 0.55)  # 55 English-word budget
    en = _estimate_dynamic_target_chars(target_duration_ms=5000, density_factor=1.0, source_word_count=100)
    assert n < en  # 0.55 < 1.8


# ── _count_cn_chars target-unit ─────────────────────────────────────────────

def test_count_cn_chars_default_is_char_count() -> None:
    t = _translator()
    t._translate_target_language = "zh-CN"
    assert t._count_cn_chars("你好世界") == 4


def test_count_cn_chars_latin_target_is_word_count() -> None:
    t = _translator()
    t._translate_target_language = "en"
    assert t._count_cn_chars("Hello there world") == 3  # words, not letters


# ── _build_groups per pair ──────────────────────────────────────────────────

def test_build_groups_en_zh_uses_word_count_and_1_8() -> None:
    groups = _build_groups(
        [_line("one two three four five six seven eight nine ten")],
        max_segment_duration_ms=45000, source_language="en", target_language="zh-CN",
    )
    assert groups[0]["source_word_count"] == 10
    assert groups[0]["target_chars_hint"] == round(10 * 1.8)


def test_build_groups_zh_en_uses_cjk_count_and_0_55() -> None:
    zh = "这是一段中文测试内容用于验证长度预算"  # 18 ideographs
    groups = _build_groups(
        [_line(zh)], max_segment_duration_ms=45000, source_language="zh-CN", target_language="en",
    )
    assert groups[0]["source_word_count"] == _count_source_words(zh, "cjk")
    assert groups[0]["target_chars_hint"] == max(1, round(groups[0]["source_word_count"] * 0.55))


# ── fingerprint freeze (the critical guard) ─────────────────────────────────

def test_default_pair_fingerprint_is_byte_identical_to_legacy() -> None:
    groups = _build_groups(
        [_line("one two three four five")],
        max_segment_duration_ms=45000, source_language="en", target_language="zh-CN",
    )
    # Fresh instance: no _translate_* attrs → legacy (pre-multilingual) payload.
    fp_legacy = _translator()._build_translation_fingerprint(groups, video_title="t", youtube_url="u")
    # Explicit default pair attrs → must produce the SAME hash (no language_pair key).
    t = _translator()
    t._translate_source_language = "en"
    t._translate_target_language = "zh-CN"
    fp_default = t._build_translation_fingerprint(groups, video_title="t", youtube_url="u")
    assert fp_default == fp_legacy


def test_non_default_pair_fingerprint_differs() -> None:
    groups = _build_groups(
        [_line("one two three four five")],
        max_segment_duration_ms=45000, source_language="en", target_language="zh-CN",
    )
    t = _translator()
    fp_default = t._build_translation_fingerprint(groups, video_title="t", youtube_url="u")
    t._translate_source_language = "zh-CN"
    t._translate_target_language = "en"
    fp_zh_en = t._build_translation_fingerprint(groups, video_title="t", youtube_url="u")
    assert fp_zh_en != fp_default  # zh->en must not reuse an en->zh cache entry
