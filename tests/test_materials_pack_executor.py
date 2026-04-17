"""Tests for gateway.background_task_executors.execute_materials_pack.

Focus: packaging logic, path safety, size limit, retention cleanup.
Uses an in-memory SQLite DB injected via patching ``async_session``.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)

# Ensure PG-specific types render under sqlite for this test file too.
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
import background_task_executors as executors  # noqa: E402
import materials_pack_common as mpc  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _setup_db() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: BackgroundTask.__table__.create(c))
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _make_project_dir(tmp_path: Path) -> Path:
    """Build a minimal project dir with source_video + subtitles artifacts."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    # Files
    source_video = project_dir / "source.mp4"
    source_video.write_bytes(b"fake mp4 bytes" * 100)
    subtitles_zh = project_dir / "subs_zh.srt"
    subtitles_zh.write_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n", encoding="utf-8")
    # Manifest
    manifest = {
        "artifact_index": {
            "source.original_video": str(source_video),
            "editor.subtitles": str(subtitles_zh),
        }
    }
    (project_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return project_dir


def test_materials_pack_happy_path(tmp_path):
    async def _go() -> None:
        Session = await _setup_db()
        project_dir = _make_project_dir(tmp_path)
        user_id = uuid.uuid4()

        async with Session() as db:
            task_id, _ = await queue_mod.create_task(
                db, job_id="job_X", user_id=user_id,
                task_type="materials_pack",
                params={"items": ["source_video", "subtitles"]},
            )
            await db.commit()

        # Patch executor's async_session to use our test DB
        with patch.object(executors, "async_session", Session):
            await executors.execute_materials_pack(
                task_id=task_id,
                job_id="job_X",
                project_dir=project_dir,
                params={"items": ["source_video", "subtitles"]},
            )

        async with Session() as db:
            task = await queue_mod.get_task(db, task_id=task_id, user_id=user_id)
            assert task is not None
            assert task["status"] == "completed", task
            assert task["result"]["filename"].endswith(".zip")
            zip_path = Path(task["result"]["zip_path"])
            assert zip_path.exists()
            # Verify zip contents
            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())
            assert "source.mp4" in names
            assert "subs_zh.srt" in names

    _run(_go())


def test_materials_pack_retention_deletes_old_zips(tmp_path):
    async def _go() -> None:
        Session = await _setup_db()
        project_dir = _make_project_dir(tmp_path)
        user_id = uuid.uuid4()

        exports_dir = project_dir / "exports"
        exports_dir.mkdir()
        old_zip = exports_dir / "materials_oldtask.zip"
        old_zip.write_bytes(b"stale")
        unrelated_file = exports_dir / "not_materials.txt"
        unrelated_file.write_text("keep me")

        async with Session() as db:
            task_id, _ = await queue_mod.create_task(
                db, job_id="job_X", user_id=user_id,
                task_type="materials_pack",
                params={"items": ["source_video"]},
            )
            await db.commit()

        with patch.object(executors, "async_session", Session):
            await executors.execute_materials_pack(
                task_id=task_id,
                job_id="job_X",
                project_dir=project_dir,
                params={"items": ["source_video"]},
            )

        assert not old_zip.exists(), "Old materials zip should be deleted"
        assert unrelated_file.exists(), "Unrelated files should be preserved"
        new_zips = list(exports_dir.glob("materials_*.zip"))
        assert len(new_zips) == 1

    _run(_go())


def test_materials_pack_rejects_empty_selection(tmp_path):
    async def _go() -> None:
        Session = await _setup_db()
        project_dir = _make_project_dir(tmp_path)
        user_id = uuid.uuid4()

        async with Session() as db:
            task_id, _ = await queue_mod.create_task(
                db, job_id="job_X", user_id=user_id,
                task_type="materials_pack", params={"items": []},
            )
            await db.commit()

        with patch.object(executors, "async_session", Session):
            await executors.execute_materials_pack(
                task_id=task_id,
                job_id="job_X",
                project_dir=project_dir,
                params={"items": []},
            )

        async with Session() as db:
            task = await queue_mod.get_task(db, task_id=task_id, user_id=user_id)
            assert task["status"] == "failed"
            assert "素材" in task["error"]

    _run(_go())


def test_materials_pack_rejects_oversize(tmp_path, monkeypatch):
    async def _go() -> None:
        Session = await _setup_db()
        project_dir = _make_project_dir(tmp_path)
        user_id = uuid.uuid4()

        # Temporarily lower the size limit so the test is fast
        monkeypatch.setattr(executors, "MAX_ZIP_SIZE_BYTES", 100)

        async with Session() as db:
            task_id, _ = await queue_mod.create_task(
                db, job_id="job_X", user_id=user_id,
                task_type="materials_pack", params={"items": ["source_video"]},
            )
            await db.commit()

        with patch.object(executors, "async_session", Session):
            await executors.execute_materials_pack(
                task_id=task_id,
                job_id="job_X",
                project_dir=project_dir,
                params={"items": ["source_video"]},
            )

        async with Session() as db:
            task = await queue_mod.get_task(db, task_id=task_id, user_id=user_id)
            assert task["status"] == "failed"
            assert "过大" in task["error"]

    _run(_go())


def test_materials_pack_rejects_path_traversal(tmp_path):
    """An artifact_index entry pointing outside project_dir must not be packed."""
    project_dir = tmp_path / "proj2"
    project_dir.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET")
    manifest = {
        "artifact_index": {
            "source.original_video": str(outside),
        }
    }
    (project_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    artifact_index = mpc.load_artifact_index(project_dir)
    resolved = mpc.resolve_artifact_path(
        project_dir, artifact_index, "source.original_video",
    )
    # Must be rejected as outside project_dir
    assert resolved is None
