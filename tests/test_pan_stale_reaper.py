"""Tests for gateway/pan/stale_reaper.py (Phase 8 §T8.3)."""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from tests.pan_fixtures import (  # noqa: F401
    insert_sample_backup_record,
    insert_sample_job,
    insert_sample_pan_credentials,
    run_async,
    setup_pan_token_env,
)


@asynccontextmanager
async def reaper_test_engine():
    """SQLite with Job + BackupRecord + PanCredentials + BackgroundTask
    (reaper enqueues residue_cleanup tasks)."""
    from models import BackupRecord, Job, PanCredentials
    from background_task_models import BackgroundTask

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            for t in (Job, PanCredentials, BackupRecord, BackgroundTask):
                await conn.run_sync(lambda c, _t=t: _t.__table__.create(c))
        yield engine
    finally:
        await engine.dispose()


async def _set_heartbeat(engine, br_id, when):
    from models import BackupRecord
    async with engine.begin() as conn:
        await conn.execute(
            update(BackupRecord)
            .where(BackupRecord.id == br_id)
            .values(heartbeat_at=when)
        )


async def _set_completed_at(engine, br_id, when):
    from models import BackupRecord
    async with engine.begin() as conn:
        await conn.execute(
            update(BackupRecord)
            .where(BackupRecord.id == br_id)
            .values(completed_at=when)
        )


# =========================================================================
# Pass 1: in-flight reap (uploading / restoring with stale heartbeat)
# =========================================================================


def test_reaper_skips_in_flight_with_fresh_heartbeat(monkeypatch):
    """A backup_record with heartbeat < 4h old must NOT be reaped — the
    executor is still alive."""
    from pan.stale_reaper import run_stale_reaper_tick

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()

    async def _go():
        async with reaper_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id='live', status='archiving',
            )
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id='live', status='uploading',
            )
            # Heartbeat 5 minutes ago — fresh.
            await _set_heartbeat(
                engine, br['id'],
                datetime.now(timezone.utc) - timedelta(minutes=5),
            )

            stats = await run_stale_reaper_tick(engine)
            assert stats['in_flight_reaped'] == 0

    run_async(_go())


def test_reaper_reaps_uploading_with_stale_heartbeat(monkeypatch):
    """uploading + heartbeat > 4h → reap: BR.status='failed',
    Job.status='succeeded' (rollback to pre-archive state)."""
    from models import BackupRecord, Job
    from pan.stale_reaper import run_stale_reaper_tick

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()

    async def _go():
        async with reaper_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id='dead', status='archiving',
            )
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id='dead', status='uploading',
            )
            # Heartbeat 5 hours ago — stale.
            await _set_heartbeat(
                engine, br['id'],
                datetime.now(timezone.utc) - timedelta(hours=5),
            )

            stats = await run_stale_reaper_tick(engine)
            assert stats['in_flight_reaped'] == 1

            Session = async_sessionmaker(engine, class_=AsyncSession,
                                         expire_on_commit=False)
            async with Session() as db:
                job_status = (await db.execute(
                    select(Job.status).where(Job.job_id == 'dead')
                )).scalar_one()
                br_after = (await db.execute(
                    select(
                        BackupRecord.status, BackupRecord.error_message,
                        BackupRecord.completed_at,
                    ).where(BackupRecord.id == br['id'])
                )).one()
            assert job_status == 'succeeded'
            assert br_after.status == 'failed'
            assert 'reaped' in (br_after.error_message or '')
            assert br_after.completed_at is not None

    run_async(_go())


def test_reaper_reaps_restoring_with_stale_heartbeat(monkeypatch):
    """restoring + heartbeat > 4h → reap: BR.status='uploaded' (revert
    to recoverable), Job.status='archived' (revert from restoring)."""
    from models import BackupRecord, Job
    from pan.stale_reaper import run_stale_reaper_tick

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()

    async def _go():
        async with reaper_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id='stuck_restore',
                status='restoring',
            )
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id='stuck_restore',
                status='restoring',
            )
            await _set_heartbeat(
                engine, br['id'],
                datetime.now(timezone.utc) - timedelta(hours=5),
            )

            stats = await run_stale_reaper_tick(engine)
            assert stats['in_flight_reaped'] == 1

            Session = async_sessionmaker(engine, class_=AsyncSession,
                                         expire_on_commit=False)
            async with Session() as db:
                job_status = (await db.execute(
                    select(Job.status).where(Job.job_id == 'stuck_restore')
                )).scalar_one()
                br_status = (await db.execute(
                    select(BackupRecord.status)
                    .where(BackupRecord.id == br['id'])
                )).scalar_one()
            assert job_status == 'archived'
            assert br_status == 'uploaded'

    run_async(_go())


def test_reaper_reaps_in_flight_with_null_heartbeat(monkeypatch):
    """heartbeat_at IS NULL → also stale (executor never wrote the
    initial heartbeat or row was created without one)."""
    from pan.stale_reaper import run_stale_reaper_tick

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()

    async def _go():
        async with reaper_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id='nohb', status='archiving',
            )
            # heartbeat_at not set (None by default).
            await insert_sample_backup_record(
                engine, user_id=user_id, job_id='nohb', status='uploading',
            )

            stats = await run_stale_reaper_tick(engine)
            assert stats['in_flight_reaped'] == 1

    run_async(_go())


def test_reaper_dry_run_does_not_modify(monkeypatch):
    from models import BackupRecord, Job
    from pan.stale_reaper import run_stale_reaper_tick

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()

    async def _go():
        async with reaper_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id='x', status='archiving',
            )
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id='x', status='uploading',
            )
            await _set_heartbeat(
                engine, br['id'],
                datetime.now(timezone.utc) - timedelta(hours=5),
            )

            stats = await run_stale_reaper_tick(engine, dry_run=True)
            assert stats['in_flight_reaped'] == 1
            assert stats['dry_run'] is True

            # State unchanged.
            Session = async_sessionmaker(engine, class_=AsyncSession,
                                         expire_on_commit=False)
            async with Session() as db:
                job_status = (await db.execute(
                    select(Job.status).where(Job.job_id == 'x')
                )).scalar_one()
                br_status = (await db.execute(
                    select(BackupRecord.status).where(BackupRecord.id == br['id'])
                )).scalar_one()
            assert job_status == 'archiving'
            assert br_status == 'uploading'

    run_async(_go())


# =========================================================================
# Pass 2: post-commit forward-resolve (jobs.archiving + br.uploaded)
# =========================================================================


def test_reaper_forward_resolves_post_commit_stuck(monkeypatch):
    """Job.status='archiving' + BackupRecord.status='uploaded' +
    completed_at > 4h ago → forward-resolve to Job.status='archived'
    and enqueue pan_residue_cleanup."""
    from background_task_models import BackgroundTask
    from models import Job
    from pan.stale_reaper import run_stale_reaper_tick

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()

    async def _go():
        async with reaper_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id='post_commit_stuck',
                status='archiving', edit_generation=0,
            )
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id='post_commit_stuck',
                status='uploaded', job_edit_generation=0,
            )
            await _set_completed_at(
                engine, br['id'],
                datetime.now(timezone.utc) - timedelta(hours=5),
            )

            stats = await run_stale_reaper_tick(engine)
            assert stats['post_commit_forwarded'] == 1
            assert stats['residue_cleanup_enqueued'] == 1

            Session = async_sessionmaker(engine, class_=AsyncSession,
                                         expire_on_commit=False)
            async with Session() as db:
                job_status = (await db.execute(
                    select(Job.status)
                    .where(Job.job_id == 'post_commit_stuck')
                )).scalar_one()
                # residue_cleanup task enqueued with backup_id in params.
                bt = (await db.execute(
                    select(BackgroundTask.task_type, BackgroundTask.params)
                    .where(BackgroundTask.job_id == 'post_commit_stuck')
                )).one_or_none()
            assert job_status == 'archived'
            assert bt is not None
            assert bt.task_type == 'pan_residue_cleanup'
            params = bt.params
            if isinstance(params, str):
                import json as _json
                params = _json.loads(params)
            assert params['backup_id'] == str(br['id'])
            assert params['user_id'] == str(user_id)

    run_async(_go())


def test_reaper_skips_post_commit_when_completed_at_fresh(monkeypatch):
    """If completed_at is recent (< 4h ago), even though jobs.status is
    'archiving', don't forward yet — give the executor time to finish
    step l."""
    from pan.stale_reaper import run_stale_reaper_tick

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()

    async def _go():
        async with reaper_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id='hot',
                status='archiving', edit_generation=0,
            )
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id='hot',
                status='uploaded', job_edit_generation=0,
            )
            await _set_completed_at(
                engine, br['id'],
                datetime.now(timezone.utc) - timedelta(minutes=30),
            )

            stats = await run_stale_reaper_tick(engine)
            assert stats['post_commit_forwarded'] == 0

    run_async(_go())


def test_reaper_skips_post_commit_generation_mismatch(monkeypatch):
    """A BackupRecord with stale job_edit_generation (Job edited past it)
    is NOT eligible for forward-resolve — it's not the current state."""
    from pan.stale_reaper import run_stale_reaper_tick

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()

    async def _go():
        async with reaper_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id='gen_drift',
                status='archiving', edit_generation=5,
            )
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id='gen_drift',
                status='uploaded', job_edit_generation=3,  # stale gen
            )
            await _set_completed_at(
                engine, br['id'],
                datetime.now(timezone.utc) - timedelta(hours=5),
            )

            stats = await run_stale_reaper_tick(engine)
            assert stats['post_commit_forwarded'] == 0

    run_async(_go())


def test_reaper_handles_mixed_workload(monkeypatch):
    """Tick with multiple rows: one Pass-1 reap + one Pass-2 forward +
    one fresh-heartbeat skip → stats reflect all three branches."""
    from pan.stale_reaper import run_stale_reaper_tick

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()

    async def _go():
        async with reaper_test_engine() as engine:
            # Row 1: uploading, stale → Pass 1 reap
            await insert_sample_job(
                engine, user_id=user_id, job_id='reap_me', status='archiving',
            )
            br1 = await insert_sample_backup_record(
                engine, user_id=user_id, job_id='reap_me', status='uploading',
            )
            await _set_heartbeat(
                engine, br1['id'],
                datetime.now(timezone.utc) - timedelta(hours=5),
            )

            # Row 2: uploaded, stale completed_at + archiving → Pass 2 forward
            await insert_sample_job(
                engine, user_id=user_id, job_id='forward_me',
                status='archiving', edit_generation=0,
            )
            br2 = await insert_sample_backup_record(
                engine, user_id=user_id, job_id='forward_me',
                status='uploaded', job_edit_generation=0,
            )
            await _set_completed_at(
                engine, br2['id'],
                datetime.now(timezone.utc) - timedelta(hours=5),
            )

            # Row 3: uploading with fresh heartbeat → skip
            await insert_sample_job(
                engine, user_id=user_id, job_id='alive', status='archiving',
            )
            br3 = await insert_sample_backup_record(
                engine, user_id=user_id, job_id='alive', status='uploading',
            )
            await _set_heartbeat(
                engine, br3['id'],
                datetime.now(timezone.utc) - timedelta(minutes=2),
            )

            stats = await run_stale_reaper_tick(engine)
            assert stats['in_flight_reaped'] == 1
            assert stats['post_commit_forwarded'] == 1
            assert stats['residue_cleanup_enqueued'] == 1

    run_async(_go())
