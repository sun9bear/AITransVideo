"""Gateway ``GET /gateway/jobs/{id}/suggested-copy-name`` endpoint.

Plan §6.4 (D17): when the user opens the "save as new copy" modal on the
edit page, Gateway picks a sensible default name — ``<源名> · 副本 N``
where N = existing copies of this source job + 1. If the full string
would exceed the column width, the source-name portion is truncated so
the ``· 副本 N`` suffix stays intact.

The endpoint is read-only and idempotent; no collision suffix is applied
here (that happens on commit via ``editing_commit``). The user can
freely edit the suggestion before confirming.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from fastapi import HTTPException  # noqa: E402
from job_intercept import intercept_suggested_copy_name  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_user():
    return SimpleNamespace(
        id="uid-1", email="u@test.com", role="user", plan_code="free",
    )


def _make_request() -> MagicMock:
    req = MagicMock()
    req.method = "GET"
    req.url = MagicMock()
    req.url.path = "/gateway/jobs/job_source/suggested-copy-name"
    req.query_params = {}
    req.headers = {}
    req.body = AsyncMock(return_value=b"")
    return req


def _make_db(
    *,
    source_job,
    existing_copy_count: int = 0,
    other_user_job=None,
):
    db = AsyncMock()

    ownership_result = MagicMock()
    ownership_result.scalar_one_or_none.return_value = source_job

    fallback_result = MagicMock()
    fallback_result.scalar_one_or_none.return_value = other_user_job or source_job

    count_result = MagicMock()
    count_result.scalar.return_value = existing_copy_count

    call_count = {"n": 0}

    async def smart_execute(stmt, *args, **kwargs):
        sql_text = str(stmt).lower()
        if "copy_of_job_id" in sql_text and "count" in sql_text:
            return count_result
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ownership_result
        return fallback_result

    db.execute = smart_execute
    return db


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_suggested_name_is_source_name_copy_1_when_no_existing_copies():
    user = _make_user()
    source = SimpleNamespace(
        job_id="job_source", user_id=user.id, display_name="我的视频",
    )
    db = _make_db(source_job=source, existing_copy_count=0)

    resp = _run(intercept_suggested_copy_name(_make_request(), "job_source", db, user))
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["suggested_name"] == "我的视频 · 副本 1"


def test_suggested_name_increments_with_existing_copies():
    user = _make_user()
    source = SimpleNamespace(
        job_id="job_source", user_id=user.id, display_name="我的视频",
    )
    db = _make_db(source_job=source, existing_copy_count=3)

    resp = _run(intercept_suggested_copy_name(_make_request(), "job_source", db, user))
    body = json.loads(resp.body)
    assert body["suggested_name"] == "我的视频 · 副本 4"


def test_suggested_name_falls_back_when_source_has_no_display_name():
    """Older jobs created before T0-4 have ``display_name=NULL``. The
    suggested copy name should use a sensible fallback rather than a
    ``None · 副本 N`` output."""
    user = _make_user()
    source = SimpleNamespace(
        job_id="job_source", user_id=user.id, display_name=None,
    )
    db = _make_db(source_job=source, existing_copy_count=0)

    resp = _run(intercept_suggested_copy_name(_make_request(), "job_source", db, user))
    body = json.loads(resp.body)
    # Any non-empty suggestion that preserves the "· 副本 N" suffix is
    # acceptable — exact fallback format is an implementation detail.
    assert body["suggested_name"].endswith("· 副本 1")
    assert len(body["suggested_name"]) > len("· 副本 1")


def test_long_source_name_truncates_so_suffix_stays_intact():
    """If ``source · 副本 N`` would exceed 60 chars, the source portion is
    truncated; the suffix is never sacrificed."""
    user = _make_user()
    long_name = "很长的标题" * 20  # 100 CJK chars = ~200 width
    source = SimpleNamespace(
        job_id="job_source", user_id=user.id, display_name=long_name,
    )
    db = _make_db(source_job=source, existing_copy_count=0)

    resp = _run(intercept_suggested_copy_name(_make_request(), "job_source", db, user))
    body = json.loads(resp.body)
    assert body["suggested_name"].endswith(" · 副本 1")
    assert len(body["suggested_name"]) <= 60


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unowned_source_raises_403():
    user = _make_user()
    # Job exists but belongs to someone else
    other_job = SimpleNamespace(job_id="job_source", user_id="other-uid", display_name="X")
    db = _make_db(source_job=None, other_user_job=other_job)

    import pytest
    with pytest.raises(HTTPException) as exc_info:
        _run(intercept_suggested_copy_name(_make_request(), "job_source", db, user))
    assert exc_info.value.status_code == 403
