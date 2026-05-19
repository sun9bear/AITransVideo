"""Tests for gateway/pan/_enqueue.py (CodeX P0-1).

The shared enqueue+launch helper must:
  1. Create a BackgroundTask row.
  2. Commit.
  3. Actually launch the executor coroutine via asyncio.create_task.

The previous scanner/reaper bug was that step 3 was missing — tasks
accumulated at status='pending' until recover_stale marked them
'failed'. This module's tests pin step 3 explicitly.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from tests.pan_fixtures import (  # noqa: F401
    run_async,
    setup_pan_token_env,
)


@asynccontextmanager
async def enqueue_test_engine():
    """SQLite + BackgroundTask only — _enqueue's session work doesn't
    touch other tables."""
    from background_task_models import BackgroundTask

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: BackgroundTask.__table__.create(c),
            )
        yield engine
    finally:
        await engine.dispose()


async def _session(engine):
    return async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )


def _install_launch_capture(monkeypatch):
    """Replace _launch_coroutine with a no-op that records every call.
    Returns the launched list."""
    from pan import _enqueue as mod

    launched: list[dict] = []

    def fake_launch(coro, name: str):
        launched.append({'name': name, 'coro_qualname': coro.__qualname__})
        coro.close()  # avoid RuntimeWarning: coroutine never awaited
        return None

    monkeypatch.setattr(mod, '_launch_coroutine', fake_launch)
    return launched


# =========================================================================
# Happy path: create row + launch executor
# =========================================================================


def test_enqueue_creates_row_and_launches_executor(monkeypatch):
    """The contract: every enqueue must (1) create a BackgroundTask row
    AND (2) call _launch_coroutine with the executor's coroutine."""
    from background_task_models import BackgroundTask
    from pan._enqueue import enqueue_pan_task

    setup_pan_token_env(monkeypatch)
    launched = _install_launch_capture(monkeypatch)

    user_id = uuid.uuid4()

    async def _go():
        async with enqueue_test_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                task_id = await enqueue_pan_task(
                    db,
                    user_id=user_id,
                    job_id='job_abc',
                    task_type='pan_backup',
                )

            # 1. BackgroundTask row created.
            assert task_id
            async with Session() as db:
                row = (await db.execute(
                    select(
                        BackgroundTask.id, BackgroundTask.task_type,
                        BackgroundTask.user_id, BackgroundTask.status,
                        BackgroundTask.params,
                    ).where(BackgroundTask.id == task_id)
                )).one()
            assert row.task_type == 'pan_backup'
            assert row.user_id == user_id
            assert row.status == 'pending'

            # 2. Executor coroutine was launched.
            assert len(launched) == 1
            assert launched[0]['name'].startswith('bgtask-pan_backup-')
            assert task_id in launched[0]['name']

    run_async(_go())


def test_enqueue_merges_extra_params(monkeypatch):
    """extra_params merges with the base {'user_id': ...} payload.
    Residue cleanup uses this for backup_id."""
    from background_task_models import BackgroundTask
    from pan._enqueue import enqueue_pan_task

    setup_pan_token_env(monkeypatch)
    _install_launch_capture(monkeypatch)

    user_id = uuid.uuid4()
    backup_id = uuid.uuid4()

    async def _go():
        async with enqueue_test_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                task_id = await enqueue_pan_task(
                    db, user_id=user_id, job_id='residue_job',
                    task_type='pan_residue_cleanup',
                    extra_params={'backup_id': str(backup_id)},
                )

            async with Session() as db:
                params = (await db.execute(
                    select(BackgroundTask.params)
                    .where(BackgroundTask.id == task_id)
                )).scalar_one()
            if isinstance(params, str):
                import json as _json
                params = _json.loads(params)
            assert params['user_id'] == str(user_id)
            assert params['backup_id'] == str(backup_id)

    run_async(_go())


def test_enqueue_raises_valueerror_on_unknown_task_type(monkeypatch):
    from pan._enqueue import enqueue_pan_task

    setup_pan_token_env(monkeypatch)
    _install_launch_capture(monkeypatch)

    async def _go():
        async with enqueue_test_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(ValueError, match='unknown pan task_type'):
                    await enqueue_pan_task(
                        db, user_id=uuid.uuid4(), job_id='x',
                        task_type='not_a_real_task',
                    )

    run_async(_go())


# =========================================================================
# Each pan task_type is registered in TASK_EXECUTORS — sanity check that
# the helper accepts all three production task types.
# =========================================================================


@pytest.mark.parametrize('task_type', [
    'pan_backup', 'pan_restore', 'pan_residue_cleanup',
])
def test_enqueue_accepts_all_three_pan_task_types(monkeypatch, task_type):
    from pan._enqueue import enqueue_pan_task

    setup_pan_token_env(monkeypatch)
    launched = _install_launch_capture(monkeypatch)

    user_id = uuid.uuid4()

    async def _go():
        async with enqueue_test_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                # residue_cleanup needs backup_id.
                extra = ({'backup_id': str(uuid.uuid4())}
                         if task_type == 'pan_residue_cleanup' else None)
                task_id = await enqueue_pan_task(
                    db, user_id=user_id, job_id=f'job_{task_type}',
                    task_type=task_type, extra_params=extra,
                )
            assert task_id
            assert len(launched) == 1
            assert task_type in launched[0]['name']

    run_async(_go())
