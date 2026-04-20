"""POST /jobs/{id}/review/voice/preview state gate — allow editing.

The endpoint is a stateless TTS probe (synthesize a sample sentence
with the requested voice + provider) and doesn't touch project_dir
or mutate job state. Originally it only accepted ``waiting_for_review``,
which blocked the Studio post-edit "音色修改" Tab from auditioning
voices — users hit 409 with message ``is not waiting_for_review``.

Fix: ``editing`` is now also allowed. These tests pin the state gate
so nobody accidentally tightens it back.

The underlying ``preview_voice`` helper is monkey-patched to a stub so
we don't call real TTS providers (paid API + network).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from services.jobs.api import build_job_api_server
from services.jobs.models import JOB_STATUS_EDITING, JOB_STATUS_SUCCEEDED, JobRecord
from services.jobs.process_runner import ProcessJobRunner
from services.jobs.service import JobService
from services.jobs.store import JobStore


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _start_server(tmp_path: Path):
    store = JobStore(tmp_path / "jobs")
    runner = ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable="python",
        popen_factory=lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("subprocess should not spawn in state-gate tests"),
        ),
        run_timeout_seconds=5,
    )
    service = JobService(store=store, runner=runner)
    server = build_job_api_server(service=service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
    return service, server, base_url


def _inject_job(service: JobService, tmp_path: Path, *, status: str) -> str:
    """Insert a job in the requested state. A project dir with a minimal
    editor/segments.json is required so api.py's _require_project_dir passes."""
    project_dir = tmp_path / "projects" / f"proj_{status}"
    (project_dir / "editor").mkdir(parents=True)
    (project_dir / "editor" / "segments.json").write_text("[]", encoding="utf-8")
    now = _iso_now()
    record = JobRecord(
        job_id=f"job_{status}",
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://example.com/v",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status=status,
        current_stage="completed",
        progress_message=None,
        created_at=now,
        updated_at=now,
        project_dir=str(project_dir),
        service_mode="studio",
    )
    service.store.save_job(record)
    return record.job_id


def _http_post_json(url: str, body: dict):
    req = Request(
        url,
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") or "{}"
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


@pytest.fixture
def stub_preview_voice(monkeypatch: pytest.MonkeyPatch):
    """Replace preview_voice so the gate-test never hits real TTS."""
    import services.jobs.review_actions as review_actions

    def _stub(*, voice_id: str, config_path, tts_provider=None, sample_text=None):
        return {
            "audio_base64": "ZmFrZQ==",  # "fake"
            "expired": False,
            "error": None,
            "voice_id": voice_id,
        }

    monkeypatch.setattr(review_actions, "preview_voice", _stub)


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------


def test_preview_allows_editing_state(tmp_path: Path, stub_preview_voice) -> None:
    """Regression: before the fix this returned 409 ``is not waiting_for_review``;
    now it must pass the gate and run the (stubbed) preview_voice."""
    service, server, base_url = _start_server(tmp_path)
    try:
        job_id = _inject_job(service, tmp_path, status=JOB_STATUS_EDITING)

        status, body = _http_post_json(
            f"{base_url}/jobs/{job_id}/review/voice/preview",
            {"voice_id": "Male-Qn-Jingying", "tts_provider": "minimax"},
        )
        assert status == 200, (status, body)
        assert body.get("audio_base64") == "ZmFrZQ=="
    finally:
        server.shutdown()


def test_preview_allows_waiting_for_review_state(
    tmp_path: Path, stub_preview_voice
) -> None:
    """Baseline: the original review-gate path must still work — fix
    mustn't accidentally break the main flow."""
    service, server, base_url = _start_server(tmp_path)
    try:
        job_id = _inject_job(service, tmp_path, status="waiting_for_review")

        status, body = _http_post_json(
            f"{base_url}/jobs/{job_id}/review/voice/preview",
            {"voice_id": "Male-Qn-Jingying", "tts_provider": "minimax"},
        )
        assert status == 200, (status, body)
    finally:
        server.shutdown()


def test_preview_rejects_succeeded_state(
    tmp_path: Path, stub_preview_voice
) -> None:
    """Succeeded jobs (outside an active editing session) are not a
    valid preview caller — gate still blocks them so users don't
    accidentally preview voices on a finished task."""
    service, server, base_url = _start_server(tmp_path)
    try:
        job_id = _inject_job(service, tmp_path, status=JOB_STATUS_SUCCEEDED)

        status, body = _http_post_json(
            f"{base_url}/jobs/{job_id}/review/voice/preview",
            {"voice_id": "Male-Qn-Jingying", "tts_provider": "minimax"},
        )
        assert status == 409, (status, body)
        assert "waiting_for_review" in body.get("error", "") or "editing" in body.get("error", "")
    finally:
        server.shutdown()
