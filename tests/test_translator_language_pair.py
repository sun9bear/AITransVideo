"""PR-CD slice 1: translator prompt registry + override fail-closed + parser alias.

Default pair en->zh-CN keeps the exact legacy prompt (byte-identical); zh-CN->en
selects an English-translation variant. A non-default-pair admin override is only
honored when it declares the pair (§2.3 fail-closed). The parser accepts
``target_text`` as an alias for the canonical ``cn_text`` (v3 §4.5). No network.
"""
from __future__ import annotations

import json

from services.gemini.translator import (
    DEFAULT_TRANSLATION_PROMPT_TEMPLATE,
    GeminiTranslator,
    _TRANSLATION_PROMPT_TEMPLATE_ZH_EN,
)


def _translator() -> GeminiTranslator:
    t = GeminiTranslator(api_key="test-key")
    t.translation_prompt_template = DEFAULT_TRANSLATION_PROMPT_TEMPLATE
    return t


# ── template selection ──────────────────────────────────────────────────────

def test_default_pair_uses_configured_template() -> None:
    t = _translator()
    assert t._select_translation_template("en", "zh-CN") is DEFAULT_TRANSLATION_PROMPT_TEMPLATE


def test_zh_en_uses_english_variant() -> None:
    t = _translator()
    assert t._select_translation_template("zh-CN", "en") is _TRANSLATION_PROMPT_TEMPLATE_ZH_EN


def test_default_pair_honors_admin_override() -> None:
    t = _translator()
    t.translation_prompt_template = "CUSTOM default override"
    assert t._select_translation_template("en", "zh-CN") == "CUSTOM default override"


def test_non_default_override_failsclosed_without_marker() -> None:
    t = _translator()
    t.translation_prompt_template = "CUSTOM no marker"
    assert t._select_translation_template("zh-CN", "en") is _TRANSLATION_PROMPT_TEMPLATE_ZH_EN


def test_non_default_override_used_when_marker_present() -> None:
    t = _translator()
    t.translation_prompt_template = "CUSTOM for zh-CN->en direction"
    assert t._select_translation_template("zh-CN", "en") == "CUSTOM for zh-CN->en direction"


# ── prompt content direction ────────────────────────────────────────────────

def test_zh_en_template_translates_to_english() -> None:
    assert "into natural, fluent English" in _TRANSLATION_PROMPT_TEMPLATE_ZH_EN
    assert "把英文视频转录稿翻译成自然流畅的中文" not in _TRANSLATION_PROMPT_TEMPLATE_ZH_EN
    # shares the token contract so _build_prompt works unchanged
    for token in ("__VIDEO_TITLE__", "__GLOSSARY_SECTION__", "__SPEAKER_INSTRUCTION__",
                  "__STRICT_LENGTH_INSTRUCTION__", "__GROUPS_JSON__"):
        assert token in _TRANSLATION_PROMPT_TEMPLATE_ZH_EN


# ── parser target_text alias ────────────────────────────────────────────────

def test_parser_reads_cn_text_byte_identical() -> None:
    t = _translator()
    out = t._parse_response('[{"segment_id": 1, "cn_text": "你好世界"}]', [{"segment_id": 1}])
    assert out[0]["cn_text"] == "你好世界"


def test_parser_accepts_target_text_alias() -> None:
    t = _translator()
    out = t._parse_response('[{"segment_id": 1, "target_text": "Hello world"}]', [{"segment_id": 1}])
    assert out[0]["cn_text"] == "Hello world"


def test_parser_prefers_target_text_over_cn_text() -> None:
    t = _translator()
    resp = json.dumps([{"segment_id": 1, "target_text": "Hello", "cn_text": "ignored"}])
    out = t._parse_response(resp, [{"segment_id": 1}])
    assert out[0]["cn_text"] == "Hello"
