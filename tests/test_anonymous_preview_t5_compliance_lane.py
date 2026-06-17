"""APF P0 T5 — 匿名严格合规 lane 测试（plan 2026-06-10 AD-2 v2 / §6 T5）。

断言级验收：
* 三条 skip 路径在 anonymous_strict 下全部 fail-closed（C2）；
* 登录 free 任务三条路径行为不变（回归）；
* S2 Pass 3 在匿名 lane 跳过（硬验收）；
* anonymous_preview 标记 JobRecord/from_dict/api 透传（严格 is True）；
* gateway 本地规则预筛 fn。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import pipeline.process as process_module
from pipeline.process import (
    ANONYMOUS_PREVIEW_MIN_TRANSCRIPT_CHARS,
    ProcessPipeline,
    _should_run_pass3,
)
from services.assemblyai.transcriber import TranscriptLine, TranscriptResult
from services.content_compliance import ContentPolicyViolationError
from services.jobs.models import JobRecord

_PROCESS_SOURCE = Path(process_module.__file__).read_text(encoding="utf-8")

CLEAN_TEXT = "今天我们聊一聊家常菜的做法，先准备食材，然后开火热锅。"
BLOCKED_TEXT = "This clip promotes an online casino."
PASS_LLM_JSON = '{"decision":"pass","confidence":0.95,"reason":"ok","categories":[]}'


def _line(text: str) -> TranscriptLine:
    return TranscriptLine(
        index=1,
        start_ms=0,
        end_ms=1000,
        speaker_id="speaker_a",
        speaker_label="Speaker A",
        source_text=text,
    )


def _transcript(text: str) -> TranscriptResult:
    return TranscriptResult(
        lines=[_line(text)],
        total_duration_ms=1000,
        language="zh",
        raw_response_path="",
        structured_transcript_path="",
    )


def _download() -> SimpleNamespace:
    return SimpleNamespace(video_title="测试视频", description="")


def _run_review(tmp_path, *, text: str, strict: bool, llm=None, admin: bool = False):
    return ProcessPipeline()._run_content_compliance_review(
        final_project_dir=tmp_path,
        transcript_result=_transcript(text),
        download_result=_download(),
        source_type="local_video",
        source_ref="upload://test",
        llm_generate_json=llm,
        llm_model_name="test-model",
        admin_override=admin,
        anonymous_strict=strict,
    )


@pytest.fixture()
def compliance_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(process_module, "is_content_compliance_enabled", lambda: True)
    monkeypatch.setattr(
        process_module, "is_content_compliance_llm_enabled", lambda: True
    )


# --- skip 路径 1：总开关关闭 ---------------------------------------------


def test_strict_master_switch_off_fail_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(process_module, "is_content_compliance_enabled", lambda: False)
    with pytest.raises(ContentPolicyViolationError):
        _run_review(tmp_path, text=CLEAN_TEXT, strict=True)


def test_non_strict_master_switch_off_still_skips(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(process_module, "is_content_compliance_enabled", lambda: False)
    payload = _run_review(tmp_path, text=CLEAN_TEXT, strict=False)
    assert payload["status"] == "skipped"


# --- skip 路径 2：LLM 层未启用/未配置 -------------------------------------


def test_strict_llm_disabled_fail_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(process_module, "is_content_compliance_enabled", lambda: True)
    monkeypatch.setattr(
        process_module, "is_content_compliance_llm_enabled", lambda: False
    )
    with pytest.raises(ContentPolicyViolationError):
        _run_review(tmp_path, text=CLEAN_TEXT, strict=True, llm=lambda p: PASS_LLM_JSON)


def test_strict_llm_fn_missing_fail_closed(tmp_path, compliance_on) -> None:
    with pytest.raises(ContentPolicyViolationError):
        _run_review(tmp_path, text=CLEAN_TEXT, strict=True, llm=None)


def test_non_strict_llm_disabled_passes_local_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(process_module, "is_content_compliance_enabled", lambda: True)
    monkeypatch.setattr(
        process_module, "is_content_compliance_llm_enabled", lambda: False
    )
    payload = _run_review(tmp_path, text=CLEAN_TEXT, strict=False, llm=None)
    assert payload["status"] == "approved"


# --- skip 路径 3：LLM 出错时 fail_closed 不读 env -------------------------


def test_strict_llm_error_fail_closed_even_when_env_open(
    tmp_path, compliance_on, monkeypatch
) -> None:
    monkeypatch.setattr(
        process_module, "is_content_compliance_llm_fail_closed", lambda: False
    )

    def _boom(prompt: str) -> str:
        raise RuntimeError("llm down")

    with pytest.raises(ContentPolicyViolationError):
        _run_review(tmp_path, text=CLEAN_TEXT, strict=True, llm=_boom)


def test_non_strict_llm_error_respects_env_fail_open(
    tmp_path, compliance_on, monkeypatch
) -> None:
    monkeypatch.setattr(
        process_module, "is_content_compliance_llm_fail_closed", lambda: False
    )

    def _boom(prompt: str) -> str:
        raise RuntimeError("llm down")

    payload = _run_review(tmp_path, text=CLEAN_TEXT, strict=False, llm=_boom)
    assert payload["status"] == "approved"


# --- 近空转录（F7 软拦截） -------------------------------------------------


def test_strict_near_empty_transcript_fail_closed(tmp_path, compliance_on) -> None:
    assert len("嗯") < ANONYMOUS_PREVIEW_MIN_TRANSCRIPT_CHARS
    with pytest.raises(ContentPolicyViolationError):
        _run_review(tmp_path, text="嗯", strict=True, llm=lambda p: PASS_LLM_JSON)


def test_non_strict_near_empty_transcript_unaffected(tmp_path, compliance_on) -> None:
    payload = _run_review(tmp_path, text="嗯", strict=False, llm=lambda p: PASS_LLM_JSON)
    assert payload["status"] == "approved"


# --- 干净内容 + LLM 正常 → 通过 -------------------------------------------


def test_strict_clean_content_passes(tmp_path, compliance_on) -> None:
    payload = _run_review(
        tmp_path, text=CLEAN_TEXT, strict=True, llm=lambda p: PASS_LLM_JSON
    )
    assert payload["status"] == "approved"


# --- admin override 在匿名 lane 失效 ---------------------------------------


def test_strict_admin_override_ignored(tmp_path, compliance_on) -> None:
    with pytest.raises(ContentPolicyViolationError):
        _run_review(
            tmp_path,
            text=BLOCKED_TEXT,
            strict=True,
            llm=lambda p: PASS_LLM_JSON,
            admin=True,
        )


# --- Pass 3 跳过（硬验收） --------------------------------------------------


def test_should_run_pass3_truth_table() -> None:
    assert _should_run_pass3({"speaker_a": {}}, False) is True
    assert _should_run_pass3({"speaker_a": {}}, True) is False
    assert _should_run_pass3({}, False) is False
    assert _should_run_pass3({}, True) is False
    assert _should_run_pass3(None, False) is False


def test_pass3_call_site_uses_gate_helper() -> None:
    # plan 2026-06-12 §E：gate helper 增 service_mode 入参（匿名按 lane
    # 分流——free 跳过不变 / express 必跑）。
    assert (
        "_should_run_pass3(_review_speaker_styles, job_anonymous_preview, job_service_mode)"
        in _PROCESS_SOURCE
    )


def test_compliance_call_site_passes_anonymous_strict() -> None:
    assert "anonymous_strict=job_anonymous_preview" in _PROCESS_SOURCE


# --- anonymous_preview 标记透传（严格 is True） -----------------------------

_RECORD_BASE = {
    "job_id": "j1",
    "job_type": "localize_video",
    "source_type": "local_video",
    "source_ref": "upload://x",
    "output_target": "editor",
    "status": "queued",
    "created_at": "2026-06-10T00:00:00Z",
    "updated_at": "2026-06-10T00:00:00Z",
}


def test_job_record_anonymous_preview_round_trip() -> None:
    record = JobRecord.from_dict({**_RECORD_BASE, "anonymous_preview": True})
    assert record.anonymous_preview is True
    assert record.to_dict()["anonymous_preview"] is True


def test_job_record_anonymous_preview_default_false() -> None:
    record = JobRecord.from_dict(dict(_RECORD_BASE))
    assert record.anonymous_preview is False
    assert record.to_dict()["anonymous_preview"] is False


@pytest.mark.parametrize("bad", ["true", 1, {}, [], "True"])
def test_job_record_anonymous_preview_rejects_coercion(bad) -> None:
    record = JobRecord.from_dict({**_RECORD_BASE, "anonymous_preview": bad})
    assert record.anonymous_preview is False


def test_service_and_api_passthrough_present() -> None:
    service_src = Path("src/services/jobs/service.py").read_text(encoding="utf-8")
    api_src = Path("src/services/jobs/api.py").read_text(encoding="utf-8")
    assert "anonymous_preview: bool = False" in service_src
    assert "anonymous_preview=anonymous_preview is True" in service_src
    assert 'payload.get("anonymous_preview") is True' in api_src


# --- gateway 本地规则预筛 ---------------------------------------------------


def test_prescreen_clean_filename_passes() -> None:
    from gateway.anonymous_preview_prescreen import prescreen_filename
    from src.services.anonymous_preview_intake import ComplianceStatus

    result = prescreen_filename("家常菜教程.mp4")
    assert result.status == ComplianceStatus.PASS
    assert result.blocked_media_retained is False


def test_prescreen_blocked_filename_blocks() -> None:
    from gateway.anonymous_preview_prescreen import prescreen_filename
    from src.services.anonymous_preview_intake import ComplianceStatus

    result = prescreen_filename("online casino promo.mp4")
    assert result.status == ComplianceStatus.BLOCK
    # reason 固定文案，不回显文件名
    assert "casino" not in result.reason


def test_prescreen_result_feeds_contract_evaluator() -> None:
    from gateway.anonymous_preview_prescreen import prescreen_filename
    from src.services.anonymous_preview_intake import (
        ComplianceStatus,
        evaluate_compliance_result,
    )

    evaluated = evaluate_compliance_result(prescreen_filename("正常视频.mp4"))
    assert evaluated.status is ComplianceStatus.PASS
