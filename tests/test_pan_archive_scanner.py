"""Tests for gateway/pan/archive_scanner.py (Phase 8 §T8.1)."""
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
    install_no_launch,
    run_async,
    setup_pan_token_env,
)


@asynccontextmanager
async def scanner_test_engine():
    """SQLite + Job + PanCredentials + BackupRecord + BackgroundTask
    + Users (the scanner JOINs users for the role filter)."""
    from models import BackupRecord, Job, PanCredentials, User
    from background_task_models import BackgroundTask

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            for t in (User, Job, PanCredentials, BackupRecord, BackgroundTask):
                await conn.run_sync(lambda c, _t=t: _t.__table__.create(c))
        yield engine
    finally:
        await engine.dispose()


async def _session(engine):
    return async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )


async def _insert_admin_user(engine, user_id: uuid.UUID, role: str = 'admin'):
    from models import User
    async with engine.begin() as conn:
        await conn.execute(User.__table__.insert().values(
            id=user_id, email=f'admin_{user_id.hex[:8]}@test.com',
            display_name='admin', is_active=True, role=role,
        ))


async def _set_job_updated_at(engine, job_id: str, when: datetime):
    """Force Job.updated_at to a specific timestamp (bypasses onupdate)."""
    from models import Job
    async with engine.begin() as conn:
        await conn.execute(
            update(Job).where(Job.job_id == job_id).values(updated_at=when)
        )


# =========================================================================
# T8.1 — archive_scanner
# =========================================================================


def test_scanner_dry_run_returns_candidates_without_enqueue(monkeypatch):
    """dry_run=True surfaces candidates but does NOT create BackgroundTask
    rows."""
    from background_task_models import BackgroundTask
    from pan.archive_scanner import run_archive_scanner_tick
    from sqlalchemy import func

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()
    old = datetime.now(timezone.utc) - timedelta(days=40)

    async def _go():
        async with scanner_test_engine() as engine:
            await _insert_admin_user(engine, admin_id)
            await insert_sample_job(
                engine, user_id=admin_id, job_id='old_job', status='succeeded',
            )
            await _set_job_updated_at(engine, 'old_job', old)
            await insert_sample_pan_credentials(engine, user_id=admin_id)

            Session = await _session(engine)
            async with Session() as db:
                result = await run_archive_scanner_tick(db, dry_run=True)

            assert result['dry_run'] is True
            assert len(result['candidates']) == 1
            assert result['candidates'][0]['job_id'] == 'old_job'
            assert result['candidates'][0]['user_id'] == str(admin_id)
            assert result['enqueued'] == 0
            assert result['enqueued_task_ids'] == []

            # No BackgroundTask rows created in dry-run.
            async with Session() as db:
                total = (await db.execute(
                    select(func.count()).select_from(BackgroundTask)
                )).scalar_one()
            assert total == 0

    run_async(_go())


def test_scanner_enqueues_pan_backup_tasks(monkeypatch):
    """Non-dry-run: each candidate becomes a BackgroundTask row of
    type='pan_backup'."""
    from background_task_models import BackgroundTask
    from pan.archive_scanner import run_archive_scanner_tick

    setup_pan_token_env(monkeypatch)
    # CodeX P2: block real backup_executor from being scheduled —
    # the enqueue path goes through pan._enqueue and we don't have
    # a real event loop / executor wiring here.
    launched = install_no_launch(monkeypatch)
    admin_id = uuid.uuid4()
    old = datetime.now(timezone.utc) - timedelta(days=40)

    async def _go():
        async with scanner_test_engine() as engine:
            await _insert_admin_user(engine, admin_id)
            for jid in ('a', 'b', 'c'):
                await insert_sample_job(
                    engine, user_id=admin_id, job_id=jid, status='succeeded',
                )
                await _set_job_updated_at(engine, jid, old)
            await insert_sample_pan_credentials(engine, user_id=admin_id)

            Session = await _session(engine)
            async with Session() as db:
                result = await run_archive_scanner_tick(db)

            assert result['enqueued'] == 3
            assert len(result['enqueued_task_ids']) == 3

            async with Session() as db:
                tasks = (await db.execute(
                    select(BackgroundTask.task_type, BackgroundTask.job_id)
                )).all()
            assert len(tasks) == 3
            assert all(t.task_type == 'pan_backup' for t in tasks)
            assert sorted(t.job_id for t in tasks) == ['a', 'b', 'c']

            # Bonus: each row got a launch attempt (one per candidate).
            assert len(launched) == 3
            assert all('pan_backup' in entry['name'] for entry in launched)

    run_async(_go())


def test_scanner_excludes_jobs_with_in_flight_backup(monkeypatch):
    """Jobs with status in (uploading/uploaded/restoring) at the current
    generation are NOT re-enqueued."""
    from pan.archive_scanner import run_archive_scanner_tick

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()
    old = datetime.now(timezone.utc) - timedelta(days=40)

    async def _go():
        async with scanner_test_engine() as engine:
            await _insert_admin_user(engine, admin_id)

            # 3 succeeded jobs — but with different backup states.
            await insert_sample_job(
                engine, user_id=admin_id, job_id='no_backup', status='succeeded',
            )
            await _set_job_updated_at(engine, 'no_backup', old)

            await insert_sample_job(
                engine, user_id=admin_id, job_id='with_uploaded',
                status='succeeded', edit_generation=0,
            )
            await _set_job_updated_at(engine, 'with_uploaded', old)
            await insert_sample_backup_record(
                engine, user_id=admin_id, job_id='with_uploaded',
                job_edit_generation=0, status='uploaded',
            )

            await insert_sample_job(
                engine, user_id=admin_id, job_id='with_uploading',
                status='succeeded', edit_generation=0,
            )
            await _set_job_updated_at(engine, 'with_uploading', old)
            await insert_sample_backup_record(
                engine, user_id=admin_id, job_id='with_uploading',
                job_edit_generation=0, status='uploading',
            )

            await insert_sample_pan_credentials(engine, user_id=admin_id)

            Session = await _session(engine)
            async with Session() as db:
                result = await run_archive_scanner_tick(db, dry_run=True)

            job_ids = [c['job_id'] for c in result['candidates']]
            assert job_ids == ['no_backup']

    run_async(_go())


def test_scanner_includes_jobs_with_failed_backup(monkeypatch):
    """A 'failed' or 'deleted' backup_records row at the current generation
    does NOT exclude the job — those aren't in-flight states. The job
    should be re-scanned."""
    from pan.archive_scanner import run_archive_scanner_tick

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()
    old = datetime.now(timezone.utc) - timedelta(days=40)

    async def _go():
        async with scanner_test_engine() as engine:
            await _insert_admin_user(engine, admin_id)
            await insert_sample_job(
                engine, user_id=admin_id, job_id='retry_me',
                status='succeeded', edit_generation=2,
            )
            await _set_job_updated_at(engine, 'retry_me', old)
            await insert_sample_backup_record(
                engine, user_id=admin_id, job_id='retry_me',
                job_edit_generation=2, status='failed',
            )
            await insert_sample_pan_credentials(engine, user_id=admin_id)

            Session = await _session(engine)
            async with Session() as db:
                result = await run_archive_scanner_tick(db, dry_run=True)

            assert [c['job_id'] for c in result['candidates']] == ['retry_me']

    run_async(_go())


def test_scanner_excludes_jobs_younger_than_threshold(monkeypatch):
    """Jobs with updated_at within the last 30d are NOT eligible."""
    from pan.archive_scanner import run_archive_scanner_tick

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()
    recent = datetime.now(timezone.utc) - timedelta(days=5)
    aged = datetime.now(timezone.utc) - timedelta(days=40)

    async def _go():
        async with scanner_test_engine() as engine:
            await _insert_admin_user(engine, admin_id)
            await insert_sample_job(
                engine, user_id=admin_id, job_id='fresh', status='succeeded',
            )
            await _set_job_updated_at(engine, 'fresh', recent)
            await insert_sample_job(
                engine, user_id=admin_id, job_id='stale', status='succeeded',
            )
            await _set_job_updated_at(engine, 'stale', aged)
            await insert_sample_pan_credentials(engine, user_id=admin_id)

            Session = await _session(engine)
            async with Session() as db:
                result = await run_archive_scanner_tick(db, dry_run=True)

            assert [c['job_id'] for c in result['candidates']] == ['stale']

    run_async(_go())


def test_scanner_excludes_non_admin_users(monkeypatch):
    """Non-admin users' jobs are skipped (the design is admin-only)."""
    from pan.archive_scanner import run_archive_scanner_tick

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    old = datetime.now(timezone.utc) - timedelta(days=40)

    async def _go():
        async with scanner_test_engine() as engine:
            await _insert_admin_user(engine, user_id, role='user')  # NOT admin
            await insert_sample_job(
                engine, user_id=user_id, job_id='user_job', status='succeeded',
            )
            await _set_job_updated_at(engine, 'user_job', old)
            await insert_sample_pan_credentials(engine, user_id=user_id)

            Session = await _session(engine)
            async with Session() as db:
                result = await run_archive_scanner_tick(db, dry_run=True)

            assert result['candidates'] == []

    run_async(_go())


def test_scanner_excludes_users_without_active_credentials(monkeypatch):
    """Admin with no PanCredentials, or only revoked ones, → no candidates."""
    from pan.archive_scanner import run_archive_scanner_tick

    setup_pan_token_env(monkeypatch)
    no_creds = uuid.uuid4()
    revoked_creds = uuid.uuid4()
    old = datetime.now(timezone.utc) - timedelta(days=40)

    async def _go():
        async with scanner_test_engine() as engine:
            await _insert_admin_user(engine, no_creds)
            await _insert_admin_user(engine, revoked_creds)
            await insert_sample_job(
                engine, user_id=no_creds, job_id='no_creds_job',
                status='succeeded',
            )
            await _set_job_updated_at(engine, 'no_creds_job', old)
            await insert_sample_job(
                engine, user_id=revoked_creds, job_id='revoked_job',
                status='succeeded',
            )
            await _set_job_updated_at(engine, 'revoked_job', old)
            await insert_sample_pan_credentials(
                engine, user_id=revoked_creds, status='revoked',
            )

            Session = await _session(engine)
            async with Session() as db:
                result = await run_archive_scanner_tick(db, dry_run=True)

            assert result['candidates'] == []

    run_async(_go())


def test_scanner_respects_max_per_run(monkeypatch):
    """If more than max_per_run candidates exist, only the OLDEST N are
    enqueued (ORDER BY updated_at ASC)."""
    from pan.archive_scanner import run_archive_scanner_tick

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()

    async def _go():
        async with scanner_test_engine() as engine:
            await _insert_admin_user(engine, admin_id)
            # 7 candidates with staggered updated_at (oldest first).
            for i in range(7):
                jid = f'job_{i}'
                await insert_sample_job(
                    engine, user_id=admin_id, job_id=jid, status='succeeded',
                )
                # job_0 is oldest, job_6 is most recent
                await _set_job_updated_at(
                    engine, jid,
                    datetime.now(timezone.utc) - timedelta(days=40 + (6 - i)),
                )
            await insert_sample_pan_credentials(engine, user_id=admin_id)

            Session = await _session(engine)
            async with Session() as db:
                result = await run_archive_scanner_tick(
                    db, max_per_run=3, dry_run=True,
                )

            ids = [c['job_id'] for c in result['candidates']]
            assert len(ids) == 3
            # Oldest 3 picked.
            assert ids == ['job_0', 'job_1', 'job_2']

    run_async(_go())


def test_scanner_per_candidate_enqueue_failure_continues_batch(monkeypatch):
    """If queue.create_task raises for one row, other rows still enqueue +
    failed_enqueue captures the error."""
    from pan.archive_scanner import run_archive_scanner_tick
    from pan import archive_scanner as scanner_mod
    import background_task_queue as queue

    setup_pan_token_env(monkeypatch)
    # CodeX P2: block real executor launches.
    install_no_launch(monkeypatch)
    admin_id = uuid.uuid4()
    old = datetime.now(timezone.utc) - timedelta(days=40)

    # Patch queue.create_task to fail for job 'b' only.
    real_create = queue.create_task

    async def selective_create(db, *, job_id, user_id, task_type, params):
        if job_id == 'b':
            raise RuntimeError('synthetic enqueue failure for b')
        return await real_create(
            db, job_id=job_id, user_id=user_id,
            task_type=task_type, params=params,
        )

    monkeypatch.setattr(queue, 'create_task', selective_create)

    async def _go():
        async with scanner_test_engine() as engine:
            await _insert_admin_user(engine, admin_id)
            for jid in ('a', 'b', 'c'):
                await insert_sample_job(
                    engine, user_id=admin_id, job_id=jid, status='succeeded',
                )
                await _set_job_updated_at(engine, jid, old)
            await insert_sample_pan_credentials(engine, user_id=admin_id)

            Session = await _session(engine)
            async with Session() as db:
                result = await run_archive_scanner_tick(db)

            assert result['enqueued'] == 2  # a + c
            assert len(result['failed_enqueue']) == 1
            assert result['failed_enqueue'][0]['job_id'] == 'b'
            assert 'synthetic enqueue failure' in result['failed_enqueue'][0]['error']

    run_async(_go())
