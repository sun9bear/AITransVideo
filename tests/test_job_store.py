from __future__ import annotations

from services.jobs.events import EVENT_TYPE_LOG, EVENT_TYPE_STATUS, JobEvent
from services.jobs.models import (
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_TYPE_LOCALIZE_VIDEO,
    OUTPUT_TARGET_EDITOR,
    SOURCE_TYPE_YOUTUBE_URL,
    JobRecord,
)
from services.jobs.store import JobStore


def _build_job_record(*, job_id: str, status: str = JOB_STATUS_QUEUED, updated_at: str = "2026-03-18T00:00:00+00:00") -> JobRecord:
    return JobRecord(
        job_id=job_id,
        job_type=JOB_TYPE_LOCALIZE_VIDEO,
        source_type=SOURCE_TYPE_YOUTUBE_URL,
        source_ref="https://youtube.example/watch?v=test",
        output_target=OUTPUT_TARGET_EDITOR,
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status=status,
        current_stage=None,
        progress_message="queued",
        created_at="2026-03-18T00:00:00+00:00",
        updated_at=updated_at,
    )


def test_job_store_round_trips_job_record(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs")
    record = _build_job_record(job_id="job_store_round_trip")

    store.save_job(record)
    loaded = store.require_job(record.job_id)

    assert loaded.job_id == record.job_id
    assert loaded.source_type == SOURCE_TYPE_YOUTUBE_URL
    assert loaded.output_target == OUTPUT_TARGET_EDITOR
    assert loaded.status == JOB_STATUS_QUEUED


def test_job_store_preserves_none_optional_fields_without_empty_dict_drift(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs")
    record = _build_job_record(job_id="job_store_none_fields")

    store.save_job(record)
    loaded = store.require_job(record.job_id)

    assert loaded.review_gate is None
    assert loaded.error_summary is None
    assert loaded.fallback_summary is None


def test_job_store_appends_and_reads_events(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs")
    record = _build_job_record(job_id="job_store_events")
    store.save_job(record)

    store.append_event(
        record.job_id,
        JobEvent(
            job_id=record.job_id,
            event_type=EVENT_TYPE_STATUS,
            created_at="2026-03-18T00:00:01+00:00",
            status=JOB_STATUS_RUNNING,
            message="running",
        ),
    )
    store.append_event(
        record.job_id,
        JobEvent(
            job_id=record.job_id,
            event_type=EVENT_TYPE_LOG,
            created_at="2026-03-18T00:00:02+00:00",
            stage="ingestion",
            status=JOB_STATUS_RUNNING,
            message="[S0] Downloading",
        ),
    )

    events = store.load_events(record.job_id)

    assert len(events) == 2
    assert events[0].event_type == EVENT_TYPE_STATUS
    assert events[1].message == "[S0] Downloading"


def test_job_store_lists_jobs_by_latest_updated_at(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs")
    older = _build_job_record(
        job_id="job_old",
        updated_at="2026-03-18T00:00:00+00:00",
    )
    newer = _build_job_record(
        job_id="job_new",
        updated_at="2026-03-18T00:00:05+00:00",
    )

    store.save_job(older)
    store.save_job(newer)

    jobs = store.list_jobs()

    assert [job.job_id for job in jobs] == ["job_new", "job_old"]
