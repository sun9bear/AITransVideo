"""Route-level tests for internal voice-label endpoints in Job API.

Uses build_job_api_server() with a real HTTP server on port 0, same
pattern as test_job_api.py.  Patches run_text_labeling / run_audio_profiling
to avoid calling real Gemini/TTS.
"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.client import HTTPResponse
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from services.jobs.api import build_job_api_server
from services.jobs.process_runner import ProcessJobRunner
from services.jobs.service import JobService
from services.jobs.store import JobStore
from tests.job_test_helpers import FakePopenFactory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    return server


def _url(server, path: str) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}/{path.lstrip('/')}"


def _post(server, path: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = Request(_url(server, path), method="POST", data=body)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


# Sample voice metadata
_VOICES = [
    {"voice_id": "v1", "display_name": "测试 1", "scene": "通用", "language": "zh",
     "provider_config": {"resource_id": "seed-tts-1.0"}},
]


# ---------------------------------------------------------------------------
# Tests: POST /internal/voice-label/text
# ---------------------------------------------------------------------------

class TestInternalVoiceLabelText:

    @patch("services.jobs.voice_label_tasks.run_text_labeling")
    def test_success(self, mock_run, tmp_path) -> None:
        mock_run.return_value = [
            {"voice_id": "v1", "age_group": "young", "persona_style": "warm", "energy_level": "medium"},
        ]
        server = _build_server(tmp_path)
        try:
            status, data = _post(server, "/internal/voice-label/text", {"voices": _VOICES})
            assert status == 200
            assert data["ok"] is True
            assert len(data["labels"]) == 1
            assert data["labels"][0]["voice_id"] == "v1"
            # Verify the helper was called with the voices metadata
            mock_run.assert_called_once()
            called_voices = mock_run.call_args[0][0]
            assert called_voices[0]["voice_id"] == "v1"
        finally:
            server.shutdown()

    def test_empty_voices_returns_400(self, tmp_path) -> None:
        server = _build_server(tmp_path)
        try:
            status, data = _post(server, "/internal/voice-label/text", {"voices": []})
            assert status == 400
            assert "error" in data
        finally:
            server.shutdown()

    def test_missing_voices_returns_400(self, tmp_path) -> None:
        server = _build_server(tmp_path)
        try:
            status, data = _post(server, "/internal/voice-label/text", {})
            assert status == 400
            assert "error" in data
        finally:
            server.shutdown()

    @patch("services.jobs.voice_label_tasks.run_text_labeling")
    def test_helper_exception_returns_500(self, mock_run, tmp_path) -> None:
        mock_run.side_effect = RuntimeError("GEMINI_API_KEY not set")
        server = _build_server(tmp_path)
        try:
            status, data = _post(server, "/internal/voice-label/text", {"voices": _VOICES})
            assert status == 500
            assert "GEMINI_API_KEY" in data["error"]
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# Tests: POST /internal/voice-label/audio/{round}
# ---------------------------------------------------------------------------

class TestInternalVoiceLabelAudio:

    @patch("services.jobs.voice_label_tasks.run_audio_profiling")
    def test_success_round1(self, mock_run, tmp_path) -> None:
        mock_run.return_value = [
            {"voice_id": "v1", "pitch_level": "high", "warmth": "medium"},
        ]
        server = _build_server(tmp_path)
        try:
            status, data = _post(server, "/internal/voice-label/audio/round1", {"voices": _VOICES})
            assert status == 200
            assert data["ok"] is True
            assert data["labels"][0]["pitch_level"] == "high"
            # Verify round_name was passed correctly
            mock_run.assert_called_once()
            _, round_name = mock_run.call_args[0]
            assert round_name == "round1"
        finally:
            server.shutdown()

    @patch("services.jobs.voice_label_tasks.run_audio_profiling")
    def test_round2_passes_correct_round(self, mock_run, tmp_path) -> None:
        mock_run.return_value = [{"voice_id": "v1", "pitch_level": "mid"}]
        server = _build_server(tmp_path)
        try:
            status, _ = _post(server, "/internal/voice-label/audio/round2", {"voices": _VOICES})
            assert status == 200
            _, round_name = mock_run.call_args[0]
            assert round_name == "round2"
        finally:
            server.shutdown()

    def test_empty_voices_returns_400(self, tmp_path) -> None:
        server = _build_server(tmp_path)
        try:
            status, data = _post(server, "/internal/voice-label/audio/round1", {"voices": []})
            assert status == 400
        finally:
            server.shutdown()

    @patch("services.jobs.voice_label_tasks.run_audio_profiling")
    def test_invalid_round_returns_400(self, mock_run, tmp_path) -> None:
        mock_run.side_effect = ValueError("Invalid round: round4")
        server = _build_server(tmp_path)
        try:
            status, data = _post(server, "/internal/voice-label/audio/round4", {"voices": _VOICES})
            assert status == 400
            assert "round" in data["error"].lower()
        finally:
            server.shutdown()

    @patch("services.jobs.voice_label_tasks.run_audio_profiling")
    def test_helper_exception_returns_500(self, mock_run, tmp_path) -> None:
        mock_run.side_effect = RuntimeError("profiling 脚本未生成任何 labels")
        server = _build_server(tmp_path)
        try:
            status, data = _post(server, "/internal/voice-label/audio/round1", {"voices": _VOICES})
            assert status == 500
            assert "labels" in data["error"]
        finally:
            server.shutdown()
