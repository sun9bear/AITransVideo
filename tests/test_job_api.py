from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests.job_test_helpers import FakePopenFactory, set_review_stage, wait_for, write_process_project
from services.jobs.api import build_job_api_server
from services.jobs.process_runner import ProcessJobRunner
from services.jobs.service import JobService
from services.jobs.store import JobStore


def _build_server(tmp_path: Path, *, plans: list[dict[str, object]]):
    popen_factory = FakePopenFactory(plans)
    store = JobStore(tmp_path / "jobs")
    runner = ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable="python",
        popen_factory=popen_factory,
        run_timeout_seconds=5,
    )
    service = JobService(store=store, runner=runner)
    server = build_job_api_server(service=service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return service, server, thread


def _request_json(method: str, url: str, payload: dict[str, object] | None = None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, method=method, data=body)
    request.add_header("Content-Type", "application/json; charset=utf-8")
    with urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_job_api_supports_submit_list_get_and_logs(tmp_path: Path) -> None:
    youtube_url = "https://youtube.example/watch?v=api-success"
    project_dir = write_process_project(
        tmp_path,
        project_name="job_api_success",
        youtube_url=youtube_url,
    )
    service, server, thread = _build_server(
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
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        status, created = _request_json(
            "POST",
            f"{base_url}/jobs",
            {
                "job_type": "localize_video",
                "source": {
                    "type": "youtube_url",
                    "value": youtube_url,
                },
                "output_target": "editor",
                "speakers": "2",
                "voice_a": "api-voice-a",
                "voice_b": "api-voice-b",
            },
        )
        job_id = created["job_id"]
        wait_for(lambda: service.require_job(job_id).status == "succeeded")

        list_status, jobs_payload = _request_json("GET", f"{base_url}/jobs")
        get_status, job_payload = _request_json("GET", f"{base_url}/jobs/{job_id}")
        logs_status, logs_payload = _request_json("GET", f"{base_url}/jobs/{job_id}/logs")

        assert status == HTTPStatus.ACCEPTED
        assert list_status == HTTPStatus.OK
        assert get_status == HTTPStatus.OK
        assert logs_status == HTTPStatus.OK
        assert any(job["job_id"] == job_id for job in jobs_payload["jobs"])
        assert job_payload["status"] == "succeeded"
        assert job_payload["speakers"] == "2"
        assert job_payload["voice_a"] == "api-voice-a"
        assert job_payload["voice_b"] == "api-voice-b"
        assert job_payload["manifest_path"] == str((project_dir / "manifest.json").resolve(strict=False))
        assert any("[S0]" in line for line in logs_payload["lines"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_job_api_continue_reuses_existing_review_semantics(tmp_path: Path) -> None:
    youtube_url = "https://youtube.example/watch?v=api-continue"
    project_dir = write_process_project(
        tmp_path,
        project_name="job_api_continue",
        youtube_url=youtube_url,
    )
    project_dir_text = str(project_dir.resolve(strict=False))
    escaped_project_dir_text = project_dir_text.replace("\\", "\\\\")
    service, server, thread = _build_server(
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
                    f"[S6] Done {project_dir / 'output'}",
                ],
                "returncode": 0,
            },
        ],
    )
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        _, created = _request_json(
            "POST",
            f"{base_url}/jobs",
            {
                "job_type": "localize_video",
                "source": {
                    "type": "youtube_url",
                    "value": youtube_url,
                },
                "output_target": "editor",
            },
        )
        job_id = created["job_id"]
        wait_for(lambda: service.require_job(job_id).status == "waiting_for_review")

        try:
            _request_json("POST", f"{base_url}/jobs/{job_id}/continue", {})
        except HTTPError as exc:
            assert exc.code == HTTPStatus.CONFLICT
        else:
            raise AssertionError("Expected continue to fail before review approval.")

        set_review_stage(
            project_dir,
            stage_name="voice_review",
            status="approved",
            activate=False,
        )

        continue_status, continue_payload = _request_json(
            "POST",
            f"{base_url}/jobs/{job_id}/continue",
            {},
        )
        wait_for(lambda: service.require_job(job_id).status == "succeeded")

        assert continue_status == HTTPStatus.ACCEPTED
        assert continue_payload["job_id"] == job_id
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_job_api_accepts_local_audio_source_type(tmp_path: Path) -> None:
    """local_audio is now a supported source type; Job API should accept it."""
    youtube_url = "D:/input.wav"
    project_dir = write_process_project(
        tmp_path,
        project_name="local_audio_api_project",
        youtube_url=youtube_url,
    )
    service, server, thread = _build_server(
        tmp_path,
        plans=[
            {
                "lines": [f"[S6] Done {project_dir / 'output'}"],
                "returncode": 0,
            }
        ],
    )
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        status, created = _request_json(
            "POST",
            f"{base_url}/jobs",
            {
                "job_type": "localize_video",
                "source": {
                    "type": "local_audio",
                    "value": youtube_url,
                },
                "output_target": "editor",
            },
        )
        job_id = created["job_id"]
        wait_for(lambda: service.require_job(job_id).status in ("succeeded", "failed"))

        assert status == HTTPStatus.ACCEPTED
        loaded = service.require_job(job_id)
        assert loaded.source_type == "local_audio"
        assert loaded.source_ref == youtube_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
