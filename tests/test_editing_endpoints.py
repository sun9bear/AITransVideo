"""Integration tests for T1-1 editing endpoints skeleton.

Scope:
- editing.py business logic (state transitions + filesystem + event emission)
- JobService delegate methods (enter_editing / cancel_editing / commit_editing)

Out of scope (deferred):
- Gateway FOR UPDATE lock behaviour — exercised end-to-end in manual smoke;
  unit-testing the SQLAlchemy lock path requires a live Postgres and is
  tracked as part of §17.4 smoke, not here.
- HTTP-layer dispatch in ``api.py`` ``do_POST`` — the branches are thin
  wrappers around ``service.*_editing``; testing the stdlib ThreadingHTTPServer
  adds significant fixture weight without catching bugs that the delegate
  tests below don't already cover. T1-2 onwards, when real HTTP semantics
  matter (multipart uploads, streaming, etc.), we'll add an HTTP-level suite.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.jobs.editing import (
    EDITING_SUBDIR,
    SUPPORTED_COMMIT_STRATEGIES,
    EditingConflictError,
    cancel_editing,
    commit_editing,
    enter_editing,
    touch_editing,
)
from services.jobs.models import (
    JOB_STATUS_EDITING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JobRecord,
)
from services.jobs.service import JobConflictError, JobService
from services.jobs.store import JobStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_studio_succeeded_record(tmp_path: Path) -> tuple[JobRecord, JobStore, Path]:
    """Build a studio job in succeeded state with a real project_dir
    containing an editor/segments.json baseline (enter_editing snapshots it)."""
    project_dir = tmp_path / "projects" / "job_123"
    (project_dir / "editor").mkdir(parents=True)
    (project_dir / "editor" / "segments.json").write_text(
        '[{"segment_id": "s_001", "cn_text": "hello"}]',
        encoding="utf-8",
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_123",
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
    return record, store, project_dir


class _NullRunner:
    """Minimal runner stub. editing delegates never touch runner attributes;
    JobService.__init__ just assigns it as ``self.runner``."""


def _build_service_with_editing_fixture(tmp_path: Path) -> tuple[JobService, Path]:
    _, store, project_dir = _build_studio_succeeded_record(tmp_path)
    service = JobService(store=store, runner=_NullRunner())
    return service, project_dir


# ---------------------------------------------------------------------------
# editing.enter_editing
# ---------------------------------------------------------------------------


def test_enter_editing_succeeded_to_editing(tmp_path: Path) -> None:
    record, store, project_dir = _build_studio_succeeded_record(tmp_path)

    updated = enter_editing(record, store)

    assert updated.status == JOB_STATUS_EDITING
    assert updated.editing_touched_at is not None
    editing_dir = project_dir / EDITING_SUBDIR
    assert editing_dir.is_dir()
    assert (editing_dir / "tts_segments_draft").is_dir()
    # Baseline snapshot copied byte-for-byte
    baseline = (project_dir / "editor" / "segments.json").read_text(encoding="utf-8")
    assert (editing_dir / "segments.json").read_text(encoding="utf-8") == baseline
    # Persisted
    assert store.require_job("job_123").status == JOB_STATUS_EDITING


def test_enter_editing_rejects_running(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    record = replace(record, status=JOB_STATUS_RUNNING)
    store.save_job(record)

    with pytest.raises(EditingConflictError, match="can only enter editing from succeeded"):
        enter_editing(record, store)


def test_enter_editing_rejects_already_editing(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    updated = enter_editing(record, store)

    with pytest.raises(EditingConflictError, match="already in editing state"):
        enter_editing(updated, store)


def test_enter_editing_rejects_express(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    record = replace(record, service_mode="express")
    store.save_job(record)

    with pytest.raises(EditingConflictError, match="not a Studio job"):
        enter_editing(record, store)


def test_enter_editing_rejects_missing_project_dir(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    record = replace(record, project_dir=str(tmp_path / "does_not_exist"))
    store.save_job(record)

    with pytest.raises(EditingConflictError, match="project_dir does not exist"):
        enter_editing(record, store)


def test_enter_editing_rejects_empty_project_dir(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    record = replace(record, project_dir=None)
    store.save_job(record)

    with pytest.raises(EditingConflictError, match="has no project_dir"):
        enter_editing(record, store)


def test_editing_conflict_is_job_conflict_subclass() -> None:
    """Allows api.py ``except JobConflictError`` path to cover editing errors
    without adding a bespoke except branch (current api.py depends on this)."""
    assert issubclass(EditingConflictError, JobConflictError)


# ---------------------------------------------------------------------------
# enter_editing — lazy baseline seeding from translation/segments.json.
#
# Context: Phase 1 pipeline does not yet emit editor/segments.json at publish
# time, so legacy / pre-Phase-1 succeeded tasks only have translation/segments.json
# on disk. enter_editing derives the editor/ baseline on first call from
# translation, then leaves it alone on subsequent calls (editor/ is
# authoritative once written). See editing._ensure_editor_segments_baseline.
# ---------------------------------------------------------------------------


def _build_legacy_record_with_translation_only(
    tmp_path: Path,
    *,
    translation_payload: object,
) -> tuple[JobRecord, JobStore, Path]:
    """Studio succeeded job with ONLY translation/segments.json on disk
    (no editor/segments.json). Used to exercise the lazy seed path."""
    project_dir = tmp_path / "projects" / "job_legacy"
    (project_dir / "translation").mkdir(parents=True)
    (project_dir / "translation" / "segments.json").write_text(
        json.dumps(translation_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_legacy",
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
    return record, store, project_dir


def test_enter_editing_seeds_baseline_from_translation_when_editor_absent(
    tmp_path: Path,
) -> None:
    """Legacy task: editor/segments.json missing, translation/segments.json
    carries the canonical pipeline shape ``{"segments": [...]}``. enter_editing
    should derive editor/segments.json verbatim from the segments list and
    copy it to the editing buffer."""
    translation_payload = {
        "segments": [
            {"segment_id": "s_001", "cn_text": "第一段", "speaker_id": "speaker_a", "voice_id": "v1"},
            {"segment_id": "s_002", "cn_text": "第二段", "speaker_id": "speaker_b", "voice_id": "v2"},
        ],
        "total_segments": 2,
        "output_path": "/ignored",
    }
    record, store, project_dir = _build_legacy_record_with_translation_only(
        tmp_path, translation_payload=translation_payload
    )
    assert not (project_dir / "editor" / "segments.json").exists()

    updated = enter_editing(record, store)

    assert updated.status == JOB_STATUS_EDITING
    baseline_path = project_dir / "editor" / "segments.json"
    editing_path = project_dir / EDITING_SUBDIR / "segments.json"
    assert baseline_path.is_file(), "editor/segments.json should be created"
    assert editing_path.is_file(), "editing buffer should be populated"

    baseline_segments = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline_segments == translation_payload["segments"], (
        "baseline should be the translation segments list verbatim"
    )
    editing_segments = json.loads(editing_path.read_text(encoding="utf-8"))
    assert editing_segments == baseline_segments, (
        "editing buffer should be a faithful copy of the seeded baseline"
    )


def test_enter_editing_second_visit_reads_editor_not_translation(
    tmp_path: Path,
) -> None:
    """After first enter_editing seeds editor/segments.json, a subsequent
    enter_editing (after cancel) must reuse the existing editor/ baseline
    even if translation/segments.json has since changed. This guards against
    a regression where a post-overwrite alignment rerun rewrites translation/
    and a second edit session would pick up the wrong baseline."""
    original = {
        "segments": [{"segment_id": "s_001", "cn_text": "原始", "speaker_id": "a"}],
    }
    record, store, project_dir = _build_legacy_record_with_translation_only(
        tmp_path, translation_payload=original
    )

    # First edit session: seed baseline + cancel.
    editing_record = enter_editing(record, store)
    cancelled = cancel_editing(editing_record, store, reason="user_cancel")
    assert cancelled.status == JOB_STATUS_SUCCEEDED
    baseline_path = project_dir / "editor" / "segments.json"
    assert baseline_path.is_file()
    baseline_after_first = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline_after_first == original["segments"]

    # Mutate translation/ to simulate drift (e.g. pipeline re-alignment).
    drifted = {
        "segments": [
            {"segment_id": "s_001", "cn_text": "漂移内容", "speaker_id": "z"},
            {"segment_id": "s_999", "cn_text": "不应该出现", "speaker_id": "z"},
        ],
    }
    (project_dir / "translation" / "segments.json").write_text(
        json.dumps(drifted, ensure_ascii=False), encoding="utf-8"
    )

    # Second edit session must see the ORIGINAL baseline, not the drift.
    editing_record_2 = enter_editing(cancelled, store)
    assert editing_record_2.status == JOB_STATUS_EDITING
    baseline_after_second = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline_after_second == original["segments"], (
        "editor/segments.json must be frozen after first seed; second "
        "enter_editing must not re-read translation/"
    )
    editing_path = project_dir / EDITING_SUBDIR / "segments.json"
    editing_after_second = json.loads(editing_path.read_text(encoding="utf-8"))
    assert editing_after_second == original["segments"]


def test_enter_editing_rejects_when_translation_missing(tmp_path: Path) -> None:
    """No editor/segments.json and no translation/segments.json → 409 with
    a message that points at the missing baseline, not a silently empty
    segment table."""
    project_dir = tmp_path / "projects" / "job_legacy"
    project_dir.mkdir(parents=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_legacy",
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

    with pytest.raises(EditingConflictError, match="cannot seed editor/segments.json"):
        enter_editing(record, store)


def test_enter_editing_rejects_when_translation_has_no_segments_list(
    tmp_path: Path,
) -> None:
    """translation/segments.json exists but lacks a usable ``segments`` list
    (e.g. top-level is a dict with only metadata, or a number, or malformed).
    Must 409 rather than silently seed an empty baseline."""
    for bad_payload in [
        {"total_segments": 0, "output_path": "/p"},   # dict without 'segments'
        {"segments": "not a list"},                    # dict with wrong shape
        42,                                            # not a container at all
    ]:
        record, store, _ = _build_legacy_record_with_translation_only(
            tmp_path / f"case_{id(bad_payload)}",
            translation_payload=bad_payload,
        )
        with pytest.raises(
            EditingConflictError, match="has no usable 'segments' list"
        ):
            enter_editing(record, store)


def test_enter_editing_lazy_seed_normalises_integer_segment_ids(tmp_path: Path) -> None:
    """translation/segments.json carries integer segment_id values (that's
    what pipeline emits today). The editing layer — HTTP contract, input
    validators, patch/regen lookups — all treat segment_id as str. Seed
    must normalise int → str so downstream patch / regenerate-tts calls
    with string ids like '4' can match rows written out of translation."""
    translation_payload = {
        "segments": [
            {"segment_id": 1, "cn_text": "一"},
            {"segment_id": 2, "cn_text": "二"},
            {"segment_id": 10, "cn_text": "十"},
        ],
    }
    record, store, project_dir = _build_legacy_record_with_translation_only(
        tmp_path, translation_payload=translation_payload
    )

    enter_editing(record, store)

    seeded = json.loads(
        (project_dir / "editor" / "segments.json").read_text(encoding="utf-8")
    )
    ids = [s["segment_id"] for s in seeded]
    assert ids == ["1", "2", "10"], (
        f"segment_id values in editor/segments.json must be str; got {ids}"
    )
    # Editing buffer is copied after normalisation, so it inherits str ids too.
    buffered = json.loads(
        (project_dir / EDITING_SUBDIR / "segments.json").read_text(encoding="utf-8")
    )
    assert [s["segment_id"] for s in buffered] == ["1", "2", "10"]


def test_enter_editing_lazy_seed_does_not_touch_translation(tmp_path: Path) -> None:
    """Seeding editor/segments.json must be a one-way READ of
    translation/segments.json. After enter_editing, translation/segments.json
    must be byte-for-byte unchanged — nobody in the editing layer has any
    business rewriting a pipeline artifact.

    This is the spirit of CodeX's "overwrite commit 只改 editor/segments.json"
    guardrail, but applied earlier (at seed time) so translation/ stays clean
    even if the user never commits."""
    translation_payload = {
        "segments": [{"segment_id": "s_001", "cn_text": "hi"}],
        "total_segments": 1,
    }
    record, store, project_dir = _build_legacy_record_with_translation_only(
        tmp_path, translation_payload=translation_payload
    )
    translation_path = project_dir / "translation" / "segments.json"
    before_bytes = translation_path.read_bytes()

    enter_editing(record, store)

    after_bytes = translation_path.read_bytes()
    assert after_bytes == before_bytes, (
        "translation/segments.json must be untouched by the editing layer"
    )


# ---------------------------------------------------------------------------
# editing.cancel_editing
# ---------------------------------------------------------------------------


def test_cancel_editing_drops_draft_and_reverts_status(tmp_path: Path) -> None:
    record, store, project_dir = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)
    editing_dir = project_dir / EDITING_SUBDIR
    assert editing_dir.is_dir()

    reverted = cancel_editing(editing_record, store, reason="user_cancel")

    assert reverted.status == JOB_STATUS_SUCCEEDED
    assert reverted.editing_touched_at is None
    assert not editing_dir.exists()
    assert store.require_job("job_123").status == JOB_STATUS_SUCCEEDED


def test_cancel_editing_records_reason_on_event(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)

    cancel_editing(editing_record, store, reason="admin_force")

    events = store.load_events("job_123")
    cancel_events = [e for e in events if e.message and "editing.cancelled" in e.message]
    assert len(cancel_events) == 1
    assert "reason=admin_force" in cancel_events[0].message


def test_cancel_editing_rejects_non_editing(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)

    with pytest.raises(EditingConflictError, match="not in editing state"):
        cancel_editing(record, store, reason="user_cancel")


def test_cancel_editing_survives_missing_editing_dir(tmp_path: Path) -> None:
    """Robust against partial states (e.g. manual rm -rf)."""
    record, store, project_dir = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)
    # Manually blow away the dir before cancel fires
    import shutil as _shutil

    _shutil.rmtree(project_dir / EDITING_SUBDIR)
    reverted = cancel_editing(editing_record, store, reason="user_cancel")

    assert reverted.status == JOB_STATUS_SUCCEEDED


# ---------------------------------------------------------------------------
# editing.commit_editing (T1-1 skeleton)
# ---------------------------------------------------------------------------


def test_commit_editing_valid_overwrite_hits_not_implemented(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)

    with pytest.raises(NotImplementedError, match="T1-9"):
        commit_editing(editing_record, store, strategy="overwrite")


def test_commit_editing_copy_as_new_with_name_hits_not_implemented(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)

    with pytest.raises(NotImplementedError):
        commit_editing(
            editing_record,
            store,
            strategy="copy_as_new",
            copy_display_name="A · 副本 1",
        )


def test_commit_editing_rejects_unknown_strategy(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)

    with pytest.raises(EditingConflictError, match="unsupported commit strategy"):
        commit_editing(editing_record, store, strategy="bogus")


def test_commit_editing_rejects_non_editing(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)

    with pytest.raises(EditingConflictError, match="not in editing state"):
        commit_editing(record, store, strategy="overwrite")


def test_commit_editing_copy_as_new_requires_non_empty_display_name(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)

    with pytest.raises(EditingConflictError, match="requires a non-empty copy_display_name"):
        commit_editing(editing_record, store, strategy="copy_as_new")
    with pytest.raises(EditingConflictError, match="requires a non-empty copy_display_name"):
        commit_editing(editing_record, store, strategy="copy_as_new", copy_display_name="   ")


def test_supported_commit_strategies_contract() -> None:
    """Locked so frontend can code against this set directly."""
    assert SUPPORTED_COMMIT_STRATEGIES == frozenset({"overwrite", "copy_as_new"})


# ---------------------------------------------------------------------------
# editing.touch_editing
# ---------------------------------------------------------------------------


def test_touch_editing_refreshes_touched_at(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)
    original = editing_record.editing_touched_at
    assert original is not None

    time.sleep(0.005)  # ensure observable delta in ISO timestamp string ordering

    touched = touch_editing(editing_record, store)

    assert touched.editing_touched_at is not None
    assert touched.editing_touched_at > original
    assert touched.status == JOB_STATUS_EDITING
    # Persisted
    assert store.require_job("job_123").editing_touched_at == touched.editing_touched_at


def test_touch_editing_noop_when_not_editing(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)

    result = touch_editing(record, store)

    # Succeeded job — touch should return the record unchanged and NOT
    # persist a bogus touched_at.
    assert result.status == JOB_STATUS_SUCCEEDED
    assert result.editing_touched_at is None


# ---------------------------------------------------------------------------
# Event emission (append_event on enter + cancel)
# ---------------------------------------------------------------------------


def test_enter_editing_emits_status_event(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    enter_editing(record, store)

    events = store.load_events("job_123")
    enter_events = [e for e in events if e.message and "editing.entered" in e.message]
    assert len(enter_events) == 1
    assert enter_events[0].status == JOB_STATUS_EDITING
    assert enter_events[0].event_type == "status"


def test_enter_then_cancel_emits_two_events_in_order(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)
    cancel_editing(editing_record, store, reason="user_cancel")

    events = store.load_events("job_123")
    editing_events = [
        e for e in events
        if e.message and ("editing.entered" in e.message or "editing.cancelled" in e.message)
    ]
    assert len(editing_events) == 2
    assert "editing.entered" in editing_events[0].message
    assert "editing.cancelled" in editing_events[1].message


# ---------------------------------------------------------------------------
# JobService delegates
# ---------------------------------------------------------------------------


def test_service_enter_editing_delegates(tmp_path: Path) -> None:
    service, project_dir = _build_service_with_editing_fixture(tmp_path)

    updated = service.enter_editing("job_123")

    assert updated.status == JOB_STATUS_EDITING
    assert (project_dir / EDITING_SUBDIR).is_dir()


def test_service_cancel_editing_passes_reason_through(tmp_path: Path) -> None:
    service, _ = _build_service_with_editing_fixture(tmp_path)
    service.enter_editing("job_123")

    reverted = service.cancel_editing("job_123", reason="idle_24h_auto_cancel")

    assert reverted.status == JOB_STATUS_SUCCEEDED
    events = service.read_logs("job_123")
    assert any("idle_24h_auto_cancel" in (e.message or "") for e in events)


def test_service_cancel_editing_skips_audit_when_concurrent_winner_lands_after_require_job(
    tmp_path: Path,
) -> None:
    """P1-15b batch 2 follow-up² (Codex review of c170cff):

    The race the previous diff-based predicate could not detect:
      1. JobService.cancel_editing calls require_job, gets snapshot
         with status=editing + editing_touched_at=T1.
      2. BEFORE update_job acquires its lock, another cancel commits
         → on-disk: status=succeeded + editing_touched_at=None.
      3. THIS call's mutator runs under the lock, sees current is
         already succeeded, returns current unchanged → no-op.
      4. Result is succeeded + editing_touched_at=None.

    Caller's record diff (editing→succeeded, T1→None) AND result
    state both look exactly like a real transition. The fix is to
    expose the lock-internal transition_happened flag from
    cancel_editing_atomic so JobService can distinguish.

    We construct the race by monkey-patching update_job to perform
    the concurrent winner's write inside the same lock, then return
    the freshly-loaded current state — exactly what would happen if
    the winner committed between require_job and update_job's
    own load.
    """
    from dataclasses import replace as _replace
    from services.jobs.models import JOB_STATUS_EDITING

    service, project_dir = _build_service_with_editing_fixture(tmp_path)
    service.enter_editing("job_123")

    audit_path = project_dir / "audit" / "user_edit_events.jsonl"
    pre_audit = (
        audit_path.read_text(encoding="utf-8").splitlines()
        if audit_path.exists() else []
    )

    # Monkey-patch update_job to inject the concurrent winner BETWEEN
    # the original update_job's load_job call and the mutator
    # invocation. We achieve this by wrapping update_job: on first
    # call we land the winner's transition before delegating to the
    # real implementation, so the real load_job inside update_job
    # sees status=succeeded.
    real_update_job = service.store.update_job

    def fake_update_job(job_id, mutator, *, initial=None):
        # Simulate the concurrent winner: another cancel committed
        # the editing → succeeded transition just before this call's
        # mutator runs. We do it by saving the won state directly,
        # then deferring to the real update_job which now sees
        # current.status == succeeded and the mutator no-ops.
        won = _replace(
            service.store.require_job(job_id),
            status=JOB_STATUS_SUCCEEDED,
            editing_touched_at=None,
        )
        service.store.save_job(won)
        return real_update_job(job_id, mutator, initial=initial)

    service.store.update_job = fake_update_job  # type: ignore[assignment]
    try:
        result = service.cancel_editing("job_123", reason="user_cancel")
    finally:
        service.store.update_job = real_update_job  # type: ignore[assignment]

    # The result reflects the concurrent winner's transition (so HTTP
    # caller still gets a coherent record back), but THIS call did NOT
    # contribute the transition.
    assert result.status == JOB_STATUS_SUCCEEDED
    assert result.editing_touched_at is None

    # The audit ledger MUST NOT have grown — the post_edit_cancelled
    # event must only fire on a real transition that THIS call drove.
    post_audit = (
        audit_path.read_text(encoding="utf-8").splitlines()
        if audit_path.exists() else []
    )
    assert post_audit == pre_audit, (
        f"P1-15b batch 2 follow-up² regression: post_edit_cancelled "
        f"audit emitted on a no-op cancel_editing. The diff-based "
        f"predicate could not distinguish 'this call transitioned' "
        f"from 'this call observed someone else's transition'. "
        f"audit grew by {len(post_audit) - len(pre_audit)} line(s)."
    )


def test_service_cancel_editing_skips_audit_event_on_concurrent_no_op(
    tmp_path: Path,
) -> None:
    """P1-15b batch 2 follow-up (Codex review of 5748978):
    JobService.cancel_editing must NOT emit a post_edit_cancelled
    user-edit audit event when ``editing.cancel_editing`` no-op'd
    because the underlying record already left editing state (e.g.
    a concurrent commit/cancel won the race).

    Setup: enter editing → cancel once (real transition, audit
    emitted) → cancel AGAIN with the same job_id but the helper
    sees status=succeeded and no-ops → audit log MUST NOT grow.
    """
    service, project_dir = _build_service_with_editing_fixture(tmp_path)

    # Real transition: enter then cancel once.
    service.enter_editing("job_123")
    first_result = service.cancel_editing("job_123", reason="user_cancel")
    assert first_result.status == JOB_STATUS_SUCCEEDED

    # Audit ledger lives at {project_dir}/audit/user_edit_events.jsonl
    audit_path = project_dir / "audit" / "user_edit_events.jsonl"
    audit_lines_after_first = (
        audit_path.read_text(encoding="utf-8").splitlines()
        if audit_path.exists() else []
    )
    assert any(
        "post_edit_cancelled" in line for line in audit_lines_after_first
    ), (
        "sanity: real cancel should have emitted a post_edit_cancelled "
        "audit event (so we have something to compare against)"
    )

    # Now call cancel_editing again on a job that is no longer in
    # editing state. The legacy entry-point check would have raised
    # EditingConflictError; with P1-15b batch 2 the helper still
    # raises on a pre-lock stale snapshot. We exercise the OTHER
    # no-op path by constructing the race directly: enter editing,
    # then have one path "win" the cancel via a direct save_job, and
    # verify the second cancel_editing call doesn't double-emit.
    service.enter_editing("job_123")
    # Simulate a concurrent winner: flip the on-disk record to
    # SUCCEEDED out of band so cancel_editing's mutator sees the
    # status has already changed and returns current unchanged.
    fresh = service.store.require_job("job_123")
    from dataclasses import replace as _replace
    won = _replace(
        fresh,
        status=JOB_STATUS_SUCCEEDED,
        editing_touched_at=None,
    )
    service.store.save_job(won)

    # Now JobService.cancel_editing's pre-helper require_job returns
    # SUCCEEDED, and the helper raises EditingConflictError. Catch
    # it and verify no extra audit event landed.
    audit_lines_before_second = (
        audit_path.read_text(encoding="utf-8").splitlines()
        if audit_path.exists() else []
    )
    with pytest.raises(EditingConflictError):
        service.cancel_editing("job_123", reason="user_cancel")

    audit_lines_after_second = (
        audit_path.read_text(encoding="utf-8").splitlines()
        if audit_path.exists() else []
    )
    assert audit_lines_after_second == audit_lines_before_second, (
        "P1-15b batch 2 follow-up regression: a no-op'd cancel_editing "
        "still emitted a post_edit_cancelled audit event. The audit "
        "ledger now records cancellations that didn't happen."
    )


def test_service_commit_editing_dispatches_to_pipeline(tmp_path: Path) -> None:
    """T1-9 replaced the skeleton with a real commit pipeline; service
    delegate now returns a dict and calls through the fake runner.

    (Full overwrite / copy_as_new coverage lives in tests/test_editing_commit.py —
    this test just guards the service-layer wiring from regressing.)"""
    service, _ = _build_service_with_editing_fixture(tmp_path)
    service.enter_editing("job_123")

    # The default _NullRunner has no start() — swap in a minimal recording one.
    class _RecordingRunner:
        def __init__(self) -> None:
            self.calls = []
        def start(self, record, continue_existing=False):
            self.calls.append((record.job_id, continue_existing))

    service.runner = _RecordingRunner()  # type: ignore[assignment]
    result = service.commit_editing("job_123", strategy="overwrite")
    assert result["strategy"] == "overwrite"
    assert result["job_id"] == "job_123"
    assert service.runner.calls == [("job_123", True)]

    duplicate = service.commit_editing("job_123", strategy="overwrite")
    assert duplicate["strategy"] == "overwrite"
    assert duplicate["job_id"] == "job_123"
    assert duplicate["already_started"] is True
    assert service.runner.calls == [("job_123", True)]

    completed = replace(
        service.store.load_job("job_123"),
        status=JOB_STATUS_SUCCEEDED,
        current_stage="completed",
    )
    service.store.save_job(completed)
    duplicate_after_finish = service.commit_editing("job_123", strategy="overwrite")
    assert duplicate_after_finish["strategy"] == "overwrite"
    assert duplicate_after_finish["already_started"] is True
    assert duplicate_after_finish["already_completed"] is True
    assert service.runner.calls == [("job_123", True)]


def test_service_enter_editing_on_nonexistent_job(tmp_path: Path) -> None:
    from services.jobs.service import JobNotFoundError

    store = JobStore(tmp_path / "jobs")
    service = JobService(store=store, runner=_NullRunner())

    with pytest.raises(JobNotFoundError):
        service.enter_editing("ghost")


# ---------------------------------------------------------------------------
# Cross-module contract smoke: editing_touched_at persists through store
# round-trip, and cancel clears it back to None.
# ---------------------------------------------------------------------------


def test_editing_touched_at_round_trips_through_store(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    enter_editing(record, store)

    # Reload from disk
    reloaded = store.require_job("job_123")
    assert reloaded.status == JOB_STATUS_EDITING
    assert reloaded.editing_touched_at is not None

    # Cancel and reload again
    cancel_editing(reloaded, store, reason="user_cancel")
    reloaded2 = store.require_job("job_123")
    assert reloaded2.status == JOB_STATUS_SUCCEEDED
    assert reloaded2.editing_touched_at is None
