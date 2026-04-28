from __future__ import annotations

import json
import sys
from pathlib import Path


_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from services.jobs.review_actions import (  # noqa: E402
    extract_speaker_audio_segment,
    get_speaker_audio_segments,
    reassign_speaker_audio_segment,
    set_speaker_audio_dubbing_mode,
)
from services.review_state import (  # noqa: E402
    REVIEW_STATUS_PENDING,
    VOICE_SELECTION_REVIEW_STAGE,
    ReviewStateManager,
)


def _write_project(project_dir: Path) -> None:
    transcript_dir = project_dir / "transcript"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "transcript.json").write_text(
        json.dumps(
            {
                "lines": [
                    {
                        "index": 1,
                        "speaker_id": "speaker_a",
                        "speaker_label": "A",
                        "start_ms": 0,
                        "end_ms": 5_000,
                        "source_text": "Hello.",
                    },
                    {
                        "index": 2,
                        "speaker_id": "speaker_b",
                        "speaker_label": "B",
                        "start_ms": 5_000,
                        "end_ms": 11_000,
                        "source_text": "World.",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ReviewStateManager(project_dir / "review_state.json").set_stage(
        VOICE_SELECTION_REVIEW_STAGE,
        status=REVIEW_STATUS_PENDING,
        payload={
            "speakers": [
                {
                    "speaker_id": "speaker_a",
                    "speaker_name": "主持人",
                    "segment_count": 1,
                    "total_duration_s": 5.0,
                },
                {
                    "speaker_id": "speaker_b",
                    "speaker_name": "嘉宾",
                    "segment_count": 1,
                    "total_duration_s": 6.0,
                },
            ]
        },
        activate=True,
    )


def test_reassign_speaker_audio_segment_updates_transcript_and_review_payload(tmp_path: Path) -> None:
    _write_project(tmp_path)

    result = reassign_speaker_audio_segment(
        project_dir=tmp_path,
        segment_id=1,
        from_speaker_id="speaker_a",
        to_speaker_id="speaker_b",
    )

    assert result["changed"] is True
    transcript = json.loads((tmp_path / "transcript" / "transcript.json").read_text(encoding="utf-8"))
    lines = transcript["lines"]
    assert lines[0]["speaker_id"] == "speaker_b"
    assert lines[0]["speaker_label"] == "B"
    assert lines[0]["speaker_reassigned_from"] == "speaker_a"

    speaker_a = get_speaker_audio_segments(project_dir=tmp_path, speaker_id="speaker_a")
    speaker_b = get_speaker_audio_segments(project_dir=tmp_path, speaker_id="speaker_b")
    assert speaker_a["segments"] == []
    assert [segment["segment_id"] for segment in speaker_b["segments"]] == [1, 2]

    stage = ReviewStateManager(tmp_path / "review_state.json").get_stage(VOICE_SELECTION_REVIEW_STAGE)
    assert stage is not None
    speakers = {
        speaker["speaker_id"]: speaker
        for speaker in stage["payload"]["speakers"]
    }
    assert speakers["speaker_a"]["segment_count"] == 0
    assert speakers["speaker_a"]["total_duration_s"] == 0.0
    assert speakers["speaker_b"]["segment_count"] == 2
    assert speakers["speaker_b"]["total_duration_s"] == 11.0
    assert stage["payload"]["speaker_reassignment_history"][0]["segment_id"] == 1


def test_extract_speaker_audio_segment_rejects_stale_speaker_cache(tmp_path: Path) -> None:
    _write_project(tmp_path)
    stale_cache = tmp_path / "speaker_audio" / "speaker_a" / "segment_1.wav"
    stale_cache.parent.mkdir(parents=True)
    stale_cache.write_bytes(b"stale")

    reassign_speaker_audio_segment(
        project_dir=tmp_path,
        segment_id=1,
        from_speaker_id="speaker_a",
        to_speaker_id="speaker_b",
    )

    try:
        extract_speaker_audio_segment(
            project_dir=tmp_path,
            speaker_id="speaker_a",
            segment_id=1,
        )
    except ValueError as exc:
        assert "找不到 speaker_a" in str(exc)
    else:
        raise AssertionError("stale speaker cache should not bypass transcript ownership")


def test_set_speaker_audio_dubbing_mode_persists_segment_mode(tmp_path: Path) -> None:
    _write_project(tmp_path)
    translation_dir = tmp_path / "translation"
    translation_dir.mkdir()
    (translation_dir / "segments.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"segment_id": 2, "speaker_id": "speaker_b", "dubbing_mode": "dub"}
                ],
                "total_segments": 1,
                "output_path": str(translation_dir / "segments.json"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = set_speaker_audio_dubbing_mode(
        project_dir=tmp_path,
        segment_id=2,
        speaker_id="speaker_b",
        dubbing_mode="keep_original",
    )

    assert result["changed"] is True
    assert result["dubbing_mode"] == "keep_original"
    assert result["segment_snapshot_update_count"] == 1

    transcript = json.loads((tmp_path / "transcript" / "transcript.json").read_text(encoding="utf-8"))
    line = transcript["lines"][1]
    assert line["dubbing_mode"] == "keep_original"
    assert "dubbing_mode_updated_at" in line
    translation = json.loads((translation_dir / "segments.json").read_text(encoding="utf-8"))
    assert translation["segments"][0]["dubbing_mode"] == "keep_original"

    speaker_b = get_speaker_audio_segments(project_dir=tmp_path, speaker_id="speaker_b")
    assert speaker_b["segments"][0]["dubbing_mode"] == "keep_original"

    stage = ReviewStateManager(tmp_path / "review_state.json").get_stage(VOICE_SELECTION_REVIEW_STAGE)
    assert stage is not None
    history = stage["payload"]["dubbing_mode_history"]
    assert history[0]["segment_id"] == 2
    assert history[0]["previous_mode"] == "dub"
    assert history[0]["dubbing_mode"] == "keep_original"
