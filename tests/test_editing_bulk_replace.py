from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.jobs.editing import EDITING_SUBDIR
from services.jobs.editing_bulk_replace import (
    apply_bulk_replace_terms,
    preview_bulk_replace_terms,
)
from services.jobs.editing_segments import SEGMENT_STATUS_TEXT_DIRTY


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _seed_project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "project"
    editing_dir = project_dir / EDITING_SUBDIR
    _write_json(
        editing_dir / "segments.json",
        [
            {
                "segment_id": "1",
                "speaker_id": "speaker_a",
                "cn_text": "这个 token 是令牌。令牌需要解释。",
                "voice_id": "voice_a",
                "tts_provider": "minimax",
                "tts_model_key": "speech-2.8-turbo",
            },
            {
                "segment_id": "2",
                "speaker_id": "speaker_b",
                "cn_text": "另一个令牌在这里。",
                "voice_id": "voice_b",
                "tts_provider": "cosyvoice",
            },
            {
                "segment_id": "3",
                "speaker_id": "speaker_a",
                "cn_text": "这一段不命中。",
                "voice_id": "voice_a",
                "tts_provider": "minimax",
            },
        ],
    )
    _write_json(editing_dir / "segment_status.json", {"3": "voice_dirty"})
    _write_json(
        editing_dir / "voice_map.json",
        {
            "2": {
                "provider": "cosyvoice",
                "voice_id": "clone_b",
                "tts_model_key": "cosyvoice-v3",
            }
        },
    )
    _write_json(
        editing_dir / "speakers.json",
        {
            "version": 1,
            "speakers": [
                {
                    "speaker_id": "speaker_b",
                    "display_name": "B 老师",
                    "color": None,
                    "source": "editing",
                    "created_at": "2026-01-01T00:00:00Z",
                    "profile_status": "ready",
                    "profile_error": None,
                    "voice_profile": None,
                }
            ],
        },
    )
    (editing_dir / "tts_segments_draft").mkdir(parents=True)
    (editing_dir / "tts_segments_draft" / "1.wav").write_bytes(b"stale")
    return project_dir


def test_preview_bulk_replace_includes_speaker_and_voice(tmp_path: Path) -> None:
    project_dir = _seed_project(tmp_path)

    result = preview_bulk_replace_terms(
        project_dir,
        find="令牌",
        replace="词元",
    )

    assert result["segment_count"] == 2
    assert result["total_matches"] == 3
    by_id = {item["segment_id"]: item for item in result["matches"]}
    assert by_id["1"]["match_count"] == 2
    assert by_id["1"]["provider"] == "minimax"
    assert by_id["1"]["voice_id"] == "voice_a"
    assert by_id["1"]["after_text"] == "这个 token 是词元。词元需要解释。"
    assert by_id["2"]["speaker_display_name"] == "B 老师"
    assert by_id["2"]["provider"] == "cosyvoice"
    assert by_id["2"]["voice_id"] == "clone_b"
    assert by_id["2"]["tts_model_key"] == "cosyvoice-v3"


def test_apply_bulk_replace_marks_only_affected_segments_dirty(tmp_path: Path) -> None:
    project_dir = _seed_project(tmp_path)

    result = apply_bulk_replace_terms(
        project_dir,
        find="令牌",
        replace="词元",
        expected_segment_ids=["1", "2"],
        expected_total_matches=3,
    )

    assert result["replaced_segment_ids"] == ["1", "2"]
    by_id = {str(item["segment_id"]): item for item in result["segments"]}
    assert by_id["1"]["cn_text"] == "这个 token 是词元。词元需要解释。"
    assert by_id["2"]["cn_text"] == "另一个词元在这里。"
    assert by_id["3"]["cn_text"] == "这一段不命中。"
    assert result["segment_status"]["1"] == SEGMENT_STATUS_TEXT_DIRTY
    assert result["segment_status"]["2"] == SEGMENT_STATUS_TEXT_DIRTY
    assert result["segment_status"]["3"] == "voice_dirty"
    assert not (project_dir / EDITING_SUBDIR / "tts_segments_draft" / "1.wav").exists()


def test_apply_bulk_replace_rejects_stale_preview(tmp_path: Path) -> None:
    project_dir = _seed_project(tmp_path)

    with pytest.raises(Exception, match="preview is stale"):
        apply_bulk_replace_terms(
            project_dir,
            find="令牌",
            replace="词元",
            expected_segment_ids=["2"],
            expected_total_matches=1,
        )
