"""T1-9 — editing/commit full flow.

Covers both strategies:
- overwrite: edits promoted to baseline in place; status → running; runner
  started with continue_existing=True / start_stage='alignment'
- copy_as_new: two-phase per D34 (Phase A prepare + Phase B source reset);
  A-failure rolls back without touching source; B-failure is best-effort.

Runner is faked so no real pipeline spawns.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.jobs.editing import EDITING_SUBDIR, enter_editing
from services.jobs.editing_commit import (
    CommitPipelineError,
    commit_editing_pipeline,
)
from services.jobs.editing import EditingConflictError
from services.jobs.models import (
    JOB_STATUS_EDITING,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    STAGE_ALIGNMENT,
    JobRecord,
)
from services.jobs.store import JobStore


# ---------------------------------------------------------------------------
# Fake runner
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """Minimal fake matching ``CommitRunner`` protocol. Records every
    start() call so tests can assert the hand-off shape."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.raise_next: Exception | None = None

    def start(self, record: JobRecord, continue_existing: bool = False) -> None:
        if self.raise_next is not None:
            exc = self.raise_next
            self.raise_next = None
            raise exc
        self.calls.append({
            "job_id": record.job_id,
            "status": record.status,
            "current_stage": record.current_stage,
            "continue_existing": continue_existing,
            "edit_generation": record.edit_generation,
        })


# ---------------------------------------------------------------------------
# Fixtures — editing-state job with baseline audio + editing diff populated
# ---------------------------------------------------------------------------


def _build_editing_job_with_diff(
    tmp_path: Path,
    *,
    text_edits: dict[str, str] | None = None,
    voice_map: dict[str, dict] | None = None,
    draft_wavs: dict[str, bytes] | None = None,
) -> tuple[JobStore, JobRecord, Path]:
    project_dir = tmp_path / "projects" / "job_commit"
    editor = project_dir / "editor"
    (editor / "tts_segments").mkdir(parents=True)
    for i in range(1, 4):
        (editor / "tts_segments" / f"seg_{i:03d}.wav").write_bytes(f"BASE_{i}".encode())
    (editor / "segments.json").write_text(
        json.dumps([
            {"segment_id": f"seg_{i:03d}", "cn_text": f"text{i}",
             "provider": "minimax", "voice_id": "v_default",
             "start_ms": (i - 1) * 1000, "end_ms": i * 1000}
            for i in range(1, 4)
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    (editor / "transcript.json").write_text("{}", encoding="utf-8")
    (editor / "manifest.json").write_text("{}", encoding="utf-8")

    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_commit",
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
    # Populate the editing/ dir with the requested edits
    editing = editor / "editing"
    if text_edits:
        segs = json.loads((editing / "segments.json").read_text(encoding="utf-8"))
        for seg in segs:
            if seg["segment_id"] in text_edits:
                seg["cn_text"] = text_edits[seg["segment_id"]]
        (editing / "segments.json").write_text(
            json.dumps(segs, ensure_ascii=False), encoding="utf-8"
        )
    if voice_map is not None:
        (editing / "voice_map.json").write_text(
            json.dumps(voice_map, ensure_ascii=False), encoding="utf-8"
        )
    if draft_wavs:
        drafts_dir = editing / "tts_segments_draft"
        drafts_dir.mkdir(exist_ok=True)
        for sid, payload in draft_wavs.items():
            (drafts_dir / f"{sid}.wav").write_bytes(payload)
    return store, editing_record, project_dir


# ---------------------------------------------------------------------------
# overwrite — happy path
# ---------------------------------------------------------------------------


def _add_succeeded_project_state(project_dir: Path) -> None:
    """Simulate the project_state.json left behind by a succeeded pipeline:
    every stage DONE. Used to verify overwrite commit prunes alignment +
    publish back to PENDING so re-run actually happens."""
    (project_dir / "project_state.json").write_text(
        json.dumps({
            "project_id": project_dir.name,
            "stages": {
                "ingestion":           {"status": "done", "payload": {"ok": 1}},
                "audio_preparation":   {"status": "done", "payload": {"ok": 1}},
                "media_understanding": {"status": "done", "payload": {"ok": 1}},
                "translation_review":  {"status": "done", "payload": {"ok": 1}},
                "translation":         {"status": "done", "payload": {"ok": 1}},
                "alignment":           {"status": "done", "payload": {"ok": 1}},
                "legacy_process_output": {"status": "done", "payload": {"ok": 1}},
            },
        }),
        encoding="utf-8",
    )


def test_overwrite_prunes_alignment_stages_in_project_state(tmp_path: Path) -> None:
    """After overwrite commit, project_state.json must reset alignment +
    legacy_process_output to PENDING so pipeline runs them. Without this,
    the source's ALL-DONE state persists and pipeline would conclude
    'publish already done, nothing to do' on the new segments / drafts."""
    store, record, project_dir = _build_editing_job_with_diff(
        tmp_path, text_edits={"seg_001": "EDITED_1"}
    )
    _add_succeeded_project_state(project_dir)
    runner = _RecordingRunner()

    commit_editing_pipeline(record, store, runner=runner, strategy="overwrite")

    state = json.loads((project_dir / "project_state.json").read_text(encoding="utf-8"))
    stages = state["stages"]
    # Upstream preserved
    for preserved in (
        "ingestion", "audio_preparation", "media_understanding",
        "translation_review", "translation",
    ):
        assert stages[preserved]["status"] == "done"
    # Alignment + publish pruned
    assert stages["alignment"]["status"] == "pending"
    assert stages["legacy_process_output"]["status"] == "pending"


def test_overwrite_applies_text_edit_to_baseline(tmp_path: Path) -> None:
    store, record, project_dir = _build_editing_job_with_diff(
        tmp_path, text_edits={"seg_002": "NEW_TEXT_2"}
    )
    runner = _RecordingRunner()

    result = commit_editing_pipeline(
        record, store, runner, strategy="overwrite",
    )

    assert result["strategy"] == "overwrite"
    assert result["job_id"] == "job_commit"
    assert result["edit_generation"] == 1
    # baseline segments.json updated
    out = json.loads((project_dir / "editor" / "segments.json").read_text(encoding="utf-8"))
    assert out[1]["cn_text"] == "NEW_TEXT_2"
    # editing/ dir removed
    assert not (project_dir / EDITING_SUBDIR).exists()


def test_overwrite_applies_voice_map_and_draft_wavs(tmp_path: Path) -> None:
    store, record, project_dir = _build_editing_job_with_diff(
        tmp_path,
        voice_map={"seg_001": {"provider": "cosyvoice", "voice_id": "cv"}},
        draft_wavs={"seg_002": b"NEW_002"},
    )
    runner = _RecordingRunner()

    commit_editing_pipeline(record, store, runner, strategy="overwrite")

    out = json.loads((project_dir / "editor" / "segments.json").read_text(encoding="utf-8"))
    assert out[0]["provider"] == "cosyvoice"
    assert out[0]["voice_id"] == "cv"
    assert (project_dir / "editor" / "tts_segments" / "seg_002.wav").read_bytes() == b"NEW_002"
    # seg_001 / seg_003 baselines untouched
    assert (project_dir / "editor" / "tts_segments" / "seg_001.wav").read_bytes() == b"BASE_1"
    assert (project_dir / "editor" / "tts_segments" / "seg_003.wav").read_bytes() == b"BASE_3"


def test_overwrite_flips_status_and_increments_edit_generation(tmp_path: Path) -> None:
    store, record, _ = _build_editing_job_with_diff(tmp_path)
    runner = _RecordingRunner()

    commit_editing_pipeline(record, store, runner, strategy="overwrite")

    final = store.require_job("job_commit")
    assert final.status == JOB_STATUS_RUNNING
    assert final.current_stage == STAGE_ALIGNMENT
    assert final.edit_generation == 1
    assert final.editing_touched_at is None


def test_overwrite_submits_runner_with_continue_existing(tmp_path: Path) -> None:
    store, record, _ = _build_editing_job_with_diff(tmp_path)
    runner = _RecordingRunner()

    commit_editing_pipeline(record, store, runner, strategy="overwrite")

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["job_id"] == "job_commit"
    assert call["status"] == JOB_STATUS_RUNNING
    assert call["current_stage"] == STAGE_ALIGNMENT
    assert call["continue_existing"] is True
    assert call["edit_generation"] == 1


def test_overwrite_second_commit_bumps_edit_generation_again(tmp_path: Path) -> None:
    store, record, project_dir = _build_editing_job_with_diff(tmp_path)
    runner = _RecordingRunner()

    commit_editing_pipeline(record, store, runner, strategy="overwrite")
    # Simulate pipeline finished + user re-enters editing
    finished = replace(
        store.require_job("job_commit"),
        status=JOB_STATUS_SUCCEEDED,
        current_stage="completed",
    )
    store.save_job(finished)
    editing_again = enter_editing(finished, store)

    commit_editing_pipeline(editing_again, store, runner, strategy="overwrite")

    final = store.require_job("job_commit")
    assert final.edit_generation == 2


# ---------------------------------------------------------------------------
# overwrite — runner failure rolls back to editing
# ---------------------------------------------------------------------------


def test_overwrite_runner_failure_rolls_back_to_editing(tmp_path: Path) -> None:
    store, record, project_dir = _build_editing_job_with_diff(tmp_path)
    runner = _RecordingRunner()
    runner.raise_next = RuntimeError("runner queue full")

    with pytest.raises(CommitPipelineError, match="runner failed to accept"):
        commit_editing_pipeline(record, store, runner, strategy="overwrite")

    # Status reverts to editing so user can retry. Note: the edits HAVE been
    # applied to baseline (commit wrote them before submitting the runner);
    # this is documented behaviour — the user's work is not lost, but the
    # pipeline has not run.
    final = store.require_job("job_commit")
    assert final.status == JOB_STATUS_EDITING
    assert final.editing_touched_at is not None


# ---------------------------------------------------------------------------
# copy_as_new — happy path
# ---------------------------------------------------------------------------


def test_copy_as_new_creates_new_job_with_hardlinked_audio(tmp_path: Path) -> None:
    store, record, project_dir = _build_editing_job_with_diff(
        tmp_path, text_edits={"seg_001": "copy_edit"}
    )
    runner = _RecordingRunner()

    result = commit_editing_pipeline(
        record, store, runner,
        strategy="copy_as_new",
        copy_display_name="A · 副本 1",
        new_job_id_factory=lambda: "job_copy_1",
    )

    assert result["strategy"] == "copy_as_new"
    assert result["source_job_id"] == "job_commit"
    assert result["new_job_id"] == "job_copy_1"
    assert result["new_display_name"] == "A · 副本 1"
    # New JobRecord exists
    new_record = store.require_job("job_copy_1")
    assert new_record.status == JOB_STATUS_QUEUED
    assert new_record.current_stage == STAGE_ALIGNMENT
    assert new_record.copy_of_job_id == "job_commit"
    assert new_record.root_job_id == "job_commit"
    assert new_record.display_name == "A · 副本 1"
    assert new_record.edit_generation == 0
    # New project_dir exists with merged segments.json
    new_project_dir = Path(result["new_project_dir"])
    assert new_project_dir.is_dir()
    out = json.loads((new_project_dir / "editor" / "segments.json").read_text(encoding="utf-8"))
    assert out[0]["cn_text"] == "copy_edit"
    # Hardlinked baseline audio (inode shared)
    src_wav = project_dir / "editor" / "tts_segments" / "seg_001.wav"
    new_wav = new_project_dir / "editor" / "tts_segments" / "seg_001.wav"
    assert src_wav.stat().st_ino == new_wav.stat().st_ino


def test_copy_as_new_new_record_fields_do_not_inherit_source_paths(
    tmp_path: Path,
) -> None:
    """Regression for the 2026-04-19 ``副本修改=源被污染`` incident.

    ``dataclasses.replace(record, ...)`` silently inherits any field the
    caller does not explicitly list. The original ``_commit_copy_as_new``
    listed ``project_dir`` but forgot ``workspace_dir`` and
    ``manifest_path``, so new_record carried SOURCE values for those two.

    Downstream effect: after γ completed and ``_finalize_process`` ran,
    _resolve_job_project_dir's priority-2 fallback (workspace_dir)
    pointed at source. Combined with ProcessJobRunner._record_line
    parsing stdout for project_dir mentions, the in-memory JobRecord
    drifted to source. A subsequent enter_editing + commit overwrite on
    the copy then wrote the user's edits into source instead of the
    copy — destroying source state with no warning.

    The fix is to explicitly null/rewrite workspace_dir and manifest_path
    in new_record so no source-pointing identity fields leak.
    """
    store, record, project_dir = _build_editing_job_with_diff(
        tmp_path, text_edits={"seg_001": "x"},
    )
    # Simulate the production shape: source's JobRecord carries
    # workspace_dir and manifest_path pointing at source's project_dir.
    from dataclasses import replace as _replace
    record = _replace(
        record,
        workspace_dir=f"projects/test_user/{record.job_id}",
        manifest_path=str(project_dir / "manifest.json"),
    )
    store.save_job(record)
    runner = _RecordingRunner()

    result = commit_editing_pipeline(
        record, store, runner,
        strategy="copy_as_new",
        copy_display_name="A \u00b7 copy",
        new_job_id_factory=lambda: "job_copy_identity",
    )

    new_record = store.require_job(result["new_job_id"])

    # Identity fields must point at the new job, not the source.
    assert new_record.job_id == "job_copy_identity"
    assert record.job_id not in (new_record.workspace_dir or ""), (
        f"new_record.workspace_dir still contains source job_id: "
        f"{new_record.workspace_dir!r}"
    )
    assert "job_copy_identity" in (new_record.workspace_dir or ""), (
        f"new_record.workspace_dir does not reference new job_id: "
        f"{new_record.workspace_dir!r}"
    )
    # manifest_path must be cleared (or rewritten). Source path MUST NOT
    # leak — downstream _resolve_manifest_path will regenerate during γ.
    assert new_record.manifest_path is None or record.job_id not in new_record.manifest_path, (
        f"new_record.manifest_path leaks source path: "
        f"{new_record.manifest_path!r}"
    )
    # project_dir is positive-asserted elsewhere; guard it here too for
    # defense-in-depth.
    assert new_record.project_dir == result["new_project_dir"]


def test_copy_as_new_resets_source_to_succeeded(tmp_path: Path) -> None:
    """Phase B: source editing/ dropped + source status → succeeded."""
    store, record, project_dir = _build_editing_job_with_diff(tmp_path)
    runner = _RecordingRunner()

    commit_editing_pipeline(
        record, store, runner,
        strategy="copy_as_new", copy_display_name="A · 副本",
        new_job_id_factory=lambda: "job_copy_1",
    )

    source_final = store.require_job("job_commit")
    assert source_final.status == JOB_STATUS_SUCCEEDED
    assert source_final.editing_touched_at is None
    assert not (project_dir / EDITING_SUBDIR).exists()


def test_copy_as_new_submits_runner_for_new_job_only(tmp_path: Path) -> None:
    store, record, _ = _build_editing_job_with_diff(tmp_path)
    runner = _RecordingRunner()

    commit_editing_pipeline(
        record, store, runner,
        strategy="copy_as_new", copy_display_name="X",
        new_job_id_factory=lambda: "job_copy_1",
    )

    assert len(runner.calls) == 1
    assert runner.calls[0]["job_id"] == "job_copy_1"
    assert runner.calls[0]["status"] == JOB_STATUS_QUEUED
    assert runner.calls[0]["current_stage"] == STAGE_ALIGNMENT


def test_copy_as_new_rejects_empty_display_name(tmp_path: Path) -> None:
    store, record, _ = _build_editing_job_with_diff(tmp_path)
    runner = _RecordingRunner()

    with pytest.raises(EditingConflictError, match="requires a non-empty copy_display_name"):
        commit_editing_pipeline(
            record, store, runner,
            strategy="copy_as_new",
            copy_display_name="   ",
        )


# ---------------------------------------------------------------------------
# copy_as_new — Phase A rollback preserves source
# ---------------------------------------------------------------------------


def test_copy_as_new_runner_failure_rollback_preserves_source(tmp_path: Path) -> None:
    """Core D34 invariant: A5 failure rolls back new project_dir + new
    Job record; source editing/ + source status stay untouched."""
    store, record, project_dir = _build_editing_job_with_diff(
        tmp_path,
        text_edits={"seg_001": "edit_before_failure"},
        draft_wavs={"seg_002": b"draft_before_failure"},
    )
    source_hashes_before = _hash_tree(project_dir)
    runner = _RecordingRunner()
    runner.raise_next = RuntimeError("runner denied")

    with pytest.raises(CommitPipelineError, match="runner.submit failed"):
        commit_editing_pipeline(
            record, store, runner,
            strategy="copy_as_new", copy_display_name="X",
            new_job_id_factory=lambda: "job_copy_1",
        )

    # New project_dir cleaned up
    new_project_dir = project_dir.parent / "job_copy_1"
    assert not new_project_dir.exists()
    # New Job record was cleaned up
    assert store.load_job("job_copy_1") is None
    # Source editing/ + source JobRecord untouched
    source_hashes_after = _hash_tree(project_dir)
    assert source_hashes_before == source_hashes_after
    source = store.require_job("job_commit")
    assert source.status == JOB_STATUS_EDITING
    assert source.editing_touched_at is not None


def _hash_tree(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_commit_rejects_non_editing_status(tmp_path: Path) -> None:
    store, record, _ = _build_editing_job_with_diff(tmp_path)
    non_editing = replace(record, status=JOB_STATUS_SUCCEEDED)
    store.save_job(non_editing)
    runner = _RecordingRunner()
    with pytest.raises(EditingConflictError, match="not in editing state"):
        commit_editing_pipeline(non_editing, store, runner, strategy="overwrite")


def test_commit_rejects_unknown_strategy(tmp_path: Path) -> None:
    store, record, _ = _build_editing_job_with_diff(tmp_path)
    runner = _RecordingRunner()
    with pytest.raises(ValueError, match="unsupported commit strategy"):
        commit_editing_pipeline(record, store, runner, strategy="force_push")


def test_commit_rejects_missing_project_dir(tmp_path: Path) -> None:
    store, record, _ = _build_editing_job_with_diff(tmp_path)
    no_pd = replace(record, project_dir=None)
    store.save_job(no_pd)
    runner = _RecordingRunner()
    with pytest.raises(EditingConflictError, match="has no project_dir"):
        commit_editing_pipeline(no_pd, store, runner, strategy="overwrite")


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


def test_overwrite_emits_commit_started(tmp_path: Path) -> None:
    store, record, _ = _build_editing_job_with_diff(tmp_path)
    runner = _RecordingRunner()
    commit_editing_pipeline(record, store, runner, strategy="overwrite")

    events = store.load_events("job_commit")
    messages = [e.message for e in events if e.message]
    assert any("editing.commit_started: strategy=overwrite" in m for m in messages)


def test_copy_as_new_emits_started_and_succeeded(tmp_path: Path) -> None:
    store, record, _ = _build_editing_job_with_diff(tmp_path)
    runner = _RecordingRunner()

    commit_editing_pipeline(
        record, store, runner,
        strategy="copy_as_new", copy_display_name="X",
        new_job_id_factory=lambda: "job_copy_1",
    )

    events = store.load_events("job_commit")
    messages = [e.message for e in events if e.message]
    assert any("editing.commit_started: strategy=copy_as_new" in m for m in messages)
    assert any("editing.commit_succeeded: strategy=copy_as_new" in m for m in messages)
