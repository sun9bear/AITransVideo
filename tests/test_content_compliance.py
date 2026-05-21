from __future__ import annotations

from types import SimpleNamespace

import pytest

import pipeline.process as process_module
from pipeline.process import ProcessPipeline, _call_content_compliance_llm_with_retry
from services.assemblyai.transcriber import TranscriptLine, TranscriptResult
from services.content_compliance import (
    ContentPolicyViolationError,
    LLMContentComplianceReviewer,
    MainlandChinaContentComplianceReviewer,
    validate_content_compliance_llm_response,
)
from services.llm_registry import get_prompt_model, invalidate_cache, resolve_model_id


def _line(text: str) -> TranscriptLine:
    return TranscriptLine(
        index=1,
        start_ms=0,
        end_ms=1000,
        speaker_id="speaker_a",
        speaker_label="Speaker A",
        source_text=text,
    )


def test_local_content_compliance_rule_blocks_clear_violation() -> None:
    reviewer = MainlandChinaContentComplianceReviewer()

    result = reviewer.review(
        transcript_lines=[_line("This clip promotes an online casino.")],
        video_title="Ad",
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=blocked",
    )

    assert result.blocked
    assert result.findings[0].rule_id == "obscenity_gambling_violence_crime"


def test_llm_content_compliance_response_validation_accepts_pass_json() -> None:
    validate_content_compliance_llm_response(
        '{"decision":"pass","confidence":0.9,"reason":"ok","categories":[]}'
    )


def test_content_compliance_default_model_is_gemini_31_flash_lite(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    invalidate_cache()

    assert get_prompt_model("studio", "content_compliance") == "gemini_31_flash_lite"
    assert resolve_model_id("gemini_31_flash_lite") == "gemini-3.1-flash-lite"


def test_content_compliance_llm_retry_uses_primary_retry() -> None:
    class FakeTranslator:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.usage: list[dict[str, object]] = []

        def _call_by_model(self, model_name: str, prompt: str, *, json_mode: bool) -> str:
            assert json_mode
            self.calls.append((model_name, prompt))
            if len(self.calls) == 1:
                raise RuntimeError("temporary outage")
            return '{"decision":"pass","confidence":0.9,"reason":"ok","categories":[]}'

        def _record_llm_usage(self, **kwargs: object) -> None:
            self.usage.append(kwargs)

    translator = FakeTranslator()

    response = _call_content_compliance_llm_with_retry(
        translator,
        "review this",
        primary_model="gemini_31_flash_lite",
        retry_delay_seconds=0,
        peer_cost_rank_delta=0,
    )

    assert '"decision":"pass"' in response
    assert translator.calls == [
        ("gemini_31_flash_lite", "review this"),
        ("gemini_31_flash_lite", "review this"),
    ]
    assert translator.usage[0]["task"] == "content_compliance"


def test_llm_content_compliance_reviewer_blocks_manual_review() -> None:
    reviewer = LLMContentComplianceReviewer(
        generate_json=lambda prompt: (
            '{"decision":"needs_manual_review","confidence":0.7,'
            '"reason":"manual review","categories":[]}'
        ),
        model_name="gemini_31_flash_lite",
    )
    local_result = MainlandChinaContentComplianceReviewer().review(
        transcript_lines=[_line("A normal discussion.")],
    )

    result = reviewer.review(
        transcript_lines=[_line("A normal discussion.")],
        local_result=local_result,
    )

    assert result.blocked
    assert result.status == "needs_manual_review"


def test_non_admin_content_compliance_violation_still_blocks(tmp_path) -> None:
    pipeline = ProcessPipeline()
    transcript = TranscriptResult(
        lines=[_line("This clip promotes an online casino.")],
        total_duration_ms=1000,
        language="en",
        raw_response_path="",
        structured_transcript_path="",
    )

    with pytest.raises(ContentPolicyViolationError):
        pipeline._run_content_compliance_review(
            final_project_dir=tmp_path,
            transcript_result=transcript,
            download_result=SimpleNamespace(video_title="Ad", description=""),
            source_type="youtube_url",
            source_ref="https://youtube.example/watch?v=blocked",
        )


def test_admin_content_compliance_violation_warns_without_blocking(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_dispatch(**kwargs: object) -> bool:
        calls.append(kwargs)
        return True

    monkeypatch.setattr(
        process_module,
        "_dispatch_content_compliance_admin_override_notification",
        fake_dispatch,
    )
    pipeline = ProcessPipeline()
    transcript = TranscriptResult(
        lines=[_line("This clip promotes an online casino.")],
        total_duration_ms=1000,
        language="en",
        raw_response_path="",
        structured_transcript_path="",
    )

    payload = pipeline._run_content_compliance_review(
        final_project_dir=tmp_path,
        transcript_result=transcript,
        download_result=SimpleNamespace(video_title="Ad", description=""),
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=blocked",
        admin_override=True,
        job_id="job_admin",
        user_id="00000000-0000-0000-0000-000000000001",
        display_name="Admin Task",
    )

    assert payload["status"] == "blocked"
    assert payload["admin_override"] is True
    assert payload["notification_dispatched"] is True
    assert calls[0]["job_id"] == "job_admin"
    assert calls[0]["user_id"] == "00000000-0000-0000-0000-000000000001"
    assert calls[0]["display_name"] == "Admin Task"
    assert "\u7ee7\u7eed\u7ffb\u8bd1\u6d41\u7a0b" in str(payload["message"])


def test_chinese_source_language_is_blocked_before_translation() -> None:
    pipeline = ProcessPipeline()

    with pytest.raises(ValueError, match="\u5f53\u524d\u53ea\u652f\u6301\u82f1\u6587\u89c6\u9891\u7ffb\u8bd1"):
        pipeline._enforce_english_source_language(SimpleNamespace(language="zh-CN"))


def test_chinese_transcript_text_is_blocked_even_when_language_field_is_en() -> None:
    pipeline = ProcessPipeline()
    transcript = TranscriptResult(
        lines=[_line("\u8fd9\u662f\u4e00\u4e2a\u4e2d\u6587\u89c6\u9891\uff0c\u4e0d\u5e94\u8be5\u8fdb\u5165\u7ffb\u8bd1\u6d41\u7a0b\u3002")],
        total_duration_ms=1000,
        language="en",
        raw_response_path="",
        structured_transcript_path="",
    )

    with pytest.raises(ValueError, match="\u68c0\u6d4b\u5230\u8f6c\u5f55\u7a3f\u8bed\u8a00\u4e3a\u975e\u82f1\u6587"):
        pipeline._enforce_english_transcript_language(transcript)
