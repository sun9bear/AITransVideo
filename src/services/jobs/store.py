from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Callable

from services._file_lock import file_lock
from services.jobs.events import JobEvent
from services.jobs.models import JobRecord


class JobStore:
    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir).resolve(strict=False)

    def save_job(self, record: JobRecord) -> JobRecord:
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
            self._write_json_atomic(output_path, record.to_dict())
        return record

    def update_job(
        self,
        job_id: str,
        mutator: Callable[[JobRecord], JobRecord],
        *,
        initial: JobRecord | None = None,
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
        """
        path = self._job_path(job_id)
        with file_lock(path):
            current = self.load_job(job_id)
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
            self.save_job(updated)
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

    def append_event(self, job_id: str, event: JobEvent) -> JobEvent:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._events_path(job_id)
        with output_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def load_events(self, job_id: str) -> list[JobEvent]:
        path = self._events_path(job_id)
        if not path.exists():
            return []

        events: list[JobEvent] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            normalized_line = raw_line.strip()
            if not normalized_line:
                continue
            payload = json.loads(normalized_line)
            if not isinstance(payload, dict):
                raise ValueError(f"Invalid job event payload: {path}")
            events.append(JobEvent.from_dict(payload))
        return events

    def list_jobs(self, *, limit: int | None = None, offset: int = 0) -> list[JobRecord]:
        if not self.root_dir.exists():
            return []

        jobs: list[JobRecord] = []
        for path in self.root_dir.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"Invalid job record payload: {path}")
            jobs.append(JobRecord.from_dict(payload))

        jobs.sort(key=lambda item: (item.updated_at, item.created_at, item.job_id), reverse=True)
        normalized_offset = max(int(offset), 0)
        if normalized_offset:
            jobs = jobs[normalized_offset:]
        if limit is None:
            return jobs
        return jobs[: max(limit, 0)]

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
    def _write_json_atomic(output_path: Path, payload: dict[str, object]) -> None:
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
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            os.replace(temp_path, output_path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)
