"""Tests for the real intercept_create_job and update_source_metadata flows.

These import the real gateway functions and mock only:
- AsyncSession (DB)
- proxy_request (upstream Job API call)
- _probe_youtube_duration (external yt-dlp call)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
import uuid
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
    _build_youtube_probe_command,
    _compute_source_content_hash,
    canonicalize_youtube_source_content_hash,
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


def _make_db_session(
    *,
    active_job_count: int = 0,
    existing_job=None,
    user_for_quota=None,
    existing_display_names: set[str] | None = None,
    branch4_sequence_today: int = 0,
    credit_available: int = 1_000_000,
    user_voice_count: int = 0,
    track_user_voice_query: list | None = None,
):
    """Create a mock AsyncSession.

    Query sequence in intercept_create_job (post 2026-04-21, plan §6.2 / T0-4):
      1. COUNT active jobs (concurrency check)            → count_result
      2. SELECT jobs.display_name (existing names)        → names_result
      3. SELECT COUNT(*) WHERE display_name LIKE (branch4)→ branch4_result   ← optional
      4. SELECT Job by job_id (existing job check)        → no_job_result
      5. SELECT User by id (reserve_quota lookup)         → user_result

    Smart-only (Phase 3 plan 2026-05-17 quota preflight):
      0. COUNT user_voices (smart voice-library quota)    → user_voice_count_result

    Queries 2 + 3 are new (display_name orchestrator). Dispatch is by SQL
    content rather than call index, so adding / removing a query in either
    order doesn't silently mis-wire the old ``call_count`` scheme.

    ``track_user_voice_query`` — optional list that records the count of
    times the ``user_voices`` quota SQL ran. Tests use it to assert the
    Phase 3 gate skips the DB query when consent/admin gates are closed
    (P1 Codex finding: preflight must not fire when runtime won't clone).
    """
    db = AsyncMock()

    count_result = MagicMock()
    count_result.scalar.return_value = active_job_count

    no_job_result = MagicMock()
    no_job_result.scalar_one_or_none.return_value = existing_job

    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = user_for_quota

    names_result = MagicMock()
    names_result.all.return_value = [
        (name,) for name in (existing_display_names or set())
    ]

    branch4_result = MagicMock()
    branch4_result.scalar.return_value = branch4_sequence_today

    user_voice_count_result = MagicMock()
    user_voice_count_result.scalar.return_value = user_voice_count

    credit_bucket = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=getattr(user_for_quota, "id", "uid-1"),
        bucket_type="free",
        granted=credit_available,
        remaining=credit_available,
        reserved=0,
        expires_at=None,
        source_label="free",
        related_order_id=None,
        related_subscription_id=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    credits_result = MagicMock()
    credits_result.scalar_one_or_none.return_value = credit_bucket if credit_available > 0 else None
    credits_result.scalars.return_value.all.return_value = [credit_bucket] if credit_available > 0 else []

    no_subscription_result = MagicMock()
    no_subscription_result.scalar_one_or_none.return_value = None

    # For queries we don't explicitly classify (defensive fallback), keep
    # the old call_count sequence working for the legacy 3-query path.
    legacy_call_count = {"n": 0}

    async def smart_execute(stmt, *args, **kwargs):
        sql_text = str(stmt).lower()
        # Orchestrator queries — dispatch by the unique WHERE clauses the
        # orchestrator emits, NOT by mere presence of ``jobs.display_name``
        # (which appears in ``select(Job)`` too, because display_name is
        # one of Job's columns).
        if "display_name is not null" in sql_text:
            return names_result  # existing_names SELECT
        if "display_name like" in sql_text:
            return branch4_result  # branch-4 COUNT(*)
        if "user_voices" in sql_text:
            if track_user_voice_query is not None:
                track_user_voice_query.append(sql_text)
            return user_voice_count_result
        if "credits_buckets" in sql_text:
            return credits_result
        if "subscriptions" in sql_text:
            return no_subscription_result
        # Legacy path: active-count, existing-job, user-select in order.
        legacy_call_count["n"] += 1
        if legacy_call_count["n"] == 1:
            return count_result
        if legacy_call_count["n"] == 2:
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
    def test_youtube_source_content_hash_canonicalization_contract(self):
        cases = {
            "https://www.youtube.com/watch?v=abc&t=10s": "youtube:abc",
            "https://youtu.be/abc": "youtube:abc",
            "https://www.youtube.com/shorts/abc": "youtube:abc",
            "https://m.youtube.com/watch?v=abc": "youtube:abc",
            "https://www.youtube.com/live/abc": "youtube:abc",
            "youtu.be/abc": "youtube:abc",
        }

        for url, expected in cases.items():
            assert canonicalize_youtube_source_content_hash(url) == expected

    def test_compute_source_content_hash_rejects_path_traversal(self, tmp_path, monkeypatch):
        project_root = tmp_path / "project"
        uploads_dir = project_root / "uploads" / "uid-1"
        uploads_dir.mkdir(parents=True)
        monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(project_root))

        good_bytes = b"valid upload bytes"
        good_upload = uploads_dir / "sample.mp4"
        good_upload.write_bytes(good_bytes)
        expected_hash = f"sha256:{hashlib.sha256(good_bytes).hexdigest()}"
        assert _run(_compute_source_content_hash("local_video", "uploads/uid-1/sample.mp4")) == expected_hash

        secret = tmp_path / "secret_outside.txt"
        secret.write_bytes(b"leak")
        assert _run(_compute_source_content_hash("local_video", str(secret))) is None
        assert _run(_compute_source_content_hash("local_video", "../secret_outside.txt")) is None
        assert _run(_compute_source_content_hash("local_video", "uploads/../../secret_outside.txt")) is None

        symlink = uploads_dir / "escape.mp4"
        try:
            symlink.symlink_to(secret)
        except (OSError, NotImplementedError):
            return
        assert _run(_compute_source_content_hash("local_video", "uploads/uid-1/escape.mp4")) is None

    def test_create_job_populates_source_content_hash_youtube_and_upload(self, tmp_path, monkeypatch):
        captured: dict[str, dict] = {}
        captured_jobs: dict[str, list] = {}

        async def fake_proxy_youtube(*, request, upstream_base, strip_prefix, override_body=None):
            if override_body:
                captured["youtube"] = json.loads(override_body)
            return _upstream_success("job_youtube_hash")

        youtube_req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtu.be/abc?t=10"},
            "estimated_duration_seconds": 300,
        })
        user = _make_user(plan_code="free")
        youtube_db = _make_db_session(active_job_count=0, user_for_quota=user)

        original_add = youtube_db.add

        def capture_youtube_add(obj):
            captured_jobs.setdefault("youtube", []).append(obj)
            return original_add(obj)

        youtube_db.add = capture_youtube_add
        youtube_meta = {
            "title": "Readable Video Title",
            "upload_date": "20240501",
            "channel": "Test Channel",
            "categories": ["Education"],
            "tags": ["AI", "Voice"],
            "description": "A useful source description.\nWith a second line.",
        }
        with patch("job_intercept.proxy_request", side_effect=fake_proxy_youtube):
            with patch("job_intercept._probe_youtube_metadata", return_value=youtube_meta):
                youtube_resp = _run(intercept_create_job(youtube_req, youtube_db, user))

        assert youtube_resp.status_code == 202
        assert captured["youtube"]["source_content_hash"] == "youtube:abc"
        assert captured["youtube"]["source_video_title"] == "Readable Video Title"
        assert captured["youtube"]["source_published_at"] == "2024-05-01T00:00:00+00:00"
        assert captured["youtube"]["source_content_era"] == "2024"
        assert captured["youtube"]["source_content_tags"] == {
            "channel": "Test Channel",
            "categories": ["Education"],
            "tags": ["AI", "Voice"],
        }
        assert captured["youtube"]["source_content_summary"] == (
            "频道：Test Channel；简介：A useful source description. With a second line."
        )
        from models import Job
        youtube_job = [o for o in captured_jobs["youtube"] if isinstance(o, Job)][0]
        assert youtube_job.source_content_hash == "youtube:abc"
        assert youtube_job.title == "Readable Video Title"

        upload_bytes = b"uploaded video bytes"
        upload_path = tmp_path / "uploads" / "uid-1" / "sample.mp4"
        upload_path.parent.mkdir(parents=True)
        upload_path.write_bytes(upload_bytes)
        monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
        expected_upload_hash = f"sha256:{hashlib.sha256(upload_bytes).hexdigest()}"

        async def fake_proxy_upload(*, request, upstream_base, strip_prefix, override_body=None):
            if override_body:
                captured["upload"] = json.loads(override_body)
            return _upstream_success("job_upload_hash")

        upload_req = _make_request({
            "service_mode": "express",
            "source": {"type": "local_file", "value": "uploads/uid-1/sample.mp4"},
            "estimated_duration_seconds": 300,
        })
        upload_db = _make_db_session(active_job_count=0, user_for_quota=user)
        original_upload_add = upload_db.add

        def capture_upload_add(obj):
            captured_jobs.setdefault("upload", []).append(obj)
            return original_upload_add(obj)

        upload_db.add = capture_upload_add
        with patch("job_intercept.proxy_request", side_effect=fake_proxy_upload):
            upload_resp = _run(intercept_create_job(upload_req, upload_db, user))

        assert upload_resp.status_code == 202
        assert captured["upload"]["source"]["type"] == "local_video"
        assert captured["upload"]["source_content_hash"] == expected_upload_hash
        assert captured["upload"]["source_video_title"] == captured["upload"]["display_name"]
        upload_job = [o for o in captured_jobs["upload"] if isinstance(o, Job)][0]
        assert upload_job.source_content_hash == expected_upload_hash
        assert upload_job.title == captured["upload"]["display_name"]

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
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
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
        assert datetime.fromisoformat(captured_body["expires_at"]).tzinfo is not None
        assert captured_body["display_name"].startswith("油管视频 ")
        # user_id injected for workspace isolation
        assert captured_body["user_id"] == "uid-1"

    def test_youtube_probe_command_uses_configured_cookie_file(self, tmp_path):
        cookie_file = tmp_path / "youtube.cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        config_path = tmp_path / "autodub.local.json"
        config_path.write_text(
            json.dumps({"youtube": {"cookie_file": "youtube.cookies.txt"}}),
            encoding="utf-8",
        )

        command = _build_youtube_probe_command(
            "https://youtube.com/watch?v=abc",
            config_path=config_path,
        )

        assert "--cookies" in command
        assert "--ignore-no-formats-error" in command
        assert str(cookie_file.resolve(strict=False)) in command

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
            with patch(
                "job_intercept._probe_youtube_metadata",
                return_value={"duration": 480.0, "title": "Readable Video Title"},
            ):
                resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        assert captured_body["estimated_duration_seconds"] == 480.0
        assert captured_body["display_name"].startswith("油管视频 ")

    def test_yt_dlp_probe_exceeding_limit_is_rejected(self):
        """yt-dlp probe returns 15min → free user rejected with duration_limit."""
        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=long"},
        })
        db = _make_db_session(active_job_count=0)
        user = _make_user(plan_code="free")

        with patch("job_intercept.proxy_request", new_callable=AsyncMock):
            with patch("job_intercept._probe_youtube_metadata", return_value={"duration": 900.0}):
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
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
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
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
                resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        assert captured_body["role_snapshot"] == "admin"
        assert captured_body["tts_model"] == "speech-2.8-hd"
        assert "expires_at" not in captured_body

    def test_insufficient_credits_stops_create_and_compensates_upstream(self):
        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            return _upstream_success()

        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=abc"},
            "estimated_duration_seconds": 300,
        })
        user = _make_user(plan_code="free")
        db = _make_db_session(active_job_count=0, user_for_quota=user, credit_available=0)

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
                with patch("job_intercept._compensate_upstream_job", new_callable=AsyncMock) as compensate:
                    resp = _run(intercept_create_job(req, db, user))

        body = json.loads(resp.body)
        assert resp.status_code == 402
        assert body["error"] == "insufficient_credits"
        assert body["detail"]["required_credits"] == 50
        compensate.assert_awaited_once_with("job_test123")

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
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
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

    def test_updates_auto_placeholder_display_name_from_s2(self):
        job_mock = SimpleNamespace(
            job_id="job-1",
            source_type="youtube_url",
            source_duration_seconds=None,
            title="Old Title",
            display_name="油管视频 2026-04-25 001",
        )
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job_mock
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()

        req = MagicMock()
        req.body = AsyncMock(return_value=json.dumps({
            "display_name": "巴菲特谈接班与投资",
        }, ensure_ascii=False).encode())

        resp = _run(update_source_metadata(req, "job-1", db))
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["display_name_updated"] is True
        assert job_mock.display_name == "巴菲特谈接班与投资"

    def test_s2_display_name_does_not_override_user_name(self):
        job_mock = SimpleNamespace(
            job_id="job-1",
            source_type="youtube_url",
            source_duration_seconds=None,
            title="Old Title",
            display_name="我手动改过的名称",
        )
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job_mock
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()

        req = MagicMock()
        req.body = AsyncMock(return_value=json.dumps({
            "display_name": "巴菲特谈接班与投资",
        }, ensure_ascii=False).encode())

        resp = _run(update_source_metadata(req, "job-1", db))
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["display_name_updated"] is False
        assert job_mock.display_name == "我手动改过的名称"

    def test_s2_display_name_replaces_auto_truncated_youtube_title(self):
        job_mock = SimpleNamespace(
            job_id="job-1",
            source_type="youtube_url",
            source_duration_seconds=None,
            title="Just a regular billionaire explaining succession",
            display_name="Just a regular billionai",
        )
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job_mock
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()

        req = MagicMock()
        req.body = AsyncMock(return_value=json.dumps({
            "display_title_zh": "普通亿万富翁谈接班",
        }, ensure_ascii=False).encode())

        resp = _run(update_source_metadata(req, "job-1", db))
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["display_name_updated"] is True
        assert job_mock.display_name == "普通亿万富翁谈接班"

    def test_s2_display_name_replaces_youtube_video_id_fallback(self):
        job_mock = SimpleNamespace(
            job_id="job-1",
            source_type="youtube_url",
            source_ref="https://youtube.com/watch?v=P9YTKb5PgR0",
            source_duration_seconds=None,
            title="",
            display_name="P9YTKb5PgR0",
        )
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job_mock
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()

        req = MagicMock()
        req.body = AsyncMock(return_value=json.dumps({
            "display_name": "普通亿万富翁谈接班",
        }, ensure_ascii=False).encode())

        resp = _run(update_source_metadata(req, "job-1", db))
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["display_name_updated"] is True
        assert job_mock.display_name == "普通亿万富翁谈接班"

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
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
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
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
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
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
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
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
                with patch.object(admin_mod, "load_settings", mock_load):
                    resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        assert captured_body["tts_provider"] == "volcengine"
        assert captured_body["tts_model"] is None
        assert captured_body["voice_clone_enabled"] is False


# ===================================================================
# V3-6: quality_tier truth chain tests
# ===================================================================


class TestQualityTierTruthChain:
    """Verify the quality_tier single-truth-source chain:
    compute_job_policy → upstream payload → metering_snapshot → settle readback.
    """

    def test_create_path_injects_quality_tier_into_upstream_payload(self):
        """intercept_create_job puts quality_tier from policy into upstream payload."""
        captured_body = {}

        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            if override_body:
                captured_body.update(json.loads(override_body))
            return _upstream_success()

        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=qt"},
            "estimated_duration_seconds": 120,
        })
        user = _make_user(plan_code="free")
        db = _make_db_session(active_job_count=0, user_for_quota=user)

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
                resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        # quality_tier must be present in upstream payload from policy
        assert captured_body.get("quality_tier") == "standard"

    def test_create_path_writes_quality_tier_into_metering_snapshot(self):
        """Shadow metering snapshot must contain quality_tier from policy."""
        captured_jobs = []

        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            return _upstream_success()

        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=qt2"},
            "estimated_duration_seconds": 180,
        })
        user = _make_user(plan_code="free")
        db = _make_db_session(active_job_count=0, user_for_quota=user)

        original_add = db.add
        def capture_add(obj):
            captured_jobs.append(obj)
            return original_add(obj)
        db.add = capture_add

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
                resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        # Find the Job object that was added to DB
        from models import Job
        job_objs = [o for o in captured_jobs if isinstance(o, Job)]
        assert len(job_objs) >= 1
        job = job_objs[0]
        # metering_snapshot must exist (not None) and contain quality_tier
        assert job.metering_snapshot is not None, "metering_snapshot must be written at create time"
        assert job.metering_snapshot["quality_tier"] == "standard"

    def test_create_reserve_consumes_policy_tier(self):
        """Shadow reserve must call estimate_credits with the policy's quality_tier."""
        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            return _upstream_success()

        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=qt3"},
            "estimated_duration_seconds": 300,
        })
        user = _make_user(plan_code="free")
        db = _make_db_session(active_job_count=0, user_for_quota=user)

        estimate_calls = []
        original_estimate = __import__("credits_service").estimate_credits

        def capture_estimate(minutes, service_mode="express", quality_tier="standard"):
            estimate_calls.append({"minutes": minutes, "service_mode": service_mode, "quality_tier": quality_tier})
            return original_estimate(minutes, service_mode, quality_tier)

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
                with patch("job_intercept.estimate_credits", side_effect=capture_estimate):
                    resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        # estimate_credits must have been called with quality_tier from policy
        assert any(c["quality_tier"] == "standard" for c in estimate_calls)

    def test_list_jobs_routes_terminal_settlement_through_mirror(self):
        """List-jobs must route terminal settlement through the shared mirror.

        The old list-jobs path had inline credit settlement logic. The current
        invariant is stronger: list-jobs, detail polling, and the R2 sweeper all
        use ``mirror_job_terminal_state`` so status, quota, and credits settle
        behind one idempotent entrypoint.
        """
        from job_intercept import intercept_list_jobs

        # Create a mock job with non-default quality_tier in snapshot
        mock_job = SimpleNamespace(
            job_id="job-settle-qt",
            user_id="uid-1",
            status="running",  # old status (not terminal)
            current_stage="processing",
            source_duration_seconds=300.0,
            estimated_minutes=5.0,
            actual_minutes=None,
            service_mode="studio",
            metering_snapshot={
                "credits_estimated": 75,
                "quality_tier": "high",  # non-default for test deniability
            },
            quota_state="reserved",
        )

        # Mock DB: user jobs query returns our job; upstream returns it as succeeded
        db = AsyncMock()
        call_n = {"n": 0}

        async def smart_execute(*args, **kwargs):
            call_n["n"] += 1
            r = MagicMock()
            if call_n["n"] == 1:
                # all_db_job_ids
                r.all.return_value = [("job-settle-qt",)]
            elif call_n["n"] == 2:
                # user_job_ids
                r.all.return_value = [("job-settle-qt",)]
            elif call_n["n"] == 3:
                # select Job for status sync
                r.scalar_one_or_none.return_value = mock_job
            else:
                r.scalar_one_or_none.return_value = None
            return r

        db.execute = smart_execute
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        user = _make_user()

        # Upstream response: job succeeded
        upstream_body = json.dumps({
            "jobs": [{
                "job_id": "job-settle-qt",
                "status": "succeeded",
                "current_stage": None,
            }]
        }).encode()

        req = MagicMock()
        req.headers = {}
        req.method = "GET"
        req.url = MagicMock(); req.url.path = "/job-api/jobs"
        req.query_params = {}

        async def fake_proxy(*, request, upstream_base, strip_prefix, override_query=None):
            return FastAPIResponse(
                content=upstream_body, status_code=200,
                headers={"content-type": "application/json"},
            )

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept.mirror_job_terminal_state", new_callable=AsyncMock) as mirror:
                with patch("job_intercept.settings") as mock_settings:
                    mock_settings.auth_required = True
                    resp = _run(intercept_list_jobs(req, db, user))

        assert resp.status_code == 200
        mirror.assert_awaited_once()
        upstream_record = mirror.await_args.args[2]
        assert upstream_record.job_id == "job-settle-qt"
        assert upstream_record.status == "succeeded"


# ===================================================================
# Regression: intercept_create_job must not shadow module-level
# sqlalchemy / models imports via inner re-imports (2026-05-16 P0).
#
# Background: Fix C added ``from sqlalchemy import func, select`` and
# ``from models import UserVoice`` inside a try-block guarded by
# ``if service_mode == "smart" and user and not is_admin:``. Python
# treats any name assigned anywhere in a function as function-local
# throughout, so for paths that skip that branch (admin smart /
# studio / express / no-user), the later ``select(Job)`` PG insert
# at line ~1213 raises UnboundLocalError. The PG insert is wrapped
# in ``except Exception`` and logged as "Failed to record job ... in
# DB", so the upstream JSON-store entry survives but no PG row is
# created → user task list looks empty → admin task management
# shows orphan jobs with empty owner fields.
#
# This AST-level guard catches any future re-introduction of the
# same shadowing pattern in intercept_create_job. The shadow itself
# is what breaks the scope; the runtime symptom is path-dependent
# and hard to reproduce in unit tests without full DB mocks.
# ===================================================================

class TestInterceptCreateJobImportShadowingGuard:
    """AST regression for the 2026-05-16 intercept_create_job orphan bug."""

    _SHADOWED_NAMES = frozenset({"select", "func", "UserVoice", "Job", "User"})

    def _intercept_create_job_function_node(self):
        import ast
        import pathlib

        src = pathlib.Path(_gateway_dir, "job_intercept.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "intercept_create_job"
            ):
                return node
        raise AssertionError(
            "intercept_create_job not found in gateway/job_intercept.py"
        )

    def test_no_inner_reimport_of_module_level_sqlalchemy_names(self):
        import ast

        node = self._intercept_create_job_function_node()
        offenders: list[str] = []
        for child in ast.walk(node):
            if isinstance(child, ast.ImportFrom):
                if child.module in {"sqlalchemy", "models"}:
                    names = [alias.name for alias in child.names]
                    bad = [n for n in names if n in self._SHADOWED_NAMES]
                    if bad:
                        offenders.append(
                            f"line {child.lineno}: from {child.module} "
                            f"import {', '.join(names)} (shadows {bad})"
                        )

        assert not offenders, (
            "intercept_create_job re-imports module-level names from "
            "sqlalchemy/models inside the function body. This shadows the "
            "top-level imports and makes the names function-local "
            "throughout, so any code path that skips the import statement "
            "(e.g. admin smart, studio, express) hits UnboundLocalError "
            "at the next select(Job) call. Move the imports to module top "
            "OR rename via ``import X as _local_X``. Offenders:\n  - "
            + "\n  - ".join(offenders)
        )


# ===================================================================
# Phase 3 (plan 2026-05-17-user-voice-candidate-first §Consent × Admin
# 决策矩阵) — smart voice-library quota preflight must gate on BOTH
# consent.auto_voice_clone AND admin.smart_auto_clone_enabled. When
# either gate is closed, the runtime falls to REUSE or PRESET without
# consuming a clone slot, so the near-cap preflight rejection becomes
# a false negative product-level inconsistency (admin says "no new
# clones", system still blocks user for "clone quota exhausted").
# ===================================================================


class TestSmartVoiceQuotaPreflightGates:
    """Phase 3 follow-up: quota preflight respects consent + admin gates."""

    _FULL_CONSENT_BOTH_ALLOW = {
        "auto_voice_clone": True,
        "auto_retranslate": True,
        "auto_retts": True,
        "auto_multimodal_verification": True,
        "no_extra_charge_without_confirmation": True,
        "on_budget_exhausted": "degraded_delivery_with_report",
    }

    _FULL_CONSENT_USER_BLOCKS_CLONE = {
        "auto_voice_clone": False,
        "auto_retranslate": True,
        "auto_retts": True,
        "auto_multimodal_verification": True,
        "no_extra_charge_without_confirmation": True,
        "on_budget_exhausted": "degraded_delivery_with_report",
    }

    # In-test plan_gate that lets ``plus`` use ``smart``. The runtime
    # pricing payload in the test sandbox doesn't include smart in
    # plus.allowed_service_modes (pricing_runtime fallback predates
    # PR#3C-b3g), so we override the gate to mirror the canonical
    # plan_catalog.py PLANS definition that does include smart.
    _PLUS_SMART_GATE = {
        "max_duration_minutes": 45,
        "max_concurrent_jobs": 3,
        "allowed_service_modes": ["express", "studio", "smart"],
    }

    def _smart_request_body(self, consent: dict) -> dict:
        return {
            "service_mode": "smart",
            "source": {
                "type": "youtube_url",
                "value": "https://youtube.com/watch?v=smart_test",
            },
            "estimated_duration_seconds": 120,
            "smart_consent": consent,
        }

    def test_create_smart_job_skips_voice_quota_preflight_when_admin_auto_clone_disabled(self):
        """Admin gate False → preflight must NOT query user_voices and
        MUST NOT reject near-cap users with smart_voice_library_at_safety_water_mark.

        Setup matches the breakage in plan §Consent × Admin 决策矩阵:
        - user_voice_count=28 (remaining=2 < water_mark=3 → would reject
          if preflight fired)
        - admin.smart_auto_clone_enabled=False (kill-switch for new clones)
        - consent.auto_voice_clone=True (user is OK with cloning)
        Runtime would PRESET safely (no clone slot consumed). Preflight
        must respect the admin gate and skip.
        """
        import admin_settings as admin_mod

        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            return _upstream_success()

        track_queries: list = []
        req = _make_request(
            self._smart_request_body(dict(self._FULL_CONSENT_BOTH_ALLOW))
        )
        user = _make_user(plan_code="plus")
        db = _make_db_session(
            active_job_count=0,
            user_for_quota=user,
            user_voice_count=28,
            track_user_voice_query=track_queries,
        )

        original_load = admin_mod.load_settings

        def mock_load():
            s = original_load()
            s.smart_auto_clone_enabled = False
            return s

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
                with patch.object(admin_mod, "load_settings", mock_load):
                    with patch(
                        "plan_catalog.get_effective_plan_gate",
                        return_value=self._PLUS_SMART_GATE,
                    ):
                        resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202, (
            f"Expected job creation success when admin disables auto-clone; "
            f"got {resp.status_code}, body={resp.body!r}. Preflight must "
            f"skip when admin.smart_auto_clone_enabled=False (plan §Consent × "
            f"Admin 决策矩阵 rows 5/6/7/8)."
        )
        assert track_queries == [], (
            f"user_voices quota query MUST be skipped when admin disables "
            f"auto-clone. The DB roundtrip is wasteful when the runtime "
            f"won't clone. Got query log: {track_queries!r}"
        )

    def test_create_smart_job_skips_voice_quota_preflight_when_consent_disables_auto_clone(self):
        """User gate False → preflight must skip.

        Setup:
        - user_voice_count=28 (would reject if preflight fired)
        - admin.smart_auto_clone_enabled=True (admin allows clones)
        - consent.auto_voice_clone=False (user opted out of cloning)
        Runtime falls to REUSE/PRESET without consuming a clone slot.
        """
        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            return _upstream_success()

        track_queries: list = []
        req = _make_request(
            self._smart_request_body(dict(self._FULL_CONSENT_USER_BLOCKS_CLONE))
        )
        user = _make_user(plan_code="plus")
        db = _make_db_session(
            active_job_count=0,
            user_for_quota=user,
            user_voice_count=28,
            track_user_voice_query=track_queries,
        )

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
                with patch(
                    "plan_catalog.get_effective_plan_gate",
                    return_value=self._PLUS_SMART_GATE,
                ):
                    resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202, (
            f"Expected job creation success when consent.auto_voice_clone "
            f"is False; got {resp.status_code}, body={resp.body!r}. "
            f"Preflight must skip when user did not consent to cloning "
            f"(plan §Consent × Admin 决策矩阵 rows 2/3)."
        )
        assert track_queries == [], (
            f"user_voices quota query MUST be skipped when consent denies "
            f"auto-clone. Got query log: {track_queries!r}"
        )

    def test_create_smart_job_still_quota_blocks_when_both_gates_allow_clone(self):
        """Existing behavior preserved: when BOTH gates allow new clone
        and the user is near cap, the preflight still rejects so the
        user can clean up the library before spending S0/S1/S2 budget.
        """
        async def fake_proxy(*, request, upstream_base, strip_prefix, override_body=None):
            return _upstream_success()

        track_queries: list = []
        req = _make_request(
            self._smart_request_body(dict(self._FULL_CONSENT_BOTH_ALLOW))
        )
        user = _make_user(plan_code="plus")
        db = _make_db_session(
            active_job_count=0,
            user_for_quota=user,
            user_voice_count=28,
            track_user_voice_query=track_queries,
        )

        # Both gates open (admin default is True, consent set True).
        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
                with patch(
                    "plan_catalog.get_effective_plan_gate",
                    return_value=self._PLUS_SMART_GATE,
                ):
                    resp = _run(intercept_create_job(req, db, user))

        body = json.loads(resp.body)
        assert resp.status_code == 400, (
            f"Expected 400 smart_voice_library_at_safety_water_mark when "
            f"both gates allow clone and user is near cap; got "
            f"{resp.status_code}, body={body!r}"
        )
        assert body["error"] == "smart_voice_library_at_safety_water_mark"
        # Sanity: detail carries the quota arithmetic so frontend can
        # render an actionable message.
        assert body["detail"]["quota_used"] == 28
        assert body["detail"]["quota_cap"] == 30
        assert body["detail"]["remaining"] == 2
        assert body["detail"]["water_mark"] == 3
        assert track_queries, (
            "user_voices quota query MUST run when both gates allow clone."
        )
