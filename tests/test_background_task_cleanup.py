"""Tests for gateway.background_task_queue.cleanup_expired_pack_zips.

Contract (2026-04-21): materials_pack zips live at
``{project_dir}/exports/materials_{task_id}.zip`` and must be pruned 24h
after the task completes. The cleanup function is called periodically by
the gateway startup scheduler.

Scope:
- Only tasks with ``task_type == 'materials_pack'`` AND ``status == 'completed'``
  AND ``updated_at < now - retention_hours`` are affected.
- Matching tasks have their zip file removed (if present) and their status
  transitioned to ``'expired'``.
- Other task types, other statuses, and recent completions are left alone.
- Missing zip files are tolerated — still mark expired so the UI learns
  the zip is gone.
"""

from __future__ import annotations

import asyncio
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

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


from background_task_models import BackgroundTask  # noqa: E402
import background_task_queue as queue  # noqa: E402


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
        await conn.run_sync(lambda sync: BackgroundTask.__table__.create(sync))
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _insert_task(
    Session,
    *,
    task_id: str,
    task_type: str = "materials_pack",
    status: str = "completed",
    zip_path: str | None = None,
    completed_hours_ago: float = 0,
) -> None:
    """Insert a task whose ``updated_at`` is ``completed_hours_ago`` in the past."""
    now = datetime.now(timezone.utc)
    async with Session() as db:
        task = BackgroundTask(
            id=task_id,
            job_id="job_test",
            user_id=uuid.uuid4(),
            task_type=task_type,
            params={"items": ["dubbed_audio"]},
            params_fingerprint=f"fp_{task_id}",
            status=status,
            result={"zip_path": zip_path} if zip_path else None,
            created_at=now - timedelta(hours=completed_hours_ago + 0.1),
            updated_at=now - timedelta(hours=completed_hours_ago),
        )
        db.add(task)
        await db.commit()


async def _load_task(Session, task_id: str) -> BackgroundTask:
    from sqlalchemy import select
    async with Session() as db:
        result = await db.execute(
            select(BackgroundTask).where(BackgroundTask.id == task_id)
        )
        return result.scalar_one()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_cleanup_removes_zip_and_marks_expired(tmp_path: Path) -> None:
    """A 25h-old completed materials_pack task gets its zip file deleted and
    its status transitioned to 'expired'."""
    zip_file = tmp_path / "materials_old.zip"
    zip_file.write_bytes(b"fake-zip-data")
    assert zip_file.exists()

    async def run() -> None:
        Session = await _make_session()
        await _insert_task(
            Session,
            task_id="old1",
            zip_path=str(zip_file),
            completed_hours_ago=25,
        )
        async with Session() as db:
            expired_count = await queue.cleanup_expired_pack_zips(db)
        assert expired_count == 1
        assert not zip_file.exists(), "zip file should be unlinked"
        task = await _load_task(Session, "old1")
        assert task.status == "expired"

    _run(run())


def test_cleanup_leaves_recent_completed_alone(tmp_path: Path) -> None:
    """A 1h-old completed task is still within the 24h window — no touch."""
    zip_file = tmp_path / "materials_recent.zip"
    zip_file.write_bytes(b"fresh")

    async def run() -> None:
        Session = await _make_session()
        await _insert_task(
            Session,
            task_id="recent1",
            zip_path=str(zip_file),
            completed_hours_ago=1,
        )
        async with Session() as db:
            expired_count = await queue.cleanup_expired_pack_zips(db)
        assert expired_count == 0
        assert zip_file.exists()
        task = await _load_task(Session, "recent1")
        assert task.status == "completed"

    _run(run())


def test_cleanup_ignores_other_task_types(tmp_path: Path) -> None:
    """generate_video tasks past 24h are untouched — cleanup is
    materials_pack-specific."""
    async def run() -> None:
        Session = await _make_session()
        await _insert_task(
            Session,
            task_id="vid1",
            task_type="generate_video",
            zip_path=None,
            completed_hours_ago=48,
        )
        async with Session() as db:
            expired_count = await queue.cleanup_expired_pack_zips(db)
        assert expired_count == 0
        task = await _load_task(Session, "vid1")
        assert task.status == "completed"

    _run(run())


def test_cleanup_ignores_failed_tasks(tmp_path: Path) -> None:
    """Failed tasks keep their status for user visibility, even after 24h."""
    async def run() -> None:
        Session = await _make_session()
        await _insert_task(
            Session,
            task_id="failed1",
            status="failed",
            zip_path=None,
            completed_hours_ago=48,
        )
        async with Session() as db:
            expired_count = await queue.cleanup_expired_pack_zips(db)
        assert expired_count == 0
        task = await _load_task(Session, "failed1")
        assert task.status == "failed"

    _run(run())


def test_cleanup_tolerates_missing_zip(tmp_path: Path) -> None:
    """If the zip was already deleted out-of-band (disk wipe, admin action),
    we still transition status to 'expired' so the UI reflects reality."""
    ghost_path = str(tmp_path / "never_existed.zip")

    async def run() -> None:
        Session = await _make_session()
        await _insert_task(
            Session,
            task_id="ghost1",
            zip_path=ghost_path,
            completed_hours_ago=25,
        )
        async with Session() as db:
            expired_count = await queue.cleanup_expired_pack_zips(db)
        assert expired_count == 1
        task = await _load_task(Session, "ghost1")
        assert task.status == "expired"

    _run(run())


def test_cleanup_tolerates_null_zip_path(tmp_path: Path) -> None:
    """A completed task without result.zip_path (e.g. legacy zombie) still
    gets expired — otherwise it lingers forever skewing the DB."""
    async def run() -> None:
        Session = await _make_session()
        await _insert_task(
            Session,
            task_id="null1",
            zip_path=None,
            completed_hours_ago=25,
        )
        async with Session() as db:
            expired_count = await queue.cleanup_expired_pack_zips(db)
        assert expired_count == 1
        task = await _load_task(Session, "null1")
        assert task.status == "expired"

    _run(run())


def test_cleanup_custom_retention_hours(tmp_path: Path) -> None:
    """A shorter retention window picks up tasks that would be safe at 24h."""
    zip_file = tmp_path / "materials_6h.zip"
    zip_file.write_bytes(b"x")

    async def run() -> None:
        Session = await _make_session()
        await _insert_task(
            Session,
            task_id="6h1",
            zip_path=str(zip_file),
            completed_hours_ago=7,
        )
        async with Session() as db:
            expired_count = await queue.cleanup_expired_pack_zips(
                db, retention_hours=6
            )
        assert expired_count == 1
        assert not zip_file.exists()

    _run(run())


def test_cleanup_is_idempotent(tmp_path: Path) -> None:
    """Running cleanup twice in a row only processes each task once —
    second pass finds nothing in 'completed' state."""
    zip_file = tmp_path / "materials_dup.zip"
    zip_file.write_bytes(b"x")

    async def run() -> None:
        Session = await _make_session()
        await _insert_task(
            Session,
            task_id="dup1",
            zip_path=str(zip_file),
            completed_hours_ago=25,
        )
        async with Session() as db:
            first = await queue.cleanup_expired_pack_zips(db)
        async with Session() as db:
            second = await queue.cleanup_expired_pack_zips(db)
        assert first == 1
        assert second == 0

    _run(run())
