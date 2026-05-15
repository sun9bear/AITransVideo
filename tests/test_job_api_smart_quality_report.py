"""Smart MVP P3-c: Job API ``/jobs/{id}/smart-quality-report`` endpoint.

The user-facing ``<SmartAutoDecisionPanel />`` (decision log §3) reads
smart quality report via this endpoint. Endpoint contract:

  GET /jobs/{id}/smart-quality-report
    → 200 + payload (verbatim contents of audit/smart_quality_report.json)
      when service_mode=="smart" AND the file exists.
    → 404 + ``{"error": "service_mode_not_smart"}`` for studio jobs
      (frontend hides the panel cleanly on this signal).
    → 404 + ``{"error": "quality_report_not_written", "reason": ...}``
      for smart jobs that hit handoff before terminal (file doesn't
      exist on disk).
    → 404 (existing require_job behavior) for invalid job_id.

Notes:
  - This endpoint is user-facing — it MUST NOT leak admin-only fields
    like cost_summary. Only the quality_report file is exposed.
  - Returns the file contents UNCHANGED (no field transformation).
    Renderer + admin tooling can read the schema_version=1 shape
    directly.
"""
from __future__ import annotations

import json
import sys
import threading
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _request_json(method: str, url: str):
    """GET helper that returns (status, payload_or_error_body)."""
    request = Request(url, method=method)
    request.add_header("Accept", "application/json")
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _spin_up_job_api(tmp_path: Path):
    """Spin up a real Job API server backed by JobStore at tmp_path.
    Returns (service, base_url, server, thread) — caller stops server.
    """
    from services.jobs.api import build_job_api_server
    from services.jobs.process_runner import ProcessJobRunner
    from services.jobs.service import JobService
    from services.jobs.store import JobStore

    store = JobStore(tmp_path / "jobs")
    runner = ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable="python",
        popen_factory=lambda *_a, **_kw: None,
        run_timeout_seconds=5,
    )
    service = JobService(store=store, runner=runner)
    server = build_job_api_server(service=service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    return service, base_url, server, thread


def _make_smart_job(
    tmp_path: Path, store, *, job_id: str, project_name: str
) -> Path:
    """Insert a smart JobRecord into the store + return project_dir."""
    from services.jobs.models import JobRecord

    project_dir = tmp_path / "projects" / project_name
    (project_dir / "audit").mkdir(parents=True, exist_ok=True)
    record = JobRecord(
        job_id=job_id,
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://yt.example/" + project_name,
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status="succeeded",
        current_stage="completed",
        progress_message="ok",
        created_at="2026-05-15T11:00:00+00:00",
        updated_at="2026-05-15T11:00:01+00:00",
        service_mode="smart",  # SMART
        project_dir=str(project_dir.resolve(strict=False)),
    )
    store.save_job(record)
    return project_dir


def _make_studio_job(
    tmp_path: Path, store, *, job_id: str, project_name: str
) -> Path:
    """Insert a non-smart JobRecord into the store."""
    from services.jobs.models import JobRecord

    project_dir = tmp_path / "projects" / project_name
    (project_dir / "audit").mkdir(parents=True, exist_ok=True)
    record = JobRecord(
        job_id=job_id,
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://yt.example/" + project_name,
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status="succeeded",
        current_stage="completed",
        progress_message="ok",
        created_at="2026-05-15T11:00:00+00:00",
        updated_at="2026-05-15T11:00:01+00:00",
        service_mode="studio",  # STUDIO
        project_dir=str(project_dir.resolve(strict=False)),
    )
    store.save_job(record)
    return project_dir


# ===========================================================================
# Cycle 1 — endpoint serves verbatim quality_report.json on success
# ===========================================================================


class TestSmartQualityReportEndpointHappyPath:

    def test_returns_200_and_verbatim_payload_for_smart_job(self, tmp_path):
        service, base_url, server, _thread = _spin_up_job_api(tmp_path)
        try:
            project_dir = _make_smart_job(
                tmp_path, service.store,
                job_id="job_qr_happy",
                project_name="job_qr_happy",
            )
            payload = {
                "schema_version": 1,
                "job_id": "job_qr_happy",
                "user_id": "user_abc",
                "service_mode": "smart",
                "smart_state_final": {
                    "status": "completed",
                    "credits_policy": "capture_full",
                },
                "speaker_summary": {
                    "main_speaker_count": 2,
                    "main_speaker_ids": ["speaker_a", "speaker_b"],
                    "excluded_speakers": [],
                },
                "voice_decisions": [
                    {
                        "speaker_id": "speaker_a",
                        "choice": "cloned",
                        "voice_id": "vt_speaker_a_xxx",
                        "clone_provider": "minimax_voice_clone",
                        "sample_seconds": 29.1,
                        "smart_decision_id": "abc123",
                    }
                ],
                "translation_review": {
                    "auto_approved": True,
                    "failed_check": None,
                    "metrics": {"glossary_total_terms": 0},
                },
                "retry_summary": {
                    "rewrite_attempts_used": 1,
                    "retts_attempts_used": 2,
                    "budget_remaining_minutes": 11.5,
                },
                "handoff_history": [],
                "generated_at": "2026-05-15T11:00:00+00:00",
            }
            (project_dir / "audit" / "smart_quality_report.json").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )

            status, body = _request_json(
                "GET",
                f"{base_url}/jobs/job_qr_happy/smart-quality-report",
            )
            assert status == HTTPStatus.OK
            assert body == payload  # verbatim
        finally:
            server.shutdown()


# ===========================================================================
# Cycle 2 — endpoint signals service_mode_not_smart for non-smart jobs
# ===========================================================================


class TestSmartQualityReportEndpointStudioJob:

    def test_returns_404_service_mode_not_smart_for_studio_job(self, tmp_path):
        service, base_url, server, _thread = _spin_up_job_api(tmp_path)
        try:
            _make_studio_job(
                tmp_path, service.store,
                job_id="job_studio_nosmart",
                project_name="job_studio_nosmart",
            )
            status, body = _request_json(
                "GET",
                f"{base_url}/jobs/job_studio_nosmart/smart-quality-report",
            )
            assert status == HTTPStatus.NOT_FOUND
            assert body.get("error") == "service_mode_not_smart"
        finally:
            server.shutdown()


# ===========================================================================
# Cycle 3 — endpoint signals quality_report_not_written for smart jobs
# that didn't reach happy-path terminal (handoff)
# ===========================================================================


class TestSmartQualityReportEndpointHandoffJob:

    def test_returns_404_quality_report_not_written_for_smart_without_file(
        self, tmp_path,
    ):
        service, base_url, server, _thread = _spin_up_job_api(tmp_path)
        try:
            project_dir = _make_smart_job(
                tmp_path, service.store,
                job_id="job_smart_handoff",
                project_name="job_smart_handoff",
            )
            # NO quality_report.json written (handoff scenario).
            assert not (project_dir / "audit" / "smart_quality_report.json").exists()

            status, body = _request_json(
                "GET",
                f"{base_url}/jobs/job_smart_handoff/smart-quality-report",
            )
            assert status == HTTPStatus.NOT_FOUND
            assert body.get("error") == "quality_report_not_written"
        finally:
            server.shutdown()


# ===========================================================================
# Cycle 4 — endpoint signals 404 for unknown job_id
# ===========================================================================


class TestSmartQualityReportEndpointUnknownJob:

    def test_returns_404_for_nonexistent_job(self, tmp_path):
        _service, base_url, server, _thread = _spin_up_job_api(tmp_path)
        try:
            status, body = _request_json(
                "GET",
                f"{base_url}/jobs/job_does_not_exist/smart-quality-report",
            )
            assert status == HTTPStatus.NOT_FOUND
            # Underlying require_job error message can be anything; we
            # just need a 404 so the frontend hides the panel.
        finally:
            server.shutdown()
