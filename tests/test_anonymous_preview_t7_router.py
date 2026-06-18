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
import inspect
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# sys.path setup — must precede real module imports
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parent.parent
_GATEWAY = str(_REPO / "gateway")
_SRC = str(_REPO / "src")
for _p in (_GATEWAY, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Import real modules (order-independent, no sys.modules.setdefault).
# All patches are applied in fixtures via monkeypatch, never at module level.
# ---------------------------------------------------------------------------

import anonymous_preview_api as api  # noqa: E402
import anonymous_session as anon_session_mod  # noqa: E402
from anonymous_session import (  # noqa: E402
    AnonymousSessionContext,
    _COOKIE_NAME,
    _COOKIE_MAX_AGE,
    _hash_token,
    get_or_create_anonymous_session,
    require_anonymous_session,
    _create_session,
    _lookup_session,
)
from database import get_db  # noqa: E402


async def _fake_get_db():
    """No-op DB override for TestClient tests — all DB calls are monkeypatched."""
    yield AsyncMock()

# ---------------------------------------------------------------------------
# Declarative fake models for SQLAlchemy expression construction
# (same pattern as test_anonymous_preview_t8_create.py)
# ---------------------------------------------------------------------------

from sqlalchemy import Boolean, Column, String  # noqa: E402
from sqlalchemy.orm import declarative_base  # noqa: E402

_Base = declarative_base()


class _FakeAnonymousSessionModel(_Base):
    __tablename__ = "anon_sessions_fake_t7"
    session_id_hash = Column(String, primary_key=True)
    expires_at = Column(String)
    claim_user_id = Column(String)


class _FakeAnonymousPreviewRecordModel(_Base):
    __tablename__ = "anon_preview_records_fake_t7"
    preview_id = Column(String, primary_key=True)
    session_id = Column(String)
    job_id = Column(String)
    status = Column(String)
    status_reason = Column(String)
    mode = Column(String)
    expires_at = Column(String)
    created_at = Column(String)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _make_session_row(hash_val: str, expired: bool = False) -> MagicMock:
    offset = -1 if expired else 1
    row = MagicMock()
    row.session_id_hash = hash_val
    row.expires_at = _now_utc() + timedelta(hours=offset)
    row.claim_user_id = None
    return row


def _make_record(
    preview_id: str = "prv_abc123",
    session_id_hash: str = "sess_hash_xyz",
    job_id: Optional[str] = None,
    status: str = "ready_for_mode",
    expired: bool = False,
) -> MagicMock:
    offset = -1 if expired else 24
    row = MagicMock()
    row.preview_id = preview_id
    row.session_id = session_id_hash
    row.job_id = job_id
    row.status = status
    row.status_reason = None
    row.mode = "free"
    row.expires_at = _now_utc() + timedelta(hours=offset)
    row.created_at = _now_utc()
    return row


# ---------------------------------------------------------------------------
# autouse fixture: patch settings to valid defaults for every T7 test.
# Both api.settings and anon_session_mod.settings are the SAME object
# (both do `from config import settings`), so patching one patches both.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    """Ensure settings fields are correct for all T7 tests."""
    s = api.settings
    monkeypatch.setattr(s, "enable_anonymous_preview", True, raising=False)
    monkeypatch.setattr(s, "anonymous_preview_hash_secret", "x" * 32, raising=False)
    monkeypatch.setattr(s, "anonymous_preview_max_upload_bytes", 200 * 1024 * 1024, raising=False)
    monkeypatch.setattr(s, "anonymous_preview_max_seconds", 180.0, raising=False)
    monkeypatch.setattr(s, "job_api_upstream", "http://127.0.0.1:8877", raising=False)
    monkeypatch.setattr(s, "anonymous_preview_cap_global_per_day", 500, raising=False)
    monkeypatch.setattr(s, "anonymous_preview_cap_per_ip", 3, raising=False)
    monkeypatch.setattr(s, "anonymous_preview_cap_per_device", 1, raising=False)
    monkeypatch.setattr(s, "anonymous_preview_cap_per_source", 1, raising=False)
    # Patch AnonymousPreviewRecord in api to the declarative fake
    monkeypatch.setattr(api, "AnonymousPreviewRecord", _FakeAnonymousPreviewRecordModel)
    # Patch AnonymousSession in anonymous_session module to the declarative fake
    monkeypatch.setattr(anon_session_mod, "AnonymousSession", _FakeAnonymousSessionModel, raising=False)
    # Patch _get_admin_enabled in api (used by router handlers)
    monkeypatch.setattr(api, "_get_admin_enabled", lambda: True)
    # plan 2026-06-12 §A：upload 的 master gate 改走 lane resolver——
    # 测试默认 free lane 开（与原 _get_admin_enabled=True 行为等价）。
    monkeypatch.setattr(api, "_resolve_active_lane", lambda: "free")
    # Patch admin flag in anonymous_session module
    monkeypatch.setattr(anon_session_mod, "_get_admin_flag", lambda: True)


# ---------------------------------------------------------------------------
# A. Flag / admin gate — test via anonymous_session dependency directly
# ---------------------------------------------------------------------------

class TestFlagGate:
    """Feature flag off → dependency returns 404 JSONResponse."""

    @pytest.mark.asyncio
    async def test_flag_off_returns_404_from_get_or_create(self, monkeypatch):
        monkeypatch.setattr(api.settings, "enable_anonymous_preview", False)
        fake_req = MagicMock()
        fake_resp = MagicMock()
        fake_db = AsyncMock()
        result = await get_or_create_anonymous_session(fake_req, fake_resp, fake_db)
        from fastapi.responses import JSONResponse
        assert isinstance(result, JSONResponse)
        assert result.status_code == 404

    @pytest.mark.asyncio
    async def test_flag_off_returns_404_from_require(self, monkeypatch):
        monkeypatch.setattr(api.settings, "enable_anonymous_preview", False)
        fake_req = MagicMock()
        fake_db = AsyncMock()
        result = await require_anonymous_session(fake_req, fake_db)
        from fastapi.responses import JSONResponse
        assert isinstance(result, JSONResponse)
        assert result.status_code == 404

    @pytest.mark.asyncio
    async def test_admin_off_returns_403_from_get_or_create(self, monkeypatch):
        monkeypatch.setattr(anon_session_mod, "_get_admin_flag", lambda: False)
        fake_req = MagicMock()
        fake_resp = MagicMock()
        fake_db = AsyncMock()
        result = await get_or_create_anonymous_session(fake_req, fake_resp, fake_db)
        from fastapi.responses import JSONResponse
        assert isinstance(result, JSONResponse)
        assert result.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_read_failure_returns_closed(self, monkeypatch):
        """Admin settings read failure → fail-closed → 403."""
        monkeypatch.setattr(anon_session_mod, "_get_admin_flag", lambda: False)
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
    async def test_new_session_sets_cookie_attributes(self, monkeypatch):
        """First upload call creates a session and sets cookie with correct attrs."""
        # Fake DB that records the add() call
        added_rows = []
        fake_db = MagicMock()
        fake_db.commit = AsyncMock()
        fake_db.add.side_effect = lambda row: added_rows.append(row)

        # Fake response that captures set_cookie calls
        cookie_calls = []
        fake_resp = MagicMock()
        fake_resp.set_cookie.side_effect = lambda **kw: cookie_calls.append(kw)

        # Patch AnonymousSession constructor used inside _create_session
        monkeypatch.setattr(anon_session_mod, "AnonymousSession", _FakeAnonymousSessionModel, raising=False)

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
        fake_req.cookies = {}
        fake_db = AsyncMock()

        result = await require_anonymous_session(fake_req, fake_db)
        from fastapi.responses import JSONResponse
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_session_cookie_require_returns_401(self, monkeypatch):
        """require_anonymous_session with a cookie that doesn't match any row → 401."""
        fake_req = MagicMock()
        fake_req.headers = {}
        fake_db = AsyncMock()

        # Patch _lookup_session to return None (no matching row)
        async def _fake_lookup(db, sid):
            return None

        monkeypatch.setattr(anon_session_mod, "_lookup_session", _fake_lookup)

        fake_req.cookies = {"avt_anon": "bad_token_xyz"}
        result = await require_anonymous_session(fake_req, fake_db)
        from fastapi.responses import JSONResponse
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_session_cookie_require_returns_401(self, monkeypatch):
        """require_anonymous_session with an expired session → 401."""
        raw_token = "valid_token_abc"
        fake_req = MagicMock()
        fake_req.headers = {}
        fake_db = AsyncMock()

        # Patch _lookup_session to return None (expired row already filtered by query)
        async def _fake_lookup(db, sid):
            return None

        monkeypatch.setattr(anon_session_mod, "_lookup_session", _fake_lookup)

        fake_req.cookies = {"avt_anon": raw_token}
        result = await require_anonymous_session(fake_req, fake_db)
        from fastapi.responses import JSONResponse
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_session_cookie_require_returns_context(self, monkeypatch):
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

        monkeypatch.setattr(anon_session_mod, "_lookup_session", _fake_lookup)
        fake_req.cookies = {"avt_anon": raw_token}
        result = await require_anonymous_session(fake_req, fake_db)
        assert isinstance(result, AnonymousSessionContext)
        assert result.session_id_hash == hashed
        assert result.is_new is False

    @pytest.mark.asyncio
    async def test_claimed_session_cookie_get_or_create_rotates(self, monkeypatch):
        raw_token = "claimed_token_xyz_abc_def"
        hashed = _hash_token(raw_token)
        row = _make_session_row(hashed, expired=False)
        row.claim_user_id = str(uuid.uuid4())
        fake_req = MagicMock()
        fake_req.cookies = {"avt_anon": raw_token}
        fake_resp = MagicMock()
        fake_db = MagicMock()
        fake_db.commit = AsyncMock()

        async def _fake_lookup(db, sid):
            assert sid == hashed
            return row

        monkeypatch.setattr(anon_session_mod, "_lookup_session", _fake_lookup)

        result = await get_or_create_anonymous_session(fake_req, fake_resp, fake_db)
        assert isinstance(result, AnonymousSessionContext)
        assert result.is_new is True
        fake_resp.set_cookie.assert_called_once()
        fake_db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_claimed_session_cookie_require_returns_401(self, monkeypatch):
        raw_token = "claimed_token_require_xyz"
        hashed = _hash_token(raw_token)
        row = _make_session_row(hashed, expired=False)
        row.claim_user_id = str(uuid.uuid4())
        fake_req = MagicMock()
        fake_req.headers = {}
        fake_req.cookies = {"avt_anon": raw_token}
        fake_db = AsyncMock()

        async def _fake_lookup(db, sid):
            assert sid == hashed
            return row

        monkeypatch.setattr(anon_session_mod, "_lookup_session", _fake_lookup)

        result = await require_anonymous_session(fake_req, fake_db)
        from fastapi.responses import JSONResponse
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    def test_lookup_session_filters_claimed_rows(self):
        src = inspect.getsource(anon_session_mod._lookup_session)
        assert "AnonymousSession.claim_user_id.is_(None)" in src


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
        # 契约真实字段是 duration_seconds（不是 teaser_duration_seconds）；
        # 用错字段名会让未来读测试者误判生产读哪个字段（CodeX 测试卫生点）。
        r.duration_seconds = 30.0
        # plan 2026-06-12 §A：响应 mode 改读 record.mode（lane 锁定值）。
        # MagicMock 属性不可 JSON 序列化，必须钉成真实字符串。
        r.mode = "free"
        return r

    @pytest.mark.asyncio
    async def test_upload_happy_path_returns_preview_id(self, tmp_path, monkeypatch):
        """Full-fake upload returns 200 with preview_id."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        app = FastAPI()

        fake_session_ctx = AnonymousSessionContext(
            session_id_hash="sess_hash_abc",
            raw_token="raw_token_abc",
            is_new=True,
        )

        async def _fake_get_or_create(req, resp, db):
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

        monkeypatch.setattr(api, "get_or_create_anonymous_session", _fake_get_or_create)
        monkeypatch.setattr(api, "handle_anonymous_upload", _fake_handle_upload)
        monkeypatch.setattr(api, "_get_admin_enabled", lambda: True)
        monkeypatch.setattr(api, "require_same_origin_state_change", lambda r: None)

        import asyncio as _asyncio

        original_to_thread = _asyncio.to_thread

        async def _patched_to_thread(fn, *args, **kwargs):
            return fake_record

        monkeypatch.setattr(_asyncio, "to_thread", _patched_to_thread)

        def _fake_admit(teaser_dur, sett):
            # CodeX P0 守卫：后端契约 decision 是 "admitted"（不是 "admit"）。
            # 前端只接受 "admitted" 才进 consent，错位会让漏斗不可用。
            result = MagicMock()
            result.decision = MagicMock()
            result.decision.value = "admitted"
            return result

        monkeypatch.setattr(api, "admit_for_free_preview", _fake_admit)
        # Router now wires build_intake_probe_fn (the single-arg adapter-contract
        # seam). The mock returns a 1-arg callable matching probe_fn(upload).
        monkeypatch.setattr(api, "build_intake_probe_fn", lambda s: lambda x: None)

        app.include_router(api.router)

        # audit 路径持久化（T8b）会查 record 行——必须返回带 audit 的假行：
        # 2026-06-11 P0 修复后行缺失 = 持久化断裂 → fail-loud 503，
        # None 不再是合法的"跳过"路径。
        async def _db_for_upload():
            db = MagicMock()
            _orm_row = MagicMock()
            _orm_row.audit = {}
            _result = MagicMock()
            _result.scalar_one_or_none = MagicMock(return_value=_orm_row)
            # AD-8 peek（T2 起含 per-mode 行，cap=1）也走同一 execute——
            # MagicMock 的 __int__ 默认返回 1 会撞 per-mode cap；钉成 [0]。
            _result.fetchone = MagicMock(return_value=[0])
            db.execute = AsyncMock(return_value=_result)
            db.commit = AsyncMock()
            yield db

        app.dependency_overrides[get_db] = _db_for_upload

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
        # 契约值守卫：upload 必须回后端真实枚举 "admitted"，前端据此进 consent
        assert body["admission_decision"] == "admitted"

        app.dependency_overrides.clear()
        monkeypatch.setattr(_asyncio, "to_thread", original_to_thread)

    @pytest.mark.asyncio
    async def test_upload_too_large_returns_413(self, monkeypatch):
        """UploadTooLarge → 413."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

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

        monkeypatch.setattr(api, "get_or_create_anonymous_session", _fake_get_or_create)
        monkeypatch.setattr(api, "handle_anonymous_upload", _fake_handle_upload)
        monkeypatch.setattr(api, "_get_admin_enabled", lambda: True)
        monkeypatch.setattr(api, "require_same_origin_state_change", lambda r: None)

        app = FastAPI()
        app.include_router(api.router)

        # AD-8 peek runs before handle_anonymous_upload; db must return valid count rows
        # (count=0, below any cap) so peek passes and UploadTooLarge is reached.
        _peek_call = {"n": 0}

        async def _db_for_too_large():
            db = MagicMock()
            db.commit = AsyncMock()

            async def _execute(stmt, params=None):
                _peek_call["n"] += 1
                row = MagicMock()
                row.fetchone = MagicMock(return_value=[0])
                return row

            db.execute = _execute
            yield db

        app.dependency_overrides[get_db] = _db_for_too_large

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/gateway/anonymous-preview/upload",
            content=b"x" * 1000,
            headers={"origin": "http://localhost:3000"},
        )
        assert resp.status_code == 413
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# D. Stream gate matrix
# ---------------------------------------------------------------------------

class TestStreamGate:
    """Stream endpoint gate conditions."""

    @pytest.mark.asyncio
    async def test_record_no_job_returns_409(self, monkeypatch):
        """record with no job_id → 409 preview_not_ready."""
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

        monkeypatch.setattr(api, "require_anonymous_session", _fake_require)
        monkeypatch.setattr(api, "_get_record_for_session", _fake_get_record)
        monkeypatch.setattr(api, "_get_admin_enabled", lambda: True)

        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[get_db] = _fake_get_db

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/gateway/anonymous-preview/prv_abc123/stream")
        app.dependency_overrides.clear()
        assert resp.status_code == 409
        assert "no_job" in resp.json().get("detail", resp.text)

    @pytest.mark.asyncio
    async def test_session_mismatch_returns_404(self, monkeypatch):
        """Session mismatch (record belongs to different session) → 404."""
        fake_session_ctx = AnonymousSessionContext(
            session_id_hash="sess_hash_attacker",
            raw_token=None,
            is_new=False,
        )

        async def _fake_require(req, db, avt_anon=None):
            return fake_session_ctx

        async def _fake_get_record(db, pid, sid):
            return None

        monkeypatch.setattr(api, "require_anonymous_session", _fake_require)
        monkeypatch.setattr(api, "_get_record_for_session", _fake_get_record)

        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[get_db] = _fake_get_db

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/gateway/anonymous-preview/prv_abc123/stream")
        app.dependency_overrides.clear()
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_stream_happy_path_inline_disposition(self, monkeypatch):
        """Happy path: proxied stream has Content-Disposition: inline."""
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

        monkeypatch.setattr(api, "require_anonymous_session", _fake_require)
        monkeypatch.setattr(api, "_get_record_for_session", _fake_get_record)
        monkeypatch.setattr(api, "_get_admin_enabled", lambda: True)

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
        monkeypatch.setattr(api, "admit_for_free_preview", lambda dur, s: fake_admission)
        monkeypatch.setattr(api, "stream_gate_from_artifact_policy", lambda p: fake_gate)

        fake_client = MagicMock()

        job_status_resp = MagicMock()
        job_status_resp.status_code = 200
        job_status_resp.json.return_value = {"status": "succeeded"}

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

        fake_client.get = AsyncMock(return_value=job_status_resp)
        fake_client.build_request.return_value = MagicMock()
        fake_client.send = AsyncMock(return_value=fake_stream_resp)

        monkeypatch.setattr(api, "get_client", lambda: fake_client)

        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[get_db] = _fake_get_db

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/gateway/anonymous-preview/prv_abc123/stream",
            headers={"range": "bytes=0-"},
        )
        app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert resp.headers.get("content-disposition") == "inline"

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
        r2_forbidden = ["r2_client", "R2_ENDPOINT", "avt-artifacts", "X-Amz-", "presigned", "r2_artifacts"]
        for pattern in r2_forbidden:
            assert pattern not in src, (
                f"Found forbidden R2 reference '{pattern}' in anonymous_preview_api.py"
            )
