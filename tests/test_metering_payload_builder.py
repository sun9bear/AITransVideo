from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from pipeline.process import _build_job_metering_payload


def test_build_job_metering_payload_counts_core_fields_without_network():
    segments = [
        SimpleNamespace(
            merged_cn_text="abcd",
            rewrite_count=0,
            catalog_hit=True,
            needs_review=False,
            alignment_method="direct",
            dsp_speed_param=1.0,
            target_duration_ms=1_500,
        ),
        SimpleNamespace(
            merged_cn_text="ef",
            rewrite_count=2,
            catalog_hit=False,
            needs_review=True,
            alignment_method="force_dsp",
            force_dsp_severity="high",
            force_dsp_review_suppressed=True,
            dsp_speed_param=1.12,
            target_duration_ms=900,
            first_pass_error_pct=-0.25,
        ),
    ]

    body = _build_job_metering_payload(segments)

    assert body["final_cn_chars"] == 6
    assert body["rewrite_triggered"] is True
    assert body["rewrite_count"] == 2
    assert body["total_segments"] == 2
    assert body["catalog_hit_count"] == 1
    assert body["catalog_hit_rate"] == 0.5
    assert body["skip_probe"] is False
    assert body["needs_review_count"] == 1
    assert body["needs_review_rate"] == 0.5
    assert body["micro_segment_count"] == 1
    assert body["alignment_method_distribution"] == {"direct": 1, "force_dsp": 1}
    assert body["speed_param_distribution"] == {"1.0": 1, "in_range": 0, "outside": 1}
    assert body["force_dsp_severity_distribution"] == {"high": 1}
    assert body["force_dsp_review_suppressed_count"] == 1
    assert body["first_pass_error_pct_avg"] == 0.25
    assert body["first_pass_error_pct_p50"] == 0.25
    assert body["first_pass_error_pct_p90"] == 0.25
    assert body["first_pass_error_pct_n"] == 1


def test_build_job_metering_payload_merges_optional_and_short_merge_fields():
    segments = [
        SimpleNamespace(
            cn_text="abcd",
            rewrite_count=0,
            target_duration_ms=1_500,
            short_merge_candidate=True,
            short_merge_applied=True,
            short_merge_absorbed_segment_ids="2,3,bad",
            auto_keep_original_reason="provider_unavailable",
        )
    ]

    body = _build_job_metering_payload(
        segments,
        tts_billed_chars=42,
        extra_metering={"usage_events_count": 5},
    )

    assert body["tts_billed_chars"] == 42
    assert body["usage_events_count"] == 5
    assert body["short_merge_candidate_count"] == 1
    assert body["short_merge_applied_count"] == 1
    assert body["short_merge_absorbed_count"] == 2
    assert body["auto_keep_original_count"] == 1
    assert body["auto_keep_original_reason_distribution"] == {
        "provider_unavailable": 1
    }


def test_build_job_metering_payload_includes_glossary_preservation_rate(monkeypatch):
    import services.gemini.translator as translator

    segments = [
        SimpleNamespace(cn_text="term one kept", rewrite_count=0, target_duration_ms=1_500),
    ]
    glossary = {"term_one": "term one", "term_two": "term two"}

    def fake_check_glossary_preservation(passed_segments, passed_glossary):
        assert passed_segments is segments
        assert passed_glossary is glossary
        return {
            "total_terms": 4,
            "preserved_terms": 3,
            "missing_terms": ["term_two"],
        }

    monkeypatch.setattr(
        translator,
        "check_glossary_preservation",
        fake_check_glossary_preservation,
    )

    body = _build_job_metering_payload(segments, glossary=glossary)

    assert body["glossary_total_terms"] == 4
    assert body["glossary_preserved_terms"] == 3
    assert body["term_preservation_rate"] == 0.75
    assert body["missing_glossary_terms"] == ["term_two"]


def test_build_job_metering_payload_ignores_glossary_checker_failure(monkeypatch):
    import services.gemini.translator as translator

    segments = [
        SimpleNamespace(cn_text="term one kept", rewrite_count=0, target_duration_ms=1_500),
    ]

    def fake_check_glossary_preservation(_segments, _glossary):
        raise RuntimeError("glossary checker failed")

    monkeypatch.setattr(
        translator,
        "check_glossary_preservation",
        fake_check_glossary_preservation,
    )

    body = _build_job_metering_payload(segments, glossary={"term_one": "term one"})

    assert body["final_cn_chars"] == len("term one kept")
    assert "glossary_total_terms" not in body
    assert "term_preservation_rate" not in body
