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


def test_rewrite_prompt_direction_labels_english_for_en_target() -> None:
    # re-CodeX P2: the direction label/instruction must also be English for a Latin
    # target — no Chinese 缩短/扩充 leaking into the English template.
    r = _rewriter("zh-CN", "en")
    p_shrink = r._build_rewrite_prompt("Hello world", "shrink", 5, 3)
    assert "shorten" in p_shrink
    assert "缩短" not in p_shrink and "删减冗余词汇" not in p_shrink
    p_expand = r._build_rewrite_prompt("Hi", "expand", 2, 5)
    assert "expand" in p_expand
    assert "扩充" not in p_expand


def test_rewrite_prompt_direction_labels_chinese_for_default() -> None:
    r = _rewriter("en", "zh-CN")
    p = r._build_rewrite_prompt("你好世界", "shrink", 5, 3)
    assert "缩短" in p
    assert "shorten" not in p


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


# ── char-bound → word-budget conversion (CodeX P2 part 2) ────────────────────

def test_to_target_budget_units_cjk_passthrough() -> None:
    # CJK bounds from the pipeline are already char counts → unchanged (byte-identical).
    r = _rewriter("en", "zh-CN")
    assert r._to_target_budget_units(60) == 60
    assert r._to_target_budget_units(1) == 1


def test_to_target_budget_units_latin_converts_chars_to_words() -> None:
    r = _rewriter("zh-CN", "en")
    assert r._to_target_budget_units(47) == round(47 / 4.7)  # 10
    assert r._to_target_budget_units(66) == round(66 / 4.7)  # 14
    assert r._to_target_budget_units(0) == 1  # floor never below 1


def test_rewrite_for_duration_latin_prompt_renders_word_bounds_not_char_bounds() -> None:
    # The pipeline passes CHAR bounds (47-66). For a Latin target the prompt must
    # show the WORD budget (~10-14), so the label, the model's word self-check, and
    # the pipeline's char guard stay consistent — not the raw 47/66 char counts.
    r = _rewriter("zh-CN", "en")
    captured: dict[str, str] = {}

    def _fake_call(task_name, prompt, json_mode=False):  # noqa: ANN001
        captured["prompt"] = prompt
        return "a rewritten english sentence"

    r._call_task_with_usage_phase = _fake_call  # type: ignore[assignment]
    r.rewrite_for_duration_with_profile(
        "hello there world",
        actual_duration_ms=6000,
        target_duration_ms=4000,
        source_text="你好",
        target_lower_chars=47,
        target_upper_chars=66,
    )
    prompt = captured["prompt"]
    assert "10~14" in prompt  # converted word band
    assert "47" not in prompt and "66" not in prompt  # raw char bounds gone


def test_rewrite_for_duration_cjk_prompt_keeps_char_bounds_byte_identical() -> None:
    r = _rewriter("en", "zh-CN")
    captured: dict[str, str] = {}

    def _fake_call(task_name, prompt, json_mode=False):  # noqa: ANN001
        captured["prompt"] = prompt
        return "改写后的中文"

    r._call_task_with_usage_phase = _fake_call  # type: ignore[assignment]
    r.rewrite_for_duration_with_profile(
        "你好世界你好世界",
        actual_duration_ms=6000,
        target_duration_ms=4000,
        source_text="hello",
        target_lower_chars=47,
        target_upper_chars=66,
    )
    prompt = captured["prompt"]
    assert "47~66" in prompt  # CJK bounds untouched


def test_rewrite_latin_target_clamped_into_converted_band() -> None:
    # re-CodeX P2: target_chars (fixed 2.6 wps) must not exceed the ÷4.7-converted
    # explicit band, or the prompt would say "about 10 units" while "aim for 4~5".
    r = _rewriter("zh-CN", "en")
    captured: dict[str, str] = {}

    def _fake_call(task_name, prompt, json_mode=False):  # noqa: ANN001
        captured["prompt"] = prompt
        return "a rewritten english sentence"

    r._call_task_with_usage_phase = _fake_call  # type: ignore[assignment]
    # duration 4000ms → 2.6 wps target_chars=10; char-bounds 19~24 → ÷4.7 → 4~5.
    r.rewrite_for_duration_with_profile(
        "hello world foo bar",
        actual_duration_ms=8000,
        target_duration_ms=4000,
        source_text="x",
        target_lower_chars=19,
        target_upper_chars=24,
    )
    prompt = captured["prompt"]
    assert "about 5 units" in prompt  # target clamped into the 4~5 band
    assert "about 10 units" not in prompt
    assert "4~5" in prompt
