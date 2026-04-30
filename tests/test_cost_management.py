from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

from cost_management import (  # noqa: E402
    DEFAULT_PRICE_CATALOG,
    JobCostBreakdown,
    LLMRow,
    TTSRow,
    _aggregate_usage_events,
    _job_payload,
    apply_costs,
    build_job_breakdown,
)


def _job(**overrides):
    base = {
        "job_id": "job_cost",
        "display_name": "Cost job",
        "title": "",
        "source_ref": "",
        "status": "succeeded",
        "current_stage": None,
        "service_mode": "studio",
        "tts_provider": "minimax",
        "tts_model": "speech-2.8-hd",
        "plan_code_snapshot": "plus",
        "metering_snapshot": {"credits_actual": 100, "quality_tier": "high"},
        "created_at": datetime(2026, 4, 29, tzinfo=timezone.utc),
        "completed_at": datetime(2026, 4, 29, tzinfo=timezone.utc),
        "actual_minutes": 5.0,
        "estimated_minutes": None,
        "estimated_duration_seconds": None,
        "project_dir": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_aggregate_and_apply_costs_split_llm_and_tts_buckets():
    events = [
        {
            "kind": "llm",
            "provider": "deepseek",
            "model_id": "deepseek-v4-flash",
            "task": "s3_translate",
            "phase": "s3",
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "success": True,
        },
        {
            "kind": "tts",
            "bucket": "first_tts",
            "provider": "minimax",
            "model": "speech-2.8-turbo",
            "input_chars": 5000,
            "billed_chars": 10_000,
        },
        {
            "kind": "tts",
            "bucket": "interactive_preview",
            "provider": "minimax",
            "model": "speech-2.8-hd",
            "input_chars": 100,
            "billed_chars": 200,
        },
    ]

    breakdown = _aggregate_usage_events(events)
    apply_costs(breakdown, DEFAULT_PRICE_CATALOG)

    assert len(breakdown.llm_rows) == 1
    assert breakdown.llm_rows[0].cost_rmb == 3.024
    costs_by_bucket = {row.bucket: row.cost_rmb for row in breakdown.tts_rows}
    assert costs_by_bucket["first_tts"] == 2.0
    assert costs_by_bucket["interactive_preview"] == 0.0
    preview = next(row for row in breakdown.tts_rows if row.bucket == "interactive_preview")
    assert preview.included_in_job_cost is False


def test_build_job_breakdown_falls_back_to_snapshot_when_usage_events_missing(tmp_path):
    snapshot = {
        "first_tts_billed_chars": 10_000,
        "first_tts_call_count": 2,
        "llm_input_tokens": 1000,
        "llm_output_tokens": 2000,
    }

    breakdown = build_job_breakdown(
        project_dir=str(tmp_path),
        snapshot=snapshot,
        default_tts_provider="minimax_tts",
        default_tts_model="speech-02-hd",
        catalog=DEFAULT_PRICE_CATALOG,
    )

    assert breakdown.has_usage_events is False
    assert any("usage_events artifact missing" in warning for warning in breakdown.warnings)
    assert len(breakdown.tts_rows) == 1
    assert breakdown.tts_rows[0].provider == "minimax"
    assert breakdown.tts_rows[0].model == "speech-2.8-hd"
    assert breakdown.tts_rows[0].cost_rmb == 3.5
    assert len(breakdown.llm_rows) == 1
    assert breakdown.llm_rows[0].rate_status == "missing_rate"


def test_job_payload_prefers_ledger_capture_credits_for_revenue():
    payload = _job_payload(
        _job(),
        None,
        JobCostBreakdown(),
        point_price_rmb=0.03,
        point_price_source="test",
        server_cost_per_min_rmb=0.0,
        server_cost_source="test",
        ledger_capture_credits=125,
    )

    assert payload["credits_charged"] == 125
    assert payload["credits_source"] == "credits_ledger_capture"
    assert payload["revenue_estimate_rmb"] == 3.75


def test_job_payload_margin_can_include_server_overhead():
    breakdown = JobCostBreakdown(
        llm_rows=[LLMRow(provider="deepseek", model="deepseek-v4-flash", model_id="deepseek-v4-flash", task="s3", phase="", cost_rmb=0.5)],
        tts_rows=[TTSRow(provider="minimax", model="speech-2.8-hd", bucket="first_tts", cost_rmb=0.5)],
    )

    payload = _job_payload(
        _job(actual_minutes=10.0),
        None,
        breakdown,
        point_price_rmb=0.03,
        point_price_source="test",
        server_cost_per_min_rmb=0.03,
        server_cost_source="test",
    )

    assert payload["revenue_estimate_rmb"] == 3.0
    assert payload["total_cost_rmb"] == 1.0
    assert payload["server_overhead_cost_rmb"] == 0.3
    assert payload["margin_cost_rmb"] == 1.3
    assert payload["gross_margin_pct"] == 56.67
