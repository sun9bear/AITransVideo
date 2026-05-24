from __future__ import annotations

import json
from pathlib import Path

from services.phase1b_report_summary import (
    build_phase1b_csv,
    build_phase1b_summary,
    summarize_project_reports,
)
from services.runtime_flags import runtime_flag


def test_summarize_project_reports_reads_all_phase1a_sidecars(tmp_path: Path) -> None:
    project = tmp_path / "job_1"
    (project / "reports").mkdir(parents=True)
    (project / "smart_clone_samples").mkdir()

    (project / "reports" / "translation_quality_report.json").write_text(
        json.dumps(
            {
                "schema_version": "translation_quality_report_v1",
                "checked_segments": 10,
                "issue_count": 2,
                "reason_counts": {"latin_dominant": 2},
            }
        ),
        encoding="utf-8",
    )
    (project / "reports" / "subtitle_width_report.json").write_text(
        json.dumps(
            {
                "schema_version": "subtitle_width_report_v1",
                "max_display_width": 32,
                "issue_count": 1,
                "issues": [{"width_units": 44}],
            }
        ),
        encoding="utf-8",
    )
    (project / "reports" / "speaker_evidence.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"decision": "changed", "reason_codes": ["speaker_changed"]}),
                json.dumps({"decision": "kept_uncertain", "reason_codes": ["fallback"]}),
            ]
        ),
        encoding="utf-8",
    )
    (project / "smart_clone_samples" / "speaker_a.manifest.v2.json").write_text(
        json.dumps(
            {
                "schema_version": "voice_sample_manifest_v2",
                "selected_sample_stats": {
                    "hard_reject_reasons": [],
                    "warnings": ["silence_ratio_above_warning"],
                },
                "candidate_scores": [
                    {"score": 0.7, "hard_reject_reasons": [], "warnings": []},
                    {
                        "score": 0.0,
                        "hard_reject_reasons": ["rms_below_threshold"],
                        "warnings": ["fragmented_candidate"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_project_reports(project, job_id="job_1")

    assert summary["translation_quality"]["issue_rate"] == 0.2
    assert summary["subtitle_width"]["max_width_units"] == 44
    assert summary["speaker_evidence"]["changed_count"] == 1
    assert summary["speaker_evidence"]["uncertain_count"] == 1
    assert summary["voice_sample_scoring"]["manifest_count"] == 1
    assert summary["voice_sample_scoring"]["hard_reject_candidate_count"] == 1
    assert summary["voice_sample_scoring"]["hard_reject_rate"] == 0.5


def test_build_phase1b_summary_and_csv(tmp_path: Path) -> None:
    project = tmp_path / "job_2"
    (project / "reports").mkdir(parents=True)
    (project / "reports" / "translation_quality_report.json").write_text(
        json.dumps({"checked_segments": 4, "issue_count": 1}),
        encoding="utf-8",
    )
    report = summarize_project_reports(project, job_id="job_2")
    row = {
        "job_id": "job_2",
        "service_mode": "studio",
        "status": "succeeded",
        "user_email": "admin@example.test",
        "display_name": "Demo",
        "created_at": "2026-05-24T00:00:00+00:00",
        "reports": {
            "translation_quality": report["translation_quality"],
            "subtitle_width": report["subtitle_width"],
            "speaker_evidence": report["speaker_evidence"],
            "voice_sample_scoring": report["voice_sample_scoring"],
        },
    }

    payload = build_phase1b_summary([row], days=30)
    assert payload["schema_version"] == "phase1b_report_summary_v1"
    assert payload["kpi"]["translation_issue_rate"] == 0.25
    assert payload["recommendations"]["translation_script_gate"]["status"] == "collect_more_data"

    csv_body = build_phase1b_csv([row]).decode("utf-8-sig")
    assert "job_2" in csv_body
    assert "translation_issue_rate" in csv_body.splitlines()[0]


def test_runtime_flag_admin_setting_overrides_env(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AVT_TRANSLATION_SCRIPT_GATE_SHADOW", "1")

    assert runtime_flag("AVT_TRANSLATION_SCRIPT_GATE_SHADOW") is True

    (config_dir / "admin_settings.json").write_text(
        json.dumps({"phase1b_translation_script_gate_shadow": False}),
        encoding="utf-8",
    )
    assert runtime_flag("AVT_TRANSLATION_SCRIPT_GATE_SHADOW") is False
