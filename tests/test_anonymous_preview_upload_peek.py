"""AD-8 body-before peek tests for anonymous_upload endpoint.

Verifies that the non-authoritative global/IP rate-limit pre-check fires
BEFORE handle_anonymous_upload is called (i.e. before any body is read or
file is written to disk).

Test matrix
-----------
P1. global count already at cap  → 429 {"error": "preview_queue_full"},
    handle_anonymous_upload NOT called.
P2. ip count already at cap      → 429 {"error": "rate_limited"},
    handle_anonymous_upload NOT called.
P3. peek DB raises exception     → 503 {"error": "gate_unavailable"},
    handle_anonymous_upload NOT called.
P4. both counts below cap        → peek does NOT block, upload proceeds
    (handle_anonymous_upload IS called, test asserts 200).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# sys.path — must precede module imports
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parent.parent
_GATEWAY = str(_REPO / "gateway")
_SRC = str(_REPO / "src")
for _p in (_GATEWAY, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import anonymous_preview_api as api  # noqa: E402
import anonymous_session as anon_session_mod  # noqa: E402
from anonymous_session import (  # noqa: E402
    AnonymousSessionContext,
    _COOKIE_NAME,
    _COOKIE_MAX_AGE,
    get_or_create_anonymous_session,
)
from database import get_db  # noqa: E402

# Declarative fakes for SQLAlchemy select() compatibility
from sqlalchemy import Column, String  # noqa: E402
from sqlalchemy.orm import declarative_base  # noqa: E402

_Base = declarative_base()


class _FakeSessionModel(_Base):
    __tablename__ = "anon_sessions_fake_peek"
    session_id_hash = Column(String, primary_key=True)
    expires_at = Column(String)
    claim_user_id = Column(String)


class _FakePreviewRecordModel(_Base):
    __tablename__ = "anon_preview_records_fake_peek"
    preview_id = Column(String, primary_key=True)
    session_id = Column(String)
    job_id = Column(String)
    status = Column(String)
    status_reason = Column(String)
    mode = Column(String)
    expires_at = Column(String)
    audit = Column(String)


# ---------------------------------------------------------------------------
# autouse settings fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
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
    monkeypatch.setattr(api, "AnonymousPreviewRecord", _FakePreviewRecordModel)
    monkeypatch.setattr(anon_session_mod, "AnonymousSession", _FakeSessionModel, raising=False)
    monkeypatch.setattr(api, "_get_admin_enabled", lambda: True)
    monkeypatch.setattr(anon_session_mod, "_get_admin_flag", lambda: True)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fake_session_ctx() -> AnonymousSessionContext:
    return AnonymousSessionContext(
        session_id_hash="sess_hash_peek_test",
        raw_token="raw_token_peek",
        is_new=False,
    )


def _make_db_for_peek(global_count: int, ip_count: int, raise_on_execute: bool = False):
    """Return an async db mock whose execute() returns the given row counts.

    The upload endpoint queries the DB twice in the peek path:
      1st call  → global count row
      2nd call  → ip count row

    We use a call counter to return the right value on each call.
    """
    db = MagicMock()
    db.commit = AsyncMock()

    call_index = {"n": 0}

    async def _execute(stmt, params=None):
        if raise_on_execute:
            raise Exception("DB connection lost")
        call_index["n"] += 1
        row_mock = MagicMock()
        if call_index["n"] == 1:
            # global peek
            row_mock.fetchone = MagicMock(return_value=[global_count])
        else:
            # ip peek
            row_mock.fetchone = MagicMock(return_value=[ip_count])
        return row_mock

    db.execute = _execute
    return db


async def _fake_get_or_create(req, resp, db):
    return _fake_session_ctx()


# ---------------------------------------------------------------------------
# P1: global count at cap → 429, handle_anonymous_upload NOT called
# ---------------------------------------------------------------------------

class TestPeekGlobalCap:
    @pytest.mark.asyncio
    async def test_global_cap_returns_429_and_upload_not_called(self, monkeypatch, tmp_path):
        """When global daily count >= cap, return 429 before reading body."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        upload_called = {"called": False}

        async def _spy_upload(**kwargs):
            upload_called["called"] = True
            fake_path = tmp_path / "upload.mp4"
            fake_path.write_bytes(b"x" * 100)
            return fake_path, "aabbcc" * 8, 100

        monkeypatch.setattr(api, "get_or_create_anonymous_session", _fake_get_or_create)
        monkeypatch.setattr(api, "handle_anonymous_upload", _spy_upload)
        monkeypatch.setattr(api, "require_same_origin_state_change", lambda r: None)

        # global count = cap (500), ip = 0
        db_mock = _make_db_for_peek(global_count=500, ip_count=0)

        app = FastAPI()
        app.include_router(api.router)

        async def _override_db():
            yield db_mock

        app.dependency_overrides[get_db] = _override_db

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/gateway/anonymous-preview/upload",
            content=b"fake video",
            headers={"content-type": "video/mp4", "origin": "http://localhost:3000"},
        )

        assert resp.status_code == 429
        assert resp.json() == {"error": "preview_queue_full"}
        assert upload_called["called"] is False, "handle_anonymous_upload must NOT be called when peek blocks"

        app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_global_count_above_cap_also_blocked(self, monkeypatch, tmp_path):
        """Global count > cap (e.g. 501) is also rejected."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        upload_called = {"called": False}

        async def _spy_upload(**kwargs):
            upload_called["called"] = True
            return tmp_path / "u.mp4", "x" * 64, 100

        monkeypatch.setattr(api, "get_or_create_anonymous_session", _fake_get_or_create)
        monkeypatch.setattr(api, "handle_anonymous_upload", _spy_upload)
        monkeypatch.setattr(api, "require_same_origin_state_change", lambda r: None)

        db_mock = _make_db_for_peek(global_count=999, ip_count=0)

        app = FastAPI()
        app.include_router(api.router)

        async def _override_db():
            yield db_mock

        app.dependency_overrides[get_db] = _override_db

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/gateway/anonymous-preview/upload",
            content=b"data",
            headers={"content-type": "video/mp4", "origin": "http://localhost:3000"},
        )
        assert resp.status_code == 429
        assert upload_called["called"] is False

        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# P2: ip count at cap → 429, handle_anonymous_upload NOT called
# ---------------------------------------------------------------------------

class TestPeekIpCap:
    @pytest.mark.asyncio
    async def test_ip_cap_returns_429_and_upload_not_called(self, monkeypatch, tmp_path):
        """When per-IP daily count >= cap, return 429 before reading body."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        upload_called = {"called": False}

        async def _spy_upload(**kwargs):
            upload_called["called"] = True
            fake_path = tmp_path / "upload.mp4"
            fake_path.write_bytes(b"x" * 100)
            return fake_path, "ccddee" * 8, 100

        monkeypatch.setattr(api, "get_or_create_anonymous_session", _fake_get_or_create)
        monkeypatch.setattr(api, "handle_anonymous_upload", _spy_upload)
        monkeypatch.setattr(api, "require_same_origin_state_change", lambda r: None)

        # global = 0 (below cap), ip = cap (3)
        db_mock = _make_db_for_peek(global_count=0, ip_count=3)

        app = FastAPI()
        app.include_router(api.router)

        async def _override_db():
            yield db_mock

        app.dependency_overrides[get_db] = _override_db

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/gateway/anonymous-preview/upload",
            content=b"fake video",
            headers={"content-type": "video/mp4", "origin": "http://localhost:3000"},
        )

        assert resp.status_code == 429
        assert resp.json() == {"error": "rate_limited"}
        assert upload_called["called"] is False, "handle_anonymous_upload must NOT be called when peek blocks"

        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# P3: DB error → 503, handle_anonymous_upload NOT called
# ---------------------------------------------------------------------------

class TestPeekDbError:
    @pytest.mark.asyncio
    async def test_peek_db_error_returns_503_and_upload_not_called(self, monkeypatch, tmp_path):
        """DB exception during peek → fail-closed 503, no upload."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        upload_called = {"called": False}

        async def _spy_upload(**kwargs):
            upload_called["called"] = True
            fake_path = tmp_path / "upload.mp4"
            fake_path.write_bytes(b"x" * 100)
            return fake_path, "ff00bb" * 8, 100

        monkeypatch.setattr(api, "get_or_create_anonymous_session", _fake_get_or_create)
        monkeypatch.setattr(api, "handle_anonymous_upload", _spy_upload)
        monkeypatch.setattr(api, "require_same_origin_state_change", lambda r: None)

        db_mock = _make_db_for_peek(global_count=0, ip_count=0, raise_on_execute=True)

        app = FastAPI()
        app.include_router(api.router)

        async def _override_db():
            yield db_mock

        app.dependency_overrides[get_db] = _override_db

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/gateway/anonymous-preview/upload",
            content=b"fake video",
            headers={"content-type": "video/mp4", "origin": "http://localhost:3000"},
        )

        assert resp.status_code == 503
        assert resp.json() == {"error": "gate_unavailable"}
        assert upload_called["called"] is False

        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# P4: counts below cap → peek passes, upload proceeds (200)
# ---------------------------------------------------------------------------

class TestPeekPassThrough:
    def _make_record_result(self, preview_id: str = "prv_peek_ok") -> MagicMock:
        r = MagicMock()
        r.record_id = preview_id
        r.status = MagicMock()
        r.status.value = "ready_for_mode"
        r.status_reason = None
        r.duration_seconds = 30.0
        return r

    @pytest.mark.asyncio
    async def test_below_cap_upload_proceeds(self, monkeypatch, tmp_path):
        """Both counts below cap → peek does NOT block, upload is called and returns 200."""
        import asyncio as _asyncio
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        upload_called = {"called": False}
        fake_upload_path = tmp_path / "upload_peek.mp4"
        fake_upload_path.write_bytes(b"fake video data" * 100)

        async def _fake_handle_upload(**kwargs):
            upload_called["called"] = True
            return fake_upload_path, "deadbeef" * 8, 1500

        fake_record = self._make_record_result()

        monkeypatch.setattr(api, "get_or_create_anonymous_session", _fake_get_or_create)
        monkeypatch.setattr(api, "handle_anonymous_upload", _fake_handle_upload)
        monkeypatch.setattr(api, "require_same_origin_state_change", lambda r: None)
        monkeypatch.setattr(api, "_get_admin_enabled", lambda: True)

        original_to_thread = _asyncio.to_thread

        async def _patched_to_thread(fn, *args, **kwargs):
            return fake_record

        monkeypatch.setattr(_asyncio, "to_thread", _patched_to_thread)

        def _fake_admit(teaser_dur, sett):
            result = MagicMock()
            result.decision = MagicMock()
            result.decision.value = "admitted"
            return result

        monkeypatch.setattr(api, "admit_for_free_preview", _fake_admit)
        monkeypatch.setattr(api, "build_intake_probe_fn", lambda s: lambda x: None)

        app = FastAPI()
        app.include_router(api.router)

        call_index = {"n": 0}

        async def _override_db():
            db = MagicMock()
            db.commit = AsyncMock()

            async def _execute(stmt, params=None):
                call_index["n"] += 1
                if call_index["n"] <= 2:
                    # First two calls: peek queries → return count = 0 (below any cap)
                    row_mock = MagicMock()
                    row_mock.fetchone = MagicMock(return_value=[0])
                    return row_mock
                else:
                    # Later calls: ORM audit persist → return fake row with audit
                    # (2026-06-11 P0 修复后行缺失 = fail-loud 503，None 不再合法)
                    _orm_row = MagicMock()
                    _orm_row.audit = {}
                    result_mock = MagicMock()
                    result_mock.scalar_one_or_none = MagicMock(return_value=_orm_row)
                    return result_mock

            db.execute = _execute
            yield db

        app.dependency_overrides[get_db] = _override_db

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/gateway/anonymous-preview/upload",
            content=b"fake video data",
            headers={"content-type": "video/mp4", "origin": "http://localhost:3000"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "preview_id" in body
        assert body["preview_id"] == "prv_peek_ok"
        assert upload_called["called"] is True, "handle_anonymous_upload MUST be called when peek passes"

        app.dependency_overrides.clear()
        monkeypatch.setattr(_asyncio, "to_thread", original_to_thread)
