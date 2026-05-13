from __future__ import annotations

import json

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


def test_job_store_load_events_tolerates_unknown_event_type(tmp_path) -> None:
    """CodeX P1 follow-up (plan 2026-05-07 §11, 2026-05-12):
    ``load_events`` must NOT raise when an event_type isn't in the
    process's current ``SUPPORTED_EVENT_TYPES``. Production hits this
    when Gateway writes a newly-added event type (e.g. ``stream.*``)
    before the Job API Python process has reloaded the events module.
    Old behaviour 500'd ``/jobs/{id}/logs``; new behaviour returns
    the parseable events and skips the unknown ones.
    """
    store = JobStore(tmp_path / "jobs")
    record = _build_job_record(job_id="job_tolerant")
    store.save_job(record)

    # Hand-craft a JSONL file with: one valid event, one unknown
    # event_type (mimics a Gateway-written stream.* before the Job
    # API process is aware of it), one malformed line.
    events_path = store._events_path("job_tolerant")
    events_path.write_text(
        "\n".join([
            json.dumps({
                "job_id": "job_tolerant",
                "event_type": "status",
                "created_at": "2026-05-12T10:00:00+00:00",
                "level": "info",
                "message": "first",
                "stage": None, "status": None, "payload": {},
            }),
            json.dumps({
                "job_id": "job_tolerant",
                # Deliberately a not-yet-in-SUPPORTED-EVENT-TYPES value so
                # the test simulates the cross-version drift CodeX P1
                # describes. Pick a clearly-future label so we don't
                # accidentally trip the test if a real value with that
                # name ever lands.
                "event_type": "future.hypothetical.event_type",
                "created_at": "2026-05-12T10:00:01+00:00",
                "level": "info",
                "message": "unknown to old process",
                "stage": None, "status": None, "payload": {},
            }),
            "not-json-at-all",
            json.dumps({
                "job_id": "job_tolerant",
                "event_type": "log",
                "created_at": "2026-05-12T10:00:02+00:00",
                "level": "info",
                "message": "last",
                "stage": None, "status": None, "payload": {},
            }),
        ]) + "\n",
        encoding="utf-8",
    )

    # The function must not raise on either the unknown event_type
    # or the malformed JSON. Both bad lines should be skipped.
    events = store.load_events("job_tolerant")
    messages = [e.message for e in events]
    # We get the 2 well-formed events with known types; the unknown
    # event_type line and the bad-JSON line are dropped.
    # NOTE: if a future change adds stream.* to the process's
    # SUPPORTED_EVENT_TYPES, this assertion's count goes up by one;
    # update the expected list rather than reverting the tolerance.
    assert messages == ["first", "last"], (
        f"load_events tolerance broken: got messages={messages}; "
        f"expected only the 2 parseable, known-type events."
    )


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


def test_job_store_lists_jobs_with_limit_and_offset(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs")
    for index in range(5):
        store.save_job(
            _build_job_record(
                job_id=f"job_{index}",
                updated_at=f"2026-03-18T00:00:0{index}+00:00",
            ),
        )

    jobs = store.list_jobs(limit=2, offset=1)

    assert [job.job_id for job in jobs] == ["job_3", "job_2"]
