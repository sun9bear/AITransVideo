"""PR-B slice 3: Gemini transcriber source-language prompt selection.

The English prompt must be the exact legacy prompt (byte-identical); a zh-CN
source selects a Chinese-transcription prompt. Pure module-level checks — no
genai SDK / network.
"""
from __future__ import annotations

from services.gemini.transcriber import (
    TRANSCRIPTION_PROMPT,
    TRANSCRIPTION_PROMPT_ZH,
    _transcription_prompt_for_language,
)


def test_english_prompt_is_byte_identical_legacy() -> None:
    assert _transcription_prompt_for_language("en") is TRANSCRIPTION_PROMPT
    assert "英文转录稿" in TRANSCRIPTION_PROMPT
    assert "转录必须是英文原文" in TRANSCRIPTION_PROMPT


def test_chinese_source_selects_chinese_prompt() -> None:
    p = _transcription_prompt_for_language("zh-CN")
    assert p is TRANSCRIPTION_PROMPT_ZH
    assert "中文转录稿" in p
    assert "转录必须是中文原文" in p
    assert "英文转录稿" not in p  # no English-transcription instruction leaks in


def test_unknown_or_empty_defaults_to_english() -> None:
    for raw in (None, "", "   ", "fr", "klingon"):
        assert _transcription_prompt_for_language(raw) is TRANSCRIPTION_PROMPT
