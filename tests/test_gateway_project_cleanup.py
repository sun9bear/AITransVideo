"""Gateway-side 7d project TTL cleanup tests.

Contract (2026-04-21, follow-up to per-zip cleanup):

- Finds terminal jobs whose ``expires_at`` has passed (or whose legacy
  fallback ``created_at + 7d`` has passed for rows with NULL expires_at).
- Refuses to ``rmtree`` anything outside the registered project roots
  (``/opt/aivideotrans/data/projects`` or ``/opt/aivideotrans/app/projects``).
  Guard is belt-and-braces with the existing Job API cleanup path.
- After a successful purge, transitions ``status='purged'`` so the
  frontend can surface a "已清理" badge instead of leaving a ghost row.
- Never touches active states (queued / running / waiting_for_review / editing)
  even if expires_at has somehow passed (clock skew, manual SQL, etc.).
- Tolerates a missing ``project_dir`` (already cleaned by the Job API
  side) — still flips status so the DB stops showing a ghost.
"""

from __future__ import annotations

import asyncio
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


from models import Job  # noqa: E402
import project_cleanup  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _make_session() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Job.__table__.create(sync))
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _insert_job(
    Session,
    *,
    job_id: str,
    status: str = "succeeded",
    project_dir: str | None = None,
    expires_hours_ago: float | None = 1,
    created_days_ago: float = 10,
) -> None:
    now = datetime.now(timezone.utc)
    expires_at = None
    if expires_hours_ago is not None:
        expires_at = now - timedelta(hours=expires_hours_ago)
    async with Session() as db:
        job = Job(
            id=uuid.uuid4(),
            job_id=job_id,
            user_id=uuid.uuid4(),
            source_type="youtube_url",
            source_ref="https://example.com/x",
            title="",
            speakers="auto",
            status=status,
            project_dir=project_dir,
            created_at=now - timedelta(days=created_days_ago),
            updated_at=now - timedelta(days=created_days_ago),
            expires_at=expires_at,
        )
        db.add(job)
        await db.commit()


async def _load_status(Session, job_id: str) -> str:
    from sqlalchemy import select
    async with Session() as db:
        result = await db.execute(
            select(Job.status).where(Job.job_id == job_id)
        )
        return result.scalar_one()


# ---------------------------------------------------------------------------
# Path whitelist — the critical safety surface
# ---------------------------------------------------------------------------


def test_is_safe_project_dir_accepts_data_projects_subtree(tmp_path):
    # Simulate the real mount point by monkey-patching the allowlist
    # through module reload would be heavy; instead assert against the
    # real constants using tmp_path pretending to be a project root.
    root = tmp_path / "data" / "projects"
    project = root / "user-uuid" / "job_xyz"
    project.mkdir(parents=True)
    assert project_cleanup._is_safe_project_dir(
        project, safe_roots=(root,)
    )


def test_is_safe_project_dir_rejects_root_itself(tmp_path):
    root = tmp_path / "data" / "projects"
    root.mkdir(parents=True)
    # The safe root itself must never match — deleting it would nuke
    # every user's data in one shot.
    assert not project_cleanup._is_safe_project_dir(
        root, safe_roots=(root,)
    )


def test_is_safe_project_dir_rejects_traversal(tmp_path):
    root = tmp_path / "data" / "projects"
    evil = tmp_path / "etc" / "passwd"
    evil.parent.mkdir(parents=True)
    evil.write_text("root:x:0:0")
    assert not project_cleanup._is_safe_project_dir(
        evil, safe_roots=(root,)
    )


def test_is_safe_project_dir_rejects_empty_and_root_path():
    assert not project_cleanup._is_safe_project_dir(
        Path("/"), safe_roots=(Path("/opt/aivideotrans/data/projects"),)
    )
    assert not project_cleanup._is_safe_project_dir(
        Path(""), safe_roots=(Path("/opt/aivideotrans/data/projects"),)
    )


def test_is_safe_project_dir_rejects_poisoned_path_like_slash_s(tmp_path):
    """Regression: the 2026-04-20 ``/s`` bug would have submitted a path
    like ``/s`` as project_dir. The guard must refuse to rmtree it even
    with an otherwise permissive allowlist."""
    assert not project_cleanup._is_safe_project_dir(
        Path("/s"),
        safe_roots=(Path("/opt/aivideotrans/data/projects"),)
    )


# ---------------------------------------------------------------------------
# Happy path — expired succeeded job with real project_dir
# ---------------------------------------------------------------------------


def test_cleanup_removes_expired_succeeded_and_marks_purged(tmp_path):
    root = tmp_path / "data" / "projects"
    project = root / "user-1" / "job_expired"
    project.mkdir(parents=True)
    (project / "dummy.txt").write_text("x")

    async def run():
        Session = await _make_session()
        await _insert_job(
            Session,
            job_id="job_expired",
            status="succeeded",
            project_dir=str(project),
            expires_hours_ago=1,
        )
        async with Session() as db:
            count = await project_cleanup.cleanup_expired_projects(
                db, safe_roots=(root,)
            )
        assert count == 1
        assert not project.exists()
        new_status = await _load_status(Session, "job_expired")
        assert new_status == "purged"

    _run(run())


def test_cleanup_leaves_recent_alone(tmp_path):
    """An 'expiration in 3 days' job must not be purged."""
    root = tmp_path / "data" / "projects"
    project = root / "user-1" / "job_fresh"
    project.mkdir(parents=True)

    async def run():
        Session = await _make_session()
        await _insert_job(
            Session,
            job_id="job_fresh",
            status="succeeded",
            project_dir=str(project),
            expires_hours_ago=-72,  # 3 days in the future
        )
        async with Session() as db:
            count = await project_cleanup.cleanup_expired_projects(
                db, safe_roots=(root,)
            )
        assert count == 0
        assert project.exists()
        assert await _load_status(Session, "job_fresh") == "succeeded"

    _run(run())


# ---------------------------------------------------------------------------
# Active states never touched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("active", ["queued", "running", "waiting_for_review", "editing"])
def test_cleanup_skips_active_states(tmp_path, active):
    root = tmp_path / "data" / "projects"
    project = root / "user-1" / "job_active"
    project.mkdir(parents=True)

    async def run():
        Session = await _make_session()
        await _insert_job(
            Session,
            job_id="job_active",
            status=active,
            project_dir=str(project),
            expires_hours_ago=24,  # already expired
        )
        async with Session() as db:
            count = await project_cleanup.cleanup_expired_projects(
                db, safe_roots=(root,)
            )
        assert count == 0
        assert project.exists()
        assert await _load_status(Session, "job_active") == active

    _run(run())


# ---------------------------------------------------------------------------
# Legacy fallback — NULL expires_at with old created_at
# ---------------------------------------------------------------------------


def test_cleanup_uses_legacy_7d_fallback_when_expires_at_null(tmp_path):
    """A job created > 7d ago but with NULL expires_at (pre-migration
    data or a backfill race) still gets purged."""
    root = tmp_path / "data" / "projects"
    project = root / "user-1" / "job_legacy"
    project.mkdir(parents=True)

    async def run():
        Session = await _make_session()
        await _insert_job(
            Session,
            job_id="job_legacy",
            status="succeeded",
            project_dir=str(project),
            expires_hours_ago=None,       # NULL expires_at
            created_days_ago=10,          # Older than 7-day fallback
        )
        async with Session() as db:
            count = await project_cleanup.cleanup_expired_projects(
                db, safe_roots=(root,)
            )
        assert count == 1
        assert not project.exists()
        assert await _load_status(Session, "job_legacy") == "purged"

    _run(run())


def test_cleanup_legacy_null_not_expired_by_fallback(tmp_path):
    """NULL expires_at but created < 7d ago — legacy fallback says "don't
    purge yet"."""
    root = tmp_path / "data" / "projects"
    project = root / "user-1" / "job_recent_legacy"
    project.mkdir(parents=True)

    async def run():
        Session = await _make_session()
        await _insert_job(
            Session,
            job_id="job_recent_legacy",
            status="succeeded",
            project_dir=str(project),
            expires_hours_ago=None,
            created_days_ago=3,
        )
        async with Session() as db:
            count = await project_cleanup.cleanup_expired_projects(
                db, safe_roots=(root,)
            )
        assert count == 0
        assert project.exists()

    _run(run())


# ---------------------------------------------------------------------------
# Ghost rows: Job API already rm'd project_dir, Gateway must still flip status
# ---------------------------------------------------------------------------


def test_cleanup_marks_ghost_purged_even_when_project_dir_missing(tmp_path):
    """The Job API side cleanup already removed ``project_dir``; the DB
    row is now a ghost. Gateway cleanup must transition status to 'purged'
    so the UI stops listing it."""
    root = tmp_path / "data" / "projects"
    nonexistent = str(root / "user-1" / "job_ghost")

    async def run():
        Session = await _make_session()
        await _insert_job(
            Session,
            job_id="job_ghost",
            status="succeeded",
            project_dir=nonexistent,
            expires_hours_ago=1,
        )
        async with Session() as db:
            count = await project_cleanup.cleanup_expired_projects(
                db, safe_roots=(root,)
            )
        assert count == 1
        assert await _load_status(Session, "job_ghost") == "purged"

    _run(run())


def test_cleanup_marks_purged_when_project_dir_is_null(tmp_path):
    """Legacy job with NULL project_dir — still purge the DB row rather
    than leave it lingering."""
    root = tmp_path / "data" / "projects"

    async def run():
        Session = await _make_session()
        await _insert_job(
            Session,
            job_id="job_nulldir",
            status="succeeded",
            project_dir=None,
            expires_hours_ago=1,
        )
        async with Session() as db:
            count = await project_cleanup.cleanup_expired_projects(
                db, safe_roots=(root,)
            )
        assert count == 1
        assert await _load_status(Session, "job_nulldir") == "purged"

    _run(run())


# ---------------------------------------------------------------------------
# Refuse-to-delete on unsafe path
# ---------------------------------------------------------------------------


def test_cleanup_refuses_to_rmtree_outside_safe_root(tmp_path):
    """If a malicious / corrupted row has project_dir outside the
    registered safe roots, cleanup must refuse to touch the disk. The
    DB row is still flipped to 'purged' to stop the ghost — keeping the
    bad path silent would leak rows forever."""
    root = tmp_path / "data" / "projects"
    root.mkdir(parents=True)
    evil = tmp_path / "home" / "user" / "secrets"
    evil.mkdir(parents=True)
    (evil / "passwords.txt").write_text("don't rm me")

    async def run():
        Session = await _make_session()
        await _insert_job(
            Session,
            job_id="job_evil",
            status="succeeded",
            project_dir=str(evil),
            expires_hours_ago=1,
        )
        async with Session() as db:
            count = await project_cleanup.cleanup_expired_projects(
                db, safe_roots=(root,)
            )
        # Ghost flipped to purged, but the file on disk is intact.
        assert count == 1
        assert (evil / "passwords.txt").read_text() == "don't rm me"
        assert await _load_status(Session, "job_evil") == "purged"

    _run(run())


def test_cleanup_is_idempotent(tmp_path):
    """Running cleanup twice in a row — the second pass finds nothing
    because the first pass moved status to 'purged'."""
    root = tmp_path / "data" / "projects"
    project = root / "user-1" / "job_dup"
    project.mkdir(parents=True)

    async def run():
        Session = await _make_session()
        await _insert_job(
            Session,
            job_id="job_dup",
            status="succeeded",
            project_dir=str(project),
            expires_hours_ago=1,
        )
        async with Session() as db:
            first = await project_cleanup.cleanup_expired_projects(
                db, safe_roots=(root,)
            )
        async with Session() as db:
            second = await project_cleanup.cleanup_expired_projects(
                db, safe_roots=(root,)
            )
        assert first == 1
        assert second == 0

    _run(run())


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_cleanup_dry_run_does_not_delete_or_commit(tmp_path):
    root = tmp_path / "data" / "projects"
    project = root / "user-1" / "job_dry"
    project.mkdir(parents=True)
    (project / "keep.txt").write_text("x")

    async def run():
        Session = await _make_session()
        await _insert_job(
            Session,
            job_id="job_dry",
            status="succeeded",
            project_dir=str(project),
            expires_hours_ago=1,
        )
        async with Session() as db:
            count = await project_cleanup.cleanup_expired_projects(
                db, safe_roots=(root,), dry_run=True,
            )
        # Count still reports what WOULD have been purged.
        assert count == 1
        # ...but nothing is actually touched.
        assert project.exists()
        assert (project / "keep.txt").exists()
        assert await _load_status(Session, "job_dry") == "succeeded"

    _run(run())
