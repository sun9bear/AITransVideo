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
    EditingAudioSyncRequiredError,
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


def test_commit_rejects_text_dirty_segment_without_matching_tts(
    tmp_path: Path,
) -> None:
    store, record, project_dir = _build_editing_job_with_diff(
        tmp_path,
        text_edits={"seg_001": "NEW_TEXT_1"},
    )
    editing_dir = project_dir / EDITING_SUBDIR
    (editing_dir / "segment_status.json").write_text(
        json.dumps({"seg_001": "text_dirty"}, ensure_ascii=False),
        encoding="utf-8",
    )
    runner = _RecordingRunner()

    with pytest.raises(EditingAudioSyncRequiredError) as raised:
        commit_editing_pipeline(record, store, runner, strategy="overwrite")

    assert runner.calls == []
    assert raised.value.payload["code"] == "editing_audio_sync_required"
    assert raised.value.unsynced_segments == [
        {
            "segment_id": "seg_001",
            "status": "text_dirty",
            "display_name": "",
            "speaker_id": "",
            "current_cn_text": "NEW_TEXT_1",
            "audio_cn_text": "text1",
            "current_source_text": "",
            "audio_source_text": "",
        }
    ]
    assert (project_dir / EDITING_SUBDIR).is_dir()


def test_commit_rejects_text_dirty_segment_even_when_stale_draft_exists(
    tmp_path: Path,
) -> None:
    store, record, project_dir = _build_editing_job_with_diff(
        tmp_path,
        text_edits={"seg_001": "text1"},
        draft_wavs={"seg_001": b"STALE_DRAFT"},
    )
    editing_dir = project_dir / EDITING_SUBDIR
    (editing_dir / "segment_status.json").write_text(
        json.dumps({"seg_001": "text_dirty"}, ensure_ascii=False),
        encoding="utf-8",
    )
    runner = _RecordingRunner()

    with pytest.raises(EditingAudioSyncRequiredError):
        commit_editing_pipeline(record, store, runner, strategy="overwrite")

    assert runner.calls == []
    assert (editing_dir / "tts_segments_draft" / "seg_001.wav").read_bytes() == b"STALE_DRAFT"


def test_overwrite_applies_voice_map_and_draft_wavs(tmp_path: Path) -> None:
    store, record, project_dir = _build_editing_job_with_diff(
        tmp_path,
        voice_map={
            "seg_001": {
                "provider": "cosyvoice",
                "voice_id": "cv",
                "tts_model_key": "cosyvoice-v3",
            }
        },
        draft_wavs={"seg_002": b"NEW_002"},
    )
    runner = _RecordingRunner()

    commit_editing_pipeline(record, store, runner, strategy="overwrite")

    out = json.loads((project_dir / "editor" / "segments.json").read_text(encoding="utf-8"))
    # CodeX P1 (ultrareview #2): voice_map override must land on the
    # canonical ``tts_provider`` field (not a drifted ``provider`` key).
    # DubbingSegment.tts_provider is what the γ loader / TTS router /
    # single-segment regen overlay all read — writing ``provider`` here
    # silently drops the user's provider pick at publish time.
    assert out[0]["tts_provider"] == "cosyvoice", (
        f"voice_map override must populate tts_provider on the segment; "
        f"got keys {sorted(out[0].keys())}"
    )
    assert out[0]["voice_id"] == "cv"
    assert out[0]["tts_model_key"] == "cosyvoice-v3"
    # Legacy misspelling must not be written — keeps editor/segments.json
    # clean of unknown fields that γ's DubbingSegment constructor then
    # has to filter out.
    assert "provider" not in out[0], (
        "legacy 'provider' key leaked into editor/segments.json — the "
        "correct field is tts_provider"
    )
    assert (project_dir / "editor" / "tts_segments" / "seg_002.wav").read_bytes() == b"NEW_002"
    # seg_001 / seg_003 baselines untouched
    assert (project_dir / "editor" / "tts_segments" / "seg_001.wav").read_bytes() == b"BASE_1"
    assert (project_dir / "editor" / "tts_segments" / "seg_003.wav").read_bytes() == b"BASE_3"


def test_overwrite_voice_map_applies_when_segment_id_is_int(
    tmp_path: Path,
) -> None:
    """CodeX P1 (ultrareview #2): legacy tasks carry int segment_ids
    in editing/segments.json (pre-normalise-seeder snapshots). The
    voice_map keys are always str (load_voice_map coerces). The current
    ``override = voice_map.get(sid) if isinstance(sid, str) else None``
    branch drops overrides for those legacy segments entirely — user's
    voice pick silently vanishes at commit.

    Fix: normalize via str(sid) before the dict lookup."""
    store, record, project_dir = _build_editing_job_with_diff(tmp_path)
    # Mutate editing/segments.json: change segment_id to int
    editing_segs_path = project_dir / EDITING_SUBDIR / "segments.json"
    segs = json.loads(editing_segs_path.read_text(encoding="utf-8"))
    for i, seg in enumerate(segs, start=1):
        seg["segment_id"] = i  # int instead of "seg_001" etc.
    editing_segs_path.write_text(
        json.dumps(segs, ensure_ascii=False), encoding="utf-8",
    )
    # voice_map keys are strings (as load_voice_map normalizes them)
    (project_dir / EDITING_SUBDIR / "voice_map.json").write_text(
        json.dumps({"1": {"provider": "volcengine", "voice_id": "v_x"}}),
        encoding="utf-8",
    )
    runner = _RecordingRunner()

    commit_editing_pipeline(record, store, runner, strategy="overwrite")

    out = json.loads((project_dir / "editor" / "segments.json").read_text(encoding="utf-8"))
    # Find the segment that should have been overridden (int-typed id)
    overridden = next(s for s in out if str(s.get("segment_id")) == "1")
    assert overridden["tts_provider"] == "volcengine", (
        f"voice_map override dropped by isinstance(sid, str) gate — "
        f"legacy int segment_id received no provider update. "
        f"Got: tts_provider={overridden.get('tts_provider')!r}"
    )
    assert overridden["voice_id"] == "v_x"


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
# overwrite — race-loss filesystem invariant (P1-15b batch 3 follow-up)
# ---------------------------------------------------------------------------


def test_overwrite_race_loss_does_not_mutate_filesystem(tmp_path: Path) -> None:
    """P1-15b batch 3 follow-up (Codex review of 0949f75):
    if a concurrent cancel commits ``editing → succeeded`` between
    JobService's stale require_job snapshot and ``_commit_overwrite``'s
    update_job claim, the commit must raise CommitPipelineError WITHOUT
    having mutated the filesystem. Otherwise the user/admin sees the
    cancel as winning while the commit's edits are already promoted
    into the baseline — silent inconsistency between status and disk.

    Race construction: monkey-patch ``store.update_job`` to commit the
    "concurrent cancel" winner just before the real call's mutator
    runs, so the mutator sees status=succeeded and raises. After the
    raise, none of the FS mutations
    (_apply_editing_to_baseline / _rm_editing_dir /
    _prune_overwrite_project_state) should have run.
    """
    from services.jobs.editing_commit import (
        _commit_overwrite,
        CommitPipelineError,
    )

    store, record, project_dir = _build_editing_job_with_diff(tmp_path)
    runner = _RecordingRunner()

    # Capture baseline filesystem state BEFORE attempting commit.
    editing_dir = project_dir / "editor" / "editing"
    baseline_segments = project_dir / "editor" / "segments.json"
    project_state = project_dir / "project_state.json"
    jianying_dir = project_dir / "jianying"

    pre_editing_dir_exists = editing_dir.exists()
    pre_editing_segments_text = (
        (editing_dir / "segments.json").read_text(encoding="utf-8")
        if (editing_dir / "segments.json").exists() else ""
    )
    pre_baseline_text = (
        baseline_segments.read_text(encoding="utf-8")
        if baseline_segments.exists() else ""
    )
    pre_project_state_text = (
        project_state.read_text(encoding="utf-8")
        if project_state.exists() else ""
    )
    # Touch a marker file inside jianying/ so we can detect a destructive
    # rmtree by checking the marker afterwards.
    jianying_dir.mkdir(parents=True, exist_ok=True)
    marker = jianying_dir / "draft_marker.txt"
    marker.write_text("pre-commit", encoding="utf-8")

    # Monkey-patch store.update_job: when commit overwrite calls the
    # claim mutator, FIRST commit a concurrent cancel to disk so the
    # mutator's load_job sees status=succeeded. The original update_job
    # then runs the mutator, which raises CommitPipelineError because
    # the status is no longer editing.
    real_update_job = store.update_job

    def fake_update_job(job_id, mutator, *, initial=None):
        # Concurrent cancel commits succeeded directly before our
        # mutator runs.
        from dataclasses import replace as _replace
        won = _replace(
            store.require_job(job_id),
            status=JOB_STATUS_SUCCEEDED,
            editing_touched_at=None,
        )
        # Restore real update_job before saving so save_job's lock is
        # honoured normally.
        store.update_job = real_update_job  # type: ignore[assignment]
        store.save_job(won)
        return real_update_job(job_id, mutator, initial=initial)

    store.update_job = fake_update_job  # type: ignore[assignment]

    with pytest.raises(CommitPipelineError, match="no longer in editing"):
        _commit_overwrite(record, store, runner, project_dir)

    # The filesystem must look exactly as it did before. None of the
    # destructive FS steps should have run.
    assert editing_dir.exists() == pre_editing_dir_exists, (
        "P1-15b batch 3 follow-up regression: editing/ dir was removed "
        "even though commit lost the concurrent claim race."
    )
    if pre_editing_dir_exists:
        assert (
            (editing_dir / "segments.json").read_text(encoding="utf-8")
            == pre_editing_segments_text
        ), "editing/segments.json was modified despite race loss"
    assert baseline_segments.read_text(encoding="utf-8") == pre_baseline_text, (
        "P1-15b batch 3 follow-up regression: editor/segments.json "
        "(baseline) was promoted from editing/ even though commit lost "
        "the concurrent claim race."
    )
    if pre_project_state_text:
        assert project_state.read_text(encoding="utf-8") == pre_project_state_text, (
            "P1-15b batch 3 follow-up regression: project_state.json was "
            "pruned despite race loss"
        )
    assert marker.exists(), (
        "P1-15b batch 3 follow-up regression: jianying/ was rmtree'd "
        "despite race loss"
    )

    # Runner was never called.
    assert runner.calls == []


def test_overwrite_cancel_during_popen_raises_commit_error_not_success(
    tmp_path: Path,
) -> None:
    """P1-15b batch 3 follow-up⁴ (Codex review of 3593416):
    when cancel lands DURING runner.start's Popen window (between
    update_job(_start_mutator) writing status=running and the new
    process being registered in _processes), runner.start now kills
    the orphan subprocess AND raises RunnerStartTerminalError. This
    test verifies that ``_commit_overwrite`` propagates that as a
    CommitPipelineError instead of returning a success dict.

    Without the raise, ``submit_job_from_existing_project_dir``
    ignores runner.start's return value and ``_commit_overwrite``
    treats normal return as success — so POST /editing/commit would
    respond 200 even though the user's cancel won and no pipeline
    actually started.
    """
    from dataclasses import replace as _replace
    from services.jobs.editing_commit import (
        _commit_overwrite,
        CommitPipelineError,
    )
    from services.jobs.models import JOB_STATUS_CANCELLED
    from services.jobs.process_runner import RunnerStartTerminalError

    store, record, project_dir = _build_editing_job_with_diff(tmp_path)

    class _CancelDuringPopenRunner:
        """Simulates: ProcessJobRunner.start raises
        RunnerStartTerminalError after killing the orphan, exactly
        as the post-Popen cleanup branch now does."""
        def __init__(self, store):
            self.store = store
            self.calls: list[dict] = []

        def start(self, record_arg, *, continue_existing=False):
            self.calls.append({"job_id": record_arg.job_id})
            # Simulate the cancel landing during Popen, then the
            # post-Popen cleanup raising.
            cancelled = _replace(
                self.store.require_job(record_arg.job_id),
                status=JOB_STATUS_CANCELLED,
                current_stage="failed",
                progress_message="Job cancelled by user.",
            )
            self.store.save_job(cancelled)
            raise RunnerStartTerminalError(
                record_arg.job_id, JOB_STATUS_CANCELLED,
            )

    runner = _CancelDuringPopenRunner(store)

    # MUST raise CommitPipelineError, NOT return a success dict.
    with pytest.raises(CommitPipelineError, match="terminal|refused"):
        _commit_overwrite(record, store, runner, project_dir)

    # Status is cancelled (the cancel handler's write is preserved;
    # commit overwrite's exception path does NOT roll status back to
    # editing because RunnerStartTerminalError is the trigger).
    final = store.require_job("job_commit")
    assert final.status == JOB_STATUS_CANCELLED, (
        f"P1-15b batch 3 follow-up⁴ regression: commit overwrite "
        f"rolled status back to {final.status!r} after a "
        f"RunnerStartTerminalError. The cancel was the user's intent — "
        f"don't resurrect the editing state."
    )

    # runner.start was attempted exactly once.
    assert len(runner.calls) == 1


def test_overwrite_concurrent_cancel_after_claim_does_not_resurrect(
    tmp_path: Path,
) -> None:
    """P1-15b batch 3 follow-up² (Codex review of fe922df):
    after _commit_overwrite atomically claims editing→running, the
    job is in status=running for hundreds of ms during FS prep
    BEFORE any worker process exists. A concurrent
    POST /jobs/{id}/cancel can see status=running, find no process
    to stop, and flip the record to cancelled. Without the new
    pre-submit re-check + RunnerStartTerminalError fail-closed,
    runner.start would silently overwrite cancelled with running,
    starting a pipeline the user already cancelled.

    Race construction: monkey-patch the runner to land a "concurrent
    cancel" (direct save_job to status=cancelled) when its start()
    is called. Then verify _commit_overwrite raises
    CommitPipelineError, the on-disk status remains 'cancelled', and
    no pipeline was actually scheduled.
    """
    from dataclasses import replace as _replace
    from services.jobs.editing_commit import (
        _commit_overwrite,
        CommitPipelineError,
    )
    from services.jobs.models import JOB_STATUS_CANCELLED

    store, record, project_dir = _build_editing_job_with_diff(tmp_path)

    class _CancellingRunner:
        """Simulates the cancel-during-FS-prep window: when start()
        is called, first commit a cancel directly to disk, then
        invoke the real ProcessJobRunner.start which now fail-closes
        on terminal status.

        We don't need a real ProcessJobRunner here because the
        runner.start invocation chain calls update_job(...) which
        reads the on-disk record fresh — the test's "concurrent
        cancel" save lands on disk before the mutator runs.
        """
        def __init__(self, store):
            self.store = store
            self.calls: list[dict] = []

        def start(self, record_arg, *, continue_existing=False):
            # Record the call attempt up front so the assertion below
            # can verify runner.start WAS attempted even though the
            # update_job mutator below raises.
            self.calls.append({"job_id": record_arg.job_id})
            # Step 1: simulate a concurrent cancel landing AFTER
            # _commit_overwrite's pre-submit re-validate (which
            # already passed because we hadn't cancelled yet) but
            # BEFORE the actual runner.start mutator runs.
            cancelled = _replace(
                self.store.require_job(record_arg.job_id),
                status=JOB_STATUS_CANCELLED,
                current_stage="failed",
                progress_message="Job cancelled by user.",
            )
            self.store.save_job(cancelled)
            # Step 2: now do what the real ProcessJobRunner.start
            # does — call update_job with the fail-closed mutator.
            from services.jobs.process_runner import (
                RunnerStartTerminalError,
                _TERMINAL_RUNNER_STATUSES,
            )
            from services.state_manager import utc_now_iso

            def _start_mutator(current):
                if current.status in _TERMINAL_RUNNER_STATUSES:
                    raise RunnerStartTerminalError(
                        current.job_id, current.status
                    )
                return _replace(
                    current,
                    status=JOB_STATUS_RUNNING,
                    updated_at=utc_now_iso(),
                )
            self.store.update_job(
                record_arg.job_id, _start_mutator, initial=record_arg,
            )

    runner = _CancellingRunner(store)

    with pytest.raises(CommitPipelineError, match="terminal"):
        _commit_overwrite(record, store, runner, project_dir)

    # Cancel should remain — runner.start's fail-closed mutator
    # refused to overwrite cancelled with running.
    final = store.require_job("job_commit")
    assert final.status == JOB_STATUS_CANCELLED, (
        f"P1-15b batch 3 follow-up² regression: runner.start "
        f"resurrected a cancelled job back to {final.status!r}. "
        f"The cancel-during-FS-prep race window still allows "
        f"resurrection."
    )

    # The runner saw exactly one call (we only attempted start once).
    assert len(runner.calls) == 1


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


# ---------------------------------------------------------------------------
# Jianying draft invalidation on overwrite commit
# ---------------------------------------------------------------------------


def test_overwrite_invalidates_jianying_draft_state(tmp_path: Path) -> None:
    """After overwrite commit, Jianying draft state must reset to idle.

    Post-edit commit regenerates alignment/publish (SRTs, re-TTS audio if
    changed), so any existing Jianying draft becomes stale. User must
    re-trigger generation to get an up-to-date draft.
    """
    store, record, project_dir = _build_editing_job_with_diff(tmp_path)

    # Set up pre-existing Jianying draft state
    record.jianying_draft_status = "succeeded"
    record.jianying_draft_started_at = "2026-05-01T10:00:00+00:00"
    record.jianying_draft_completed_at = "2026-05-01T10:05:00+00:00"
    record.jianying_draft_zip_path = str(project_dir / "jianying" / "exports" / "draft.zip")
    record.jianying_draft_user_root = "/Users/test/Jianying"
    store.save_job(record)

    # Create fake on-disk Jianying artifacts
    jianying_dir = project_dir / "jianying"
    (jianying_dir / "exports").mkdir(parents=True)
    (jianying_dir / "exports" / "draft.zip").write_bytes(b"fake zip content")
    (jianying_dir / "content").write_text("fake content")

    runner = _RecordingRunner()

    commit_editing_pipeline(record, store, runner, strategy="overwrite")

    # Verify state reset
    final = store.require_job("job_commit")
    assert final.jianying_draft_status == "idle"
    assert final.jianying_draft_started_at is None
    assert final.jianying_draft_completed_at is None
    assert final.jianying_draft_error is None
    assert final.jianying_draft_zip_path is None
    assert final.jianying_draft_user_root is None

    # Verify on-disk artifacts deleted
    assert not jianying_dir.exists()


def test_overwrite_invalidates_jianying_draft_already_idle_is_noop(tmp_path: Path) -> None:
    """Invalidating a job with idle Jianying state is idempotent."""
    store, record, project_dir = _build_editing_job_with_diff(tmp_path)

    # Jianying state already idle (the default)
    assert record.jianying_draft_status == "idle"
    assert record.jianying_draft_zip_path is None

    runner = _RecordingRunner()

    commit_editing_pipeline(record, store, runner, strategy="overwrite")

    final = store.require_job("job_commit")
    assert final.jianying_draft_status == "idle"
    assert final.jianying_draft_zip_path is None


def test_overwrite_invalidates_jianying_draft_no_disk_artifacts_no_error(
    tmp_path: Path,
) -> None:
    """Invalidating a job with Jianying state but no on-disk directory is safe."""
    store, record, project_dir = _build_editing_job_with_diff(tmp_path)

    # Set up state without creating on-disk artifacts
    record.jianying_draft_status = "succeeded"
    record.jianying_draft_zip_path = "/nonexistent/path.zip"
    store.save_job(record)

    # Verify no jianying/ directory exists
    assert not (project_dir / "jianying").exists()

    runner = _RecordingRunner()

    # Should not raise; idempotent shutil.rmtree with ignore_errors=True
    commit_editing_pipeline(record, store, runner, strategy="overwrite")

    final = store.require_job("job_commit")
    assert final.jianying_draft_status == "idle"
    assert final.jianying_draft_zip_path is None


def test_overwrite_invalidates_jianying_draft_preserves_other_edits(
    tmp_path: Path,
) -> None:
    """Jianying invalidation should not interfere with normal edit commit flow."""
    store, record, project_dir = _build_editing_job_with_diff(
        tmp_path,
        text_edits={"seg_001": "EDITED_TEXT"},
        draft_wavs={"seg_002": b"NEW_WAV"},
    )

    # Add pre-existing Jianying draft
    record.jianying_draft_status = "succeeded"
    record.jianying_draft_zip_path = str(project_dir / "jianying" / "draft.zip")
    (project_dir / "jianying").mkdir()
    (project_dir / "jianying" / "draft.zip").write_bytes(b"stale")
    store.save_job(record)

    runner = _RecordingRunner()

    commit_editing_pipeline(record, store, runner, strategy="overwrite")

    # Verify normal edits applied
    segments = json.loads((project_dir / "editor" / "segments.json").read_text(encoding="utf-8"))
    assert segments[0]["cn_text"] == "EDITED_TEXT"
    assert (project_dir / "editor" / "tts_segments" / "seg_002.wav").read_bytes() == b"NEW_WAV"

    # Verify Jianying state reset
    final = store.require_job("job_commit")
    assert final.jianying_draft_status == "idle"
    assert final.jianying_draft_zip_path is None

    # Verify on-disk artifacts deleted
    assert not (project_dir / "jianying").exists()


def test_copy_as_new_does_not_reset_jianying_state(tmp_path: Path) -> None:
    """copy_as_new creates a fresh JobRecord with default idle state — no reset needed.

    The new JobRecord starts with jianying_draft_status='idle' by default,
    so there's nothing to reset. Only the OVERWRITE path needs the reset
    (since it reuses and mutates the existing JobRecord).
    """
    store, record, project_dir = _build_editing_job_with_diff(tmp_path)

    # Set source to have pre-existing Jianying state
    record.jianying_draft_status = "succeeded"
    record.jianying_draft_zip_path = str(project_dir / "jianying" / "draft.zip")
    (project_dir / "jianying").mkdir()
    (project_dir / "jianying" / "draft.zip").write_bytes(b"source draft")
    store.save_job(record)

    runner = _RecordingRunner()

    result = commit_editing_pipeline(
        record, store, runner,
        strategy="copy_as_new", copy_display_name="Copy",
        new_job_id_factory=lambda: "job_copy_1",
    )

    # Source Jianying state should still be preserved (copy_as_new doesn't touch it)
    source = store.require_job("job_commit")
    assert source.jianying_draft_status == "succeeded"
    assert source.jianying_draft_zip_path is not None
    assert (project_dir / "jianying" / "draft.zip").exists()

    # New copy starts with idle state (default in JobRecord)
    new_copy = store.require_job(result["new_job_id"])
    assert new_copy.jianying_draft_status == "idle"
    assert new_copy.jianying_draft_zip_path is None
