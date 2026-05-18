"""Tests for pan executor dispatcher adapters in
gateway/background_task_executors.py.

CodeX 2026-05-18 P2: pan executors had a payload-dict signature
incompatible with the BackgroundTask dispatcher convention
`(*, task_id, job_id, project_dir, params)`. The adapters bridge the
two and drive BackgroundTask lifecycle for UI consistency.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import types
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _setup_db():
    """In-memory SQLite + BackgroundTask table."""
    from background_task_models import BackgroundTask

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: BackgroundTask.__table__.create(c))
    return async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )


# --- TASK_EXECUTORS registration ---


def test_all_three_pan_executors_registered_in_dispatch_table():
    """gateway.background_task_executors.TASK_EXECUTORS must include the
    three pan task types. Phase 7 admin API checks against this dict to
    accept incoming task_type values."""
    from background_task_executors import TASK_EXECUTORS
    assert 'pan_backup' in TASK_EXECUTORS
    assert 'pan_restore' in TASK_EXECUTORS
    assert 'pan_residue_cleanup' in TASK_EXECUTORS


# --- Signature compatibility ---


@pytest.mark.parametrize('name', [
    'execute_pan_backup_dispatched',
    'execute_pan_restore_dispatched',
    'execute_pan_residue_cleanup_dispatched',
])
def test_dispatcher_signature_matches_convention(name):
    """All three adapters must be keyword-only (task_id, job_id,
    project_dir, params) — the convention background_task_api.py:98
    calls executors with."""
    import background_task_executors as bte
    fn = getattr(bte, name)
    sig = inspect.signature(fn)
    expected = {'task_id', 'job_id', 'project_dir', 'params'}
    assert set(sig.parameters.keys()) == expected
    for param in sig.parameters.values():
        assert param.kind == inspect.Parameter.KEYWORD_ONLY


# --- Adapter wiring (payload translation + queue lifecycle) ---


def test_backup_dispatcher_translates_signature_to_payload(tmp_path):
    """Dispatcher converts (task_id, job_id, project_dir, params) into
    the payload dict the pan backup_executor consumes. params['user_id']
    becomes payload['user_id']; provider defaults to 'baidu_pan'."""
    import background_task_queue as queue_mod
    import background_task_executors as bte
    import pan.backup_executor as be_mod

    captured: list[dict] = []

    async def fake_executor(payload):
        captured.append(payload)

    async def _go():
        Session = await _setup_db()
        user_id = uuid.uuid4()
        async with Session() as db:
            task_id, _ = await queue_mod.create_task(
                db, job_id='job_X', user_id=user_id,
                task_type='pan_backup',
                params={'user_id': str(user_id)},
            )
            await db.commit()

        # Patch async_session for the dispatcher's queue.* calls,
        # and the inside-the-adapter `from ... import execute_pan_backup`
        # via the source module attribute.
        with patch.object(bte, 'async_session', Session):
            with patch.object(be_mod, 'execute_pan_backup', fake_executor):
                await bte.execute_pan_backup_dispatched(
                    task_id=task_id, job_id='job_X',
                    project_dir=tmp_path / 'ignored',
                    params={'user_id': str(user_id)},
                )

        assert len(captured) == 1
        assert captured[0]['job_id'] == 'job_X'
        assert captured[0]['user_id'] == str(user_id)
        assert captured[0]['provider'] == 'baidu_pan'  # default

        # BackgroundTask should be marked completed.
        async with Session() as db:
            task = await queue_mod.get_task(db, task_id=task_id, user_id=user_id)
            assert task['status'] == 'completed', task

    _run(_go())


def test_backup_dispatcher_propagates_provider_override(tmp_path):
    """params['provider'] overrides the default 'baidu_pan'."""
    import background_task_queue as queue_mod
    import background_task_executors as bte
    import pan.backup_executor as be_mod

    captured: list[dict] = []

    async def fake_executor(payload):
        captured.append(payload)

    async def _go():
        Session = await _setup_db()
        user_id = uuid.uuid4()
        async with Session() as db:
            task_id, _ = await queue_mod.create_task(
                db, job_id='job_Y', user_id=user_id,
                task_type='pan_backup',
                params={'user_id': str(user_id), 'provider': 'aliyun_pan'},
            )
            await db.commit()

        with patch.object(bte, 'async_session', Session):
            with patch.object(be_mod, 'execute_pan_backup', fake_executor):
                await bte.execute_pan_backup_dispatched(
                    task_id=task_id, job_id='job_Y',
                    project_dir=tmp_path / 'x',
                    params={'user_id': str(user_id),
                            'provider': 'aliyun_pan'},
                )
        assert captured[0]['provider'] == 'aliyun_pan'

    _run(_go())


def test_backup_dispatcher_marks_failed_on_executor_exception(tmp_path):
    """If the underlying executor raises, dispatcher marks task=failed
    and SWALLOWS the exception (background_task_api.py creates an
    asyncio task — re-raising would just go to the event loop unheard)."""
    import background_task_queue as queue_mod
    import background_task_executors as bte
    import pan.backup_executor as be_mod

    async def boom(payload):
        raise RuntimeError('synthetic executor failure')

    async def _go():
        Session = await _setup_db()
        user_id = uuid.uuid4()
        async with Session() as db:
            task_id, _ = await queue_mod.create_task(
                db, job_id='job_Z', user_id=user_id,
                task_type='pan_backup',
                params={'user_id': str(user_id)},
            )
            await db.commit()

        with patch.object(bte, 'async_session', Session):
            with patch.object(be_mod, 'execute_pan_backup', boom):
                # Dispatcher MUST NOT raise.
                await bte.execute_pan_backup_dispatched(
                    task_id=task_id, job_id='job_Z',
                    project_dir=tmp_path / 'x',
                    params={'user_id': str(user_id)},
                )

        async with Session() as db:
            task = await queue_mod.get_task(db, task_id=task_id, user_id=user_id)
            assert task['status'] == 'failed', task
            assert 'synthetic executor failure' in (task.get('error') or '')

    _run(_go())


# Repeat the wiring test for restore + residue_cleanup at minimum
# integration-coverage. Failure paths reused via the same pattern.


def test_restore_dispatcher_translates_signature_to_payload(tmp_path):
    import background_task_queue as queue_mod
    import background_task_executors as bte
    import pan.restore_executor as re_mod

    captured: list[dict] = []

    async def fake_executor(payload):
        captured.append(payload)

    async def _go():
        Session = await _setup_db()
        user_id = uuid.uuid4()
        async with Session() as db:
            task_id, _ = await queue_mod.create_task(
                db, job_id='job_R', user_id=user_id,
                task_type='pan_restore',
                params={'user_id': str(user_id)},
            )
            await db.commit()

        with patch.object(bte, 'async_session', Session):
            with patch.object(re_mod, 'execute_pan_restore', fake_executor):
                await bte.execute_pan_restore_dispatched(
                    task_id=task_id, job_id='job_R',
                    project_dir=tmp_path / 'x',
                    params={'user_id': str(user_id)},
                )
        assert captured[0]['job_id'] == 'job_R'

    _run(_go())


def test_residue_cleanup_dispatcher_translates_signature_to_payload(tmp_path):
    import background_task_queue as queue_mod
    import background_task_executors as bte
    import pan.residue_cleanup as rc_mod

    captured: list[dict] = []

    async def fake_executor(payload):
        captured.append(payload)

    async def _go():
        Session = await _setup_db()
        user_id = uuid.uuid4()
        backup_id = uuid.uuid4()
        async with Session() as db:
            task_id, _ = await queue_mod.create_task(
                db, job_id='job_C', user_id=user_id,
                task_type='pan_residue_cleanup',
                params={'user_id': str(user_id),
                        'backup_id': str(backup_id)},
            )
            await db.commit()

        with patch.object(bte, 'async_session', Session):
            with patch.object(rc_mod, 'execute_pan_residue_cleanup',
                               fake_executor):
                await bte.execute_pan_residue_cleanup_dispatched(
                    task_id=task_id, job_id='job_C',
                    project_dir=tmp_path / 'x',
                    params={'user_id': str(user_id),
                            'backup_id': str(backup_id)},
                )
        assert captured[0]['job_id'] == 'job_C'
        assert captured[0]['backup_id'] == str(backup_id)

    _run(_go())


def test_residue_cleanup_dispatcher_rejects_missing_backup_id(tmp_path):
    """CodeX P2: dispatcher refuses if params lacks 'backup_id'. Phase 8
    stale_reaper MUST include the specific BackupRecord row to act on."""
    import background_task_executors as bte

    async def _go():
        Session = await _setup_db()
        user_id = uuid.uuid4()
        with patch.object(bte, 'async_session', Session):
            with pytest.raises(ValueError, match='backup_id'):
                await bte.execute_pan_residue_cleanup_dispatched(
                    task_id=uuid.uuid4(), job_id='job_X',
                    project_dir=tmp_path / 'x',
                    params={'user_id': str(user_id)},  # no backup_id
                )

    _run(_go())
