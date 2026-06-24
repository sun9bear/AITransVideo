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
from job_intercept import intercept_get_job, intercept_list_jobs  # noqa: E402


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


class _ScalarOneResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _ExpiringJob:
    def __init__(self, **values):
        self._values = values
        self.expired = False

    def __getattr__(self, name):
        if self.expired:
            raise RuntimeError(f"expired ORM attribute: {name}")
        return self._values.get(name)


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


def test_list_jobs_pages_after_user_filtering_and_strips_upstream_query():
    upstream_jobs = [
        {
            "job_id": f"job_{index}",
            "status": "succeeded",
            "current_stage": "completed",
            "created_at": f"2026-04-18T00:00:0{index}+00:00",
            "updated_at": f"2026-04-25T12:00:0{index}+00:00",
        }
        for index in range(4)
    ]
    db_rows = [
        SimpleNamespace(
            job_id=f"job_{index}",
            status="succeeded",
            current_stage="completed",
            display_name=None,
            expires_at=None,
            editing_touched_at=None,
            copy_of_job_id=None,
            root_job_id=f"job_{index}",
            edit_generation=0,
        )
        for index in range(4)
    ]

    db = AsyncMock()
    calls = {"n": 0}

    async def execute(_stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            return _AllResult([(row.job_id,) for row in db_rows])
        return _ScalarsResult(db_rows)

    db.execute = execute
    db.commit = AsyncMock()
    request = _make_request()
    request.query_params = {"limit": "2", "offset": "1"}
    request.url.query = "limit=2&offset=1"
    user = SimpleNamespace(id="uid-1")
    upstream = FastAPIResponse(
        content=json.dumps({"jobs": upstream_jobs}).encode("utf-8"),
        status_code=200,
        headers={"content-type": "application/json"},
    )

    proxy_mock = AsyncMock(return_value=upstream)
    with patch("job_intercept.proxy_request", new=proxy_mock):
        response = _run(intercept_list_jobs(request, db, user))

    payload = json.loads(response.body)
    assert [job["job_id"] for job in payload["jobs"]] == ["job_1", "job_2"]
    assert payload["total"] == 4
    assert payload["limit"] == 2
    assert payload["offset"] == 1
    assert payload["has_more"] is True
    assert proxy_mock.call_args.kwargs["override_query"] == ""


def test_list_jobs_rolls_back_when_terminal_mirror_fails():
    import job_intercept

    upstream_job = {
        "job_id": "job_1",
        "status": "succeeded",
        "current_stage": "completed",
        "completed_at": "2026-05-10T07:29:11+00:00",
    }
    db_row = SimpleNamespace(
        job_id="job_1",
        status="running",
        current_stage="s5",
        display_name=None,
        expires_at=None,
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

    async def boom(*_args, **_kwargs):
        raise RuntimeError("simulated list mirror failure")

    db.execute = execute
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    request = _make_request()
    user = SimpleNamespace(id="uid-1")
    upstream = FastAPIResponse(
        content=json.dumps({"jobs": [upstream_job]}).encode("utf-8"),
        status_code=200,
        headers={"content-type": "application/json"},
    )

    with patch("job_intercept.proxy_request", new=AsyncMock(return_value=upstream)), \
         patch.object(job_intercept, "mirror_job_terminal_state", new=boom):
        response = _run(intercept_list_jobs(request, db, user))

    assert response.status_code == 200
    assert json.loads(response.body)["jobs"][0]["job_id"] == "job_1"
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


def test_list_jobs_uses_snapshot_metadata_after_rollback_expires_orm_instances():
    import job_intercept

    upstream_jobs = [
        {
            "job_id": "job_1",
            "status": "succeeded",
            "current_stage": "completed",
            "display_name": None,
        },
        {
            "job_id": "other_user_job",
            "status": "succeeded",
            "current_stage": "completed",
        },
    ]
    db_row = _ExpiringJob(
        job_id="job_1",
        status="running",
        current_stage="s5",
        display_name="Owned detail",
        expires_at=None,
        editing_touched_at=None,
        copy_of_job_id=None,
        root_job_id="job_1",
        edit_generation=0,
        role_snapshot=None,
        source_language="en",
        target_language="zh-CN",
        language_pair="en:zh-CN",
    )

    db = AsyncMock()
    calls = {"n": 0}

    async def execute(_stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            return _AllResult([("job_1",), ("other_user_job",)])
        return _ScalarsResult([db_row])

    async def boom(*_args, **_kwargs):
        raise RuntimeError("simulated list mirror failure")

    async def expire_on_rollback():
        db_row.expired = True

    db.execute = execute
    db.commit = AsyncMock()
    db.rollback = AsyncMock(side_effect=expire_on_rollback)
    request = _make_request()
    user = SimpleNamespace(id="uid-1")
    upstream = FastAPIResponse(
        content=json.dumps({"jobs": upstream_jobs}).encode("utf-8"),
        status_code=200,
        headers={"content-type": "application/json"},
    )

    with patch("job_intercept.proxy_request", new=AsyncMock(return_value=upstream)), \
         patch.object(job_intercept, "mirror_job_terminal_state", new=boom):
        response = _run(intercept_list_jobs(request, db, user))

    payload = json.loads(response.body)
    assert [job["job_id"] for job in payload["jobs"]] == ["job_1"]
    assert payload["jobs"][0]["display_name"] == "Owned detail"
    assert payload["jobs"][0]["language_pair"] == "en:zh-CN"
    db.rollback.assert_awaited_once()


def test_get_job_routes_terminal_payload_through_mirror_before_merge():
    import job_intercept

    db_job = SimpleNamespace(
        job_id="job_detail",
        user_id="uid-1",
        status="running",
        current_stage="s5",
        display_name="Detail job",
        expires_at=None,
        editing_touched_at=None,
        copy_of_job_id=None,
        root_job_id="job_detail",
        edit_generation=0,
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarOneResult(db_job))
    db.commit = AsyncMock()
    request = _make_request()
    upstream = FastAPIResponse(
        content=json.dumps({
            "job_id": "job_detail",
            "status": "succeeded",
            "current_stage": "completed",
            "completed_at": "2026-05-10T07:29:11+00:00",
            "edit_generation": 0,
        }).encode("utf-8"),
        status_code=200,
        headers={"content-type": "application/json"},
    )
    user = SimpleNamespace(id="uid-1")
    notify = AsyncMock()
    mirror = AsyncMock()

    with patch("job_intercept._verify_job_ownership", new=AsyncMock()), \
         patch("job_intercept.proxy_request", new=AsyncMock(return_value=upstream)), \
         patch("notifications_helpers.maybe_dispatch_job_transition", new=notify), \
         patch.object(job_intercept, "mirror_job_terminal_state", new=mirror):
        response = _run(intercept_get_job(request, "job_detail", db, user))

    assert response.status_code == 200
    notify.assert_awaited_once_with(
        db,
        db_job=db_job,
        upstream_status="succeeded",
    )
    mirror.assert_awaited_once()
    upstream_record = mirror.await_args.args[2]
    assert upstream_record.job_id == "job_detail"
    assert upstream_record.status == "succeeded"
    assert mirror.await_args.kwargs["settle_smart_clone"] is True
    db.commit.assert_awaited_once()


def test_get_job_rolls_back_when_terminal_mirror_hook_fails():
    import job_intercept

    db_job = SimpleNamespace(
        job_id="job_detail",
        user_id="uid-1",
        status="running",
        current_stage="s5",
        display_name="Detail job",
        expires_at=None,
        editing_touched_at=None,
        copy_of_job_id=None,
        root_job_id="job_detail",
        edit_generation=0,
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarOneResult(db_job))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    request = _make_request()
    upstream = FastAPIResponse(
        content=json.dumps({
            "job_id": "job_detail",
            "status": "succeeded",
            "current_stage": "completed",
            "completed_at": "2026-05-10T07:29:11+00:00",
            "edit_generation": 0,
        }).encode("utf-8"),
        status_code=200,
        headers={"content-type": "application/json"},
    )
    user = SimpleNamespace(id="uid-1")

    async def boom(*_args, **_kwargs):
        raise RuntimeError("simulated terminal mirror failure")

    with patch("job_intercept._verify_job_ownership", new=AsyncMock()), \
         patch("job_intercept.proxy_request", new=AsyncMock(return_value=upstream)), \
         patch("notifications_helpers.maybe_dispatch_job_transition", new=AsyncMock()), \
         patch.object(job_intercept, "mirror_job_terminal_state", new=boom):
        response = _run(intercept_get_job(request, "job_detail", db, user))

    assert response.status_code == 200
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


def test_get_job_rolls_back_when_terminal_hook_commit_fails():
    import job_intercept

    db_job = SimpleNamespace(
        job_id="job_detail",
        user_id="uid-1",
        status="running",
        current_stage="s5",
        display_name="Detail job",
        expires_at=None,
        editing_touched_at=None,
        copy_of_job_id=None,
        root_job_id="job_detail",
        edit_generation=0,
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarOneResult(db_job))
    db.commit = AsyncMock(side_effect=RuntimeError("simulated aborted transaction"))
    db.rollback = AsyncMock()
    request = _make_request()
    upstream = FastAPIResponse(
        content=json.dumps({
            "job_id": "job_detail",
            "status": "succeeded",
            "current_stage": "completed",
            "completed_at": "2026-05-10T07:29:11+00:00",
            "edit_generation": 0,
        }).encode("utf-8"),
        status_code=200,
        headers={"content-type": "application/json"},
    )
    user = SimpleNamespace(id="uid-1")

    with patch("job_intercept._verify_job_ownership", new=AsyncMock()), \
         patch("job_intercept.proxy_request", new=AsyncMock(return_value=upstream)), \
         patch("notifications_helpers.maybe_dispatch_job_transition", new=AsyncMock()), \
         patch.object(job_intercept, "mirror_job_terminal_state", new=AsyncMock()):
        response = _run(intercept_get_job(request, "job_detail", db, user))

    assert response.status_code == 200
    db.commit.assert_awaited_once()
    db.rollback.assert_awaited_once()


def test_get_job_uses_snapshot_metadata_after_rollback_expires_orm_instance():
    import job_intercept

    db_job = _ExpiringJob(
        job_id="job_detail",
        user_id="uid-1",
        status="running",
        current_stage="s5",
        display_name="Detail job",
        expires_at=None,
        editing_touched_at=None,
        copy_of_job_id=None,
        root_job_id="job_detail",
        edit_generation=0,
        role_snapshot=None,
        source_language="en",
        target_language="zh-CN",
        language_pair="en:zh-CN",
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarOneResult(db_job))
    db.commit = AsyncMock()

    async def expire_on_rollback():
        db_job.expired = True

    db.rollback = AsyncMock(side_effect=expire_on_rollback)
    request = _make_request()
    upstream = FastAPIResponse(
        content=json.dumps({
            "job_id": "job_detail",
            "status": "succeeded",
            "current_stage": "completed",
            "completed_at": "2026-05-10T07:29:11+00:00",
            "edit_generation": 0,
            "display_name": None,
        }).encode("utf-8"),
        status_code=200,
        headers={"content-type": "application/json"},
    )
    user = SimpleNamespace(id="uid-1")

    async def boom(*_args, **_kwargs):
        raise RuntimeError("simulated terminal mirror failure")

    with patch("job_intercept._verify_job_ownership", new=AsyncMock()), \
         patch("job_intercept.proxy_request", new=AsyncMock(return_value=upstream)), \
         patch("notifications_helpers.maybe_dispatch_job_transition", new=AsyncMock()), \
         patch.object(job_intercept, "mirror_job_terminal_state", new=boom):
        response = _run(intercept_get_job(request, "job_detail", db, user))

    payload = json.loads(response.body)
    assert payload["display_name"] == "Detail job"
    assert payload["language_pair"] == "en:zh-CN"
    db.rollback.assert_awaited_once()
