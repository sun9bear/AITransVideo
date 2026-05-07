"""P1-15b (audit 2026-05-07) regression: JobStore.update_job(mutator)
guarantees atomic load → mutate → save under a per-job file_lock.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        D-CRITICAL-2 — every existing caller did
                       ``record = store.require_job(); next = replace(record, ...);
                       store.save_job(next)`` with no lock around the
                       sequence. P0-5 closed the file_lock gap on
                       editing/admin/state, but deliberately left
                       JobStore.save_job alone because the race lives at
                       the caller layer. P1-15b is the caller-layer fix.

Two layers of guard:

§1  Real concurrency: multiple threads calling update_job on the same
    job_id with different mutators must all survive. Without the lock,
    last-write-wins drops at least one mutation.

§2  Reentrancy: a mutator that itself calls update_job (defensively, or
    via cross-helper recursion) must not deadlock. file_lock is
    reentrant per-thread.
"""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from services.jobs.models import JobRecord
from services.jobs.store import JobStore


def _make_record(job_id: str, **overrides) -> JobRecord:
    """Build a minimal JobRecord with a few mutable scalar fields.

    Mirrors the helper in tests/test_jianying_draft_runner.py — the
    JobRecord schema requires job_type / source_type / source_ref /
    output_target / service_mode beyond the obvious fields.
    """
    base = {
        "job_id": job_id,
        "job_type": "localize_video",
        "source_type": "youtube_url",
        "source_ref": "https://youtube.com/watch?v=test",
        "output_target": "editor",
        "speakers": "auto",
        "status": "queued",
        "service_mode": "studio",
        "created_at": "2026-05-07T00:00:00Z",
        "updated_at": "2026-05-07T00:00:00Z",
    }
    base.update(overrides)
    return JobRecord.from_dict(base)


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs")


def test_update_job_writes_mutator_result_to_disk(store):
    job = store.save_job(_make_record("job-1"))
    updated = store.update_job(
        "job-1",
        lambda current: replace(current, status="running"),
    )
    assert updated.status == "running"
    # Round-trip via disk to confirm persistence.
    on_disk = store.require_job("job-1")
    assert on_disk.status == "running"


def test_update_job_passes_freshly_loaded_record_to_mutator(store):
    """If the on-disk record changed between caller's last load and
    update_job, the mutator must see the FRESH record, not a stale one.
    Otherwise concurrent updates from other threads silently disappear
    when the next caller's mutator computes from a stale base."""
    store.save_job(_make_record("job-2"))

    # Simulate "another writer" updating the file directly between
    # our require_job and our update_job calls.
    store.save_job(_make_record("job-2", current_stage="alignment"))

    seen_stage: list[str] = []

    def capture_then_replace(current):
        seen_stage.append(current.current_stage)
        return replace(current, status="running")

    store.update_job("job-2", capture_then_replace)
    # The mutator received the FRESH stage, not whatever a caller
    # might have loaded earlier.
    assert seen_stage == ["alignment"]


def test_update_job_concurrent_mutations_do_not_lose_updates(store):
    """Spawn 8 threads, each doing update_job that appends a unique
    marker to a list field. Without the per-job file_lock, late writes
    overwrite earlier ones and the final list is short."""
    initial = _make_record("job-3", error_summary={"markers": []})
    store.save_job(initial)

    barrier = threading.Barrier(8)

    def worker(marker: str):
        barrier.wait()  # release all threads at once

        def mutator(current):
            existing = list(current.error_summary.get("markers", []))
            existing.append(marker)
            return replace(current, error_summary={"markers": existing})

        store.update_job("job-3", mutator)

    threads = [
        threading.Thread(target=worker, args=(f"m-{i}",)) for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = store.require_job("job-3")
    survived = set(final.error_summary["markers"])
    expected = {f"m-{i}" for i in range(8)}
    missing = expected - survived
    assert not missing, (
        f"P1-15b regression: {len(missing)} of {len(expected)} concurrent "
        f"update_job mutators were lost. Missing markers: {missing}. "
        f"final count: {len(survived)}, expected 8."
    )


def test_update_job_is_reentrant_within_same_thread(store):
    """A mutator that itself calls update_job (e.g. via cross-module
    helper) must not deadlock. file_lock uses RLock + per-thread depth."""
    store.save_job(_make_record("job-4"))

    def outer_mutator(current):
        # Nested update_job from inside the outer mutator's lock scope.
        store.update_job(
            "job-4",
            lambda c: replace(c, current_stage="alignment"),
        )
        # After the nested update, the outer mutator continues with the
        # ORIGINAL `current` it was passed. update_job's contract is
        # that `current` reflects the moment update_job was called;
        # nested writes are visible only to subsequent require_job /
        # update_job calls.
        return replace(current, status="running")

    # If the lock is non-reentrant, this hangs forever; pytest will
    # eventually time out. We add a generous threading-side cancel just
    # in case the test is run interactively.
    completed = threading.Event()

    def driver():
        store.update_job("job-4", outer_mutator)
        completed.set()

    t = threading.Thread(target=driver)
    t.start()
    t.join(timeout=5.0)
    assert completed.is_set(), (
        "P1-15b regression: nested update_job hung — file_lock is no "
        "longer reentrant for the JobStore path."
    )


def test_update_job_raises_when_job_does_not_exist(store):
    """update_job's contract requires the job to already exist; the
    mutator never receives a sentinel because that would invite
    "create-by-update" race conditions.
    """
    with pytest.raises(KeyError):
        store.update_job("nonexistent", lambda c: c)


def test_update_job_with_initial_fallback_writes_first_time(store):
    """``initial`` parameter enables the first-write path that
    ProcessJobRunner.start needs: the JobRecord exists in memory but
    hasn't been persisted yet. update_job must accept it as the mutator
    base instead of raising KeyError.
    """
    seed = _make_record("job-first-write", status="queued")
    updated = store.update_job(
        "job-first-write",
        lambda current: replace(current, status="running"),
        initial=seed,
    )
    assert updated.status == "running"
    on_disk = store.require_job("job-first-write")
    assert on_disk.status == "running"


def test_save_job_serializes_with_concurrent_update_job(store):
    """P1-15b follow-up (Codex review b1fee3a): a direct ``save_job``
    call (the legacy ``require_job → replace → save_job`` pattern still
    used by JobService.update_display_name and others) MUST serialize
    with ``update_job`` holders. Without it, update_job only excludes
    other update_job callers — a stale-snapshot direct save could slip
    in between update_job's load and its internal save_job, clobbering
    fields on both sides.

    We exercise the actual race: thread A enters update_job and the
    mutator sleeps briefly. While A's mutator is running, thread B
    calls save_job directly with a stale snapshot. Without per-job
    file_lock around save_job, B writes during A's critical section
    and A's later save overwrites B's mutation. With the lock, B
    blocks until A finishes; A's mutation lands; then B's save lands
    — but B's snapshot is stale, so this test asserts the FINAL
    on-disk record reflects A's mutation (B re-overwriting with stale
    data is a different problem the broader caller migration solves).

    The minimal observable property here: B must NOT execute its
    save while A is mid-mutation. We check this by having B record
    the timestamp at which it managed to write; that timestamp must
    exceed A's mutator-completion timestamp.
    """
    import time

    store.save_job(_make_record("job-7", status="queued"))

    timeline: list[tuple[str, float]] = []
    barrier = threading.Barrier(2)

    def thread_a_update_job():
        def slow_mutator(current):
            barrier.wait()  # release B
            time.sleep(0.05)  # hold the lock for 50ms
            timeline.append(("a-mutator-finished", time.monotonic()))
            return replace(current, status="running")

        store.update_job("job-7", slow_mutator)
        timeline.append(("a-update-returned", time.monotonic()))

    def thread_b_direct_save():
        barrier.wait()  # released by A's mutator entry
        # Attempt a direct save while A's mutator is mid-flight.
        time.sleep(0.005)  # ensure A entered the lock first
        store.save_job(_make_record("job-7", status="failed"))
        timeline.append(("b-save-returned", time.monotonic()))

    ta = threading.Thread(target=thread_a_update_job)
    tb = threading.Thread(target=thread_b_direct_save)
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    # Extract event timestamps for the assertion.
    by_name = dict(timeline)
    a_mutator_end = by_name["a-mutator-finished"]
    b_save_end = by_name["b-save-returned"]
    assert b_save_end > a_mutator_end, (
        f"P1-15b follow-up regression: thread B's save_job completed "
        f"at {b_save_end:.4f} BEFORE thread A's mutator finished at "
        f"{a_mutator_end:.4f}. save_job is no longer serialized with "
        f"update_job's lock; concurrent direct save can clobber "
        f"in-flight mutations."
    )


def test_update_job_rejects_mutator_changing_job_id(store):
    """P1-15b follow-up (Codex review b1fee3a): the mutator MUST NOT
    return a JobRecord with a different job_id. We hold file_lock(job-A)
    but a mutator that returns ``replace(c, job_id="job-B")`` would
    write to job-B.json under the WRONG lock, silently bypassing
    atomicity. Reject this defensively before save."""
    store.save_job(_make_record("job-8a"))
    store.save_job(_make_record("job-8b"))

    def cross_job_mutator(current):
        return replace(current, job_id="job-8b", status="running")

    with pytest.raises(ValueError, match="changed job_id"):
        store.update_job("job-8a", cross_job_mutator)

    # Both jobs should be unchanged (the failed update didn't write).
    assert store.require_job("job-8a").status == "queued"
    assert store.require_job("job-8b").status == "queued"


def test_update_job_initial_is_ignored_when_record_already_exists(store):
    """``initial`` is a fallback, NOT an override — if the record IS
    already on disk, the mutator must see the on-disk state, not the
    stale ``initial``. Otherwise concurrent writes from other threads
    silently disappear when this caller's ``initial`` clobbers them.
    """
    store.save_job(_make_record("job-init-fresh", status="running"))
    stale_initial = _make_record("job-init-fresh", status="queued")

    seen_status: list[str] = []

    def capture(current):
        seen_status.append(current.status)
        return current

    store.update_job(
        "job-init-fresh", capture, initial=stale_initial,
    )
    # The mutator received the FRESH on-disk status, not the stale
    # initial that the caller hand-built.
    assert seen_status == ["running"]


def test_update_job_release_lock_on_mutator_exception(store):
    """If the mutator raises, the file_lock must release so a subsequent
    call doesn't deadlock."""
    store.save_job(_make_record("job-5"))

    class BoomError(Exception):
        pass

    with pytest.raises(BoomError):
        store.update_job("job-5", lambda c: (_ for _ in ()).throw(BoomError()))

    # Must work after the failure (proves the lock was released):
    store.update_job(
        "job-5",
        lambda c: replace(c, status="running"),
    )
    assert store.require_job("job-5").status == "running"


def test_update_job_to_dict_round_trip_preserves_record_structure(store):
    """The mutator's output is serialized via record.to_dict() and
    re-read by the next JobStore call. Ensure the round-trip preserves
    arbitrary dict fields (e.g. error_summary)."""
    initial = _make_record(
        "job-6",
        error_summary={"key": "value", "nested": {"a": 1}},
    )
    store.save_job(initial)
    updated = store.update_job(
        "job-6",
        lambda c: replace(
            c,
            status="failed",
            error_summary={**c.error_summary, "step": "tts"},
        ),
    )
    assert updated.status == "failed"
    assert updated.error_summary["key"] == "value"
    assert updated.error_summary["step"] == "tts"
    # Verify on disk:
    raw = json.loads(
        (store.root_dir / "job-6.json").read_text(encoding="utf-8")
    )
    assert raw["error_summary"]["step"] == "tts"
