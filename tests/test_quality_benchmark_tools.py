from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts.benchmark.quality_dataset import (
    BuildPaths,
    build_baseline_report,
    build_quality_dataset,
    sanitize_text,
    validate_quality_dataset,
)


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_sanitize_text_redacts_urls_email_and_long_numbers() -> None:
    sanitized = sanitize_text(
        "Contact a@example.com at https://example.test/watch?v=1234567890",
        max_chars=80,
    )

    assert "[email]" in sanitized["snippet"]
    assert "[url]" in sanitized["snippet"]
    assert "example.com" not in sanitized["snippet"]
    assert "https://" not in sanitized["snippet"]


def test_build_validate_and_report_quality_dataset(tmp_path: Path) -> None:
    analysis_dir = tmp_path / "analysis"
    artifacts_root = tmp_path / "artifacts"
    output_dir = tmp_path / "tests" / "fixtures" / "benchmark" / "video_translation_quality"
    job_dir = artifacts_root / "projects" / "user_one" / "job_alpha"
    (job_dir / "transcript").mkdir(parents=True)
    (job_dir / "translation").mkdir(parents=True)
    (job_dir / "transcript" / "s2_review_audit.json").write_text(
        json.dumps({"speaker_corrections_applied": 1}),
        encoding="utf-8",
    )
    (job_dir / "translation" / "segments.json").write_text(
        json.dumps({"segments": []}),
        encoding="utf-8",
    )

    _write_csv(
        analysis_dir / "segment_trace.csv",
        [
            {
                "user_root": "user_one",
                "job_id": "job_alpha",
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "display_name": "Speaker A",
                "start_ms": 0,
                "end_ms": 1000,
                "target_duration_ms": 1000,
                "source_chars": 20,
                "cn_chars": 30,
                "pre_rewrite_direction": "shrink",
                "first_pass_error_pct_calc": -0.2,
                "alignment_method": "force_dsp",
                "rewrite_count": 1,
                "needs_review": "true",
                "tts_provider": "minimax",
            },
            {
                "user_root": "user_one",
                "job_id": "job_beta",
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "display_name": "Speaker A",
                "start_ms": 0,
                "end_ms": 2000,
                "target_duration_ms": 2000,
                "source_chars": 10,
                "cn_chars": 12,
                "alignment_method": "direct",
                "rewrite_count": 0,
                "needs_review": "false",
                "tts_provider": "volcengine",
            },
        ],
    )
    _write_csv(
        analysis_dir / "speaker_diff_summary.csv",
        [
            {
                "user_root": "user_one",
                "job_id": "job_alpha",
                "phase": "original_to_after_corrections",
                "position": 1,
                "before_speaker_id": "speaker_a",
                "after_speaker_id": "speaker_b",
                "start_ms": 0,
                "end_ms": 1000,
                "duration_ms": 1000,
                "source_text": "Email a@example.com and open https://example.test",
            }
        ],
    )
    _write_csv(
        analysis_dir / "job_metering_joined.csv",
        [
            {
                "job_id": "job_alpha",
                "status": "succeeded",
                "service_mode": "studio",
                "tts_provider": "minimax",
                "tts_model": "speech-2.8-hd",
                "actual_minutes": 1.5,
                "rewrite_count": 2,
                "tts_billed_chars": "",
                "credits_actual": "",
            }
        ],
    )

    manifest = build_quality_dataset(
        paths=BuildPaths(
            analysis_dir=analysis_dir,
            artifacts_root=artifacts_root,
            output_dir=output_dir,
        ),
        max_jobs=2,
        force=True,
    )
    assert len(manifest["jobs"]) == 2

    validation = validate_quality_dataset(output_dir)
    assert validation["status"] == "ok"

    report = build_baseline_report(
        dataset_dir=output_dir,
        output_dir=tmp_path / "reports",
    )
    assert report["quality_baseline"]["pre_tts_contradictions"] == 1
    assert report["quality_baseline"]["speaker_corrections"] == 1

    fixture_text = (output_dir / "jobs" / "bench_001" / "speaker_corrections.json").read_text(
        encoding="utf-8"
    )
    assert "https://" not in fixture_text
    assert "a@example.com" not in fixture_text


def test_validate_rejects_forbidden_source_ref(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "fixture"
    (dataset_dir / "jobs" / "bench_001").mkdir(parents=True)
    manifest = {
        "version": "video_translation_quality.v1",
        "jobs": [
            {
                "benchmark_id": "bench_001",
                "files": {"meta": "jobs/bench_001/job_meta.json"},
            }
        ],
    }
    (dataset_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (dataset_dir / "jobs" / "bench_001" / "job_meta.json").write_text(
        json.dumps({"source_ref": "https://example.test/video"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Forbidden data"):
        validate_quality_dataset(dataset_dir)
