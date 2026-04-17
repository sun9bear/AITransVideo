"""HTTP-level tests for gateway.background_task_api.

Uses FastAPI TestClient + an in-memory SQLite DB so queue state is real.
Verifies: ownership enforcement, task_type validation, download gating,
and completed-state restore (the P1 bug CodeX flagged).
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)

# SQLite compatibility for PG-only types (same pattern as queue test).
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


from background_task_models import BackgroundTask  # noqa: E402
import background_task_queue as queue_mod  # noqa: E402
from background_task_api import router as bg_router  # noqa: E402
from auth import require_auth  # noqa: E402
from database import get_db  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

async def _make_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        # Table.create() also creates indexes declared via __table_args__,
        # so the partial unique `idx_bg_tasks_active` is present here and
        # the IntegrityError-recovery path in queue.create_task is
        # actually exercised by the dedupe tests.
        await conn.run_sync(lambda c: BackgroundTask.__table__.create(c))
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_user(user_id: uuid.UUID | None = None, role: str = "user"):
    return SimpleNamespace(
        id=user_id or uuid.uuid4(),
        role=role,
        email="u@test.com",
    )


def _make_job(job_id: str, user_id: uuid.UUID, project_dir: str):
    return SimpleNamespace(
        job_id=job_id,
        user_id=user_id,
        project_dir=project_dir,
    )


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    # Minimal manifest so executors don't explode if they run
    (d / "manifest.json").write_text(json.dumps({"artifact_index": {}}), encoding="utf-8")
    return d


@pytest.fixture
def app_client(project_dir):
    """Build a FastAPI app wired to in-memory sqlite + mocked auth + mocked Job lookup."""
    engine, Session = _run(_make_engine())

    # Default owner; tests can swap user via app.state
    owner = _make_user()
    job_id = "job_X"
    job = _make_job(job_id, owner.id, str(project_dir))

    app = FastAPI()
    app.include_router(bg_router)
    app.state.user = owner
    app.state.job = job
    app.state.session_maker = Session

    async def override_require_auth():
        return app.state.user

    async def override_get_db():
        async with app.state.session_maker() as db:
            yield db

    app.dependency_overrides[require_auth] = override_require_auth
    app.dependency_overrides[get_db] = override_get_db

    # Patch job ownership query: _require_job_ownership issues
    # `select(Job).where(Job.job_id == job_id)`. Easiest route: patch the
    # helper itself to return our prebuilt Job.
    import background_task_api as api_mod

    async def fake_require_job_ownership(db, *, job_id, user):
        if user is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="未登录")
        if job_id != app.state.job.job_id:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="任务不存在")
        if user.id != app.state.job.user_id and getattr(user, "role", "user") != "admin":
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="无权访问")
        return app.state.job

    app.state._orig_require = api_mod._require_job_ownership
    api_mod._require_job_ownership = fake_require_job_ownership

    # Prevent executors from actually running — they would try to open httpx
    # and real file I/O. We just want to test the API plumbing.
    app.state._orig_launch = api_mod._launch_executor
    api_mod._launch_executor = lambda **kwargs: None  # noqa: ARG005

    client = TestClient(app)
    try:
        yield app, client, job_id, owner
    finally:
        api_mod._require_job_ownership = app.state._orig_require
        api_mod._launch_executor = app.state._orig_launch
        _run(engine.dispose())


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_create_task_happy_path(app_client):
    _, client, job_id, _ = app_client
    resp = client.post(
        f"/api/jobs/{job_id}/tasks",
        json={"task_type": "materials_pack", "params": {"items": ["source_video"]}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["task"]["status"] == "pending"
    assert body["task"]["task_type"] == "materials_pack"


def test_create_task_rejects_unknown_type(app_client):
    _, client, job_id, _ = app_client
    resp = client.post(
        f"/api/jobs/{job_id}/tasks",
        json={"task_type": "delete_everything", "params": {}},
    )
    assert resp.status_code == 400
    assert "未知任务类型" in resp.json()["detail"]


def test_create_task_dedupes_same_fingerprint(app_client):
    _, client, job_id, _ = app_client
    payload = {"task_type": "materials_pack", "params": {"items": ["source_video"]}}
    r1 = client.post(f"/api/jobs/{job_id}/tasks", json=payload)
    r2 = client.post(f"/api/jobs/{job_id}/tasks", json=payload)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["task_id"] == r2.json()["task_id"]
    assert r1.json()["created"] is True
    assert r2.json()["created"] is False


def test_create_task_different_params_different_tasks(app_client):
    _, client, job_id, _ = app_client
    r1 = client.post(
        f"/api/jobs/{job_id}/tasks",
        json={"task_type": "materials_pack", "params": {"items": ["source_video"]}},
    )
    r2 = client.post(
        f"/api/jobs/{job_id}/tasks",
        json={"task_type": "materials_pack", "params": {"items": ["source_video", "subtitles"]}},
    )
    assert r1.json()["task_id"] != r2.json()["task_id"]


def test_create_task_rejects_wrong_owner(app_client):
    app, client, job_id, _ = app_client
    # Swap in a different user; job lookup will 403
    app.state.user = _make_user()
    resp = client.post(
        f"/api/jobs/{job_id}/tasks",
        json={"task_type": "materials_pack", "params": {"items": ["x"]}},
    )
    assert resp.status_code == 403


def test_create_task_rejects_missing_job(app_client):
    _, client, _, _ = app_client
    resp = client.post(
        "/api/jobs/does_not_exist/tasks",
        json={"task_type": "materials_pack", "params": {"items": ["x"]}},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# latest
# ---------------------------------------------------------------------------

def test_latest_returns_completed_task_after_page_refresh(app_client):
    """The P1 bug CodeX found: users who close the page and come back must
    see the 'materials pack ready to download' state, not a blank button."""
    app, client, job_id, owner = app_client

    # Create a task
    payload = {"task_type": "materials_pack", "params": {"items": ["source_video"]}}
    create_resp = client.post(f"/api/jobs/{job_id}/tasks", json=payload)
    task_id = create_resp.json()["task_id"]

    # Simulate the executor marking it completed while the user had the
    # browser tab closed
    async def _complete():
        async with app.state.session_maker() as db:
            await queue_mod.mark_completed(db, task_id, {"zip_path": "/tmp/x.zip"})
    _run(_complete())

    # User opens the page → UI calls /latest to restore state.
    # Default (active_only=false) MUST return the completed task.
    import hashlib
    fp = hashlib.sha256(b'{"items":["source_video"]}').hexdigest()
    resp = client.get(
        f"/api/jobs/{job_id}/tasks/latest?type=materials_pack&fingerprint={fp}",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body is not None, "Completed task must be returned for page-refresh state restore"
    assert body["status"] == "completed"
    assert body["task_id"] == task_id


def test_latest_active_only_hides_completed_tasks(app_client):
    """active_only=true should preserve the old polling semantics."""
    app, client, job_id, _ = app_client

    payload = {"task_type": "materials_pack", "params": {"items": ["source_video"]}}
    create_resp = client.post(f"/api/jobs/{job_id}/tasks", json=payload)
    task_id = create_resp.json()["task_id"]

    async def _complete():
        async with app.state.session_maker() as db:
            await queue_mod.mark_completed(db, task_id, {"zip_path": "/tmp/x.zip"})
    _run(_complete())

    import hashlib
    fp = hashlib.sha256(b'{"items":["source_video"]}').hexdigest()
    resp = client.get(
        f"/api/jobs/{job_id}/tasks/latest"
        f"?type=materials_pack&fingerprint={fp}&active_only=true",
    )
    assert resp.status_code == 200
    assert resp.json() is None


def test_latest_rejects_unknown_type(app_client):
    _, client, job_id, _ = app_client
    resp = client.get(f"/api/jobs/{job_id}/tasks/latest?type=bogus")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# get task
# ---------------------------------------------------------------------------

def test_get_task_owner_can_read(app_client):
    _, client, job_id, _ = app_client
    create = client.post(
        f"/api/jobs/{job_id}/tasks",
        json={"task_type": "materials_pack", "params": {"items": ["x"]}},
    )
    task_id = create.json()["task_id"]
    resp = client.get(f"/api/jobs/{job_id}/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["task_id"] == task_id


def test_get_task_rejects_different_owner(app_client):
    app, client, job_id, _ = app_client
    create = client.post(
        f"/api/jobs/{job_id}/tasks",
        json={"task_type": "materials_pack", "params": {"items": ["x"]}},
    )
    task_id = create.json()["task_id"]
    # Swap user — job ownership check will reject before task lookup
    app.state.user = _make_user()
    resp = client.get(f"/api/jobs/{job_id}/tasks/{task_id}")
    assert resp.status_code == 403


def test_get_task_404_on_missing_task(app_client):
    _, client, job_id, _ = app_client
    resp = client.get(f"/api/jobs/{job_id}/tasks/nope-12345")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

def test_download_rejects_generate_video(app_client):
    """Only materials_pack produces a downloadable artifact. generate_video
    completes into manifest; UI refreshes availability, no task-scoped download."""
    app, client, job_id, _ = app_client
    create = client.post(
        f"/api/jobs/{job_id}/tasks",
        json={"task_type": "generate_video", "params": {}},
    )
    task_id = create.json()["task_id"]

    async def _complete():
        async with app.state.session_maker() as db:
            await queue_mod.mark_completed(db, task_id, {"video_ready": True})
    _run(_complete())

    resp = client.get(f"/api/jobs/{job_id}/tasks/{task_id}/download")
    assert resp.status_code == 400
    assert "不支持" in resp.json()["detail"]


def test_download_rejects_incomplete_task(app_client):
    _, client, job_id, _ = app_client
    create = client.post(
        f"/api/jobs/{job_id}/tasks",
        json={"task_type": "materials_pack", "params": {"items": ["x"]}},
    )
    task_id = create.json()["task_id"]
    # Do NOT mark completed — download should refuse
    resp = client.get(f"/api/jobs/{job_id}/tasks/{task_id}/download")
    assert resp.status_code == 409


def test_download_404_on_missing_file(app_client, project_dir):
    """Completed task whose zip was GC'd → 404 at download time."""
    app, client, job_id, _ = app_client
    create = client.post(
        f"/api/jobs/{job_id}/tasks",
        json={"task_type": "materials_pack", "params": {"items": ["x"]}},
    )
    task_id = create.json()["task_id"]

    # Create zip path inside project_dir, but DON'T write the file
    exports = project_dir / "exports"
    exports.mkdir()
    ghost_zip = exports / f"materials_{task_id}.zip"

    async def _complete():
        async with app.state.session_maker() as db:
            await queue_mod.mark_completed(
                db, task_id,
                {"zip_path": str(ghost_zip), "size_bytes": 0, "filename": "materials_x.zip"},
            )
    _run(_complete())

    resp = client.get(f"/api/jobs/{job_id}/tasks/{task_id}/download")
    assert resp.status_code == 404


def test_download_rejects_path_traversal_in_result(app_client, project_dir, tmp_path):
    """If something wrote a zip_path pointing outside project_dir, refuse it.
    Defense in depth against a poisoned result payload."""
    app, client, job_id, _ = app_client
    create = client.post(
        f"/api/jobs/{job_id}/tasks",
        json={"task_type": "materials_pack", "params": {"items": ["x"]}},
    )
    task_id = create.json()["task_id"]

    # Outside project_dir
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"secret")

    async def _complete():
        async with app.state.session_maker() as db:
            await queue_mod.mark_completed(
                db, task_id,
                {"zip_path": str(outside), "filename": "materials_x.zip"},
            )
    _run(_complete())

    resp = client.get(f"/api/jobs/{job_id}/tasks/{task_id}/download")
    assert resp.status_code == 400
    assert "越界" in resp.json()["detail"]


def test_admin_created_task_is_visible_to_job_owner(app_client):
    """If an admin triggers a task on behalf of a user, the job owner must
    be able to restore its state on next page load — task ownership follows
    the job, not the caller."""
    app, client, job_id, owner = app_client

    # Admin acts on behalf of the owner's job
    admin = _make_user(role="admin")
    app.state.user = admin

    payload = {"task_type": "materials_pack", "params": {"items": ["source_video"]}}
    create_resp = client.post(f"/api/jobs/{job_id}/tasks", json=payload)
    assert create_resp.status_code == 200
    task_id = create_resp.json()["task_id"]

    # Now simulate owner reopening the browser — switch caller to owner
    app.state.user = owner

    # Owner MUST see the same task via GET /tasks/{task_id}
    get_resp = client.get(f"/api/jobs/{job_id}/tasks/{task_id}")
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["task_id"] == task_id

    # Owner MUST see it via /latest (state restore path)
    import hashlib
    fp = hashlib.sha256(b'{"items":["source_video"]}').hexdigest()
    latest_resp = client.get(
        f"/api/jobs/{job_id}/tasks/latest?type=materials_pack&fingerprint={fp}",
    )
    assert latest_resp.status_code == 200
    body = latest_resp.json()
    assert body is not None
    assert body["task_id"] == task_id


def test_owner_retry_during_admin_task_returns_same_task(app_client):
    """Owner creates after admin created — dedupe must merge, and the /latest
    for the owner must resolve the existing task rather than return null."""
    app, client, job_id, owner = app_client

    admin = _make_user(role="admin")
    app.state.user = admin
    payload = {"task_type": "materials_pack", "params": {"items": ["source_video"]}}
    r_admin = client.post(f"/api/jobs/{job_id}/tasks", json=payload)
    admin_task_id = r_admin.json()["task_id"]

    # Owner clicks while admin's task is still pending — dedupe should kick in
    app.state.user = owner
    r_owner = client.post(f"/api/jobs/{job_id}/tasks", json=payload)
    assert r_owner.status_code == 200
    body = r_owner.json()
    assert body["task_id"] == admin_task_id
    assert body["created"] is False
    # The returned task payload must be non-null (was None before the fix)
    assert body["task"] is not None
    assert body["task"]["status"] in ("pending", "running")


def test_download_happy_path(app_client, project_dir):
    app, client, job_id, _ = app_client
    create = client.post(
        f"/api/jobs/{job_id}/tasks",
        json={"task_type": "materials_pack", "params": {"items": ["x"]}},
    )
    task_id = create.json()["task_id"]

    exports = project_dir / "exports"
    exports.mkdir()
    zip_path = exports / f"materials_{task_id}.zip"
    zip_path.write_bytes(b"PK\x03\x04fake_zip_bytes")

    async def _complete():
        async with app.state.session_maker() as db:
            await queue_mod.mark_completed(
                db, task_id,
                {"zip_path": str(zip_path), "filename": "materials_fancy.zip"},
            )
    _run(_complete())

    resp = client.get(f"/api/jobs/{job_id}/tasks/{task_id}/download")
    assert resp.status_code == 200
    assert resp.content == b"PK\x03\x04fake_zip_bytes"
    # FileResponse sets an attachment Content-Disposition with our filename
    cd = resp.headers.get("content-disposition", "")
    assert "materials_fancy.zip" in cd
