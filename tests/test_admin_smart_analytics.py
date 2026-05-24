"""Smart analytics dashboard backend — aggregation + endpoint tests.

== Spec ==

docs/plans/2026-05-22-smart-analytics-v1.md §3 — two endpoints:

  GET /api/admin/smart-analytics/summary?days=N&status=&user=
  GET /api/admin/smart-analytics/csv?days=N&status=&user=

Both admin-only. /summary returns the full JSON payload (KPI + 3 tabs
+ task_table). /csv returns task_table as Excel-compatible CSV with
UTF-8 BOM.

This file tests the pure helpers + the public endpoints. Helpers are
exercised with synthetic fixtures (alignment_report text, JSONL
content). Endpoints are tested with a fake DB + project_dir on
tmp_path.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


_GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


# ─────────────────────────────────────────────────────────────────────
# Sample alignment_report.txt — exact format from production
# ─────────────────────────────────────────────────────────────────────

_SAMPLE_ALIGNMENT_REPORT = """对齐质量报告
============
视频：Stanford CS153 Frontier Systems | Jensen Huang from NVIDIA on the Compute Behind Intelligence
总段数：107段
Speaker A（主持人）：40段
Speaker B（黄仁勋）：67段

对齐方式统计：
  直接使用（误差<5%）：48段（37%）
  DSP变速：19段（15%）
  Gemini重写后直接使用：9段（7%）
  Gemini重写后DSP对齐：5段（4%）
  强制DSP兜底：18段（14%）
  短段听感保护DSP：27段（21%）

⚠️ 需要手工检查的段落（共16段）：
  segment_001  Speaker A  00:00:00 → 00:00:04  [强制DSP，变速幅度过大]
  ...
"""


# ─────────────────────────────────────────────────────────────────────
# Parser tests
# ─────────────────────────────────────────────────────────────────────


class TestParseAlignmentReport:
    """Pin the regex-based parser for alignment_report.txt."""

    def test_full_text_extracts_all_six_stats_plus_total_plus_review(self):
        from admin_smart_analytics_api import _parse_alignment_report

        out = _parse_alignment_report(_SAMPLE_ALIGNMENT_REPORT)
        assert out["total_segments"] == 107
        assert out["direct_pct"] == pytest.approx(0.37)
        assert out["dsp_pct"] == pytest.approx(0.15)
        assert out["rewrite_direct_pct"] == pytest.approx(0.07)
        assert out["rewrite_dsp_pct"] == pytest.approx(0.04)
        assert out["forced_dsp_pct"] == pytest.approx(0.14)
        assert out["short_segment_dsp_pct"] == pytest.approx(0.21)
        assert out["manual_review_segments"] == 16

    def test_missing_fields_return_none_per_field(self):
        """Partial parse: graceful degradation."""
        from admin_smart_analytics_api import _parse_alignment_report

        partial = """对齐质量报告
总段数：50段
对齐方式统计：
  直接使用（误差<5%）：20段（40%）
  强制DSP兜底：5段（10%）
"""
        out = _parse_alignment_report(partial)
        assert out["total_segments"] == 50
        assert out["direct_pct"] == pytest.approx(0.40)
        assert out["forced_dsp_pct"] == pytest.approx(0.10)
        # Missing fields stay None — don't crash
        assert out["dsp_pct"] is None
        assert out["rewrite_direct_pct"] is None
        assert out["short_segment_dsp_pct"] is None
        assert out["manual_review_segments"] is None

    def test_empty_text_returns_all_none(self):
        from admin_smart_analytics_api import _parse_alignment_report

        out = _parse_alignment_report("")
        for key in (
            "total_segments", "direct_pct", "dsp_pct",
            "rewrite_direct_pct", "rewrite_dsp_pct",
            "forced_dsp_pct", "short_segment_dsp_pct",
            "manual_review_segments",
        ):
            assert out[key] is None

    def test_real_production_report_with_no_manual_review(self):
        """Job that needed no human review: count line absent."""
        from admin_smart_analytics_api import _parse_alignment_report

        clean_report = """对齐质量报告
总段数：30段

对齐方式统计：
  直接使用（误差<5%）：28段（93%）
  DSP变速：2段（7%）
  Gemini重写后直接使用：0段（0%）
  Gemini重写后DSP对齐：0段（0%）
  强制DSP兜底：0段（0%）
  短段听感保护DSP：0段（0%）
"""
        out = _parse_alignment_report(clean_report)
        assert out["total_segments"] == 30
        assert out["direct_pct"] == pytest.approx(0.93)
        assert out["forced_dsp_pct"] == pytest.approx(0.0)
        assert out["manual_review_segments"] is None


class TestCountHandoffReasons:
    """Pin smart_decisions.jsonl → handoff reason_code counts."""

    def test_counts_downgrade_handoff_decisions_only(self, tmp_path):
        from admin_smart_analytics_api import _count_handoff_reasons_from_decisions

        jsonl = tmp_path / "smart_decisions.jsonl"
        jsonl.write_text("\n".join([
            json.dumps({"decision_type": "speaker_gate", "decision": "approved"}),
            json.dumps({"decision_type": "voice_clone", "decision": "approved"}),
            json.dumps({"decision_type": "downgrade_handoff", "decision": "rejected",
                       "reason_code": "uncertain_speaker_share"}),
            json.dumps({"decision_type": "downgrade_handoff", "decision": "rejected",
                       "reason_code": "glossary_preservation"}),
            json.dumps({"decision_type": "downgrade_handoff", "decision": "rejected",
                       "reason_code": "uncertain_speaker_share"}),
        ]), encoding="utf-8")

        counts = _count_handoff_reasons_from_decisions(jsonl)
        assert counts == {
            "uncertain_speaker_share": 2,
            "glossary_preservation": 1,
        }

    def test_missing_file_returns_empty_dict(self, tmp_path):
        from admin_smart_analytics_api import _count_handoff_reasons_from_decisions

        assert _count_handoff_reasons_from_decisions(tmp_path / "nonexistent.jsonl") == {}

    def test_malformed_lines_skipped_silently(self, tmp_path):
        from admin_smart_analytics_api import _count_handoff_reasons_from_decisions

        jsonl = tmp_path / "decisions.jsonl"
        jsonl.write_text("\n".join([
            "not json",
            "",
            json.dumps({"decision_type": "downgrade_handoff", "reason_code": "x"}),
            "{partial json",
        ]), encoding="utf-8")

        counts = _count_handoff_reasons_from_decisions(jsonl)
        assert counts == {"x": 1}


class TestCountEditEvents:
    """Pin user_edit_events.jsonl → event_type counts."""

    def test_counts_each_event_type(self, tmp_path):
        from admin_smart_analytics_api import _count_edit_events

        jsonl = tmp_path / "user_edit_events.jsonl"
        jsonl.write_text("\n".join([
            json.dumps({"event_type": "text_changed", "segment_id": "s1"}),
            json.dumps({"event_type": "text_changed", "segment_id": "s2"}),
            json.dumps({"event_type": "tts_regenerated", "segment_id": "s1"}),
            json.dumps({"event_type": "split_confirmed", "segment_id": "s3"}),
            json.dumps({"event_type": "speaker_changed", "segment_id": "s2"}),
        ]), encoding="utf-8")

        counts = _count_edit_events(jsonl)
        assert counts == {
            "text_changed": 2,
            "tts_regenerated": 1,
            "split_confirmed": 1,
            "speaker_changed": 1,
        }

    def test_missing_file_returns_empty(self, tmp_path):
        from admin_smart_analytics_api import _count_edit_events

        assert _count_edit_events(tmp_path / "missing.jsonl") == {}


class TestClassifySmartOutcome:
    """Pin smart job → outcome category mapping for handoff distribution."""

    def test_succeeded_clean_no_handoff(self):
        from admin_smart_analytics_api import _classify_smart_outcome

        job = SimpleNamespace(status="succeeded", error_summary=None)
        assert _classify_smart_outcome(job, smart_state={}) == "succeeded_clean"

    def test_succeeded_with_handoff_history(self):
        from admin_smart_analytics_api import _classify_smart_outcome

        # 2026-05-20 spec: handoff status can be preserved in smart_state
        # even when the pipeline completed (admin approved manually).
        job = SimpleNamespace(status="succeeded", error_summary=None)
        smart_state = {
            "status": "completed",
            "reason": "uncertain_speaker_share",
            "failed_check": "uncertain_speaker_share",
        }
        out = _classify_smart_outcome(job, smart_state)
        assert out == "succeeded_with_handoff_uncertain_speaker_share"

    def test_failed_pipeline_uses_error_type(self):
        from admin_smart_analytics_api import _classify_smart_outcome

        job = SimpleNamespace(
            status="failed",
            error_summary={"error_type": "ContentPolicyViolation"},
        )
        out = _classify_smart_outcome(job, smart_state={})
        assert out == "pipeline_failed_ContentPolicyViolation"

    def test_failed_no_error_type_falls_to_unknown(self):
        from admin_smart_analytics_api import _classify_smart_outcome

        job = SimpleNamespace(status="failed", error_summary=None)
        out = _classify_smart_outcome(job, smart_state={})
        assert out == "pipeline_failed_unknown"

    def test_in_flight_categories(self):
        from admin_smart_analytics_api import _classify_smart_outcome

        job_running = SimpleNamespace(status="running", error_summary=None)
        job_editing = SimpleNamespace(status="editing", error_summary=None)
        assert _classify_smart_outcome(job_running, smart_state={}) == "in_flight_running"
        assert _classify_smart_outcome(job_editing, smart_state={}) == "in_flight_editing"


# ─────────────────────────────────────────────────────────────────────
# Aggregation tests
# ─────────────────────────────────────────────────────────────────────


def _make_job(
    *,
    job_id: str = "job_xyz",
    user_id: str = "u-1",
    display_name: str = "Test Video",
    status: str = "succeeded",
    source_duration_seconds: float = 600.0,
    project_dir: str | None = None,
    smart_state: dict | None = None,
    error_summary: dict | None = None,
    edit_generation: int | None = 0,
    created_at: datetime | None = None,
    title: str = "",
):
    return SimpleNamespace(
        job_id=job_id,
        user_id=user_id,
        display_name=display_name,
        title=title,
        status=status,
        source_duration_seconds=source_duration_seconds,
        project_dir=project_dir,
        smart_state=smart_state,
        error_summary=error_summary,
        edit_generation=edit_generation,
        created_at=created_at or datetime(2026, 5, 22, tzinfo=timezone.utc),
    )


class TestAggregateJob:
    """Pin _aggregate_job with on-disk project_dir."""

    def test_succeeded_job_with_full_audit_directory(self, tmp_path):
        from admin_smart_analytics_api import _aggregate_job

        # Build a synthetic project_dir
        proj = tmp_path / "proj_xyz"
        (proj / "output").mkdir(parents=True)
        (proj / "audit").mkdir()
        (proj / "output" / "alignment_report.txt").write_text(
            _SAMPLE_ALIGNMENT_REPORT, encoding="utf-8",
        )
        (proj / "audit" / "user_edit_events.jsonl").write_text("\n".join([
            json.dumps({"event_type": "text_changed"}),
            json.dumps({"event_type": "text_changed"}),
            json.dumps({"event_type": "tts_regenerated"}),
        ]), encoding="utf-8")

        job = _make_job(
            project_dir=str(proj),
            source_duration_seconds=4103.616,  # = 68.39 min
        )
        m = _aggregate_job(job, user_email="admin@test.com")

        assert m.job_id == "job_xyz"
        assert m.status == "succeeded"
        assert m.source_duration_minutes == pytest.approx(68.39, rel=1e-2)
        assert m.total_segments == 107
        assert m.forced_dsp_pct == pytest.approx(0.14)
        assert m.manual_review_segments == 16
        assert m.edit_event_count == 3
        assert m.entered_editing is True  # 3 edit events > 0
        assert m.outcome_category == "succeeded_clean"
        assert m.user_email == "admin@test.com"
        assert m.edit_events_by_type == {
            "text_changed": 2,
            "tts_regenerated": 1,
        }

    def test_job_without_project_dir_gets_none_metrics(self):
        from admin_smart_analytics_api import _aggregate_job

        job = _make_job(project_dir=None)
        m = _aggregate_job(job, user_email=None)

        assert m.total_segments is None
        assert m.forced_dsp_pct is None
        assert m.edit_event_count == 0
        assert m.entered_editing is False

    def test_succeeded_with_handoff_history_classified(self, tmp_path):
        from admin_smart_analytics_api import _aggregate_job

        job = _make_job(
            project_dir=None,
            smart_state={
                "status": "completed",
                "reason": "glossary_preservation_low_0.79",
                "failed_check": "glossary_preservation",
            },
        )
        m = _aggregate_job(job, user_email=None)
        assert m.outcome_category == "succeeded_with_handoff_glossary_preservation_low_0.79"
        assert m.smart_handoff_reason == "glossary_preservation_low_0.79"

    def test_edit_generation_implies_entered_editing(self):
        from admin_smart_analytics_api import _aggregate_job

        job = _make_job(project_dir=None, edit_generation=2)
        m = _aggregate_job(job, user_email=None)
        assert m.entered_editing is True


# ─────────────────────────────────────────────────────────────────────
# Summary payload tests
# ─────────────────────────────────────────────────────────────────────


def _metric(
    *,
    job_id="j1",
    user_id="u-1",
    user_email="a@x",
    status="succeeded",
    source_duration_seconds=600.0,
    forced_dsp_pct=0.1,
    total_segments=20,
    smart_handoff_reason=None,
    entered_editing=False,
    edit_event_count=0,
    edit_events_by_type=None,
    outcome_category=None,
):
    from admin_smart_analytics_api import JobAggregatedMetrics

    if outcome_category is None:
        if status == "succeeded":
            outcome_category = (
                f"succeeded_with_handoff_{smart_handoff_reason}"
                if smart_handoff_reason
                else "succeeded_clean"
            )
        elif status == "failed":
            outcome_category = "pipeline_failed_unknown"
        else:
            outcome_category = f"in_flight_{status}"

    return JobAggregatedMetrics(
        job_id=job_id,
        user_id=user_id,
        user_email=user_email,
        display_name=f"Display {job_id}",
        status=status,
        source_duration_seconds=source_duration_seconds,
        source_duration_minutes=source_duration_seconds / 60.0,
        total_segments=total_segments,
        outcome_category=outcome_category,
        smart_handoff_reason=smart_handoff_reason,
        direct_pct=0.5,
        dsp_pct=0.3,
        rewrite_direct_pct=0.05,
        rewrite_dsp_pct=0.05,
        forced_dsp_pct=forced_dsp_pct,
        short_segment_dsp_pct=0.05,
        manual_review_segments=2,
        entered_editing=entered_editing,
        edit_event_count=edit_event_count,
        edit_events_by_type=edit_events_by_type or {},
        created_at=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).isoformat(),
    )


class TestBuildSummaryPayload:
    """Pin the /summary endpoint payload shape + KPI math."""

    def test_empty_metrics_returns_zeroed_kpi(self):
        from admin_smart_analytics_api import _build_summary_payload

        payload = _build_summary_payload([], days=30)
        assert payload["kpi"]["total_smart_jobs"] == 0
        assert payload["kpi"]["handoff_rate"] == 0
        assert payload["kpi"]["rework_rate"] == 0
        assert payload["handoff_distribution"] == []
        assert payload["alignment_quality"] == []
        assert payload["task_table"] == []
        # window present
        assert payload["window"]["days"] == 30

    def test_kpi_math_with_mixed_outcomes(self):
        from admin_smart_analytics_api import _build_summary_payload

        metrics = [
            _metric(job_id="j1", status="succeeded", forced_dsp_pct=0.10),
            _metric(job_id="j2", status="succeeded", forced_dsp_pct=0.20),
            _metric(job_id="j3", status="succeeded", forced_dsp_pct=0.30,
                    smart_handoff_reason="uncertain_speaker_share"),
            _metric(job_id="j4", status="failed"),
            _metric(job_id="j5", status="succeeded", forced_dsp_pct=0.05,
                    entered_editing=True, edit_event_count=4,
                    edit_events_by_type={"text_changed": 4}),
        ]
        payload = _build_summary_payload(metrics, days=30)

        # 5 total, 4 succeeded (incl handoff), 1 failed
        assert payload["kpi"]["total_smart_jobs"] == 5
        assert payload["kpi"]["succeeded"] == 4
        assert payload["kpi"]["failed"] == 1
        # handoff: 1 succeeded-w-handoff + 1 failed = 2/5 = 0.4
        assert payload["kpi"]["handoff_rate"] == pytest.approx(0.4)
        # avg forced_dsp_pct over succeeded: (0.1+0.2+0.3+0.05)/4 = 0.1625
        assert payload["kpi"]["avg_forced_dsp_pct"] == pytest.approx(0.1625, rel=1e-3)
        # rework: 1 entered editing / 5 = 0.2
        assert payload["kpi"]["rework_rate"] == pytest.approx(0.2)
        assert payload["kpi"]["avg_edited_segments"] == pytest.approx(4.0)

    def test_handoff_distribution_includes_sample_job_ids(self):
        from admin_smart_analytics_api import _build_summary_payload

        metrics = [
            _metric(job_id="a", smart_handoff_reason="x"),
            _metric(job_id="b", smart_handoff_reason="x"),
            _metric(job_id="c", smart_handoff_reason="y"),
        ]
        payload = _build_summary_payload(metrics, days=30)

        by_code = {row["reason_code"]: row for row in payload["handoff_distribution"]}
        assert by_code["succeeded_with_handoff_x"]["count"] == 2
        assert "a" in by_code["succeeded_with_handoff_x"]["sample_job_ids"]
        assert by_code["succeeded_with_handoff_y"]["count"] == 1

    def test_alignment_quality_sorted_by_forced_dsp_desc(self):
        from admin_smart_analytics_api import _build_summary_payload

        metrics = [
            _metric(job_id="low", forced_dsp_pct=0.05),
            _metric(job_id="high", forced_dsp_pct=0.40),
            _metric(job_id="mid", forced_dsp_pct=0.20),
        ]
        payload = _build_summary_payload(metrics, days=30)

        ids = [r["job_id"] for r in payload["alignment_quality"]]
        assert ids == ["high", "mid", "low"]

    def test_rework_by_user_aggregates_correctly(self):
        from admin_smart_analytics_api import _build_summary_payload

        metrics = [
            # admin: 3 jobs, 2 entered editing
            _metric(job_id="a1", user_id="admin", user_email="a@x"),
            _metric(job_id="a2", user_id="admin", user_email="a@x",
                    entered_editing=True, edit_event_count=5),
            _metric(job_id="a3", user_id="admin", user_email="a@x",
                    entered_editing=True, edit_event_count=3),
            # plus: 1 job, 0 entered editing
            _metric(job_id="p1", user_id="plus", user_email="p@x"),
        ]
        payload = _build_summary_payload(metrics, days=30)

        by_uid = {row["user_id"]: row for row in payload["rework_by_user"]}
        assert by_uid["admin"]["smart_job_count"] == 3
        assert by_uid["admin"]["entered_editing_count"] == 2
        assert by_uid["admin"]["rework_rate"] == pytest.approx(2 / 3, rel=1e-3)
        assert by_uid["admin"]["avg_edited_segments"] == pytest.approx(4.0)  # (5+3)/2
        assert by_uid["plus"]["smart_job_count"] == 1
        assert by_uid["plus"]["rework_rate"] == 0.0

    def test_edit_event_distribution_aggregates_across_users(self):
        from admin_smart_analytics_api import _build_summary_payload

        metrics = [
            _metric(job_id="j1", entered_editing=True, edit_event_count=3,
                    edit_events_by_type={"text_changed": 2, "tts_regenerated": 1}),
            _metric(job_id="j2", entered_editing=True, edit_event_count=5,
                    edit_events_by_type={"text_changed": 3, "split_confirmed": 2}),
        ]
        payload = _build_summary_payload(metrics, days=30)

        by_type = {row["event_type"]: row for row in payload["edit_event_distribution"]}
        assert by_type["text_changed"]["count"] == 5  # 2 + 3
        assert by_type["tts_regenerated"]["count"] == 1
        assert by_type["split_confirmed"]["count"] == 2
        # pct: 5 / 8 = 0.625
        assert by_type["text_changed"]["pct"] == pytest.approx(0.625, rel=1e-3)

    def test_task_table_sorted_by_created_at_desc(self):
        from admin_smart_analytics_api import _build_summary_payload, JobAggregatedMetrics

        m_old = _metric(job_id="old")
        m_new = _metric(job_id="new")
        # Override created_at via dataclasses.replace style — these are dataclass
        from dataclasses import replace
        m_old = replace(m_old, created_at="2026-01-01T00:00:00+00:00")
        m_new = replace(m_new, created_at="2026-05-01T00:00:00+00:00")

        payload = _build_summary_payload([m_old, m_new], days=365)
        ids = [r["job_id"] for r in payload["task_table"]]
        assert ids == ["new", "old"]


# ─────────────────────────────────────────────────────────────────────
# CSV export tests
# ─────────────────────────────────────────────────────────────────────


class TestBuildCsv:
    """Pin the CSV export shape + Excel compatibility."""

    def test_csv_has_utf8_bom_for_excel_chinese(self):
        from admin_smart_analytics_api import _build_csv

        metrics = [_metric(job_id="j1")]
        body = _build_csv(metrics)
        # UTF-8 BOM = \xef\xbb\xbf
        assert body.startswith(b"\xef\xbb\xbf"), (
            "CSV must start with UTF-8 BOM so Excel renders Chinese "
            "correctly. Without it, Excel guesses GBK and mangles."
        )

    def test_csv_has_header_row(self):
        from admin_smart_analytics_api import _build_csv

        body = _build_csv([_metric(job_id="j1")])
        text = body.decode("utf-8-sig")
        first_line = text.split("\n")[0]
        for col in (
            "job_id", "user_email", "display_name", "status",
            "source_duration_minutes", "total_segments",
            "smart_handoff_reason",
            "forced_dsp_pct", "dsp_pct",
            "entered_editing", "edit_event_count", "created_at",
        ):
            assert col in first_line, f"CSV header missing column {col!r}: {first_line}"

    def test_csv_data_row_includes_job_data(self):
        from admin_smart_analytics_api import _build_csv

        metrics = [
            _metric(
                job_id="job_test1",
                user_email="admin@x",
                status="succeeded",
                source_duration_seconds=600.0,
                forced_dsp_pct=0.15,
            ),
        ]
        body = _build_csv(metrics)
        text = body.decode("utf-8-sig")
        assert "job_test1" in text
        assert "admin@x" in text
        assert "succeeded" in text

    def test_csv_empty_metrics_only_has_header(self):
        from admin_smart_analytics_api import _build_csv

        body = _build_csv([])
        text = body.decode("utf-8-sig")
        non_empty_lines = [line for line in text.split("\n") if line.strip()]
        assert len(non_empty_lines) == 1, (
            f"Empty metrics list should produce header-only CSV; got "
            f"{len(non_empty_lines)} lines."
        )


# ─────────────────────────────────────────────────────────────────────
# Endpoint integration smoke
# ─────────────────────────────────────────────────────────────────────


def _run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestEndpointSummary:
    """Smoke: /summary endpoint orchestration."""

    def test_admin_role_required(self):
        from admin_smart_analytics_api import get_summary
        from fastapi import HTTPException

        # Mimic non-admin user
        non_admin = SimpleNamespace(role="user", email="x@y")
        db_mock = MagicMock()
        db_mock.execute = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            _run(get_summary(
                days=30, status="all", user="all",
                user_acc=non_admin, db=db_mock,
            ))
        assert exc_info.value.status_code in (401, 403)

    def test_summary_returns_payload_with_expected_keys(self):
        """Smoke: admin gets back a payload with all top-level keys."""
        from admin_smart_analytics_api import get_summary

        admin_user = SimpleNamespace(role="admin", email="a@x")
        db_result = MagicMock()
        db_result.all = MagicMock(return_value=[])  # no jobs
        db_mock = MagicMock()
        db_mock.execute = AsyncMock(return_value=db_result)

        resp = _run(get_summary(
            days=30, status="all", user="all",
            user_acc=admin_user, db=db_mock,
        ))
        assert resp.status_code == 200
        payload = json.loads(resp.body)
        for key in (
            "window", "kpi", "handoff_distribution",
            "alignment_quality", "rework_by_user",
            "edit_event_distribution", "task_table",
        ):
            assert key in payload, f"missing key {key!r}"


class TestEndpointCsv:
    """Smoke: /csv endpoint."""

    def test_csv_endpoint_returns_csv_content_type(self):
        from admin_smart_analytics_api import get_csv

        admin_user = SimpleNamespace(role="admin", email="a@x")
        db_result = MagicMock()
        db_result.all = MagicMock(return_value=[])
        db_mock = MagicMock()
        db_mock.execute = AsyncMock(return_value=db_result)

        resp = _run(get_csv(
            days=30, status="all", user="all",
            user_acc=admin_user, db=db_mock,
        ))
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "attachment" in resp.headers["content-disposition"]


class TestPhase1bReportEndpoints:
    def test_job_reports_summary_aggregates_report_sidecars(self, tmp_path):
        from admin_smart_analytics_api import get_job_reports_summary

        project = tmp_path / "job_reports"
        (project / "reports").mkdir(parents=True)
        (project / "reports" / "translation_quality_report.json").write_text(
            json.dumps({"checked_segments": 8, "issue_count": 2}),
            encoding="utf-8",
        )
        job = _make_job(
            job_id="job_reports",
            project_dir=str(project),
            status="succeeded",
            created_at=datetime.now(timezone.utc),
        )
        owner = SimpleNamespace(email="admin@example.test")
        admin_user = SimpleNamespace(role="admin", email="a@x")
        db_result = MagicMock()
        db_result.all = MagicMock(return_value=[(job, owner)])
        db_mock = MagicMock()
        db_mock.execute = AsyncMock(return_value=db_result)

        resp = _run(
            get_job_reports_summary(
                days=30,
                status="all",
                user="all",
                service_mode="all",
                user_acc=admin_user,
                db=db_mock,
            )
        )

        assert resp.status_code == 200
        payload = json.loads(resp.body)
        assert payload["kpi"]["translation_issue_count"] == 2
        assert payload["jobs"][0]["reports"]["translation_quality"]["issue_rate"] == 0.25

    def test_phase1b_flags_update_persists_admin_values(self, tmp_path, monkeypatch):
        import admin_smart_analytics_api as mod

        settings_path = tmp_path / "admin_settings.json"
        monkeypatch.setattr(mod.admin_settings_store, "SETTINGS_FILE", settings_path)
        monkeypatch.setenv("AVT_TRANSLATION_SCRIPT_GATE_SHADOW", "1")
        admin_user = SimpleNamespace(role="admin", email="a@x")

        body = mod.Phase1bFlagUpdate(
            flags={
                "voice_sample_scoring": True,
            }
        )
        resp = _run(mod.update_phase1b_flags(body=body, user_acc=admin_user))

        assert resp.status_code == 200
        payload = json.loads(resp.body)
        by_key = {row["key"]: row for row in payload["flags"]}
        assert by_key["translation_script_gate_shadow"]["effective"] is True
        assert by_key["voice_sample_scoring"]["effective"] is True
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert saved["phase1b_translation_script_gate_shadow"] is True
        assert saved["phase1b_voice_sample_scoring_enabled"] is True
