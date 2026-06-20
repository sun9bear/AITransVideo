"""PR-B: AssemblyAI per-source-language ASR profile (plan 2026-06-13 v3, Phase 2.2).

The English profile must be byte-identical to the legacy hard-coded config
(``language_code='en'``, ``disfluencies=True``, English filler-word prompt). A
non-English source maps to its AssemblyAI ``language_code`` and drops the
English-specific filler prompt + disfluencies. Pure stdlib — a fake ``aai`` SDK
captures the built config; no network, no paid API.
"""
from __future__ import annotations

from types import SimpleNamespace

from services.assemblyai.transcriber import (
    DEFAULT_LANGUAGE_CODE,
    DEFAULT_TRANSCRIPTION_PROMPT,
    _asr_profile_for_language,
    _build_transcription_config,
    _ends_sentence,
    _extract_language,
    _join_tokens,
    _script_for_language,
)


class _FakeConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeAai:
    TranscriptionConfig = _FakeConfig


# ── profile resolution ─────────────────────────────────────────────────────

def test_english_profile_is_legacy() -> None:
    p = _asr_profile_for_language("en")
    assert p["language_code"] == "en"
    assert p["disfluencies"] is True
    assert p["prompt"] == DEFAULT_TRANSCRIPTION_PROMPT


def test_chinese_profile_maps_to_zh_no_english_prompt() -> None:
    p = _asr_profile_for_language("zh-CN")
    assert p["language_code"] == "zh"
    assert p["disfluencies"] is False
    assert p["prompt"] is None


def test_unknown_or_empty_defaults_to_english() -> None:
    for raw in (None, "", "   ", "fr", "klingon"):
        assert _asr_profile_for_language(raw)["language_code"] == "en"


# ── config building ────────────────────────────────────────────────────────

def test_build_config_default_is_byte_identical_english() -> None:
    # No language arg → DEFAULT_LANGUAGE_CODE (en): same shape as the legacy config.
    cfg = _build_transcription_config(_FakeAai(), speaker_labels=False, speakers_expected=None)
    assert cfg.kwargs["language_code"] == DEFAULT_LANGUAGE_CODE == "en"
    assert cfg.kwargs["disfluencies"] is True
    assert cfg.kwargs["prompt"] == DEFAULT_TRANSCRIPTION_PROMPT
    assert cfg.kwargs["speaker_labels"] is False
    assert "speech_models" in cfg.kwargs


def test_build_config_english_explicit_byte_identical() -> None:
    cfg = _build_transcription_config(_FakeAai(), language="en", speaker_labels=True, speakers_expected=3)
    assert cfg.kwargs["language_code"] == "en"
    assert cfg.kwargs["disfluencies"] is True
    assert cfg.kwargs["prompt"] == DEFAULT_TRANSCRIPTION_PROMPT
    assert cfg.kwargs["speakers_expected"] == 3


def test_build_config_chinese_drops_english_prompt() -> None:
    cfg = _build_transcription_config(_FakeAai(), language="zh-CN", speaker_labels=True, speakers_expected=2)
    assert cfg.kwargs["language_code"] == "zh"
    assert cfg.kwargs["disfluencies"] is False
    assert "prompt" not in cfg.kwargs  # no English filler prompt for a zh source
    assert cfg.kwargs["speakers_expected"] == 2


def test_build_config_unknown_language_falls_back_to_english() -> None:
    cfg = _build_transcription_config(_FakeAai(), language="fr", speaker_labels=False, speakers_expected=None)
    assert cfg.kwargs["language_code"] == "en"
    assert cfg.kwargs["prompt"] == DEFAULT_TRANSCRIPTION_PROMPT


# ── script family ──────────────────────────────────────────────────────────

def test_script_for_language() -> None:
    assert _script_for_language("en") == "latin"
    assert _script_for_language("zh-CN") == "cjk"
    for raw in (None, "", "fr"):
        assert _script_for_language(raw) == "latin"  # default


# ── _ends_sentence — script-aware sentence boundary ────────────────────────

def test_ends_sentence_latin_byte_identical() -> None:
    assert _ends_sentence("end.") is True
    assert _ends_sentence("middle") is False
    assert _ends_sentence("句。") is False  # full-width invisible to the Latin pattern


def test_ends_sentence_cjk_full_width() -> None:
    assert _ends_sentence("句。", "cjk") is True
    assert _ends_sentence("问？", "cjk") is True
    assert _ends_sentence("中间", "cjk") is False
    assert _ends_sentence("end.", "cjk") is True  # ASCII still recognized in CJK text


# ── _join_tokens — script-aware joining ────────────────────────────────────

def test_join_tokens_latin_byte_identical() -> None:
    # '.' is attached punctuation (no leading space); "'s" attaches as a clitic.
    assert _join_tokens(["Hello", "world", "."]) == "Hello world."
    assert _join_tokens(["It", "'s", "fine"]) == "It's fine"


def test_join_tokens_cjk_no_spaces() -> None:
    assert _join_tokens(["这", "是", "中", "文", "。"], "cjk") == "这是中文。"
    assert " " not in _join_tokens(["你", "好", "世", "界"], "cjk")


# ── _extract_language — missing metadata fail-closes to requested source ────

def test_extract_language_uses_provider_value_when_present() -> None:
    t = SimpleNamespace(language_code="zh", language=None)
    assert _extract_language(t, {}, default_language="en") == "zh"


def test_extract_language_missing_defaults_to_en_byte_identical() -> None:
    t = SimpleNamespace(language_code=None, language=None)
    assert _extract_language(t, {}) == "en"  # legacy default preserved


def test_extract_language_missing_defaults_to_requested_source() -> None:
    # The fix: a zh-CN request with no provider language must NOT become "en"
    # (which PR-W's transcript gate would reject as a provider mismatch).
    t = SimpleNamespace(language_code=None, language=None)
    assert _extract_language(t, {}, default_language="zh-CN") == "zh-CN"
    assert _extract_language(t, {"foo": "bar"}, default_language="zh-CN") == "zh-CN"
