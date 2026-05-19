"""Tests for gateway/background_task_reconciler.py (CodeX 2026-05-19).

The reconciler closes the gap in the 2-step task enqueue pattern: when
gateway crashes between ``queue.create_task`` and ``asyncio.create_task``,
the BackgroundTask row sits at ``status='pending'`` with no live executor.
Previously ``recover_stale`` swept those rows to ``failed`` and the job
was silently lost. Now the reconciler re-launches them on startup.

Coverage (mirrors tests/test_pan_enqueue.py's launch-isolation pattern):
  - Happy path: pending row inside 24h cutoff → launched.
  - Ancient pending (created > 24h ago) → left alone for recover_stale.
  - Unknown task_type → ``mark_failed`` (not launched).
  - Missing Job row → ``mark_failed`` (not launched).
  - Duplicate fingerprint already running → skipped, not launched.
  - Reconciler ``mark_running`` runs BEFORE launch so the row's
    ``updated_at`` survives a subsequent ``recover_stale`` pass.
  - All three pan task_types dispatch via TASK_EXECUTORS without special-casing.
"""
from __future__ import annotations

import asyncio
import sys
import types
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

# --- sys.path bootstrap for bare imports (mirrors tests/pan_fixtures.py) ---

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

# Stub ``database`` so executor module imports don't pull in real PG wiring
# when the reconciler does its lazy ``from background_task_executors import
# TASK_EXECUTORS`` (which itself imports ``from database import async_session``).
_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)

# Map PG-only types so SQLite can build the schemas.
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


# --- async helper ---

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@asynccontextmanager
async def _reconciler_test_engine():
    """In-memory SQLite with BackgroundTask + Job tables.

    Reconciler reads from both; nothing else is touched at the SQL layer
    in the reconciler's own code (executor launch is short-circuited by
    the test's monkeypatch).
    """
    from background_task_models import BackgroundTask
    from models import Job

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            for table_cls in (BackgroundTask, Job):
                await conn.run_sync(
                    lambda c, t=table_cls: t.__table__.create(c),
                )
        yield engine
    finally:
        await engine.dispose()


async def _new_session(engine):
    return async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )


def _install_launch_capture(monkeypatch):
    """Replace _launch_pending_executor with a no-op that records every
    call. Returns the launched list. Mirrors
    tests/test_pan_enqueue.py::_install_launch_capture."""
    from background_task_reconciler import _launch_pending_executor  # noqa: F401
    import background_task_reconciler as mod

    launched: list[dict] = []

    def fake_launch(coro, name: str):
        launched.append({"name": name, "coro_qualname": coro.__qualname__})
        coro.close()  # avoid RuntimeWarning: coroutine never awaited
        return None

    monkeypatch.setattr(mod, "_launch_pending_executor", fake_launch)
    return launched


async def _insert_job(engine, *, job_id: str, user_id: uuid.UUID,
                     project_dir: str | None = None):
    from models import Job

    row_id = uuid.uuid4()
    values = {
        "id": row_id,
        "job_id": job_id,
        "user_id": user_id,
        "status": "succeeded",
    }
    if project_dir is not None:
        values["project_dir"] = project_dir
    async with engine.begin() as conn:
        await conn.execute(Job.__table__.insert().values(**values))


async def _insert_bg_task(
    engine,
    *,
    task_id: str,
    job_id: str,
    user_id: uuid.UUID,
    task_type: str = "materials_pack",
    status: str = "pending",
    created_at: datetime | None = None,
    params: dict | None = None,
    fingerprint: str | None = None,
):
    from background_task_models import BackgroundTask
    from background_task_queue import compute_params_fingerprint

    params = params if params is not None else {"items": ["x"]}
    fingerprint = fingerprint or compute_params_fingerprint(params)
    created_at = created_at or datetime.now(timezone.utc)
    values = {
        "id": task_id,
        "job_id": job_id,
        "user_id": user_id,
        "task_type": task_type,
        "params": params,
        "params_fingerprint": fingerprint,
        "status": status,
        "created_at": created_at,
        "updated_at": created_at,
    }
    async with engine.begin() as conn:
        await conn.execute(BackgroundTask.__table__.insert().values(**values))


# =========================================================================
# Happy path: recent pending → launched
# =========================================================================


def test_recent_pending_is_launched(monkeypatch):
    """A pending row with created_at within 24h → launched via
    _launch_pending_executor + atomically marked running."""
    from background_task_reconciler import reconcile_pending_tasks
    from background_task_models import BackgroundTask

    launched = _install_launch_capture(monkeypatch)

    user_id = uuid.uuid4()

    async def _go():
        async with _reconciler_test_engine() as engine:
            Session = await _new_session(engine)

            await _insert_job(
                engine, job_id="job_recent", user_id=user_id,
                project_dir="/tmp/project_recent",
            )
            await _insert_bg_task(
                engine, task_id="t_recent",
                job_id="job_recent", user_id=user_id,
                task_type="materials_pack",
            )

            async with Session() as db:
                stats = await reconcile_pending_tasks(db)

            assert stats["launched"] == 1
            assert stats["failed"] == 0
            assert stats["skipped_duplicate"] == 0
            assert stats["total"] == 1

            # Launch was recorded.
            assert len(launched) == 1
            assert launched[0]["name"].startswith("bgtask-reconcile-materials_pack-")
            assert "t_recent" in launched[0]["name"]

            # Row was promoted to running BEFORE launch (so the subsequent
            # recover_stale sweep with cutoff=startup will skip it).
            async with Session() as db:
                row = (await db.execute(
                    select(BackgroundTask.status)
                    .where(BackgroundTask.id == "t_recent")
                )).scalar_one()
            assert row == "running"

    _run(_go())


# =========================================================================
# Cutoff: ancient pending (created > 24h ago) → NOT launched
# =========================================================================


def test_ancient_pending_is_skipped(monkeypatch):
    """Pending rows older than 24h are left alone for recover_stale to
    handle. The reconciler reports them only via the total count."""
    from background_task_reconciler import reconcile_pending_tasks
    from background_task_models import BackgroundTask

    launched = _install_launch_capture(monkeypatch)

    user_id = uuid.uuid4()

    async def _go():
        async with _reconciler_test_engine() as engine:
            Session = await _new_session(engine)

            ancient = datetime.now(timezone.utc) - timedelta(hours=25)
            await _insert_job(
                engine, job_id="job_old", user_id=user_id,
                project_dir="/tmp/project_old",
            )
            await _insert_bg_task(
                engine, task_id="t_old",
                job_id="job_old", user_id=user_id,
                task_type="materials_pack",
                created_at=ancient,
            )

            async with Session() as db:
                stats = await reconcile_pending_tasks(db)

            assert stats["launched"] == 0
            assert stats["failed"] == 0
            assert stats["total"] == 0  # ancient row not in select-set
            assert launched == []

            # Row is still pending — recover_stale's responsibility now.
            async with Session() as db:
                row = (await db.execute(
                    select(BackgroundTask.status)
                    .where(BackgroundTask.id == "t_old")
                )).scalar_one()
            assert row == "pending"

    _run(_go())


# =========================================================================
# Unknown task_type → mark_failed (not launched)
# =========================================================================


def test_unknown_task_type_is_marked_failed(monkeypatch):
    from background_task_reconciler import reconcile_pending_tasks
    from background_task_models import BackgroundTask

    launched = _install_launch_capture(monkeypatch)

    user_id = uuid.uuid4()

    async def _go():
        async with _reconciler_test_engine() as engine:
            Session = await _new_session(engine)

            await _insert_job(
                engine, job_id="job_unknown", user_id=user_id,
                project_dir="/tmp/project_unknown",
            )
            await _insert_bg_task(
                engine, task_id="t_unknown",
                job_id="job_unknown", user_id=user_id,
                task_type="not_a_real_task_type",
            )

            async with Session() as db:
                stats = await reconcile_pending_tasks(db)

            assert stats["launched"] == 0
            assert stats["failed"] == 1
            assert stats["total"] == 1
            assert launched == []

            async with Session() as db:
                row = (await db.execute(
                    select(BackgroundTask.status, BackgroundTask.error)
                    .where(BackgroundTask.id == "t_unknown")
                )).one()
            assert row.status == "failed"
            assert "未知任务类型" in row.error

    _run(_go())


# =========================================================================
# Missing Job row → mark_failed (not launched)
# =========================================================================


def test_missing_job_is_marked_failed(monkeypatch):
    from background_task_reconciler import reconcile_pending_tasks
    from background_task_models import BackgroundTask

    launched = _install_launch_capture(monkeypatch)

    user_id = uuid.uuid4()

    async def _go():
        async with _reconciler_test_engine() as engine:
            Session = await _new_session(engine)

            # NOTE: no _insert_job — the task references a job that doesn't
            # exist in the jobs table.
            await _insert_bg_task(
                engine, task_id="t_orphan",
                job_id="job_does_not_exist", user_id=user_id,
                task_type="materials_pack",
            )

            async with Session() as db:
                stats = await reconcile_pending_tasks(db)

            assert stats["launched"] == 0
            assert stats["failed"] == 1
            assert launched == []

            async with Session() as db:
                row = (await db.execute(
                    select(BackgroundTask.status, BackgroundTask.error)
                    .where(BackgroundTask.id == "t_orphan")
                )).one()
            assert row.status == "failed"
            assert "Job" in row.error and "不存在" in row.error

    _run(_go())


# =========================================================================
# Defensive duplicate guard: same fingerprint already running → skipped
# =========================================================================


def test_duplicate_running_fingerprint_is_skipped(monkeypatch):
    """The partial unique index makes this state nominally impossible but
    if it ever happens (manual SQL, migration accident), the reconciler
    must NOT launch a second executor — two concurrent executors on the
    same task would race each other to write terminal status."""
    from background_task_reconciler import reconcile_pending_tasks
    from background_task_models import BackgroundTask
    from background_task_queue import compute_params_fingerprint

    launched = _install_launch_capture(monkeypatch)

    user_id = uuid.uuid4()
    params = {"items": ["dup"]}
    fingerprint = compute_params_fingerprint(params)

    async def _go():
        async with _reconciler_test_engine() as engine:
            Session = await _new_session(engine)

            await _insert_job(
                engine, job_id="job_dup", user_id=user_id,
                project_dir="/tmp/project_dup",
            )
            # Insert the rows directly so we can bypass the partial unique
            # index (SQLite enforces it but inserting first the running
            # one then the pending one is fine — index targets BOTH).
            # We disable the constraint by giving the rows DIFFERENT
            # fingerprints to insert, then patch the pending row's
            # fingerprint to collide post-hoc. (SQLite has no SET CONSTRAINT
            # DEFERRED, so we go via two distinct rows.)
            await _insert_bg_task(
                engine, task_id="t_running",
                job_id="job_dup", user_id=user_id,
                task_type="materials_pack",
                status="running",
                params=params,
                fingerprint=fingerprint,
            )
            await _insert_bg_task(
                engine, task_id="t_pending_dup",
                job_id="job_dup", user_id=user_id,
                task_type="materials_pack",
                status="pending",
                params={"items": ["different_to_avoid_index"]},
                fingerprint="distinct_to_avoid_index_violation",
            )
            # Now post-hoc patch the pending row to share the running
            # row's fingerprint. The partial unique index in SQLite was
            # checked at INSERT only; UPDATE will trigger it. So we drop
            # the unique index first.
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "DROP INDEX IF EXISTS idx_bg_tasks_active"
                )
                await conn.exec_driver_sql(
                    "UPDATE background_tasks SET params_fingerprint = :fp "
                    "WHERE id = 't_pending_dup'",
                    {"fp": fingerprint},
                )

            async with Session() as db:
                stats = await reconcile_pending_tasks(db)

            assert stats["launched"] == 0
            assert stats["skipped_duplicate"] == 1
            assert launched == []

            # Pending row was NOT promoted to running.
            async with Session() as db:
                row = (await db.execute(
                    select(BackgroundTask.status)
                    .where(BackgroundTask.id == "t_pending_dup")
                )).scalar_one()
            assert row == "pending"

    _run(_go())


# =========================================================================
# Reconciler + recover_stale interaction: just-launched rows must not be
# clobbered by the subsequent recover_stale pass.
# =========================================================================


def test_reconciler_protects_launched_rows_from_recover_stale(monkeypatch):
    """End-to-end: reconciler launches a pending row and marks_running;
    recover_stale(cutoff_dt=startup) then sees updated_at >= cutoff and
    leaves it alone."""
    import background_task_queue as queue
    from background_task_reconciler import reconcile_pending_tasks
    from background_task_models import BackgroundTask

    _install_launch_capture(monkeypatch)

    user_id = uuid.uuid4()

    async def _go():
        async with _reconciler_test_engine() as engine:
            Session = await _new_session(engine)

            await _insert_job(
                engine, job_id="job_protect", user_id=user_id,
                project_dir="/tmp/project_protect",
            )
            # Insert as if previously created — created_at and updated_at
            # both in the past.
            past = datetime.now(timezone.utc) - timedelta(hours=1)
            await _insert_bg_task(
                engine, task_id="t_protect",
                job_id="job_protect", user_id=user_id,
                task_type="materials_pack",
                created_at=past,
            )

            startup_dt = datetime.now(timezone.utc)

            async with Session() as db:
                stats = await reconcile_pending_tasks(db)
                assert stats["launched"] == 1

                recovered = await queue.recover_stale(db, cutoff_dt=startup_dt)
                # The just-launched row had updated_at bumped past startup_dt
                # via the reconciler's mark_running, so recover_stale skips it.
                assert recovered == 0

            async with Session() as db:
                row = (await db.execute(
                    select(BackgroundTask.status, BackgroundTask.error)
                    .where(BackgroundTask.id == "t_protect")
                )).one()
            # Still 'running' (not "Gateway 重启").
            assert row.status == "running"
            assert row.error is None

    _run(_go())


# =========================================================================
# All pan task_types dispatch via the generic reconciler (no special-casing).
# =========================================================================


@pytest.mark.parametrize("task_type", [
    "materials_pack", "generate_video",
    "pan_backup", "pan_restore", "pan_residue_cleanup",
])
def test_reconciler_handles_all_registered_task_types(monkeypatch, task_type):
    """All five task types currently in TASK_EXECUTORS must be launchable
    via the reconciler — i.e., the dispatch is task-type-agnostic."""
    from background_task_reconciler import reconcile_pending_tasks

    launched = _install_launch_capture(monkeypatch)

    user_id = uuid.uuid4()

    async def _go():
        async with _reconciler_test_engine() as engine:
            Session = await _new_session(engine)

            await _insert_job(
                engine, job_id=f"job_{task_type}", user_id=user_id,
                project_dir=f"/tmp/project_{task_type}",
            )
            await _insert_bg_task(
                engine, task_id=f"t_{task_type}",
                job_id=f"job_{task_type}", user_id=user_id,
                task_type=task_type,
            )

            async with Session() as db:
                stats = await reconcile_pending_tasks(db)

            assert stats["launched"] == 1
            assert len(launched) == 1
            assert task_type in launched[0]["name"]

    _run(_go())
