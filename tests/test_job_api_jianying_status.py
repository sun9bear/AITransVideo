"""Job API endpoint: GET /jobs/{id}/jianying-draft-status (Task K5).

Tests for the HTTP endpoint that polls for jianying draft generation status.
Uses a real HTTP server backed by JobStore on tmp_path, following the pattern
in tests/test_job_api_jianying_generate.py.

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md §11.4 + §11.7 K5
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from services.jobs.api import build_job_api_server
from services.jobs.jianying_draft_runner import JianyingDraftRunner
from services.jobs.models import JobRecord
from services.jobs.process_runner import ProcessJobRunner
from services.jobs.service import JobService
from services.jobs.store import JobStore


# ---------------------------------------------------------------------------
# Fixture plumbing
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _start_server(tmp_path: Path, *, jianying_runner: Any = None):
    """Start a real HTTP server backed by JobStore."""
    store = JobStore(tmp_path / "jobs")
    runner = ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable="python",
        popen_factory=lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("subprocess should not be spawned in jianying tests"),
        ),
        run_timeout_seconds=5,
    )
    service = JobService(store=store, runner=runner)
    server = build_job_api_server(
        service=service,
        host="127.0.0.1",
        port=0,
        jianying_runner=jianying_runner,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
    return service, store, server, thread, base_url


def _inject_job(
    store: JobStore,
    *,
    job_id: str,
    status: str = "succeeded",
    service_mode: str | None = "studio",
    jianying_draft_status: str = "idle",
    jianying_draft_started_at: str | None = None,
    jianying_draft_zip_path: str | None = None,
    jianying_draft_error: str | None = None,
    jianying_draft_completed_at: str | None = None,
) -> JobRecord:
    """Insert a pre-built JobRecord directly into the store."""
    now = _iso_now()
    _stage_map = {
        "succeeded": "completed",
        "failed": "failed",
        "running": "ingestion",
        "queued": None,
        "editing": "completed",
        "waiting_for_review": "translation_review",
    }
    current_stage = _stage_map.get(status, None)
    record = JobRecord(
        job_id=job_id,
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=test",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status=status,
        current_stage=current_stage,
        progress_message=None,
        created_at=now,
        updated_at=now,
        completed_at=now if status == "succeeded" else None,
        project_dir=None,
        service_mode=service_mode,
        jianying_draft_status=jianying_draft_status,
        jianying_draft_started_at=jianying_draft_started_at,
        jianying_draft_zip_path=jianying_draft_zip_path,
        jianying_draft_error=jianying_draft_error,
        jianying_draft_completed_at=jianying_draft_completed_at,
    )
    store.save_job(record)
    return record


def _http_get_json(url: str, *, headers: dict | None = None):
    """GET JSON from url; returns (status_code, body_dict)."""
    req = Request(
        url,
        method="GET",
        headers=headers or {},
    )
    try:
        with urlopen(req, timeout=5) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8")
    except HTTPError as e:
        status = e.code
        raw = e.read().decode("utf-8", errors="replace") or "{}"
    try:
        body_out = json.loads(raw)
    except json.JSONDecodeError:
        body_out = {"raw": raw}
    return status, body_out


def _internal_headers() -> dict:
    """Return X-Internal-Key header if AVT_INTERNAL_API_KEY is set in env."""
    key = os.environ.get("AVT_INTERNAL_API_KEY", "").strip()
    if key:
        return {"X-Internal-Key": key}
    return {}


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------

class TestGetJianyingDraftStatusEndpoint:
    """6 scenarios for GET /jobs/{id}/jianying-draft-status."""

    # ------------------------------------------------------------------
    # 1. Idle status — job exists, no generation triggered yet
    # ------------------------------------------------------------------
    def test_idle_status_returns_200(self, tmp_path: Path) -> None:
        """Pre-create JobRecord with jianying_draft_status=idle.
        GET -> 200 + {status: idle, other fields: null/None}."""
        service, store, server, _, base_url = _start_server(tmp_path)
        try:
            record = _inject_job(store, job_id="job_idle", jianying_draft_status="idle")

            status, body = _http_get_json(
                f"{base_url}/jobs/job_idle/jianying-draft-status",
            )

            assert status == 200, f"expected 200, got {status}; body={body!r}"
            assert body.get("status") == "idle"
            assert body.get("started_at") is None
            assert body.get("completed_at") is None
            assert body.get("error") is None
            assert body.get("artifact_key") is None
            assert body.get("draft_zip_size_bytes") is None
            assert body.get("compatibility_report_path") is None
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 2. Running status — generation in progress
    # ------------------------------------------------------------------
    def test_running_status_returns_200(self, tmp_path: Path) -> None:
        """Pre-set jianying_draft_status=running with started_at.
        GET -> 200 + {status: running, started_at: ..., completed_at: null, ...}."""
        service, store, server, _, base_url = _start_server(tmp_path)
        try:
            started = _iso_now()
            record = _inject_job(
                store, job_id="job_running",
                jianying_draft_status="running",
                jianying_draft_started_at=started,
            )

            status, body = _http_get_json(
                f"{base_url}/jobs/job_running/jianying-draft-status",
            )

            assert status == 200, f"expected 200, got {status}; body={body!r}"
            assert body.get("status") == "running"
            assert body.get("started_at") == started
            assert body.get("completed_at") is None
            assert body.get("error") is None
            assert body.get("artifact_key") is None
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 3. Succeeded status — zip exists, size returned, artifact_key set
    # ------------------------------------------------------------------
    def test_succeeded_status_with_zip_file_returns_200(self, tmp_path: Path) -> None:
        """Pre-set status=succeeded with zip_path to actual file.
        GET -> 200 + artifact_key, draft_zip_size_bytes from file."""
        service, store, server, _, base_url = _start_server(tmp_path)
        try:
            # Create a fake zip file
            zip_dir = tmp_path / "zips"
            zip_dir.mkdir(parents=True, exist_ok=True)
            zip_path = zip_dir / "draft.zip"
            zip_path.write_bytes(b"fake zip content 12345")  # 22 bytes

            completed = _iso_now()
            record = _inject_job(
                store, job_id="job_succeeded",
                jianying_draft_status="succeeded",
                jianying_draft_zip_path=str(zip_path),
                jianying_draft_completed_at=completed,
            )

            status, body = _http_get_json(
                f"{base_url}/jobs/job_succeeded/jianying-draft-status",
            )

            assert status == 200, f"expected 200, got {status}; body={body!r}"
            assert body.get("status") == "succeeded"
            assert body.get("artifact_key") == "editor.jianying_draft_zip"
            assert body.get("completed_at") == completed
            assert body.get("draft_zip_size_bytes") == 22
            assert body.get("error") is None
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 4. Failed status — error message present, no artifact
    # ------------------------------------------------------------------
    def test_failed_status_returns_200(self, tmp_path: Path) -> None:
        """Pre-set status=failed with error message.
        GET -> 200 + {status: failed, error: ..., artifact_key: null}."""
        service, store, server, _, base_url = _start_server(tmp_path)
        try:
            error_msg = "Backend validation failed: some reason"
            completed = _iso_now()
            record = _inject_job(
                store, job_id="job_failed",
                jianying_draft_status="failed",
                jianying_draft_error=error_msg,
                jianying_draft_completed_at=completed,
            )

            status, body = _http_get_json(
                f"{base_url}/jobs/job_failed/jianying-draft-status",
            )

            assert status == 200, f"expected 200, got {status}; body={body!r}"
            assert body.get("status") == "failed"
            assert body.get("error") == error_msg
            assert body.get("artifact_key") is None
            assert body.get("draft_zip_size_bytes") is None
            assert body.get("completed_at") == completed
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 5. Job not found -> 404
    # ------------------------------------------------------------------
    def test_job_not_found_returns_404(self, tmp_path: Path) -> None:
        """GET /jobs/nonexistent/jianying-draft-status -> 404."""
        _, store, server, _, base_url = _start_server(tmp_path)
        try:
            status, body = _http_get_json(
                f"{base_url}/jobs/nonexistent_job_xyz/jianying-draft-status",
            )

            assert status == 404, f"expected 404, got {status}; body={body!r}"
            assert body.get("code") == "job_not_found"
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 6. Missing internal-key auth -> 403 when AVT_INTERNAL_API_KEY set
    # ------------------------------------------------------------------
    def test_wrong_internal_key_returns_403_when_key_configured(self, tmp_path: Path) -> None:
        """When AVT_INTERNAL_API_KEY is set and request has wrong key, return 403."""
        _, store, server, _, base_url = _start_server(tmp_path)
        try:
            _inject_job(store, job_id="job_auth_test")
            # Temporarily set the env var for this test
            with patch.dict(os.environ, {"AVT_INTERNAL_API_KEY": "correct-test-key-abc123"}):
                # Send request with wrong key
                status, body = _http_get_json(
                    f"{base_url}/jobs/job_auth_test/jianying-draft-status",
                    headers={"X-Internal-Key": "wrong-key"},
                )
            assert status == 403, f"expected 403 for wrong key, got {status}; body={body!r}"
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 7. Succeeded but zip file missing -> size is null (defensive)
    # ------------------------------------------------------------------
    def test_succeeded_but_zip_missing_returns_200_with_null_size(self, tmp_path: Path) -> None:
        """Pre-set status=succeeded but zip_path doesn't exist.
        GET -> 200 + artifact_key, draft_zip_size_bytes=null."""
        service, store, server, _, base_url = _start_server(tmp_path)
        try:
            completed = _iso_now()
            record = _inject_job(
                store, job_id="job_succeeded_no_file",
                jianying_draft_status="succeeded",
                jianying_draft_zip_path="/nonexistent/draft.zip",
                jianying_draft_completed_at=completed,
            )

            status, body = _http_get_json(
                f"{base_url}/jobs/job_succeeded_no_file/jianying-draft-status",
            )

            assert status == 200, f"expected 200, got {status}; body={body!r}"
            assert body.get("status") == "succeeded"
            assert body.get("artifact_key") == "editor.jianying_draft_zip"
            assert body.get("draft_zip_size_bytes") is None
        finally:
            server.shutdown()
