from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from fastapi import Response as FastAPIResponse  # noqa: E402
from job_intercept import intercept_list_jobs  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_request() -> MagicMock:
    req = MagicMock()
    req.method = "GET"
    req.url = MagicMock()
    req.url.path = "/job-api/jobs"
    req.query_params = {}
    req.headers = {}
    return req


class _AllResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarRows(self._rows)


def test_list_jobs_merges_gateway_metadata_and_preserves_purged_status():
    upstream_job = {
        "job_id": "job_1",
        "status": "succeeded",
        "current_stage": "completed",
        "display_name": None,
        "expires_at": None,
        "created_at": "2026-04-18T00:00:00+00:00",
        "updated_at": "2026-04-25T12:00:00+00:00",
    }
    db_expires_at = datetime(2026, 4, 25, 0, 0, tzinfo=timezone.utc)
    db_row = SimpleNamespace(
        job_id="job_1",
        status="purged",
        current_stage="completed",
        display_name="用户友好标题",
        expires_at=db_expires_at,
        editing_touched_at=None,
        copy_of_job_id=None,
        root_job_id="job_1",
        edit_generation=0,
    )

    db = AsyncMock()
    calls = {"n": 0}

    async def execute(_stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            return _AllResult([("job_1",)])
        return _ScalarsResult([db_row])

    db.execute = execute
    db.commit = AsyncMock()
    user = SimpleNamespace(id="uid-1")
    request = _make_request()
    upstream = FastAPIResponse(
        content=json.dumps({"jobs": [upstream_job]}).encode("utf-8"),
        status_code=200,
        headers={"content-type": "application/json"},
    )

    with patch("job_intercept.proxy_request", new=AsyncMock(return_value=upstream)):
        response = _run(intercept_list_jobs(request, db, user))

    payload = json.loads(response.body)
    merged = payload["jobs"][0]
    assert merged["status"] == "purged"
    assert merged["display_name"] == "用户友好标题"
    assert merged["expires_at"] == db_expires_at.isoformat()
    assert merged["root_job_id"] == "job_1"
    # The upstream updated_at may move during no-op edit/cancel. TTL display
    # must be driven by explicit expires_at, not this transient timestamp.
    assert merged["updated_at"] == upstream_job["updated_at"]
