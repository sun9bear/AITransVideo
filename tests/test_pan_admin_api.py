"""Tests for gateway/pan/admin_api.py.

Plan 2026-05-14 Phase 7a §T7.1-T7.6. Exercises all 8 admin endpoints:
  - GET    /status
  - GET    /backups (list with filters)
  - GET    /backups/{id}/manifest
  - POST   /backups (single)
  - POST   /backups/batch
  - POST   /restores
  - DELETE /credentials
  - DELETE /backups/{id} (with §6 412 guard)
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

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
async def admin_api_engine():
    """In-memory SQLite with Job + PanCredentials + BackupRecord +
    BackgroundTask tables for admin_pan_api tests."""
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


async def _session(engine):
    return async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )


def _admin_user(user_id: uuid.UUID | None = None):
    """A SimpleNamespace User stand-in good enough for _require_admin +
    handler-level access to .id / .role. Tests bypass FastAPI Depends
    by calling the handler functions directly."""
    return SimpleNamespace(
        id=user_id or uuid.uuid4(),
        role='admin',
    )


def _non_admin_user():
    return SimpleNamespace(id=uuid.uuid4(), role='user')


# =========================================================================
# Admin gate
# =========================================================================


def test_endpoints_reject_non_admin():
    """Every endpoint that depends on _require_admin must reject role!=admin."""
    from fastapi import HTTPException
    from pan.admin_api import (
        get_pan_status, list_backups, get_backup_manifest,
        create_backup, create_backup_batch, create_restore,
        disconnect_credentials, delete_backup,
        BackupCreateRequest, BatchBackupRequest, RestoreRequest,
    )

    async def _go():
        async with admin_api_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                non_admin = _non_admin_user()
                # All handlers raise 403 when user.role != admin.
                with pytest.raises(HTTPException) as exc:
                    await get_pan_status(user=non_admin, db=db)
                assert exc.value.status_code == 403

                with pytest.raises(HTTPException) as exc:
                    await list_backups(user=non_admin, db=db)
                assert exc.value.status_code == 403

                with pytest.raises(HTTPException) as exc:
                    await get_backup_manifest(
                        backup_id=str(uuid.uuid4()), user=non_admin, db=db,
                    )
                assert exc.value.status_code == 403

                with pytest.raises(HTTPException) as exc:
                    await create_backup(
                        body=BackupCreateRequest(job_id='x'),
                        user=non_admin, db=db,
                    )
                assert exc.value.status_code == 403

                with pytest.raises(HTTPException) as exc:
                    await create_backup_batch(
                        body=BatchBackupRequest(job_ids=['x']),
                        user=non_admin, db=db,
                    )
                assert exc.value.status_code == 403

                with pytest.raises(HTTPException) as exc:
                    await create_restore(
                        body=RestoreRequest(job_id='x'),
                        user=non_admin, db=db,
                    )
                assert exc.value.status_code == 403

                with pytest.raises(HTTPException) as exc:
                    await disconnect_credentials(user=non_admin, db=db)
                assert exc.value.status_code == 403

                with pytest.raises(HTTPException) as exc:
                    await delete_backup(
                        backup_id=str(uuid.uuid4()), user=non_admin, db=db,
                    )
                assert exc.value.status_code == 403

    run_async(_go())


def test_endpoints_reject_unauthenticated():
    """user=None → 401."""
    from fastapi import HTTPException
    from pan.admin_api import get_pan_status

    async def _go():
        async with admin_api_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await get_pan_status(user=None, db=db)
                assert exc.value.status_code == 401

    run_async(_go())


# =========================================================================
# T7.1 — GET /status
# =========================================================================


def test_status_disconnected_when_no_credentials(monkeypatch):
    from pan.admin_api import get_pan_status

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                resp = await get_pan_status(user=admin, db=db)
            assert resp == {
                "connected": False, "status": "disconnected",
                "quota": None, "scope": None,
                "last_refreshed_at": None, "connected_at": None,
            }

    run_async(_go())


def test_status_revoked_returns_revoked_no_quota_call(monkeypatch):
    """If credentials are revoked, return status='revoked' WITHOUT
    calling get_quota (which would try to use a dead token)."""
    from pan.admin_api import get_pan_status
    from pan import admin_api as api_mod

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    # If the code accidentally tries to fetch quota, this client raises.
    class BoomClient(FakeBaiduPanClient):
        def get_quota(self, *, access_token):
            raise AssertionError("get_quota must NOT be called on revoked creds")

    monkeypatch.setattr(api_mod, '_client_factory', lambda: BoomClient())

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_pan_credentials(
                engine, user_id=admin.id, status='revoked',
            )
            Session = await _session(engine)
            async with Session() as db:
                resp = await get_pan_status(user=admin, db=db)
            assert resp["connected"] is True
            assert resp["status"] == 'revoked'
            assert resp["quota"] is None

    run_async(_go())


def test_status_active_fetches_quota(monkeypatch):
    from pan.admin_api import get_pan_status
    from pan import admin_api as api_mod

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    captured_token: list[str] = []

    class StubClient(FakeBaiduPanClient):
        def get_quota(self, *, access_token):
            captured_token.append(access_token)
            return {"total": 2 * 10**12, "used": 100, "free": 2 * 10**12 - 100}

    monkeypatch.setattr(api_mod, '_client_factory', lambda: StubClient())

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_pan_credentials(
                engine, user_id=admin.id, status='active',
                access_token='at_xyz', refresh_token='rt_xyz',
            )
            Session = await _session(engine)
            async with Session() as db:
                resp = await get_pan_status(user=admin, db=db)
            assert resp["status"] == 'active'
            assert resp["quota"] == {
                "total": 2 * 10**12, "used": 100, "free": 2 * 10**12 - 100,
            }
            assert captured_token == ['at_xyz']

    run_async(_go())


def test_status_active_quota_error_returns_active_without_quota(monkeypatch):
    """get_quota failure (network, Baidu down) → still report active=true
    with quota=null + quota_error message. Status endpoint must not 500."""
    from pan.admin_api import get_pan_status
    from pan import admin_api as api_mod

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    class FailQuotaClient(FakeBaiduPanClient):
        def get_quota(self, *, access_token):
            raise RuntimeError('synthetic baidu outage')

    monkeypatch.setattr(api_mod, '_client_factory', lambda: FailQuotaClient())

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_pan_credentials(
                engine, user_id=admin.id, status='active',
            )
            Session = await _session(engine)
            async with Session() as db:
                resp = await get_pan_status(user=admin, db=db)
            assert resp["status"] == 'active'
            assert resp["quota"] is None
            assert 'synthetic baidu outage' in resp.get("quota_error", "")

    run_async(_go())


# =========================================================================
# T7.4 — GET /backups (list)
# =========================================================================


def test_list_backups_filters_by_status(monkeypatch):
    from pan.admin_api import list_backups

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_backup_record(
                engine, user_id=admin.id, job_id='job_a', status='uploaded',
            )
            await insert_sample_backup_record(
                engine, user_id=admin.id, job_id='job_b', status='failed',
            )
            await insert_sample_backup_record(
                engine, user_id=admin.id, job_id='job_c', status='deleted',
            )

            Session = await _session(engine)
            async with Session() as db:
                # No filter: all 3 rows.
                resp = await list_backups(user=admin, db=db)
                assert resp["total"] == 3

                # Filter status=uploaded.
                resp = await list_backups(
                    user=admin, db=db, status=['uploaded'],
                )
                assert resp["total"] == 1
                assert resp["items"][0]["job_id"] == 'job_a'

                # Multi-status filter.
                resp = await list_backups(
                    user=admin, db=db, status=['uploaded', 'deleted'],
                )
                assert resp["total"] == 2
                jobs = sorted(i["job_id"] for i in resp["items"])
                assert jobs == ['job_a', 'job_c']

    run_async(_go())


def test_list_backups_filter_by_user_id_and_pagination():
    from pan.admin_api import list_backups

    admin = _admin_user()
    other_user = uuid.uuid4()

    async def _go():
        async with admin_api_engine() as engine:
            for i in range(5):
                await insert_sample_backup_record(
                    engine, user_id=admin.id, job_id=f'job_{i}', status='uploaded',
                )
            await insert_sample_backup_record(
                engine, user_id=other_user, job_id='job_other', status='uploaded',
            )

            Session = await _session(engine)
            async with Session() as db:
                # Filter by other user.
                resp = await list_backups(
                    user=admin, db=db, user_id=str(other_user),
                )
                assert resp["total"] == 1
                assert resp["items"][0]["user_id"] == str(other_user)

                # Pagination on admin's rows.
                resp = await list_backups(
                    user=admin, db=db, user_id=str(admin.id),
                    limit=2, offset=0,
                )
                assert resp["total"] == 5
                assert len(resp["items"]) == 2
                resp2 = await list_backups(
                    user=admin, db=db, user_id=str(admin.id),
                    limit=2, offset=2,
                )
                assert len(resp2["items"]) == 2
                # Different page → different items.
                assert {r["id"] for r in resp["items"]} != {
                    r["id"] for r in resp2["items"]
                }

    run_async(_go())


# =========================================================================
# Manifest endpoint
# =========================================================================


def test_get_backup_manifest_returns_persisted_dict():
    from pan.admin_api import get_backup_manifest

    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            br = await insert_sample_backup_record(
                engine, user_id=admin.id, job_id='job_m', status='uploaded',
                manifest_json={'backup_format_version': 1,
                               'job_record': {'job_id': 'job_m'}},
            )
            Session = await _session(engine)
            async with Session() as db:
                resp = await get_backup_manifest(
                    backup_id=str(br['id']), user=admin, db=db,
                )
            assert resp["backup_id"] == str(br['id'])
            assert resp["status"] == 'uploaded'
            assert resp["manifest"]["backup_format_version"] == 1
            assert resp["manifest"]["job_record"]["job_id"] == 'job_m'

    run_async(_go())


def test_get_backup_manifest_404_on_unknown():
    from fastapi import HTTPException
    from pan.admin_api import get_backup_manifest

    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await get_backup_manifest(
                        backup_id=str(uuid.uuid4()), user=admin, db=db,
                    )
            assert exc.value.status_code == 404

    run_async(_go())


def test_get_backup_manifest_400_on_invalid_uuid():
    from fastapi import HTTPException
    from pan.admin_api import get_backup_manifest

    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await get_backup_manifest(
                        backup_id='not-a-uuid', user=admin, db=db,
                    )
            assert exc.value.status_code == 400

    run_async(_go())


# =========================================================================
# T7.2 — POST /backups (single)
# =========================================================================


def test_create_backup_404_when_job_missing(monkeypatch):
    from fastapi import HTTPException
    from pan.admin_api import create_backup, BackupCreateRequest

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await create_backup(
                        body=BackupCreateRequest(job_id='missing'),
                        user=admin, db=db,
                    )
            assert exc.value.status_code == 404

    run_async(_go())


def test_create_backup_412_when_job_not_succeeded(monkeypatch):
    from fastapi import HTTPException
    from pan.admin_api import create_backup, BackupCreateRequest

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_job(
                engine, user_id=admin.id, job_id='running_job', status='running',
            )
            await insert_sample_pan_credentials(engine, user_id=admin.id)
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await create_backup(
                        body=BackupCreateRequest(job_id='running_job'),
                        user=admin, db=db,
                    )
            assert exc.value.status_code == 412
            assert "succeeded" in str(exc.value.detail)

    run_async(_go())


def test_create_backup_412_when_credentials_missing(monkeypatch):
    from fastapi import HTTPException
    from pan.admin_api import create_backup, BackupCreateRequest

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_job(
                engine, user_id=admin.id, job_id='good_job', status='succeeded',
            )
            # No PanCredentials.
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await create_backup(
                        body=BackupCreateRequest(job_id='good_job'),
                        user=admin, db=db,
                    )
            assert exc.value.status_code == 412
            assert "连接网盘" in str(exc.value.detail)

    run_async(_go())


def test_create_backup_412_when_credentials_revoked(monkeypatch):
    from fastapi import HTTPException
    from pan.admin_api import create_backup, BackupCreateRequest

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_job(
                engine, user_id=admin.id, job_id='good_job', status='succeeded',
            )
            await insert_sample_pan_credentials(
                engine, user_id=admin.id, status='revoked',
            )
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await create_backup(
                        body=BackupCreateRequest(job_id='good_job'),
                        user=admin, db=db,
                    )
            assert exc.value.status_code == 412

    run_async(_go())


def test_create_backup_happy_path_enqueues_task(monkeypatch):
    from pan.admin_api import create_backup, BackupCreateRequest
    from pan import admin_api as api_mod
    from background_task_models import BackgroundTask

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    # Replace _enqueue_pan_task's dispatch with a no-op so we don't try
    # to run the real executor in tests.
    dispatched: list[dict] = []

    real_enqueue = api_mod._enqueue_pan_task

    async def fake_enqueue(db, *, user_id, job_id, task_type):
        # Still create the BackgroundTask row so we can assert it.
        import background_task_queue as queue
        task_id, _ = await queue.create_task(
            db, job_id=job_id, user_id=user_id,
            task_type=task_type, params={'user_id': str(user_id)},
        )
        await db.commit()
        dispatched.append({
            'task_id': task_id, 'task_type': task_type, 'job_id': job_id,
        })
        return task_id

    monkeypatch.setattr(api_mod, '_enqueue_pan_task', fake_enqueue)

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_job(
                engine, user_id=admin.id, job_id='good_job', status='succeeded',
            )
            await insert_sample_pan_credentials(engine, user_id=admin.id)

            Session = await _session(engine)
            async with Session() as db:
                resp = await create_backup(
                    body=BackupCreateRequest(job_id='good_job'),
                    user=admin, db=db,
                )

            assert resp["status"] == 'pending'
            assert resp["job_id"] == 'good_job'
            assert resp["task_id"]

            # BackgroundTask row created with correct params.
            async with Session() as db:
                row = (await db.execute(
                    select(
                        BackgroundTask.task_type,
                        BackgroundTask.job_id,
                        BackgroundTask.user_id,
                        BackgroundTask.status,
                        BackgroundTask.params,
                    ).where(BackgroundTask.job_id == 'good_job')
                )).one()
            assert row.task_type == 'pan_backup'
            assert row.user_id == admin.id
            assert row.status == 'pending'
            params = row.params
            if isinstance(params, str):
                params = json.loads(params)
            assert params['user_id'] == str(admin.id)

            assert dispatched[0]['task_type'] == 'pan_backup'

    run_async(_go())


# =========================================================================
# T7.3 — POST /backups/batch
# =========================================================================


def test_create_backup_batch_partial_success(monkeypatch):
    """3 jobs: succeeded ✓ / running ✗ / nonexistent ✗ → per-job results."""
    from pan.admin_api import create_backup_batch, BatchBackupRequest
    from pan import admin_api as api_mod

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def fake_enqueue(db, *, user_id, job_id, task_type):
        import background_task_queue as queue
        task_id, _ = await queue.create_task(
            db, job_id=job_id, user_id=user_id,
            task_type=task_type, params={'user_id': str(user_id)},
        )
        await db.commit()
        return task_id

    monkeypatch.setattr(api_mod, '_enqueue_pan_task', fake_enqueue)

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_job(
                engine, user_id=admin.id, job_id='ok_job', status='succeeded',
            )
            await insert_sample_job(
                engine, user_id=admin.id, job_id='busy_job', status='running',
            )
            await insert_sample_pan_credentials(engine, user_id=admin.id)

            Session = await _session(engine)
            async with Session() as db:
                resp = await create_backup_batch(
                    body=BatchBackupRequest(
                        job_ids=['ok_job', 'busy_job', 'missing_job'],
                    ),
                    user=admin, db=db,
                )

            assert len(resp["succeeded"]) == 1
            assert resp["succeeded"][0]["job_id"] == 'ok_job'

            assert len(resp["failed"]) == 2
            failures = {f["job_id"]: f["reason"] for f in resp["failed"]}
            assert "succeeded" in failures['busy_job']
            assert "不存在" in failures['missing_job']

    run_async(_go())


def test_create_backup_batch_412_when_credentials_revoked(monkeypatch):
    from fastapi import HTTPException
    from pan.admin_api import create_backup_batch, BatchBackupRequest

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_job(
                engine, user_id=admin.id, job_id='x', status='succeeded',
            )
            await insert_sample_pan_credentials(
                engine, user_id=admin.id, status='revoked',
            )
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await create_backup_batch(
                        body=BatchBackupRequest(job_ids=['x']),
                        user=admin, db=db,
                    )
            assert exc.value.status_code == 412

    run_async(_go())


# =========================================================================
# T7.5 — POST /restores
# =========================================================================


def test_create_restore_404_when_job_missing(monkeypatch):
    from fastapi import HTTPException
    from pan.admin_api import create_restore, RestoreRequest

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await create_restore(
                        body=RestoreRequest(job_id='gone'),
                        user=admin, db=db,
                    )
            assert exc.value.status_code == 404

    run_async(_go())


def test_create_restore_412_when_job_not_archived(monkeypatch):
    from fastapi import HTTPException
    from pan.admin_api import create_restore, RestoreRequest

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_job(
                engine, user_id=admin.id, job_id='succ',
                status='succeeded',  # not 'archived'
            )
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await create_restore(
                        body=RestoreRequest(job_id='succ'),
                        user=admin, db=db,
                    )
            assert exc.value.status_code == 412

    run_async(_go())


def test_create_restore_412_when_no_uploaded_at_current_generation(monkeypatch):
    """Job at gen=5, only 'uploaded' BackupRecord is at gen=3 → 412."""
    from fastapi import HTTPException
    from pan.admin_api import create_restore, RestoreRequest

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_job(
                engine, user_id=admin.id, job_id='j', status='archived',
                edit_generation=5,
            )
            await insert_sample_backup_record(
                engine, user_id=admin.id, job_id='j', status='uploaded',
                job_edit_generation=3,
            )
            await insert_sample_pan_credentials(engine, user_id=admin.id)
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await create_restore(
                        body=RestoreRequest(job_id='j'),
                        user=admin, db=db,
                    )
            assert exc.value.status_code == 412
            assert "generation=5" in str(exc.value.detail)

    run_async(_go())


def test_create_restore_happy_path(monkeypatch):
    from pan.admin_api import create_restore, RestoreRequest
    from pan import admin_api as api_mod

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    enqueued: list[str] = []

    async def fake_enqueue(db, *, user_id, job_id, task_type):
        import background_task_queue as queue
        task_id, _ = await queue.create_task(
            db, job_id=job_id, user_id=user_id,
            task_type=task_type, params={'user_id': str(user_id)},
        )
        await db.commit()
        enqueued.append(task_type)
        return task_id

    monkeypatch.setattr(api_mod, '_enqueue_pan_task', fake_enqueue)

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_job(
                engine, user_id=admin.id, job_id='arc', status='archived',
                edit_generation=0,
            )
            await insert_sample_backup_record(
                engine, user_id=admin.id, job_id='arc', status='uploaded',
                job_edit_generation=0,
            )
            await insert_sample_pan_credentials(engine, user_id=admin.id)
            Session = await _session(engine)
            async with Session() as db:
                resp = await create_restore(
                    body=RestoreRequest(job_id='arc'),
                    user=admin, db=db,
                )
            assert resp["status"] == 'pending'
            assert enqueued == ['pan_restore']

    run_async(_go())


# =========================================================================
# DELETE /credentials
# =========================================================================


def test_disconnect_credentials_flips_to_revoked(monkeypatch):
    from models import PanCredentials
    from pan.admin_api import disconnect_credentials

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_pan_credentials(
                engine, user_id=admin.id, status='active',
            )
            Session = await _session(engine)
            async with Session() as db:
                resp = await disconnect_credentials(user=admin, db=db)
            assert resp.status_code == 204

            async with Session() as db:
                status = (await db.execute(
                    select(PanCredentials.status)
                    .where(PanCredentials.user_id == admin.id)
                )).scalar_one()
            assert status == 'revoked'

    run_async(_go())


def test_disconnect_credentials_idempotent_when_no_row():
    """Disconnecting with no PanCredentials row is a no-op 204."""
    from pan.admin_api import disconnect_credentials

    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                resp = await disconnect_credentials(user=admin, db=db)
            assert resp.status_code == 204

    run_async(_go())


# =========================================================================
# T7.6 — DELETE /backups/{id}: spec §6 protection logic
# =========================================================================


async def _run_delete(monkeypatch, backup_id, user, db, *, client_cls=None):
    from pan.admin_api import _delete_backup_impl
    client_cls = client_cls or FakeBaiduPanClient
    return await _delete_backup_impl(
        backup_id=backup_id, user=user, db=db,
        client_factory=lambda: client_cls(),
    )


def test_delete_backup_404_on_missing(monkeypatch):
    from fastapi import HTTPException

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await _run_delete(
                        monkeypatch, str(uuid.uuid4()), admin, db,
                    )
            assert exc.value.status_code == 404

    run_async(_go())


def test_delete_backup_idempotent_on_already_deleted(monkeypatch):
    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    deleted_remote: list[str] = []

    class TrackClient(FakeBaiduPanClient):
        def delete(self, remote_path, *, access_token):
            deleted_remote.append(remote_path)
            return super().delete(remote_path, access_token=access_token)

    async def _go():
        async with admin_api_engine() as engine:
            br = await insert_sample_backup_record(
                engine, user_id=admin.id, job_id='gone',
                status='deleted',
            )
            Session = await _session(engine)
            async with Session() as db:
                resp = await _run_delete(
                    monkeypatch, str(br['id']), admin, db,
                    client_cls=TrackClient,
                )
            assert resp.status_code == 204
            # Remote delete NOT invoked for already-deleted row.
            assert deleted_remote == []

    run_async(_go())


def test_delete_backup_412_when_unique_recoverable_copy_of_archived_job(
    monkeypatch,
):
    """§6 protection: archived job at gen=N, only one 'uploaded' BackupRecord
    at gen=N → DELETE refused with 412. Admin must restore first or take a
    new backup."""
    from fastapi import HTTPException
    from models import BackupRecord

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_job(
                engine, user_id=admin.id, job_id='ar', status='archived',
                edit_generation=2,
            )
            br = await insert_sample_backup_record(
                engine, user_id=admin.id, job_id='ar',
                job_edit_generation=2, status='uploaded',
            )
            await insert_sample_pan_credentials(engine, user_id=admin.id)
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await _run_delete(
                        monkeypatch, str(br['id']), admin, db,
                    )
            assert exc.value.status_code == 412
            assert "唯一可恢复副本" in str(exc.value.detail)

            # Row UNCHANGED.
            async with Session() as db:
                status = (await db.execute(
                    select(BackupRecord.status)
                    .where(BackupRecord.id == br['id'])
                )).scalar_one()
            assert status == 'uploaded'

    run_async(_go())


def test_delete_backup_allowed_when_sibling_uploaded_exists(monkeypatch):
    """archived job + TWO 'uploaded' BackupRecord rows at same generation →
    delete is allowed (the sibling still keeps the job recoverable)."""
    from models import BackupRecord

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_job(
                engine, user_id=admin.id, job_id='ar', status='archived',
                edit_generation=2,
            )
            br_doomed = await insert_sample_backup_record(
                engine, user_id=admin.id, job_id='ar',
                job_edit_generation=2, status='uploaded',
                remote_path='/apps/AIVideoTrans/backups/doomed.tar.gz',
            )
            br_sibling = await insert_sample_backup_record(
                engine, user_id=admin.id, job_id='ar',
                job_edit_generation=2, status='uploaded',
            )
            await insert_sample_pan_credentials(engine, user_id=admin.id)
            Session = await _session(engine)
            async with Session() as db:
                resp = await _run_delete(
                    monkeypatch, str(br_doomed['id']), admin, db,
                )
            assert resp.status_code == 204

            async with Session() as db:
                doomed_status = (await db.execute(
                    select(BackupRecord.status)
                    .where(BackupRecord.id == br_doomed['id'])
                )).scalar_one()
                sibling_status = (await db.execute(
                    select(BackupRecord.status)
                    .where(BackupRecord.id == br_sibling['id'])
                )).scalar_one()
            assert doomed_status == 'deleted'
            assert sibling_status == 'uploaded'

    run_async(_go())


def test_delete_backup_allowed_when_job_not_archived(monkeypatch):
    """Job is 'succeeded' (re-backup scenario) → DELETE of an uploaded
    backup is allowed; the live project_dir still has the data."""
    from models import BackupRecord

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_job(
                engine, user_id=admin.id, job_id='live', status='succeeded',
            )
            br = await insert_sample_backup_record(
                engine, user_id=admin.id, job_id='live', status='uploaded',
            )
            await insert_sample_pan_credentials(engine, user_id=admin.id)
            Session = await _session(engine)
            async with Session() as db:
                resp = await _run_delete(
                    monkeypatch, str(br['id']), admin, db,
                )
            assert resp.status_code == 204

            async with Session() as db:
                status = (await db.execute(
                    select(BackupRecord.status)
                    .where(BackupRecord.id == br['id'])
                )).scalar_one()
            assert status == 'deleted'

    run_async(_go())


def test_delete_backup_409_when_uploading(monkeypatch):
    """CodeX P0: DELETE while backup_executor is mid-write (status=
    'uploading') must 409. Otherwise we'd let admin destroy a partial
    upload AND any subsequent successful executor commit would write
    to a remote_path that's been deleted."""
    from fastapi import HTTPException
    from models import BackupRecord

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    remote_deletes: list[str] = []

    class TrackClient(FakeBaiduPanClient):
        def delete(self, remote_path, *, access_token):
            remote_deletes.append(remote_path)
            return super().delete(remote_path, access_token=access_token)

    async def _go():
        async with admin_api_engine() as engine:
            br = await insert_sample_backup_record(
                engine, user_id=admin.id, job_id='live',
                status='uploading',  # backup_executor in progress
                remote_path='/apps/AIVideoTrans/backups/inflight.tar.gz',
            )
            await insert_sample_pan_credentials(engine, user_id=admin.id)
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await _run_delete(
                        monkeypatch, str(br['id']), admin, db,
                        client_cls=TrackClient,
                    )
            assert exc.value.status_code == 409
            assert 'uploading' in str(exc.value.detail)

            # NO remote delete attempted.
            assert remote_deletes == []

            # Row state unchanged.
            async with Session() as db:
                status = (await db.execute(
                    select(BackupRecord.status)
                    .where(BackupRecord.id == br['id'])
                )).scalar_one()
            assert status == 'uploading'

    run_async(_go())


def test_delete_backup_409_when_restoring(monkeypatch):
    """CodeX P0 (data safety): DELETE during restore must 409. restore_
    executor sets status='restoring' then DOWNLOADS the remote tar; if
    DELETE wiped the remote tar mid-restore, the executor would fail
    AND its rollback to status='uploaded' would leave the row pointing
    at a dead remote_path — permanently broken state."""
    from fastapi import HTTPException
    from models import BackupRecord

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    remote_deletes: list[str] = []

    class TrackClient(FakeBaiduPanClient):
        def delete(self, remote_path, *, access_token):
            remote_deletes.append(remote_path)
            return super().delete(remote_path, access_token=access_token)

    async def _go():
        async with admin_api_engine() as engine:
            br = await insert_sample_backup_record(
                engine, user_id=admin.id, job_id='restoring_job',
                status='restoring',
                remote_path='/apps/AIVideoTrans/backups/being_restored.tar.gz',
            )
            await insert_sample_pan_credentials(engine, user_id=admin.id)
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc:
                    await _run_delete(
                        monkeypatch, str(br['id']), admin, db,
                        client_cls=TrackClient,
                    )
            assert exc.value.status_code == 409
            assert 'restoring' in str(exc.value.detail)
            assert remote_deletes == [], (
                "remote tar MUST NOT be deleted during restore"
            )

    run_async(_go())


def test_delete_backup_allowed_on_terminal_states(monkeypatch):
    """Terminal states ('failed', 'restored') allow soft-delete.
    'uploaded' goes through the §6 412 guard (covered separately).
    'deleted' is idempotent 204 (covered separately).

    Parametrize across the two clear-cut "allowed" terminal states.
    """
    from models import BackupRecord

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    for terminal_status in ('failed', 'restored'):
        async def _go(status=terminal_status):
            async with admin_api_engine() as engine:
                br = await insert_sample_backup_record(
                    engine, user_id=admin.id, job_id=f'job_{status}',
                    status=status,
                    remote_path=f'/apps/AIVideoTrans/backups/{status}.tar.gz',
                )
                await insert_sample_pan_credentials(engine, user_id=admin.id)
                Session = await _session(engine)
                async with Session() as db:
                    resp = await _run_delete(
                        monkeypatch, str(br['id']), admin, db,
                    )
                assert resp.status_code == 204, (
                    f"DELETE on terminal status={status!r} should succeed"
                )
                async with Session() as db:
                    new_status = (await db.execute(
                        select(BackupRecord.status)
                        .where(BackupRecord.id == br['id'])
                    )).scalar_one()
                assert new_status == 'deleted'

        run_async(_go())


def test_list_backups_status_filter_is_query_param_not_body():
    """CodeX P1: `status` MUST be registered as a query parameter, NOT a
    request body. With plain `list[str] | None = None`, FastAPI defaults
    list types to body — production GET /backups?status=uploaded would
    NOT filter. Annotated[list[str] | None, Query()] keeps direct-call
    default as None AND registers as multi-value query.

    Inspect the route's DependantNode to lock the contract."""
    from pan.admin_api import router

    list_route = next(
        r for r in router.routes
        if getattr(r, 'path', '') == '/api/admin/pan/backups'
        and 'GET' in getattr(r, 'methods', set())
    )
    query_param_names = {p.name for p in list_route.dependant.query_params}
    body_param_names = {p.name for p in list_route.dependant.body_params}

    # 'status' MUST be query, not body.
    assert 'status' in query_param_names, (
        f"`status` must be registered as a query parameter, got "
        f"query={query_param_names}, body={body_param_names}"
    )
    assert 'status' not in body_param_names, (
        f"`status` must NOT be registered as a body parameter"
    )
    # Other filters also query (auto-detected from simple types).
    assert 'user_id' in query_param_names
    assert 'job_id' in query_param_names
    assert 'limit' in query_param_names
    assert 'offset' in query_param_names


def test_list_backups_status_filter_works_via_test_client():
    """Integration sanity: real FastAPI TestClient with ?status=X&status=Y
    actually filters. The previous bug (status as body) wouldn't have
    been caught by direct-call tests — only HTTP-level integration
    exercises the query parsing."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from pan.admin_api import router
    from pan import admin_api as api_mod

    app = FastAPI()
    app.include_router(router)

    admin = _admin_user()

    # Override admin auth + DB deps via FastAPI's dependency_overrides.
    from auth import get_current_user
    from database import get_db

    async def fake_user():
        return admin

    captured_filters: list[dict] = []

    # Stub list_backups handler by patching the underlying SQL — but
    # easier: mock the SQLAlchemy session to capture the resulting query.
    # Simplest: patch the handler to capture its args, run via TestClient.
    real_handler = api_mod.list_backups

    async def capturing_handler(
        user=None, db=None,
        status=None, user_id=None, job_id=None, limit=50, offset=0,
    ):
        captured_filters.append({
            'status': status, 'user_id': user_id, 'job_id': job_id,
            'limit': limit, 'offset': offset,
        })
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    async def fake_db():
        yield None

    app.dependency_overrides[get_current_user] = fake_user
    app.dependency_overrides[get_db] = fake_db

    # Swap the handler reference on the route's endpoint.
    for r in app.routes:
        if getattr(r, 'path', '') == '/api/admin/pan/backups' \
                and 'GET' in getattr(r, 'methods', set()):
            r.endpoint = capturing_handler
            r.dependant.call = capturing_handler

    try:
        client = TestClient(app)
        resp = client.get(
            "/api/admin/pan/backups?status=uploaded&status=deleted&limit=10",
        )
        assert resp.status_code == 200, resp.text
    finally:
        # Restore the real handler so other tests in the module aren't
        # affected (shouldn't matter since router is per-import-once,
        # but defensive).
        for r in app.routes:
            if getattr(r, 'path', '') == '/api/admin/pan/backups' \
                    and 'GET' in getattr(r, 'methods', set()):
                r.endpoint = real_handler

    # The CRUCIAL assertion: `status` got parsed as a 2-element list,
    # NOT as None (which is what happens if FastAPI treats it as a
    # missing body field).
    assert len(captured_filters) == 1
    cap = captured_filters[0]
    assert cap['status'] == ['uploaded', 'deleted'], (
        f"?status=uploaded&status=deleted should parse to "
        f"['uploaded', 'deleted'], got {cap['status']!r}"
    )
    assert cap['limit'] == 10


def test_admin_pan_router_registered_in_main():
    """T7 wire-up: gateway/main.py imports pan.admin_api.router and
    calls app.include_router(pan_admin_router). Without both lines the
    8 admin endpoints are 404 in production."""
    import ast
    from pathlib import Path

    main_py = (
        Path(__file__).resolve().parent.parent / 'gateway' / 'main.py'
    )
    text = main_py.read_text(encoding='utf-8')
    tree = ast.parse(text)

    imported_as: str | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == 'pan.admin_api':
            for alias in node.names:
                if alias.name == 'router':
                    imported_as = alias.asname or 'router'
                    break
    assert imported_as is not None, (
        "gateway/main.py must `from pan.admin_api import router as pan_admin_router`"
    )

    found_include = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'include_router'
            and len(node.args) >= 1
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == imported_as
        ):
            found_include = True
            break
    assert found_include, (
        f"gateway/main.py must call app.include_router({imported_as})"
    )


def test_delete_backup_continues_pg_soft_delete_on_remote_failure(monkeypatch):
    """Remote tar.gz delete failure → log + continue to PG soft delete.
    Orphan_cleanup picks up the stranded remote object later."""
    from models import BackupRecord

    setup_pan_token_env(monkeypatch)
    admin = _admin_user()

    class FailingDeleteClient(FakeBaiduPanClient):
        def delete(self, remote_path, *, access_token):
            raise RuntimeError('synthetic remote delete failure')

    async def _go():
        async with admin_api_engine() as engine:
            await insert_sample_job(
                engine, user_id=admin.id, job_id='live', status='succeeded',
            )
            br = await insert_sample_backup_record(
                engine, user_id=admin.id, job_id='live', status='uploaded',
                remote_path='/apps/AIVideoTrans/backups/stranded.tar.gz',
            )
            await insert_sample_pan_credentials(engine, user_id=admin.id)
            Session = await _session(engine)
            async with Session() as db:
                resp = await _run_delete(
                    monkeypatch, str(br['id']), admin, db,
                    client_cls=FailingDeleteClient,
                )
            assert resp.status_code == 204

            async with Session() as db:
                status = (await db.execute(
                    select(BackupRecord.status)
                    .where(BackupRecord.id == br['id'])
                )).scalar_one()
            # Still soft-deleted in PG despite remote failure.
            assert status == 'deleted'

    run_async(_go())
