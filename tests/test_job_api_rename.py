"""Job API PATCH /jobs/{id} — display_name rename support.

Covers the T0-4 (plan §6.5) rename contract on the Job API side. The gateway
layer adds ownership + collision-suffix logic on top; those are tested
separately. Here we isolate:

- ``display_name`` field is writable via PATCH
- empty / whitespace-only resets to NULL (clears the name)
- unsupported body keys return 400
- non-existent job returns 404
- normalisation matches submit_job (strip + 60-char cap)
"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests.job_test_helpers import FakePopenFactory, write_process_project
from services.jobs.api import build_job_api_server
from services.jobs.process_runner import ProcessJobRunner
from services.jobs.service import JobService
from services.jobs.store import JobStore


def _build_server(tmp_path: Path):
    popen_factory = FakePopenFactory([])
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


def _patch_json(url: str, payload: dict[str, object]):
    body = json.dumps(payload).encode("utf-8")
    request = Request(url, method="PATCH", data=body)
    request.add_header("Content-Type", "application/json; charset=utf-8")
    with urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _seed_job(service: JobService) -> str:
    """Insert a JobRecord directly via the store — bypasses submit_job's
    runner.start() which would call the FakePopenFactory. We only care
    about the PATCH surface here, so skip the live-worker machinery."""
    from services.jobs.models import JobRecord
    from services.state_manager import utc_now_iso
    now = utc_now_iso()
    record = JobRecord(
        job_id="job_rename_test",
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=rename",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status="queued",
        current_stage=None,
        progress_message="",
        created_at=now,
        updated_at=now,
        user_id="u1",
        display_name="Original",
    )
    service.store.save_job(record)
    return record.job_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_patch_display_name_updates_field(tmp_path: Path) -> None:
    service, server, thread = _build_server(tmp_path)
    try:
        job_id = _seed_job(service)
        base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"

        status, body = _patch_json(
            f"{base_url}/jobs/{job_id}", {"display_name": "重命名后"}
        )
        assert status == HTTPStatus.OK
        assert body["display_name"] == "重命名后"

        # Round-trip: verify the store actually holds the new value.
        refreshed = service.require_job(job_id)
        assert refreshed.display_name == "重命名后"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_patch_display_name_whitespace_clears_to_null(tmp_path: Path) -> None:
    service, server, thread = _build_server(tmp_path)
    try:
        job_id = _seed_job(service)
        base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"

        status, body = _patch_json(
            f"{base_url}/jobs/{job_id}", {"display_name": "   \t\n  "}
        )
        assert status == HTTPStatus.OK
        assert body["display_name"] is None
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_patch_display_name_caps_at_60_chars(tmp_path: Path) -> None:
    service, server, thread = _build_server(tmp_path)
    try:
        job_id = _seed_job(service)
        base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"

        long_name = "a" * 120
        status, body = _patch_json(
            f"{base_url}/jobs/{job_id}", {"display_name": long_name}
        )
        assert status == HTTPStatus.OK
        assert len(body["display_name"]) == 60
    finally:
        server.shutdown()
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_patch_unsupported_field_returns_400(tmp_path: Path) -> None:
    service, server, thread = _build_server(tmp_path)
    try:
        job_id = _seed_job(service)
        base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"

        try:
            _patch_json(f"{base_url}/jobs/{job_id}", {"speakers": "2"})
            raise AssertionError("expected 400 for unsupported PATCH field")
        except HTTPError as exc:
            assert exc.code == HTTPStatus.BAD_REQUEST
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_patch_missing_job_returns_404(tmp_path: Path) -> None:
    service, server, thread = _build_server(tmp_path)
    try:
        base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"

        try:
            _patch_json(
                f"{base_url}/jobs/does_not_exist",
                {"display_name": "x"},
            )
            raise AssertionError("expected 404 for missing job")
        except HTTPError as exc:
            assert exc.code == HTTPStatus.NOT_FOUND
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_patch_empty_body_returns_400(tmp_path: Path) -> None:
    service, server, thread = _build_server(tmp_path)
    try:
        job_id = _seed_job(service)
        base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"

        try:
            _patch_json(f"{base_url}/jobs/{job_id}", {})
            raise AssertionError("expected 400 for empty PATCH body")
        except HTTPError as exc:
            assert exc.code == HTTPStatus.BAD_REQUEST
    finally:
        server.shutdown()
        thread.join(timeout=2)
