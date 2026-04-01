"""Phase 1 tests: job-scoped review-state, cancel, download, tts-segments-zip, voice-library."""
from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests.job_test_helpers import FakePopenFactory, wait_for, write_process_project
from services.jobs.api import build_job_api_server
from services.jobs.process_runner import ProcessJobRunner
from services.jobs.service import JobService
from services.jobs.store import JobStore
from services.web_ui.constants import PUBLIC_RESULT_DOWNLOAD_KEYS


def _build_server(tmp_path: Path, *, plans: list[dict]):
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


def _request_json(method: str, url: str, payload: dict | None = None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, method=method, data=body)
    request.add_header("Content-Type", "application/json; charset=utf-8")
    with urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _request_raw(method: str, url: str):
    request = Request(url, method=method)
    with urlopen(request, timeout=5) as response:
        return response.status, response.read(), dict(response.headers)


def _submit_and_wait(service, base_url, tmp_path, *, youtube_url, project_name, plans_lines, wait_status="succeeded"):
    """Submit a job, wait for target status, return (job_id, project_dir)."""
    project_dir = write_process_project(
        tmp_path,
        project_name=project_name,
        youtube_url=youtube_url,
    )
    _, created = _request_json(
        "POST",
        f"{base_url}/jobs",
        {
            "job_type": "localize_video",
            "source": {"type": "youtube_url", "value": youtube_url},
            "output_target": "editor",
        },
    )
    job_id = created["job_id"]
    wait_for(lambda: service.require_job(job_id).status == wait_status)
    return job_id, project_dir


# ===================================================================
# review-state: job-scoped, no global discovery
# ===================================================================


class TestReviewState:

    def test_review_state_returns_for_explicit_job_id(self, tmp_path: Path) -> None:
        youtube_url = "https://youtube.example/watch?v=review-state-test"
        project_dir = write_process_project(
            tmp_path, project_name="review_state_test", youtube_url=youtube_url,
        )
        service, server, thread = _build_server(
            tmp_path,
            plans=[{
                "lines": [f"[S6] Done {project_dir / 'output'}"],
                "returncode": 0,
            }],
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            job_id, _ = _submit_and_wait(
                service, base_url, tmp_path,
                youtube_url=youtube_url,
                project_name="review_state_test",
                plans_lines=[],
            )
            status, payload = _request_json("GET", f"{base_url}/jobs/{job_id}/review-state")
            assert status == HTTPStatus.OK
            assert payload["job_id"] == job_id
            assert "results" in payload
            assert "status" in payload
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_review_state_rejects_unknown_job_id(self, tmp_path: Path) -> None:
        service, server, thread = _build_server(tmp_path, plans=[])
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            try:
                _request_json("GET", f"{base_url}/jobs/nonexistent_job/review-state")
            except HTTPError as exc:
                assert exc.code == HTTPStatus.NOT_FOUND
            else:
                raise AssertionError("Expected 404 for unknown job_id")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_review_state_does_not_cross_jobs(self, tmp_path: Path) -> None:
        """When two jobs exist, review-state for job A returns A's data, not B's."""
        url_a = "https://youtube.example/watch?v=job-a"
        url_b = "https://youtube.example/watch?v=job-b"
        project_a = write_process_project(tmp_path, project_name="project_a", youtube_url=url_a)
        project_b = write_process_project(tmp_path, project_name="project_b", youtube_url=url_b)
        service, server, thread = _build_server(
            tmp_path,
            plans=[
                {"lines": [f"[S6] Done {project_a / 'output'}"], "returncode": 0},
                {"lines": [f"[S6] Done {project_b / 'output'}"], "returncode": 0},
            ],
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            _, created_a = _request_json("POST", f"{base_url}/jobs", {
                "job_type": "localize_video",
                "source": {"type": "youtube_url", "value": url_a},
                "output_target": "editor",
            })
            job_id_a = created_a["job_id"]
            wait_for(lambda: service.require_job(job_id_a).status == "succeeded")

            _, created_b = _request_json("POST", f"{base_url}/jobs", {
                "job_type": "localize_video",
                "source": {"type": "youtube_url", "value": url_b},
                "output_target": "editor",
            })
            job_id_b = created_b["job_id"]
            wait_for(lambda: service.require_job(job_id_b).status == "succeeded")

            _, state_a = _request_json("GET", f"{base_url}/jobs/{job_id_a}/review-state")
            _, state_b = _request_json("GET", f"{base_url}/jobs/{job_id_b}/review-state")

            assert state_a["job_id"] == job_id_a
            assert state_b["job_id"] == job_id_b
            # Ensure they don't return each other's data
            assert state_a["job_id"] != state_b["job_id"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_review_state_rejects_job_without_project_dir_even_if_url_matches(self, tmp_path: Path) -> None:
        """Regression: job exists, project_dir is empty, source_ref matches another project.

        Must return error, NOT the matched project's review state.
        This prevents the youtube_url fallback from leaking another project's data.
        """
        shared_url = "https://youtube.example/watch?v=shared-url-leak"
        # Step 1: Create a real project A on disk that matches the shared URL
        write_process_project(
            tmp_path, project_name="existing_project_for_url", youtube_url=shared_url,
        )

        # Step 2: Create a job B that succeeds quickly but whose project_dir won't
        # match the URL-matched project (it gets a different project_dir assigned by runner).
        # We use a different URL so project B doesn't interfere.
        other_url = "https://youtube.example/watch?v=other-url"
        other_project = write_process_project(
            tmp_path, project_name="other_project", youtube_url=other_url,
        )
        service, server, thread = _build_server(
            tmp_path,
            plans=[
                {"lines": [f"[S6] Done {other_project / 'output'}"], "returncode": 0},
            ],
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            # Submit a job, let it finish so we have a valid job_id
            _, created = _request_json("POST", f"{base_url}/jobs", {
                "job_type": "localize_video",
                "source": {"type": "youtube_url", "value": other_url},
                "output_target": "editor",
            })
            job_id = created["job_id"]
            wait_for(lambda: service.require_job(job_id).status == "succeeded")

            # Step 3: Tamper with the job record — clear project_dir, set source_ref to shared_url
            record = service.require_job(job_id)
            from dataclasses import replace as _replace
            tampered = _replace(record, project_dir=None, source_ref=shared_url)
            service.store.save_job(tampered)

            # Step 4: Request review-state — should fail, NOT return existing_project_for_url's data
            try:
                _request_json("GET", f"{base_url}/jobs/{job_id}/review-state")
            except HTTPError as exc:
                assert exc.code == HTTPStatus.NOT_FOUND, (
                    f"Expected 404 when project_dir is missing, got {exc.code}"
                )
            else:
                raise AssertionError(
                    "Expected review-state to fail when project_dir is missing, "
                    "but it returned 200 (likely fell back to URL-matched project)"
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# ===================================================================
# cancel: job-scoped, no global active job scanning
# ===================================================================


class TestCancel:

    def test_cancel_specific_job(self, tmp_path: Path) -> None:
        youtube_url = "https://youtube.example/watch?v=cancel-test"
        project_dir = write_process_project(
            tmp_path, project_name="cancel_test", youtube_url=youtube_url,
        )
        escaped = str(project_dir.resolve(strict=False)).replace("\\", "\\\\")
        service, server, thread = _build_server(
            tmp_path,
            plans=[{
                "lines": [
                    f'[WEB_REVIEW] {{"stage":"voice_review","tab":"voice-library",'
                    f'"project_dir":"{escaped}",'
                    f'"message":"review required"}}'
                ],
                "returncode": 0,
            }],
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            _, created = _request_json("POST", f"{base_url}/jobs", {
                "job_type": "localize_video",
                "source": {"type": "youtube_url", "value": youtube_url},
                "output_target": "editor",
            })
            job_id = created["job_id"]
            wait_for(lambda: service.require_job(job_id).status == "waiting_for_review")

            status, result = _request_json("POST", f"{base_url}/jobs/{job_id}/cancel", {})
            assert status == HTTPStatus.OK
            assert result["success"] is True
            assert result["job"]["status"] == "cancelled"
            assert result["job"]["job_id"] == job_id
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_cancel_does_not_affect_other_jobs(self, tmp_path: Path) -> None:
        """Cancel job A should not touch job B."""
        url_a = "https://youtube.example/watch?v=cancel-a"
        url_b = "https://youtube.example/watch?v=cancel-b"
        project_a = write_process_project(tmp_path, project_name="cancel_a", youtube_url=url_a)
        project_b = write_process_project(tmp_path, project_name="cancel_b", youtube_url=url_b)
        escaped_a = str(project_a.resolve(strict=False)).replace("\\", "\\\\")
        escaped_b = str(project_b.resolve(strict=False)).replace("\\", "\\\\")
        service, server, thread = _build_server(
            tmp_path,
            plans=[
                {
                    "lines": [
                        f'[WEB_REVIEW] {{"stage":"voice_review","tab":"voice-library",'
                        f'"project_dir":"{escaped_a}","message":"review A"}}'
                    ],
                    "returncode": 0,
                },
                {
                    "lines": [
                        f'[WEB_REVIEW] {{"stage":"voice_review","tab":"voice-library",'
                        f'"project_dir":"{escaped_b}","message":"review B"}}'
                    ],
                    "returncode": 0,
                },
            ],
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            _, created_a = _request_json("POST", f"{base_url}/jobs", {
                "job_type": "localize_video",
                "source": {"type": "youtube_url", "value": url_a},
                "output_target": "editor",
            })
            job_a = created_a["job_id"]
            wait_for(lambda: service.require_job(job_a).status == "waiting_for_review")

            _, created_b = _request_json("POST", f"{base_url}/jobs", {
                "job_type": "localize_video",
                "source": {"type": "youtube_url", "value": url_b},
                "output_target": "editor",
            })
            job_b = created_b["job_id"]
            wait_for(lambda: service.require_job(job_b).status == "waiting_for_review")

            # Cancel A
            _request_json("POST", f"{base_url}/jobs/{job_a}/cancel", {})

            # B should still be waiting_for_review
            record_b = service.require_job(job_b)
            assert record_b.status == "waiting_for_review"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_cancel_rejects_unknown_job_id(self, tmp_path: Path) -> None:
        service, server, thread = _build_server(tmp_path, plans=[])
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            try:
                _request_json("POST", f"{base_url}/jobs/nonexistent/cancel", {})
            except HTTPError as exc:
                assert exc.code == HTTPStatus.NOT_FOUND
            else:
                raise AssertionError("Expected 404")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_cancel_rejects_already_succeeded_job(self, tmp_path: Path) -> None:
        youtube_url = "https://youtube.example/watch?v=cancel-succeeded"
        project_dir = write_process_project(
            tmp_path, project_name="cancel_succeeded", youtube_url=youtube_url,
        )
        service, server, thread = _build_server(
            tmp_path,
            plans=[{"lines": [f"[S6] Done {project_dir / 'output'}"], "returncode": 0}],
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            job_id, _ = _submit_and_wait(
                service, base_url, tmp_path,
                youtube_url=youtube_url,
                project_name="cancel_succeeded",
                plans_lines=[],
            )
            try:
                _request_json("POST", f"{base_url}/jobs/{job_id}/cancel", {})
            except HTTPError as exc:
                assert exc.code == HTTPStatus.CONFLICT
            else:
                raise AssertionError("Expected 409 for already succeeded job")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# ===================================================================
# download: key-based whitelist
# ===================================================================


class TestDownload:

    def test_download_whitelisted_key_for_existing_artifact(self, tmp_path: Path) -> None:
        youtube_url = "https://youtube.example/watch?v=download-test"
        project_dir = write_process_project(
            tmp_path, project_name="download_test", youtube_url=youtube_url,
        )
        # Create the manifest key file
        manifest_path = project_dir / "manifest.json"
        assert manifest_path.exists()  # created by write_process_project

        service, server, thread = _build_server(
            tmp_path,
            plans=[{"lines": [f"[S6] Done {project_dir / 'output'}"], "returncode": 0}],
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            job_id, _ = _submit_and_wait(
                service, base_url, tmp_path,
                youtube_url=youtube_url,
                project_name="download_test",
                plans_lines=[],
            )
            status, body, headers = _request_raw(
                "GET", f"{base_url}/jobs/{job_id}/download/manifest.file",
            )
            assert status == HTTPStatus.OK
            # Should be valid JSON (manifest.json)
            json.loads(body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_download_rejects_non_whitelisted_key(self, tmp_path: Path) -> None:
        youtube_url = "https://youtube.example/watch?v=download-reject"
        project_dir = write_process_project(
            tmp_path, project_name="download_reject", youtube_url=youtube_url,
        )
        service, server, thread = _build_server(
            tmp_path,
            plans=[{"lines": [f"[S6] Done {project_dir / 'output'}"], "returncode": 0}],
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            job_id, _ = _submit_and_wait(
                service, base_url, tmp_path,
                youtube_url=youtube_url,
                project_name="download_reject",
                plans_lines=[],
            )
            try:
                _request_raw("GET", f"{base_url}/jobs/{job_id}/download/secret.internal.config")
            except HTTPError as exc:
                # Should be rejected by whitelist validation (400 or 403)
                assert exc.code in (HTTPStatus.BAD_REQUEST, HTTPStatus.FORBIDDEN)
            else:
                raise AssertionError("Expected rejection for non-whitelisted key")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class TestTtsSegmentsZip:

    def test_tts_segments_zip_returns_zip(self, tmp_path: Path) -> None:
        youtube_url = "https://youtube.example/watch?v=tts-zip-test"
        project_dir = write_process_project(
            tmp_path, project_name="tts_zip_test", youtube_url=youtube_url,
        )
        # Create fake TTS aligned files
        tts_dir = project_dir / "tts"
        tts_dir.mkdir(parents=True, exist_ok=True)
        (tts_dir / "segment_001_speaker_a_aligned.wav").write_bytes(b"RIFF" + b"\x00" * 40)
        (tts_dir / "segment_002_speaker_a_aligned.wav").write_bytes(b"RIFF" + b"\x00" * 40)

        service, server, thread = _build_server(
            tmp_path,
            plans=[{"lines": [f"[S6] Done {project_dir / 'output'}"], "returncode": 0}],
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            job_id, _ = _submit_and_wait(
                service, base_url, tmp_path,
                youtube_url=youtube_url,
                project_name="tts_zip_test",
                plans_lines=[],
            )
            status, body, headers = _request_raw(
                "GET", f"{base_url}/jobs/{job_id}/tts-segments-zip",
            )
            assert status == HTTPStatus.OK
            assert b"PK" in body[:4]  # ZIP magic bytes
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# ===================================================================
# voice-library: global read endpoint
# ===================================================================


class TestVoiceLibrary:

    def test_voice_library_returns_global_snapshot(self, tmp_path: Path) -> None:
        service, server, thread = _build_server(tmp_path, plans=[])
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            status, payload = _request_json("GET", f"{base_url}/voice-library")
            assert status == HTTPStatus.OK
            # Should have voice library structure keys
            assert "speakers" in payload
            assert "builtin_voice_options" in payload
            assert "speaker_count" in payload
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_voice_library_does_not_require_job_id(self, tmp_path: Path) -> None:
        """voice-library is a global endpoint, not under /jobs/{id}."""
        service, server, thread = _build_server(tmp_path, plans=[])
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            status, payload = _request_json("GET", f"{base_url}/voice-library")
            assert status == HTTPStatus.OK
            # No job_id in the response
            assert "job_id" not in payload
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
