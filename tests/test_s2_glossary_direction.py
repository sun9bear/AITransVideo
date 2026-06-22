"""PR-H slice 1: S2 Pass 2 glossary direction + override fail-closed (plan v3 §2.3).

The default pair en->zh-CN keeps the exact legacy Pass 2 prompt (byte-identical);
zh-CN->en uses a reversed-glossary variant ({Chinese term: English translation}).
A non-default-pair admin override that does not declare the pair fails closed to
the registry template. Pure module-level checks — no LLM / network.
"""
from __future__ import annotations

import services.transcript_reviewer as tr
from services.transcript_reviewer import (
    _PASS2_PROMPT,
    _PASS2_PROMPT_ZH_EN,
    _override_declares_language_pair,
    _select_pass2_template,
)

_FMT = " {video_title} {line_count} {transcript_body} {speakers_json}"


def test_default_pair_uses_legacy_pass2_prompt(monkeypatch) -> None:
    monkeypatch.setattr(tr, "_get_admin_prompt_override", lambda key: None)
    assert _select_pass2_template("en", "zh-CN") is _PASS2_PROMPT


def test_zh_en_uses_reversed_glossary_template(monkeypatch) -> None:
    monkeypatch.setattr(tr, "_get_admin_prompt_override", lambda key: None)
    assert _select_pass2_template("zh-CN", "en") is _PASS2_PROMPT_ZH_EN


def test_glossary_example_direction() -> None:
    # en->zh: English term -> Chinese value; zh->en: Chinese term -> English value.
    assert '"Berkshire Hathaway": "伯克希尔·哈撒韦"' in _PASS2_PROMPT
    assert '"伯克希尔·哈撒韦": "Berkshire Hathaway"' in _PASS2_PROMPT_ZH_EN
    assert "中文源词" in _PASS2_PROMPT_ZH_EN  # explicit direction hint


def test_override_declares_language_pair() -> None:
    assert _override_declares_language_pair("prompt … zh-CN->en … rest", "zh-CN", "en") is True
    assert _override_declares_language_pair("prompt with no marker", "zh-CN", "en") is False
    assert _override_declares_language_pair("", "zh-CN", "en") is False


def test_default_pair_uses_override_when_present(monkeypatch) -> None:
    # Byte-identical legacy behavior: default pair always honors the admin override.
    monkeypatch.setattr(tr, "_get_admin_prompt_override", lambda key: "CUSTOM" + _FMT)
    assert _select_pass2_template("en", "zh-CN").startswith("CUSTOM")


def test_non_default_pair_failsclosed_on_unaware_override(monkeypatch) -> None:
    # Override present but does not declare the pair → fail-closed to registry template.
    monkeypatch.setattr(tr, "_get_admin_prompt_override", lambda key: "CUSTOM no-marker" + _FMT)
    assert _select_pass2_template("zh-CN", "en") is _PASS2_PROMPT_ZH_EN


def test_non_default_pair_uses_override_when_declared(monkeypatch) -> None:
    monkeypatch.setattr(tr, "_get_admin_prompt_override", lambda key: "CUSTOM zh-CN->en" + _FMT)
    assert _select_pass2_template("zh-CN", "en").startswith("CUSTOM")
