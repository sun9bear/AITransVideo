"""PR-CD slice 4: probe-translation language-pair dispatch.

Default en->zh-CN keeps the exact PROBE template (byte-identical); zh-CN->en uses
the English probe variant. Output key stays cn_text (canonical container). The
admin override path fails closed for a non-default pair. No network.
"""
from __future__ import annotations

from services.gemini.translator import (
    GeminiTranslator,
    PROBE_TRANSLATION_PROMPT_TEMPLATE,
    _PROBE_TRANSLATION_PROMPT_TEMPLATE_ZH_EN,
)


def _t(source: str, target: str) -> GeminiTranslator:
    t = GeminiTranslator(api_key="test-key")
    t._translate_source_language = source
    t._translate_target_language = target
    return t


def test_probe_template_default_is_byte_identical() -> None:
    # No admin override in tests → get_effective returns PROBE; default pair
    # returns it verbatim (byte-identical legacy path).
    assert _t("en", "zh-CN")._select_probe_template("en", "zh-CN") == PROBE_TRANSLATION_PROMPT_TEMPLATE


def test_probe_template_zh_en_uses_english_variant() -> None:
    assert (
        _t("zh-CN", "en")._select_probe_template("zh-CN", "en")
        == _PROBE_TRANSLATION_PROMPT_TEMPLATE_ZH_EN
    )


def test_probe_template_unknown_pair_falls_back_to_default_template() -> None:
    # Unknown pairs are blocked upstream (pipeline_ready / resolve_language_pair);
    # the registry returns the PROBE template as a safe net rather than crashing.
    assert _t("ja", "en")._select_probe_template("ja", "en") == PROBE_TRANSLATION_PROMPT_TEMPLATE


def test_build_probe_prompt_zh_en_is_english_and_keeps_cn_text_key() -> None:
    t = _t("zh-CN", "en")
    prompt = t._build_probe_prompt(
        [{"segment_id": 1, "speaker_id": "A", "source_text": "你好世界", "target_duration_seconds": 2.0}]
    )
    assert "natural, fluent English" in prompt
    assert "cn_text" in prompt  # canonical output container is unchanged


def test_build_probe_prompt_default_is_chinese() -> None:
    t = _t("en", "zh-CN")
    prompt = t._build_probe_prompt(
        [{"segment_id": 1, "speaker_id": "A", "source_text": "hello", "target_duration_seconds": 2.0}]
    )
    assert "中文口播文本" in prompt
