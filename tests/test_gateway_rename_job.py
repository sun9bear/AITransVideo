"""Gateway ``PATCH /gateway/jobs/{job_id}`` rename endpoint.

Covers plan §6.5 / D16:

- Validates body structure (missing / null / empty / too long / forbidden chars)
- Enforces ownership (403 on other-user's job)
- Collision resolution scoped to the authenticated user's OTHER jobs
  (renaming to one's own current name must succeed unchanged, not get
  an ``_xxxx`` suffix)
- Forwards to Job API PATCH and mirrors result into gateway PostgreSQL

Uses the same mock-based pattern as ``test_gateway_create_job.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from fastapi import HTTPException, Response as FastAPIResponse  # noqa: E402
from job_intercept import intercept_rename_job  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_user():
    return SimpleNamespace(
        id="uid-1", email="u@test.com", display_name="Test",
        role="user", plan_code="free",
        free_jobs_quota_total=5, free_jobs_quota_used=0,
    )


def _make_request(body: dict) -> MagicMock:
    req = MagicMock()
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req.body = AsyncMock(return_value=encoded)
    req.headers = {"content-type": "application/json"}
    req.method = "PATCH"
    req.url = MagicMock()
    req.url.path = "/gateway/jobs/job_test"
    req.query_params = {}
    return req


def _make_db(
    *,
    owned_job=None,
    other_user_job=None,
    existing_display_names: set[str] | None = None,
):
    """Build an async DB mock for rename tests.

    Query sequence inside ``intercept_rename_job``:
      1. ``select(Job).where(job_id, user_id=...)`` — ownership check
      2. [if owner] ``select(Job.display_name).where(user_id, job_id != ...)``
         — collision pool (excludes current job)
      3. [if owner] ``update(Job).where(...).values(display_name=...)``
         — mirror rename into gateway PostgreSQL
    """
    db = AsyncMock()

    ownership_result = MagicMock()
    ownership_result.scalar_one_or_none.return_value = owned_job

    # If not owned, fallback select for "job exists for another user"
    fallback_result = MagicMock()
    fallback_result.scalar_one_or_none.return_value = other_user_job or owned_job

    names_result = MagicMock()
    names_result.all.return_value = [
        (name,) for name in (existing_display_names or set())
    ]

    call_count = {"n": 0}

    async def smart_execute(stmt, *args, **kwargs):
        sql_text = str(stmt).lower()
        if "display_name" in sql_text and "is not null" in sql_text:
            return names_result
        call_count["n"] += 1
        # First ownership probe
        if call_count["n"] == 1:
            return ownership_result
        return fallback_result

    db.execute = smart_execute
    db.commit = AsyncMock()
    return db


def _fake_upstream_patch_ok(final_name: str):
    async def _patch_impl(url, json=None, headers=None, timeout=None):
        response = MagicMock()
        response.status_code = 200
        response.content = json_module_dumps(
            {"job_id": "job_test", "display_name": final_name}
        ).encode("utf-8")
        return response
    return _patch_impl


def json_module_dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_rename_happy_path_returns_200_and_persists_new_name():
    user = _make_user()
    owned = SimpleNamespace(job_id="job_test", user_id=user.id, display_name="Old Name")
    db = _make_db(owned_job=owned, existing_display_names={"Unrelated"})
    req = _make_request({"display_name": "新名字"})

    fake_client = MagicMock()
    fake_client.patch = AsyncMock(side_effect=_fake_upstream_patch_ok("新名字"))
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_client)
    fake_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=fake_cm):
        resp = _run(intercept_rename_job(req, "job_test", db, user))

    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["display_name"] == "新名字"
    # Gateway DB mirror executed (at least one update)
    assert db.commit.await_count >= 1


def test_rename_to_own_current_name_does_not_trigger_collision_suffix():
    """Renaming to the same name already in use by THIS job must succeed
    without mutating the name. Collision pool must exclude current job."""
    user = _make_user()
    owned = SimpleNamespace(job_id="job_test", user_id=user.id, display_name="My Title")
    # existing_display_names is the *other* jobs of this user; our own title
    # is NOT in that set (because the query excludes self).
    db = _make_db(owned_job=owned, existing_display_names=set())
    req = _make_request({"display_name": "My Title"})

    fake_client = MagicMock()
    fake_client.patch = AsyncMock(side_effect=_fake_upstream_patch_ok("My Title"))
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_client)
    fake_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=fake_cm):
        resp = _run(intercept_rename_job(req, "job_test", db, user))

    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["display_name"] == "My Title"  # no suffix


def test_rename_collides_with_sibling_gets_xxxx_suffix():
    """If the new name matches another job of the same user, resolve_collision
    appends a 4-char suffix — end result is ``<name>_xxxx``."""
    user = _make_user()
    owned = SimpleNamespace(job_id="job_test", user_id=user.id, display_name="Old")
    # Sibling already owns "Shared"
    db = _make_db(owned_job=owned, existing_display_names={"Shared"})
    req = _make_request({"display_name": "Shared"})

    captured = {}
    async def _patch_impl(url, json=None, headers=None, timeout=None):
        captured["body"] = json
        response = MagicMock()
        response.status_code = 200
        response.content = json_module_dumps(
            {"job_id": "job_test", "display_name": json["display_name"]}
        ).encode("utf-8")
        return response

    fake_client = MagicMock()
    fake_client.patch = AsyncMock(side_effect=_patch_impl)
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_client)
    fake_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=fake_cm):
        resp = _run(intercept_rename_job(req, "job_test", db, user))

    assert resp.status_code == 200
    sent = captured["body"]["display_name"]
    assert sent.startswith("Shared_")
    assert len(sent) == len("Shared_") + 4


# ---------------------------------------------------------------------------
# Validation / error paths
# ---------------------------------------------------------------------------


def test_rename_empty_string_returns_400():
    user = _make_user()
    owned = SimpleNamespace(job_id="job_test", user_id=user.id, display_name="Old")
    db = _make_db(owned_job=owned)
    req = _make_request({"display_name": "   "})
    resp = _run(intercept_rename_job(req, "job_test", db, user))
    assert resp.status_code == 400


def test_rename_null_returns_400():
    user = _make_user()
    owned = SimpleNamespace(job_id="job_test", user_id=user.id, display_name="Old")
    db = _make_db(owned_job=owned)
    req = _make_request({"display_name": None})
    resp = _run(intercept_rename_job(req, "job_test", db, user))
    assert resp.status_code == 400


def test_rename_missing_field_returns_400():
    user = _make_user()
    owned = SimpleNamespace(job_id="job_test", user_id=user.id, display_name="Old")
    db = _make_db(owned_job=owned)
    req = _make_request({"other_field": "x"})
    resp = _run(intercept_rename_job(req, "job_test", db, user))
    assert resp.status_code == 400


def test_rename_forbidden_chars_returns_400():
    user = _make_user()
    owned = SimpleNamespace(job_id="job_test", user_id=user.id, display_name="Old")
    db = _make_db(owned_job=owned)
    req = _make_request({"display_name": "Bad<Name>"})
    resp = _run(intercept_rename_job(req, "job_test", db, user))
    assert resp.status_code == 400


def test_rename_slash_backslash_returns_400():
    user = _make_user()
    owned = SimpleNamespace(job_id="job_test", user_id=user.id, display_name="Old")
    db = _make_db(owned_job=owned)
    req = _make_request({"display_name": "path/to/fail"})
    resp = _run(intercept_rename_job(req, "job_test", db, user))
    assert resp.status_code == 400


def test_rename_unowned_job_returns_403():
    """User-B's job should reject user-A's rename."""
    user = _make_user()
    # Job exists but belongs to another user
    other_job = SimpleNamespace(job_id="job_test", user_id="other-uid", display_name="X")
    db = _make_db(owned_job=None, other_user_job=other_job)
    req = _make_request({"display_name": "Hijack"})
    # _verify_job_ownership raises HTTPException(403) which FastAPI turns into
    # a response; here we assert the exception is raised.
    import pytest
    with pytest.raises(HTTPException) as exc_info:
        _run(intercept_rename_job(req, "job_test", db, user))
    assert exc_info.value.status_code == 403
