"""Phase 2b — load_segment_word_context kernel tests.

Covers (plan 2026-05-17 §5.4):
- happy path: words filtered to segment's [start_ms, end_ms]
- segment not found → EditingConflictError
- raw_assemblyai.json missing → available=False with empty words
- malformed words list → graceful filter (skip non-dicts)
- schema trim: only text/start/end/speaker keys returned
- safety cap at 1000 words
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.jobs.editing import enter_editing
from services.jobs.editing_segments import (
    EditingConflictError,
    load_segment_word_context,
)
from services.jobs.models import JOB_STATUS_SUCCEEDED, JobRecord
from services.jobs.store import JobStore


def _make_project(
    tmp_path: Path,
    *,
    words: list[dict] | None = None,
) -> Path:
    """Build a 1-segment editing-mode project with optional transcript."""
    project_dir = tmp_path / "projects" / "job_wc"
    (project_dir / "editor").mkdir(parents=True)
    baseline = [
        {
            "segment_id": "seg_001",
            "speaker_id": "A",
            "cn_text": "测试段落内容",
            "source_text": "this is a test segment",
            "start_ms": 5000,
            "end_ms": 15000,
        },
    ]
    (project_dir / "editor" / "segments.json").write_text(
        json.dumps(baseline, ensure_ascii=False), encoding="utf-8"
    )

    if words is not None:
        (project_dir / "transcript").mkdir(parents=True, exist_ok=True)
        (project_dir / "transcript" / "raw_assemblyai.json").write_text(
            json.dumps({"words": words}, ensure_ascii=False), encoding="utf-8"
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_wc",
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="x",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status=JOB_STATUS_SUCCEEDED,
        current_stage="completed",
        progress_message=None,
        created_at=now_iso,
        updated_at=now_iso,
        project_dir=str(project_dir),
        service_mode="studio",
    )
    store = JobStore(tmp_path / "jobs")
    store.save_job(record)
    enter_editing(record, store)
    return project_dir


def test_word_context_happy_filters_to_segment_range(tmp_path: Path) -> None:
    words = [
        {"text": "before", "start": 100, "end": 500, "speaker": "A", "confidence": 0.9},
        {"text": "first", "start": 5100, "end": 5600, "speaker": "A", "confidence": 0.95},
        {"text": "in", "start": 5700, "end": 5900, "speaker": "A", "confidence": 0.88},
        {"text": "range", "start": 10000, "end": 11000, "speaker": "B", "confidence": 0.92},
        {"text": "after", "start": 16000, "end": 17000, "speaker": "B", "confidence": 0.9},
    ]
    project_dir = _make_project(tmp_path, words=words)
    result = load_segment_word_context(project_dir, "seg_001")

    assert result["segment_id"] == "seg_001"
    assert result["available"] is True
    assert len(result["words"]) == 3, "should drop words outside [5000, 15000]"
    assert [w["text"] for w in result["words"]] == ["first", "in", "range"]
    # Schema trim — no confidence / channel in output
    for w in result["words"]:
        assert set(w.keys()) == {"text", "start", "end", "speaker"}


def test_word_context_segment_not_found(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path, words=[])
    with pytest.raises(EditingConflictError, match="segment_id"):
        load_segment_word_context(project_dir, "seg_does_not_exist")


def test_word_context_no_transcript(tmp_path: Path) -> None:
    """When raw_assemblyai.json is missing, return available=False + []."""
    project_dir = _make_project(tmp_path, words=None)  # no transcript dir
    result = load_segment_word_context(project_dir, "seg_001")

    assert result["segment_id"] == "seg_001"
    assert result["available"] is False
    assert result["words"] == []


def test_word_context_speaker_preserved(tmp_path: Path) -> None:
    """speaker label flows through unchanged (used by smart-prefill)."""
    words = [
        {"text": "alpha", "start": 5100, "end": 5500, "speaker": "A"},
        {"text": "beta", "start": 8000, "end": 8500, "speaker": "B"},
        {"text": "gamma", "start": 10000, "end": 10500, "speaker": "A"},
    ]
    project_dir = _make_project(tmp_path, words=words)
    result = load_segment_word_context(project_dir, "seg_001")
    assert [w["speaker"] for w in result["words"]] == ["A", "B", "A"]


def test_word_context_skips_malformed(tmp_path: Path) -> None:
    """Non-dict entries silently skipped; doesn't blow up."""
    words = [
        {"text": "good", "start": 5100, "end": 5500, "speaker": "A"},
        "not a dict",  # type: ignore[list-item]
        None,
        {"text": "also_good", "start": 6000, "end": 6500, "speaker": "B"},
    ]
    project_dir = _make_project(tmp_path, words=words)  # type: ignore[arg-type]
    result = load_segment_word_context(project_dir, "seg_001")
    assert len(result["words"]) == 2
    assert [w["text"] for w in result["words"]] == ["good", "also_good"]


def test_word_context_safety_cap_1000(tmp_path: Path) -> None:
    """Defensive cap — a segment with >1000 words shouldn't blow the
    response. (Realistic max is ~100 for very long single-segment;
    cap is just safety net.)"""
    words = [
        {"text": f"w{i}", "start": 5000 + i, "end": 5000 + i + 1, "speaker": "A"}
        for i in range(1500)
    ]
    project_dir = _make_project(tmp_path, words=words)
    result = load_segment_word_context(project_dir, "seg_001")
    assert len(result["words"]) == 1000  # capped
