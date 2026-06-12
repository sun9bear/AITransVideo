from __future__ import annotations

import asyncio
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


# Stub database before importing gateway modules (matches other gateway tests).
# Without this, the real `database` module gets cached in sys.modules during
# collection and later test files' `sys.modules.setdefault("database", fake)`
# becomes a no-op (order-dependent pollution).
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
_fake_database.init_db = MagicMock()
sys.modules.setdefault("database", _fake_database)

from models import Job  # noqa: E402
import admin_disk_api  # noqa: E402


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
    project_dir: Path,
    now: datetime,
    status: str = "succeeded",
    title: str = "",
    display_name: str | None = None,
    expires_delta: timedelta = timedelta(days=-1),
    role_snapshot: str | None = None,
) -> None:
    async with Session() as db:
        db.add(
            Job(
                id=uuid.uuid4(),
                job_id=job_id,
                user_id=uuid.uuid4(),
                source_type="youtube_url",
                source_ref="https://example.com/watch?v=x",
                title=title,
                display_name=display_name,
                speakers="auto",
                status=status,
                project_dir=str(project_dir),
                role_snapshot=role_snapshot,
                created_at=now - timedelta(days=10),
                updated_at=now - timedelta(days=1),
                expires_at=now + expires_delta,
            )
        )
        await db.commit()


def _job_dir(root: Path, user: str, job_id: str, payload: bytes = b"x") -> Path:
    path = root / user / job_id
    path.mkdir(parents=True)
    (path / "payload.bin").write_bytes(payload)
    return path


def test_overview_classifies_orphan_expired_and_admin_protected(tmp_path):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    root = tmp_path / "projects"
    orphan = _job_dir(root, "user-1", "job_orphan_12345678", b"orphan")
    (orphan / "manifest.json").write_text('{"title":"孤儿任务"}', encoding="utf-8")
    expired = _job_dir(root, "user-1", "job_expired_12345678", b"expired")
    protected = _job_dir(root, "user-1", "job_admin_12345678", b"admin")
    active = _job_dir(root, "user-1", "job_active_12345678", b"active")

    async def run():
        Session = await _make_session()
        await _insert_job(
            Session,
            job_id="job_expired_12345678",
            project_dir=expired,
            now=now,
            display_name="过期任务",
        )
        await _insert_job(
            Session,
            job_id="job_admin_12345678",
            project_dir=protected,
            now=now,
            display_name="管理员保护任务",
            role_snapshot="admin",
        )
        await _insert_job(
            Session,
            job_id="job_active_12345678",
            project_dir=active,
            now=now,
            display_name="未过期任务",
            expires_delta=timedelta(days=3),
        )
        async with Session() as db:
            overview = await admin_disk_api.build_disk_overview(
                db,
                project_root=root,
                now=now,
            )
        assert overview["summary"]["orphan_dirs_count"] == 1
        assert overview["categories"]["orphan_dirs"][0]["title"] == "孤儿任务"
        assert overview["summary"]["expired_dirs_count"] == 1
        assert overview["categories"]["expired_dirs"][0]["job_id"] == "job_expired_12345678"
        assert overview["summary"]["protected_expired_dirs_count"] == 1
        assert overview["categories"]["protected_expired_dirs"][0]["role_snapshot"] == "admin"
        assert overview["summary"]["disk_job_dir_count"] == 4

    _run(run())


def test_cleanup_orphan_dirs_rechecks_db_and_deletes_only_absent_jobs(tmp_path):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    root = tmp_path / "projects"
    orphan = _job_dir(root, "user-1", "job_orphan_abcdef12", b"orphan")
    db_backed = _job_dir(root, "user-1", "job_db_abcdef12", b"db")

    async def run():
        Session = await _make_session()
        await _insert_job(
            Session,
            job_id="job_db_abcdef12",
            project_dir=db_backed,
            now=now,
        )
        async with Session() as db:
            with pytest.raises(HTTPException) as exc:
                await admin_disk_api.cleanup_orphan_dirs(
                    db,
                    job_ids=["job_orphan_abcdef12", "job_db_abcdef12"],
                    dry_run=False,
                    project_root=root,
                    safe_roots=(root,),
                    enforce_safe_root=False,
                )
        assert exc.value.status_code == 409
        assert orphan.exists()
        assert db_backed.exists()

        async with Session() as db:
            dry = await admin_disk_api.cleanup_orphan_dirs(
                db,
                job_ids=["job_orphan_abcdef12"],
                dry_run=True,
                project_root=root,
                safe_roots=(root,),
                enforce_safe_root=False,
            )
        assert dry["items"][0]["status"] == "would_delete"
        assert orphan.exists()

        async with Session() as db:
            done = await admin_disk_api.cleanup_orphan_dirs(
                db,
                job_ids=["job_orphan_abcdef12"],
                dry_run=False,
                project_root=root,
                safe_roots=(root,),
                enforce_safe_root=False,
            )
        assert done["freed_bytes"] > 0
        assert not orphan.exists()
        assert db_backed.exists()

    _run(run())


def test_cleanup_orphan_dirs_rejects_unsafe_scan_root_by_default(tmp_path):
    root = tmp_path / "projects"
    _job_dir(root, "user-1", "job_orphan_unsafe12", b"orphan")

    async def run():
        Session = await _make_session()
        async with Session() as db:
            with pytest.raises(HTTPException) as exc:
                await admin_disk_api.cleanup_orphan_dirs(
                    db,
                    job_ids=["job_orphan_unsafe12"],
                    dry_run=False,
                    project_root=root,
                )
        assert exc.value.status_code == 400
        assert (root / "user-1" / "job_orphan_unsafe12").exists()

    _run(run())


def test_resize_filesystem_rejects_when_feature_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("AVT_ADMIN_DISK_RESIZE_ENABLED", raising=False)
    root = tmp_path / "projects"
    root.mkdir()

    async def run():
        with pytest.raises(HTTPException) as exc:
            await admin_disk_api.resize_filesystem(
                dry_run=False,
                confirm=True,
                project_root=root,
            )
        assert exc.value.status_code == 403

    _run(run())


def test_resize_filesystem_delegates_to_helper_under_lock(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    before = {
        "feature_enabled": True,
        "can_resize": True,
        "needs_resize": True,
        "device": "/dev/testdisk",
        "device_bytes": 200,
        "filesystem_bytes": 100,
        "reason": "needs resize",
    }
    after = {
        **before,
        "can_resize": False,
        "needs_resize": False,
        "filesystem_bytes": 200,
        "reason": "done",
    }
    statuses = [before, after]
    calls: list[dict] = []

    def fake_status(path):
        assert path == root
        return statuses.pop(0)

    async def fake_helper(*, dry_run, confirm, timeout):  # noqa: ARG001
        calls.append({"dry_run": dry_run, "confirm": confirm})
        return {
            "dry_run": False,
            "ran": True,
            "device": "/dev/testdisk",
            "output": "resize2fs ok",
        }

    monkeypatch.setenv("AVT_ADMIN_DISK_RESIZE_ENABLED", "true")
    monkeypatch.setattr(admin_disk_api, "_build_resize_hint", fake_status)
    monkeypatch.setattr(admin_disk_api, "_post_resize_helper", fake_helper)

    async def run():
        result = await admin_disk_api.resize_filesystem(
            dry_run=False,
            confirm=True,
            project_root=root,
        )
        assert result["ran"] is True
        assert result["device"] == "/dev/testdisk"
        assert result["after"]["reason"] == "done"

    _run(run())
    assert calls == [{"dry_run": False, "confirm": True}]
