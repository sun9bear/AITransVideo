"""HTTP-layer tests for ``POST/GET /jobs/{id}/editing/speakers`` (Task 3,
plan 2026-05-04).

Covers:
- POST creates an editing-mode speaker (201 + body)
- GET merges baseline (from review_state.json) + editing speakers
- POST rejects empty / non-string display_name (400)
- POST rejects when job is not in editing state (409)
- POST 409 on duplicate display_name (case-sensitive, against baseline + editing)
- GET works in non-editing state (read-only — see endpoint comment)

Mirrors fixture style from tests/test_draft_audio_endpoint.py.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from services.jobs.api import build_job_api_server
from services.jobs.editing import enter_editing
from services.jobs.models import JOB_STATUS_SUCCEEDED, JobRecord
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
            AssertionError("subprocess should not be spawned in these tests"),
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


def _build_editing_job(
    service: JobService,
    tmp_path: Path,
    *,
    job_id: str = "job_speakers",
    baseline_speaker_names: dict[str, str] | None = None,
) -> tuple[str, Path]:
    """Create an editing-state Studio job with optional baseline speakers
    seeded into ``<project_dir>/review_state.json``."""
    project_dir = tmp_path / "projects" / job_id
    editor = project_dir / "editor"
    (editor / "tts_segments").mkdir(parents=True)
    (editor / "tts_segments" / "seg_001.wav").write_bytes(b"BASE_001")
    (editor / "segments.json").write_text(
        json.dumps([
            {"segment_id": "seg_001", "cn_text": "你好", "voice_id": "v1",
             "start_ms": 0, "end_ms": 1000, "speaker_id": "speaker_a"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    if baseline_speaker_names:
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "review_state.json").write_text(
            json.dumps({
                "stages": {
                    "speaker_review": {
                        "payload": {"speaker_names": baseline_speaker_names},
                    }
                }
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    now = _iso_now()
    record = JobRecord(
        job_id=job_id,
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://example.com/v",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status=JOB_STATUS_SUCCEEDED,
        current_stage="completed",
        progress_message=None,
        created_at=now,
        updated_at=now,
        completed_at=now,
        project_dir=str(project_dir),
        service_mode="studio",
    )
    service.store.save_job(record)
    editing_record = enter_editing(record, service.store)
    return editing_record.job_id, project_dir


def _http_request(method: str, url: str, payload: dict | None = None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(url, method=method, data=body)
    if body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# POST happy path
# ---------------------------------------------------------------------------


def test_post_creates_editing_speaker(tmp_path: Path) -> None:
    service, server, base_url = _start_server(tmp_path)
    try:
        job_id, project_dir = _build_editing_job(service, tmp_path)
        status, body = _http_request(
            "POST",
            f"{base_url}/jobs/{job_id}/editing/speakers",
            {"display_name": "桑达尔"},
        )
        assert status == HTTPStatus.CREATED.value, (status, body)
        assert body["display_name"] == "桑达尔"
        assert body["source"] == "editing"
        assert body["speaker_id"].startswith("speaker_")
        assert body["profile_status"] == "pending_segments"
        # Persisted on disk
        speakers_file = project_dir / "editor" / "editing" / "speakers.json"
        assert speakers_file.is_file()
        data = json.loads(speakers_file.read_text("utf-8"))
        assert data["speakers"][0]["display_name"] == "桑达尔"
    finally:
        server.shutdown()


def test_post_speaker_id_skips_baseline(tmp_path: Path) -> None:
    """When baseline already has speaker_a/speaker_b, new editing speaker
    must be allocated speaker_c."""
    service, server, base_url = _start_server(tmp_path)
    try:
        job_id, _ = _build_editing_job(
            service, tmp_path,
            baseline_speaker_names={
                "speaker_a": "Demis", "speaker_b": "Gary",
            },
        )
        status, body = _http_request(
            "POST",
            f"{base_url}/jobs/{job_id}/editing/speakers",
            {"display_name": "Sundar"},
        )
        assert status == HTTPStatus.CREATED.value, (status, body)
        assert body["speaker_id"] == "speaker_c"
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# POST validation
# ---------------------------------------------------------------------------


def test_post_400_when_display_name_missing(tmp_path: Path) -> None:
    service, server, base_url = _start_server(tmp_path)
    try:
        job_id, _ = _build_editing_job(service, tmp_path)
        status, body = _http_request(
            "POST",
            f"{base_url}/jobs/{job_id}/editing/speakers",
            {},  # no display_name
        )
        assert status == HTTPStatus.BAD_REQUEST.value, (status, body)
    finally:
        server.shutdown()


def test_post_400_when_display_name_blank(tmp_path: Path) -> None:
    service, server, base_url = _start_server(tmp_path)
    try:
        job_id, _ = _build_editing_job(service, tmp_path)
        status, body = _http_request(
            "POST",
            f"{base_url}/jobs/{job_id}/editing/speakers",
            {"display_name": "   "},
        )
        assert status == HTTPStatus.BAD_REQUEST.value, (status, body)
    finally:
        server.shutdown()


def test_post_409_when_job_not_in_editing(tmp_path: Path) -> None:
    """A succeeded job (NOT entered editing) must reject POST with 409."""
    service, server, base_url = _start_server(tmp_path)
    try:
        # Build a job but DO NOT call enter_editing
        project_dir = tmp_path / "projects" / "job_not_editing"
        editor = project_dir / "editor"
        editor.mkdir(parents=True)
        (editor / "segments.json").write_text("[]", encoding="utf-8")
        now = _iso_now()
        record = JobRecord(
            job_id="job_not_editing",
            job_type="localize_video",
            source_type="youtube_url",
            source_ref="https://example.com/v",
            output_target="editor",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            status=JOB_STATUS_SUCCEEDED,
            current_stage="completed",
            progress_message=None,
            created_at=now,
            updated_at=now,
            completed_at=now,
            project_dir=str(project_dir),
            service_mode="studio",
        )
        service.store.save_job(record)

        status, body = _http_request(
            "POST",
            f"{base_url}/jobs/job_not_editing/editing/speakers",
            {"display_name": "桑达尔"},
        )
        assert status == HTTPStatus.CONFLICT.value, (status, body)
        assert "not in editing" in body["error"].lower()
    finally:
        server.shutdown()


def test_post_409_on_duplicate_display_name(tmp_path: Path) -> None:
    service, server, base_url = _start_server(tmp_path)
    try:
        job_id, _ = _build_editing_job(
            service, tmp_path,
            baseline_speaker_names={"speaker_a": "Demis"},
        )
        # Attempt to create a speaker with same display_name as baseline
        status, body = _http_request(
            "POST",
            f"{base_url}/jobs/{job_id}/editing/speakers",
            {"display_name": "Demis"},
        )
        assert status == HTTPStatus.CONFLICT.value, (status, body)
        assert body.get("code") == "display_name_conflict"
    finally:
        server.shutdown()


def test_post_404_when_job_missing(tmp_path: Path) -> None:
    service, server, base_url = _start_server(tmp_path)
    try:
        status, body = _http_request(
            "POST",
            f"{base_url}/jobs/nonexistent/editing/speakers",
            {"display_name": "X"},
        )
        assert status == HTTPStatus.NOT_FOUND.value, (status, body)
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# GET merged view
# ---------------------------------------------------------------------------


def test_get_merges_baseline_and_editing(tmp_path: Path) -> None:
    service, server, base_url = _start_server(tmp_path)
    try:
        job_id, _ = _build_editing_job(
            service, tmp_path,
            baseline_speaker_names={
                "speaker_a": "Demis", "speaker_b": "Gary",
            },
        )
        # Create one editing speaker
        post_status, _ = _http_request(
            "POST",
            f"{base_url}/jobs/{job_id}/editing/speakers",
            {"display_name": "Sundar"},
        )
        assert post_status == HTTPStatus.CREATED.value

        get_status, body = _http_request(
            "GET",
            f"{base_url}/jobs/{job_id}/editing/speakers",
        )
        assert get_status == HTTPStatus.OK.value
        speakers = body["speakers"]
        # 2 baseline + 1 editing
        assert len(speakers) == 3
        sources = {s["source"] for s in speakers}
        assert sources == {"baseline", "editing"}
        baseline_names = {
            s["display_name"] for s in speakers if s["source"] == "baseline"
        }
        assert baseline_names == {"Demis", "Gary"}
        editing_names = {
            s["display_name"] for s in speakers if s["source"] == "editing"
        }
        assert editing_names == {"Sundar"}
    finally:
        server.shutdown()


def test_get_empty_when_no_baseline_no_editing(tmp_path: Path) -> None:
    service, server, base_url = _start_server(tmp_path)
    try:
        job_id, _ = _build_editing_job(service, tmp_path)
        status, body = _http_request(
            "GET",
            f"{base_url}/jobs/{job_id}/editing/speakers",
        )
        assert status == HTTPStatus.OK.value
        assert body == {"speakers": []}
    finally:
        server.shutdown()


def test_get_404_when_job_missing(tmp_path: Path) -> None:
    service, server, base_url = _start_server(tmp_path)
    try:
        status, body = _http_request(
            "GET",
            f"{base_url}/jobs/nonexistent/editing/speakers",
        )
        assert status == HTTPStatus.NOT_FOUND.value, (status, body)
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# Retry voice-profile inference (Task 5)
# ---------------------------------------------------------------------------


def test_retry_profile_resets_status_to_pending(tmp_path: Path, monkeypatch) -> None:
    """POST /editing/speakers/{sid}/retry-profile resets failed → pending
    and reschedules inference. With a sync executor + mocked Pass 3 we
    can observe the full chain in one HTTP call."""
    from concurrent.futures import Future
    from unittest.mock import patch as _patch

    from services.jobs import editing_voice_profile as evp
    from services.jobs.editing_speakers import load_speakers
    from services.jobs.editing_voice_profile import _update_speaker_status

    service, server, base_url = _start_server(tmp_path)
    try:
        job_id, project_dir = _build_editing_job(service, tmp_path)
        # Create a new speaker, mark it failed so retry has work to do.
        post_status, post_body = _http_request(
            "POST",
            f"{base_url}/jobs/{job_id}/editing/speakers",
            {"display_name": "Sundar"},
        )
        assert post_status == HTTPStatus.CREATED.value, (post_status, post_body)
        speaker_id = post_body["speaker_id"]
        _update_speaker_status(
            project_dir, speaker_id,
            status="failed", error="LLM down",
        )

        # Sync executor so the retry's submit() runs the inference inline,
        # giving us a deterministic post-state to assert on.
        class _SyncExecutor:
            def submit(self, fn, *args, **kw):
                with _patch(
                    "services.jobs.editing_voice_profile.review_pass3_voice_profiles",
                    return_value={speaker_id: {"voice_description": "x"}},
                ):
                    fn(*args, **kw)
                f = Future(); f.set_result(None); return f
        monkeypatch.setattr(evp, "_executor", _SyncExecutor())

        retry_status, retry_body = _http_request(
            "POST",
            f"{base_url}/jobs/{job_id}/editing/speakers/{speaker_id}/retry-profile",
            None,
        )
        assert retry_status == HTTPStatus.ACCEPTED.value, (retry_status, retry_body)
        assert retry_body == {
            "speaker_id": speaker_id, "status": "pending_segments",
        }
        sp = next(
            s for s in load_speakers(project_dir) if s.speaker_id == speaker_id
        )
        # After the sync executor's mocked Pass 3, status is 'ready'.
        assert sp.profile_status == "ready"
        assert sp.voice_profile == {"voice_description": "x"}
    finally:
        server.shutdown()


def test_retry_profile_unknown_speaker_is_soft_noop(tmp_path: Path) -> None:
    """Unknown speaker_id → 202 + no crash; speakers.json unchanged."""
    service, server, base_url = _start_server(tmp_path)
    try:
        job_id, project_dir = _build_editing_job(service, tmp_path)
        speakers_path = project_dir / "editor" / "editing" / "speakers.json"
        before = (
            speakers_path.read_text("utf-8") if speakers_path.is_file() else None
        )
        status, body = _http_request(
            "POST",
            f"{base_url}/jobs/{job_id}/editing/speakers/speaker_zzz/retry-profile",
            None,
        )
        assert status == HTTPStatus.ACCEPTED.value, (status, body)
        after = (
            speakers_path.read_text("utf-8") if speakers_path.is_file() else None
        )
        assert before == after  # no mutation for unknown id
    finally:
        server.shutdown()


def test_retry_profile_bad_speaker_id_format_returns_400(tmp_path: Path) -> None:
    """speaker_id not matching ``speaker_[a-z0-9_]{1,16}`` → 400 ValueError."""
    service, server, base_url = _start_server(tmp_path)
    try:
        job_id, _ = _build_editing_job(service, tmp_path)
        # ``Speaker_A`` (uppercase) violates the regex.
        status, body = _http_request(
            "POST",
            f"{base_url}/jobs/{job_id}/editing/speakers/Speaker_A/retry-profile",
            None,
        )
        assert status == HTTPStatus.BAD_REQUEST.value, (status, body)
        assert "speaker_id" in body["error"].lower()
    finally:
        server.shutdown()


def test_retry_profile_409_when_job_not_in_editing(tmp_path: Path) -> None:
    """Retry must be gated by editing state — same 409 as other mutations."""
    service, server, base_url = _start_server(tmp_path)
    try:
        # Build a succeeded job but DO NOT call enter_editing
        project_dir = tmp_path / "projects" / "job_nope"
        editor = project_dir / "editor"
        editor.mkdir(parents=True)
        (editor / "segments.json").write_text("[]", encoding="utf-8")
        now = _iso_now()
        record = JobRecord(
            job_id="job_nope",
            job_type="localize_video",
            source_type="youtube_url",
            source_ref="https://example.com/v",
            output_target="editor",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            status=JOB_STATUS_SUCCEEDED,
            current_stage="completed",
            progress_message=None,
            created_at=now,
            updated_at=now,
            completed_at=now,
            project_dir=str(project_dir),
            service_mode="studio",
        )
        service.store.save_job(record)

        status, body = _http_request(
            "POST",
            f"{base_url}/jobs/job_nope/editing/speakers/speaker_a/retry-profile",
            None,
        )
        assert status == HTTPStatus.CONFLICT.value, (status, body)
    finally:
        server.shutdown()
