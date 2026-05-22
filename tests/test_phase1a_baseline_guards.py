from __future__ import annotations

import json
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO / "baselines" / "2026-05-baseline.json"
BASELINE_JOBS_PATH = REPO / "tests" / "fixtures" / "baseline_jobs.json"
BENCHMARK_MANIFEST_PATH = (
    REPO / "tests" / "fixtures" / "benchmark" / "video_translation_quality" / "manifest.json"
)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_phase1a_baseline_files_exist_and_are_relative_path_only() -> None:
    assert BASELINE_PATH.exists()
    assert BASELINE_JOBS_PATH.exists()

    serialized = BASELINE_PATH.read_text(encoding="utf-8") + BASELINE_JOBS_PATH.read_text(
        encoding="utf-8"
    )
    assert str(REPO) not in serialized
    assert ":\\" not in serialized
    assert "/opt/" not in serialized


def test_phase1a_baseline_jobs_pin_existing_benchmark_fixture() -> None:
    baseline_jobs = _load_json(BASELINE_JOBS_PATH)
    benchmark = _load_json(BENCHMARK_MANIFEST_PATH)

    jobs = baseline_jobs["jobs"]
    benchmark_jobs = {job["benchmark_id"]: job for job in benchmark["jobs"]}

    assert baseline_jobs["schema_version"] == "phase1a_baseline_jobs_v1"
    assert len(jobs) >= baseline_jobs["selection_policy"]["min_jobs"]
    assert set(job["benchmark_id"] for job in jobs) == set(benchmark_jobs)

    for job in jobs:
        benchmark_job = benchmark_jobs[job["benchmark_id"]]
        for key, fixture_key in (
            ("segment_count", "segment_count"),
            ("rewrite_count", "rewrite_count"),
            ("force_dsp_segments", "force_dsp_segments"),
            ("needs_review_segments", "needs_review_segments"),
            ("pre_tts_events", "pre_tts_events"),
            ("pre_tts_contradictions", "pre_tts_contradictions"),
            ("speaker_corrections", "speaker_corrections"),
        ):
            assert job["metrics"][key] == benchmark_job["metrics"][fixture_key]


def test_phase1a_baseline_aggregate_metrics_match_jobs_fixture() -> None:
    baseline = _load_json(BASELINE_PATH)
    baseline_jobs = _load_json(BASELINE_JOBS_PATH)

    jobs = baseline_jobs["jobs"]
    aggregates = baseline["aggregate_metrics"]
    assert baseline["schema_version"] == "phase1a_observability_baseline_v1"
    assert baseline["dataset"]["job_count"] == len(jobs)
    assert aggregates["segment_count"] == sum(job["metrics"]["segment_count"] for job in jobs)
    assert aggregates["rewrite_count"] == sum(job["metrics"]["rewrite_count"] for job in jobs)
    assert aggregates["capped_dsp_count"] == sum(
        job["metrics"]["force_dsp_segments"] for job in jobs
    )
    assert aggregates["needs_review_segments"] == sum(
        job["metrics"]["needs_review_segments"] for job in jobs
    )
    assert aggregates["pre_tts_events"] == sum(job["metrics"]["pre_tts_events"] for job in jobs)
    assert aggregates["pre_tts_contradictions"] == sum(
        job["metrics"]["pre_tts_contradictions"] for job in jobs
    )
    assert aggregates["speaker_corrections"] == sum(
        job["metrics"]["speaker_corrections"] for job in jobs
    )

    required_behavior_flags = {
        "AVT_TRANSLATION_SCRIPT_GATE",
        "AVT_TRANSLATION_SCRIPT_GATE_RETRY",
        "AVT_VOICE_SAMPLE_SCORING",
        "AVT_AUDIO_TAIL_TRIM",
        "AVT_WHISPER_QUALITY_GATE",
    }
    assert required_behavior_flags.issubset(
        set(baseline["rerun_contract"]["behavior_flags_that_require_diff"])
    )
