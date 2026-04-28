from __future__ import annotations

import json
from pathlib import Path

from scripts.benchmark.speaker_attribution_report import (
    ReportConfig,
    build_report,
    render_markdown,
    write_report,
)


def _write_job(
    root: Path,
    job_id: str,
    *,
    title: str,
    segments: list[dict],
) -> Path:
    job_dir = root / "user_one" / job_id
    (job_dir / "translation").mkdir(parents=True)
    (job_dir / "download_metadata.json").write_text(
        json.dumps({"video_title": title, "url": "https://example.test/video"}),
        encoding="utf-8",
    )
    (job_dir / "translation" / "segments.json").write_text(
        json.dumps({"segments": segments, "total_segments": len(segments)}),
        encoding="utf-8",
    )
    return job_dir


def test_speaker_attribution_report_flags_fragmented_low_share_speakers(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    segments = [
        {
            "segment_id": 1,
            "speaker_id": "speaker_a",
            "display_name": "Main",
            "start_ms": 0,
            "end_ms": 90_000,
            "speaker_role": "primary",
            "speaker_duration_share": 0.9,
            "speaker_duration_ms": 900_000,
            "speaker_segment_count": 10,
            "alignment_method": "direct",
        },
        {
            "segment_id": 2,
            "speaker_id": "speaker_b",
            "display_name": "Unknown",
            "start_ms": 90_000,
            "end_ms": 94_000,
            "speaker_role": "fragmented",
            "speaker_duration_share": 0.05,
            "speaker_duration_ms": 50_000,
            "speaker_segment_count": 4,
            "speaker_short_segment_count": 3,
            "speaker_short_segment_rate": 0.75,
            "speaker_structure_reason": "low_share_fragmented",
            "alignment_method": "force_dsp",
            "force_dsp_severity": "high",
            "needs_review": True,
            "source_text": "short fragment",
        },
        {
            "segment_id": 3,
            "speaker_id": "speaker_c",
            "display_name": "Audience",
            "start_ms": 95_000,
            "end_ms": 98_000,
            "speaker_role": "fragmented",
            "speaker_duration_share": 0.05,
            "speaker_duration_ms": 50_000,
            "speaker_segment_count": 4,
            "speaker_short_segment_count": 3,
            "speaker_short_segment_rate": 0.75,
            "speaker_structure_reason": "low_share_fragmented",
            "alignment_method": "force_dsp",
            "force_dsp_severity": "high",
            "needs_review": True,
            "source_text": "another fragment",
        },
    ]
    _write_job(root, "job_alpha", title="Alpha", segments=segments)

    report = build_report(
        ReportConfig(
            projects_root=root,
            output_dir=tmp_path / "reports",
            force=True,
        )
    )
    assert report["job_count"] == 1
    assert report["high_risk_job_count"] == 1
    assert report["jobs"][0]["risk_reasons"] == [
        "dominant_primary_with_multiple_fragmented_speakers"
    ]

    json_path, md_path = write_report(
        report,
        ReportConfig(projects_root=root, output_dir=tmp_path / "reports", force=True),
    )
    assert json_path.exists()
    assert md_path.exists()
    assert "P2 Speaker Attribution Convergence Report" in render_markdown(report)


def test_speaker_attribution_report_falls_back_to_segment_durations(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _write_job(
        root,
        "job_legacy",
        title="Legacy",
        segments=[
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "display_name": "Main",
                "start_ms": 0,
                "end_ms": 90_000,
                "alignment_method": "direct",
            },
            {
                "segment_id": 2,
                "speaker_id": "speaker_b",
                "display_name": "Guest",
                "start_ms": 90_000,
                "end_ms": 100_000,
                "alignment_method": "direct",
            },
        ],
    )

    report = build_report(
        ReportConfig(
            projects_root=root,
            output_dir=tmp_path / "reports",
            force=True,
        )
    )

    job = report["jobs"][0]
    assert job["primary_speaker_id"] == "speaker_a"
    assert job["primary_share"] == 0.9
    assert job["speakers"]["speaker_a"]["duration_ms"] == 90_000


def test_speaker_attribution_report_includes_duplicate_job_summaries(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    segments = [
        {
            "segment_id": 1,
            "speaker_id": "speaker_a",
            "display_name": "Main",
            "start_ms": 0,
            "end_ms": 10_000,
            "alignment_method": "direct",
        }
    ]
    _write_job(root, "job_one", title="Same Source", segments=segments)
    _write_job(root, "job_two", title="Same Source", segments=segments)

    report = build_report(
        ReportConfig(
            projects_root=root,
            output_dir=tmp_path / "reports",
            force=True,
        )
    )

    assert report["summary"]["duplicate_groups"] == 1
    assert report["duplicate_group_count"] == 1
    group = report["duplicate_groups"][0]
    assert group["jobs"] == ["job_one", "job_two"]
    assert [item["job_id"] for item in group["job_summaries"]] == ["job_one", "job_two"]
    assert "force_dsp_count" in group["job_summaries"][0]
