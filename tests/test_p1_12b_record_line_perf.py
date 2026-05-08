"""Audit P1-12b regression: ProcessJobRunner._record_line group-commit + skip-noop.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        P-CRITICAL-2 — every pipeline stdout line triggered:
                       (a) full JobRecord JSON rewrite (~30 KB)
                       (b) 2× ``os.fsync`` (one in _write_json_atomic,
                           one in append_event)
                       3000 lines per 30-min pipeline = 6-30s of pure
                       IO + 60-180 MB write amplification on SSD,
                       3-5 min on HDD.

Two fixes ship together in P1-12b:

  1. **Skip-noop short-circuit**: ``_record_line`` no longer rewrites
     the JobRecord when ``current_stage`` / ``progress_message`` /
     ``project_dir`` are unchanged from the prior line. Many stdout
     lines map to the same parsed stage+message; skipping the
     rewrite eliminates the bulk of the write amplification.

  2. **fsync=False group-commit**: per-line writes (both the
     JobRecord rewrite when needed AND the events.jsonl append) skip
     ``os.fsync``. The OS page cache still absorbs the bytes; the
     next strict write — terminal status flip in
     ``_finalize_process``, or any HTTP-side mutation — flushes the
     filesystem journal and durably commits the buffered writes.

Terminal writes (start, finalize, status flips, copy_as_new) MUST
keep ``fsync=True`` so a crash never loses the acknowledgement of
state transitions.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = str(_REPO_ROOT / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


# ---------------------------------------------------------------------------
# JobStore-level fsync flag honored
# ---------------------------------------------------------------------------


def _make_store(tmp_path):
    from services.jobs.store import JobStore
    return JobStore(tmp_path / "jobs")


def _make_minimal_record():
    from services.jobs.models import JobRecord
    return JobRecord.from_dict({
        "job_id": "p112b-test",
        "job_type": "localize_video",
        "source_type": "youtube_url",
        "source_ref": "https://youtu.be/test",
        "output_target": "editor",
        "speakers": "auto",
        "status": "queued",
        "service_mode": "studio",
        "created_at": "2026-05-08T00:00:00Z",
        "updated_at": "2026-05-08T00:00:00Z",
    })


def test_save_job_with_fsync_true_calls_os_fsync(tmp_path):
    """Default behavior: ``save_job`` calls ``os.fsync`` once for the
    temp file before the rename. Crash-durability guarantee depends
    on this."""
    store = _make_store(tmp_path)
    record = _make_minimal_record()
    with patch("services.jobs.store.os.fsync") as mock_fsync:
        store.save_job(record)
    assert mock_fsync.call_count == 1, (
        "P1-12b regression: strict save_job no longer fsyncs. "
        "Terminal writes need durability — without fsync a crash "
        "between rename and journal flush could leave zeroed bytes."
    )


def test_save_job_with_fsync_false_skips_os_fsync(tmp_path):
    """P1-12b group-commit: pass ``fsync=False`` to skip fsync."""
    store = _make_store(tmp_path)
    record = _make_minimal_record()
    with patch("services.jobs.store.os.fsync") as mock_fsync:
        store.save_job(record, fsync=False)
    assert mock_fsync.call_count == 0, (
        "P1-12b regression: save_job(..., fsync=False) still called "
        f"os.fsync {mock_fsync.call_count} times. Group-commit mode "
        "MUST skip fsync — that's the entire point of the parameter."
    )


def test_append_event_with_fsync_true_calls_os_fsync(tmp_path):
    from services.jobs.events import EVENT_TYPE_LOG, JobEvent
    store = _make_store(tmp_path)
    event = JobEvent(
        job_id="p112b-test",
        event_type=EVENT_TYPE_LOG,
        created_at="2026-05-08T00:00:00Z",
        message="hello",
    )
    with patch("services.jobs.store.os.fsync") as mock_fsync:
        store.append_event("p112b-test", event)
    assert mock_fsync.call_count == 1


def test_append_event_with_fsync_false_skips_os_fsync(tmp_path):
    from services.jobs.events import EVENT_TYPE_LOG, JobEvent
    store = _make_store(tmp_path)
    event = JobEvent(
        job_id="p112b-test",
        event_type=EVENT_TYPE_LOG,
        created_at="2026-05-08T00:00:00Z",
        message="hello",
    )
    with patch("services.jobs.store.os.fsync") as mock_fsync:
        store.append_event("p112b-test", event, fsync=False)
    assert mock_fsync.call_count == 0


def test_update_job_with_fsync_false_propagates(tmp_path):
    from dataclasses import replace as dc_replace
    store = _make_store(tmp_path)
    store.save_job(_make_minimal_record())  # fsync=True (default) on baseline
    with patch("services.jobs.store.os.fsync") as mock_fsync:
        store.update_job(
            "p112b-test",
            lambda current: dc_replace(current, status="running"),
            fsync=False,
        )
    assert mock_fsync.call_count == 0


def test_update_job_skips_write_when_mutator_returns_unchanged(tmp_path):
    """P1-12b skip-noop fast path. If the mutator returns the SAME
    dataclass instance (or an equal one), no rewrite happens —
    PROVIDED the on-disk record was loaded successfully. First-write
    callers (initial=record, identity mutator) MUST still persist;
    that case has its own regression test below."""
    store = _make_store(tmp_path)
    store.save_job(_make_minimal_record())

    with patch.object(store, "save_job", wraps=store.save_job) as save_spy:
        result = store.update_job(
            "p112b-test",
            lambda current: current,  # identity mutator
        )
    assert save_spy.call_count == 0, (
        "P1-12b regression: update_job called save_job even though "
        "the mutator returned the input unchanged. The fast path is "
        "the entire point of P-CRITICAL-2 fix — without it,"
        "_record_line still triggers a write per stdout line on no-op "
        "stage/message updates."
    )
    # And the returned record is the unchanged current.
    assert result.status == "queued"


def test_update_job_persists_first_write_even_when_mutator_is_identity(tmp_path):
    """P1-12b regression (introduced + fixed in same commit): the
    skip-noop optimization must NOT fire on first writes.
    ``ProcessJobRunner.start`` and ``editing_commit.copy_as_new`` both
    call ``update_job(..., initial=record, mutator=lambda c: c)`` to
    create a new record with a fresh job_id — the on-disk file
    doesn't exist yet, the mutator is identity, so trivially
    ``updated == current``. Without the ``loaded_from_disk`` guard
    the optimization would skip persisting the initial record
    entirely; subsequent ``require_job`` calls would fail with
    KeyError and the new copy would be invisible to the runner."""
    store = _make_store(tmp_path)
    fresh_record = _make_minimal_record()
    # No prior save_job — file doesn't exist on disk.

    result = store.update_job(
        fresh_record.job_id,
        lambda current: current,  # identity mutator
        initial=fresh_record,
    )
    # The record is now persisted and reachable via require_job.
    on_disk = store.require_job(fresh_record.job_id)
    assert on_disk.job_id == fresh_record.job_id, (
        "P1-12b regression: first-write update_job(initial=...) with "
        "an identity mutator did NOT persist the initial record. "
        "copy_as_new and ProcessJobRunner.start would silently fail "
        "to create new job records."
    )
    assert result.job_id == fresh_record.job_id


# ---------------------------------------------------------------------------
# ProcessJobRunner._record_line: skip-noop + fsync=False
# ---------------------------------------------------------------------------


def _make_runner_with_record(tmp_path):
    from services.jobs.process_runner import ProcessJobRunner
    store = _make_store(tmp_path)
    record = _make_minimal_record()
    # Bring it to a state where a typical pipeline log line wouldn't
    # change anything (status=running, current_stage=ingestion).
    from dataclasses import replace as dc_replace
    record = dc_replace(record, status="running", current_stage="ingestion")
    store.save_job(record)
    runner = ProcessJobRunner(
        project_root=tmp_path / "projects",
        store=store,
    )
    return runner, store, record


def test_record_line_no_op_does_not_rewrite_jobrecord(tmp_path):
    """P1-12b: a stdout line that doesn't move stage / message must
    NOT trigger a JobRecord rewrite. We assert by spying on save_job
    via update_job's call into it — count must stay at 0 even after
    multiple no-op lines."""
    runner, store, _ = _make_runner_with_record(tmp_path)

    with patch.object(store, "save_job", wraps=store.save_job) as save_spy:
        # A truly arbitrary log line that the stage parser won't
        # recognize as a stage transition. ``_resolve_stage_from_log_line``
        # returns the input stage / message unchanged on unknown text.
        for _ in range(5):
            runner._record_line("p112b-test", "[INFO] arbitrary noise that maps to no stage")

    assert save_spy.call_count == 0, (
        "P1-12b regression: _record_line called save_job "
        f"{save_spy.call_count} time(s) for no-op log lines. The "
        "skip-noop short-circuit must keep the JobRecord untouched "
        "when stage / message / project_dir don't change. Without "
        "this, 3000-line pipelines write 90 MB of redundant JSON."
    )


def test_record_line_writes_when_stage_changes(tmp_path):
    """No-regression: when the log line DOES move stage, we still
    write — just with fsync=False.

    ``_resolve_stage_from_log_line`` recognises ``[S<N>]`` /
    ``[RESUME/S<N>]`` prefixes (see ``STAGE_LOG_PATTERN`` +
    ``STAGE_CODE_MAP``). The current_stage is "ingestion" (S0); we
    feed an [S2] line so the parser MUST flip stage and trigger a
    write."""
    runner, store, _ = _make_runner_with_record(tmp_path)

    # Verify the probe line actually moves stage in the parser. We
    # don't pin specific stage constants because that's an
    # implementation detail; we just need a stage transition.
    from services.jobs.process_runner import _resolve_stage_from_log_line
    test_line = "[S2] 说话人识别完成"
    new_stage, _ = _resolve_stage_from_log_line(
        line=test_line, current_stage="ingestion", current_message=None,
    )
    assert new_stage != "ingestion", (
        "test fixture broken — STAGE_LOG_PATTERN no longer recognises "
        f"'[S2] ...' as a stage transition. Got new_stage={new_stage!r}."
    )

    with patch("services.jobs.store.os.fsync") as mock_fsync:
        with patch.object(store, "save_job", wraps=store.save_job) as save_spy:
            runner._record_line("p112b-test", test_line)

    # save_job was called (a real change happened) but fsync was NOT
    # called for it — fsync=False propagated.
    assert save_spy.call_count >= 1, (
        "P1-12b regression: a real stage change skipped the JobRecord "
        "write. The skip-noop short-circuit is too aggressive."
    )
    # Both the JobRecord rewrite AND the events.jsonl append are
    # per-line writes — both must use fsync=False (group-commit).
    assert mock_fsync.call_count == 0, (
        f"P1-12b regression: per-line write triggered "
        f"{mock_fsync.call_count} fsync(s). fsync=False must propagate "
        "all the way down to JobStore._write_json_atomic and "
        "append_event."
    )


def test_record_line_appends_event_for_every_line(tmp_path):
    """No-regression: skip-noop must NOT skip the events log. UI's
    log tail panel relies on every stdout line landing in
    events.jsonl, even when no JobRecord field changed."""
    runner, store, _ = _make_runner_with_record(tmp_path)

    with patch.object(store, "append_event", wraps=store.append_event) as event_spy:
        # Five no-op log lines.
        for i in range(5):
            runner._record_line("p112b-test", f"[INFO] line {i}")

    assert event_spy.call_count == 5, (
        f"P1-12b regression: only {event_spy.call_count} events "
        "appended for 5 log lines. The skip-noop short-circuit must "
        "ONLY skip the JobRecord rewrite — events.jsonl is the UI's "
        "log tail and every line must land there."
    )
    # Each event was appended with fsync=False.
    for call in event_spy.call_args_list:
        assert call.kwargs.get("fsync") is False, (
            f"P1-12b regression: _record_line appended an event with "
            f"fsync != False. Call: {call}. Per-line events MUST use "
            "group-commit mode."
        )
