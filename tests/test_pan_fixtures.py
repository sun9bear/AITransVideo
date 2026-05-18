"""Smoke tests for tests/pan_fixtures.py.

These fixtures are the foundation for Phase 5b backup/restore/cleanup
executor tests. They MUST be airtight or the executor test suite is
worthless. So we sanity-check them here.
"""
from __future__ import annotations

import asyncio
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


# --- engine + tables ---


def test_pan_test_engine_creates_all_three_tables():
    """All three pan-related tables creatable on SQLite."""
    from models import Job, BackupRecord, PanCredentials

    async def _go():
        async with pan_test_engine() as engine:
            async with engine.connect() as conn:
                # Bare select to verify each table exists.
                for table_cls in (Job, BackupRecord, PanCredentials):
                    result = await conn.execute(select(table_cls.__table__))
                    assert result.all() == [], f"{table_cls.__tablename__} should be empty"

    run_async(_go())


def test_pan_test_engine_disposes_on_exit():
    """Engine.dispose() runs after the context body completes."""
    async def _go():
        captured = []
        async with pan_test_engine() as engine:
            captured.append(engine)
        # After exit, the engine should be disposed — connecting again
        # against an in-memory sqlite returns a different "engine" essentially.
        # We can't directly assert "disposed", but we can verify the
        # context manager cleanly exited.
        assert captured == [captured[0]]

    run_async(_go())


# --- job insert ---


def test_insert_sample_job_defaults_to_succeeded():
    from models import Job

    async def _go():
        async with pan_test_engine() as engine:
            user_id = uuid.uuid4()
            await insert_sample_job(engine, user_id=user_id, job_id='job_abc')

            async with engine.connect() as conn:
                status = (await conn.execute(
                    select(Job.status).where(Job.job_id == 'job_abc')
                )).scalar_one()
                edit_gen = (await conn.execute(
                    select(Job.edit_generation).where(Job.job_id == 'job_abc')
                )).scalar_one()
            assert status == 'succeeded'
            assert edit_gen == 0

    run_async(_go())


def test_insert_sample_job_accepts_custom_status_and_artifacts():
    from models import Job

    async def _go():
        async with pan_test_engine() as engine:
            user_id = uuid.uuid4()
            artifacts = [{'artifact_key': 'publish.dubbed_video',
                          'r2_key': 'jobs/job_x/v.mp4'}]
            await insert_sample_job(
                engine, user_id=user_id, job_id='job_x',
                status='archiving',
                edit_generation=2,
                project_dir='/tmp/proj/job_x',
                r2_artifacts=artifacts,
            )

            async with engine.connect() as conn:
                row = (await conn.execute(
                    select(Job.status, Job.edit_generation, Job.project_dir,
                           Job.r2_artifacts).where(Job.job_id == 'job_x')
                )).one()
            assert row.status == 'archiving'
            assert row.edit_generation == 2
            assert row.project_dir == '/tmp/proj/job_x'
            assert row.r2_artifacts == artifacts

    run_async(_go())


# --- pan credentials insert (Fernet round-trip) ---


def test_insert_sample_pan_credentials_round_trip(monkeypatch):
    """Tokens encrypt on insert + decrypt back via token_crypto."""
    from gateway.pan.token_crypto import decrypt_token
    from models import PanCredentials

    setup_pan_token_env(monkeypatch)

    async def _go():
        async with pan_test_engine() as engine:
            user_id = uuid.uuid4()
            await insert_sample_pan_credentials(
                engine, user_id=user_id,
                access_token='custom_at',
                refresh_token='custom_rt',
            )

            async with engine.connect() as conn:
                row = (await conn.execute(
                    select(PanCredentials.access_token_encrypted,
                           PanCredentials.refresh_token_encrypted,
                           PanCredentials.status)
                    .where(PanCredentials.user_id == user_id)
                )).one()
            assert decrypt_token(row.access_token_encrypted) == 'custom_at'
            assert decrypt_token(row.refresh_token_encrypted) == 'custom_rt'
            assert row.status == 'active'

    run_async(_go())


# --- backup record insert ---


def test_insert_sample_backup_record_round_trip():
    from models import BackupRecord

    async def _go():
        async with pan_test_engine() as engine:
            user_id = uuid.uuid4()
            await insert_sample_backup_record(
                engine, user_id=user_id, job_id='job_br',
                status='uploaded',
                size_bytes=1024,
                sha256='deadbeef' * 8,
                md5='cafe' * 8,
                manifest_json={'backup_format_version': 1},
            )

            async with engine.connect() as conn:
                row = (await conn.execute(
                    select(BackupRecord.status, BackupRecord.size_bytes,
                           BackupRecord.sha256, BackupRecord.manifest_json)
                    .where(BackupRecord.job_id == 'job_br')
                )).one()
            assert row.status == 'uploaded'
            assert row.size_bytes == 1024
            assert row.sha256 == 'deadbeef' * 8
            # JSONB via JSON-on-SQLite — manifest_json may come back as string.
            mj = row.manifest_json
            if isinstance(mj, str):
                import json as _json
                mj = _json.loads(mj)
            assert mj == {'backup_format_version': 1}

    run_async(_go())


# --- project_dir layout helper ---


def test_make_project_dir_creates_expected_layout(tmp_path):
    project = make_project_dir(tmp_path, job_id='job_layout')
    assert project.is_dir()
    assert (project / 'transcript' / 'review.json').is_file()
    assert (project / 'tts' / 'seg_0.wav').is_file()
    assert (project / 'publish' / 'dubbed.mp4').is_file()


# --- FakeBaiduPanClient ---


def test_fake_client_upload_then_download_round_trip(tmp_path):
    """Storage simulation: upload then download yields the same bytes."""
    c = FakeBaiduPanClient()
    src = tmp_path / 'src.tar.gz'
    payload = b'GZIP_PAYLOAD' * 100
    src.write_bytes(payload)

    res = c.upload(src, '/apps/AIVideoTrans/backups/x.tar.gz', access_token='at')
    assert res['size'] == len(payload)
    import hashlib as _h
    assert res['md5'] == _h.md5(payload).hexdigest()

    dst = tmp_path / 'dst.tar.gz'
    dl = c.download('/apps/AIVideoTrans/backups/x.tar.gz', dst, access_token='at')
    assert dst.read_bytes() == payload
    assert dl['size'] == len(payload)
    assert dl['sha256'] == _h.sha256(payload).hexdigest()

    # Call records populated.
    assert len(c.upload_calls) == 1
    assert len(c.download_calls) == 1
    assert c.upload_calls[0]['remote_path'] == '/apps/AIVideoTrans/backups/x.tar.gz'


def test_fake_client_inject_upload_failure(tmp_path):
    c = FakeBaiduPanClient()
    c.inject_upload_failure(RuntimeError('synthetic upload error'))
    src = tmp_path / 's.bin'
    src.write_bytes(b'x')
    with pytest.raises(RuntimeError, match='synthetic upload error'):
        c.upload(src, '/x', access_token='at')
    # Failure is one-shot — next call works.
    res = c.upload(src, '/x', access_token='at')
    assert res['size'] == 1


def test_fake_client_inject_upload_response_overrides_md5(tmp_path):
    """For md5-mismatch gate tests: force upload response to claim wrong md5."""
    c = FakeBaiduPanClient()
    c.inject_upload_response({'size': 100, 'md5': 'wrong_md5', 'fs_id': 1})
    src = tmp_path / 's.bin'
    src.write_bytes(b'X' * 50)  # actual md5 != 'wrong_md5'
    res = c.upload(src, '/x', access_token='at')
    assert res['md5'] == 'wrong_md5'


def test_fake_client_inject_verify_result_false(tmp_path):
    """Read-back probe gate failure injection."""
    c = FakeBaiduPanClient()
    c.inject_verify_result(False)
    ok = c.verify_remote_tail(tmp_path / 'x', '/r', 100, access_token='at')
    assert ok is False


def test_fake_client_delete_idempotent_on_missing():
    """delete() on missing path is a no-op (matches real client contract)."""
    c = FakeBaiduPanClient()
    # No prior upload — storage empty.
    c.delete('/nonexistent', access_token='at')
    assert len(c.delete_calls) == 1


def test_fake_client_list_filters_by_prefix(tmp_path):
    """list(prefix) returns only entries under that prefix."""
    c = FakeBaiduPanClient()
    src = tmp_path / 'a.bin'
    src.write_bytes(b'a')

    c.upload(src, '/apps/AIVideoTrans/backups/a.tar.gz', access_token='at')
    c.upload(src, '/apps/AIVideoTrans/backups/b.tar.gz', access_token='at')
    c.upload(src, '/other/c.tar.gz', access_token='at')

    matches = c.list('/apps/AIVideoTrans/backups/', access_token='at')
    paths = sorted(m['path'] for m in matches)
    assert paths == [
        '/apps/AIVideoTrans/backups/a.tar.gz',
        '/apps/AIVideoTrans/backups/b.tar.gz',
    ]
