"""T1-2 — editing segments CRUD tests.

Covers:
- input_validators.validate_segment_id (allowlist regex)
- editing_segments.load_editing_segments / load_segment_status / editing_payload
- editing_segments.patch_editing_segment (cn_text + translation_confirmed +
  rewrite_requested; silently drops non-patchable keys; auto-flags text_dirty)
- editing_segments.mark_segment_status (setting + clearing for accepted)
- JobService delegates (get_editing_segments / patch_editing_segment /
  mark_editing_segment_status) with touched_at refresh + editing-state check
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.jobs.editing import (
    EDITING_SUBDIR,
    EditingConflictError,
    enter_editing,
)
from services.jobs.editing_segments import (
    PATCHABLE_SEGMENT_FIELDS,
    SEGMENT_STATUS_ACCEPTED,
    SEGMENT_STATUS_TEXT_DIRTY,
    SEGMENT_STATUS_TTS_DIRTY,
    SUPPORTED_SEGMENT_STATUSES,
    editing_payload,
    load_editing_segments,
    load_segment_status,
    mark_segment_status,
    patch_editing_segment,
)
from services.jobs.input_validators import (
    SEGMENT_ID_RE,
    validate_commit_strategy,
    validate_segment_id,
)
from services.jobs.models import JOB_STATUS_EDITING, JOB_STATUS_SUCCEEDED, JobRecord
from services.jobs.service import JobService
from services.jobs.store import JobStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _NullRunner:
    """JobService.__init__ only stores the runner; editing delegates never
    touch its attributes, so a bare class is enough."""


def _build_editing_job(tmp_path: Path) -> tuple[JobService, Path, JobRecord]:
    """Returns (service, project_dir, editing_record). The baseline
    segments.json has 3 segments we can patch against."""
    project_dir = tmp_path / "projects" / "job_abc"
    (project_dir / "editor").mkdir(parents=True)
    baseline_segments = [
        {
            "segment_id": "seg_001",
            "speaker_id": "A",
            "cn_text": "你好",
            "source_text": "hello",
            "start_ms": 0,
            "end_ms": 1000,
        },
        {
            "segment_id": "seg_002",
            "speaker_id": "B",
            "cn_text": "世界",
            "source_text": "world",
            "start_ms": 1000,
            "end_ms": 2000,
        },
        {
            "segment_id": "seg_003",
            "speaker_id": "A",
            "cn_text": "再见",
            "source_text": "goodbye",
            "start_ms": 2000,
            "end_ms": 3000,
        },
    ]
    (project_dir / "editor" / "segments.json").write_text(
        json.dumps(baseline_segments, ensure_ascii=False), encoding="utf-8"
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_abc",
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://example.com/video",
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
    editing_record = enter_editing(record, store)
    service = JobService(store=store, runner=_NullRunner())
    return service, project_dir, editing_record


# ---------------------------------------------------------------------------
# validate_segment_id / SEGMENT_ID_RE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ok",
    ["seg_001", "s1", "abc", "1", "seg_" + "a" * 60, "speaker_a_042"],
)
def test_validate_segment_id_accepts_allowlist(ok: str) -> None:
    assert validate_segment_id(ok) == ok


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "Seg_001",        # uppercase
        "seg-001",        # hyphen
        "seg.001",        # dot
        "seg/001",        # slash
        "seg\\001",       # backslash
        "../etc/passwd",  # traversal
        "seg_" + "a" * 61,  # too long (65 chars)
        "seg_001 ",       # trailing space
        " seg_001",       # leading space
    ],
)
def test_validate_segment_id_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid segment_id"):
        validate_segment_id(bad)


def test_validate_segment_id_rejects_non_string() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        validate_segment_id(42)  # type: ignore[arg-type]


def test_segment_id_regex_is_anchored() -> None:
    """Confirm the regex does not let ``seg_001/../x`` slip through as a
    substring match."""
    assert SEGMENT_ID_RE.match("seg_001/../x") is None


# ---------------------------------------------------------------------------
# validate_commit_strategy
# ---------------------------------------------------------------------------


def test_validate_commit_strategy_allows_overwrite_and_copy() -> None:
    assert validate_commit_strategy("overwrite") == "overwrite"
    assert validate_commit_strategy("copy_as_new") == "copy_as_new"


def test_validate_commit_strategy_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unsupported commit strategy"):
        validate_commit_strategy("force_push")


# ---------------------------------------------------------------------------
# editing_segments.load_* / editing_payload
# ---------------------------------------------------------------------------


def test_load_editing_segments_returns_list(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    segments = load_editing_segments(project_dir)
    assert len(segments) == 3
    assert segments[0]["segment_id"] == "seg_001"
    assert segments[0]["cn_text"] == "你好"


def test_load_segment_status_missing_returns_empty(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    # enter_editing doesn't pre-create segment_status.json
    assert load_segment_status(project_dir) == {}


def test_editing_payload_bundle(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    payload = editing_payload(project_dir)
    assert payload["total"] == 3
    assert len(payload["segments"]) == 3
    assert payload["segment_status"] == {}


# ---------------------------------------------------------------------------
# patch_editing_segment
# ---------------------------------------------------------------------------


def test_patch_segment_updates_cn_text_and_flags_dirty(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)

    updated = patch_editing_segment(
        project_dir, "seg_001", {"cn_text": "你好呀"}
    )

    assert updated["cn_text"] == "你好呀"
    # Reload from disk to confirm persistence
    segments = load_editing_segments(project_dir)
    assert segments[0]["cn_text"] == "你好呀"
    assert segments[1]["cn_text"] == "世界"  # untouched
    # Status auto-flagged
    status = load_segment_status(project_dir)
    assert status["seg_001"] == SEGMENT_STATUS_TEXT_DIRTY


def test_patch_segment_accepts_translation_confirmed(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    updated = patch_editing_segment(
        project_dir, "seg_001", {"translation_confirmed": True}
    )
    assert updated["translation_confirmed"] is True
    # translation_confirmed alone does NOT flag text_dirty (TTS still valid)
    status = load_segment_status(project_dir)
    assert "seg_001" not in status


def test_patch_segment_silently_drops_non_patchable_fields(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    updated = patch_editing_segment(
        project_dir, "seg_001",
        {"cn_text": "new", "voice_id": "X", "segment_id": "hacker", "start_ms": 99999},
    )
    # cn_text applied; others dropped
    assert updated["cn_text"] == "new"
    assert updated["segment_id"] == "seg_001"  # NOT "hacker"
    assert updated["start_ms"] == 0            # NOT 99999
    assert "voice_id" not in updated           # voice_id goes through voice_map path


def test_patch_segment_with_only_unknown_fields_raises(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    with pytest.raises(ValueError, match="no patchable fields"):
        patch_editing_segment(project_dir, "seg_001", {"foo": "bar"})


def test_patch_segment_unknown_segment_id_raises_conflict(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    with pytest.raises(EditingConflictError, match="not found"):
        patch_editing_segment(project_dir, "seg_999", {"cn_text": "x"})


def test_patch_segment_rejects_bad_id_before_fs_access(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    with pytest.raises(ValueError, match="invalid segment_id"):
        patch_editing_segment(project_dir, "../hack", {"cn_text": "x"})


def test_patch_segment_missing_editing_dir_raises(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    # Blow away editing/ to simulate corruption
    import shutil
    shutil.rmtree(project_dir / EDITING_SUBDIR)
    with pytest.raises(EditingConflictError, match="editing dir does not exist"):
        patch_editing_segment(project_dir, "seg_001", {"cn_text": "x"})


# ---------------------------------------------------------------------------
# mark_segment_status
# ---------------------------------------------------------------------------


def test_mark_status_sets_and_reloads(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    mark_segment_status(project_dir, "seg_001", SEGMENT_STATUS_TTS_DIRTY)
    assert load_segment_status(project_dir) == {"seg_001": "tts_dirty"}


def test_mark_status_accepted_clears_entry(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    mark_segment_status(project_dir, "seg_001", SEGMENT_STATUS_TEXT_DIRTY)
    assert "seg_001" in load_segment_status(project_dir)
    mark_segment_status(project_dir, "seg_001", SEGMENT_STATUS_ACCEPTED)
    assert load_segment_status(project_dir) == {}


def test_mark_status_rejects_unknown_status(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    with pytest.raises(ValueError, match="unsupported segment status"):
        mark_segment_status(project_dir, "seg_001", "bogus_status")


def test_supported_segment_statuses_contract() -> None:
    assert "accepted" in SUPPORTED_SEGMENT_STATUSES
    assert "text_dirty" in SUPPORTED_SEGMENT_STATUSES
    assert "tts_dirty" in SUPPORTED_SEGMENT_STATUSES
    assert "voice_dirty" in SUPPORTED_SEGMENT_STATUSES
    # Implicit accepted ≡ absent from map, so "accepted" cannot be stored
    # but IS a valid input (clears entry) — covered by the dedicated test.


def test_patchable_fields_contract() -> None:
    """voice_id deliberately excluded — goes through voice_map.json in T1-6."""
    assert "cn_text" in PATCHABLE_SEGMENT_FIELDS
    assert "voice_id" not in PATCHABLE_SEGMENT_FIELDS


# ---------------------------------------------------------------------------
# JobService delegates
# ---------------------------------------------------------------------------


def test_service_get_editing_segments_bundles_metadata(tmp_path: Path) -> None:
    service, _, editing_record = _build_editing_job(tmp_path)

    payload = service.get_editing_segments("job_abc")

    assert payload["total"] == 3
    assert payload["editing_touched_at"] == editing_record.editing_touched_at
    assert payload["edit_generation"] == 0
    assert payload["segment_status"] == {}


def test_service_get_editing_segments_rejects_non_editing(tmp_path: Path) -> None:
    service, _, _ = _build_editing_job(tmp_path)
    # Force back to succeeded without going through cancel
    record = service.require_job("job_abc")
    from dataclasses import replace as _replace
    service.store.save_job(_replace(record, status=JOB_STATUS_SUCCEEDED))

    with pytest.raises(EditingConflictError, match="not in editing state"):
        service.get_editing_segments("job_abc")


def test_service_patch_refreshes_editing_touched_at(tmp_path: Path) -> None:
    service, _, editing_record = _build_editing_job(tmp_path)
    original_touched = editing_record.editing_touched_at
    assert original_touched is not None

    time.sleep(0.005)
    service.patch_editing_segment("job_abc", "seg_001", {"cn_text": "hi"})

    after = service.require_job("job_abc")
    assert after.editing_touched_at is not None
    assert after.editing_touched_at > original_touched


def test_service_patch_returns_updated_segment_and_status(tmp_path: Path) -> None:
    service, _, _ = _build_editing_job(tmp_path)
    result = service.patch_editing_segment("job_abc", "seg_002", {"cn_text": "新世界"})
    assert result["segment"]["cn_text"] == "新世界"
    assert result["segment_status"] == {"seg_002": "text_dirty"}


def test_service_patch_rejects_bad_segment_id(tmp_path: Path) -> None:
    service, _, _ = _build_editing_job(tmp_path)
    with pytest.raises(ValueError, match="invalid segment_id"):
        service.patch_editing_segment("job_abc", "../hack", {"cn_text": "x"})


def test_service_mark_status_refreshes_touched_at(tmp_path: Path) -> None:
    service, _, editing_record = _build_editing_job(tmp_path)
    original = editing_record.editing_touched_at

    time.sleep(0.005)
    result = service.mark_editing_segment_status("job_abc", "seg_001", "tts_dirty")

    after = service.require_job("job_abc")
    assert after.editing_touched_at > original
    assert result["segment_status"]["seg_001"] == "tts_dirty"


def test_service_mark_status_accept_removes_entry(tmp_path: Path) -> None:
    service, _, _ = _build_editing_job(tmp_path)
    service.mark_editing_segment_status("job_abc", "seg_001", "tts_dirty")
    result = service.mark_editing_segment_status("job_abc", "seg_001", "accepted")
    assert result["segment_status"] == {}


def test_service_patch_non_editing_job_rejected(tmp_path: Path) -> None:
    service, _, _ = _build_editing_job(tmp_path)
    from dataclasses import replace as _replace
    record = service.require_job("job_abc")
    service.store.save_job(_replace(record, status=JOB_STATUS_SUCCEEDED))
    with pytest.raises(EditingConflictError, match="not in editing state"):
        service.patch_editing_segment("job_abc", "seg_001", {"cn_text": "x"})


# ---------------------------------------------------------------------------
# Atomic write correctness: segments.json must not be partial after crash
# ---------------------------------------------------------------------------


def test_atomic_write_replaces_segments_cleanly(tmp_path: Path) -> None:
    """After patch_editing_segment, there should be no .tmp leftover file
    and segments.json should be a valid JSON list."""
    _, project_dir, _ = _build_editing_job(tmp_path)

    patch_editing_segment(project_dir, "seg_001", {"cn_text": "x"})

    editing_dir = project_dir / EDITING_SUBDIR
    tmp_files = list(editing_dir.glob("*.tmp"))
    assert tmp_files == [], f"stray temp files: {tmp_files}"
    data = json.loads((editing_dir / "segments.json").read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 3
