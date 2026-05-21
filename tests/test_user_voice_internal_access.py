"""Regression guards for user_voice_api internal endpoint access control."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


_REPO = Path(__file__).resolve().parents[1]
_GATEWAY = _REPO / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


_TEST_KEY = "test-user-voice-internal-key-32"


class _Request:
    def __init__(
        self,
        *,
        body: dict | bytes | None = None,
        key: str | None = _TEST_KEY,
        host: str = "127.0.0.1",
    ) -> None:
        self.headers = {}
        if key is not None:
            self.headers["X-Internal-Key"] = key
        self.client = SimpleNamespace(host=host)
        if body is None:
            self._body = b""
        elif isinstance(body, bytes):
            self._body = body
        else:
            self._body = json.dumps(body).encode("utf-8")

    async def body(self) -> bytes:
        return self._body


@pytest.fixture(autouse=True)
def _set_internal_key(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "internal_api_key", _TEST_KEY)


def test_internal_access_accepts_valid_key_from_loopback():
    import user_voice_api

    assert user_voice_api._internal_access_error(_Request()) is None


def test_internal_access_rejects_missing_key():
    import user_voice_api

    resp = user_voice_api._internal_access_error(_Request(key=None))

    assert resp is not None
    assert resp.status_code == 403
    assert json.loads(resp.body.decode("utf-8"))["error"] == "invalid_internal_key"


def test_internal_access_rejects_non_loopback_client():
    import user_voice_api

    resp = user_voice_api._internal_access_error(_Request(host="203.0.113.10"))

    assert resp is not None
    assert resp.status_code == 403
    assert json.loads(resp.body.decode("utf-8"))["error"] == "non_loopback_client_not_allowed"


def test_internal_access_fails_closed_when_key_unset(monkeypatch):
    from config import settings
    import user_voice_api

    monkeypatch.setattr(settings, "internal_api_key", "")
    resp = user_voice_api._internal_access_error(_Request())

    assert resp is not None
    assert resp.status_code == 503
    assert json.loads(resp.body.decode("utf-8"))["error"] == "internal_endpoint_misconfigured"


@pytest.mark.asyncio
async def test_internal_expire_voice_rejects_non_loopback_before_body_read():
    import user_voice_api

    req = _Request(body={"voice_id": "vt_1"}, host="203.0.113.10")
    req.body = AsyncMock(side_effect=AssertionError("body should not be read"))

    resp = await user_voice_api.internal_expire_voice(request=req, db=MagicMock())

    assert resp.status_code == 403
    assert json.loads(resp.body.decode("utf-8"))["error"] == "non_loopback_client_not_allowed"


@pytest.mark.asyncio
async def test_internal_expire_voice_marks_voice_when_authorized(monkeypatch):
    import user_voice_api

    marker = AsyncMock(return_value=True)
    monkeypatch.setattr(user_voice_api, "mark_voice_expired", marker)
    db = MagicMock()
    req = _Request(body={
        "user_id": "00000000-0000-0000-0000-000000000001",
        "voice_id": "vt_1",
    })

    resp = await user_voice_api.internal_expire_voice(request=req, db=db)

    assert resp.status_code == 200
    assert json.loads(resp.body.decode("utf-8")) == {"ok": True, "expired": True}
    marker.assert_awaited_once_with(
        db,
        "00000000-0000-0000-0000-000000000001",
        "vt_1",
    )
