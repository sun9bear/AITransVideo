"""Integration tests for executor → JSONL emit (Phase 9 §T9.4 + CodeX P2).

The helper-only tests in tests/test_pan_events_emit.py prove that
``emit_pan_event_safe`` works in isolation. These tests prove the
EXECUTORS actually call it at the right state transitions.

Why these matter: refactoring an executor (e.g. moving the
commit-point earlier) could accidentally delete the emit call without
breaking any existing tests — observability would silently disappear.
Each test below executes a real path through one of the four pan
executors and asserts the right JSONL events landed in
``{settings.jobs_dir}/{job_id}.events.jsonl``.

CodeX 2026-05-19 P2 ask: three minimum scenarios — backup happy path,
restore failure, residue cleanup finalize. We add a fourth for
backup failure (mirror of restore failure) for symmetry, plus a
notification-dispatch test for the new P1d wiring.
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


def _read_events(jobs_dir: Path, job_id: str) -> list[dict]:
    """Read all events for a job from its JSONL file."""
    path = jobs_dir / f"{job_id}.events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _patch_jobs_dir(monkeypatch, tmp_path: Path) -> Path:
    """Point ``settings.jobs_dir`` at a temp directory and return it.

    The executors call into ``emit_download_event`` which writes to
    ``settings.jobs_dir``. Tests need a writable / isolated location.
    """
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    import config
    monkeypatch.setattr(
        config.settings, "jobs_dir", str(jobs_dir), raising=False,
    )
    return jobs_dir


def _noop_rmtree(p):
    pass


def _noop_r2_delete(k):
    pass


# =========================================================================
# Backup happy path → started + succeeded events
# =========================================================================


def test_backup_happy_path_emits_started_and_succeeded(monkeypatch, tmp_path):
    """End-to-end: a successful backup writes pan.backup.started +
    pan.backup.succeeded to the per-job JSONL file. Without these,
    r2_observability would show 0 backups even when real ones ran."""
    setup_pan_token_env(monkeypatch)
    jobs_dir = _patch_jobs_dir(monkeypatch, tmp_path)

    user_id = uuid.uuid4()
    job_id = "job_emit_happy"

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project), edit_generation=3,
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            from pan.backup_executor import _execute_pan_backup_impl
            await _execute_pan_backup_impl(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client_factory=lambda: client,
                rmtree_fn=_noop_rmtree, r2_delete_fn=_noop_r2_delete,
                heartbeat_enabled=False,
            )

            events = _read_events(jobs_dir, job_id)
            types = [e['event_type'] for e in events]
            assert 'pan.backup.started' in types, (
                f"backup must emit pan.backup.started; got {types}"
            )
            assert 'pan.backup.succeeded' in types, (
                f"backup must emit pan.backup.succeeded; got {types}"
            )
            # Order: started before succeeded.
            assert types.index('pan.backup.started') < types.index('pan.backup.succeeded')

            # All pan events must have stage='pan'.
            pan_events = [e for e in events if e['event_type'].startswith('pan.')]
            assert all(e['stage'] == 'pan' for e in pan_events)

            # succeeded payload must carry size + sha256 for ops triage.
            succ = next(e for e in events if e['event_type'] == 'pan.backup.succeeded')
            assert 'sha256' in succ['payload']
            assert succ['payload']['size_bytes'] > 0

    run_async(_go())


# =========================================================================
# Backup failure path → failed event + dispatch notification
# =========================================================================


def test_backup_failure_emits_failed_and_dispatches_notification(
    monkeypatch, tmp_path,
):
    """Backup that fails before the commit point must:
      1. Write pan.backup.failed JSONL with the error in reason.
      2. Insert a UserNotification row via dispatch_pan_failure_notification
         (CodeX P1d — the recipe was previously dead config).
    """
    from models import UserNotification

    setup_pan_token_env(monkeypatch)
    jobs_dir = _patch_jobs_dir(monkeypatch, tmp_path)

    user_id = uuid.uuid4()
    job_id = "job_emit_fail"

    # Force upload to fail.
    class FailingClient(FakeBaiduPanClient):
        def upload(self, *args, **kwargs):
            raise RuntimeError("synthetic upload boom")

    async def _go():
        async with pan_test_engine() as engine:
            # Add UserNotification table for dispatch_event to write to.
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda c: UserNotification.__table__.create(c, checkfirst=True),
                )

            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            # CodeX 2026-05-19 P2a: insert with BOTH source title and
            # user-editable display_name so the test verifies the
            # resolver picks display_name (the user-visible one).
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
                title='Original Source Video Title',
                display_name='我的中文配音任务',
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            from pan.backup_executor import _execute_pan_backup_impl
            with pytest.raises(RuntimeError, match='upload boom'):
                await _execute_pan_backup_impl(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client_factory=lambda: FailingClient(),
                    rmtree_fn=_noop_rmtree, r2_delete_fn=_noop_r2_delete,
                    heartbeat_enabled=False,
                )

            # 1. JSONL emit.
            events = _read_events(jobs_dir, job_id)
            failed = [e for e in events if e['event_type'] == 'pan.backup.failed']
            assert len(failed) == 1, (
                f"expected exactly 1 pan.backup.failed event, got {events}"
            )
            assert 'upload boom' in failed[0]['payload']['reason']
            assert failed[0]['level'] == 'error'

            # 2. UserNotification row (CodeX P1d).
            from sqlalchemy.ext.asyncio import (
                AsyncSession, async_sessionmaker,
            )
            Session = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False,
            )
            async with Session() as db:
                notifs = (await db.execute(
                    select(UserNotification)
                    .where(UserNotification.user_id == user_id)
                )).scalars().all()
            assert len(notifs) == 1, (
                f"expected exactly 1 user_notifications row, got {len(notifs)}"
            )
            n = notifs[0]
            assert n.severity == 'error'
            assert 'upload boom' in n.body
            assert '{reason}' not in n.body
            assert '{display_name}' not in n.body
            # CodeX 2026-05-19 P2a: notification body must show the
            # user-editable display_name (not Job.title or job_id).
            assert '我的中文配音任务' in n.body, (
                f"expected display_name in body, got: {n.body!r}"
            )
            # Source title must NOT leak through when display_name is set.
            assert 'Original Source Video Title' not in n.body

    run_async(_go())


def test_failure_notification_falls_back_to_title_when_display_name_empty(
    monkeypatch, tmp_path,
):
    """CodeX 2026-05-19 P2a: if Job.display_name is NULL, the fallback
    chain uses Job.title. Only when BOTH are empty does it fall back
    to job_id."""
    from models import UserNotification

    setup_pan_token_env(monkeypatch)
    _patch_jobs_dir(monkeypatch, tmp_path)

    user_id = uuid.uuid4()
    job_id = "job_fallback_title"

    class FailingClient(FakeBaiduPanClient):
        def upload(self, *args, **kwargs):
            raise RuntimeError("forced fail for fallback test")

    async def _go():
        async with pan_test_engine() as engine:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda c: UserNotification.__table__.create(c, checkfirst=True),
                )

            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            # Only title, no display_name.
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
                title='YouTube Source Video Title',
                # display_name omitted → NULL.
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            from pan.backup_executor import _execute_pan_backup_impl
            with pytest.raises(RuntimeError):
                await _execute_pan_backup_impl(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client_factory=lambda: FailingClient(),
                    rmtree_fn=_noop_rmtree, r2_delete_fn=_noop_r2_delete,
                    heartbeat_enabled=False,
                )

            from sqlalchemy.ext.asyncio import (
                AsyncSession, async_sessionmaker,
            )
            Session = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False,
            )
            async with Session() as db:
                notifs = (await db.execute(
                    select(UserNotification)
                    .where(UserNotification.user_id == user_id)
                )).scalars().all()
            assert len(notifs) == 1
            # title is the fallback when display_name is NULL.
            assert 'YouTube Source Video Title' in notifs[0].body

    run_async(_go())


# =========================================================================
# Restore failure path → failed event + dispatch notification
# =========================================================================


def test_restore_failure_emits_failed_event(monkeypatch, tmp_path):
    """Restore that fails (e.g. sha256 mismatch on download) must emit
    pan.restore.failed and dispatch the corresponding notification."""
    from models import UserNotification

    setup_pan_token_env(monkeypatch)
    jobs_dir = _patch_jobs_dir(monkeypatch, tmp_path)
    # Register tmp_path as a safe project root so verify_project_dir_safe
    # accepts our restore target. (gateway.project_cleanup safe-roots
    # whitelist — see pan_fixtures.make_project_dir for the same trick.)
    monkeypatch.setenv('AIVIDEOTRANS_PROJECTS_DIR', str(tmp_path))

    user_id = uuid.uuid4()
    job_id = "job_emit_restore_fail"

    # Force download to return wrong sha256.
    class CorruptClient(FakeBaiduPanClient):
        def download(self, remote_path, local_path, *, access_token):
            super().download(
                remote_path, local_path, access_token=access_token,
            )
            # Override sha256 in the result so executor sees mismatch.
            return {'sha256': 'deadbeef' * 8}

    async def _go():
        async with pan_test_engine() as engine:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda c: UserNotification.__table__.create(c, checkfirst=True),
                )

            # Setup: archived job with an uploaded BackupRecord.
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archived', edit_generation=0,
                project_dir=str(tmp_path / 'restore_target'),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id,
                status='uploaded', job_edit_generation=0,
                remote_path='/apps/AIVideoTrans/backups/test.tar.gz',
                sha256='a' * 64,
            )

            # Pre-stage some bytes at the remote so download has something
            # to fetch — the CorruptClient overrides sha in the return.
            client = CorruptClient()
            client._storage[
                '/apps/AIVideoTrans/backups/test.tar.gz'
            ] = b'fake tar bytes'

            from pan.restore_executor import _execute_pan_restore_impl
            with pytest.raises(RuntimeError, match='sha256 mismatch'):
                await _execute_pan_restore_impl(
                    {
                        'job_id': job_id, 'user_id': str(user_id),
                        'backup_id': str(br['id']),
                    },
                    engine=engine, client_factory=lambda: client,
                    heartbeat_enabled=False,
                )

            events = _read_events(jobs_dir, job_id)
            types = [e['event_type'] for e in events]
            # started before failed.
            assert 'pan.restore.started' in types
            assert 'pan.restore.failed' in types
            failed = next(e for e in events if e['event_type'] == 'pan.restore.failed')
            assert 'sha256 mismatch' in failed['payload']['reason']
            assert failed['level'] == 'error'
            # moved=False because failure was pre-move.
            assert failed['payload']['moved'] is False

            # Notification row landed (CodeX P1d).
            from sqlalchemy.ext.asyncio import (
                AsyncSession, async_sessionmaker,
            )
            Session = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False,
            )
            async with Session() as db:
                notifs = (await db.execute(
                    select(UserNotification)
                    .where(UserNotification.user_id == user_id)
                )).scalars().all()
            assert len(notifs) == 1
            assert 'sha256 mismatch' in notifs[0].body
            assert notifs[0].severity == 'error'

    run_async(_go())


# =========================================================================
# Residue cleanup finalize → completed event (CodeX 2026-05-19 P2b)
# =========================================================================


def test_residue_cleanup_finalize_emits_completed(monkeypatch, tmp_path):
    """Residue cleanup successfully forward-resolving a stuck 'archiving'
    job to 'archived' must emit pan.residue_cleanup.completed JSONL.

    Coverage gap fixed in CodeX 2026-05-19 P2b: the file docstring
    promised this scenario but it wasn't actually implemented.
    """
    from models import Job

    setup_pan_token_env(monkeypatch)
    jobs_dir = _patch_jobs_dir(monkeypatch, tmp_path)

    user_id = uuid.uuid4()
    job_id = "job_residue_finalize"

    rmtree_calls: list[Path] = []
    r2_deleted: list[str] = []

    def rec_rmtree(p):
        rmtree_calls.append(Path(p))

    def rec_r2(k):
        r2_deleted.append(k)

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(
                tmp_path, job_id=job_id, monkeypatch=monkeypatch,
            )
            # Stuck state: Job 'archiving' + BackupRecord 'uploaded' + an
            # r2_artifacts entry to verify cleanup hits the R2 path too.
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving',
                project_dir=str(project),
                r2_artifacts=[
                    {'artifact_key': 'publish.dubbed_video',
                     'r2_key': f'jobs/{job_id}/v.mp4'},
                ],
            )
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id, status='uploaded',
            )

            from pan.residue_cleanup import _execute_pan_residue_cleanup_impl
            await _execute_pan_residue_cleanup_impl(
                {
                    'job_id': job_id,
                    'user_id': str(user_id),
                    'backup_id': str(br['id']),
                },
                engine=engine,
                rmtree_fn=rec_rmtree,
                r2_delete_fn=rec_r2,
            )

            # Cleanup ran.
            assert rmtree_calls == [project]
            assert r2_deleted == [f'jobs/{job_id}/v.mp4']

            # DB finalized.
            async with engine.connect() as conn:
                row = (await conn.execute(
                    select(Job.status, Job.r2_artifacts)
                    .where(Job.job_id == job_id)
                )).one()
            assert row.status == 'archived'
            assert row.r2_artifacts is None

            # JSONL emit landed.
            events = _read_events(jobs_dir, job_id)
            types = [e['event_type'] for e in events]
            assert 'pan.residue_cleanup.completed' in types, (
                f"residue cleanup must emit pan.residue_cleanup.completed; "
                f"got {types}"
            )
            completed = next(
                e for e in events
                if e['event_type'] == 'pan.residue_cleanup.completed'
            )
            assert completed['stage'] == 'pan'
            assert completed['payload']['backup_id'] == str(br['id'])

    run_async(_go())


def test_residue_cleanup_partial_failure_skips_completed_emit(
    monkeypatch, tmp_path,
):
    """If rmtree fails partway, the finalize branch is skipped and
    pan.residue_cleanup.completed must NOT emit (the cleanup didn't
    actually complete — next stale_reaper pass retries).
    """
    setup_pan_token_env(monkeypatch)
    jobs_dir = _patch_jobs_dir(monkeypatch, tmp_path)

    user_id = uuid.uuid4()
    job_id = "job_residue_partial"

    def failing_rmtree(p):
        raise OSError("simulated permission denied")

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(
                tmp_path, job_id=job_id, monkeypatch=monkeypatch,
            )
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='archiving', project_dir=str(project),
            )
            br = await insert_sample_backup_record(
                engine, user_id=user_id, job_id=job_id, status='uploaded',
            )

            from pan.residue_cleanup import _execute_pan_residue_cleanup_impl
            await _execute_pan_residue_cleanup_impl(
                {
                    'job_id': job_id,
                    'user_id': str(user_id),
                    'backup_id': str(br['id']),
                },
                engine=engine,
                rmtree_fn=failing_rmtree,
                r2_delete_fn=lambda k: None,
            )

            # Cleanup did NOT finalize. No completed emit.
            events = _read_events(jobs_dir, job_id)
            types = [e['event_type'] for e in events]
            assert 'pan.residue_cleanup.completed' not in types, (
                f"completed event must NOT fire when rmtree failed; "
                f"got {types}"
            )

    run_async(_go())
