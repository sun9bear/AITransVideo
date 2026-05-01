from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

import voice_selection_api
from services.review_state import (
    REVIEW_STATUS_PENDING,
    VOICE_SELECTION_REVIEW_STAGE,
    ReviewStateManager,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_request(body: dict) -> MagicMock:
    request = MagicMock()
    request.body = AsyncMock(return_value=json.dumps(body, ensure_ascii=False).encode("utf-8"))
    return request


def _make_db(job: object) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = job
    db.execute = AsyncMock(return_value=result)
    return db


def _write_voice_selection_review_state(
    project_dir: Path,
    *,
    speaker_id: str = "speaker_a",
    started_at: str | None = None,
) -> None:
    manager = ReviewStateManager(project_dir / "review_state.json")
    speaker_payload: dict[str, object] = {
        "speaker_id": speaker_id,
        "voice_id": "auto",
        "voice_source": "auto_matched",
    }
    if started_at is not None:
        speaker_payload["cloning"] = {"started_at": started_at}
    manager.set_stage(
        VOICE_SELECTION_REVIEW_STAGE,
        status=REVIEW_STATUS_PENDING,
        payload={
            "speakers": [speaker_payload],
        },
        activate=True,
    )


def _load_stage_payload(project_dir: Path) -> dict:
    manager = ReviewStateManager(project_dir / "review_state.json")
    stage = manager.get_stage(VOICE_SELECTION_REVIEW_STAGE)
    assert stage is not None
    return stage.get("payload", {})


def _write_clone_project(
    project_dir: Path,
    *,
    speaker_id: str = "speaker_a",
    include_audio: bool = True,
) -> None:
    transcript_dir = project_dir / "transcript"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    if include_audio:
        audio_dir = project_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / "original.wav").write_bytes(b"fake-audio")
    transcript_payload = {
        "lines": [
            {
                "index": 1,
                "speaker_id": speaker_id,
                "start_ms": 0,
                "end_ms": 12_000,
                "source_text": "Hello there from the target speaker.",
            }
        ]
    }
    (transcript_dir / "transcript.json").write_text(
        json.dumps(transcript_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_voice_selection_review_state(project_dir, speaker_id=speaker_id)


class _FakeAsyncClient:
    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, *args, **kwargs):
        del args, kwargs
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"results": {"project_dir": str(self._project_dir)}},
        )


class TestCloneLockHelpers:
    def test_acquire_clone_lock_marks_speaker_as_cloning(self, tmp_path: Path) -> None:
        _write_voice_selection_review_state(tmp_path)

        acquired, message = voice_selection_api._acquire_clone_lock(tmp_path, "speaker_a")

        assert acquired is True
        assert message is None
        payload = _load_stage_payload(tmp_path)
        speaker = payload["speakers"][0]
        assert speaker["cloning"]["started_at"]

    def test_acquire_clone_lock_rejects_recent_lock(self, tmp_path: Path) -> None:
        _write_voice_selection_review_state(
            tmp_path,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        acquired, message = voice_selection_api._acquire_clone_lock(tmp_path, "speaker_a")

        assert acquired is False
        assert "正在克隆" in str(message)

    def test_clear_clone_lock_removes_cloning_marker(self, tmp_path: Path) -> None:
        _write_voice_selection_review_state(
            tmp_path,
            started_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        )

        voice_selection_api._clear_clone_lock(tmp_path, "speaker_a")

        payload = _load_stage_payload(tmp_path)
        speaker = payload["speakers"][0]
        assert "cloning" not in speaker


class TestVoiceCloneEndpointCloneLock:
    def test_voice_clone_for_selection_returns_409_when_clone_is_in_progress(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        project_dir = tmp_path / "project_locked"
        _write_clone_project(project_dir)
        _write_voice_selection_review_state(
            project_dir,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        db = _make_db(SimpleNamespace(job_id="job-1"))
        request = _make_request({"speaker_id": "speaker_a", "segment_ids": [1]})

        monkeypatch.setattr(voice_selection_api.settings, "auth_required", False)

        with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(project_dir)):
            response = _run(voice_selection_api.voice_clone_for_selection(request, "job-1", db, None))

        body = json.loads(response.body.decode("utf-8"))
        assert response.status_code == 409
        assert body["error"] == "clone_in_progress"

    def test_voice_clone_for_selection_clears_lock_after_clone_failure(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        project_dir = tmp_path / "project_clone_fail"
        _write_clone_project(project_dir)

        db = _make_db(SimpleNamespace(job_id="job-2"))
        request = _make_request({"speaker_id": "speaker_a", "segment_ids": [1]})

        monkeypatch.setattr(voice_selection_api.settings, "auth_required", False)

        with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(project_dir)):
            with patch.object(voice_selection_api, "_concat_segments_ffmpeg", return_value=project_dir / "speaker_audio" / "speaker_a" / "clone_sample.wav"):
                with patch.object(voice_selection_api, "_clone_via_minimax", side_effect=RuntimeError("clone boom")):
                    response = _run(voice_selection_api.voice_clone_for_selection(request, "job-2", db, None))

        body = json.loads(response.body.decode("utf-8"))
        assert response.status_code == 500
        assert body["error"] == "clone_failed"
        payload = _load_stage_payload(project_dir)
        assert "cloning" not in payload["speakers"][0]

    def test_voice_clone_for_selection_clears_lock_after_clone_success(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        project_dir = tmp_path / "project_clone_success"
        _write_clone_project(project_dir)

        db = _make_db(SimpleNamespace(job_id="job-3"))
        request = _make_request({"speaker_id": "speaker_a", "segment_ids": [1]})

        monkeypatch.setattr(voice_selection_api.settings, "auth_required", False)

        with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(project_dir)):
            with patch.object(voice_selection_api, "_concat_segments_ffmpeg", return_value=project_dir / "speaker_audio" / "speaker_a" / "clone_sample.wav"):
                with patch.object(voice_selection_api, "_clone_via_minimax", return_value="vt_speaker_a_123"):
                    response = _run(voice_selection_api.voice_clone_for_selection(request, "job-3", db, None))

        body = json.loads(response.body.decode("utf-8"))
        assert response.status_code == 200
        assert body["voice_id"] == "vt_speaker_a_123"
        payload = _load_stage_payload(project_dir)
        assert "cloning" not in payload["speakers"][0]

    def test_voice_clone_credits_use_scoped_reserve_and_release(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        project_dir = tmp_path / "project_credit_release"
        _write_clone_project(project_dir, include_audio=False)

        db = _make_db(SimpleNamespace(job_id="job-4"))
        user = SimpleNamespace(id="user-1", trial_granted_at=None, trial_ends_at=None)
        request = _make_request({"speaker_id": "speaker_a", "segment_ids": [1]})
        calls = []

        async def record_shadow(fn, *args, **kwargs):
            calls.append((fn.__name__, args, kwargs))
            return []

        async def record_live_reserve(*args, **kwargs):
            calls.append(("reserve_credits_or_raise", args, kwargs))
            return []

        monkeypatch.setattr(voice_selection_api.settings, "auth_required", False)
        monkeypatch.setattr(voice_selection_api, "shadow_safe", record_shadow)
        monkeypatch.setattr(voice_selection_api, "reserve_credits_or_raise", record_live_reserve)
        monkeypatch.setattr(voice_selection_api, "ensure_credit_buckets_for_user", AsyncMock())
        monkeypatch.setattr(voice_selection_api, "_get_clone_cost_credits", lambda: 500)

        with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(project_dir)):
            response = _run(voice_selection_api.voice_clone_for_selection(request, "job-4", db, user))

        body = json.loads(response.body.decode("utf-8"))
        assert response.status_code == 400
        assert body["error"] == "no_source_audio"

        reserve = next(call for call in calls if call[0] == "reserve_credits_or_raise")
        assert reserve[1][0] is db
        assert reserve[2]["estimated_credits"] == 500
        assert reserve[2]["service_mode"] == "studio"
        assert "amount" not in reserve[2]
        assert "metadata_json" not in reserve[2]

        release = next(call for call in calls if call[0] == "shadow_release")
        assert release[1][0] is db
        assert release[2]["reserve_reason_code"] == reserve[2]["reason_code"]
        assert release[2]["reason_code"] == "voice_clone_no_source_audio"
        assert all(call[0] != "shadow_capture" for call in calls)
