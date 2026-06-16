"""Tests for gateway.pan.status_mutator.rollback_archive_attempt.

2026-05-26 postmortem P2b: the controlled operator entry point for
aborting an in-flight pan backup. Replaces raw ``UPDATE jobs SET ...`` /
partial-state edits — the 2026-06-02 manual abort marked backup_records
'failed' but never flipped Job.status back to 'succeeded', deadlocking
job_c31bd38126fd47ed8c2d3c1749c15ccf in 'archiving' for 7 days (no
stale_reaper pass matched; admin_api.create_backup requires 'succeeded').

Contract under test:
  - flips Job 'archiving' → 'succeeded' + marks 'uploading' BRs 'failed'
  - idempotent (second call is a no-op success)
  - refuses when the per-job advisory lock is held (live executor)
  - refuses post-COMMIT-POINT states (BR 'uploaded' at current gen)
  - refuses non-eligible Job statuses ('restoring', 'running', ...)
  - never touches terminal BRs ('failed' / 'restored' / 'deleted') or
    stale-generation 'uploaded' history
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.pan_fixtures import (  # noqa: F401
    insert_sample_backup_record,
    insert_sample_job,
    pan_test_engine,
    run_async,
)


async def _read_state(engine, job_id, br_ids=()):
    """Snapshot Job.status + {br_id: (status, error_message, completed_at)}."""
    from models import BackupRecord, Job

    Session = async_sessionmaker(engine, class_=AsyncSession,
                                 expire_on_commit=False)
    async with Session() as db:
        job_status = (await db.execute(
            select(Job.status).where(Job.job_id == job_id)
        )).scalar_one()
        brs = {}
        for br_id in br_ids:
            row = (await db.execute(
                select(
                    BackupRecord.status, BackupRecord.error_message,
                    BackupRecord.completed_at,
                ).where(BackupRecord.id == br_id)
            )).one()
            brs[br_id] = row
    return job_status, brs


def test_rollback_flips_archiving_job_and_fails_uploading_record(
    tmp_path, monkeypatch,
):
    """Happy path: Job 'archiving' + BR 'uploading' → Job 'succeeded',
    BR 'failed' with reason + completed_at. JSON mirror follows (PG +
    JSON in lockstep via set_archive_status)."""
    from pan.status_mutator import rollback_archive_attempt

    user_id = uuid.uuid4()
    job_id = 'abort_me'
    monkeypatch.setenv('AIVIDEOTRANS_JOBS_DIR', str(tmp_path))
    json_path = tmp_path / f'{job_id}.json'
    json_path.write_text(json.dumps({
        'job_id': job_id, 'status': 'archiving',
    }), encoding='utf-8')

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving', edit_generation=0,
            )
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id,
                status='uploading', job_edit_generation=0,
            )

            async with engine.connect() as conn:
                summary = await rollback_archive_attempt(
                    user_id, job_id, conn=conn,
                    reason='deploy abort: AVT_PAN_UPLOAD_CHUNK_BYTES bump',
                )

            assert summary['changed'] is True
            assert summary['status_before'] == 'archiving'
            assert summary['status_after'] == 'succeeded'
            assert summary['backup_records_failed'] == 1

            job_status, brs = await _read_state(engine, job_id, [br['id']])
            assert job_status == 'succeeded'
            br_row = brs[br['id']]
            assert br_row.status == 'failed'
            assert 'deploy abort' in (br_row.error_message or '')
            assert br_row.completed_at is not None

    run_async(_go())

    # JSON mirror flipped alongside PG.
    record = json.loads(json_path.read_text(encoding='utf-8'))
    assert record['status'] == 'succeeded'


def test_rollback_is_idempotent(tmp_path, monkeypatch):
    """Second call after a successful rollback is a no-op success —
    exactly what a runbook retry / double-fire needs."""
    from pan.status_mutator import rollback_archive_attempt

    user_id = uuid.uuid4()
    job_id = 'twice'
    monkeypatch.setenv('AIVIDEOTRANS_JOBS_DIR', str(tmp_path))

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving', edit_generation=0,
            )
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id,
                status='uploading', job_edit_generation=0,
            )

            async with engine.connect() as conn:
                first = await rollback_archive_attempt(
                    user_id, job_id, conn=conn,
                )
                second = await rollback_archive_attempt(
                    user_id, job_id, conn=conn,
                )

            assert first['changed'] is True
            assert second['changed'] is False
            assert second['status_before'] == 'succeeded'
            assert second['backup_records_failed'] == 0

            job_status, brs = await _read_state(engine, job_id, [br['id']])
            assert job_status == 'succeeded'
            assert brs[br['id']].status == 'failed'

    run_async(_go())


def test_rollback_noop_when_job_already_succeeded(tmp_path, monkeypatch):
    """Job already 'succeeded' with no in-flight BRs → no-op, no raise.
    Covers the half-recovered incident shape (ops flipped the Job by hand
    earlier, then standardizes on this entry point)."""
    from pan.status_mutator import rollback_archive_attempt

    user_id = uuid.uuid4()
    job_id = 'already_ok'
    monkeypatch.setenv('AIVIDEOTRANS_JOBS_DIR', str(tmp_path))

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='succeeded', edit_generation=0,
            )
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id,
                status='failed', job_edit_generation=0,
            )

            async with engine.connect() as conn:
                summary = await rollback_archive_attempt(
                    user_id, job_id, conn=conn,
                )

            assert summary['changed'] is False
            assert summary['backup_records_failed'] == 0

            job_status, brs = await _read_state(engine, job_id, [br['id']])
            assert job_status == 'succeeded'
            assert brs[br['id']].status == 'failed'

    run_async(_go())


def test_rollback_refuses_post_commit_point(tmp_path, monkeypatch):
    """BR 'uploaded' at the CURRENT generation + Job 'archiving' → the
    COMMIT POINT passed; local data may already be rmtree'd. Rolling back
    to 'succeeded' would lie about local availability — that state is
    owned by stale_reaper Pass 2 + residue_cleanup ('archived' flip).
    Nothing may be modified."""
    from pan.status_mutator import rollback_archive_attempt

    user_id = uuid.uuid4()
    job_id = 'post_commit'
    monkeypatch.setenv('AIVIDEOTRANS_JOBS_DIR', str(tmp_path))

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving', edit_generation=0,
            )
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id,
                status='uploaded', job_edit_generation=0,
            )

            async with engine.connect() as conn:
                with pytest.raises(RuntimeError, match='COMMIT POINT'):
                    await rollback_archive_attempt(
                        user_id, job_id, conn=conn,
                    )

            job_status, brs = await _read_state(engine, job_id, [br['id']])
            assert job_status == 'archiving'
            assert brs[br['id']].status == 'uploaded'

    run_async(_go())


def test_rollback_ignores_stale_generation_uploaded_history(
    tmp_path, monkeypatch,
):
    """An 'uploaded' BR at an OLD generation is restore history, not a
    commit point for the current attempt — it must neither block the
    rollback nor be touched by it."""
    from pan.status_mutator import rollback_archive_attempt

    user_id = uuid.uuid4()
    job_id = 'gen_history'
    monkeypatch.setenv('AIVIDEOTRANS_JOBS_DIR', str(tmp_path))

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving', edit_generation=2,
            )
            br_old = await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id,
                status='uploaded', job_edit_generation=0,
            )
            br_cur = await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id,
                status='uploading', job_edit_generation=2,
            )

            async with engine.connect() as conn:
                summary = await rollback_archive_attempt(
                    user_id, job_id, conn=conn,
                )

            assert summary['changed'] is True
            assert summary['backup_records_failed'] == 1

            job_status, brs = await _read_state(
                engine, job_id, [br_old['id'], br_cur['id']],
            )
            assert job_status == 'succeeded'
            assert brs[br_old['id']].status == 'uploaded'  # untouched
            assert brs[br_cur['id']].status == 'failed'

    run_async(_go())


def test_rollback_refuses_ineligible_job_statuses(tmp_path, monkeypatch):
    """'restoring' is a restore-flow state (stale_reaper Pass 1 owns it,
    with its project_dir-exists forward/rollback split); 'running' /
    'archived' are not archive attempts at all. All must be refused
    without modification."""
    from pan.status_mutator import rollback_archive_attempt

    user_id = uuid.uuid4()
    monkeypatch.setenv('AIVIDEOTRANS_JOBS_DIR', str(tmp_path))

    async def _go():
        async with pan_test_engine() as engine:
            for status in ('restoring', 'running', 'archived'):
                job_id = f'wrong_{status}'
                await insert_sample_job(
                    engine, user_id=user_id, job_id=job_id, status=status,
                )
                async with engine.connect() as conn:
                    with pytest.raises(
                        RuntimeError, match='not rollback-eligible',
                    ):
                        await rollback_archive_attempt(
                            user_id, job_id, conn=conn,
                        )
                job_status, _ = await _read_state(engine, job_id)
                assert job_status == status

    run_async(_go())


def test_rollback_raises_when_job_missing(tmp_path, monkeypatch):
    from pan.status_mutator import rollback_archive_attempt

    monkeypatch.setenv('AIVIDEOTRANS_JOBS_DIR', str(tmp_path))

    async def _go():
        async with pan_test_engine() as engine:
            async with engine.connect() as conn:
                with pytest.raises(RuntimeError, match='no Job matched'):
                    await rollback_archive_attempt(
                        uuid.uuid4(), 'ghost', conn=conn,
                    )

    run_async(_go())


def test_rollback_refuses_when_advisory_lock_held(tmp_path, monkeypatch):
    """The lock probe is the no-bypass guarantee: a live executor holds
    pan_lock_key(user, job), so rollback must refuse and modify nothing.
    SQLite can't simulate a held PG advisory lock — monkeypatch the
    probe, exactly like the stale_reaper lock tests."""
    from pan import status_mutator as sm

    user_id = uuid.uuid4()
    job_id = 'executor_alive'
    monkeypatch.setenv('AIVIDEOTRANS_JOBS_DIR', str(tmp_path))

    async def deny_lock(conn, key):
        return False

    monkeypatch.setattr(sm, '_try_advisory_lock', deny_lock)

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving', edit_generation=0,
            )
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id,
                status='uploading', job_edit_generation=0,
            )

            async with engine.connect() as conn:
                with pytest.raises(RuntimeError, match='advisory lock held'):
                    await sm.rollback_archive_attempt(
                        user_id, job_id, conn=conn,
                    )

            job_status, brs = await _read_state(engine, job_id, [br['id']])
            assert job_status == 'archiving'
            assert brs[br['id']].status == 'uploading'

    run_async(_go())


def test_rollback_leaves_terminal_records_untouched(tmp_path, monkeypatch):
    """Only 'uploading' BRs are failed; 'restored' / 'deleted' / 'failed'
    are terminal and stay as-is."""
    from pan.status_mutator import rollback_archive_attempt

    user_id = uuid.uuid4()
    job_id = 'mixed_brs'
    monkeypatch.setenv('AIVIDEOTRANS_JOBS_DIR', str(tmp_path))

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving', edit_generation=1,
            )
            brs_in = {}
            for status, gen in (
                ('restored', 0), ('deleted', 0), ('failed', 1),
                ('uploading', 1),
            ):
                row = await insert_sample_backup_record(
                    engine, user_id=user_id, job_id=job_id,
                    status=status, job_edit_generation=gen,
                )
                brs_in[status] = row['id']

            async with engine.connect() as conn:
                summary = await rollback_archive_attempt(
                    user_id, job_id, conn=conn,
                )

            assert summary['backup_records_failed'] == 1

            job_status, brs = await _read_state(
                engine, job_id, list(brs_in.values()),
            )
            assert job_status == 'succeeded'
            assert brs[brs_in['restored']].status == 'restored'
            assert brs[brs_in['deleted']].status == 'deleted'
            assert brs[brs_in['failed']].status == 'failed'
            assert brs[brs_in['uploading']].status == 'failed'

    run_async(_go())
