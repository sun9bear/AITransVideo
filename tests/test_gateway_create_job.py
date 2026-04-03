"""Tests for the real intercept_create_job and update_source_metadata flows.

These import the real gateway functions and mock only:
- AsyncSession (DB)
- proxy_request (upstream Job API call)
- _probe_youtube_duration (external yt-dlp call)
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub database module
_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from fastapi import Response as FastAPIResponse  # noqa: E402
from job_intercept import (  # noqa: E402
    intercept_create_job,
    update_source_metadata,
    _error_response,
    PLAN_CATALOG,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_user(*, role="user", plan_code="free"):
    return SimpleNamespace(
        id="uid-1", email="u@test.com", display_name="Test",
        role=role, plan_code=plan_code,
        free_jobs_quota_total=5, free_jobs_quota_used=0,
    )


def _make_request(body: dict) -> MagicMock:
    """Create a mock FastAPI Request with given JSON body."""
    req = MagicMock()
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req.body = AsyncMock(return_value=encoded)
    req.headers = {"content-type": "application/json"}
    req.method = "POST"
    req.url = MagicMock()
    req.url.path = "/job-api/jobs"
    req.query_params = {}
    return req


def _make_db_session(*, active_job_count: int = 0, existing_job=None, user_for_quota=None):
    """Create a mock AsyncSession.

    Query sequence in intercept_create_job:
      1. COUNT active jobs (concurrency check)
      2. SELECT Job by job_id (existing job check)
      3. SELECT User by id (reserve_quota user lookup)
    """
    db = AsyncMock()

    count_result = MagicMock()
    count_result.scalar.return_value = active_job_count

    no_job_result = MagicMock()
    no_job_result.scalar_one_or_none.return_value = existing_job

    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = user_for_quota

    call_count = {"n": 0}

    async def smart_execute(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return count_result
        if call_count["n"] == 2:
            return no_job_result
        return user_result

    db.execute = smart_execute
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _upstream_success(job_id="job_test123"):
    """Simulate a successful upstream response."""
    body = json.dumps({"job_id": job_id, "status": "queued"}).encode()
    return FastAPIResponse(content=body, status_code=202,
                           headers={"content-type": "application/json"})


def _upstream_conflict():
    body = json.dumps({"error": "job still active"}).encode()
    return FastAPIResponse(content=body, status_code=409,
                           headers={"content-type": "application/json"})


# ===================================================================
# intercept_create_job — rejection paths
# ===================================================================

class TestCreateJobRejections:
    def test_service_mode_not_allowed_for_free(self):
        req = _make_request({
            "service_mode": "studio",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
        })
        db = _make_db_session()
        user = _make_user(plan_code="free")

        with patch("job_intercept.proxy_request", new_callable=AsyncMock):
            resp = _run(intercept_create_job(req, db, user))

        body = json.loads(resp.body)
        assert resp.status_code == 403
        assert body["error"] == "service_mode_not_allowed"
        assert "studio" in body["detail"]["requested_mode"]

    def test_concurrent_limit_for_free(self):
        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
        })
        db = _make_db_session(active_job_count=1)
        user = _make_user(plan_code="free")  # max_concurrent = 1

        with patch("job_intercept.proxy_request", new_callable=AsyncMock):
            resp = _run(intercept_create_job(req, db, user))

        body = json.loads(resp.body)
        assert resp.status_code == 409
        assert body["error"] == "concurrent_limit"

    def test_duration_limit_rejects_long_video(self):
        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
            "estimated_duration_seconds": 900,  # 15 min > 10 min free limit
        })
        db = _make_db_session(active_job_count=0)
        user = _make_user(plan_code="free")

        with patch("job_intercept.proxy_request", new_callable=AsyncMock):
            resp = _run(intercept_create_job(req, db, user))

        body = json.loads(resp.body)
        assert resp.status_code == 403
        assert body["error"] == "duration_limit"

    def test_invalid_source_missing_type(self):
        req = _make_request({
            "service_mode": "express",
            "source": {"type": "", "value": "something"},
        })
        db = _make_db_session()
        user = _make_user()

        with patch("job_intercept.proxy_request", new_callable=AsyncMock):
            resp = _run(intercept_create_job(req, db, user))

        body = json.loads(resp.body)
        assert resp.status_code == 400
        assert body["error"] == "invalid_source"

    def test_invalid_source_missing_value(self):
        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": ""},
        })
        db = _make_db_session()
        user = _make_user()

        with patch("job_intercept.proxy_request", new_callable=AsyncMock):
            resp = _run(intercept_create_job(req, db, user))

        body = json.loads(resp.body)
        assert resp.status_code == 400
        assert body["error"] == "invalid_source"


# ===================================================================
# intercept_create_job — success path, snapshot in downstream payload
# ===================================================================

class TestCreateJobSuccess:
    def test_snapshot_fields_injected_into_upstream_payload(self):
        """Verify the full snapshot is injected into the payload sent to Job API."""
        captured_body = {}

        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            if override_body:
                captured_body.update(json.loads(override_body))
            return _upstream_success()

        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=abc"},
            "estimated_duration_seconds": 300,
        })
        user = _make_user(plan_code="free")
        db = _make_db_session(active_job_count=0, user_for_quota=user)

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_duration", return_value=None):
                resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        # Verify snapshot fields in upstream payload
        assert captured_body["service_mode"] == "express"
        assert captured_body["tts_provider"] == "cosyvoice"
        assert captured_body["tts_model"] == "cosyvoice-v3-flash"
        assert captured_body["requires_review"] is False
        assert captured_body["voice_clone_enabled"] is False
        assert captured_body["voice_strategy"] == "preset_mapping"
        assert captured_body["plan_code_snapshot"] == "free"
        assert captured_body["role_snapshot"] == "user"
        assert captured_body["estimated_duration_seconds"] == 300
        assert captured_body["quota_state"] == "none"
        assert captured_body["create_idempotency_key"] is not None
        # user_id injected for workspace isolation
        assert captured_body["user_id"] == "uid-1"

    def test_yt_dlp_probe_populates_estimated_duration(self):
        """When frontend doesn't send duration, yt-dlp probe fills it."""
        captured_body = {}

        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            if override_body:
                captured_body.update(json.loads(override_body))
            return _upstream_success()

        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=abc"},
            # no estimated_duration_seconds
        })
        user = _make_user(plan_code="free")
        db = _make_db_session(active_job_count=0, user_for_quota=user)

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_duration", return_value=480.0):
                resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        assert captured_body["estimated_duration_seconds"] == 480.0

    def test_yt_dlp_probe_exceeding_limit_is_rejected(self):
        """yt-dlp probe returns 15min → free user rejected with duration_limit."""
        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=long"},
        })
        db = _make_db_session(active_job_count=0)
        user = _make_user(plan_code="free")

        with patch("job_intercept.proxy_request", new_callable=AsyncMock):
            with patch("job_intercept._probe_youtube_duration", return_value=900.0):
                resp = _run(intercept_create_job(req, db, user))

        body = json.loads(resp.body)
        assert resp.status_code == 403
        assert body["error"] == "duration_limit"

    def test_upstream_conflict_returns_structured_error(self):
        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
        })
        db = _make_db_session(active_job_count=0)
        user = _make_user()

        async def fake_proxy(**kw):
            return _upstream_conflict()

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_duration", return_value=None):
                resp = _run(intercept_create_job(req, db, user))

        body = json.loads(resp.body)
        assert resp.status_code == 409
        assert body["error"] == "job_create_conflict"

    def test_admin_bypasses_all_checks(self):
        captured_body = {}

        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            if override_body:
                captured_body.update(json.loads(override_body))
            return _upstream_success()

        req = _make_request({
            "service_mode": "studio",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
            "estimated_duration_seconds": 99999,  # way over any limit
        })
        user = _make_user(role="admin", plan_code="free")
        db = _make_db_session(active_job_count=99, user_for_quota=user)

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_duration", return_value=None):
                resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        assert captured_body["role_snapshot"] == "admin"
        assert captured_body["tts_model"] == "speech-2.8-hd"

    def test_idempotency_key_from_frontend_is_preserved(self):
        captured_body = {}

        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            if override_body:
                captured_body.update(json.loads(override_body))
            return _upstream_success()

        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
            "create_idempotency_key": "frontend-uuid-123",
        })
        user = _make_user()
        db = _make_db_session(active_job_count=0, user_for_quota=user)

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_duration", return_value=None):
                _run(intercept_create_job(req, db, user))

        assert captured_body["create_idempotency_key"] == "frontend-uuid-123"

    def test_local_file_normalized_to_local_video(self):
        """Frontend sends source.type='local_file'; Gateway normalizes to 'local_video'."""
        captured_body = {}

        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            if override_body:
                captured_body.update(json.loads(override_body))
            return _upstream_success()

        req = _make_request({
            "service_mode": "express",
            "source": {"type": "local_file", "value": "/uploads/video.mp4"},
        })
        user = _make_user(plan_code="free")
        db = _make_db_session(active_job_count=0, user_for_quota=user)

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        # source.type in the upstream payload is normalized
        assert captured_body["source"]["type"] == "local_video"
        # Full snapshot still present
        assert captured_body["service_mode"] == "express"
        assert captured_body["tts_provider"] == "cosyvoice"
        assert captured_body["plan_code_snapshot"] == "free"
        assert captured_body["quota_state"] == "none"
        assert captured_body["create_idempotency_key"] is not None


# ===================================================================
# update_source_metadata
# ===================================================================

class TestUpdateSourceMetadata:
    def test_updates_duration_and_title(self):
        job_mock = SimpleNamespace(
            job_id="job-1", source_duration_seconds=None, title="Old Title",
        )
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job_mock
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()

        req = MagicMock()
        req.body = AsyncMock(return_value=json.dumps({
            "source_duration_seconds": 532.0,
            "title": "New Title",
        }).encode())

        resp = _run(update_source_metadata(req, "job-1", db))
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert job_mock.source_duration_seconds == 532.0
        assert job_mock.title == "New Title"

    def test_missing_job_returns_ok_with_note(self):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=result)

        req = MagicMock()
        req.body = AsyncMock(return_value=json.dumps({
            "source_duration_seconds": 100.0,
        }).encode())

        resp = _run(update_source_metadata(req, "nonexistent", db))
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert "not in gateway DB" in body.get("note", "")

    def test_no_fields_returns_400(self):
        db = AsyncMock()
        req = MagicMock()
        req.body = AsyncMock(return_value=json.dumps({}).encode())

        resp = _run(update_source_metadata(req, "job-1", db))
        body = json.loads(resp.body)
        assert resp.status_code == 400
        assert body["error"] == "no_update_fields"


# ===================================================================
# Job API → Service → Store round-trip (snapshot fields)
# ===================================================================

class TestJobServiceStoreRoundTrip:
    """Verify snapshot fields survive the full submit_job → save → load path."""

    def test_snapshot_fields_persisted_in_store(self, tmp_path):
        from services.jobs.store import JobStore
        from services.jobs.models import JobRecord
        from services.jobs.service import JobService

        store = JobStore(tmp_path / "jobs")
        runner = MagicMock()
        runner.start = MagicMock()
        svc = JobService(store=store, runner=runner)

        job = svc.submit_job(
            source_type="youtube_url",
            source_ref="https://youtube.com/watch?v=test",
            service_mode="studio",
            tts_provider="minimax",
            tts_model="speech-2.8-hd",
            requires_review=True,
            voice_clone_enabled=True,
            voice_strategy="user_selected",
            plan_code_snapshot="pro",
            role_snapshot="user",
            source_duration_seconds=532.0,
            estimated_duration_seconds=540.0,
            quota_cost=1,
            quota_state="reserved",
            create_idempotency_key="idem-test-001",
            user_id="42",
            source_content_hash="sha256:abc123",
        )

        # Load back from disk
        loaded = store.load_job(job.job_id)
        assert loaded is not None
        assert loaded.service_mode == "studio"
        assert loaded.tts_provider == "minimax"
        assert loaded.tts_model == "speech-2.8-hd"
        assert loaded.requires_review is True
        assert loaded.voice_clone_enabled is True
        assert loaded.voice_strategy == "user_selected"
        assert loaded.plan_code_snapshot == "pro"
        assert loaded.role_snapshot == "user"
        assert loaded.source_duration_seconds == 532.0
        assert loaded.estimated_duration_seconds == 540.0
        assert loaded.quota_cost == 1
        assert loaded.quota_state == "reserved"
        assert loaded.create_idempotency_key == "idem-test-001"
        assert loaded.user_id == "42"
        assert loaded.workspace_dir == "projects/42/" + job.job_id
        assert loaded.source_content_hash == "sha256:abc123"


# ===================================================================
# Admin-selected TTS provider flows through to upstream payload
# ===================================================================

class TestAdminSettingsTTSProvider:
    def test_express_volcengine_injected_into_upstream(self):
        """When admin sets express_tts_provider=volcengine, upstream payload has tts_provider=volcengine."""
        import admin_settings as admin_mod

        captured_body = {}

        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            if override_body:
                captured_body.update(json.loads(override_body))
            return _upstream_success()

        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=abc"},
            "estimated_duration_seconds": 120,
        })
        user = _make_user(plan_code="free")
        db = _make_db_session(active_job_count=0, user_for_quota=user)

        original_load = admin_mod.load_settings

        def mock_load():
            s = original_load()
            s.express_tts_provider = "volcengine"
            return s

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_duration", return_value=None):
                with patch.object(admin_mod, "load_settings", mock_load):
                    resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        assert captured_body["tts_provider"] == "volcengine"

    def test_studio_volcengine_injected_into_upstream(self):
        """When admin sets studio_tts_provider=volcengine, upstream payload has tts_provider=volcengine."""
        import admin_settings as admin_mod

        captured_body = {}

        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            if override_body:
                captured_body.update(json.loads(override_body))
            return _upstream_success()

        req = _make_request({
            "service_mode": "studio",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=abc"},
            "estimated_duration_seconds": 120,
        })
        user = _make_user(plan_code="plus")
        db = _make_db_session(active_job_count=0, user_for_quota=user)

        original_load = admin_mod.load_settings

        def mock_load():
            s = original_load()
            s.studio_tts_provider = "volcengine"
            return s

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_duration", return_value=None):
                with patch.object(admin_mod, "load_settings", mock_load):
                    resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        assert captured_body["tts_provider"] == "volcengine"


# ===================================================================
# B2: volcengine dual-mode snapshot in upstream payload
# ===================================================================

class TestVolcengineDualModeSnapshot:
    def test_express_volcengine_snapshot_has_seed_tts_1_1(self):
        """express + volcengine → tts_model='seed-tts-1.1', voice_clone_enabled=False."""
        import admin_settings as admin_mod

        captured_body = {}

        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            if override_body:
                captured_body.update(json.loads(override_body))
            return _upstream_success()

        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=abc"},
        })
        user = _make_user(plan_code="free")
        db = _make_db_session(active_job_count=0, user_for_quota=user)
        original_load = admin_mod.load_settings

        def mock_load():
            s = original_load()
            s.express_tts_provider = "volcengine"
            return s

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_duration", return_value=None):
                with patch.object(admin_mod, "load_settings", mock_load):
                    resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        assert captured_body["tts_provider"] == "volcengine"
        assert captured_body["tts_model"] == "seed-tts-1.1"
        assert captured_body["voice_clone_enabled"] is False

    def test_studio_volcengine_snapshot_has_none_model(self):
        """studio + volcengine → tts_model is None, voice_clone_enabled=False."""
        import admin_settings as admin_mod

        captured_body = {}

        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            if override_body:
                captured_body.update(json.loads(override_body))
            return _upstream_success()

        req = _make_request({
            "service_mode": "studio",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=abc"},
        })
        user = _make_user(plan_code="plus")
        db = _make_db_session(active_job_count=0, user_for_quota=user)
        original_load = admin_mod.load_settings

        def mock_load():
            s = original_load()
            s.studio_tts_provider = "volcengine"
            return s

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_duration", return_value=None):
                with patch.object(admin_mod, "load_settings", mock_load):
                    resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        assert captured_body["tts_provider"] == "volcengine"
        assert captured_body["tts_model"] is None
        assert captured_body["voice_clone_enabled"] is False
