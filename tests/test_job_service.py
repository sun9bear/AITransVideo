from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.job_test_helpers import FakePopenFactory, set_review_stage, wait_for, write_process_project
from services.jobs import ProcessJobRunner
from services.jobs.models import (
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_WAITING_FOR_REVIEW,
    JobRecord,
)
from services.jobs.service import JobConflictError, JobService, UnsupportedJobRequestError
from services.jobs.store import JobStore


def _build_service(tmp_path: Path, *, plans: list[dict[str, object]]) -> tuple[JobService, FakePopenFactory]:
    popen_factory = FakePopenFactory(plans)
    store = JobStore(tmp_path / "jobs")
    runner = ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable="python",
        popen_factory=popen_factory,
        run_timeout_seconds=5,
    )
    return JobService(store=store, runner=runner), popen_factory


def _wait_for_job_status(service: JobService, job_id: str, expected_status: str):
    wait_for(lambda: service.require_job(job_id).status == expected_status)
    return service.require_job(job_id)


# -------------------------------------------------------------------
# 2026-04-20 root fix: submit_job pre-fills JobRecord.project_dir with
# an absolute path derived from build_workspace_dir(user_id, job_id).
#
# The previous design bootstrapped project_dir from pipeline stdout
# via regex, which was vulnerable to noise like yt-dlp's `KiB/s` unit
# matching as "/s". Poisoning JobRecord.project_dir="/s" on first
# download progress tick → write-once identity locked → all downstream
# endpoints break with "Project directory does not exist: /s".
#
# The fix: when user_id is known (99% of gateway traffic), compute the
# absolute project_dir at submit time directly. Stdout regex parsing
# then short-circuits via dad5172's "current is None" guard — regex
# machinery becomes pure legacy fallback for user_id=None CLI direct
# submits.
# -------------------------------------------------------------------


def test_submit_job_with_user_id_pre_fills_absolute_project_dir(
    tmp_path: Path,
) -> None:
    """Gateway-style submit passes user_id → project_dir is filled
    synchronously with the absolute path, no waiting for pipeline logs."""
    service, _ = _build_service(
        tmp_path,
        plans=[{"lines": [], "returncode": 0}],  # irrelevant — we never run
    )

    created = service.submit_job(
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=xxx",
        user_id="7",
    )

    assert created.project_dir, (
        "submit_job with user_id MUST set project_dir synchronously, "
        "not leave it None for stdout bootstrap"
    )
    expected_abs = str((tmp_path / "projects" / "7" / created.job_id).resolve(strict=False))
    assert created.project_dir == expected_abs, (
        f"project_dir should be the absolute form of workspace_dir; "
        f"expected {expected_abs!r}, got {created.project_dir!r}"
    )


def test_submit_job_without_user_id_leaves_project_dir_for_bootstrap(
    tmp_path: Path,
) -> None:
    """Legacy CLI direct submits (no user_id) keep the old None+stdout
    bootstrap path so they're not regressed."""
    service, _ = _build_service(
        tmp_path,
        plans=[{"lines": [], "returncode": 0}],
    )
    created = service.submit_job(
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=xxx",
        # user_id omitted
    )
    assert created.project_dir is None
    assert created.workspace_dir is None


def test_submit_job_persists_explicit_expires_at(tmp_path: Path) -> None:
    """Gateway supplies the retention deadline; Job API must store it so
    post-edit updated_at churn cannot extend cleanup/display TTL."""
    service, _ = _build_service(
        tmp_path,
        plans=[{"lines": [], "returncode": 0}],
    )
    expires_at = "2026-04-25T00:00:00+00:00"

    created = service.submit_job(
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=ttl",
        user_id="7",
        expires_at=expires_at,
    )

    assert created.expires_at == expires_at
    assert service.require_job(created.job_id).expires_at == expires_at


def test_submit_job_with_user_id_is_immune_to_yt_dlp_progress_lines(
    tmp_path: Path,
) -> None:
    """Regression: even if pipeline stdout spews a noisy KiB/s line first
    (previously poisoned project_dir to `/s`), the pre-filled absolute
    path must survive unchanged through the run."""
    # Write the project fixture so _finalize_process finds artifacts.
    project_dir_on_disk = tmp_path / "projects" / "9" / "job_placeholder_will_be_replaced"
    # The actual job_id is generated inside submit_job; we can't know it
    # ahead of time. Instead, we let the fixture write a generic one and
    # rely on the fact that the runner doesn't actually move files —
    # we just care that project_dir on JobRecord is untouched by regex.
    service, popen_factory = _build_service(
        tmp_path,
        plans=[
            {
                # Mimic the real yt-dlp progress that poisoned /s in prod
                "lines": [
                    "[download]   0.0% of   30.40MiB at   55.88KiB/s ETA 09:17",
                    "[download]  100.0% of   30.40MiB at   1.2MiB/s ETA 00:00",
                ],
                "returncode": 1,  # fail early — we only care about project_dir mutation
            }
        ],
    )

    created = service.submit_job(
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=xxx",
        user_id="9",
    )
    pre_fill = created.project_dir
    assert pre_fill, "pre-fill should have put an absolute path here"

    # Let the (short) run complete
    final = _wait_for_job_status(service, created.job_id, JOB_STATUS_FAILED)
    assert final.project_dir == pre_fill, (
        f"KiB/s regression: project_dir was mutated from {pre_fill!r} "
        f"to {final.project_dir!r} during the run"
    )
    assert "/s" not in (final.project_dir or ""), (
        f"Never again: {final.project_dir!r} contains '/s' leaked from "
        "yt-dlp progress line"
    )


def test_job_service_submit_runs_lifecycle_to_success_and_backfills_manifest(tmp_path: Path) -> None:
    youtube_url = "https://youtube.example/watch?v=job-success"
    project_dir = write_process_project(
        tmp_path,
        project_name="job_success_project",
        youtube_url=youtube_url,
        fallback_summary={"tts": {"applied": False}},
    )
    service, popen_factory = _build_service(
        tmp_path,
        plans=[
            {
                "lines": [
                    "[S0] Downloading source...",
                    "[S1] Understanding media...",
                    "[S3] Translating text...",
                    f"[S6] Done {project_dir / 'output'}",
                ],
                "returncode": 0,
            }
        ],
    )

    created = service.submit_job(source_type="youtube_url", source_ref=youtube_url)
    completed = _wait_for_job_status(service, created.job_id, JOB_STATUS_SUCCEEDED)
    events = service.read_logs(created.job_id)

    assert popen_factory.calls[0]["command"][:4] == ["python", "-u", str(Path(__file__).resolve().parents[1] / "main.py"), "process"]
    assert completed.current_stage == "completed"
    assert completed.project_dir == str(project_dir.resolve(strict=False))
    assert completed.manifest_path == str((project_dir / "manifest.json").resolve(strict=False))
    assert completed.fallback_summary == {"tts": {"applied": False}}
    assert any(event.stage == "media_understanding" for event in events)
    assert any(event.stage == "translation_review" for event in events)
    assert any(event.stage == "legacy_process_output" for event in events)


def test_job_service_submit_propagates_speakers_and_voice_overrides_to_runner(tmp_path: Path) -> None:
    youtube_url = "https://youtube.example/watch?v=job-voice-overrides"
    project_dir = write_process_project(
        tmp_path,
        project_name="job_voice_override_project",
        youtube_url=youtube_url,
    )
    service, popen_factory = _build_service(
        tmp_path,
        plans=[
            {
                "lines": [
                    "[S0] Downloading source...",
                    f"[S6] Done {project_dir / 'output'}",
                ],
                "returncode": 0,
            }
        ],
    )

    created = service.submit_job(
        source_type="youtube_url",
        source_ref=youtube_url,
        speakers="2",
        voice_a="voice-speaker-a",
        voice_b="voice-speaker-b",
    )
    completed = _wait_for_job_status(service, created.job_id, JOB_STATUS_SUCCEEDED)
    command = popen_factory.calls[0]["command"]

    assert completed.speakers == "2"
    assert completed.voice_a == "voice-speaker-a"
    assert completed.voice_b == "voice-speaker-b"
    assert "--speakers" in command
    assert command[command.index("--speakers") + 1] == "2"
    assert "--voice-a" in command
    assert command[command.index("--voice-a") + 1] == "voice-speaker-a"
    assert "--voice-b" in command
    assert command[command.index("--voice-b") + 1] == "voice-speaker-b"


def test_job_service_records_failed_stage_summary_from_project_state(tmp_path: Path) -> None:
    youtube_url = "https://youtube.example/watch?v=job-failed"
    write_process_project(
        tmp_path,
        project_name="job_failed_project",
        youtube_url=youtube_url,
        failed_stage_name="translation",
        failed_stage_error="translation provider failed",
        failed_stage_error_type="provider_error",
    )
    service, _popen_factory = _build_service(
        tmp_path,
        plans=[
            {
                "lines": [
                    "[S0] Downloading source...",
                    "[S3] Translating text...",
                ],
                "returncode": 1,
            }
        ],
    )

    created = service.submit_job(source_type="youtube_url", source_ref=youtube_url)
    failed = _wait_for_job_status(service, created.job_id, JOB_STATUS_FAILED)

    assert failed.current_stage == "failed"
    assert failed.error_summary == {
        "stage": "translation_review",
        "error_type": "provider_error",
        "message": "translation provider failed",
    }


def test_job_service_enters_waiting_for_review_from_web_review_marker(tmp_path: Path) -> None:
    youtube_url = "https://youtube.example/watch?v=job-review"
    project_dir = write_process_project(
        tmp_path,
        project_name="job_review_project",
        youtube_url=youtube_url,
    )
    escaped_project_dir_text = str(project_dir.resolve(strict=False)).replace("\\", "\\\\")
    service, _popen_factory = _build_service(
        tmp_path,
        plans=[
            {
                "lines": [
                    "[S2] Inspecting voices...",
                    (
                        '[WEB_REVIEW] {"stage":"voice_review","tab":"voice-library",'
                        f'"project_dir":"{escaped_project_dir_text}",'
                        '"message":"voice review required before continue"}'
                    ),
                ],
                "returncode": 0,
            }
        ],
    )

    created = service.submit_job(source_type="youtube_url", source_ref=youtube_url)
    waiting = _wait_for_job_status(service, created.job_id, JOB_STATUS_WAITING_FOR_REVIEW)

    assert waiting.current_stage == "voice_review"
    assert waiting.project_dir == str(project_dir.resolve(strict=False))
    assert waiting.review_gate == {
        "stage": "voice_review",
        "message": "voice review required before continue",
    }


def test_job_service_continue_requires_authoritative_review_approval_and_resumes_same_job(tmp_path: Path) -> None:
    youtube_url = "https://youtube.example/watch?v=job-continue"
    project_dir = write_process_project(
        tmp_path,
        project_name="job_continue_project",
        youtube_url=youtube_url,
    )
    project_dir_text = str(project_dir.resolve(strict=False))
    escaped_project_dir_text = project_dir_text.replace("\\", "\\\\")
    service, popen_factory = _build_service(
        tmp_path,
        plans=[
            {
                "lines": [
                    "[S2] Inspecting voices...",
                    (
                        '[WEB_REVIEW] {"stage":"voice_review","tab":"voice-library",'
                        f'"project_dir":"{escaped_project_dir_text}",'
                        '"message":"voice review required before continue"}'
                    ),
                ],
                "returncode": 0,
            },
            {
                "lines": [
                    "[S3] Applied approved translation review snapshot.",
                    f"[S6] Done {project_dir / 'output'}",
                ],
                "returncode": 0,
            },
        ],
    )

    created = service.submit_job(source_type="youtube_url", source_ref=youtube_url)
    waiting = _wait_for_job_status(service, created.job_id, JOB_STATUS_WAITING_FOR_REVIEW)

    with pytest.raises(JobConflictError, match="not approved"):
        service.continue_job(waiting.job_id)

    set_review_stage(
        project_dir,
        stage_name="voice_review",
        status="approved",
        payload={"reason": "sample_too_short"},
        activate=False,
    )

    continued = service.continue_job(waiting.job_id)
    completed = _wait_for_job_status(service, continued.job_id, JOB_STATUS_SUCCEEDED)

    assert completed.job_id == created.job_id
    assert "--project-dir" in popen_factory.calls[1]["command"]
    assert project_dir_text in popen_factory.calls[1]["command"]
    assert completed.manifest_path == str((project_dir / "manifest.json").resolve(strict=False))


def test_job_service_continue_treats_invalid_review_state_as_not_approved(tmp_path: Path) -> None:
    youtube_url = "https://youtube.example/watch?v=job-invalid-review-state"
    project_dir = write_process_project(
        tmp_path,
        project_name="job_invalid_review_state",
        youtube_url=youtube_url,
    )
    escaped_project_dir_text = str(project_dir.resolve(strict=False)).replace("\\", "\\\\")
    service, _popen_factory = _build_service(
        tmp_path,
        plans=[
            {
                "lines": [
                    (
                        '[WEB_REVIEW] {"stage":"voice_review","tab":"voice-library",'
                        f'"project_dir":"{escaped_project_dir_text}",'
                        '"message":"voice review required before continue"}'
                    ),
                ],
                "returncode": 0,
            }
        ],
    )

    created = service.submit_job(source_type="youtube_url", source_ref=youtube_url)
    waiting = _wait_for_job_status(service, created.job_id, JOB_STATUS_WAITING_FOR_REVIEW)
    (project_dir / "review_state.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(JobConflictError, match="not approved"):
        service.continue_job(waiting.job_id)


def test_job_service_allows_second_submit_while_first_job_is_active(tmp_path: Path) -> None:
    """Concurrency control is now at gateway layer; Job API allows parallel jobs."""
    youtube_url = "https://youtube.example/watch?v=job-concurrent"
    project_dir = write_process_project(
        tmp_path,
        project_name="job_concurrent_first",
        youtube_url=youtube_url,
    )
    second_url = "https://youtube.example/watch?v=second-job"
    second_project_dir = write_process_project(
        tmp_path,
        project_name="job_concurrent_second",
        youtube_url=second_url,
    )
    escaped_project_dir_text = str(project_dir.resolve(strict=False)).replace("\\", "\\\\")
    service, _popen_factory = _build_service(
        tmp_path,
        plans=[
            {
                "lines": [
                    (
                        '[WEB_REVIEW] {"stage":"voice_review","tab":"voice-library",'
                        f'"project_dir":"{escaped_project_dir_text}",'
                        '"message":"voice review required before continue"}'
                    ),
                ],
                "returncode": 0,
            },
            {
                "lines": [
                    f"[S6] Done {second_project_dir / 'output'}",
                ],
                "returncode": 0,
            },
        ],
    )

    first = service.submit_job(source_type="youtube_url", source_ref=youtube_url)
    _wait_for_job_status(service, first.job_id, JOB_STATUS_WAITING_FOR_REVIEW)

    # Second submit should succeed — no global single-active gate
    second = service.submit_job(source_type="youtube_url", source_ref=second_url)
    _wait_for_job_status(service, second.job_id, JOB_STATUS_SUCCEEDED)
    assert first.job_id != second.job_id


def test_job_service_reaps_stale_running_job_without_live_process_before_new_submit(
    tmp_path: Path,
) -> None:
    stale_job_id = "job-stale-running"
    stale_record = JobRecord(
        job_id=stale_job_id,
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=stale-job",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status=JOB_STATUS_RUNNING,
        current_stage="media_understanding",
        progress_message="Processing stale job...",
        created_at="2026-03-19T03:23:20Z",
        updated_at="2026-03-19T03:23:22Z",
        started_at="2026-03-19T03:23:20Z",
    )
    project_dir = write_process_project(
        tmp_path,
        project_name="job_after_stale_cleanup",
        youtube_url="https://youtube.example/watch?v=job-after-stale",
    )
    service, _popen_factory = _build_service(
        tmp_path,
        plans=[
            {
                "lines": [
                    "[S0] Downloading source...",
                    f"[S6] Done {project_dir / 'output'}",
                ],
                "returncode": 0,
            }
        ],
    )
    service.store.save_job(stale_record)

    created = service.submit_job(
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=job-after-stale",
    )
    completed = _wait_for_job_status(service, created.job_id, JOB_STATUS_SUCCEEDED)
    recovered = service.require_job(stale_job_id)
    recovered_events = service.read_logs(stale_job_id)

    assert completed.job_id != stale_job_id
    assert recovered.status == JOB_STATUS_FAILED
    assert recovered.current_stage == "failed"
    assert recovered.error_summary == {
        "stage": "failed",
        "error_type": "stale_active_job",
        "message": "Recovered stale active job without a live worker process.",
    }
    assert recovered_events[-1].status == JOB_STATUS_FAILED
    assert recovered_events[-1].message == "Recovered stale active job without a live worker process."


def test_mark_stale_active_job_skips_event_when_runner_resumed(
    tmp_path: Path,
) -> None:
    """P1-15b follow-up² (Codex review of a687ae6, P2):
    _mark_stale_active_job_failed's mutator can decide to leave the
    record alone when runner.is_process_active returns True under the
    lock (the runner came back to life between scan and lock acquire).
    In that case we MUST NOT append a stale_active_job error event,
    otherwise the log gets false "Recovered stale active job" noise
    for jobs that explicitly weren't marked stale.

    Setup: stale-looking record on disk + fake runner whose
    is_process_active returns True. Call _mark_stale_active_job_failed
    directly. Assert: record is unchanged AND no error event was
    appended.
    """
    record = JobRecord(
        job_id="job-runner-revived",
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=revived",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status=JOB_STATUS_RUNNING,
        current_stage="alignment",
        progress_message="Aligning segments...",
        created_at="2026-05-07T00:00:00Z",
        updated_at="2026-05-07T00:00:01Z",
        started_at="2026-05-07T00:00:00Z",
    )
    service, _ = _build_service(tmp_path, plans=[])
    service.store.save_job(record)

    # Fake runner: claim the process is alive. The mutator should
    # observe this and return current unchanged.
    class _AliveRunner:
        def is_process_active(self, job_id: str) -> bool:
            return True

    service.runner = _AliveRunner()  # type: ignore[assignment]

    pre_events = service.read_logs(record.job_id)
    pre_count = len(pre_events)
    result = service._mark_stale_active_job_failed(record)

    # Record unchanged.
    assert result.status == JOB_STATUS_RUNNING
    assert result.current_stage == "alignment"
    assert result.error_summary in (None, {})

    # No new event was appended.
    post_events = service.read_logs(record.job_id)
    assert len(post_events) == pre_count, (
        f"P1-15b follow-up² regression: a stale_active_job error event "
        f"was emitted even though the mutator decided not to mark the "
        f"record failed (runner came back to life). Event delta: "
        f"{len(post_events) - pre_count}"
    )


def test_mark_stale_active_job_skips_event_when_status_no_longer_active(
    tmp_path: Path,
) -> None:
    """Mirror of the above for the OTHER no-op branch: the record's
    status is no longer worker-active under the lock (it transitioned
    to e.g. SUCCEEDED between the scanner's read and our acquire).
    Mutator returns current; no event must be emitted."""
    record = JobRecord(
        job_id="job-finished-mid-scan",
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=finished",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status=JOB_STATUS_RUNNING,  # caller's stale snapshot
        current_stage="media_understanding",
        progress_message="Stale snapshot from scanner",
        created_at="2026-05-07T00:00:00Z",
        updated_at="2026-05-07T00:00:01Z",
        started_at="2026-05-07T00:00:00Z",
    )
    service, _ = _build_service(tmp_path, plans=[])

    # Disk reflects the FRESH state: job already succeeded.
    fresh = replace(record, status=JOB_STATUS_SUCCEEDED, current_stage="legacy_process_output")
    service.store.save_job(fresh)

    # Runner doesn't matter — the status check fires first.
    class _DeadRunner:
        def is_process_active(self, job_id: str) -> bool:
            return False

    service.runner = _DeadRunner()  # type: ignore[assignment]

    pre_events = service.read_logs(record.job_id)
    pre_count = len(pre_events)
    result = service._mark_stale_active_job_failed(record)

    # Record kept the FRESH state (succeeded) — not flipped to failed.
    assert result.status == JOB_STATUS_SUCCEEDED
    assert result.current_stage == "legacy_process_output"

    # No new event was appended.
    post_events = service.read_logs(record.job_id)
    assert len(post_events) == pre_count


def test_job_service_accepts_local_audio_source_type(tmp_path: Path) -> None:
    """local_audio is a supported source type and should be accepted by submit_job."""
    project_dir = write_process_project(
        tmp_path,
        project_name="local_audio_project",
        youtube_url="D:/input.wav",
    )
    service, _popen_factory = _build_service(
        tmp_path,
        plans=[
            {
                "lines": [f"[S6] Done {project_dir / 'output'}"],
                "returncode": 0,
            }
        ],
    )

    created = service.submit_job(source_type="local_audio", source_ref="D:/input.wav")
    completed = _wait_for_job_status(service, created.job_id, JOB_STATUS_SUCCEEDED)

    assert completed.source_type == "local_audio"
    assert completed.source_ref == "D:/input.wav"


def test_job_service_rejects_unsupported_speakers_value(tmp_path: Path) -> None:
    service, _popen_factory = _build_service(tmp_path, plans=[])

    with pytest.raises(UnsupportedJobRequestError, match="unsupported speakers"):
        service.submit_job(
            source_type="youtube_url",
            source_ref="https://youtube.example/watch?v=job-bad-speakers",
            speakers="3",
        )
