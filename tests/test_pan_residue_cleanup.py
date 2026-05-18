"""Tests for gateway.pan.residue_cleanup.

Plan §10 + Phase 5b T5.12. residue_cleanup forward-resolves backups
stuck in 'archiving' state (COMMIT POINT passed but post-commit cleanup
crashed). Called by pan_stale_reaper. Must be idempotent.

T5.11.6 background_tasks-table-reuse contract guards are at the bottom.
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.pan_fixtures import (
    insert_sample_backup_record,
    insert_sample_job,
    insert_sample_pan_credentials,
    make_project_dir,
    pan_test_engine,
    run_async,
    setup_pan_token_env,
)


def _noop_rmtree(path):
    pass


def _noop_r2_delete(key):
    pass


async def _run_cleanup(
    payload, *, engine,
    rmtree_fn=_noop_rmtree, r2_delete_fn=_noop_r2_delete,
):
    from gateway.pan.residue_cleanup import _execute_pan_residue_cleanup_impl
    await _execute_pan_residue_cleanup_impl(
        payload, engine=engine, rmtree_fn=rmtree_fn, r2_delete_fn=r2_delete_fn,
    )


# =========================================================================
# Happy path: forward-resolve stuck archiving
# =========================================================================


def test_cleanup_forward_resolves_stuck_archiving(monkeypatch, tmp_path):
    """Job stuck at 'archiving' with 'uploaded' BackupRecord — cleanup
    flips it to 'archived', clears r2_artifacts, calls rmtree + R2 delete."""
    from models import Job

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_stuck'

    rmtree_calls: list[Path] = []
    r2_deleted: list[str] = []

    def rec_rmtree(p):
        rmtree_calls.append(Path(p))

    def rec_r2(k):
        r2_deleted.append(k)

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving',  # ← stuck
                project_dir=str(project),
                r2_artifacts=[
                    {'artifact_key': 'publish.dubbed_video',
                     'r2_key': f'jobs/{job_id}/v.mp4'},
                ],
            )
            await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id, status='uploaded',
            )

            await _run_cleanup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine,
                rmtree_fn=rec_rmtree, r2_delete_fn=rec_r2,
            )

            assert len(rmtree_calls) == 1
            assert rmtree_calls[0] == project
            assert r2_deleted == [f'jobs/{job_id}/v.mp4']

            async with engine.connect() as conn:
                row = (await conn.execute(
                    select(Job.status, Job.r2_artifacts)
                    .where(Job.job_id == job_id)
                )).one()
            assert row.status == 'archived'
            assert row.r2_artifacts is None

    run_async(_go())


# =========================================================================
# State preconditions — no-op cases
# =========================================================================


def test_cleanup_noop_when_job_already_archived(monkeypatch, tmp_path):
    """Job.status already 'archived' (real executor finished) — cleanup
    is no-op. No rmtree, no R2 delete, no status flip."""
    from models import Job

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_done'

    rmtree_calls = []
    r2_deleted = []

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archived',
                project_dir=str(tmp_path / job_id),
            )
            await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id, status='uploaded',
            )

            await _run_cleanup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine,
                rmtree_fn=lambda p: rmtree_calls.append(p),
                r2_delete_fn=lambda k: r2_deleted.append(k),
            )

            assert rmtree_calls == []
            assert r2_deleted == []

            async with engine.connect() as conn:
                status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
            assert status == 'archived'  # unchanged

    run_async(_go())


def test_cleanup_noop_when_no_uploaded_backup_record(monkeypatch, tmp_path):
    """If BackupRecord.status is NOT 'uploaded' (e.g. 'uploading' — never
    hit COMMIT POINT), residue cleanup is NOT the right tool. No-op."""
    from models import Job

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_no_commit'

    rmtree_calls = []
    r2_deleted = []

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving', project_dir=str(project),
            )
            await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id, status='uploading',
            )

            await _run_cleanup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine,
                rmtree_fn=lambda p: rmtree_calls.append(p),
                r2_delete_fn=lambda k: r2_deleted.append(k),
            )

            assert rmtree_calls == [], 'cleanup must not touch pre-commit residue'
            assert r2_deleted == []

            async with engine.connect() as conn:
                status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
            assert status == 'archiving'  # untouched

    run_async(_go())


def test_cleanup_noop_when_job_missing(monkeypatch):
    """Job row doesn't exist (deleted between reaper detection and cleanup)
    — gracefully no-op, no exception."""
    setup_pan_token_env(monkeypatch)

    async def _go():
        async with pan_test_engine() as engine:
            await _run_cleanup(
                {'job_id': 'ghost_job', 'user_id': str(uuid.uuid4())},
                engine=engine,
            )

    run_async(_go())


# =========================================================================
# Idempotency
# =========================================================================


def test_cleanup_is_idempotent(monkeypatch, tmp_path):
    """Running cleanup twice in a row leaves the second call as no-op
    (first call moved status to 'archived')."""
    from models import Job

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_idem'

    rmtree_calls = []
    r2_deleted = []

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving', project_dir=str(project),
                r2_artifacts=[{'r2_key': 'jobs/x/v.mp4'}],
            )
            await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id, status='uploaded',
            )

            payload = {'job_id': job_id, 'user_id': str(user_id)}
            await _run_cleanup(
                payload, engine=engine,
                rmtree_fn=lambda p: rmtree_calls.append(p),
                r2_delete_fn=lambda k: r2_deleted.append(k),
            )
            # First call did the work.
            assert len(rmtree_calls) == 1
            assert len(r2_deleted) == 1

            # Second call: no-op.
            await _run_cleanup(
                payload, engine=engine,
                rmtree_fn=lambda p: rmtree_calls.append(p),
                r2_delete_fn=lambda k: r2_deleted.append(k),
            )
            assert len(rmtree_calls) == 1, "second call should not rmtree again"
            assert len(r2_deleted) == 1, "second call should not r2-delete again"

            async with engine.connect() as conn:
                status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
            assert status == 'archived'

    run_async(_go())


# =========================================================================
# Error tolerance
# =========================================================================


def test_cleanup_rmtree_failure_keeps_archiving_for_next_pass(monkeypatch, tmp_path):
    """CodeX P0-3: rmtree failure → log + leave Job at 'archiving' +
    r2_artifacts INTACT. Old behavior (set 'archived' anyway) destroyed
    the link the next stale_reaper pass needs."""
    from models import Job

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_rmtree_err'

    def failing_rmtree(path):
        raise OSError('synthetic permission denied')

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving', project_dir=str(project),
                r2_artifacts=[{'r2_key': 'jobs/x/k.mp4'}],
            )
            await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id, status='uploaded',
            )

            await _run_cleanup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, rmtree_fn=failing_rmtree,
            )

            async with engine.connect() as conn:
                row = (await conn.execute(
                    select(Job.status, Job.r2_artifacts)
                    .where(Job.job_id == job_id)
                )).one()
            assert row.status == 'archiving'
            # r2_artifacts MUST stay intact so we can find the keys
            # on the next pass.
            artifacts_out = row.r2_artifacts
            if isinstance(artifacts_out, str):
                import json as _json
                artifacts_out = _json.loads(artifacts_out)
            assert artifacts_out == [{'r2_key': 'jobs/x/k.mp4'}]

    run_async(_go())


def test_cleanup_r2_delete_failure_keeps_archiving_for_next_pass(monkeypatch, tmp_path):
    """CodeX P0-3: partial R2 delete failure → log + leave at 'archiving'
    so next pass retries the failed keys."""
    from models import Job

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_r2_err'

    failed_keys = ['jobs/x/fail.mp4']

    def selective_r2_delete(key):
        if key in failed_keys:
            raise RuntimeError(f'synthetic R2 error for {key}')

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            r2_artifacts = [
                {'r2_key': 'jobs/x/ok.mp4'},
                {'r2_key': 'jobs/x/fail.mp4'},
            ]
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving', project_dir=str(project),
                r2_artifacts=r2_artifacts,
            )
            await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id, status='uploaded',
            )

            await _run_cleanup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, r2_delete_fn=selective_r2_delete,
            )

            async with engine.connect() as conn:
                row = (await conn.execute(
                    select(Job.status, Job.r2_artifacts)
                    .where(Job.job_id == job_id)
                )).one()
            assert row.status == 'archiving'
            # Critical: r2_artifacts still has ALL entries (including the
            # one that succeeded) so next pass tries them again. R2 delete
            # is idempotent, so retrying the already-deleted one is OK.
            artifacts_out = row.r2_artifacts
            if isinstance(artifacts_out, str):
                import json as _json
                artifacts_out = _json.loads(artifacts_out)
            assert artifacts_out == r2_artifacts

    run_async(_go())


def test_cleanup_handles_missing_project_dir_on_disk(monkeypatch, tmp_path):
    """If project_dir was already deleted (crashed AFTER rmtree but BEFORE
    status flip), cleanup must still complete — skip rmtree, set
    status='archived'."""
    from models import Job

    setup_pan_token_env(monkeypatch)
    # CodeX P0: tmp_path must be registered as a safe root so the
    # non-existent path (still under it) passes the safety check.
    monkeypatch.setenv('AIVIDEOTRANS_PROJECTS_DIR', str(tmp_path))
    user_id = uuid.uuid4()
    job_id = 'job_disk_gone'

    rmtree_calls = []

    async def _go():
        async with pan_test_engine() as engine:
            # project_dir points to a path that doesn't exist on disk.
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving',
                project_dir=str(tmp_path / 'never_existed'),
            )
            await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id, status='uploaded',
            )

            await _run_cleanup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine,
                rmtree_fn=lambda p: rmtree_calls.append(p),
            )

            assert rmtree_calls == [], 'rmtree must not be called on a non-existent path'
            async with engine.connect() as conn:
                status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
            assert status == 'archived'

    run_async(_go())


# =========================================================================
# T5.11.6 — background_tasks vs backup_records contract
# =========================================================================


def test_backup_executor_does_not_reference_background_tasks_model():
    """Contract: backup_executor.py source must not import or reference
    BackgroundTask. Scheduling and lifecycle are decoupled — executors
    own BackupRecord, scheduling owns BackgroundTask.

    If a future change makes backup_executor poke at the BackgroundTask
    row, the source-of-truth split breaks and the UI starts seeing
    inconsistent state (failed task + uploaded backup, etc.). Lock it
    here."""
    src = (Path(__file__).resolve().parent.parent /
           'gateway' / 'pan' / 'backup_executor.py')
    text_body = src.read_text(encoding='utf-8')
    # Allow the literal in docstrings/comments — disallow in code.
    code_lines = [
        line for line in text_body.splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    ]
    in_docstring = False
    triple = '"""'
    suspect = []
    for line in code_lines:
        marker = line.count(triple)
        if marker == 1:
            in_docstring = not in_docstring
            continue
        if marker >= 2:
            continue
        if in_docstring:
            continue
        if 'BackgroundTask' in line:
            suspect.append(line)
    assert not suspect, (
        "backup_executor.py code must not reference BackgroundTask. "
        "Source-of-truth split: BackupRecord is authoritative for backup "
        "lifecycle, BackgroundTask is just scheduling. Offending lines:\n"
        + "\n".join(suspect)
    )


def test_restore_executor_does_not_reference_background_tasks_model():
    """Same contract for restore_executor."""
    src = (Path(__file__).resolve().parent.parent /
           'gateway' / 'pan' / 'restore_executor.py')
    text_body = src.read_text(encoding='utf-8')
    code_lines = [
        line for line in text_body.splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    ]
    in_docstring = False
    triple = '"""'
    suspect = []
    for line in code_lines:
        marker = line.count(triple)
        if marker == 1:
            in_docstring = not in_docstring
            continue
        if marker >= 2:
            continue
        if in_docstring:
            continue
        if 'BackgroundTask' in line:
            suspect.append(line)
    assert not suspect, (
        "restore_executor.py code must not reference BackgroundTask:\n"
        + "\n".join(suspect)
    )


def test_cleanup_refuses_rmtree_when_project_dir_outside_safe_root(
    monkeypatch, tmp_path,
):
    """CodeX P0: residue_cleanup MUST refuse rmtree when project_dir is
    not under any safe root, even when AIVIDEOTRANS_PROJECTS_DIR is unset.
    Previously residue_cleanup had no guard at all — a poisoned Job.project_dir
    would have cascaded into arbitrary rmtree."""
    from models import Job

    setup_pan_token_env(monkeypatch)
    # Deliberately do NOT set AIVIDEOTRANS_PROJECTS_DIR. tmp_path is
    # outside /opt/aivideotrans/{data,app}/projects so the safety check
    # fires.
    user_id = uuid.uuid4()
    job_id = 'job_unsafe_path'

    rmtree_calls: list = []

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving',
                project_dir=str(tmp_path / 'rogue_dir'),
            )
            await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id, status='uploaded',
            )

            await _run_cleanup(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine,
                rmtree_fn=lambda p: rmtree_calls.append(p),
            )

            # rmtree refused — no calls.
            assert rmtree_calls == []
            # Status stays at 'archiving' — finalize gated on rmtree_ok.
            async with engine.connect() as conn:
                status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
            assert status == 'archiving'

    run_async(_go())


def test_residue_cleanup_does_not_reference_background_tasks_model():
    """Same contract for residue_cleanup."""
    src = (Path(__file__).resolve().parent.parent /
           'gateway' / 'pan' / 'residue_cleanup.py')
    text_body = src.read_text(encoding='utf-8')
    code_lines = [
        line for line in text_body.splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    ]
    in_docstring = False
    triple = '"""'
    suspect = []
    for line in code_lines:
        marker = line.count(triple)
        if marker == 1:
            in_docstring = not in_docstring
            continue
        if marker >= 2:
            continue
        if in_docstring:
            continue
        if 'BackgroundTask' in line:
            suspect.append(line)
    assert not suspect, (
        "residue_cleanup.py code must not reference BackgroundTask:\n"
        + "\n".join(suspect)
    )
