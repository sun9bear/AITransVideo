"""Audit P1-12a regression: JobStore.list_jobs in-memory cache + mtime-based
incremental refresh.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        P-CRITICAL-1 — list_jobs ran ``json.loads`` +
                       ``JobRecord.from_dict`` on every {job_id}.json
                       per call. 1000 jobs → 200-800 ms / call;
                       front-end polling 4 s × 5 users could saturate
                       Job API.

The fix maintains a per-store ``_list_cache`` keyed by job_id with
``(mtime_ns, JobRecord)`` entries. Each list_jobs call:

  1. Globs the dir + stat()s each file (cheap)
  2. For each file:
     - Cache hit (mtime unchanged) → reuse cached record (copy)
     - Cache miss / mtime drift → re-parse + update cache
  3. Files that vanished from the glob are dropped from cache

These tests pin the contract:
  * Functional: list_jobs returns the same JobRecords as before (same
    sort, same pagination)
  * Cache: a file with unchanged mtime is NOT re-read on the second
    call
  * Invalidation: a file with bumped mtime IS re-read
  * Cleanup: a deleted file is dropped from cache
  * Defensive copy: caller-side mutation of returned records does NOT
    poison the cache
  * Cross-process: files modified externally (mtime change) get picked
    up without an explicit invalidation hook
"""
from __future__ import annotations

import sys
import time
from dataclasses import replace as dc_replace
from pathlib import Path
from unittest.mock import patch

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = str(_REPO_ROOT / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


def _make_store(tmp_path: Path):
    from services.jobs.store import JobStore
    return JobStore(tmp_path / "jobs")


def _make_record(job_id: str, **overrides):
    from services.jobs.models import JobRecord
    base = {
        "job_id": job_id,
        "job_type": "localize_video",
        "source_type": "youtube_url",
        "source_ref": "https://youtu.be/test",
        "output_target": "editor",
        "speakers": "auto",
        "status": "queued",
        "service_mode": "studio",
        "created_at": "2026-05-08T00:00:00Z",
        "updated_at": "2026-05-08T00:00:00Z",
    }
    base.update(overrides)
    return JobRecord.from_dict(base)


def _bump_mtime(path: Path, *, seconds: float = 1.0) -> None:
    """Force a fresh mtime on ``path``. ``os.utime`` rewrites both atime
    and mtime; we offset by a positive number so the new mtime is
    strictly greater than any previous mtime even on filesystems with
    coarse mtime resolution (Windows NTFS reports 100-ns precision but
    some test harnesses round)."""
    import os as _os
    stat = path.stat()
    new_mtime = stat.st_mtime + seconds
    _os.utime(path, (stat.st_atime, new_mtime))


# ---------------------------------------------------------------------------
# Functional: list_jobs returns same shape as pre-cache
# ---------------------------------------------------------------------------


def test_list_jobs_returns_all_records(tmp_path):
    store = _make_store(tmp_path)
    store.save_job(_make_record("a", updated_at="2026-05-08T01:00:00Z"))
    store.save_job(_make_record("b", updated_at="2026-05-08T02:00:00Z"))
    store.save_job(_make_record("c", updated_at="2026-05-08T03:00:00Z"))

    jobs = store.list_jobs()
    assert [j.job_id for j in jobs] == ["c", "b", "a"], (
        "P1-12a regression: list_jobs sort order changed. Spec is "
        "(updated_at, created_at, job_id) descending."
    )


def test_list_jobs_pagination_unchanged(tmp_path):
    store = _make_store(tmp_path)
    for i in range(5):
        store.save_job(_make_record(
            f"job-{i:02d}",
            updated_at=f"2026-05-08T{i:02d}:00:00Z",
        ))
    page = store.list_jobs(limit=2, offset=1)
    # Sorted descending by updated_at, offset=1 skips the newest,
    # limit=2 picks the next two.
    assert [j.job_id for j in page] == ["job-03", "job-02"]


def test_list_jobs_returns_empty_when_dir_missing(tmp_path):
    """Defensive: missing root_dir returns empty list, doesn't crash."""
    from services.jobs.store import JobStore
    nonexistent = JobStore(tmp_path / "does-not-exist")
    assert nonexistent.list_jobs() == []


# ---------------------------------------------------------------------------
# Cache hit: unchanged mtime skips json.loads
# ---------------------------------------------------------------------------


def test_list_jobs_second_call_skips_json_parse(tmp_path):
    """The second list_jobs call must NOT re-parse JSON for files
    whose mtime hasn't changed. We assert by spying on
    ``json.loads`` calls inside the store module."""
    import services.jobs.store as store_mod

    store = _make_store(tmp_path)
    store.save_job(_make_record("a"))
    store.save_job(_make_record("b"))
    store.save_job(_make_record("c"))

    # Warm the cache with one call.
    store.list_jobs()

    # Second call: mtimes unchanged, so json.loads should NOT fire
    # for any cached entry.
    with patch.object(store_mod, "json", wraps=store_mod.json) as json_spy:
        store.list_jobs()
        loads_calls = json_spy.loads.call_count

    assert loads_calls == 0, (
        f"P1-12a regression: list_jobs called json.loads {loads_calls} "
        "time(s) on the second call despite no mtime drift. Cache hit "
        "path must skip json.loads entirely — that's the entire point "
        "of P-CRITICAL-1's fix."
    )


def test_list_jobs_skips_disk_read_on_cache_hit(tmp_path):
    """Same idea, but more sensitive: on a pure cache hit, even
    ``Path.read_text`` should not run for cached entries — only stat
    is needed."""
    store = _make_store(tmp_path)
    for i in range(3):
        store.save_job(_make_record(f"job-{i}"))

    store.list_jobs()  # warm cache

    read_call_paths: list[Path] = []
    real_read = Path.read_text

    def _spy_read_text(self, *args, **kwargs):
        read_call_paths.append(self)
        return real_read(self, *args, **kwargs)

    with patch.object(Path, "read_text", _spy_read_text):
        store.list_jobs()

    # Only files whose mtime drifted should be re-read; we touched
    # nothing so nothing should be read.
    assert read_call_paths == [], (
        "P1-12a regression: list_jobs read files from disk on a pure "
        f"cache-hit call. read_text was invoked on: {read_call_paths}. "
        "Cache hit path must use stat() only."
    )


# ---------------------------------------------------------------------------
# Cache invalidation: bumped mtime triggers re-parse
# ---------------------------------------------------------------------------


def test_list_jobs_reparses_when_file_mtime_changes(tmp_path):
    """When a file's mtime changes (e.g. another worker rewrote it),
    list_jobs must re-parse and pick up the new content. Cross-process
    invalidation depends on this."""
    store = _make_store(tmp_path)
    store.save_job(_make_record("a", status="queued"))

    store.list_jobs()  # warm cache

    # Simulate another worker rewriting the file with new content.
    from services.jobs.store import JobStore
    other_worker = JobStore(tmp_path / "jobs")
    other_worker.save_job(_make_record("a", status="running"))
    # Force mtime forward in case fs resolution is coarse.
    _bump_mtime(other_worker._job_path("a"), seconds=2.0)

    fresh = store.list_jobs()
    assert len(fresh) == 1
    assert fresh[0].status == "running", (
        f"P1-12a regression: list_jobs returned stale status "
        f"({fresh[0].status!r}) after another worker rewrote the file. "
        "mtime-based invalidation must re-parse on drift."
    )


def test_list_jobs_drops_deleted_files_from_cache(tmp_path):
    """When a file is deleted, the cache entry must be dropped on the
    next list_jobs call. Otherwise stale entries would accumulate."""
    store = _make_store(tmp_path)
    store.save_job(_make_record("a"))
    store.save_job(_make_record("b"))

    store.list_jobs()  # warm cache with both
    assert len(store._list_cache) == 2

    store.delete_job("b")
    after = store.list_jobs()

    assert {j.job_id for j in after} == {"a"}
    assert "b" not in store._list_cache, (
        "P1-12a regression: deleted job 'b' lingered in _list_cache "
        f"after list_jobs ran. Found cache: {set(store._list_cache.keys())}. "
        "Cache entries for files no longer in the glob must be dropped."
    )


# ---------------------------------------------------------------------------
# Defensive copy: caller mutation does not poison the cache
# ---------------------------------------------------------------------------


def test_list_jobs_returns_copies_callers_can_mutate(tmp_path):
    """JobRecord is @dataclass(slots=True) — mutable. At least one
    existing caller does ``record.field = ...; save_job(record)``.
    list_jobs MUST return defensive copies so a caller mutating its
    return value does NOT poison the cache."""
    store = _make_store(tmp_path)
    store.save_job(_make_record("a", status="queued"))

    first = store.list_jobs()
    assert len(first) == 1
    # Mutate the returned record — simulating a careless caller or
    # a save_job-via-replace pattern that aliases.
    first[0].status = "running"

    # Cache must NOT have been mutated.
    cached_record = store._list_cache["a"][1]
    assert cached_record.status == "queued", (
        f"P1-12a regression: cached record's status was mutated to "
        f"{cached_record.status!r} via caller-side mutation of the "
        "returned object. list_jobs must return copies."
    )

    # And the next list_jobs call returns the original (unchanged)
    # cached value, not the mutated one.
    second = store.list_jobs()
    assert second[0].status == "queued", (
        f"P1-12a regression: list_jobs returned mutated status "
        f"{second[0].status!r} on second call. Cache poisoning."
    )


def test_list_jobs_returns_deep_copies_nested_dict_mutation_isolated(tmp_path):
    """P1-12a follow-up (Codex review of 97cc777): the v0
    implementation used ``dataclasses.replace(cached_record)``,
    which only copies the dataclass shell. JobRecord has three
    ``dict[str, object]`` fields — ``review_gate`` / ``error_summary``
    / ``fallback_summary`` — that may carry NESTED dicts/lists. A
    shallow copy left the inner mutable containers aliased between
    the cached record and the returned copy, so a caller doing
    ``jobs[0].review_gate['metadata']['x'] = 999`` would poison the
    cache through the shared inner dict.

    The fix is ``copy.deepcopy`` (via ``_clone_record``). This test
    locks in the contract: nested mutation through a returned record
    is NOT visible on the next list_jobs call.
    """
    store = _make_store(tmp_path)
    store.save_job(_make_record(
        "a",
        review_gate={
            "stage": "voice_review",
            "metadata": {"counter": 0, "tags": ["alpha"]},
        },
        error_summary={"history": [{"code": "x1", "stage": "s2"}]},
        fallback_summary={"tts": {"failures": 0}},
    ))

    first_call = store.list_jobs()
    record = first_call[0]

    # Mutate every nested mutable container the dataclass exposes.
    # If any of these sneaks back into _list_cache, list_jobs() #2
    # will surface the poison.
    record.review_gate["stage"] = "MUTATED"  # top-level dict mutation
    record.review_gate["metadata"]["counter"] = 999  # nested-dict mutation
    record.review_gate["metadata"]["tags"].append("beta")  # nested-list append
    record.error_summary["history"][0]["code"] = "MUTATED"  # nested dict-in-list
    record.error_summary["history"].append({"code": "extra"})  # list append
    record.fallback_summary["tts"]["failures"] = 999  # nested dict mutation

    # Cache (the source of truth for the next list_jobs call) must
    # be untouched.
    cached = store._list_cache["a"][1]
    assert cached.review_gate == {
        "stage": "voice_review",
        "metadata": {"counter": 0, "tags": ["alpha"]},
    }, (
        "P1-12a follow-up regression: review_gate's nested dict was "
        f"mutated through the returned record. Cached value: "
        f"{cached.review_gate!r}. Use copy.deepcopy in _clone_record."
    )
    assert cached.error_summary == {
        "history": [{"code": "x1", "stage": "s2"}]
    }, (
        "P1-12a follow-up regression: error_summary's nested list-of-"
        f"dicts was mutated through the returned record. Cached: "
        f"{cached.error_summary!r}."
    )
    assert cached.fallback_summary == {"tts": {"failures": 0}}, (
        "P1-12a follow-up regression: fallback_summary's nested dict "
        f"was mutated. Cached: {cached.fallback_summary!r}."
    )

    # And the next list_jobs call returns clean records — no leak.
    second_call = store.list_jobs()
    fresh = second_call[0]
    assert fresh.review_gate == {
        "stage": "voice_review",
        "metadata": {"counter": 0, "tags": ["alpha"]},
    }
    assert fresh.error_summary == {
        "history": [{"code": "x1", "stage": "s2"}]
    }
    assert fresh.fallback_summary == {"tts": {"failures": 0}}


# ---------------------------------------------------------------------------
# Concurrency: list_jobs + concurrent save_job don't deadlock or lose
# updates
# ---------------------------------------------------------------------------


def test_concurrent_list_and_save_does_not_deadlock(tmp_path):
    """list_jobs holds the cache lock briefly for snapshot + final
    swap; save_job holds the file lock. They use DIFFERENT locks so
    no deadlock is possible — but a regression that introduced a
    shared lock would surface here."""
    import threading

    store = _make_store(tmp_path)
    for i in range(5):
        store.save_job(_make_record(f"job-{i}"))

    barrier = threading.Barrier(8)
    errors: list[Exception] = []

    def _list_worker():
        try:
            barrier.wait()
            for _ in range(20):
                store.list_jobs()
        except Exception as exc:  # pragma: no cover — debugging help
            errors.append(exc)

    def _save_worker(job_id: str):
        try:
            barrier.wait()
            for _ in range(10):
                store.save_job(_make_record(
                    job_id, updated_at=f"2026-05-08T{int(time.time()) % 24:02d}:00:00Z",
                ))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = (
        [threading.Thread(target=_list_worker) for _ in range(4)]
        + [threading.Thread(target=_save_worker, args=(f"job-{i}",)) for i in range(4)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    for t in threads:
        assert not t.is_alive(), (
            "P1-12a regression: concurrent list_jobs + save_job "
            "deadlocked. The cache lock and file lock must remain "
            "independent."
        )
    assert errors == [], (
        f"P1-12a regression: concurrent workload raised {errors}"
    )


# ---------------------------------------------------------------------------
# Cache survives mtime collision — mtime equality means use cache
# ---------------------------------------------------------------------------


def test_list_jobs_does_not_explode_on_invalid_payload(tmp_path):
    """No-regression: an externally-corrupted JSON file must still
    raise the same ValueError as before, not silently slip into the
    cache as a malformed record."""
    store = _make_store(tmp_path)
    store.save_job(_make_record("a"))

    # Corrupt the file out-of-band.
    bad_path = store.root_dir / "b.json"
    bad_path.write_text("[]", encoding="utf-8")  # array, not dict

    with pytest.raises(ValueError):
        store.list_jobs()
