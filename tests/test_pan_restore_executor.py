"""Tests for gateway.pan.restore_executor.

Plan §8 + Phase 5b T5.11. The most realistic test is a full round-trip:
backup_executor produces a real archive via FakeBaiduPanClient, then
restore_executor pulls it back and verifies project_dir matches the
original byte-for-byte.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.pan_fixtures import (
    FakeBaiduPanClient,
    insert_sample_backup_record,
    insert_sample_job,
    insert_sample_pan_credentials,
    make_project_dir,
    pan_test_engine,
    run_async,
    setup_pan_token_env,
)


# --- helpers ---


def _noop_rmtree(path):
    pass


def _noop_r2_delete(key):
    pass


async def _run_backup(
    payload, *, engine, client, rmtree_fn=_noop_rmtree,
    r2_delete_fn=_noop_r2_delete,
):
    from gateway.pan.backup_executor import _execute_pan_backup_impl

    await _execute_pan_backup_impl(
        payload,
        engine=engine,
        client_factory=lambda: client,
        rmtree_fn=rmtree_fn,
        r2_delete_fn=r2_delete_fn,
        heartbeat_enabled=False,
    )


async def _run_restore(payload, *, engine, client, staging_root=None):
    from gateway.pan.restore_executor import _execute_pan_restore_impl

    await _execute_pan_restore_impl(
        payload,
        engine=engine,
        client_factory=lambda: client,
        staging_root=staging_root,
        heartbeat_enabled=False,  # no 60s waits in tests
    )


# =========================================================================
# Precondition
# =========================================================================


def test_precondition_rejects_non_archived_job(monkeypatch, tmp_path):
    """Job must be in 'archived' state to restore."""
    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_not_arch'

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='succeeded', project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            with pytest.raises(RuntimeError, match="not 'archived'|412"):
                await _run_restore(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                )

    run_async(_go())


def test_precondition_rejects_missing_backup_record(monkeypatch, tmp_path):
    """archived status but no BackupRecord row → raise."""
    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_no_br'

    async def _go():
        async with pan_test_engine() as engine:
            project_path = tmp_path / job_id
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archived', project_dir=str(project_path),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)
            # No BackupRecord inserted.

            client = FakeBaiduPanClient()
            with pytest.raises(RuntimeError, match='No .* BackupRecord'):
                await _run_restore(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                )

    run_async(_go())


def test_precondition_rejects_backup_with_failed_status(monkeypatch, tmp_path):
    """BackupRecord exists but status='failed' → no 'uploaded' row found."""
    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_br_failed'

    async def _go():
        async with pan_test_engine() as engine:
            project_path = tmp_path / job_id
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archived', project_dir=str(project_path),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)
            await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id, status='failed',
            )

            client = FakeBaiduPanClient()
            with pytest.raises(RuntimeError, match='No .* BackupRecord'):
                await _run_restore(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                )

    run_async(_go())


def test_precondition_rejects_missing_project_dir(monkeypatch):
    """Job.project_dir is NULL → cannot determine restore destination."""
    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_no_proj_path'

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id, status='archived',
                # project_dir=None (default)
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            with pytest.raises(RuntimeError, match='no project_dir'):
                await _run_restore(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                )

    run_async(_go())


# =========================================================================
# Happy path round-trip
# =========================================================================


def test_round_trip_backup_then_restore(monkeypatch, tmp_path):
    """End-to-end: backup_executor archives a project_dir, then restore_executor
    pulls it back. Restored project_dir must equal the original byte-for-byte."""
    from models import Job, BackupRecord

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_roundtrip'

    # Use a real rmtree so backup actually deletes project_dir — then we
    # can assert restore re-creates it.
    import shutil

    # Capture original snapshot before backup.
    project = make_project_dir(tmp_path, job_id=job_id)
    original_snapshot = _snapshot_dir(project)

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='succeeded', project_dir=str(project),
                edit_generation=2,
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            # Shared client — same fake storage across backup + restore.
            client = FakeBaiduPanClient()
            await _run_backup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client, rmtree_fn=shutil.rmtree,
            )

            # After backup: project_dir gone, Job.status='archived'.
            assert not project.exists()
            async with engine.connect() as conn:
                status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
            assert status == 'archived'

            # Now restore.
            await _run_restore(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client,
                staging_root=tmp_path / '_staging',
            )

            # Project_dir is back, Job.status='succeeded'.
            assert project.exists()
            restored_snapshot = _snapshot_dir(project)
            assert restored_snapshot == original_snapshot

            async with engine.connect() as conn:
                final_status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
            assert final_status == 'succeeded'

    run_async(_go())


def test_round_trip_chinese_filenames_and_content(monkeypatch, tmp_path):
    """Unicode in filenames AND content survives backup→restore."""
    import shutil

    setup_pan_function_env_helper(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_unicode'

    project = tmp_path / job_id
    (project / '中文目录').mkdir(parents=True)
    (project / '中文目录' / '文件.json').write_text(
        json.dumps({'note': '配音任务 ✨', 'segs': [1, 2, 3]}, ensure_ascii=False),
        encoding='utf-8',
    )
    original_snapshot = _snapshot_dir(project)

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='succeeded', project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            await _run_backup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client, rmtree_fn=shutil.rmtree,
            )
            await _run_restore(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client,
                staging_root=tmp_path / '_staging',
            )
            restored = _snapshot_dir(project)
            assert restored == original_snapshot

    run_async(_go())


# =========================================================================
# Failure paths
# =========================================================================


def test_sha256_mismatch_rolls_back_to_archived(monkeypatch, tmp_path):
    """If BackupRecord.sha256 doesn't match the downloaded bytes' sha256
    (data drift / corruption), restore raises and Job.status rolls back
    to 'archived'. project_dir must NOT be partially populated."""
    from models import Job
    import shutil

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_sha_drift'

    project = make_project_dir(tmp_path, job_id=job_id)

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='succeeded', project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            await _run_backup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client, rmtree_fn=shutil.rmtree,
            )

            # Tamper with BackupRecord.sha256 to simulate data drift.
            from models import BackupRecord
            async with engine.begin() as conn:
                await conn.execute(
                    BackupRecord.__table__.update()
                    .where(BackupRecord.job_id == job_id)
                    .values(sha256='deadbeef' * 8)  # fake sha
                )

            with pytest.raises(RuntimeError, match='sha256 mismatch'):
                await _run_restore(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                    staging_root=tmp_path / '_staging',
                )

            # Job.status rolled back.
            async with engine.connect() as conn:
                status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
            assert status == 'archived'

            # Project_dir NOT created — failure was pre-extract.
            assert not project.exists()

    run_async(_go())


def test_status_rolls_back_on_inventory_verify_failure(monkeypatch, tmp_path):
    """If file inventory verification fails (file content drift post-extract),
    Job.status rolls back to 'archived'. Test via monkey-patching the
    inventory helper to always raise."""
    from models import Job
    import shutil

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_inv_fail'
    project = make_project_dir(tmp_path, job_id=job_id)

    # Patch _verify_inventory to raise.
    from gateway.pan import restore_executor as re_mod

    def fake_verify(staged, inv):
        raise RuntimeError('synthetic inventory failure')

    monkeypatch.setattr(re_mod, '_verify_inventory', fake_verify)

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='succeeded', project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            await _run_backup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client, rmtree_fn=shutil.rmtree,
            )

            with pytest.raises(RuntimeError, match='synthetic inventory failure'):
                await _run_restore(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                    staging_root=tmp_path / '_staging',
                )

            async with engine.connect() as conn:
                status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
            assert status == 'archived'

            # Project_dir NOT moved into place — staging held the tampered
            # files and was cleaned up.
            assert not project.exists()

    run_async(_go())


def test_status_rolls_back_on_download_failure(monkeypatch, tmp_path):
    """Pan download throws → status back to 'archived'."""
    from models import Job
    import shutil

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_dl_fail'
    project = make_project_dir(tmp_path, job_id=job_id)

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='succeeded', project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            await _run_backup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client, rmtree_fn=shutil.rmtree,
            )

            # Now inject download failure for the restore call.
            client.inject_download_failure(RuntimeError('synthetic download err'))
            with pytest.raises(RuntimeError, match='synthetic download err'):
                await _run_restore(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                    staging_root=tmp_path / '_staging',
                )

            async with engine.connect() as conn:
                status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
            assert status == 'archived'

    run_async(_go())


# =========================================================================
# Multiple BackupRecord rows — pick latest
# =========================================================================


def test_restore_picks_latest_uploaded_backup_record(monkeypatch, tmp_path):
    """If a job has multiple BackupRecord rows (e.g. previously failed +
    successful re-backup), restore uses the LATEST uploaded one."""
    import shutil
    from models import BackupRecord

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_multi_br'

    project = make_project_dir(tmp_path, job_id=job_id)
    original_snapshot = _snapshot_dir(project)

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='succeeded', project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()

            # First do a real backup (creates an 'uploaded' BackupRecord).
            await _run_backup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client, rmtree_fn=shutil.rmtree,
            )
            # Now insert a stale 'failed' BackupRecord with bogus data —
            # it has a later created_at by chance, but status='failed' so
            # restore must skip it.
            from datetime import datetime, timezone, timedelta
            future = datetime.now(timezone.utc) + timedelta(seconds=10)
            await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id,
                status='failed',
                remote_path='/garbage/never_existed.tar.gz',
                sha256='000' * 21 + 'a',
                heartbeat_at=future,
            )

            # Restore should still work — picks the real 'uploaded' row.
            await _run_restore(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client,
                staging_root=tmp_path / '_staging',
            )
            assert project.exists()
            assert _snapshot_dir(project) == original_snapshot

    run_async(_go())


# =========================================================================
# _verify_inventory unit tests
# =========================================================================


def test_restore_flips_backup_record_status_to_restored(monkeypatch, tmp_path):
    """CodeX P1-1: BackupRecord.status moves uploaded → restoring → restored
    over the lifecycle. Heartbeat updates while in 'restoring'."""
    import shutil
    from models import BackupRecord

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_br_lifecycle'

    project = make_project_dir(tmp_path, job_id=job_id)

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='succeeded', project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            await _run_backup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client, rmtree_fn=shutil.rmtree,
            )

            # Pre-restore: BackupRecord status='uploaded'.
            async with engine.connect() as conn:
                status_before = (await conn.execute(
                    select(BackupRecord.status).where(BackupRecord.job_id == job_id)
                )).scalar_one()
            assert status_before == 'uploaded'

            await _run_restore(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client,
                staging_root=tmp_path / '_staging',
            )

            # Post-restore: BackupRecord status='restored', completed_at set.
            async with engine.connect() as conn:
                row = (await conn.execute(
                    select(BackupRecord.status, BackupRecord.completed_at)
                    .where(BackupRecord.job_id == job_id)
                )).one()
            assert row.status == 'restored'
            assert row.completed_at is not None

    run_async(_go())


def test_restore_reverts_backup_record_to_uploaded_on_failure(monkeypatch, tmp_path):
    """CodeX P1-1: failure path reverts BackupRecord.status='restoring' back
    to 'uploaded' so the next restore attempt can re-pick this row."""
    import shutil
    from models import BackupRecord

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_br_revert'

    project = make_project_dir(tmp_path, job_id=job_id)

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='succeeded', project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            await _run_backup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client, rmtree_fn=shutil.rmtree,
            )

            client.inject_download_failure(RuntimeError('synthetic dl'))
            with pytest.raises(RuntimeError, match='synthetic dl'):
                await _run_restore(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                    staging_root=tmp_path / '_staging',
                )

            # BackupRecord reverted to 'uploaded' — next attempt re-picks it.
            async with engine.connect() as conn:
                status = (await conn.execute(
                    select(BackupRecord.status).where(BackupRecord.job_id == job_id)
                )).scalar_one()
            assert status == 'uploaded'

    run_async(_go())


def test_restore_rejects_mismatched_edit_generation(monkeypatch, tmp_path):
    """CodeX P1-1 + plan §8: restore must NOT pick a BackupRecord whose
    job_edit_generation doesn't match the current Job.edit_generation.
    Restoring an older snapshot onto a newer Job would corrupt state."""
    import shutil
    from models import Job, BackupRecord

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_gen_mismatch'

    project = make_project_dir(tmp_path, job_id=job_id)

    async def _go():
        async with pan_test_engine() as engine:
            # Insert Job at edit_generation=3 to start.
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='succeeded', project_dir=str(project),
                edit_generation=3,
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            # Backup captures gen=3.
            client = FakeBaiduPanClient()
            await _run_backup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client, rmtree_fn=shutil.rmtree,
            )

            # Simulate Job getting edited AFTER archive (would have to be
            # via some recovery flow — pretend admin tooling bumped it).
            async with engine.begin() as conn:
                await conn.execute(
                    Job.__table__.update()
                    .where(Job.job_id == job_id)
                    .values(edit_generation=5)
                )

            # Restore must refuse — generations don't match.
            with pytest.raises(RuntimeError, match='job_edit_generation=5|gen=3'):
                await _run_restore(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                    staging_root=tmp_path / '_staging',
                )

            # BackupRecord stays at 'uploaded' (we never started restoring).
            async with engine.connect() as conn:
                status = (await conn.execute(
                    select(BackupRecord.status).where(BackupRecord.job_id == job_id)
                )).scalar_one()
            assert status == 'uploaded'

    run_async(_go())


def test_verify_inventory_passes_on_correct_files(tmp_path):
    """Happy path: inventory matches actual files."""
    import hashlib
    from gateway.pan.restore_executor import _verify_inventory

    staged = tmp_path / 'job_x'
    (staged / 'sub').mkdir(parents=True)
    payload = b'real_content' * 50
    (staged / 'sub' / 'f.bin').write_bytes(payload)

    inventory = [{
        'path': 'sub/f.bin',
        'size': len(payload),
        'sha256': hashlib.sha256(payload).hexdigest(),
    }]
    _verify_inventory(staged, inventory)  # no raise


def test_verify_inventory_raises_on_missing_file(tmp_path):
    from gateway.pan.restore_executor import _verify_inventory

    staged = tmp_path / 'job_x'
    staged.mkdir()
    inventory = [{
        'path': 'never_existed.bin',
        'size': 10,
        'sha256': 'a' * 64,
    }]
    with pytest.raises(RuntimeError, match='file missing post-extract'):
        _verify_inventory(staged, inventory)


def test_verify_inventory_raises_on_size_mismatch(tmp_path):
    from gateway.pan.restore_executor import _verify_inventory

    staged = tmp_path / 'job_x'
    staged.mkdir()
    (staged / 'a.bin').write_bytes(b'small')  # size 5

    inventory = [{
        'path': 'a.bin',
        'size': 9999,  # claim 9999 bytes
        'sha256': 'a' * 64,
    }]
    with pytest.raises(RuntimeError, match='size mismatch'):
        _verify_inventory(staged, inventory)


def test_post_lock_re_read_detects_concurrent_state_change(monkeypatch, tmp_path):
    """CodeX P0-2 regression: restore must read Job state AFTER acquiring
    the lock. A concurrent worker that flips status away from 'archived'
    while we wait must be visible — without this, we'd proceed with
    stale snapshot and corrupt state."""
    import shutil
    from models import Job
    from gateway.pan import restore_executor as re_mod

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_restore_toctou'
    project = make_project_dir(tmp_path, job_id=job_id)

    # restore_executor imports _acquire_advisory_lock function-locally
    # from backup_executor, so we patch the source module — the function
    # re-imports on each call and will see the patch.
    from gateway.pan import backup_executor as be_mod
    real_acquire = be_mod._acquire_advisory_lock
    engine_holder: list = []

    async def lock_then_mutate(conn, key):
        await real_acquire(conn, key)
        engine_inner = engine_holder[0]
        async with engine_inner.begin() as side_conn:
            await side_conn.execute(
                Job.__table__.update()
                .where(Job.job_id == job_id)
                .values(status='succeeded')  # concurrent restore just finished
            )

    async def _go():
        async with pan_test_engine() as engine:
            engine_holder.append(engine)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='succeeded', project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            # Set up state where restore would normally succeed.
            client = FakeBaiduPanClient()
            await _run_backup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client, rmtree_fn=shutil.rmtree,
            )

            # Now patch the lock to simulate concurrent restore happening
            # during our wait. After this, Job.status will be 'succeeded'
            # by the time our restore acquires the lock.
            monkeypatch.setattr(be_mod, '_acquire_advisory_lock', lock_then_mutate)

            with pytest.raises(RuntimeError, match="not 'archived'|412"):
                await _run_restore(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                    staging_root=tmp_path / '_staging',
                )

            # Project_dir must NOT have been re-created by this stale restore.
            # (The concurrent restore that flipped status to 'succeeded'
            # would have already created it, but the side-conn update we
            # used didn't actually restore files, so project_dir is gone.
            # The key assertion: status is still 'succeeded' — our stale
            # restore did NOT roll it back to 'archived'.)
            async with engine.connect() as conn:
                status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
            assert status == 'succeeded'

    run_async(_go())


def test_verify_inventory_raises_on_sha256_mismatch(tmp_path):
    from gateway.pan.restore_executor import _verify_inventory

    staged = tmp_path / 'job_x'
    staged.mkdir()
    (staged / 'a.bin').write_bytes(b'content')

    inventory = [{
        'path': 'a.bin',
        'size': 7,
        'sha256': '0' * 64,  # wrong
    }]
    with pytest.raises(RuntimeError, match='sha256 mismatch'):
        _verify_inventory(staged, inventory)


# =========================================================================
# helpers
# =========================================================================


def _snapshot_dir(project: Path) -> dict[str, bytes]:
    """Recursively read all files under project, keyed by POSIX-relative path."""
    out: dict[str, bytes] = {}
    for p in project.rglob('*'):
        if p.is_file():
            rel = p.relative_to(project).as_posix()
            out[rel] = p.read_bytes()
    return out


def setup_pan_function_env_helper(monkeypatch):
    """Alias for setup_pan_token_env so other tests in this file find it."""
    return setup_pan_token_env(monkeypatch)
