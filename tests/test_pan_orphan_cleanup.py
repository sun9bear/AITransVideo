"""Tests for gateway/pan/orphan_cleanup.py (Phase 8 §T8.2)."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from tests.pan_fixtures import (  # noqa: F401
    FakeBaiduPanClient,
    insert_sample_backup_record,
    insert_sample_job,
    insert_sample_pan_credentials,
    run_async,
    setup_pan_token_env,
)


@asynccontextmanager
async def cleanup_engine():
    """SQLite + Job + PanCredentials + BackupRecord + PanOauthState."""
    from models import BackupRecord, Job, PanCredentials, PanOauthState

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            for t in (Job, PanCredentials, BackupRecord, PanOauthState):
                await conn.run_sync(lambda c, _t=t: _t.__table__.create(c))
        yield engine
    finally:
        await engine.dispose()


# =========================================================================
# Pass A — pan remote orphans
# =========================================================================


def test_pass_a_finds_and_deletes_orphans(monkeypatch, tmp_path):
    """A pan remote path NOT in PG.backup_records.remote_path → orphan.
    Safety: only paths under /apps/AIVideoTrans/backups/ are deleted."""
    from pan.orphan_cleanup import run_orphan_cleanup_tick

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()

    client = FakeBaiduPanClient()
    # Pre-populate the fake client's "remote storage" with bytes (simulating
    # 3 files on pan). The client's list() returns all uploaded paths.
    src = tmp_path / 'fake.bin'
    src.write_bytes(b'x')
    client.upload(src, '/apps/AIVideoTrans/backups/known.tar.gz', access_token='at')
    client.upload(src, '/apps/AIVideoTrans/backups/orphan1.tar.gz', access_token='at')
    client.upload(src, '/apps/AIVideoTrans/backups/orphan2.tar.gz', access_token='at')

    async def _go():
        async with cleanup_engine() as engine:
            await insert_sample_pan_credentials(engine, user_id=admin_id)
            # Only 'known.tar.gz' has a matching BackupRecord.
            await insert_sample_backup_record(
                engine, user_id=admin_id, job_id='known_job',
                status='uploaded',
                remote_path='/apps/AIVideoTrans/backups/known.tar.gz',
            )

            stats = await run_orphan_cleanup_tick(
                engine, client_factory=lambda: client,
                r2_delete_fn=lambda k: None,
            )

            pass_a = stats['pass_a']
            assert sorted(pass_a['orphans']) == [
                '/apps/AIVideoTrans/backups/orphan1.tar.gz',
                '/apps/AIVideoTrans/backups/orphan2.tar.gz',
            ]
            assert pass_a['deleted'] == 2
            # The fake client's storage should no longer have the orphans.
            assert '/apps/AIVideoTrans/backups/orphan1.tar.gz' not in client._storage
            assert '/apps/AIVideoTrans/backups/orphan2.tar.gz' not in client._storage
            # known.tar.gz preserved.
            assert '/apps/AIVideoTrans/backups/known.tar.gz' in client._storage

    run_async(_go())


def test_pass_a_refuses_unsafe_prefix(monkeypatch, tmp_path):
    """A list response containing a path OUTSIDE the trusted prefix
    must NOT be deleted, even though it's not in PG."""
    from pan.orphan_cleanup import run_orphan_cleanup_tick

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()

    class TrojanClient(FakeBaiduPanClient):
        def list(self, prefix, *, access_token):
            # Pretend pan returns a malicious entry alongside a legit one.
            return [
                {'path': '/apps/AIVideoTrans/backups/legit_orphan.tar.gz',
                 'size': 1, 'fs_id': 1},
                {'path': '/etc/passwd', 'size': 1, 'fs_id': 2},  # ← refuse
            ]

    deleted: list[str] = []

    class TrackDeleteClient(TrojanClient):
        def delete(self, path, *, access_token):
            deleted.append(path)

    client = TrackDeleteClient()

    async def _go():
        async with cleanup_engine() as engine:
            await insert_sample_pan_credentials(engine, user_id=admin_id)
            stats = await run_orphan_cleanup_tick(
                engine, client_factory=lambda: client,
                r2_delete_fn=lambda k: None,
            )

            # /etc/passwd must NOT be in deleted, even though it's not in PG.
            assert '/etc/passwd' not in deleted
            # Legit orphan WAS deleted.
            assert '/apps/AIVideoTrans/backups/legit_orphan.tar.gz' in deleted
            # Error recorded.
            errs = stats['pass_a']['errors']
            assert any('unsafe' in e for e in errs)

    run_async(_go())


def test_pass_a_skips_when_no_active_credentials(monkeypatch, tmp_path):
    """If there are NO PanCredentials with status='active', Pass A is a
    no-op (we'd have nothing to authenticate the list/delete with)."""
    from pan.orphan_cleanup import run_orphan_cleanup_tick

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()

    client = FakeBaiduPanClient()

    async def _go():
        async with cleanup_engine() as engine:
            await insert_sample_pan_credentials(
                engine, user_id=admin_id, status='revoked',
            )
            stats = await run_orphan_cleanup_tick(
                engine, client_factory=lambda: client,
                r2_delete_fn=lambda k: None,
            )
            assert stats['pass_a']['orphans'] == []
            assert stats['pass_a']['deleted'] == 0

    run_async(_go())


def test_pass_a_dry_run_collects_without_delete(monkeypatch, tmp_path):
    from pan.orphan_cleanup import run_orphan_cleanup_tick

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()

    client = FakeBaiduPanClient()
    src = tmp_path / 'x'
    src.write_bytes(b'x')
    client.upload(src, '/apps/AIVideoTrans/backups/lonely.tar.gz', access_token='at')

    async def _go():
        async with cleanup_engine() as engine:
            await insert_sample_pan_credentials(engine, user_id=admin_id)
            stats = await run_orphan_cleanup_tick(
                engine, client_factory=lambda: client,
                r2_delete_fn=lambda k: None,
                dry_run=True,
            )
            assert stats['pass_a']['orphans'] == [
                '/apps/AIVideoTrans/backups/lonely.tar.gz',
            ]
            assert stats['pass_a']['deleted'] == 0
            # Still present in fake storage — no actual delete.
            assert '/apps/AIVideoTrans/backups/lonely.tar.gz' in client._storage

    run_async(_go())


def test_pass_a_per_orphan_delete_failure_continues(monkeypatch, tmp_path):
    """If one delete fails, the others still proceed + errors logged."""
    from pan.orphan_cleanup import run_orphan_cleanup_tick

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()

    src = tmp_path / 'x'
    src.write_bytes(b'x')

    class SelectiveFailClient(FakeBaiduPanClient):
        def delete(self, path, *, access_token):
            if 'fail' in path:
                raise RuntimeError(f'synthetic delete fail for {path}')
            return super().delete(path, access_token=access_token)

    client = SelectiveFailClient()
    client.upload(src, '/apps/AIVideoTrans/backups/ok.tar.gz', access_token='at')
    client.upload(src, '/apps/AIVideoTrans/backups/fail_me.tar.gz', access_token='at')

    async def _go():
        async with cleanup_engine() as engine:
            await insert_sample_pan_credentials(engine, user_id=admin_id)
            stats = await run_orphan_cleanup_tick(
                engine, client_factory=lambda: client,
                r2_delete_fn=lambda k: None,
            )
            assert stats['pass_a']['deleted'] == 1  # ok.tar.gz
            assert len(stats['pass_a']['errors']) == 1
            assert 'fail_me' in stats['pass_a']['errors'][0]

    run_async(_go())


# =========================================================================
# Pass B — R2 residue
# =========================================================================


def test_pass_b_deletes_r2_keys_for_archived_jobs(monkeypatch):
    from models import Job
    from pan.orphan_cleanup import run_orphan_cleanup_tick

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()

    deleted: list[str] = []

    async def _go():
        async with cleanup_engine() as engine:
            await insert_sample_pan_credentials(
                engine, user_id=admin_id, status='revoked',  # Skip Pass A
            )
            await insert_sample_job(
                engine, user_id=admin_id, job_id='archived_residue',
                status='archived',
                r2_artifacts=[
                    {'artifact_key': 'video', 'r2_key': 'jobs/x/v.mp4'},
                    {'artifact_key': 'subs', 'r2_key': 'jobs/x/s.srt'},
                ],
            )

            stats = await run_orphan_cleanup_tick(
                engine, client_factory=lambda: FakeBaiduPanClient(),
                r2_delete_fn=lambda k: deleted.append(k),
            )
            assert sorted(deleted) == ['jobs/x/s.srt', 'jobs/x/v.mp4']
            assert stats['pass_b']['jobs_processed'] == 1
            assert stats['pass_b']['keys_deleted'] == 2

            # r2_artifacts cleared to NULL after both successfully deleted.
            Session = async_sessionmaker(engine, class_=AsyncSession,
                                         expire_on_commit=False)
            async with Session() as db:
                row = (await db.execute(
                    select(Job.r2_artifacts).where(
                        Job.job_id == 'archived_residue',
                    )
                )).one()
            assert row.r2_artifacts is None

    run_async(_go())


def test_pass_b_keeps_failed_keys_in_jsonb(monkeypatch):
    """If r2_delete fails for one key, that key STAYS in r2_artifacts
    for the next pass. Successfully-deleted keys are removed."""
    from models import Job
    from pan.orphan_cleanup import run_orphan_cleanup_tick

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()

    def selective_fail(key: str) -> None:
        if 'fail' in key:
            raise RuntimeError('synthetic r2 fail')

    async def _go():
        async with cleanup_engine() as engine:
            await insert_sample_pan_credentials(
                engine, user_id=admin_id, status='revoked',
            )
            await insert_sample_job(
                engine, user_id=admin_id, job_id='mixed',
                status='archived',
                r2_artifacts=[
                    {'r2_key': 'jobs/m/ok.mp4'},
                    {'r2_key': 'jobs/m/fail.mp4'},
                ],
            )

            stats = await run_orphan_cleanup_tick(
                engine, client_factory=lambda: FakeBaiduPanClient(),
                r2_delete_fn=selective_fail,
            )
            assert stats['pass_b']['keys_deleted'] == 1
            assert len(stats['pass_b']['errors']) == 1
            assert stats['pass_b']['errors'][0]['key'] == 'jobs/m/fail.mp4'

            # Only the failed key remains in r2_artifacts.
            Session = async_sessionmaker(engine, class_=AsyncSession,
                                         expire_on_commit=False)
            async with Session() as db:
                row = (await db.execute(
                    select(Job.r2_artifacts).where(Job.job_id == 'mixed')
                )).one()
            artifacts = row.r2_artifacts
            if isinstance(artifacts, str):
                import json as _json
                artifacts = _json.loads(artifacts)
            assert len(artifacts) == 1
            assert artifacts[0]['r2_key'] == 'jobs/m/fail.mp4'

    run_async(_go())


def test_pass_b_skips_non_archived_jobs(monkeypatch):
    """Jobs not in 'archived' status are NOT touched, even if they have
    r2_artifacts (those are live artifacts being used)."""
    from pan.orphan_cleanup import run_orphan_cleanup_tick

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()

    deleted: list[str] = []

    async def _go():
        async with cleanup_engine() as engine:
            await insert_sample_pan_credentials(
                engine, user_id=admin_id, status='revoked',
            )
            await insert_sample_job(
                engine, user_id=admin_id, job_id='live', status='succeeded',
                r2_artifacts=[{'r2_key': 'live/v.mp4'}],
            )
            stats = await run_orphan_cleanup_tick(
                engine, client_factory=lambda: FakeBaiduPanClient(),
                r2_delete_fn=lambda k: deleted.append(k),
            )
            assert deleted == []
            assert stats['pass_b']['jobs_processed'] == 0

    run_async(_go())


# =========================================================================
# Pass C — pan_oauth_states GC
# =========================================================================


def test_pass_c_deletes_expired_oauth_states(monkeypatch):
    from models import PanOauthState
    from pan.orphan_cleanup import run_orphan_cleanup_tick
    import secrets

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()

    async def _go():
        async with cleanup_engine() as engine:
            await insert_sample_pan_credentials(
                engine, user_id=admin_id, status='revoked',
            )
            # 3 expired + 1 fresh oauth state.
            now = datetime.now(timezone.utc)
            async with engine.begin() as conn:
                for i in range(3):
                    await conn.execute(
                        PanOauthState.__table__.insert().values(
                            token=secrets.token_urlsafe(32),
                            user_id=admin_id,
                            expires_at=now - timedelta(minutes=5),
                        )
                    )
                await conn.execute(
                    PanOauthState.__table__.insert().values(
                        token=secrets.token_urlsafe(32),
                        user_id=admin_id,
                        expires_at=now + timedelta(minutes=5),
                    )
                )

            stats = await run_orphan_cleanup_tick(
                engine, client_factory=lambda: FakeBaiduPanClient(),
                r2_delete_fn=lambda k: None,
            )
            assert stats['pass_c']['states_deleted'] == 3

            # Only the fresh row remains.
            Session = async_sessionmaker(engine, class_=AsyncSession,
                                         expire_on_commit=False)
            async with Session() as db:
                from sqlalchemy import func as _func
                remaining = (await db.execute(
                    select(_func.count()).select_from(PanOauthState)
                )).scalar_one()
            assert remaining == 1

    run_async(_go())


def test_pass_c_dry_run_counts_without_deleting(monkeypatch):
    from models import PanOauthState
    from pan.orphan_cleanup import run_orphan_cleanup_tick
    import secrets

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()

    async def _go():
        async with cleanup_engine() as engine:
            await insert_sample_pan_credentials(
                engine, user_id=admin_id, status='revoked',
            )
            now = datetime.now(timezone.utc)
            async with engine.begin() as conn:
                for _ in range(5):
                    await conn.execute(
                        PanOauthState.__table__.insert().values(
                            token=secrets.token_urlsafe(32),
                            user_id=admin_id,
                            expires_at=now - timedelta(minutes=5),
                        )
                    )

            stats = await run_orphan_cleanup_tick(
                engine, client_factory=lambda: FakeBaiduPanClient(),
                r2_delete_fn=lambda k: None,
                dry_run=True,
            )
            assert stats['pass_c']['states_deleted'] == 5

            # All rows still present.
            Session = async_sessionmaker(engine, class_=AsyncSession,
                                         expire_on_commit=False)
            async with Session() as db:
                from sqlalchemy import func as _func
                count = (await db.execute(
                    select(_func.count()).select_from(PanOauthState)
                )).scalar_one()
            assert count == 5

    run_async(_go())


# =========================================================================
# Integration — 3-pass orchestration
# =========================================================================


def test_all_three_passes_run_in_one_tick(monkeypatch, tmp_path):
    """One tick exercises all 3 passes; stats reflect all 3."""
    from pan.orphan_cleanup import run_orphan_cleanup_tick
    from models import PanOauthState
    import secrets

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()

    client = FakeBaiduPanClient()
    src = tmp_path / 'x'
    src.write_bytes(b'x')
    client.upload(src, '/apps/AIVideoTrans/backups/orphan.tar.gz', access_token='at')

    deleted_r2: list[str] = []

    async def _go():
        async with cleanup_engine() as engine:
            await insert_sample_pan_credentials(engine, user_id=admin_id)
            await insert_sample_job(
                engine, user_id=admin_id, job_id='arch_res', status='archived',
                r2_artifacts=[{'r2_key': 'foo/bar.mp4'}],
            )
            async with engine.begin() as conn:
                await conn.execute(
                    PanOauthState.__table__.insert().values(
                        token=secrets.token_urlsafe(32),
                        user_id=admin_id,
                        expires_at=datetime.now(timezone.utc)
                        - timedelta(minutes=5),
                    )
                )

            stats = await run_orphan_cleanup_tick(
                engine, client_factory=lambda: client,
                r2_delete_fn=lambda k: deleted_r2.append(k),
            )

            assert stats['pass_a']['deleted'] == 1
            assert stats['pass_b']['keys_deleted'] == 1
            assert stats['pass_c']['states_deleted'] == 1
            assert deleted_r2 == ['foo/bar.mp4']

    run_async(_go())


def test_pass_failure_does_not_block_other_passes(monkeypatch, tmp_path):
    """If Pass A's list call raises, Pass B and C still run."""
    from pan.orphan_cleanup import run_orphan_cleanup_tick
    from models import PanOauthState
    import secrets

    setup_pan_token_env(monkeypatch)
    admin_id = uuid.uuid4()

    class BoomListClient(FakeBaiduPanClient):
        def list(self, prefix, *, access_token):
            raise RuntimeError('synthetic Pass A failure')

    deleted_r2: list[str] = []

    async def _go():
        async with cleanup_engine() as engine:
            await insert_sample_pan_credentials(engine, user_id=admin_id)
            await insert_sample_job(
                engine, user_id=admin_id, job_id='still_b', status='archived',
                r2_artifacts=[{'r2_key': 'k1'}],
            )
            async with engine.begin() as conn:
                await conn.execute(
                    PanOauthState.__table__.insert().values(
                        token=secrets.token_urlsafe(32),
                        user_id=admin_id,
                        expires_at=datetime.now(timezone.utc)
                        - timedelta(minutes=5),
                    )
                )

            stats = await run_orphan_cleanup_tick(
                engine, client_factory=lambda: BoomListClient(),
                r2_delete_fn=lambda k: deleted_r2.append(k),
            )

            # Pass A recorded the error.
            assert any('synthetic' in e for e in stats['pass_a']['errors'])
            # Pass B still ran.
            assert stats['pass_b']['keys_deleted'] == 1
            assert deleted_r2 == ['k1']
            # Pass C still ran.
            assert stats['pass_c']['states_deleted'] == 1

    run_async(_go())
