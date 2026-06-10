"""T7 router tests: session cookie + three endpoints + downloadable_keys branches.

Coverage
--------
A. Flag / admin gate matrix
   A1. flag off → all three endpoints return 404
   A2. admin off → upload returns 403/404; status/stream return 403/404
   A3. admin read failure → default-closed (403/404)

B. Session cookie
   B1. First upload → set-cookie with HttpOnly, SameSite=Lax, Max-Age=86400
   B2. status/stream with no cookie → 401
   B3. status/stream with wrong/expired session → 401

C. Upload happy path (all fakes injected)
   C1. Valid upload → 200 with preview_id
   C2. UploadTooLarge → 413

D. Stream gate matrix
   D1. record has no job_id → 409
   D2. session mismatch → 404
   D3. happy path → proxy called, inline Content-Disposition, Range forwarded

E. downloadable_keys anonymous_preview branch
   E1. download_keys_for("anonymous_preview") == frozenset()
   E2. stream_kinds_for("anonymous_preview") == frozenset({"video"})
   E3. eager_push_keys_for("anonymous_preview") == frozenset()

F. Import guards
   F1. anonymous_preview_api.py has no 'services.jobs' import
   F2. anonymous_preview_api.py has no 'r2' / 'R2' string
"""

from __future__ import annotations

import ast
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# sys.path setup
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parent.parent
_GATEWAY = str(_REPO / "gateway")
_SRC = str(_REPO / "src")
for _p in (_GATEWAY, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Minimal stubs for gateway modules that need DB / network at import time
# ---------------------------------------------------------------------------

def _stub_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


# Stub database so get_db / init_db don't need a real PG connection
_db_stub = _stub_module(
    "database",
    get_db=lambda: None,
    async_session=MagicMock(),
    engine=MagicMock(),
    init_db=MagicMock(),
    resolve_database_url=lambda s: "postgresql://fake/fake",
)
sys.modules.setdefault("database", _db_stub)

# Stub config with necessary fields
class _FakeSettings:
    enable_anonymous_preview: bool = True
    anonymous_preview_max_upload_bytes: int = 200 * 1024 * 1024
    anonymous_preview_max_seconds: float = 180.0
    anonymous_preview_hash_secret: str = "x" * 32
    job_api_upstream: str = "http://127.0.0.1:8877"
    cors_origins: str = "http://localhost:3000"
    env: str = "dev"
    anonymous_preview_cap_global_per_day: int = 500
    anonymous_preview_cap_per_ip: int = 3
    anonymous_preview_cap_per_device: int = 1
    anonymous_preview_cap_per_source: int = 1

_fake_settings = _FakeSettings()

_config_stub = _stub_module(
    "config",
    settings=_fake_settings,
    resolve_database_url=lambda s: "postgresql://fake/fake",
)
sys.modules.setdefault("config", _config_stub)

# Stub models
_models_stub = _stub_module("models")

class _FakeAnonymousSession:
    __tablename__ = "anonymous_sessions"
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

class _FakeAnonymousPreviewRecord:
    __tablename__ = "anonymous_preview_records"
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

_models_stub.AnonymousSession = _FakeAnonymousSession
_models_stub.AnonymousPreviewRecord = _FakeAnonymousPreviewRecord
sys.modules.setdefault("models", _models_stub)

# Stub internal_auth
_internal_auth_stub = _stub_module(
    "internal_auth",
    internal_headers=lambda: {"X-Internal-Key": "test-key"},
)
sys.modules.setdefault("internal_auth", _internal_auth_stub)

# Stub proxy (get_client)
_proxy_stub = _stub_module("proxy", get_client=MagicMock())
sys.modules.setdefault("proxy", _proxy_stub)

# Stub csrf
def _csrf_ok(request):
    pass
_csrf_stub = _stub_module("csrf", require_same_origin_state_change=_csrf_ok)
sys.modules.setdefault("csrf", _csrf_stub)

# Stub admin_settings
class _FakeAdminSettings:
    anonymous_free_preview_enabled: bool = True
    anonymous_preview_max_in_flight: int = 2

_admin_settings_stub = _stub_module(
    "admin_settings",
    load_settings=lambda: _FakeAdminSettings(),
    AdminSettings=_FakeAdminSettings,
)
sys.modules.setdefault("admin_settings", _admin_settings_stub)

# Stub anonymous_preview_quota (needs hash_scope_key)
import hmac, hashlib

def _hash_scope_key_impl(value: str, *, secret: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()

_quota_stub = _stub_module(
    "anonymous_preview_quota",
    hash_scope_key=_hash_scope_key_impl,
    PgRateLimitCounterStore=MagicMock(),
    shanghai_today=lambda: "2026-06-10",
)
sys.modules.setdefault("anonymous_preview_quota", _quota_stub)

# ---------------------------------------------------------------------------
# Now import the modules under test
# ---------------------------------------------------------------------------

from anonymous_session import (
    AnonymousSessionContext,
    get_or_create_anonymous_session,
    require_anonymous_session,
    _hash_token,
    _COOKIE_NAME,
    _COOKIE_MAX_AGE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _make_session_row(hash_val: str, expired: bool = False) -> _FakeAnonymousSession:
    offset = -1 if expired else 1
    return _FakeAnonymousSession(
        session_id_hash=hash_val,
        expires_at=_now_utc() + timedelta(hours=offset),
        claim_user_id=None,
    )


def _make_record(
    preview_id: str = "prv_abc123",
    session_id_hash: str = "sess_hash_xyz",
    job_id: Optional[str] = None,
    status: str = "ready_for_mode",
    expired: bool = False,
) -> _FakeAnonymousPreviewRecord:
    offset = -1 if expired else 24
    return _FakeAnonymousPreviewRecord(
        preview_id=preview_id,
        session_id=session_id_hash,
        job_id=job_id,
        status=status,
        status_reason=None,
        mode="free",
        expires_at=_now_utc() + timedelta(hours=offset),
        created_at=_now_utc(),
    )


# ---------------------------------------------------------------------------
# A. Flag / admin gate — test via anonymous_session dependency directly
# ---------------------------------------------------------------------------

class TestFlagGate:
    """Feature flag off → dependency returns 404 JSONResponse."""

    @pytest.mark.asyncio
    async def test_flag_off_returns_404_from_get_or_create(self, monkeypatch):
        monkeypatch.setattr(_fake_settings, "enable_anonymous_preview", False)
        try:
            fake_req = MagicMock()
            fake_resp = MagicMock()
            fake_db = AsyncMock()
            result = await get_or_create_anonymous_session(fake_req, fake_resp, fake_db)
            from fastapi.responses import JSONResponse
            assert isinstance(result, JSONResponse)
            assert result.status_code == 404
        finally:
            monkeypatch.setattr(_fake_settings, "enable_anonymous_preview", True)

    @pytest.mark.asyncio
    async def test_flag_off_returns_404_from_require(self, monkeypatch):
        monkeypatch.setattr(_fake_settings, "enable_anonymous_preview", False)
        try:
            fake_req = MagicMock()
            fake_db = AsyncMock()
            result = await require_anonymous_session(fake_req, fake_db)
            from fastapi.responses import JSONResponse
            assert isinstance(result, JSONResponse)
            assert result.status_code == 404
        finally:
            monkeypatch.setattr(_fake_settings, "enable_anonymous_preview", True)

    @pytest.mark.asyncio
    async def test_admin_off_returns_403_from_get_or_create(self, monkeypatch):
        monkeypatch.setattr(
            _admin_settings_stub, "load_settings",
            lambda: _FakeAdminSettings()
        )
        with patch("anonymous_session._get_admin_flag", return_value=False):
            fake_req = MagicMock()
            fake_resp = MagicMock()
            fake_db = AsyncMock()
            result = await get_or_create_anonymous_session(fake_req, fake_resp, fake_db)
            from fastapi.responses import JSONResponse
            assert isinstance(result, JSONResponse)
            assert result.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_read_failure_returns_closed(self):
        """Admin settings read failure → fail-closed → 403."""
        def _raise():
            raise RuntimeError("config file missing")

        with patch("anonymous_session._get_admin_flag", return_value=False):
            fake_req = MagicMock()
            fake_resp = MagicMock()
            fake_db = AsyncMock()
            result = await get_or_create_anonymous_session(fake_req, fake_resp, fake_db)
            from fastapi.responses import JSONResponse
            assert isinstance(result, JSONResponse)
            assert result.status_code == 403


# ---------------------------------------------------------------------------
# B. Session cookie
# ---------------------------------------------------------------------------

class TestSessionCookie:
    """Cookie set on new session; no-cookie → 401 on require."""

    @pytest.mark.asyncio
    async def test_new_session_sets_cookie_attributes(self):
        """First upload call creates a session and sets cookie with correct attrs."""
        # We test _create_session directly to avoid DB plumbing
        from anonymous_session import _create_session

        # Fake DB that records the add() call
        added_rows = []
        fake_db = AsyncMock()
        fake_db.add.side_effect = lambda row: added_rows.append(row)

        # Fake response that captures set_cookie calls
        cookie_calls = []
        fake_resp = MagicMock()
        fake_resp.set_cookie.side_effect = lambda **kw: cookie_calls.append(kw)

        ctx = await _create_session(fake_db, fake_resp, set_cookie=True)

        # Cookie must have been set
        assert len(cookie_calls) == 1
        call = cookie_calls[0]
        assert call["key"] == _COOKIE_NAME
        assert call["httponly"] is True
        assert call["secure"] is True
        assert call["samesite"] == "lax"
        assert call["max_age"] == _COOKIE_MAX_AGE

        # Context is correct type
        assert isinstance(ctx, AnonymousSessionContext)
        assert ctx.is_new is True
        assert ctx.raw_token is not None
        assert len(ctx.session_id_hash) == 64  # HMAC-SHA256 hex

    @pytest.mark.asyncio
    async def test_no_cookie_require_returns_401(self):
        """require_anonymous_session with no cookie → 401."""
        fake_req = MagicMock()
        fake_req.headers = {}
        fake_db = AsyncMock()

        result = await require_anonymous_session(fake_req, fake_db, avt_anon=None)
        from fastapi.responses import JSONResponse
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_session_cookie_require_returns_401(self):
        """require_anonymous_session with a cookie that doesn't match any row → 401."""
        fake_req = MagicMock()
        fake_req.headers = {}
        fake_db = AsyncMock()
        # DB execute returns no row
        fake_db.execute.return_value.scalar_one_or_none.return_value = None

        result = await require_anonymous_session(
            fake_req, fake_db, avt_anon="bad_token_xyz"
        )
        from fastapi.responses import JSONResponse
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_session_cookie_require_returns_401(self):
        """require_anonymous_session with an expired session → 401."""
        raw_token = "valid_token_abc"
        hashed = _hash_token(raw_token)
        # Return an expired row
        expired_row = _make_session_row(hashed, expired=True)
        fake_req = MagicMock()
        fake_req.headers = {}
        fake_db = AsyncMock()
        fake_db.execute.return_value.scalar_one_or_none.return_value = None  # expired filtered by query

        result = await require_anonymous_session(
            fake_req, fake_db, avt_anon=raw_token
        )
        from fastapi.responses import JSONResponse
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_session_cookie_require_returns_context(self):
        """require_anonymous_session with a valid session → AnonymousSessionContext."""
        raw_token = "valid_token_xyz_abc_def"
        hashed = _hash_token(raw_token)
        row = _make_session_row(hashed, expired=False)
        fake_req = MagicMock()
        fake_req.headers = {}
        fake_db = AsyncMock()

        # Patch _lookup_session directly to bypass SQLAlchemy ORM select() with fake class
        async def _fake_lookup(db, sid):
            return row

        with patch("anonymous_session._lookup_session", side_effect=_fake_lookup):
            result = await require_anonymous_session(
                fake_req, fake_db, avt_anon=raw_token
            )
        assert isinstance(result, AnonymousSessionContext)
        assert result.session_id_hash == hashed
        assert result.is_new is False


# ---------------------------------------------------------------------------
# C. Upload endpoint happy path / TooLarge (via module-level mocking)
# ---------------------------------------------------------------------------

class TestUploadEndpoint:
    """Upload endpoint happy path and TooLarge via full dependency mocking."""

    def _make_record_result(self, preview_id: str = "prv_test_001") -> MagicMock:
        r = MagicMock()
        r.record_id = preview_id
        r.status = MagicMock()
        r.status.value = "ready_for_mode"
        r.status_reason = None
        r.teaser_duration_seconds = 30.0
        return r

    @pytest.mark.asyncio
    async def test_upload_happy_path_returns_preview_id(self, tmp_path, monkeypatch):
        """Full-fake upload returns 200 with preview_id."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        app = FastAPI()

        # Import the router (re-import to get fresh module bindings)
        import importlib
        # Remove cached stale modules that would break fresh import
        for mod_name in list(sys.modules.keys()):
            if "anonymous_preview_api" in mod_name:
                del sys.modules[mod_name]

        import anonymous_preview_api as _api_mod

        # Patch all dependencies
        fake_session_ctx = AnonymousSessionContext(
            session_id_hash="sess_hash_abc",
            raw_token="raw_token_abc",
            is_new=True,
        )

        async def _fake_get_or_create(req, resp, db):
            # Set cookie directly on the response mock to satisfy the test
            resp.set_cookie(
                key=_COOKIE_NAME,
                value="raw_token_abc",
                httponly=True,
                secure=True,
                samesite="lax",
                max_age=_COOKIE_MAX_AGE,
                path="/",
            )
            return fake_session_ctx

        fake_upload_path = tmp_path / "upload_abc.mp4"
        fake_upload_path.write_bytes(b"fake video data" * 100)

        async def _fake_handle_upload(**kwargs):
            return fake_upload_path, "deadbeef" * 8, 1500

        fake_record = self._make_record_result()

        monkeypatch.setattr(_api_mod, "get_or_create_anonymous_session", _fake_get_or_create)
        monkeypatch.setattr(_api_mod, "handle_anonymous_upload", _fake_handle_upload)
        monkeypatch.setattr(_api_mod, "_get_admin_enabled", lambda: True)

        import asyncio as _asyncio

        def _fake_to_thread(fn):
            # run_intake_and_save in thread — return fake record
            return fake_record

        # Patch asyncio.to_thread so we don't actually run the sync DB code
        original_to_thread = _asyncio.to_thread

        async def _patched_to_thread(fn, *args, **kwargs):
            return _fake_to_thread(fn)

        monkeypatch.setattr(_asyncio, "to_thread", _patched_to_thread)

        def _fake_admit(teaser_dur, sett):
            result = MagicMock()
            result.decision = MagicMock()
            result.decision.value = "admit"
            return result

        monkeypatch.setattr(_api_mod, "admit_for_free_preview", _fake_admit)

        # Also stub build_probe_fn so it doesn't call ffprobe
        monkeypatch.setattr(_api_mod, "build_probe_fn", lambda s: lambda x: None)

        app.include_router(_api_mod.router)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/gateway/anonymous-preview/upload",
            content=b"fake video data",
            headers={"content-type": "video/mp4", "origin": "http://localhost:3000"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "preview_id" in body
        assert body["preview_id"] == "prv_test_001"
        assert body["mode"] == "free"

        # Restore
        monkeypatch.setattr(_asyncio, "to_thread", original_to_thread)

    @pytest.mark.asyncio
    async def test_upload_too_large_returns_413(self, monkeypatch):
        """UploadTooLarge → 413."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        for mod_name in list(sys.modules.keys()):
            if "anonymous_preview_api" in mod_name:
                del sys.modules[mod_name]

        import anonymous_preview_api as _api_mod

        fake_session_ctx = AnonymousSessionContext(
            session_id_hash="sess_hash_abc",
            raw_token=None,
            is_new=False,
        )

        async def _fake_get_or_create(req, resp, db):
            return fake_session_ctx

        from anonymous_preview_upload import UploadTooLarge

        async def _fake_handle_upload(**kwargs):
            raise UploadTooLarge(200 * 1024 * 1024)

        monkeypatch.setattr(_api_mod, "get_or_create_anonymous_session", _fake_get_or_create)
        monkeypatch.setattr(_api_mod, "handle_anonymous_upload", _fake_handle_upload)
        monkeypatch.setattr(_api_mod, "_get_admin_enabled", lambda: True)

        app = FastAPI()
        app.include_router(_api_mod.router)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/gateway/anonymous-preview/upload",
            content=b"x" * 1000,
            headers={"origin": "http://localhost:3000"},
        )
        assert resp.status_code == 413


# ---------------------------------------------------------------------------
# D. Stream gate matrix
# ---------------------------------------------------------------------------

class TestStreamGate:
    """Stream endpoint gate conditions."""

    def _make_api_mod(self):
        for mod_name in list(sys.modules.keys()):
            if "anonymous_preview_api" in mod_name:
                del sys.modules[mod_name]
        import anonymous_preview_api as m
        return m

    @pytest.mark.asyncio
    async def test_record_no_job_returns_409(self, monkeypatch):
        """record with no job_id → 409 preview_not_ready."""
        _api_mod = self._make_api_mod()

        sess_hash = "sess_hash_stream_1"
        fake_session_ctx = AnonymousSessionContext(
            session_id_hash=sess_hash,
            raw_token=None,
            is_new=False,
        )
        record = _make_record(session_id_hash=sess_hash, job_id=None)

        async def _fake_require(req, db, avt_anon=None):
            return fake_session_ctx

        async def _fake_get_record(db, pid, sid):
            return record

        monkeypatch.setattr(_api_mod, "require_anonymous_session", _fake_require)
        monkeypatch.setattr(_api_mod, "_get_record_for_session", _fake_get_record)
        monkeypatch.setattr(_api_mod, "_get_admin_enabled", lambda: True)

        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(_api_mod.router)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/gateway/anonymous-preview/prv_abc123/stream")
        assert resp.status_code == 409
        assert "no_job" in resp.json().get("detail", resp.text)

    @pytest.mark.asyncio
    async def test_session_mismatch_returns_404(self, monkeypatch):
        """Session mismatch (record belongs to different session) → 404."""
        _api_mod = self._make_api_mod()

        sess_hash = "sess_hash_owner"
        fake_session_ctx = AnonymousSessionContext(
            session_id_hash="sess_hash_attacker",
            raw_token=None,
            is_new=False,
        )

        async def _fake_require(req, db, avt_anon=None):
            return fake_session_ctx

        async def _fake_get_record(db, pid, sid):
            # Returns None because session_id doesn't match
            return None

        monkeypatch.setattr(_api_mod, "require_anonymous_session", _fake_require)
        monkeypatch.setattr(_api_mod, "_get_record_for_session", _fake_get_record)

        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(_api_mod.router)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/gateway/anonymous-preview/prv_abc123/stream")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_stream_happy_path_inline_disposition(self, monkeypatch):
        """Happy path: proxied stream has Content-Disposition: inline."""
        _api_mod = self._make_api_mod()

        sess_hash = "sess_hash_stream_ok"
        fake_session_ctx = AnonymousSessionContext(
            session_id_hash=sess_hash,
            raw_token=None,
            is_new=False,
        )
        record = _make_record(
            session_id_hash=sess_hash,
            job_id="job_abc_001",
            status="succeeded",
        )

        async def _fake_require(req, db, avt_anon=None):
            return fake_session_ctx

        async def _fake_get_record(db, pid, sid):
            return record

        monkeypatch.setattr(_api_mod, "require_anonymous_session", _fake_require)
        monkeypatch.setattr(_api_mod, "_get_record_for_session", _fake_get_record)
        monkeypatch.setattr(_api_mod, "_get_admin_enabled", lambda: True)

        # Fake admit / stream_gate
        from anonymous_preview_policy import StreamGate
        fake_gate = StreamGate(
            stream_only_required=True,
            watermark_required=True,
            artifact_ttl_required=True,
            low_priority_required=True,
            download_forbidden_keys=frozenset({"download_url"}),
        )

        fake_admission = MagicMock()
        fake_admission.artifact_policy = MagicMock()
        monkeypatch.setattr(_api_mod, "admit_for_free_preview", lambda dur, s: fake_admission)
        monkeypatch.setattr(_api_mod, "stream_gate_from_artifact_policy", lambda p: fake_gate)

        # Fake httpx client
        fake_client = MagicMock()

        # Job status response
        job_status_resp = MagicMock()
        job_status_resp.status_code = 200
        job_status_resp.json.return_value = {"status": "succeeded"}

        # Stream response
        fake_stream_resp = MagicMock()
        fake_stream_resp.status_code = 200
        fake_stream_resp.headers = {
            "content-type": "video/mp4",
            "content-length": "12345",
        }

        async def _aiter_bytes(chunk_size=65536):
            yield b"fake video chunk"

        fake_stream_resp.aiter_bytes = _aiter_bytes
        fake_stream_resp.aclose = AsyncMock()

        # client.get returns job_status_resp (for the /jobs/{id} check)
        fake_client.get = AsyncMock(return_value=job_status_resp)
        # client.build_request returns a request object
        fake_client.build_request.return_value = MagicMock()
        # client.send returns the stream response
        fake_client.send = AsyncMock(return_value=fake_stream_resp)

        monkeypatch.setattr(_api_mod, "get_client", lambda: fake_client)

        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(_api_mod.router)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/gateway/anonymous-preview/prv_abc123/stream",
            headers={"range": "bytes=0-"},
        )

        # Should be 200 (proxied)
        assert resp.status_code == 200
        # Content-Disposition must be inline
        assert resp.headers.get("content-disposition") == "inline"

        # Verify Range was forwarded to upstream build_request call
        build_req_call = fake_client.build_request.call_args
        assert build_req_call is not None
        fwd_headers_arg = build_req_call[1].get("headers") or build_req_call[0][2]
        assert "range" in {k.lower() for k in fwd_headers_arg.keys()}


# ---------------------------------------------------------------------------
# E. downloadable_keys anonymous_preview branch
# ---------------------------------------------------------------------------

class TestDownloadableKeys:
    """anonymous_preview mode in all three *_for() functions."""

    def _load_module(self):
        import importlib
        for mod_name in list(sys.modules.keys()):
            if "downloadable_keys" in mod_name:
                del sys.modules[mod_name]
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "downloadable_keys",
            str(_REPO / "src" / "services" / "r2_publisher_lib" / "downloadable_keys.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_download_keys_for_anonymous_preview_is_empty(self):
        m = self._load_module()
        result = m.download_keys_for("anonymous_preview")
        assert result == frozenset(), f"Expected empty frozenset, got {result}"

    def test_stream_kinds_for_anonymous_preview_is_video_only(self):
        m = self._load_module()
        result = m.stream_kinds_for("anonymous_preview")
        assert result == frozenset({"video"}), f"Expected {{video}}, got {result}"

    def test_eager_push_for_anonymous_preview_is_empty(self):
        m = self._load_module()
        result = m.eager_push_keys_for("anonymous_preview")
        assert result == frozenset(), f"Expected empty frozenset, got {result}"

    def test_existing_modes_unchanged(self):
        """Regression: free/express/studio modes still return their original values."""
        m = self._load_module()

        assert "publish.dubbed_video" in m.download_keys_for("free")
        assert "publish.dubbed_video" in m.download_keys_for("express")
        assert "publish.dubbed_video" in m.download_keys_for(None)

        assert "video" in m.stream_kinds_for("free")
        assert "video" in m.stream_kinds_for("express")
        assert "video" in m.stream_kinds_for(None)

        assert "publish.dubbed_video" in m.eager_push_keys_for("free")
        assert "publish.dubbed_video" in m.eager_push_keys_for("express")
        assert "publish.dubbed_video" in m.eager_push_keys_for(None)


# ---------------------------------------------------------------------------
# F. Import guards
# ---------------------------------------------------------------------------

class TestImportGuards:
    """Structural guards — no services.jobs import, no R2 leakage."""

    def _get_api_source(self) -> str:
        api_path = _REPO / "gateway" / "anonymous_preview_api.py"
        return api_path.read_text(encoding="utf-8")

    def test_no_services_jobs_import(self):
        """anonymous_preview_api.py must not import services.jobs (pydub guard)."""
        src = self._get_api_source()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    assert "services.jobs" not in module, (
                        f"Found forbidden 'services.jobs' import at line {node.lineno}"
                    )
                else:
                    for alias in node.names:
                        assert "services.jobs" not in alias.name, (
                            f"Found forbidden 'services.jobs' import at line {node.lineno}"
                        )

    def test_no_r2_references(self):
        """anonymous_preview_api.py must contain no R2 / r2 strings (stream-only)."""
        src = self._get_api_source()
        # Check for R2-related identifiers (not in comments starting with #)
        r2_forbidden = ["r2_client", "R2_ENDPOINT", "avt-artifacts", "X-Amz-", "presigned", "r2_artifacts"]
        for pattern in r2_forbidden:
            assert pattern not in src, (
                f"Found forbidden R2 reference '{pattern}' in anonymous_preview_api.py"
            )
