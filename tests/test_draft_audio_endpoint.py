"""GET /jobs/{id}/segments/{sid}/draft-audio — streams draft TTS wav.

Phase 2 addition (plan §7.4 + §7.10 "接受/丢弃" row): the per-segment
"接受 / 丢弃" UI must not be blind. After single-segment regen, the
user needs to actually hear the new draft before deciding. This
endpoint serves the draft wav at
``editor/editing/tts_segments_draft/{sid}.wav`` as a Range-aware
``audio/wav`` stream so ``<audio controls>`` can seek.

Contract pinned here:

1. 200 + ``audio/wav`` body for a segment whose draft wav exists on disk.
2. 404 when no draft exists (fresh editing session; user hasn't
   regenerated anything) — frontend surfaces "暂无试听版本".
3. 400 on invalid segment_id (regex allowlist from D36).
4. 404 when job is not in editing state — drafts only live during
   editing; production artifact playback goes through ``stream/audio``.
5. Range header is honoured (206 + Content-Range) — HTML5 ``<audio>``
   seek requires range support.
6. Response has NO Content-Disposition: attachment — reuse of
   ``_write_stream`` guarantees this (it's for inline playback).
"""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from services.jobs.api import build_job_api_server
from services.jobs.editing import enter_editing
from services.jobs.editing_tts import DRAFT_TTS_SUBDIR
from services.jobs.models import JOB_STATUS_SUCCEEDED, JobRecord
from services.jobs.process_runner import ProcessJobRunner
from services.jobs.service import JobService
from services.jobs.store import JobStore


# ---------------------------------------------------------------------------
# Fixture plumbing — mirror tests/test_job_api_express_filter.py style
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _start_server(tmp_path: Path):
    store = JobStore(tmp_path / "jobs")
    runner = ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable="python",
        popen_factory=lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("subprocess should not be spawned in draft-audio tests"),
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
    with_draft_wav: bytes | None = None,
    draft_segment_id: str = "seg_001",
) -> tuple[str, Path]:
    """Create an editing-state job with baseline segments + optional draft wav."""
    project_dir = tmp_path / "projects" / "proj_draft_audio"
    editor = project_dir / "editor"
    (editor / "tts_segments").mkdir(parents=True)
    (editor / "tts_segments" / "seg_001.wav").write_bytes(b"BASE_001")
    (editor / "segments.json").write_text(
        json.dumps([
            {"segment_id": "seg_001", "cn_text": "你好", "voice_id": "v1",
             "start_ms": 0, "end_ms": 1000, "speaker_id": "A"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )

    now = _iso_now()
    record = JobRecord(
        job_id="job_draft",
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

    if with_draft_wav is not None:
        draft_path = project_dir / DRAFT_TTS_SUBDIR / f"{draft_segment_id}.wav"
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_path.write_bytes(with_draft_wav)

    return editing_record.job_id, project_dir


def _http_get(url: str, *, headers: dict[str, str] | None = None):
    req = Request(url, method="GET", headers=headers or {})
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except HTTPError as e:
        return e.code, dict(e.headers.items()), e.read()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_draft_audio_returns_wav_bytes_when_draft_exists(tmp_path: Path) -> None:
    service, server, base_url = _start_server(tmp_path)
    try:
        payload = b"RIFF" + b"\x00" * 100  # minimal fake wav
        job_id, _ = _build_editing_job(service, tmp_path, with_draft_wav=payload)

        status, headers, body = _http_get(
            f"{base_url}/jobs/{job_id}/segments/seg_001/draft-audio",
        )
        assert status == 200, (status, body[:120])
        assert headers.get("Content-Type") == "audio/wav", headers
        # No attachment disposition — inline playback only
        assert "attachment" not in (headers.get("Content-Disposition") or "").lower()
        assert body == payload
    finally:
        server.shutdown()


def test_draft_audio_honors_range_request(tmp_path: Path) -> None:
    """HTML5 <audio> seeking requires Range support (206 + Content-Range)."""
    service, server, base_url = _start_server(tmp_path)
    try:
        payload = bytes(range(256)) * 4  # 1024 bytes, distinguishable pattern
        job_id, _ = _build_editing_job(service, tmp_path, with_draft_wav=payload)

        status, headers, body = _http_get(
            f"{base_url}/jobs/{job_id}/segments/seg_001/draft-audio",
            headers={"Range": "bytes=100-199"},
        )
        assert status == 206, (status, body[:120])
        assert headers.get("Content-Type") == "audio/wav"
        assert headers.get("Content-Range") == f"bytes 100-199/{len(payload)}"
        assert len(body) == 100
        assert body == payload[100:200]
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# Missing draft / bad input
# ---------------------------------------------------------------------------


def test_draft_audio_404_when_draft_not_regenerated_yet(tmp_path: Path) -> None:
    service, server, base_url = _start_server(tmp_path)
    try:
        # Editing session exists but no draft was produced yet
        job_id, _ = _build_editing_job(service, tmp_path, with_draft_wav=None)

        status, _, body = _http_get(
            f"{base_url}/jobs/{job_id}/segments/seg_001/draft-audio",
        )
        assert status == 404, (status, body[:120])
    finally:
        server.shutdown()


def test_draft_audio_rejects_bad_segment_id(tmp_path: Path) -> None:
    """D36 regex ``^[a-z0-9_]{1,64}$`` — non-allowlisted characters must
    be rejected before touching disk. (Path traversal / whitespace cases
    are blocked by ``urllib`` itself before hitting the server, so we
    test the patterns that reach the server.)"""
    service, server, base_url = _start_server(tmp_path)
    try:
        job_id, _ = _build_editing_job(service, tmp_path, with_draft_wav=b"RIFF")

        for bad in ("SEG_001", "seg.001", "seg-001"):
            status, _, _ = _http_get(
                f"{base_url}/jobs/{job_id}/segments/{bad}/draft-audio",
            )
            assert status in (400, 404), (bad, status)
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# Non-editing state
# ---------------------------------------------------------------------------


def test_draft_audio_rejects_when_job_not_in_editing(tmp_path: Path) -> None:
    """Draft wavs only live during an editing session. A succeeded job
    without an active edit has no draft to serve — 404 (not 409) so
    the frontend treats it as "no draft yet" uniformly."""
    service, server, base_url = _start_server(tmp_path)
    try:
        # Create a succeeded job but DON'T enter editing
        project_dir = tmp_path / "projects" / "proj_not_editing"
        (project_dir / "editor" / "tts_segments").mkdir(parents=True)
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

        status, _, _ = _http_get(
            f"{base_url}/jobs/{record.job_id}/segments/seg_001/draft-audio",
        )
        assert status == 404
    finally:
        server.shutdown()
