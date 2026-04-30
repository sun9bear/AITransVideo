from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from services.jobs.events import JobEvent
from services.jobs.models import JobRecord


class JobStore:
    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir).resolve(strict=False)

    def save_job(self, record: JobRecord) -> JobRecord:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._job_path(record.job_id)
        self._write_json_atomic(output_path, record.to_dict())
        return record

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
