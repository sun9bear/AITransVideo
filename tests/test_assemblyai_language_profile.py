"""PR-B: AssemblyAI per-source-language ASR profile (plan 2026-06-13 v3, Phase 2.2).

The English profile must be byte-identical to the legacy hard-coded config
(``language_code='en'``, ``disfluencies=True``, English filler-word prompt). A
non-English source maps to its AssemblyAI ``language_code`` and drops the
English-specific filler prompt + disfluencies. Pure stdlib — a fake ``aai`` SDK
captures the built config; no network, no paid API.
"""
from __future__ import annotations

from services.assemblyai.transcriber import (
    DEFAULT_LANGUAGE_CODE,
    DEFAULT_TRANSCRIPTION_PROMPT,
    _asr_profile_for_language,
    _build_transcription_config,
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
