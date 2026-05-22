import json
from pathlib import Path

from services.speaker_evidence import (
    build_speaker_evidence_from_snapshots,
    speaker_evidence_path,
    write_speaker_evidence_jsonl,
)


def test_speaker_evidence_rows_do_not_contain_absolute_paths(tmp_path: Path) -> None:
    rows = build_speaker_evidence_from_snapshots(
        original_snapshot=[
            {
                "index": 1,
                "start_ms": 0,
                "end_ms": 1000,
                "speaker_id": "speaker_a",
                "source_text": "hello",
            }
        ],
        final_snapshot=[
            {
                "index": 1,
                "start_ms": 0,
                "end_ms": 1000,
                "speaker_id": "speaker_b",
                "source_text": "hello",
            }
        ],
        review_model="unit",
        has_audio=True,
    )

    target = speaker_evidence_path(tmp_path)
    assert write_speaker_evidence_jsonl(target, rows) is True

    line = target.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["schema_version"] == 1
    assert payload["line_id"] == "line_000001"
    assert payload["source_line_ids"] == ["line_000001"]
    assert payload["decision"] == "changed"
    assert payload["source"] == "reviewer"
    assert payload["initial_speaker_id"] == "speaker_a"
    assert payload["final_speaker_id"] == "speaker_b"
    assert str(tmp_path) not in line
