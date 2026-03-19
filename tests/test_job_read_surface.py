from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
import threading
from urllib.request import Request, urlopen

from services.jobs.api import build_job_api_server
from services.jobs.process_runner import ProcessJobRunner
from services.jobs.service import JobService
from services.jobs.store import JobStore
from tests.job_test_helpers import FakePopenFactory, wait_for, write_process_project


def _build_service(tmp_path: Path, *, plans: list[dict[str, object]]) -> JobService:
    popen_factory = FakePopenFactory(plans)
    store = JobStore(tmp_path / "jobs")
    runner = ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable="python",
        popen_factory=popen_factory,
        run_timeout_seconds=5,
    )
    return JobService(store=store, runner=runner)


def _build_server(tmp_path: Path, *, plans: list[dict[str, object]]):
    service = _build_service(tmp_path, plans=plans)
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


def _write_manifest_for_read_surface(project_dir: Path) -> dict[str, Path]:
    output_dir = project_dir / "output"
    publish_dir = project_dir / "publish"
    translation_dir = project_dir / "translation"
    output_dir.mkdir(parents=True, exist_ok=True)
    publish_dir.mkdir(parents=True, exist_ok=True)
    translation_dir.mkdir(parents=True, exist_ok=True)

    artifacts = {
        "project_state": (project_dir / "project_state.json").resolve(strict=False),
        "review_state": (project_dir / "review_state.json").resolve(strict=False),
        "translation_segments": (translation_dir / "segments.json").resolve(strict=False),
        "dubbed_audio": (output_dir / "dubbed_audio_complete.wav").resolve(strict=False),
        "subtitles": (output_dir / "subtitles.srt").resolve(strict=False),
        "dubbed_video": (publish_dir / "dubbed_video.mp4").resolve(strict=False),
    }
    artifacts["review_state"].write_text(
        json.dumps({"active_stage": None, "stages": {}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifacts["translation_segments"].write_text(
        json.dumps({"segments": []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifacts["dubbed_audio"].write_bytes(b"audio")
    artifacts["subtitles"].write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    artifacts["dubbed_video"].write_bytes(b"video")

    (project_dir / "manifest.json").write_text(
        json.dumps(
            {
                "fallback_summary": {
                    "tts": {
                        "applied": False,
                    }
                },
                "artifact_index": {
                    "state.project": str(artifacts["project_state"]),
                    "state.review": "review_state.json",
                    "translation.segments": "translation/segments.json",
                    "editor.dubbed_audio_complete": "output/dubbed_audio_complete.wav",
                    "editor.subtitles": "output/subtitles.srt",
                    "publish.dubbed_video": str(artifacts["dubbed_video"]),
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return artifacts


def _submit_success_job(service: JobService, *, youtube_url: str) -> str:
    created = service.submit_job(source_type="youtube_url", source_ref=youtube_url)
    wait_for(lambda: service.require_job(created.job_id).status == "succeeded")
    return created.job_id


def test_job_service_result_summary_and_artifacts_are_manifest_derived(tmp_path: Path) -> None:
    youtube_url = "https://youtube.example/watch?v=read-surface-success"
    project_dir = write_process_project(
        tmp_path,
        project_name="read_surface_success",
        youtube_url=youtube_url,
        fallback_summary={"tts": {"applied": False}},
    )
    artifacts = _write_manifest_for_read_surface(project_dir)
    service = _build_service(
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

    job_id = _submit_success_job(service, youtube_url=youtube_url)
    result_summary = service.get_result_summary(job_id)
    artifacts_payload = service.get_artifacts(job_id)

    assert result_summary["job_id"] == job_id
    assert result_summary["status"] == "succeeded"
    assert result_summary["project_dir"] == str(project_dir.resolve(strict=False))
    assert result_summary["manifest"]["available"] is True
    assert result_summary["manifest"]["artifact_count"] == 6
    assert result_summary["fallback_summary"] == {"tts": {"applied": False}}
    assert {item["key"] for item in result_summary["outputs"]} == {
        "state.project",
        "state.review",
        "translation.segments",
        "editor.dubbed_audio_complete",
        "editor.subtitles",
        "publish.dubbed_video",
    }
    assert result_summary["artifacts"]["total_count"] == 6
    assert any(
        category == {"name": "editor", "count": 2, "existing_count": 2}
        for category in result_summary["artifacts"]["categories"]
    )
    assert artifacts_payload["manifest"]["available"] is True
    assert len(artifacts_payload["artifacts"]) == 6
    assert any(
        artifact["key"] == "publish.dubbed_video"
        and artifact["path"] == str(artifacts["dubbed_video"])
        and artifact["exists"] is True
        for artifact in artifacts_payload["artifacts"]
    )


def test_job_service_read_surface_tolerates_missing_manifest(tmp_path: Path) -> None:
    youtube_url = "https://youtube.example/watch?v=read-surface-missing-manifest"
    project_dir = write_process_project(
        tmp_path,
        project_name="read_surface_missing_manifest",
        youtube_url=youtube_url,
    )
    service = _build_service(
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

    job_id = _submit_success_job(service, youtube_url=youtube_url)
    manifest_path = project_dir / "manifest.json"
    manifest_path.unlink()

    result_summary = service.get_result_summary(job_id)
    artifacts_payload = service.get_artifacts(job_id)

    assert result_summary["manifest_path"] == str(manifest_path.resolve(strict=False))
    assert result_summary["manifest"]["available"] is False
    assert result_summary["manifest"]["artifact_count"] == 0
    assert result_summary["outputs"] == []
    assert result_summary["artifacts"]["total_count"] == 0
    assert artifacts_payload["artifacts"] == []


def test_job_service_read_surface_tolerates_incomplete_manifest_without_new_truth_source(
    tmp_path: Path,
) -> None:
    youtube_url = "https://youtube.example/watch?v=read-surface-incomplete-manifest"
    project_dir = write_process_project(
        tmp_path,
        project_name="read_surface_incomplete_manifest",
        youtube_url=youtube_url,
    )
    service = _build_service(
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

    job_id = _submit_success_job(service, youtube_url=youtube_url)
    (project_dir / "manifest.json").write_text(
        json.dumps({}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result_summary = service.get_result_summary(job_id)
    artifacts_payload = service.get_artifacts(job_id)
    stored_job_payload = json.loads(
        (tmp_path / "jobs" / f"{job_id}.json").read_text(encoding="utf-8")
    )

    assert result_summary["manifest"]["available"] is True
    assert result_summary["manifest"]["artifact_count"] == 0
    assert result_summary["outputs"] == []
    assert artifacts_payload["artifacts"] == []
    assert "artifacts" not in stored_job_payload
    assert "result_summary" not in stored_job_payload


def test_job_api_exposes_result_summary_and_artifacts(tmp_path: Path) -> None:
    youtube_url = "https://youtube.example/watch?v=read-surface-api"
    project_dir = write_process_project(
        tmp_path,
        project_name="read_surface_api",
        youtube_url=youtube_url,
        fallback_summary={"tts": {"applied": False}},
    )
    artifacts = _write_manifest_for_read_surface(project_dir)
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
        wait_for(lambda: service.require_job(job_id).status == "succeeded")

        summary_status, result_summary = _request_json(
            "GET",
            f"{base_url}/jobs/{job_id}/result-summary",
        )
        artifacts_status, artifacts_payload = _request_json(
            "GET",
            f"{base_url}/jobs/{job_id}/artifacts",
        )

        assert summary_status == HTTPStatus.OK
        assert artifacts_status == HTTPStatus.OK
        assert result_summary["job_id"] == job_id
        assert result_summary["manifest"]["artifact_count"] == 6
        assert any(
            item["key"] == "editor.dubbed_audio_complete"
            and item["path"] == str(artifacts["dubbed_audio"])
            for item in result_summary["outputs"]
        )
        assert any(
            artifact["key"] == "state.review"
            and artifact["exists"] is True
            for artifact in artifacts_payload["artifacts"]
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
