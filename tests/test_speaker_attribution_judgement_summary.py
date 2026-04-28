from __future__ import annotations

import json
from pathlib import Path

from scripts.benchmark.speaker_attribution_judgement_summary import (
    SummaryConfig,
    build_summary,
    render_markdown,
    write_outputs,
)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_speaker_attribution_judgement_summary_dedupes_and_counts(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.json"
    _write_json(
        audit_path,
        {
            "candidates": [
                {
                    "candidate_id": "job_a_cand_001",
                    "job_id": "job_a",
                    "segment_id": 1,
                    "assigned_speaker_id": "speaker_b",
                    "assigned_display_name": "Audience",
                    "duration_ms": 1500,
                    "reasons": ["non_primary_speaker", "short_interaction"],
                },
                {
                    "candidate_id": "job_a_cand_002",
                    "job_id": "job_a",
                    "segment_id": 2,
                    "assigned_speaker_id": "speaker_c",
                    "assigned_display_name": "Music",
                    "duration_ms": 9000,
                    "reasons": ["non_primary_speaker", "long_low_support_segment"],
                },
            ]
        },
    )
    part1 = tmp_path / "part1.json"
    part2 = tmp_path / "part2.json"
    _write_json(
        part1,
        {
            "summary": {"decisions": 2},
            "decisions": [
                {
                    "candidate_id": "job_a_cand_001",
                    "decision": "s2_speaker",
                    "confidence": "high",
                    "recommended_action": "keep",
                    "reason": "assigned voice matches",
                },
                {
                    "candidate_id": "job_a_cand_002",
                    "decision": "music_or_non_speech",
                    "confidence": "high",
                    "recommended_action": "mark_non_speech",
                    "reason": "music only",
                },
            ],
        },
    )
    _write_json(
        part2,
        {
            "summary": {"decisions": 1},
            "decisions": [
                {
                    "candidate_id": "job_a_cand_001",
                    "decision": "main_speaker",
                    "confidence": "high",
                    "recommended_action": "reassign_to_main",
                    "reason": "duplicate should be ignored",
                }
            ],
        },
    )

    payload = build_summary(
        SummaryConfig(
            audit_batch=audit_path,
            judgement_files=(part1, part2),
            output_dir=tmp_path / "reports",
        )
    )

    assert payload["summary"]["audit_candidates"] == 2
    assert payload["summary"]["judged_unique_candidates"] == 2
    assert payload["summary"]["duplicate_decisions_ignored"] == 1
    assert payload["summary"]["decision_counts"] == {
        "s2_speaker": 1,
        "music_or_non_speech": 1,
    }
    assert payload["summary"]["recommended_action_counts"] == {
        "keep": 1,
        "mark_non_speech": 1,
    }
    assert payload["summary"]["non_keep_count"] == 1
    assert payload["reason_action_counts"]["non_primary_speaker"] == {
        "keep": 1,
        "mark_non_speech": 1,
    }
    assert payload["go_no_go"]["phrase_or_title_specific_rules"]["decision"] == "NO_GO"


def test_speaker_attribution_judgement_summary_writes_outputs(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.json"
    part_path = tmp_path / "part.json"
    _write_json(
        audit_path,
        {
            "candidates": [
                {
                    "candidate_id": "job_b_cand_001",
                    "job_id": "job_b",
                    "segment_id": 5,
                    "assigned_speaker_id": "speaker_b",
                    "reasons": ["low_duration_share"],
                }
            ]
        },
    )
    _write_json(
        part_path,
        {
            "decisions": [
                {
                    "candidate_id": "job_b_cand_001",
                    "decision": "main_speaker",
                    "confidence": "high",
                    "recommended_action": "reassign_to_main",
                    "reason": "same voice",
                }
            ]
        },
    )
    config = SummaryConfig(
        audit_batch=audit_path,
        judgement_files=(part_path,),
        output_dir=tmp_path / "reports",
        force=True,
    )
    payload = build_summary(config)
    json_path, md_path = write_outputs(payload, config)

    assert json_path.exists()
    assert md_path.exists()
    text = render_markdown(payload)
    assert "P2 Speaker Attribution Judgement Summary" in text
    assert "`verifier_gated_main_reassignment`" in text
