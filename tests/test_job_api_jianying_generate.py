"""Job API endpoint: POST /jobs/{id}/generate-jianying-draft (Task K4).

Tests for the HTTP endpoint that triggers background Jianying draft generation.
Uses a real HTTP server backed by JobStore on tmp_path, following the pattern
in tests/test_job_api_express_filter.py.

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md §11.4 + §11.7 K4
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
from services.jobs.jianying_draft_runner import (
    JianyingDraftRunner,
    JianyingInvalidDraftRoot,
    JianyingNotAllowedError,
)
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
    """Start a real HTTP server backed by JobStore.

    If jianying_runner is provided (e.g. a MagicMock), it is passed to
    build_job_api_server to replace the real runner. Otherwise the real runner
    is constructed with no backend (so it would fail on actual generation, but
    that's fine for API-layer tests).
    """
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
    # Pick a valid current_stage per SUPPORTED_PUBLIC_STAGES
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


def _http_post_json(url: str, body: dict | None = None, *, headers: dict | None = None):
    """POST JSON to url; returns (status_code, body_dict)."""
    data = json.dumps(body or {}).encode("utf-8")
    req = Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
            **(headers or {}),
        },
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

class TestGenerateJianyingDraftEndpoint:
    """16 scenarios for POST /jobs/{id}/generate-jianying-draft (K4 + K12)."""

    # ------------------------------------------------------------------
    # 1. Happy path: idle job, succeeded status, studio mode -> 202
    # ------------------------------------------------------------------
    def test_happy_path_idle_succeeded_studio_returns_202(self, tmp_path: Path) -> None:
        """Pre-create JobRecord with status=succeeded, service_mode=studio,
        jianying_draft_status=idle. POST -> 202 + {status: running, started_at: ...}.
        Verify store updated to running after the call."""
        service, store, server, _, base_url = _start_server(tmp_path)
        try:
            record = _inject_job(store, job_id="job_idle_ok", jianying_draft_status="idle")

            status, body = _http_post_json(
                f"{base_url}/jobs/job_idle_ok/generate-jianying-draft",
            )

            assert status == 202, f"expected 202, got {status}; body={body!r}"
            assert body.get("status") == "running"
            assert body.get("started_at") is not None

            # Verify store was updated
            updated = store.require_job("job_idle_ok")
            assert updated.jianying_draft_status == "running"
            assert updated.jianying_draft_started_at is not None
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 2. Job not found -> 404
    # ------------------------------------------------------------------
    def test_job_not_found_returns_404(self, tmp_path: Path) -> None:
        _, store, server, _, base_url = _start_server(tmp_path)
        try:
            status, body = _http_post_json(
                f"{base_url}/jobs/nonexistent_job_xyz/generate-jianying-draft",
            )
            assert status == 404, f"expected 404, got {status}; body={body!r}"
            assert body.get("code") == "job_not_found"
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 3. Job status != succeeded -> 400
    # ------------------------------------------------------------------
    @pytest.mark.parametrize("bad_status", ["running", "failed", "editing", "queued"])
    def test_job_not_succeeded_returns_400(self, tmp_path: Path, bad_status: str) -> None:
        _, store, server, _, base_url = _start_server(tmp_path)
        try:
            _inject_job(
                store,
                job_id=f"job_bad_{bad_status}",
                status=bad_status,
                service_mode="studio",
            )
            status, body = _http_post_json(
                f"{base_url}/jobs/job_bad_{bad_status}/generate-jianying-draft",
            )
            assert status == 400, f"expected 400 for status={bad_status}, got {status}; body={body!r}"
            assert body.get("code") == "job_not_succeeded"
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 4. service_mode != "studio" -> 403
    # ------------------------------------------------------------------
    def test_express_mode_returns_403(self, tmp_path: Path) -> None:
        _, store, server, _, base_url = _start_server(tmp_path)
        try:
            _inject_job(
                store, job_id="job_express_403",
                status="succeeded", service_mode="express",
            )
            status, body = _http_post_json(
                f"{base_url}/jobs/job_express_403/generate-jianying-draft",
            )
            assert status == 403, f"expected 403 for express mode, got {status}; body={body!r}"
            assert body.get("code") == "service_mode_not_studio"
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 5. service_mode is None -> 403 (defensive)
    # ------------------------------------------------------------------
    def test_service_mode_none_returns_403(self, tmp_path: Path) -> None:
        _, store, server, _, base_url = _start_server(tmp_path)
        try:
            _inject_job(
                store, job_id="job_no_mode_403",
                status="succeeded", service_mode=None,
            )
            status, body = _http_post_json(
                f"{base_url}/jobs/job_no_mode_403/generate-jianying-draft",
            )
            assert status == 403, f"expected 403 for service_mode=None, got {status}; body={body!r}"
            assert body.get("code") == "service_mode_not_studio"
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 6. Already running -> 409
    # ------------------------------------------------------------------
    def test_already_running_returns_409(self, tmp_path: Path) -> None:
        _, store, server, _, base_url = _start_server(tmp_path)
        try:
            _inject_job(
                store, job_id="job_running_409",
                status="succeeded", service_mode="studio",
                jianying_draft_status="running",
                jianying_draft_started_at=_iso_now(),
            )
            status, body = _http_post_json(
                f"{base_url}/jobs/job_running_409/generate-jianying-draft",
            )
            assert status == 409, f"expected 409 for already-running, got {status}; body={body!r}"
            assert body.get("status") == "running"
            # Should have an informative message
            msg = body.get("message", "")
            assert "in progress" in msg.lower() or "already" in msg.lower() or msg, \
                f"409 body should have informative message, got: {body!r}"
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 7. Already succeeded -> 200, no re-dispatch
    # ------------------------------------------------------------------
    def test_already_succeeded_returns_200_no_redispatch(self, tmp_path: Path) -> None:
        """Already-succeeded jobs return 200 with existing zip path.
        The runner trigger() must NOT spawn a new thread."""
        # Use a mock runner to verify trigger is called but backend is not
        mock_runner = MagicMock(spec=JianyingDraftRunner)
        mock_runner.trigger.return_value = {
            "status": "succeeded",
            "completed_at": _iso_now(),
            "draft_zip_path": "/fake/path/draft.zip",
            "artifact_key": "editor.jianying_draft_zip",
        }
        mock_runner.reap_stale.return_value = 0

        _, store, server, _, base_url = _start_server(tmp_path, jianying_runner=mock_runner)
        try:
            _inject_job(
                store, job_id="job_succeeded_200",
                status="succeeded", service_mode="studio",
                jianying_draft_status="succeeded",
                jianying_draft_zip_path="/fake/path/draft.zip",
                jianying_draft_completed_at=_iso_now(),
            )
            status, body = _http_post_json(
                f"{base_url}/jobs/job_succeeded_200/generate-jianying-draft",
            )
            assert status == 200, f"expected 200 for already-succeeded, got {status}; body={body!r}"
            assert body.get("status") == "succeeded"
            assert body.get("draft_zip_path") == "/fake/path/draft.zip"
            # Verify trigger was called exactly once (no second dispatch)
            # K12: now trigger is called with user_draft_root=None kwarg
            mock_runner.trigger.assert_called_once_with("job_succeeded_200", user_draft_root=None)
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 8. Failed -> restarts to 202, error cleared
    # ------------------------------------------------------------------
    def test_failed_restarts_to_202(self, tmp_path: Path) -> None:
        """Previously failed draft generation should be re-triggered (202)
        and the old error cleared from the store."""
        service, store, server, _, base_url = _start_server(tmp_path)
        try:
            _inject_job(
                store, job_id="job_failed_retry",
                status="succeeded", service_mode="studio",
                jianying_draft_status="failed",
                jianying_draft_error="some previous error",
            )
            status, body = _http_post_json(
                f"{base_url}/jobs/job_failed_retry/generate-jianying-draft",
            )
            assert status == 202, f"expected 202 for failed->retry, got {status}; body={body!r}"
            assert body.get("status") == "running"

            # Verify error cleared in store
            updated = store.require_job("job_failed_retry")
            assert updated.jianying_draft_status == "running"
            assert updated.jianying_draft_error is None
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 9. Missing/wrong internal-key -> 403 when AVT_INTERNAL_API_KEY is set
    # ------------------------------------------------------------------
    def test_wrong_internal_key_returns_403_when_key_configured(self, tmp_path: Path) -> None:
        """When AVT_INTERNAL_API_KEY is set in the environment and a request
        arrives with a wrong key, the endpoint must return 403."""
        _, store, server, _, base_url = _start_server(tmp_path)
        try:
            _inject_job(store, job_id="job_auth_test")
            # Temporarily set the env var for this test
            with patch.dict(os.environ, {"AVT_INTERNAL_API_KEY": "correct-test-key-abc123"}):
                # Send request with wrong key
                status, body = _http_post_json(
                    f"{base_url}/jobs/job_auth_test/generate-jianying-draft",
                    headers={"X-Internal-Key": "wrong-key"},
                )
            assert status == 403, f"expected 403 for wrong key, got {status}; body={body!r}"
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # 10. Engine unavailable -> 503 (mock runner raises JianyingEngineUnavailable)
    # ------------------------------------------------------------------
    def test_engine_unavailable_returns_503(self, tmp_path: Path) -> None:
        """When the runner raises JianyingEngineUnavailable (e.g. pyJianYingDraft
        not installed), the endpoint must return 503."""
        from services.jobs.jianying_draft_runner import JianyingEngineUnavailable

        mock_runner = MagicMock(spec=JianyingDraftRunner)
        mock_runner.trigger.side_effect = JianyingEngineUnavailable(
            "pyJianYingDraft package is not installed"
        )
        mock_runner.reap_stale.return_value = 0

        _, store, server, _, base_url = _start_server(tmp_path, jianying_runner=mock_runner)
        try:
            _inject_job(store, job_id="job_engine_503")
            status, body = _http_post_json(
                f"{base_url}/jobs/job_engine_503/generate-jianying-draft",
            )
            assert status == 503, f"expected 503 for engine unavailable, got {status}; body={body!r}"
            assert body.get("code") == "engine_unavailable"
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # K12: 11. POST with valid user_draft_root body -> runner called with kwarg
    # ------------------------------------------------------------------
    def test_post_with_valid_user_draft_root_calls_runner_with_kwarg(self, tmp_path: Path) -> None:
        """POST with valid user_draft_root in JSON body → runner.trigger called
        with user_draft_root kwarg. Response 202."""
        mock_runner = MagicMock(spec=JianyingDraftRunner)
        mock_runner.trigger.return_value = {
            "status": "running",
            "started_at": _iso_now(),
        }
        mock_runner.reap_stale.return_value = 0

        _, store, server, _, base_url = _start_server(tmp_path, jianying_runner=mock_runner)
        try:
            _inject_job(store, job_id="job_with_draft_root")
            user_root = "F:\\Drafts\\JianyingPro"
            status, body = _http_post_json(
                f"{base_url}/jobs/job_with_draft_root/generate-jianying-draft",
                body={"user_draft_root": user_root},
            )
            assert status == 202, f"expected 202, got {status}; body={body!r}"
            assert body.get("status") == "running"
            # Verify trigger was called with user_draft_root kwarg
            mock_runner.trigger.assert_called_once_with("job_with_draft_root", user_draft_root=user_root)
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # K12: 12. POST with empty user_draft_root -> runner raises validation error
    # ------------------------------------------------------------------
    def test_post_with_empty_user_draft_root_returns_400(self, tmp_path: Path) -> None:
        """POST with empty user_draft_root (after strip) → runner raises
        JianyingInvalidDraftRoot → endpoint returns 400."""
        mock_runner = MagicMock(spec=JianyingDraftRunner)
        mock_runner.trigger.side_effect = JianyingInvalidDraftRoot(
            "user_draft_root must not be empty after stripping whitespace."
        )
        mock_runner.reap_stale.return_value = 0

        _, store, server, _, base_url = _start_server(tmp_path, jianying_runner=mock_runner)
        try:
            _inject_job(store, job_id="job_empty_root")
            status, body = _http_post_json(
                f"{base_url}/jobs/job_empty_root/generate-jianying-draft",
                body={"user_draft_root": ""},
            )
            assert status == 400, f"expected 400, got {status}; body={body!r}"
            assert body.get("code") == "invalid_user_draft_root"
            assert "empty" in str(body.get("message", "")).lower()
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # K12: 13. POST with URL-like user_draft_root -> runner rejects it
    # ------------------------------------------------------------------
    def test_post_with_url_user_draft_root_returns_400(self, tmp_path: Path) -> None:
        """POST with user_draft_root that looks like a URL (http://...)
        → runner raises JianyingInvalidDraftRoot → endpoint returns 400."""
        mock_runner = MagicMock(spec=JianyingDraftRunner)
        mock_runner.trigger.side_effect = JianyingInvalidDraftRoot(
            "user_draft_root looks like a URL (http://...); please provide a local filesystem path instead."
        )
        mock_runner.reap_stale.return_value = 0

        _, store, server, _, base_url = _start_server(tmp_path, jianying_runner=mock_runner)
        try:
            _inject_job(store, job_id="job_url_root")
            status, body = _http_post_json(
                f"{base_url}/jobs/job_url_root/generate-jianying-draft",
                body={"user_draft_root": "http://example.com/drafts"},
            )
            assert status == 400, f"expected 400, got {status}; body={body!r}"
            assert body.get("code") == "invalid_user_draft_root"
            assert "url" in str(body.get("message", "")).lower()
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # K12: 14. POST with non-string user_draft_root (e.g. number) -> 400
    # ------------------------------------------------------------------
    def test_post_with_non_string_user_draft_root_returns_400(self, tmp_path: Path) -> None:
        """POST with user_draft_root as a number (123) or dict → endpoint
        catches at body-validation stage (before runner) → 400."""
        _, store, server, _, base_url = _start_server(tmp_path)
        try:
            _inject_job(store, job_id="job_non_string_root")
            status, body = _http_post_json(
                f"{base_url}/jobs/job_non_string_root/generate-jianying-draft",
                body={"user_draft_root": 123},
            )
            assert status == 400, f"expected 400, got {status}; body={body!r}"
            assert body.get("code") == "invalid_user_draft_root"
            assert "string" in str(body.get("message", "")).lower()
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # K12: 15. POST with malformed JSON body -> 400
    # ------------------------------------------------------------------
    def test_post_with_malformed_json_returns_400(self, tmp_path: Path) -> None:
        """POST with malformed JSON body → endpoint catches at parsing stage
        → 400 + code 'invalid_body'."""
        _, store, server, _, base_url = _start_server(tmp_path)
        try:
            _inject_job(store, job_id="job_bad_json")
            # Manually craft a bad request with invalid JSON
            bad_json_data = b"{invalid json"
            req = Request(
                f"{base_url}/jobs/job_bad_json/generate-jianying-draft",
                data=bad_json_data,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(bad_json_data)),
                },
            )
            try:
                with urlopen(req, timeout=5) as resp:
                    status = resp.status
                    raw = resp.read().decode("utf-8")
            except HTTPError as e:
                status = e.code
                raw = e.read().decode("utf-8", errors="replace") or "{}"
            body = json.loads(raw) if raw else {}
            assert status == 400, f"expected 400 for bad JSON, got {status}; body={body!r}"
            assert body.get("code") == "invalid_body"
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # K12: 16. POST without body (legacy K4 behavior) -> 202
    # ------------------------------------------------------------------
    def test_post_without_body_legacy_behavior_returns_202(self, tmp_path: Path) -> None:
        """POST without body at all (empty or no Content-Length) → runner.trigger
        called with user_draft_root=None. Response 202. (Legacy K4 behavior.)"""
        service, store, server, _, base_url = _start_server(tmp_path)
        try:
            _inject_job(store, job_id="job_no_body")
            # POST with no body
            status, body = _http_post_json(
                f"{base_url}/jobs/job_no_body/generate-jianying-draft",
                body=None,
            )
            assert status == 202, f"expected 202 for empty body, got {status}; body={body!r}"
            assert body.get("status") == "running"
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# reap_stale at startup (functional check via real store)
# ---------------------------------------------------------------------------

class TestReapStaleAtStartup:
    def test_stale_running_job_reaped_on_server_start(self, tmp_path: Path) -> None:
        """A jianying_draft_status=running job with an old started_at should be
        marked failed by reap_stale() when the server starts."""
        # Pre-populate the store with a stale running job BEFORE starting server
        store = JobStore(tmp_path / "jobs")
        old_time = "2020-01-01T00:00:00+00:00"  # definitely stale
        record = JobRecord(
            job_id="job_stale_reap",
            job_type="localize_video",
            source_type="youtube_url",
            source_ref="https://youtube.example/watch?v=stale",
            output_target="editor",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            status="succeeded",
            current_stage="completed",
            progress_message=None,
            created_at=old_time,
            updated_at=old_time,
            completed_at=old_time,
            project_dir=None,
            service_mode="studio",
            jianying_draft_status="running",
            jianying_draft_started_at=old_time,
        )
        store.save_job(record)

        # Now start the server — reap_stale() should fire at startup
        runner_proc = ProcessJobRunner(
            store=store,
            project_root=tmp_path,
            python_executable="python",
            popen_factory=lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("no subprocess in reap test"),
            ),
            run_timeout_seconds=5,
        )
        service = JobService(store=store, runner=runner_proc)
        server = build_job_api_server(service=service, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # Give server time to start and run reap_stale
            import time; time.sleep(0.1)
            # Verify stale job was reaped
            reaped = store.require_job("job_stale_reap")
            assert reaped.jianying_draft_status == "failed", (
                f"Stale job should be reaped to failed, got {reaped.jianying_draft_status!r}"
            )
            assert reaped.jianying_draft_error is not None
        finally:
            server.shutdown()
