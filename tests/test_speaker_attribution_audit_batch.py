from __future__ import annotations

import json
from pathlib import Path

from scripts.benchmark.speaker_attribution_audit_batch import (
    AuditConfig,
    build_audit_batch,
    write_outputs,
)


def _write_job(
    root: Path,
    job_id: str,
    *,
    segments: list[dict],
    transcript_lines: list[dict] | None = None,
) -> Path:
    job_dir = root / "user_one" / job_id
    (job_dir / "translation").mkdir(parents=True)
    (job_dir / "transcript").mkdir(parents=True)
    (job_dir / "audio").mkdir(parents=True)
    (job_dir / "translation" / "segments.json").write_text(
        json.dumps({"segments": segments}, ensure_ascii=False),
        encoding="utf-8",
    )
    (job_dir / "transcript" / "transcript.json").write_text(
        json.dumps({"lines": transcript_lines or []}, ensure_ascii=False),
        encoding="utf-8",
    )
    (job_dir / "audio" / "original.wav").write_bytes(b"not-a-real-wav")
    return job_dir


def _write_report(path: Path, *, job_dir: Path) -> None:
    payload = {
        "jobs": [
            {
                "job_id": job_dir.name,
                "project_dir": job_dir.as_posix(),
                "source_key": "same_source",
                "title": "Audit Source",
                "risk_level": "high",
                "primary_speaker_id": "speaker_a",
                "speakers": {
                    "speaker_a": {
                        "display_name": "Main",
                        "role": "primary",
                        "duration_share": 0.94,
                        "segment_count": 8,
                    },
                    "speaker_b": {
                        "display_name": "Audience",
                        "role": "fragmented",
                        "duration_share": 0.03,
                        "segment_count": 1,
                        "reason": "low_share_fragmented",
                    },
                },
            }
        ],
        "duplicate_groups": [
            {
                "source_key": "same_source",
                "jobs": [job_dir.name, "job_other"],
                "best_by_cost": job_dir.name,
                "best_by_speaker": "job_other",
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_speaker_attribution_audit_batch_selects_low_support_candidates(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    job_dir = _write_job(
        root,
        "job_alpha",
        segments=[
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "display_name": "Main",
                "start_ms": 0,
                "end_ms": 20_000,
                "alignment_method": "direct",
                "source_text": "main speech",
            },
            {
                "segment_id": 2,
                "speaker_id": "speaker_b",
                "display_name": "Audience",
                "start_ms": 20_500,
                "end_ms": 22_000,
                "alignment_method": "force_dsp",
                "needs_review": True,
                "source_text": "short answer",
            },
        ],
        transcript_lines=[
            {"index": 1, "speaker_id": "speaker_a", "source_text": "main speech"},
            {"index": 2, "speaker_id": "speaker_a", "source_text": "short answer"},
        ],
    )
    report_path = tmp_path / "report.json"
    _write_report(report_path, job_dir=job_dir)

    batch = build_audit_batch(
        AuditConfig(
            report_path=report_path,
            projects_root=root,
            output_dir=tmp_path / "reports",
        )
    )

    assert batch["summary"]["target_jobs"] == 1
    assert batch["summary"]["candidates"] == 1
    candidate = batch["candidates"][0]
    assert candidate["assigned_speaker_id"] == "speaker_b"
    assert "fragmented_speaker" in candidate["reasons"]
    assert "asr_s2_speaker_changed" in candidate["reasons"]
    assert "duplicate_source_group" in candidate["reasons"]
    assert candidate["model_task"]["decision_options"]


def test_speaker_attribution_audit_batch_writes_json_md_and_model_inputs(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    job_dir = _write_job(
        root,
        "job_alpha",
        segments=[
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "display_name": "Main",
                "start_ms": 0,
                "end_ms": 20_000,
                "alignment_method": "direct",
            },
            {
                "segment_id": 2,
                "speaker_id": "speaker_b",
                "display_name": "Unknown",
                "start_ms": 20_500,
                "end_ms": 23_000,
                "alignment_method": "direct",
                "source_text": "maybe main speaker",
            },
        ],
    )
    report_path = tmp_path / "report.json"
    _write_report(report_path, job_dir=job_dir)
    config = AuditConfig(
        report_path=report_path,
        projects_root=root,
        output_dir=tmp_path / "reports",
        force=True,
    )
    batch = build_audit_batch(config)

    json_path, md_path, jsonl_path = write_outputs(batch, config)

    assert json_path.exists()
    assert md_path.exists()
    assert jsonl_path.exists()
    assert "P2-b Speaker Attribution Audit Batch" in md_path.read_text(encoding="utf-8")
    assert len(jsonl_path.read_text(encoding="utf-8").strip().splitlines()) == 1
