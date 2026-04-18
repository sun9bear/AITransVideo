"""T1-6 — batch re-TTS + voice_map unit tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.jobs.editing import EDITING_SUBDIR, enter_editing
from services.jobs.editing_batch import (
    BATCH_REGENERATE_TRIGGER_STATUSES,
    regenerate_all_dirty_segments,
)
from services.jobs.editing_segments import (
    SEGMENT_STATUS_ACCEPTED,
    SEGMENT_STATUS_TEXT_DIRTY,
    SEGMENT_STATUS_TTS_DIRTY,
    SEGMENT_STATUS_TTS_FAILED,
    SEGMENT_STATUS_VOICE_DIRTY,
    load_segment_status,
    mark_segment_status,
)
from services.jobs.editing_tts import TtsNotWiredError
from services.jobs.editing_voice_map import (
    VOICE_MAP_ENTRY_FIELDS,
    VOICE_MAP_FILE,
    clear_voice_override,
    load_voice_map,
    set_voice_override,
)
from services.jobs.editing import EditingConflictError
from services.jobs.models import JOB_STATUS_SUCCEEDED, JobRecord
from services.jobs.service import JobService
from services.jobs.store import JobStore


class _NullRunner:
    pass


def _build_editing_job(tmp_path: Path, *, n_segments: int = 3) -> tuple[JobService, Path]:
    project_dir = tmp_path / "projects" / "job_batch"
    editor_dir = project_dir / "editor"
    editor_dir.mkdir(parents=True)
    (editor_dir / "tts_segments").mkdir()
    segments = []
    for i in range(1, n_segments + 1):
        sid = f"seg_{i:03d}"
        (editor_dir / "tts_segments" / f"{sid}.wav").write_bytes(f"BASE_{sid}".encode())
        segments.append({
            "segment_id": sid,
            "speaker_id": "A" if i % 2 else "B",
            "cn_text": f"段落{i}",
            "start_ms": (i - 1) * 1000,
            "end_ms": i * 1000,
            "voice_id": "baseline_voice",
        })
    (editor_dir / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False), encoding="utf-8"
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_batch",
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://example.com/v",
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
    service = JobService(store=store, runner=_NullRunner())
    return service, project_dir


def _fake_tts_caller():
    """TTS caller that always succeeds, writing unique bytes per segment."""
    calls: list[str] = []

    def caller(segment, output_path):
        calls.append(segment["segment_id"])
        output_path.write_bytes(f"TTS_{segment['segment_id']}".encode())

    caller.calls = calls  # type: ignore[attr-defined]
    return caller


# ---------------------------------------------------------------------------
# editing_voice_map
# ---------------------------------------------------------------------------


def test_voice_map_empty_by_default(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    assert load_voice_map(project_dir) == {}


def test_set_voice_override_persists_and_flags_dirty(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    result = set_voice_override(
        project_dir, "seg_001",
        provider="minimax", voice_id="male_1",
    )
    assert result == {"segment_id": "seg_001", "provider": "minimax", "voice_id": "male_1"}
    vm = load_voice_map(project_dir)
    assert vm == {"seg_001": {"provider": "minimax", "voice_id": "male_1"}}
    assert load_segment_status(project_dir) == {"seg_001": "voice_dirty"}


def test_set_voice_override_overwrite_semantics(tmp_path: Path) -> None:
    """Plan H3: same segment second set replaces, no history stack."""
    _, project_dir = _build_editing_job(tmp_path)
    set_voice_override(project_dir, "seg_001", provider="minimax", voice_id="v1")
    set_voice_override(project_dir, "seg_001", provider="cosyvoice", voice_id="v2")
    vm = load_voice_map(project_dir)
    assert vm == {"seg_001": {"provider": "cosyvoice", "voice_id": "v2"}}


def test_clear_voice_override_removes_entry(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    set_voice_override(project_dir, "seg_001", provider="minimax", voice_id="v1")
    result = clear_voice_override(project_dir, "seg_001")
    assert result["cleared"] is True
    assert load_voice_map(project_dir) == {}
    assert load_segment_status(project_dir) == {}


def test_clear_voice_override_idempotent(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    # Never set anything; clear should succeed anyway
    result = clear_voice_override(project_dir, "seg_001")
    assert result["cleared"] is True


def test_set_voice_override_rejects_empty(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    with pytest.raises(ValueError, match="provider must be non-empty"):
        set_voice_override(project_dir, "seg_001", provider="", voice_id="v")
    with pytest.raises(ValueError, match="voice_id must be non-empty"):
        set_voice_override(project_dir, "seg_001", provider="p", voice_id="")


def test_set_voice_override_rejects_bad_sid(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    with pytest.raises(ValueError, match="invalid segment_id"):
        set_voice_override(project_dir, "../hack", provider="p", voice_id="v")


def test_voice_map_skips_malformed_entries(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    path = project_dir / VOICE_MAP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "seg_001": {"provider": "ok", "voice_id": "v1"},
        "seg_002": {"provider": "", "voice_id": "v2"},   # empty provider → drop
        "seg_003": "not a dict",                          # wrong type → drop
        "seg_004": {"provider": "ok"},                    # missing voice_id → drop
    }), encoding="utf-8")
    vm = load_voice_map(project_dir)
    assert set(vm.keys()) == {"seg_001"}


def test_voice_map_entry_fields_contract() -> None:
    assert VOICE_MAP_ENTRY_FIELDS == {"provider", "voice_id"}


def test_set_voice_override_missing_editing_dir(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    import shutil
    shutil.rmtree(project_dir / EDITING_SUBDIR)
    with pytest.raises(EditingConflictError, match="editing dir does not exist"):
        set_voice_override(project_dir, "seg_001", provider="p", voice_id="v")


# ---------------------------------------------------------------------------
# editing_batch
# ---------------------------------------------------------------------------


def test_batch_regenerate_empty_segment_status_is_noop(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    caller = _fake_tts_caller()
    result = regenerate_all_dirty_segments(project_dir, tts_caller=caller)
    assert result == {
        "total": 0,
        "succeeded_count": 0,
        "failed_count": 0,
        "succeeded_segment_ids": [],
        "failed_segment_ids": [],
        "failures": [],
    }
    assert caller.calls == []


def test_batch_regenerates_only_trigger_statuses(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path, n_segments=5)
    # Flag various statuses: text_dirty / voice_dirty / tts_failed → regenerate
    #                        tts_loading / tts_dirty               → skip
    mark_segment_status(project_dir, "seg_001", SEGMENT_STATUS_TEXT_DIRTY)
    mark_segment_status(project_dir, "seg_002", SEGMENT_STATUS_VOICE_DIRTY)
    mark_segment_status(project_dir, "seg_003", SEGMENT_STATUS_TTS_FAILED)
    mark_segment_status(project_dir, "seg_004", SEGMENT_STATUS_TTS_DIRTY)  # skip
    mark_segment_status(project_dir, "seg_005", "tts_loading")              # skip

    caller = _fake_tts_caller()
    result = regenerate_all_dirty_segments(project_dir, tts_caller=caller)
    assert result["total"] == 3
    assert result["succeeded_count"] == 3
    assert sorted(caller.calls) == ["seg_001", "seg_002", "seg_003"]


def test_batch_partial_failure_response_shape_matches_d38(tmp_path: Path) -> None:
    """Plan D38 response: succeeded_count / failed_count / failed_segment_ids."""
    _, project_dir = _build_editing_job(tmp_path, n_segments=3)
    for sid in ("seg_001", "seg_002", "seg_003"):
        mark_segment_status(project_dir, sid, SEGMENT_STATUS_TEXT_DIRTY)

    def mostly_ok_caller(segment, output_path):
        if segment["segment_id"] == "seg_002":
            raise RuntimeError("upstream 429")
        output_path.write_bytes(b"ok")

    result = regenerate_all_dirty_segments(project_dir, tts_caller=mostly_ok_caller)
    assert result["total"] == 3
    assert result["succeeded_count"] == 2
    assert result["failed_count"] == 1
    assert result["failed_segment_ids"] == ["seg_002"]
    assert result["failures"][0]["error"] == "upstream 429"


def test_batch_respects_not_wired_caller(tmp_path: Path) -> None:
    """Without a caller, every segment fails with TtsNotWiredError but the
    batch does NOT abort — each segment is recorded as failed."""
    _, project_dir = _build_editing_job(tmp_path, n_segments=2)
    for sid in ("seg_001", "seg_002"):
        mark_segment_status(project_dir, sid, SEGMENT_STATUS_TEXT_DIRTY)

    result = regenerate_all_dirty_segments(project_dir)  # default caller → not wired
    assert result["failed_count"] == 2
    assert result["succeeded_count"] == 0
    assert set(result["failed_segment_ids"]) == {"seg_001", "seg_002"}


def test_batch_continues_past_failures_sequentially(tmp_path: Path) -> None:
    """The 3rd segment must still be processed even if the 1st fails."""
    _, project_dir = _build_editing_job(tmp_path, n_segments=3)
    for sid in ("seg_001", "seg_002", "seg_003"):
        mark_segment_status(project_dir, sid, SEGMENT_STATUS_TEXT_DIRTY)

    ordering_log: list[str] = []

    def caller_with_first_failing(segment, output_path):
        ordering_log.append(segment["segment_id"])
        if segment["segment_id"] == "seg_001":
            raise RuntimeError("boom")
        output_path.write_bytes(b"ok")

    result = regenerate_all_dirty_segments(
        project_dir, tts_caller=caller_with_first_failing
    )
    # All three were attempted
    assert sorted(ordering_log) == ["seg_001", "seg_002", "seg_003"]
    assert result["succeeded_count"] == 2
    assert result["failed_segment_ids"] == ["seg_001"]


def test_batch_trigger_statuses_contract() -> None:
    """Plan: text/voice/tts_failed trigger regenerate; tts_dirty / loading /
    accepted do not."""
    assert SEGMENT_STATUS_TEXT_DIRTY in BATCH_REGENERATE_TRIGGER_STATUSES
    assert SEGMENT_STATUS_VOICE_DIRTY in BATCH_REGENERATE_TRIGGER_STATUSES
    assert SEGMENT_STATUS_TTS_FAILED in BATCH_REGENERATE_TRIGGER_STATUSES
    assert SEGMENT_STATUS_TTS_DIRTY not in BATCH_REGENERATE_TRIGGER_STATUSES
    assert SEGMENT_STATUS_ACCEPTED not in BATCH_REGENERATE_TRIGGER_STATUSES


# ---------------------------------------------------------------------------
# JobService delegates for T1-6
# ---------------------------------------------------------------------------


def test_service_regenerate_all_dirty_touches_editing_touched_at(tmp_path: Path) -> None:
    import time

    service, project_dir = _build_editing_job(tmp_path, n_segments=2)
    mark_segment_status(project_dir, "seg_001", SEGMENT_STATUS_TEXT_DIRTY)
    before = service.require_job("job_batch").editing_touched_at
    time.sleep(0.005)
    caller = _fake_tts_caller()
    service.regenerate_all_dirty_segments("job_batch", tts_caller=caller)
    after = service.require_job("job_batch").editing_touched_at
    assert after > before


def test_service_voice_map_get_set_clear(tmp_path: Path) -> None:
    service, _ = _build_editing_job(tmp_path, n_segments=2)
    assert service.get_editing_voice_map("job_batch")["voice_map"] == {}

    service.set_editing_voice_override(
        "job_batch", "seg_001",
        provider="minimax", voice_id="male_1",
    )
    vm = service.get_editing_voice_map("job_batch")["voice_map"]
    assert vm == {"seg_001": {"provider": "minimax", "voice_id": "male_1"}}

    service.clear_editing_voice_override("job_batch", "seg_001")
    assert service.get_editing_voice_map("job_batch")["voice_map"] == {}


def test_service_voice_map_rejects_non_editing(tmp_path: Path) -> None:
    from dataclasses import replace

    service, _ = _build_editing_job(tmp_path)
    record = service.require_job("job_batch")
    service.store.save_job(replace(record, status=JOB_STATUS_SUCCEEDED))

    with pytest.raises(EditingConflictError, match="not in editing state"):
        service.set_editing_voice_override(
            "job_batch", "seg_001", provider="p", voice_id="v",
        )


def test_service_regenerate_all_rejects_non_editing(tmp_path: Path) -> None:
    from dataclasses import replace

    service, _ = _build_editing_job(tmp_path)
    record = service.require_job("job_batch")
    service.store.save_job(replace(record, status=JOB_STATUS_SUCCEEDED))

    with pytest.raises(EditingConflictError, match="not in editing state"):
        service.regenerate_all_dirty_segments("job_batch")
