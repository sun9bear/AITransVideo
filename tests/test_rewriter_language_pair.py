"""PR-CD slice 3: GeminiRewriter language-pair dispatch.

Default target zh-CN keeps the exact Chinese rewrite prompts + char counting
(byte-identical); an English target uses English rewrite prompts + word counting.
Admin override fails closed for a non-default pair. No network.
"""
from __future__ import annotations

from services.gemini.rewriter import GeminiRewriter
from services.gemini.translator import (
    DEFAULT_REWRITE_PROMPT_TEMPLATE,
    GeminiTranslator,
    _REWRITE_PROMPT_TEMPLATE_ZH_EN,
)


def _rewriter(source: str = "en", target: str = "zh-CN") -> GeminiRewriter:
    t = GeminiTranslator(api_key="test-key")
    t._translate_source_language = source
    t._translate_target_language = target
    return GeminiRewriter(t)


# ── template selection ──────────────────────────────────────────────────────

def test_default_target_uses_chinese_template() -> None:
    r = _rewriter("en", "zh-CN")
    assert r.rewrite_prompt_template == DEFAULT_REWRITE_PROMPT_TEMPLATE
    assert r._target_is_latin is False


def test_en_target_uses_english_template() -> None:
    r = _rewriter("zh-CN", "en")
    assert r.rewrite_prompt_template == _REWRITE_PROMPT_TEMPLATE_ZH_EN
    assert r._target_is_latin is True


def test_select_rewrite_template_failsclosed_for_non_default() -> None:
    r = _rewriter("zh-CN", "en")
    assert r._select_rewrite_template("custom no marker") == _REWRITE_PROMPT_TEMPLATE_ZH_EN
    assert r._select_rewrite_template("custom zh-CN->en marker") == "custom zh-CN->en marker"


def test_select_rewrite_template_default_uses_configured() -> None:
    r = _rewriter("en", "zh-CN")
    assert r._select_rewrite_template("anything configured") == "anything configured"


# ── spoken-unit counting ────────────────────────────────────────────────────

def test_spoken_units_cjk_is_char_count() -> None:
    assert _rewriter("en", "zh-CN")._spoken_units("你好世界") == 4


def test_spoken_units_latin_is_word_count() -> None:
    assert _rewriter("zh-CN", "en")._spoken_units("hello there world") == 3


# ── prompt content direction ────────────────────────────────────────────────

def test_compact_prompt_english_for_en_target() -> None:
    r = _rewriter("zh-CN", "en")
    p = r._build_short_content_compact_prompt(
        "Hello world", source_text="你好", target_duration_ms=3000,
        target_lower_chars=2, target_upper_chars=5,
    )
    assert "compression editor" in p
    assert "压缩成" not in p


def test_compact_prompt_chinese_for_default() -> None:
    r = _rewriter("en", "zh-CN")
    p = r._build_short_content_compact_prompt(
        "你好世界", source_text="hi", target_duration_ms=3000,
        target_lower_chars=2, target_upper_chars=5,
    )
    assert "压缩成" in p


def test_rewrite_prompt_tail_english_for_en_target() -> None:
    r = _rewriter("zh-CN", "en")
    p = r._build_rewrite_prompt("Hello world", "shrink", 5, 3)
    assert "Output only the rewritten English text" in p
    assert "最终只输出改写后的中文文本" not in p


def test_rewrite_prompt_tail_chinese_for_default() -> None:
    r = _rewriter("en", "zh-CN")
    p = r._build_rewrite_prompt("你好世界", "shrink", 5, 3)
    assert "最终只输出改写后的中文文本" in p


# ── rate unit consistency (CodeX PR-CD P2) ──────────────────────────────────

def test_units_per_second_cjk_is_byte_identical_calibrated() -> None:
    # CJK target keeps the legacy calibrated-or-default char-rate exactly.
    t = GeminiTranslator(api_key="k")
    t._translate_source_language = "en"
    t._translate_target_language = "zh-CN"
    r = GeminiRewriter(t, chars_per_second=4.5, chars_per_second_by_speaker={"A": 5.0})
    assert r._spoken_units_per_second("A") == 5.0  # calibrated wins
    assert r._spoken_units_per_second("Z") == 4.5  # default fallback


def test_units_per_second_latin_uses_word_rate_not_char_cps() -> None:
    # Latin target must NOT use the char-based per-voice cps (wrong unit); it uses
    # the language word-rate (descriptor default 2.6 wps) regardless of calibration.
    t = GeminiTranslator(api_key="k")
    t._translate_source_language = "zh-CN"
    t._translate_target_language = "en"
    r = GeminiRewriter(t, chars_per_second=4.5, chars_per_second_by_speaker={"A": 13.0})
    assert r._spoken_units_per_second("A") == 2.6
    assert r._spoken_units_per_second("Z") == 2.6
