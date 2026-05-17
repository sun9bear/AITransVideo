"""Phase 2a — split_editing_segment_many kernel + journal reconciler tests.

Covers (plan 2026-05-17 §5.6 + Codex round 2/3/5/6/7):
- happy path (N cuts → N+1 segments)
- validation (empty cuts / non-monotonic / out-of-bounds / speaker count
  mismatch / zero-duration piece in ms-space)
- voice_map migration (parent override → all N+1 sub-segs)
- draft wav cleanup (parent's draft .wav deleted on split)
- journal reconcile state A: journal fresh + parent still in segments
- journal reconcile state B: journal stale + sub-segs present + user has
  edited a sub-seg → reconciler does NOT overwrite user edits
- journal reconcile state C: mixed → raises EditingCorruptionError
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.jobs.editing import enter_editing
from services.jobs.editing_segments import (
    EditingCorruptionError,
    SEGMENT_STATUS_TEXT_DIRTY,
    _editing_dir,
    _read_segments_json_raw,
    _read_segment_status_json_raw,
    _reconcile_split_journal_if_needed,
    _split_journal_path,
    load_editing_segments,
    load_segment_status,
    split_editing_segment_many,
)
from services.jobs.editing_voice_map import (
    _voice_map_path,
    load_voice_map,
    set_voice_override,
)
from services.jobs.models import JOB_STATUS_SUCCEEDED, JobRecord
from services.jobs.store import JobStore


def _make_project(tmp_path: Path) -> Path:
    """Build a 1-segment editing-mode project. The single segment has
    long enough source/cn text to allow 2 cuts → 3 pieces."""
    project_dir = tmp_path / "projects" / "job_xyz"
    (project_dir / "editor").mkdir(parents=True)
    baseline = [
        {
            "segment_id": "seg_001",
            "speaker_id": "A",
            "cn_text": "你好世界你好朋友你好家人",  # 12 chars
            "source_text": "hello world hello friend hello family",  # 37 chars
            "start_ms": 0,
            "end_ms": 9000,
            "target_duration_ms": 9000,
        },
    ]
    (project_dir / "editor" / "segments.json").write_text(
        json.dumps(baseline, ensure_ascii=False), encoding="utf-8"
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_xyz",
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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_split_many_happy_two_cuts_three_pieces(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    result = split_editing_segment_many(
        project_dir,
        segment_id="seg_001",
        cuts=[
            {"source_index": 12, "cn_index": 4},
            {"source_index": 25, "cn_index": 8},
        ],
        speaker_ids=["A", "B", "A"],
    )
    assert result["replaced_segment_id"] == "seg_001"
    assert result["total_count"] == 3
    pieces = result["new_segments"]
    assert len(pieces) == 3
    assert [p["segment_id"] for p in pieces] == ["seg_001_a", "seg_001_b", "seg_001_c"]
    assert [p["speaker_id"] for p in pieces] == ["A", "B", "A"]
    # Text was sliced
    assert pieces[0]["source_text"] == "hello world "
    assert pieces[1]["source_text"] == "hello friend "
    assert pieces[2]["source_text"] == "hello family"
    assert pieces[0]["cn_text"] == "你好世界"
    assert pieces[1]["cn_text"] == "你好朋友"
    assert pieces[2]["cn_text"] == "你好家人"
    # Status — all three new ids text_dirty, parent gone
    status = load_segment_status(project_dir)
    for nid in ("seg_001_a", "seg_001_b", "seg_001_c"):
        assert status[nid] == SEGMENT_STATUS_TEXT_DIRTY
    assert "seg_001" not in status
    # Journal cleaned up on happy path
    assert not list(_editing_dir(project_dir).glob(".split_journal_*.json"))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_split_many_empty_cuts(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    with pytest.raises(ValueError, match="cuts must be a non-empty list"):
        split_editing_segment_many(
            project_dir,
            segment_id="seg_001",
            cuts=[],
            speaker_ids=["A"],
        )


def test_split_many_speaker_count_mismatch(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    with pytest.raises(ValueError, match="speaker_ids count .* must equal cuts\\+1"):
        split_editing_segment_many(
            project_dir,
            segment_id="seg_001",
            cuts=[{"source_index": 12, "cn_index": 4}],
            speaker_ids=["A", "B", "C"],  # too many
        )


def test_split_many_non_monotonic_source_index(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    with pytest.raises(ValueError, match="source_index .* must be strictly greater"):
        split_editing_segment_many(
            project_dir,
            segment_id="seg_001",
            cuts=[
                {"source_index": 20, "cn_index": 4},
                {"source_index": 12, "cn_index": 8},  # not monotonic
            ],
            speaker_ids=["A", "B", "C"],
        )


def test_split_many_cut_out_of_bounds(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    with pytest.raises(ValueError, match="source_index"):
        split_editing_segment_many(
            project_dir,
            segment_id="seg_001",
            cuts=[{"source_index": 999, "cn_index": 4}],
            speaker_ids=["A", "B"],
        )


# ---------------------------------------------------------------------------
# Voice_map migration (P0-8 pattern extended to N+1)
# ---------------------------------------------------------------------------


def test_split_many_voice_map_migration(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    # Set a voice override on the parent BEFORE splitting.
    set_voice_override(
        project_dir,
        "seg_001",
        provider="minimax",
        voice_id="vid_parent",
    )
    assert load_voice_map(project_dir)["seg_001"]["voice_id"] == "vid_parent"

    split_editing_segment_many(
        project_dir,
        segment_id="seg_001",
        cuts=[
            {"source_index": 12, "cn_index": 4},
            {"source_index": 25, "cn_index": 8},
        ],
        speaker_ids=["A", "B", "A"],
    )

    vm = load_voice_map(project_dir)
    # Parent override removed, all 3 sub-segs inherit it
    assert "seg_001" not in vm
    for nid in ("seg_001_a", "seg_001_b", "seg_001_c"):
        assert nid in vm, f"sub-seg {nid} missing voice override"
        assert vm[nid]["voice_id"] == "vid_parent"
        assert vm[nid]["provider"] == "minimax"


# ---------------------------------------------------------------------------
# Draft wav cleanup
# ---------------------------------------------------------------------------


def test_split_many_deletes_parent_draft_wav(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    # Manually create a fake parent draft wav (orphan after split).
    draft_dir = _editing_dir(project_dir) / "tts_segments_draft"
    draft_dir.mkdir(parents=True, exist_ok=True)
    parent_wav = draft_dir / "seg_001.wav"
    parent_wav.write_bytes(b"fake wav content")
    assert parent_wav.exists()

    split_editing_segment_many(
        project_dir,
        segment_id="seg_001",
        cuts=[{"source_index": 12, "cn_index": 4}],
        speaker_ids=["A", "B"],
    )

    # Parent's draft must be gone (would otherwise be picked up by
    # commit's draft-promotion phase as an orphan).
    assert not parent_wav.exists()


# ---------------------------------------------------------------------------
# Journal reconcile — state A (fresh journal, parent still in segments)
# ---------------------------------------------------------------------------


def test_reconcile_state_a_applies_journal(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    # Manually plant a journal: segments.json STILL has seg_001 (split
    # didn't get past step 5). Reconciler should apply the journal:
    # overwrite segments.json + status.json + voice_map.json.
    journal_payload = {
        "schema_version": 1,
        "parent_sid": "seg_001",
        "new_sub_segment_ids": ["seg_001_a", "seg_001_b"],
        "new_segments": [
            {
                "segment_id": "seg_001_a",
                "speaker_id": "A",
                "cn_text": "你好",
                "source_text": "hello ",
                "start_ms": 0,
                "end_ms": 4000,
            },
            {
                "segment_id": "seg_001_b",
                "speaker_id": "B",
                "cn_text": "世界",
                "source_text": "world",
                "start_ms": 4000,
                "end_ms": 9000,
            },
        ],
        "new_status": {
            "seg_001_a": "text_dirty",
            "seg_001_b": "text_dirty",
        },
        "new_voice_map": {},
    }
    journal_path = _split_journal_path(project_dir, "seg_001")
    journal_path.write_text(json.dumps(journal_payload), encoding="utf-8")

    # Call public loader — triggers reconcile via state A path
    segs = load_editing_segments(project_dir)
    ids = [s["segment_id"] for s in segs]
    assert ids == ["seg_001_a", "seg_001_b"]
    status = load_segment_status(project_dir)
    assert status["seg_001_a"] == "text_dirty"
    # Journal removed after apply
    assert not journal_path.exists()


# ---------------------------------------------------------------------------
# Journal reconcile — state B (journal stale; subs already in segments;
# user may have already edited a sub-seg; reconciler MUST NOT overwrite)
# ---------------------------------------------------------------------------


def test_reconcile_state_b_preserves_user_edits(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    # Do a real split — leaves three files updated + journal deleted.
    split_editing_segment_many(
        project_dir,
        segment_id="seg_001",
        cuts=[{"source_index": 12, "cn_index": 4}],
        speaker_ids=["A", "B"],
    )
    # Verify split landed
    segs = _read_segments_json_raw(project_dir)
    sub_ids = [s["segment_id"] for s in segs]
    assert sub_ids == ["seg_001_a", "seg_001_b"]

    # Simulate user edit to sub-seg AFTER split: change cn_text via direct
    # write (mimics a later patch_editing_segment + then journal-delete-
    # failure-on-next-split scenario, plus user editing in between).
    segs[0]["cn_text"] = "用户已经手动改过的内容"
    (_editing_dir(project_dir) / "segments.json").write_text(
        json.dumps(segs, ensure_ascii=False), encoding="utf-8"
    )

    # Plant a STALE journal mimicking "delete-step failed" — payload
    # still has the OLD sub-seg cn_text. State B reconciler must NOT
    # overwrite segments.json (would clobber user's edit).
    stale_payload = {
        "schema_version": 1,
        "parent_sid": "seg_001",
        "new_sub_segment_ids": ["seg_001_a", "seg_001_b"],
        "new_segments": [
            {
                "segment_id": "seg_001_a",
                "speaker_id": "A",
                "cn_text": "你好",  # stale!
                "source_text": "hello world ",
                "start_ms": 0,
                "end_ms": 4000,
            },
            {
                "segment_id": "seg_001_b",
                "speaker_id": "B",
                "cn_text": "朋友家人",
                "source_text": "hello friend hello family",
                "start_ms": 4000,
                "end_ms": 9000,
            },
        ],
        "new_status": {
            "seg_001_a": "text_dirty",
            "seg_001_b": "text_dirty",
        },
        "new_voice_map": {},
    }
    journal_path = _split_journal_path(project_dir, "seg_001")
    journal_path.write_text(json.dumps(stale_payload, ensure_ascii=False), encoding="utf-8")

    # Trigger reconcile
    segs_after = load_editing_segments(project_dir)
    # State B: segments.json NOT overwritten — user's edit preserved
    cn_a = next(s["cn_text"] for s in segs_after if s["segment_id"] == "seg_001_a")
    assert cn_a == "用户已经手动改过的内容", (
        f"State B reconciler clobbered user edit; got {cn_a!r}"
    )
    # Journal removed
    assert not journal_path.exists()


# ---------------------------------------------------------------------------
# Journal reconcile — state C (mixed state, must raise)
# ---------------------------------------------------------------------------


def test_reconcile_state_c_raises_corruption(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    # Plant a journal AND leave segments.json with NEITHER parent NOR
    # all subs present → state C (inconsistent).
    seg_path = _editing_dir(project_dir) / "segments.json"
    seg_path.write_text(
        json.dumps([
            {
                "segment_id": "seg_001_a",  # one sub present
                "speaker_id": "A",
                "cn_text": "x",
                "source_text": "x",
                "start_ms": 0,
                "end_ms": 1000,
            },
            # seg_001_b MISSING; parent seg_001 also MISSING
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    journal_path = _split_journal_path(project_dir, "seg_001")
    journal_path.write_text(
        json.dumps({
            "schema_version": 1,
            "parent_sid": "seg_001",
            "new_sub_segment_ids": ["seg_001_a", "seg_001_b"],  # _b missing in segments
            "new_segments": [],
            "new_status": {},
            "new_voice_map": {},
        }),
        encoding="utf-8",
    )

    with pytest.raises(EditingCorruptionError, match="inconsistent state"):
        _reconcile_split_journal_if_needed(project_dir)
    # Journal stays for operator inspection
    assert journal_path.exists()
