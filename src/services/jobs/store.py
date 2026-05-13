from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
import tempfile
from typing import Callable

from services._file_lock import file_lock
from services.jobs.events import JobEvent
from services.jobs.models import JobRecord

logger = logging.getLogger(__name__)


def _clone_record(record: JobRecord) -> JobRecord:
    """Deep clone a JobRecord so a caller mutating the result can't
    poison the JobStore's ``_list_cache`` entry.

    P1-12a follow-up (Codex review of 97cc777): the v0 implementation
    used ``dataclasses.replace(record)`` to copy the dataclass shell.
    That fires ``__post_init__`` (which runs ``_copy_optional_dict``
    on each dict field), but ``_copy_optional_dict(value)`` is itself
    a shallow ``dict(value)`` — only the top-level keys get copied;
    a nested dict / list stays aliased. So a caller doing
    ``jobs[0].review_gate['metadata']['x'] = 999`` or
    ``jobs[0].error_summary['details'].append(...)`` would still
    mutate the cached record through the shared inner reference,
    and the next ``list_jobs`` call would surface the stale state
    even though disk hadn't changed.

    ``copy.deepcopy`` walks the entire object graph and clones every
    nested mutable container. The cost on a JobRecord with ~50 scalar
    fields + 3 small dicts is single-digit microseconds — still a
    massive win over re-running ``json.loads + JobRecord.from_dict``
    on a cache hit.
    """
    return copy.deepcopy(record)


class JobStore:
    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir).resolve(strict=False)
        # P1-12a (audit 2026-05-07, P-CRITICAL-1): in-memory cache for
        # ``list_jobs`` to avoid re-parsing every {job_id}.json on every
        # call. Each entry is ``(mtime_ns, JobRecord)``; we re-parse only
        # when the on-disk mtime differs from the cached mtime, which
        # makes the steady-state cost of list_jobs O(N stat calls) rather
        # than O(N json.loads + JobRecord.from_dict).
        #
        # Cross-process invalidation: each gateway worker holds its own
        # cache, but per-file mtime detection still triggers re-parse
        # when another worker mutates the file. No need for shared
        # invalidation infrastructure.
        #
        # Concurrency: ``_list_cache_lock`` protects the dict reference
        # only — parses happen outside the lock so concurrent list_jobs
        # callers don't serialize on the json.loads path.
        self._list_cache: dict[str, tuple[int, JobRecord]] = {}
        self._list_cache_lock = threading.Lock()

    def save_job(self, record: JobRecord, *, fsync: bool = True) -> JobRecord:
        """Persist a JobRecord atomically.

        ``fsync=True`` (default) keeps the strict durability guarantee
        — the bytes hit physical storage before this method returns.
        ``fsync=False`` is the **group commit** mode introduced for
        P1-12b: skip the per-write ``os.fsync`` so the high-frequency
        ``ProcessJobRunner._record_line`` log-line path doesn't spend
        2 fsyncs per stdout line (60-180 MB write amplification on a
        30-min pipeline). The OS page cache still buffers the write;
        a subsequent strict write from the same process (terminal
        status flip, ``finalize_process``) flushes the journal and
        durably-commits the prior buffered writes.

        Crash-window tradeoff: with ``fsync=False`` and a *kernel*
        crash, recent log-line updates may be lost — the next
        pipeline / Job API restart re-derives state from events.jsonl
        + the last fsynced JobRecord. **Status flips and terminal
        writes MUST stay fsync=True**.
        """
        self.root_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._job_path(record.job_id)
        # P1-15b follow-up (Codex review of b1fee3a): take the per-job
        # ``file_lock`` so direct ``save_job`` callers (HTTP threads
        # doing ``require_job → replace → save_job`` without going
        # through ``update_job``) serialize with ``update_job`` holders.
        # Without this, update_job's lock only excluded other
        # update_job callers — a stale-snapshot direct save from
        # JobService.update_display_name (or any other path that hasn't
        # been migrated to update_job yet) could still slip in between
        # update_job's load and its internal save_job, clobbering both
        # sides' fields. The lock is reentrant per-thread, so when
        # update_job calls into save_job from inside its own critical
        # section the re-acquire is free.
        with file_lock(output_path):
            self._write_json_atomic(output_path, record.to_dict(), fsync=fsync)
        return record

    def update_job(
        self,
        job_id: str,
        mutator: Callable[[JobRecord], JobRecord],
        *,
        initial: JobRecord | None = None,
        fsync: bool = True,
    ) -> JobRecord:
        """Atomic load → mutator → save under a per-job ``file_lock``.

        P1-15b (audit 2026-05-07, P0-5 caller-layer follow-up):
        every existing caller of ``save_job`` first did
        ``record = store.require_job(); record = replace(record, ...);
        store.save_job(record)``. P0-5 added file_lock around editing/
        admin/state hot paths but deliberately scoped JobStore out
        because fixing it requires changing the *caller* contract,
        not just the save side. This helper closes that gap: callers
        pass a mutator and we load+save atomically under the same
        per-job lock, so concurrent HTTP threads + pipeline runner
        threads cannot interleave their reads and lose updates.

        The mutator MUST be pure: produce a new ``JobRecord`` from the
        passed-in ``current`` without side effects. Any IO done inside
        the mutator extends the critical section unnecessarily.

        Reentrant: ``services._file_lock.file_lock`` uses an RLock + a
        per-thread depth counter, so nested ``update_job`` calls on the
        same job from the same thread are safe.

        ``initial`` is the fallback record to feed the mutator when the
        on-disk record doesn't exist yet (the typical first-write path
        in ``ProcessJobRunner.start``: caller has the in-memory record
        but it hasn't been persisted yet). Without ``initial`` the
        method raises ``KeyError`` to preserve the original strict
        require_job contract for callers that genuinely expect the
        record to exist.

        ``fsync`` defaults to True; pass False for high-frequency
        log-line updates (P1-12b group-commit mode). See ``save_job``
        for the durability tradeoff.
        """
        path = self._job_path(job_id)
        with file_lock(path):
            current = self.load_job(job_id)
            # Track whether the ``current`` record came from disk
            # (loaded successfully) or fell back to ``initial`` (first
            # write). The skip-noop optimization below MUST NOT fire
            # on first writes — even if the mutator is identity, we
            # need to persist ``initial`` so subsequent require_job
            # calls find the record.
            loaded_from_disk = current is not None
            if current is None:
                if initial is None:
                    raise KeyError(f"Job not found: {job_id}")
                current = initial
            updated = mutator(current)
            # P1-15b follow-up (Codex review of b1fee3a): a buggy
            # mutator that returns a JobRecord with a different job_id
            # would cause us to write to a different file (under the
            # WRONG file_lock — we hold the lock for ``job_id``, not
            # for ``updated.job_id``), silently bypassing the atomicity
            # guarantee. Reject this defensively before save.
            if updated.job_id != job_id:
                raise ValueError(
                    f"update_job mutator changed job_id from {job_id!r} "
                    f"to {updated.job_id!r}. Mutators must not change "
                    f"the job_id; the lock is keyed by the original id."
                )
            # P1-12b (audit 2026-05-07): fast no-op path. If the
            # mutator returned the SAME record (dataclass equality),
            # nothing on disk needs to change — skip the rewrite.
            # ``ProcessJobRunner._record_line`` exploits this: many
            # stdout lines map to the same (current_stage, progress_message)
            # so skipping the rewrite avoids 30 KB × 3000 lines = 90 MB
            # of needless write amplification per pipeline run.
            #
            # Guarded on ``loaded_from_disk``: first-write callers
            # (e.g. copy_as_new passing ``initial=new_record`` with an
            # identity mutator) require the on-disk file to be created
            # even though "current == updated" trivially holds. Without
            # this guard the file is never written and subsequent
            # ``require_job`` calls fail with KeyError.
            if loaded_from_disk and updated == current:
                return current
            self.save_job(updated, fsync=fsync)
            return updated

    def load_job(self, job_id: str) -> JobRecord | None:
        path = self._job_path(job_id)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid job record payload: {path}")
        return JobRecord.from_dict(payload)

    def require_job(self, job_id: str) -> JobRecord:
        record = self.load_job(job_id)
        if record is None:
            raise KeyError(f"Job not found: {job_id}")
        return record

    def append_event(
        self, job_id: str, event: JobEvent, *, fsync: bool = True
    ) -> JobEvent:
        """Append a JobEvent to ``{job_id}.events.jsonl``.

        ``fsync`` default True keeps strict durability for status /
        terminal events. Pass ``fsync=False`` for the high-volume log
        path (``ProcessJobRunner._record_line``) where each pipeline
        stdout line emits a JobEvent — without the flag, that path
        does ~3000 fsyncs per 30-min run, which is the larger half of
        the 6-30s pipeline IO tax measured in the audit. The OS page
        cache still buffers the bytes; a subsequent strict write
        from the same process flushes the journal.
        """
        self.root_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._events_path(job_id)
        with output_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False))
            handle.write("\n")
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        return event

    def load_events(self, job_id: str) -> list[JobEvent]:
        """Read all events for ``job_id`` from ``{job_id}.events.jsonl``.

        Returns parsed events in file order. Malformed lines (bad JSON,
        unknown event_type) are skipped and logged at WARNING so a single
        polluted line never takes down ``/jobs/{id}/logs``.

        Why be tolerant: Gateway writes some event types (``stream.*``,
        ``download.*``) that may be added to ``services.jobs.events``
        AFTER the Job API process started, especially during a deploy
        where ``app`` container code is bind-mounted but the Python
        process isn't restarted. Pre-tolerance behavior was to raise
        ``ValueError("Unsupported event_type")`` from ``JobEvent.from_dict``,
        which bubbled up as a 500 on the logs endpoint and broke admin /
        user visibility into otherwise-healthy jobs. Skipping is the
        documented fail-open pattern (mirrors ``services.web_ui.logs_redactor``
        loader semantics: never let observability tooling kill the
        primary content path).
        """
        path = self._events_path(job_id)
        if not path.exists():
            return []

        events: list[JobEvent] = []
        skipped = 0
        for line_no, raw_line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1,
        ):
            normalized_line = raw_line.strip()
            if not normalized_line:
                continue
            try:
                payload = json.loads(normalized_line)
                if not isinstance(payload, dict):
                    raise ValueError("payload is not a JSON object")
                events.append(JobEvent.from_dict(payload))
            except Exception as exc:
                # Fail-open: log the offending line index + reason but
                # keep returning the events we did parse. Production
                # symptoms of the old strict behavior: a Gateway-written
                # event_type that the Job API process hadn't loaded yet
                # → entire logs page 500.
                skipped += 1
                logger.warning(
                    "load_events: skipping malformed event line %s in %s (%s)",
                    line_no, path.name, exc,
                )
        if skipped:
            logger.info(
                "load_events: skipped %d malformed event line(s) for job=%s",
                skipped, job_id,
            )
        return events

    def list_jobs(self, *, limit: int | None = None, offset: int = 0) -> list[JobRecord]:
        """List all JobRecords sorted by recency.

        P1-12a (audit 2026-05-07, P-CRITICAL-1): each call previously
        ran ``json.loads`` + ``JobRecord.from_dict`` on every
        ``{job_id}.json`` in ``root_dir``. With 1000 jobs that came to
        200-800 ms / call, and the workspace front-end polled multiple
        list endpoints every 4 s — 5 concurrent users could saturate
        the Job API.

        New design: maintain an in-memory cache keyed by ``job_id`` with
        ``(mtime_ns, JobRecord)`` entries. On each call we glob the dir
        and stat() each file (cheap), then re-parse ONLY the files whose
        on-disk mtime differs from the cached mtime. Steady state on a
        stable workload (no writes) hits the cache for every entry and
        does zero JSON parsing.

        Correctness:
          * mtime is read AFTER the glob and BEFORE the parse, so a
            concurrent writer's commit is detected on the next list_jobs.
          * Returns ``_clone_record(cached_record)`` deep copies so
            callers can mutate without poisoning the cache. JobRecord
            is ``@dataclass(slots=True)`` (mutable), with three
            ``dict[str, object]`` fields (``review_gate``,
            ``error_summary``, ``fallback_summary``) that may carry
            nested dicts/lists. ``copy.deepcopy`` is the only safe
            choice — ``dataclasses.replace`` is one-level, and the
            model's ``_copy_optional_dict`` is itself ``dict(value)``
            (shallow), so a nested mutation like
            ``jobs[0].review_gate['metadata']['x'] = 999`` would
            otherwise leak into the cached entry.
          * Cross-process: each gateway worker has its own cache;
            mtime drift naturally invalidates entries when another
            worker writes. No shared cache infra needed.
          * Files that disappear between glob and stat are skipped
            silently (matches pre-cache "file disappeared" semantics).
        """
        if not self.root_dir.exists():
            return []

        # Snapshot the current cache under the lock so concurrent
        # list_jobs callers don't race on the dict reference. Parses
        # happen OUTSIDE this lock — only the snapshot + final swap
        # are inside.
        with self._list_cache_lock:
            cache_snapshot = dict(self._list_cache)

        new_cache: dict[str, tuple[int, JobRecord]] = {}
        records: list[JobRecord] = []

        # 2026-05-11: defensive list — earlier we used ``*.json`` and
        # trusted from_dict to fail-loud on stray files; that turned
        # out to crash the WHOLE list (single bad file → 500 →
        # workspace empty for every user). Operator-left sidecar files
        # (``_patch.json``, ``_correct-names.json``) shouldn't be able
        # to nuke the list. Two-line defense:
        #   1. Skip files whose name starts with ``_`` — that's the
        #      convention for operator-left sidecars and lock files.
        #      Real job_id values never start with underscore.
        #   2. Per-file try/except logs + skips, never aborts the list.
        for path in self.root_dir.glob("*.json"):
            if path.name.startswith("_"):
                continue
            try:
                mtime_ns = path.stat().st_mtime_ns
            except (FileNotFoundError, OSError):
                # File vanished between glob and stat (e.g. concurrent
                # delete_job). Skip — matches pre-cache behavior.
                continue

            job_id = path.stem
            cached = cache_snapshot.get(job_id)
            if cached is not None and cached[0] == mtime_ns:
                # Cache hit — bypass json.loads + from_dict entirely.
                # Carry the cached entry forward into new_cache so a
                # subsequent call still hits.
                new_cache[job_id] = cached
                # Deep-copy so nested-dict mutations from callers
                # don't poison the cache (see _clone_record docstring).
                records.append(_clone_record(cached[1]))
                continue

            # Cache miss path. Take the per-job file_lock so we don't
            # race a concurrent ``save_job`` mid-rename — particularly
            # on Windows where ``os.replace`` fails with PermissionError
            # if the destination is held open elsewhere. The lock is
            # reentrant so this is safe even when a JobStore caller
            # already holds the lock from update_job's outer
            # acquisition; the cache hit path takes no lock so common-
            # case readers don't queue behind writers.
            try:
                with file_lock(path):
                    payload_text = path.read_text(encoding="utf-8")
            except (FileNotFoundError, OSError):
                # File vanished after the stat — skip.
                continue
            # Per-file fail-safe: malformed JSON, non-dict payload, or
            # from_dict raising on missing/invalid fields must NOT
            # cascade into a list-wide 500. Log loudly, skip the file,
            # let the rest of the list go through. See the 2026-05-11
            # incident where a stray operator file in this directory
            # blanked the workspace for every user.
            try:
                payload = json.loads(payload_text)
                if not isinstance(payload, dict):
                    raise ValueError(f"payload is not a dict: type={type(payload).__name__}")
                record = JobRecord.from_dict(payload)
            except Exception as exc:  # noqa: BLE001 — last-resort guard
                logger.warning(
                    "JobStore.list_jobs: skipping unparseable record at %s (%s)",
                    path, exc,
                )
                continue
            new_cache[job_id] = (mtime_ns, record)
            # Deep-copy on the cache-miss path too — same reasoning as
            # the cache-hit branch above (see _clone_record docstring).
            records.append(_clone_record(record))

        # Atomically swap in the new cache. Files that no longer exist
        # in the glob are NOT in new_cache and are dropped — natural
        # GC for deleted jobs without an explicit invalidation hook.
        with self._list_cache_lock:
            self._list_cache = new_cache

        records.sort(
            key=lambda item: (item.updated_at, item.created_at, item.job_id),
            reverse=True,
        )
        normalized_offset = max(int(offset), 0)
        if normalized_offset:
            records = records[normalized_offset:]
        if limit is None:
            return records
        return records[: max(limit, 0)]

    def delete_job(self, job_id: str) -> bool:
        """Delete a job record and its events file. Returns True if the job existed."""
        job_path = self._job_path(job_id)
        existed = job_path.exists()
        job_path.unlink(missing_ok=True)
        self._events_path(job_id).unlink(missing_ok=True)
        return existed

    def _job_path(self, job_id: str) -> Path:
        normalized_job_id = str(job_id).strip()
        if not normalized_job_id:
            raise ValueError("job_id is required")
        return self.root_dir / f"{normalized_job_id}.json"

    def _events_path(self, job_id: str) -> Path:
        normalized_job_id = str(job_id).strip()
        if not normalized_job_id:
            raise ValueError("job_id is required")
        return self.root_dir / f"{normalized_job_id}.events.jsonl"

    @staticmethod
    def _write_json_atomic(
        output_path: Path,
        payload: dict[str, object],
        *,
        fsync: bool = True,
    ) -> None:
        """Atomic temp + rename JSON write.

        ``fsync=True`` (default) flushes the temp file's bytes to disk
        before the rename, so a crash mid-rename leaves either the old
        or the new file content but never corrupted partial bytes.

        ``fsync=False`` (P1-12b group-commit mode) skips that fsync.
        The temp + rename still preserves crash atomicity at the
        filesystem-rename level (POSIX rename is atomic on the same
        directory, journaling FS like ext4 / NTFS guarantee the
        rename either lands or doesn't). What we lose is durability
        of the bytes themselves: a kernel-level crash between the
        rename and the next FS journal flush could leave the renamed
        path pointing at zeroed blocks. For the per-line log-update
        use case this is acceptable — the next pipeline / API restart
        re-derives state from the persisted events.jsonl tail.
        """
        temp_path: Path | None = None
        try:
            serialized_payload = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output_path.parent,
                prefix=f"{output_path.stem}_",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_file.write(serialized_payload)
                temp_file.flush()
                if fsync:
                    os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            os.replace(temp_path, output_path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)
