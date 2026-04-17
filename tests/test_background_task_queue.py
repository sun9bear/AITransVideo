"""Unit tests for gateway.background_task_queue with in-memory SQLite."""
from __future__ import annotations

import asyncio
import sys
import types
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Column, String, Text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

# Stub database module — queue module doesn't import it, but main.py / others
# pulled during import might. Not strictly needed for this test file.
_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)

# Map PostgreSQL-only types to SQLite-compatible equivalents so Base.metadata
# can round-trip through an in-memory test DB. Only affects the test env.
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


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


async def _make_session() -> tuple[async_sessionmaker[AsyncSession], object]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        # Create only the background_tasks table — avoid unrelated tables that
        # depend on Postgres-specific features (partial indexes on predicates etc.)
        await conn.run_sync(lambda sync_conn: BackgroundTask.__table__.create(sync_conn))
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return Session, engine


def test_fingerprint_deterministic():
    fp1 = queue.compute_params_fingerprint({"items": ["a", "b"]})
    fp2 = queue.compute_params_fingerprint({"items": ["a", "b"]})
    assert fp1 == fp2
    # Order-insensitive for dict keys
    fp3 = queue.compute_params_fingerprint({"x": 1, "y": 2})
    fp4 = queue.compute_params_fingerprint({"y": 2, "x": 1})
    assert fp3 == fp4


def test_fingerprint_distinguishes_different_params():
    fp_a = queue.compute_params_fingerprint({"items": ["a"]})
    fp_b = queue.compute_params_fingerprint({"items": ["b"]})
    assert fp_a != fp_b


def test_fingerprint_matches_frontend_canonical_form():
    # Mirror of JS canonicalJson({}).
    import hashlib
    assert queue.compute_params_fingerprint({}) == hashlib.sha256(b"{}").hexdigest()


def test_create_task_dedupes_same_fingerprint():
    async def _go() -> None:
        Session, _ = await _make_session()
        user_id = uuid.uuid4()
        async with Session() as db:
            tid1, created1 = await queue.create_task(
                db,
                job_id="job_A",
                user_id=user_id,
                task_type="materials_pack",
                params={"items": ["x"]},
            )
            await db.commit()
            assert created1 is True
            tid2, created2 = await queue.create_task(
                db,
                job_id="job_A",
                user_id=user_id,
                task_type="materials_pack",
                params={"items": ["x"]},
            )
            await db.commit()
            assert created2 is False
            assert tid1 == tid2
    _run(_go())


def test_create_task_different_params_are_independent():
    async def _go() -> None:
        Session, _ = await _make_session()
        user_id = uuid.uuid4()
        async with Session() as db:
            tid1, _ = await queue.create_task(
                db, job_id="job_A", user_id=user_id,
                task_type="materials_pack", params={"items": ["x"]},
            )
            tid2, _ = await queue.create_task(
                db, job_id="job_A", user_id=user_id,
                task_type="materials_pack", params={"items": ["x", "y"]},
            )
            await db.commit()
            assert tid1 != tid2
    _run(_go())


def test_latest_active_only_returns_pending_or_running():
    async def _go() -> None:
        Session, _ = await _make_session()
        user_id = uuid.uuid4()
        async with Session() as db:
            tid, _ = await queue.create_task(
                db, job_id="job_A", user_id=user_id,
                task_type="materials_pack", params={"items": ["x"]},
            )
            await db.commit()
            fp = queue.compute_params_fingerprint({"items": ["x"]})

            latest = await queue.get_latest_active(
                db, job_id="job_A", user_id=user_id,
                task_type="materials_pack", params_fingerprint=fp,
            )
            assert latest is not None
            assert latest["task_id"] == tid

            await queue.mark_completed(db, tid, {"zip_path": "/tmp/x.zip"})

            # Completed task should NOT surface as latest_active
            latest2 = await queue.get_latest_active(
                db, job_id="job_A", user_id=user_id,
                task_type="materials_pack", params_fingerprint=fp,
            )
            assert latest2 is None
    _run(_go())


def test_get_latest_returns_completed_for_state_restore():
    """P1 from CodeX review: get_latest (include_terminal=True) must return
    completed tasks so the frontend can show 素材包可下载 after refresh."""
    async def _go() -> None:
        Session, _ = await _make_session()
        user_id = uuid.uuid4()
        async with Session() as db:
            tid, _ = await queue.create_task(
                db, job_id="job_A", user_id=user_id,
                task_type="materials_pack", params={"items": ["x"]},
            )
            await db.commit()
            fp = queue.compute_params_fingerprint({"items": ["x"]})
            await queue.mark_completed(db, tid, {"zip_path": "/tmp/x.zip"})

            latest = await queue.get_latest(
                db, job_id="job_A", user_id=user_id,
                task_type="materials_pack", params_fingerprint=fp,
            )
            assert latest is not None, "completed task must be restorable"
            assert latest["status"] == "completed"
            assert latest["task_id"] == tid

            # include_terminal=False behaves like get_latest_active
            none_ = await queue.get_latest(
                db, job_id="job_A", user_id=user_id,
                task_type="materials_pack", params_fingerprint=fp,
                include_terminal=False,
            )
            assert none_ is None
    _run(_go())


def test_partial_unique_index_rejects_second_active_with_same_fingerprint():
    """Bypass create_task's fast-path and prove the DB-level partial unique
    index is the correctness barrier — two raw inserts with overlapping
    (job_id, task_type, fingerprint) in pending/running MUST raise
    IntegrityError."""
    from sqlalchemy.exc import IntegrityError

    async def _go() -> None:
        Session, _ = await _make_session()
        user_id = uuid.uuid4()
        fp = queue.compute_params_fingerprint({"items": ["x"]})
        async with Session() as db:
            t1 = BackgroundTask(
                id="aaaaaaaaaaaa",
                job_id="job_R",
                user_id=user_id,
                task_type="materials_pack",
                params={"items": ["x"]},
                params_fingerprint=fp,
                status="pending",
            )
            db.add(t1)
            await db.flush()  # first insert OK

            t2 = BackgroundTask(
                id="bbbbbbbbbbbb",
                job_id="job_R",
                user_id=user_id,
                task_type="materials_pack",
                params={"items": ["x"]},
                params_fingerprint=fp,
                status="pending",
            )
            db.add(t2)
            with pytest.raises(IntegrityError):
                await db.flush()  # second must violate idx_bg_tasks_active
    _run(_go())


def test_create_task_catches_integrity_error_from_race():
    """End-to-end race: two creators both miss the fast-path, second INSERT
    collides on partial unique index, create_task must catch IntegrityError
    and fall back to the winning row."""
    async def _go() -> None:
        Session, _ = await _make_session()
        user_id = uuid.uuid4()
        async with Session() as db:
            # First call creates the row via the normal path
            tid1, created1 = await queue.create_task(
                db, job_id="job_RC", user_id=user_id,
                task_type="materials_pack", params={"items": ["x"]},
            )
            await db.commit()
            assert created1 is True

            # Force the next create_task to skip its pre-check, so the second
            # INSERT reaches the DB-level unique index and collides. The
            # recovery path must re-query via the real _fetch_active.
            original = queue._fetch_active
            call_count = {"n": 0}

            async def fake_fetch_active(*args, **kwargs):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return None  # simulate missed fast-path
                return await original(*args, **kwargs)

            queue._fetch_active = fake_fetch_active
            try:
                tid2, created2 = await queue.create_task(
                    db, job_id="job_RC", user_id=user_id,
                    task_type="materials_pack", params={"items": ["x"]},
                )
            finally:
                queue._fetch_active = original

            assert created2 is False, "IntegrityError recovery must return created=False"
            assert tid2 == tid1, "Recovery must return the winning task_id"
            # And we must have hit the recovery _fetch_active call at least once
            assert call_count["n"] >= 2
    _run(_go())


def test_create_after_completion_with_same_fingerprint_succeeds():
    """The partial unique index predicate excludes terminal statuses, so a
    user can re-pack with the same items after the previous task completed."""
    async def _go() -> None:
        Session, _ = await _make_session()
        user_id = uuid.uuid4()
        async with Session() as db:
            tid1, created1 = await queue.create_task(
                db, job_id="job_Z", user_id=user_id,
                task_type="materials_pack", params={"items": ["x"]},
            )
            await db.commit()
            assert created1 is True
            await queue.mark_completed(db, tid1, {"ok": True})

            # Same fingerprint, old task is terminal → new row must succeed
            tid2, created2 = await queue.create_task(
                db, job_id="job_Z", user_id=user_id,
                task_type="materials_pack", params={"items": ["x"]},
            )
            await db.commit()
            assert created2 is True
            assert tid2 != tid1
    _run(_go())


def test_state_machine_transitions():
    async def _go() -> None:
        Session, _ = await _make_session()
        user_id = uuid.uuid4()
        async with Session() as db:
            tid, _ = await queue.create_task(
                db, job_id="job_B", user_id=user_id,
                task_type="generate_video", params={},
            )
            await db.commit()

            await queue.mark_running(db, tid)
            task = await queue.get_task(db, task_id=tid, user_id=user_id)
            assert task is not None
            assert task["status"] == "running"

            await queue.update_progress(db, tid, {"stage": "muxing", "percent": 40})
            task = await queue.get_task(db, task_id=tid, user_id=user_id)
            assert task["progress"] == {"stage": "muxing", "percent": 40}

            await queue.mark_completed(db, tid, {"video_ready": True})
            task = await queue.get_task(db, task_id=tid, user_id=user_id)
            assert task["status"] == "completed"
            assert task["result"] == {"video_ready": True}
    _run(_go())


def test_mark_failed_sets_error():
    async def _go() -> None:
        Session, _ = await _make_session()
        user_id = uuid.uuid4()
        async with Session() as db:
            tid, _ = await queue.create_task(
                db, job_id="job_C", user_id=user_id,
                task_type="materials_pack", params={"items": ["x"]},
            )
            await db.commit()
            await queue.mark_failed(db, tid, "disk full")
            task = await queue.get_task(db, task_id=tid, user_id=user_id)
            assert task["status"] == "failed"
            assert task["error"] == "disk full"
    _run(_go())


def test_recover_stale_clears_both_running_and_pending():
    async def _go() -> None:
        Session, _ = await _make_session()
        user_id = uuid.uuid4()
        async with Session() as db:
            t_pending, _ = await queue.create_task(
                db, job_id="job_D", user_id=user_id,
                task_type="materials_pack", params={"items": ["a"]},
            )
            t_running, _ = await queue.create_task(
                db, job_id="job_D", user_id=user_id,
                task_type="materials_pack", params={"items": ["b"]},
            )
            t_done, _ = await queue.create_task(
                db, job_id="job_D", user_id=user_id,
                task_type="materials_pack", params={"items": ["c"]},
            )
            await db.commit()
            await queue.mark_running(db, t_running)
            await queue.mark_completed(db, t_done, {"ok": True})

            recovered = await queue.recover_stale(db)
            assert recovered == 2

            task_p = await queue.get_task(db, task_id=t_pending, user_id=user_id)
            task_r = await queue.get_task(db, task_id=t_running, user_id=user_id)
            task_d = await queue.get_task(db, task_id=t_done, user_id=user_id)
            assert task_p["status"] == "failed"
            assert task_r["status"] == "failed"
            assert task_d["status"] == "completed"  # untouched
    _run(_go())


def test_get_task_enforces_user_ownership():
    async def _go() -> None:
        Session, _ = await _make_session()
        owner = uuid.uuid4()
        attacker = uuid.uuid4()
        async with Session() as db:
            tid, _ = await queue.create_task(
                db, job_id="job_E", user_id=owner,
                task_type="materials_pack", params={"items": ["x"]},
            )
            await db.commit()

            # Owner can read
            assert await queue.get_task(db, task_id=tid, user_id=owner) is not None
            # Attacker cannot
            assert await queue.get_task(db, task_id=tid, user_id=attacker) is None
            # No user_id filter returns the task
            assert await queue.get_task(db, task_id=tid) is not None
    _run(_go())
