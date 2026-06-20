"""PR-W: source-aware language gate (plan 2026-06-13 v3 §3.1 / Phase 2).

Covers two invariants:

* **Zero regression (default pair en->zh-CN):** the source gate is byte-for-byte
  the legacy English-only gate — accepts English source / transcript, rejects
  Chinese, fails open when source-language metadata is absent (local uploads).
* **New capability (non-default pair zh-CN->en):** the gate is source-aware —
  accepts a Chinese source / transcript, rejects English, and still fails open
  when metadata is absent.

These tests construct ``ProcessPipeline`` directly and set the language profile
the way the pipeline body does (from the job snapshot). They never touch the
network or any paid API.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline.process import ProcessPipeline
from services.assemblyai.transcriber import TranscriptLine, TranscriptResult
from services.language_registry import (
    DEFAULT_LANGUAGE_PAIR_PROFILE,
    resolve_language_pair,
)


def _line(text: str) -> TranscriptLine:
    return TranscriptLine(
        index=1,
        start_ms=0,
        end_ms=1000,
        speaker_id="speaker_a",
        speaker_label="Speaker A",
        source_text=text,
    )


def _transcript(text: str, *, language: str = "") -> TranscriptResult:
    return TranscriptResult(
        lines=[_line(text)],
        total_duration_ms=1000,
        language=language,
        raw_response_path="",
        structured_transcript_path="",
    )


def _pipeline_for(source_language: str, target_language: str) -> ProcessPipeline:
    """Build a pipeline with the language profile wired as run() would.

    Only ``_language_profile`` is set — the language-aware helpers derive their
    per-side script descriptors from it on demand (single source of truth).
    """
    pipeline = ProcessPipeline()
    profile = resolve_language_pair(source_language, target_language)
    assert profile is not None, f"unsupported test pair {source_language}->{target_language}"
    pipeline._language_profile = profile
    return pipeline


_EN_TEXT = "This is an English interview clip that should pass the source gate."
_ZH_TEXT = "这是一段中文访谈视频，应当通过中文源语言闸门的检测。"


# --------------------------------------------------------------------------
# Default pair en->zh-CN — byte-identical legacy behavior
# --------------------------------------------------------------------------

def test_default_pair_accepts_english_source_metadata() -> None:
    pipeline = _pipeline_for("en", "zh-CN")
    # English metadata: no raise.
    pipeline._enforce_source_language(SimpleNamespace(language="en"))
    pipeline._enforce_source_language(SimpleNamespace(language="en-US"))


def test_default_pair_rejects_chinese_source_metadata() -> None:
    pipeline = _pipeline_for("en", "zh-CN")
    with pytest.raises(ValueError, match="当前只支持英文视频翻译"):
        pipeline._enforce_source_language(SimpleNamespace(language="zh-CN"))


def test_default_pair_source_metadata_missing_fails_open() -> None:
    pipeline = _pipeline_for("en", "zh-CN")
    # Local uploads carry no language metadata — must NOT raise.
    pipeline._enforce_source_language(SimpleNamespace(language=""))
    pipeline._enforce_source_language(SimpleNamespace(language=None))


def test_default_pair_accepts_english_transcript_rejects_chinese() -> None:
    pipeline = _pipeline_for("en", "zh-CN")
    pipeline._enforce_transcript_language(_transcript(_EN_TEXT, language="en"))
    with pytest.raises(ValueError, match="检测到转录稿语言为非英文"):
        pipeline._enforce_transcript_language(_transcript(_ZH_TEXT, language="en"))


def test_no_profile_instance_defaults_to_legacy_english_gate() -> None:
    # A pipeline with no profile set at all (e.g. helper called in isolation)
    # must default to the GA en->zh-CN legacy behavior.
    pipeline = ProcessPipeline()
    pipeline._enforce_source_language(SimpleNamespace(language="en"))
    with pytest.raises(ValueError, match="当前只支持英文视频翻译"):
        pipeline._enforce_source_language(SimpleNamespace(language="zh-CN"))


# --------------------------------------------------------------------------
# Non-default pair zh-CN->en — source-aware behavior
# --------------------------------------------------------------------------

def test_zh_en_accepts_chinese_source_metadata() -> None:
    pipeline = _pipeline_for("zh-CN", "en")
    pipeline._enforce_source_language(SimpleNamespace(language="zh-CN"))
    pipeline._enforce_source_language(SimpleNamespace(language="zh"))  # alias


def test_zh_en_rejects_english_source_metadata() -> None:
    pipeline = _pipeline_for("zh-CN", "en")
    with pytest.raises(ValueError, match="任务源语言为 zh-CN"):
        pipeline._enforce_source_language(SimpleNamespace(language="en"))


def test_zh_en_source_metadata_missing_fails_open() -> None:
    pipeline = _pipeline_for("zh-CN", "en")
    pipeline._enforce_source_language(SimpleNamespace(language=""))


def test_zh_en_accepts_chinese_transcript_rejects_english() -> None:
    pipeline = _pipeline_for("zh-CN", "en")
    pipeline._enforce_transcript_language(_transcript(_ZH_TEXT, language="zh-CN"))
    with pytest.raises(ValueError, match="任务源语言 zh-CN 不一致"):
        pipeline._enforce_transcript_language(_transcript(_EN_TEXT, language=""))


def test_zh_en_rejects_explicit_provider_language_mismatch() -> None:
    pipeline = _pipeline_for("zh-CN", "en")
    with pytest.raises(ValueError, match="任务源语言为 zh-CN"):
        pipeline._enforce_transcript_language(_transcript(_ZH_TEXT, language="en"))


def test_zh_en_skips_auto_provider_language_then_detects_script() -> None:
    pipeline = _pipeline_for("zh-CN", "en")
    # provider "auto" is skipped; CJK script detection still passes.
    pipeline._enforce_transcript_language(_transcript(_ZH_TEXT, language="auto"))


def test_zh_en_accepts_chinese_transcript_with_english_terms() -> None:
    # Mixed text: mostly Chinese with English names + numbers. The CJK ratio
    # denominator counts letter-like chars (CJK + Latin), so this must NOT be
    # falsely rejected as non-Chinese (guards the mixed-text false-negative).
    pipeline = _pipeline_for("zh-CN", "en")
    mixed = "我们今天聊聊 OpenAI 的 GPT-5 模型，2026 年 Q2 的 ARR 增长非常快，团队也在持续扩张。"
    pipeline._enforce_transcript_language(_transcript(mixed, language="zh-CN"))


# --------------------------------------------------------------------------
# _resolve_job_language_profile — missing (→ default) vs explicit-bad (fail-closed)
# --------------------------------------------------------------------------

def test_resolve_profile_both_absent_defaults_to_ga() -> None:
    # Legacy job / no snapshot → GA default (byte-identical pipeline).
    assert ProcessPipeline._resolve_job_language_profile(None, None) is DEFAULT_LANGUAGE_PAIR_PROFILE
    assert ProcessPipeline._resolve_job_language_profile("", "  ") is DEFAULT_LANGUAGE_PAIR_PROFILE


def test_resolve_profile_canonical_pairs() -> None:
    assert ProcessPipeline._resolve_job_language_profile("en", "zh-CN").is_default is True
    assert ProcessPipeline._resolve_job_language_profile("zh-CN", "en").language_pair == "zh-CN->en"


@pytest.mark.parametrize(
    ("src", "tgt"),
    [
        ("fr", "en"),     # explicit unknown source — must NOT run as en->zh
        ("en", "fr"),     # explicit unknown target
        ("fr", None),     # one explicit bad, other absent
        ("en", "en"),     # present but not a supported pair
    ],
)
def test_resolve_profile_explicit_unsupported_fails_closed(src, tgt) -> None:
    with pytest.raises(ValueError, match="不支持的语言对"):
        ProcessPipeline._resolve_job_language_profile(src, tgt)


@pytest.mark.parametrize(("src", "tgt"), [("en", None), ("en", ""), (None, "zh-CN"), ("  ", "zh-CN")])
def test_resolve_profile_partial_present_resolves_to_default(src, tgt) -> None:
    # One end present and consistent with the GA default, the other absent →
    # GA default (matches the Gateway create-path's "incomplete → default pair").
    assert ProcessPipeline._resolve_job_language_profile(src, tgt).is_default is True


@pytest.mark.parametrize(("src", "tgt"), [("zh-CN", None), (None, "en")])
def test_resolve_profile_partial_unresolvable_fails_closed(src, tgt) -> None:
    # A lone source/target that does not form a supported pair with the default
    # other end fails closed rather than silently defaulting to en->zh.
    with pytest.raises(ValueError, match="不支持的语言对"):
        ProcessPipeline._resolve_job_language_profile(src, tgt)


# --------------------------------------------------------------------------
# Hard byte-identical assertions for the GA default pair (capsys + exact text)
# --------------------------------------------------------------------------

def test_default_pair_source_success_log_is_byte_identical(capsys) -> None:
    pipeline = _pipeline_for("en", "zh-CN")
    pipeline._enforce_source_language(SimpleNamespace(language="en"))
    assert "[S0] 视频源语言元数据：en" in capsys.readouterr().out


def test_default_pair_source_reject_message_is_legacy_exact() -> None:
    pipeline = _pipeline_for("en", "zh-CN")
    with pytest.raises(ValueError) as excinfo:
        pipeline._enforce_source_language(SimpleNamespace(language="zh-CN"))
    assert str(excinfo.value) == (
        "当前只支持英文视频翻译。"
        "视频源语言元数据为 'zh-CN'，请确认输入的视频是英文内容。"
    )


def test_default_pair_transcript_detection_log_is_byte_identical(capsys) -> None:
    pipeline = _pipeline_for("en", "zh-CN")
    pipeline._enforce_transcript_language(_transcript(_EN_TEXT, language="en"))
    out = capsys.readouterr().out
    assert "[S1] 语言检测：英文字符占比" in out
    assert "（阈值 60%）" in out
