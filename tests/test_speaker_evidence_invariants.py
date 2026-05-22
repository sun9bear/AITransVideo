from __future__ import annotations

import json
from pathlib import Path

from services.speaker_evidence import (
    build_speaker_evidence_from_snapshots,
    speaker_evidence_path,
    write_speaker_evidence_jsonl,
)


def test_speaker_evidence_path_is_job_scoped_reports_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "job_project"

    assert speaker_evidence_path(project_dir) == (
        project_dir / "reports" / "speaker_evidence.jsonl"
    )


def test_speaker_evidence_sidecar_uses_logic_ids_not_filesystem_identity(
    tmp_path: Path,
) -> None:
    rows = build_speaker_evidence_from_snapshots(
        original_snapshot=[
            {
                "index": 7,
                "start_ms": 100,
                "end_ms": 900,
                "speaker_id": "speaker_a",
                "source_text": "hello",
            }
        ],
        final_snapshot=[
            {
                "index": 7,
                "start_ms": 100,
                "end_ms": 900,
                "speaker_id": "speaker_b",
                "source_text": "hello",
            }
        ],
        review_model="unit",
        has_audio=True,
    )

    path = speaker_evidence_path(tmp_path)
    assert write_speaker_evidence_jsonl(path, rows) is True

    payload = json.loads(path.read_text(encoding="utf-8").strip())
    assert payload["line_id"] == "line_000007"
    assert payload["source_line_ids"] == ["line_000007"]
    assert payload["segment_id"] == 7
    assert payload["final_segment_id"] == "seg_000007"

    serialized = json.dumps(payload, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert ":\\" not in serialized
    assert "/opt/" not in serialized


def test_speaker_evidence_unmatched_original_is_explicit_fallback() -> None:
    rows = build_speaker_evidence_from_snapshots(
        original_snapshot=[],
        final_snapshot=[
            {
                "index": 3,
                "start_ms": 0,
                "end_ms": 500,
                "speaker_id": "speaker_a",
                "source_text": "new line",
            }
        ],
        review_model="unit",
        has_audio=False,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.decision == "kept_uncertain"
    assert row.source == "fallback"
    assert row.reason_codes == ["original_line_unmatched"]
    assert row.source_line_ids == []
