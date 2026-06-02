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
    VoiceCloneRow,
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
        {
            "kind": "voice_clone",
            "bucket": "voice_clone",
            "provider": "minimax_voice_clone",
            "model": "voice_clone",
            "source_audio_seconds": 18.5,
            "selected_segment_count": 3,
            "clone_count": 1,
            "billable": True,
            "success": True,
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
    assert len(breakdown.voice_clone_rows) == 1
    assert breakdown.voice_clone_rows[0].provider == "minimax"
    assert breakdown.voice_clone_rows[0].billable_clones == 1
    assert breakdown.voice_clone_rows[0].cost_rmb == 9.9


def test_cosyvoice_v35_tts_rates_and_free_clone_cost():
    events = [
        {
            "kind": "tts",
            "bucket": "first_tts",
            "provider": "cosyvoice",
            "model": "cosyvoice-v3.5-flash",
            "billed_chars": 10_000,
        },
        {
            "kind": "tts",
            "bucket": "first_tts",
            "provider": "cosyvoice",
            "model": "cosyvoice-v3.5-plus",
            "billed_chars": 10_000,
        },
        {
            "kind": "voice_clone",
            "bucket": "voice_clone",
            "provider": "cosyvoice_voice_clone",
            "model": "cosyvoice-v3.5-flash",
            "clone_count": 1,
            "billable": False,
            "success": True,
        },
    ]

    breakdown = _aggregate_usage_events(events)
    apply_costs(breakdown, DEFAULT_PRICE_CATALOG)

    costs_by_model = {row.model: row.cost_rmb for row in breakdown.tts_rows}
    assert costs_by_model["cosyvoice-v3.5-flash"] == 0.8
    assert costs_by_model["cosyvoice-v3.5-plus"] == 1.5
    clone = breakdown.voice_clone_rows[0]
    assert clone.provider == "cosyvoice"
    assert clone.billable_clones == 0
    assert clone.cost_rmb == 0.0
    assert clone.rate_status == "configured"


def test_cosyvoice_historical_wrong_model_infers_v35_from_voice_id():
    events = [
        {
            "kind": "tts",
            "bucket": "first_tts",
            "provider": "cosyvoice",
            "model": "speech-2.8-turbo",
            "selected_voice": "cosyvoice-v3.5-flash-avtspeak-abc",
            "billed_chars": 10_000,
        },
    ]

    breakdown = _aggregate_usage_events(
        events,
        default_tts_provider="cosyvoice",
        default_tts_model="cosyvoice-v3-flash",
    )
    apply_costs(breakdown, DEFAULT_PRICE_CATALOG)

    row = breakdown.tts_rows[0]
    assert row.model == "cosyvoice-v3.5-flash"
    assert row.cost_rmb == 0.8
    assert any(
        "inferred_tts_model_from_voice:"
        "cosyvoice:speech-2.8-turbo->cosyvoice-v3.5-flash" in warning
        for warning in breakdown.warnings
    )


def test_cosyvoice_historical_v3_default_infers_v35_plus_from_worker_target_model():
    events = [
        {
            "kind": "tts",
            "bucket": "first_tts",
            "provider": "cosyvoice",
            "model": "cosyvoice-v3-flash",
            "worker_target_model": "cosyvoice-v3.5-plus",
            "billed_chars": 10_000,
        },
    ]

    breakdown = _aggregate_usage_events(events)
    apply_costs(breakdown, DEFAULT_PRICE_CATALOG)

    row = breakdown.tts_rows[0]
    assert row.model == "cosyvoice-v3.5-plus"
    assert row.cost_rmb == 1.5


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


def test_snapshot_breakdown_uses_llm_model_distribution(tmp_path):
    snapshot = {
        "llm_input_tokens": 1_010_000,
        "llm_output_tokens": 101_000,
        "llm_model_call_distribution": {
            "gemini:gemini-3.5-flash:s3_translate": 2,
            "gemini:gemini-3.1-pro-preview:s2_pass1": 1,
        },
        "s3_translate_llm_input_tokens": 1_000_000,
        "s3_translate_llm_output_tokens": 100_000,
        "s2_pass1_llm_input_tokens": 10_000,
        "s2_pass1_llm_output_tokens": 1_000,
    }

    breakdown = build_job_breakdown(
        project_dir=str(tmp_path),
        snapshot=snapshot,
        default_tts_provider="minimax",
        default_tts_model="speech-2.8-hd",
        catalog=DEFAULT_PRICE_CATALOG,
    )

    rows = {(row.provider, row.model_id, row.task): row for row in breakdown.llm_rows}
    assert rows[("gemini", "gemini-3.5-flash", "s3_translate")].rate_status == "configured"
    assert rows[("gemini", "gemini-3.5-flash", "s3_translate")].cost_rmb == 17.28
    assert rows[("gemini", "gemini-3.1-pro-preview", "s2_pass1")].rate_status == "configured"
    assert rows[("gemini", "gemini-3.1-pro-preview", "s2_pass1")].cost_rmb == 0.2304
    assert all(row.provider != "unknown" for row in breakdown.llm_rows)
    assert any("snapshot_llm_model_distribution_fallback" in warning for warning in breakdown.warnings)


def test_snapshot_breakdown_allocates_llm_residual_tokens_to_distribution_gap(tmp_path):
    snapshot = {
        "llm_input_tokens": 1_001_000,
        "llm_output_tokens": 100_100,
        "llm_model_call_distribution": {
            "gemini:gemini-3.5-flash:s3_translate": 2,
            "gemini:gemini-3.1-flash-lite:content_compliance": 1,
        },
        "s3_translate_llm_input_tokens": 1_000_000,
        "s3_translate_llm_output_tokens": 100_000,
    }

    breakdown = build_job_breakdown(
        project_dir=str(tmp_path),
        snapshot=snapshot,
        default_tts_provider="minimax",
        default_tts_model="speech-2.8-hd",
        catalog=DEFAULT_PRICE_CATALOG,
    )

    rows = {(row.provider, row.model_id, row.task): row for row in breakdown.llm_rows}
    residual = rows[("gemini", "gemini-3.1-flash-lite", "content_compliance")]
    assert residual.input_tokens == 1000
    assert residual.output_tokens == 100
    assert residual.rate_status == "configured"
    assert residual.cost_rmb == 0.00288
    assert any("snapshot_llm_residual_tokens_allocated" in warning for warning in breakdown.warnings)


def test_mimo_v25_llm_cost_text_and_cached():
    # Plan 2026-05-27 PR 1: MiMo LLM RMB-direct catalog entry.
    events = [
        {
            "kind": "llm",
            "provider": "mimo",
            "model_id": "mimo-v2.5",
            "task": "s3_translate",
            "phase": "s3",
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cached_input_tokens": 1_000_000,
            "success": True,
        },
    ]
    breakdown = _aggregate_usage_events(events)
    apply_costs(breakdown, DEFAULT_PRICE_CATALOG)

    assert len(breakdown.llm_rows) == 1
    row = breakdown.llm_rows[0]
    # input 1M*1.0 + output 1M*2.0 + cached 1M*0.02 = 3.02
    assert row.cost_rmb == 3.02
    assert row.rate_status == "configured"


def test_mimo_v25_pro_llm_rate_configured():
    events = [
        {
            "kind": "llm",
            "provider": "mimo",
            "model_id": "mimo-v2.5-pro",
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "success": True,
        },
    ]
    breakdown = _aggregate_usage_events(events)
    apply_costs(breakdown, DEFAULT_PRICE_CATALOG)

    assert breakdown.llm_rows[0].cost_rmb == 3.0
    assert breakdown.llm_rows[0].rate_status == "configured"


def test_mimo_omni_alias_bills_against_v25_catalog():
    # Plan Phase 0b: deprecated mimo_omni resolves to mimo-v2.5, so its
    # metering rows (model_id=mimo-v2.5) must hit the same catalog entry.
    events = [
        {
            "kind": "llm",
            "provider": "mimo",
            "model_id": "mimo-v2.5",
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "success": True,
        },
    ]
    breakdown = _aggregate_usage_events(events)
    apply_costs(breakdown, DEFAULT_PRICE_CATALOG)
    assert breakdown.llm_rows[0].rate_status == "configured"
    assert breakdown.llm_rows[0].cost_rmb == 1.0


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


def test_job_payload_warns_when_clone_capture_masks_missing_job_capture():
    payload = _job_payload(
        _job(metering_snapshot={"credits_estimated": 249, "quality_tier": "flagship"}),
        None,
        JobCostBreakdown(),
        point_price_rmb=0.03,
        point_price_source="test",
        server_cost_per_min_rmb=0.0,
        server_cost_source="test",
        ledger_capture_credits=500,
        ledger_job_capture_credits=0,
        ledger_voice_clone_capture_credits=500,
    )

    assert payload["credits_charged"] == 500
    assert payload["job_credits_charged"] == 0
    assert payload["voice_clone_credits_charged"] == 500
    assert any("missing_job_capture" in warning for warning in payload["warnings"])


def test_job_payload_margin_can_include_server_overhead():
    breakdown = JobCostBreakdown(
        llm_rows=[LLMRow(provider="deepseek", model="deepseek-v4-flash", model_id="deepseek-v4-flash", task="s3", phase="", cost_rmb=0.5)],
        tts_rows=[TTSRow(provider="minimax", model="speech-2.8-hd", bucket="first_tts", cost_rmb=0.5)],
        voice_clone_rows=[VoiceCloneRow(provider="minimax", model="voice_clone", bucket="voice_clone", cost_rmb=0.2)],
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
    assert payload["voice_clone_cost_rmb"] == 0.2
    assert payload["total_cost_rmb"] == 1.2
    assert payload["server_overhead_cost_rmb"] == 0.3
    assert payload["margin_cost_rmb"] == 1.5
    assert payload["gross_margin_pct"] == 50.0
