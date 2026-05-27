"""Tests for gateway.pan.backup_executor.

Plan §7 + Phase 5b T5.2-T5.10. Exercises the full state machine:
precondition → advisory lock → INSERT backup_records → tar build →
upload → 3 gates → COMMIT POINT → rmtree → R2 delete → status='archived'.

All tests run on in-memory SQLite via tests/pan_fixtures.py. FakeBaiduPanClient
substitutes for the real Baidu API. heartbeat_enabled=False everywhere
because we don't want 60s waits in unit tests.
"""
from __future__ import annotations

import json
import tarfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.pan_fixtures import (
    FakeBaiduPanClient,
    insert_sample_job,
    insert_sample_pan_credentials,
    make_project_dir,
    pan_test_engine,
    run_async,
    setup_pan_token_env,
)


def _noop_rmtree(path: Path) -> None:
    """Default rmtree mock for tests that don't care about cleanup."""
    # Don't actually delete — tests may want to inspect the layout after.
    pass


def _noop_r2_delete(key: str) -> None:
    pass


async def _run_executor(
    payload, *, engine, client, rmtree_fn=_noop_rmtree,
    r2_delete_fn=_noop_r2_delete,
):
    """Convenience: call _execute_pan_backup_impl with test defaults."""
    from pan.backup_executor import _execute_pan_backup_impl

    await _execute_pan_backup_impl(
        payload,
        engine=engine,
        client_factory=lambda: client,
        rmtree_fn=rmtree_fn,
        r2_delete_fn=r2_delete_fn,
        heartbeat_enabled=False,
    )


# =========================================================================
# T5.2 — precondition
# =========================================================================


def test_precondition_rejects_non_succeeded_job(monkeypatch):
    """Plan §7 step 0: only succeeded jobs are eligible."""
    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_not_succ'

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id, status='running',
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            with pytest.raises(RuntimeError, match="not 'succeeded'|412"):
                await _run_executor(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                )
            # No upload attempted
            assert client.upload_calls == []

    run_async(_go())


def test_precondition_rejects_missing_credentials(monkeypatch, tmp_path):
    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_no_cred'

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
            )
            # No pan credentials inserted.

            client = FakeBaiduPanClient()
            with pytest.raises(RuntimeError, match='Pan credentials missing'):
                await _run_executor(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                )

    run_async(_go())


def test_precondition_rejects_revoked_credentials(monkeypatch, tmp_path):
    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_revoked'

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
            )
            await insert_sample_pan_credentials(
                engine, user_id=user_id, status='revoked',
            )

            client = FakeBaiduPanClient()
            with pytest.raises(RuntimeError, match="status.*'revoked'|need 'active'"):
                await _run_executor(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                )

    run_async(_go())


def test_precondition_rejects_missing_job(monkeypatch):
    """Job row doesn't exist at all."""
    setup_pan_token_env(monkeypatch)

    async def _go():
        async with pan_test_engine() as engine:
            client = FakeBaiduPanClient()
            with pytest.raises(RuntimeError, match='Job not found'):
                await _run_executor(
                    {'job_id': 'ghost', 'user_id': str(uuid.uuid4())},
                    engine=engine, client=client,
                )

    run_async(_go())


def test_precondition_rejects_missing_project_dir(monkeypatch):
    """Job has status='succeeded' but project_dir is NULL."""
    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_no_dir'

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                # project_dir=None (default)
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            with pytest.raises(RuntimeError, match='no project_dir'):
                await _run_executor(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                )

    run_async(_go())


# =========================================================================
# T5.2 — backup_records INSERT
# =========================================================================


def test_inserts_backup_record_with_uploading_status(monkeypatch, tmp_path):
    """Plan §7 step c: BackupRecord row inserted with status='uploading',
    heartbeat_at populated, job_edit_generation copied from Job."""
    from models import BackupRecord

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_insert_br'

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project), edit_generation=3,
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            await _run_executor(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client,
            )

            async with engine.connect() as conn:
                row = (await conn.execute(
                    select(
                        BackupRecord.status, BackupRecord.job_edit_generation,
                        BackupRecord.heartbeat_at, BackupRecord.user_id,
                        BackupRecord.job_id, BackupRecord.provider,
                    ).where(BackupRecord.job_id == job_id)
                )).one()
            # After full happy path: status='uploaded' (committed)
            assert row.status == 'uploaded'
            assert row.job_edit_generation == 3
            assert row.heartbeat_at is not None
            assert row.user_id == user_id
            assert row.job_id == job_id
            assert row.provider == 'baidu_pan'

    run_async(_go())


# =========================================================================
# T5.5 — three gates
# =========================================================================


def test_gate_size_mismatch_raises_and_rolls_back(monkeypatch, tmp_path):
    """Plan §7 step h1: if server-reported size != local size, raise,
    set BackupRecord.status='failed', Job.status back to 'succeeded'."""
    from models import Job, BackupRecord

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_gate_size'

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            # Wrong size in upload response.
            client.inject_upload_response({
                'size': 1, 'md5': 'whatever', 'fs_id': 1,
            })

            with pytest.raises(RuntimeError, match='Gate 1 \\(size\\)'):
                await _run_executor(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                )

            async with engine.connect() as conn:
                job_status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
                br_status = (await conn.execute(
                    select(BackupRecord.status)
                    .where(BackupRecord.job_id == job_id)
                )).scalar_one()
            assert job_status == 'succeeded'  # rolled back
            assert br_status == 'failed'

    run_async(_go())


def test_gate_md5_skips_when_server_returns_non_hex(monkeypatch, tmp_path):
    """Production 2026-05-19: Baidu sometimes returns a 32-char md5 that
    contains non-hex characters (obfuscated for rapid-upload / data
    security). Gate 2 must treat these as best-effort, NOT raise — the
    layered guarantees (block_list + size + read-back probe) carry safety.
    """
    from models import Job, BackupRecord

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_gate_md5_nonhex'

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            class ObfuscatedMd5Client(FakeBaiduPanClient):
                def upload(self, local_path, remote_path, *, access_token):
                    res = super().upload(local_path, remote_path,
                                         access_token=access_token)
                    # Real production sample (note 'v' at position 11):
                    res['md5'] = '6d3a845fevc7f34602947bd7d978bef1'
                    return res

            # Must NOT raise — obfuscated md5 is logged + skipped.
            await _run_executor(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=ObfuscatedMd5Client(),
            )

            # Backup proceeded to commit (status='uploaded' after the
            # post-Gate-2 path) — job ends at 'archived' assuming rmtree
            # + R2 deletion succeed in this test fixture.
            async with engine.connect() as conn:
                br_status = (await conn.execute(
                    select(BackupRecord.status)
                    .where(BackupRecord.job_id == job_id)
                )).scalar_one()
            assert br_status in ('uploaded', 'failed')  # depends on fixture
            # The point: did NOT raise Gate 2 RuntimeError above.

    run_async(_go())


def test_gate_md5_mismatch_raises_and_rolls_back(monkeypatch, tmp_path):
    """Plan §7 step h2: server md5 != local md5 → raise, rollback."""
    from models import Job, BackupRecord

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_gate_md5'

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            # We need correct size but wrong md5. Easiest: call upload
            # with real bytes (no override) and then patch the *response*
            # md5 alone by injecting AFTER computing real size — but
            # FakeBaiduPanClient inject_upload_response replaces both.
            # Workaround: post-injection — patch the verify_remote_tail
            # to return True (gate 3 OK) and override response to claim
            # wrong md5 + lie about size match.
            # We need size to match real tar size. Easiest: write tar
            # without override and override response with wrong md5 but
            # right size. But we don't know real tar size in advance.
            # Solution: don't override response — wrap the client to
            # alter only md5 by subclassing.
            class WrongMd5Client(FakeBaiduPanClient):
                def upload(self, local_path, remote_path, *, access_token):
                    res = super().upload(local_path, remote_path,
                                         access_token=access_token)
                    # Must be a valid 32-char lowercase hex string to
                    # trigger the strict-comparison branch in Gate 2
                    # (2026-05-19 fix: non-hex server md5 is now treated
                    # as Baidu's obfuscated/rapid-upload variant and
                    # skipped, see backup_executor.py Gate 2 section).
                    res['md5'] = 'a' * 32
                    return res

            wrong = WrongMd5Client()
            with pytest.raises(RuntimeError, match='Gate 2 \\(md5\\)'):
                await _run_executor(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=wrong,
                )

            async with engine.connect() as conn:
                job_status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
                br_status = (await conn.execute(
                    select(BackupRecord.status)
                    .where(BackupRecord.job_id == job_id)
                )).scalar_one()
            assert job_status == 'succeeded'
            assert br_status == 'failed'

    run_async(_go())


def test_gate_read_back_probe_failure_raises_and_rolls_back(monkeypatch, tmp_path):
    """Plan §7 step h3: read-back probe False → raise, rollback. No local
    delete should have happened — this gate's whole point is "refuse to
    delete local when remote state is suspect"."""
    from models import Job, BackupRecord

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_gate_probe'
    rmtree_calls: list[Path] = []

    def recording_rmtree(path: Path) -> None:
        rmtree_calls.append(path)

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            client.inject_verify_result(False)

            with pytest.raises(RuntimeError, match='Gate 3 \\(read-back'):
                await _run_executor(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                    rmtree_fn=recording_rmtree,
                )

            assert rmtree_calls == [], "local rmtree must NOT run when gate fails"

            async with engine.connect() as conn:
                job_status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
                br_status = (await conn.execute(
                    select(BackupRecord.status)
                    .where(BackupRecord.job_id == job_id)
                )).scalar_one()
            assert job_status == 'succeeded'
            assert br_status == 'failed'

    run_async(_go())


# =========================================================================
# T5.6 — COMMIT POINT
# =========================================================================


def test_commit_point_writes_full_metadata(monkeypatch, tmp_path):
    """After COMMIT POINT, BackupRecord row has all final fields populated
    (status='uploaded', remote_path, sha256, md5, size_bytes, manifest_json,
    completed_at)."""
    from models import BackupRecord

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_commit_meta'

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project), edit_generation=1,
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            await _run_executor(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client,
            )

            async with engine.connect() as conn:
                row = (await conn.execute(
                    select(
                        BackupRecord.status, BackupRecord.remote_path,
                        BackupRecord.sha256, BackupRecord.md5,
                        BackupRecord.size_bytes, BackupRecord.manifest_json,
                        BackupRecord.completed_at,
                    ).where(BackupRecord.job_id == job_id)
                )).one()
            assert row.status == 'uploaded'
            assert row.remote_path.startswith('/apps/AIVideoTrans/backups/')
            assert row.remote_path.endswith('.tar.gz')
            assert len(row.sha256) == 64  # hex sha256
            assert len(row.md5) == 32     # hex md5
            assert row.size_bytes > 0
            mj = row.manifest_json
            if isinstance(mj, str):
                mj = json.loads(mj)
            assert mj['backup_format_version'] == 1
            assert mj['job_record']['job_id'] == job_id
            assert row.completed_at is not None

    run_async(_go())


# =========================================================================
# T5.7 — rmtree safety
# =========================================================================


def test_rmtree_safety_refuses_project_dir_outside_projects_root(
    monkeypatch, tmp_path,
):
    """If AIVIDEOTRANS_PROJECTS_DIR is set, executor refuses a project_dir
    that's not inside it. Even though FakeBaiduPanClient happily uploads,
    pre-commit safety check should raise BEFORE INSERT."""
    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_outside'

    # Set projects_root to a SIBLING dir, then put project_dir somewhere else.
    projects_root = tmp_path / 'projects_real'
    projects_root.mkdir()
    monkeypatch.setenv('AIVIDEOTRANS_PROJECTS_DIR', str(projects_root))

    # Project_dir is OUTSIDE projects_root.
    project_outside = make_project_dir(tmp_path / 'outside', job_id=job_id)

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project_outside),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            with pytest.raises(RuntimeError, match='not under any safe root'):
                await _run_executor(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                )
            # Nothing was uploaded — safety check happens pre-upload.
            assert client.upload_calls == []

    run_async(_go())


def test_rmtree_safety_refuses_project_dir_equals_projects_root(
    monkeypatch, tmp_path,
):
    """project_dir == projects_root → refuse (would rm the whole root)."""
    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_eq_root'

    projects_root = tmp_path / 'projects'
    projects_root.mkdir()
    monkeypatch.setenv('AIVIDEOTRANS_PROJECTS_DIR', str(projects_root))

    async def _go():
        async with pan_test_engine() as engine:
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(projects_root),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            with pytest.raises(RuntimeError, match='not under any safe root'):
                await _run_executor(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                )

    run_async(_go())


def test_rmtree_invoked_after_commit_point(monkeypatch, tmp_path):
    """Plan §7 step j: post-COMMIT rmtree gets called on project_dir."""
    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_rmtree_ok'

    rmtree_calls: list[Path] = []

    def recording_rmtree(path: Path) -> None:
        rmtree_calls.append(Path(path))

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            await _run_executor(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client,
                rmtree_fn=recording_rmtree,
            )
            assert len(rmtree_calls) == 1
            assert rmtree_calls[0].resolve() == project.resolve()

    run_async(_go())


# =========================================================================
# T5.8 — R2 cleanup
# =========================================================================


def test_r2_artifacts_deleted_after_commit(monkeypatch, tmp_path):
    """Plan §7 step k: each artifact's r2_key passed to r2_delete_fn."""
    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_r2_delete'

    deleted_keys: list[str] = []

    def recording_r2_delete(key: str) -> None:
        deleted_keys.append(key)

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
                r2_artifacts=[
                    {'artifact_key': 'publish.dubbed_video',
                     'r2_key': f'jobs/{job_id}/publish.dubbed_video.mp4'},
                    {'artifact_key': 'publish.subtitles',
                     'r2_key': f'jobs/{job_id}/publish.subtitles.srt'},
                ],
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            await _run_executor(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client,
                r2_delete_fn=recording_r2_delete,
            )
            assert sorted(deleted_keys) == sorted([
                f'jobs/{job_id}/publish.dubbed_video.mp4',
                f'jobs/{job_id}/publish.subtitles.srt',
            ])

    run_async(_go())


def test_r2_delete_failure_keeps_archiving_for_retry(monkeypatch, tmp_path):
    """CodeX P0-3: post-commit R2 failure → log + leave Job at 'archiving'
    + r2_artifacts INTACT. The old behavior (set status='archived' anyway
    + clear r2_artifacts) destroyed the link residue_cleanup needs to
    find the orphan R2 keys."""
    from models import Job

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_r2_fail'

    def failing_r2_delete(key: str) -> None:
        raise RuntimeError('synthetic R2 failure')

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            r2_artifacts = [
                {'artifact_key': 'publish.dubbed_video',
                 'r2_key': 'jobs/x/v.mp4'},
            ]
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
                r2_artifacts=r2_artifacts,
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            # Should NOT raise (failure is post-commit, log+continue).
            await _run_executor(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client,
                r2_delete_fn=failing_r2_delete,
            )

            async with engine.connect() as conn:
                row = (await conn.execute(
                    select(Job.status, Job.r2_artifacts)
                    .where(Job.job_id == job_id)
                )).one()
            # Status stays 'archiving' for residue_cleanup retry.
            assert row.status == 'archiving'
            # r2_artifacts INTACT — residue_cleanup needs this to find the
            # orphan R2 keys.
            artifacts_out = row.r2_artifacts
            if isinstance(artifacts_out, str):
                import json as _json
                artifacts_out = _json.loads(artifacts_out)
            assert artifacts_out == r2_artifacts

    run_async(_go())


def test_rmtree_failure_keeps_archiving_for_retry(monkeypatch, tmp_path):
    """CodeX P0-3: post-commit rmtree failure → log + leave Job at 'archiving'.
    Same rationale: residue_cleanup needs the chance to retry."""
    from models import Job

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_rmtree_fail'

    def failing_rmtree(path) -> None:
        raise OSError('synthetic permission denied')

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
                r2_artifacts=[{'r2_key': 'k1'}],
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            await _run_executor(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client,
                rmtree_fn=failing_rmtree,
            )

            async with engine.connect() as conn:
                row = (await conn.execute(
                    select(Job.status, Job.r2_artifacts)
                    .where(Job.job_id == job_id)
                )).one()
            assert row.status == 'archiving'
            # r2_artifacts intact (we never even got to step k because
            # we don't bail — but step l doesn't run because rmtree failed).
            # Actually step k DOES still run after rmtree fails. But the
            # FakeBaiduPanClient default r2_delete_fn is _noop_r2_delete,
            # so it succeeded. Only rmtree failed → status stays archiving.
            artifacts_out = row.r2_artifacts
            if isinstance(artifacts_out, str):
                import json as _json
                artifacts_out = _json.loads(artifacts_out)
            assert artifacts_out == [{'r2_key': 'k1'}]

    run_async(_go())


# =========================================================================
# T5.9 — status='archived'
# =========================================================================


def test_archived_status_set_after_success(monkeypatch, tmp_path):
    """Plan §7 step l: Job.status='archived' + r2_artifacts cleared (None)."""
    from models import Job

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_arch'

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
                r2_artifacts=[{'artifact_key': 'x', 'r2_key': 'k1'}],
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            await _run_executor(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client,
            )

            async with engine.connect() as conn:
                row = (await conn.execute(
                    select(Job.status, Job.r2_artifacts)
                    .where(Job.job_id == job_id)
                )).one()
            assert row.status == 'archived'
            assert row.r2_artifacts is None

    run_async(_go())


# =========================================================================
# T5.10 — full happy path integration
# =========================================================================


def test_happy_path_full_integration(monkeypatch, tmp_path):
    """Full pipeline: succeeded → archiving → uploaded → archived. Verify
    all observable side effects: upload called once, verify_remote_tail
    called, rmtree called, R2 keys deleted, status='archived', BackupRecord
    has full metadata, tar was a valid gzip with manifest as first entry."""
    from models import Job, BackupRecord

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_full'

    rmtree_calls = []
    r2_deleted = []

    def rec_rmtree(p):
        rmtree_calls.append(Path(p))

    def rec_r2_delete(k):
        r2_deleted.append(k)

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project), edit_generation=5,
                r2_artifacts=[
                    {'artifact_key': 'publish.dubbed_video',
                     'r2_key': f'jobs/{job_id}/v.mp4'},
                ],
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            await _run_executor(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client,
                rmtree_fn=rec_rmtree, r2_delete_fn=rec_r2_delete,
            )

            # FakeBaiduPanClient interactions.
            assert len(client.upload_calls) == 1
            assert len(client.verify_remote_tail_calls) == 1
            # Sanity: storage actually has the tar bytes — verify it's a
            # gzip tar with manifest as first entry.
            remote_path = client.upload_calls[0]['remote_path']
            tar_bytes = client._storage[remote_path]
            import io as _io
            with tarfile.open(fileobj=_io.BytesIO(tar_bytes), mode='r:gz') as tf:
                names = tf.getnames()
            assert names[0] == 'manifest.json'

            # Side effects.
            assert len(rmtree_calls) == 1
            assert rmtree_calls[0].resolve() == project.resolve()
            assert r2_deleted == [f'jobs/{job_id}/v.mp4']

            # DB state.
            async with engine.connect() as conn:
                job_row = (await conn.execute(
                    select(Job.status, Job.r2_artifacts)
                    .where(Job.job_id == job_id)
                )).one()
                br_row = (await conn.execute(
                    select(
                        BackupRecord.status, BackupRecord.job_edit_generation,
                        BackupRecord.size_bytes, BackupRecord.completed_at,
                    ).where(BackupRecord.job_id == job_id)
                )).one()
            assert job_row.status == 'archived'
            assert job_row.r2_artifacts is None
            assert br_row.status == 'uploaded'
            assert br_row.job_edit_generation == 5
            assert br_row.size_bytes > 0
            assert br_row.completed_at is not None

    run_async(_go())


def test_post_lock_re_read_detects_concurrent_archive(monkeypatch, tmp_path):
    """CodeX P0-2 regression: if Job.status changes from 'succeeded' to
    'archived' WHILE backup_executor is waiting on the advisory lock
    (concurrent worker finished first), the post-lock re-read must
    detect this and refuse — NOT proceed with the stale snapshot.

    Without this guard, the failure path would 'roll back' the
    already-archived Job from 'archived' back to 'succeeded'. The
    instrumented _acquire_advisory_lock here simulates the concurrent
    archive happening between caller-side scheduling and actual lock
    acquisition.
    """
    from pan import backup_executor as be_mod
    from models import Job, BackupRecord

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_toctou'

    real_acquire = be_mod._acquire_advisory_lock
    engine_holder = []

    async def lock_then_mutate(conn, key):
        await real_acquire(conn, key)
        # Simulate "while we waited for the lock, a concurrent worker
        # finished archiving the same job".
        engine_inner = engine_holder[0]
        async with engine_inner.begin() as side_conn:
            await side_conn.execute(
                Job.__table__.update()
                .where(Job.job_id == job_id)
                .values(status='archived')
            )

    monkeypatch.setattr(be_mod, '_acquire_advisory_lock', lock_then_mutate)

    async def _go():
        async with pan_test_engine() as engine:
            engine_holder.append(engine)
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                status='succeeded', project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)

            client = FakeBaiduPanClient()
            # Expect the post-lock re-read to detect 'archived' and raise.
            with pytest.raises(RuntimeError, match="not 'succeeded'|412"):
                await _run_executor(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                )

            # No upload attempted, no BackupRecord row created.
            assert client.upload_calls == []
            async with engine.connect() as conn:
                br_id = (await conn.execute(
                    select(BackupRecord.id).where(BackupRecord.job_id == job_id)
                )).first()
                # Critically: status MUST still be 'archived' — executor
                # did NOT roll it back to 'succeeded' (which would have
                # been the bug pre-fix).
                status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
            assert br_id is None
            assert status == 'archived'

    run_async(_go())


def test_payload_provider_override(monkeypatch, tmp_path):
    """payload['provider'] is honored — credentials lookup uses it."""
    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_provider'

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(tmp_path, job_id=job_id, monkeypatch=monkeypatch)
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
            )
            # Insert credentials with NON-default provider.
            await insert_sample_pan_credentials(
                engine, user_id=user_id, provider='aliyun_pan',
            )
            # If payload says 'baidu_pan' (default), credentials lookup misses.
            client = FakeBaiduPanClient()
            with pytest.raises(RuntimeError, match='Pan credentials missing'):
                await _run_executor(
                    {'job_id': job_id, 'user_id': str(user_id)},
                    engine=engine, client=client,
                )
            # With matching provider, succeeds.
            await _run_executor(
                {'job_id': job_id, 'user_id': str(user_id),
                 'provider': 'aliyun_pan'},
                engine=engine, client=client,
            )

    run_async(_go())


# =========================================================================
# Global serialization lock — 2026-05-26 postmortem P0b (Codex 2nd round)
# =========================================================================


def test_backup_executor_acquires_global_lock_before_per_job_lock(
        monkeypatch, tmp_path,
):
    """Pin the lock acquisition ORDER: global → per-job.

    Reverse order (per-job first, then global) would create a deadlock
    window between two concurrent backup_executors that pick conflicting
    per-job + global pairs in opposite orders. The fixed global → per-job
    order means all waiters block on the SAME first hop (global), so
    there's a single queue and no deadlock.

    Also verifies that:
      - Global lock IS acquired (regression guard against accidental removal)
      - It's acquired exactly once per executor invocation
    """
    from pan import backup_executor as be_mod
    from pan._lock_keys import pan_lock_key, PAN_BACKUP_GLOBAL_LOCK_KEY

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    job_id = 'job_global_lock_order'

    acquire_order: list[int] = []
    release_order: list[int] = []
    real_acquire = be_mod._acquire_advisory_lock
    real_release = be_mod._release_advisory_lock

    async def spy_acquire(conn, key):
        acquire_order.append(key)
        return await real_acquire(conn, key)

    async def spy_release(conn, key):
        release_order.append(key)
        return await real_release(conn, key)

    monkeypatch.setattr(be_mod, '_acquire_advisory_lock', spy_acquire)
    monkeypatch.setattr(be_mod, '_release_advisory_lock', spy_release)

    async def _go():
        async with pan_test_engine() as engine:
            project = make_project_dir(
                tmp_path, job_id=job_id, monkeypatch=monkeypatch,
            )
            await insert_sample_job(
                engine, user_id=user_id, job_id=job_id,
                project_dir=str(project),
            )
            await insert_sample_pan_credentials(engine, user_id=user_id)
            client = FakeBaiduPanClient()
            await _run_executor(
                {'job_id': job_id, 'user_id': str(user_id)},
                engine=engine, client=client,
            )

    run_async(_go())

    per_job_key = pan_lock_key(user_id, job_id)

    # Acquire order: GLOBAL must come before per-job.
    assert PAN_BACKUP_GLOBAL_LOCK_KEY in acquire_order, (
        f"expected GLOBAL lock acquired, got {acquire_order}"
    )
    assert per_job_key in acquire_order, (
        f"expected per-job lock acquired, got {acquire_order}"
    )
    global_idx = acquire_order.index(PAN_BACKUP_GLOBAL_LOCK_KEY)
    per_job_idx = acquire_order.index(per_job_key)
    assert global_idx < per_job_idx, (
        f"GLOBAL lock must be acquired BEFORE per-job lock to prevent "
        f"deadlock between concurrent executors. "
        f"global at index {global_idx}, per-job at {per_job_idx}. "
        f"Full acquire order: {acquire_order}"
    )

    # Both locks released. Order of release within finally is per-job
    # then global (reverse of acquire), but only the SET membership
    # matters for correctness — both must be released.
    assert PAN_BACKUP_GLOBAL_LOCK_KEY in release_order, (
        f"GLOBAL lock not released, would block all subsequent backups. "
        f"Release order: {release_order}"
    )
    assert per_job_key in release_order, (
        f"per-job lock not released. Release order: {release_order}"
    )


def test_global_lock_key_distinct_from_per_job_keys():
    """Contract guard: PAN_BACKUP_GLOBAL_LOCK_KEY must not collide with
    any plausible pan_lock_key(user_id, job_id) output.

    sha256 collisions are astronomically unlikely in theory, but a
    cheap structural test catches any future refactor that accidentally
    derives the constant from the same input pattern (e.g. someone
    defining ``GLOBAL = pan_lock_key(uuid.UUID(int=0), "")`` — sha256
    still distinct, but the test pins the contract that GLOBAL key is
    derived from a clearly-distinct fixed string."""
    from pan._lock_keys import pan_lock_key, PAN_BACKUP_GLOBAL_LOCK_KEY

    # Smoke a few plausible (user, job) pairs.
    samples = [
        (uuid.UUID(int=0), ""),
        (uuid.UUID(int=0), "job_0"),
        (uuid.uuid4(), "job_typical"),
        (uuid.uuid4(), "job_88bdca0966ce468fb6af36dc0bf4adeb"),
    ]
    for user_id, job_id in samples:
        derived = pan_lock_key(user_id, job_id)
        assert derived != PAN_BACKUP_GLOBAL_LOCK_KEY, (
            f"GLOBAL key {PAN_BACKUP_GLOBAL_LOCK_KEY} collides with "
            f"pan_lock_key({user_id}, {job_id!r})={derived}. "
            "GLOBAL must be derived from a clearly-distinct fixed string."
        )
