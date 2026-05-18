"""Pan backup executor (Phase 5b T5.2-T5.10).

Plan §7. Archives a succeeded job's project_dir + r2_artifacts metadata
to the user's pan backend (Baidu Pan in MVP). After three-gate verification
passes (size / server md5 / read-back probe), the local project_dir is
deleted, R2 artifacts are deleted, and Job.status flips to 'archived'.

## State machine

  succeeded
      │  (admin triggers backup)
      ▼
  archiving        ← set by set_archive_status
      │  ┌──── pre-commit failure ──→ Job.status back to 'succeeded'
      │  │                            BackupRecord.status='failed'
      ▼  │
  [tar build + upload + 3 gates]
      │
      ▼   ← COMMIT POINT (BackupRecord.status='uploaded')
      │
   ┌──┴──── any post-commit step fails ──→ log+continue. stale_reaper +
   │                                       residue_cleanup pick up later.
   ▼
  archived        ← set by set_archive_status (post-rmtree + R2 cleanup)

## Single-connection long-hold (CodeX C2)

The entire executor lifecycle holds ONE AsyncConnection so
pg_advisory_lock (session-level) stays valid. Short transactions
inside use `async with conn.begin():`. Blocking I/O (requests / tarfile /
hashlib / shutil.rmtree) is always wrapped in asyncio.to_thread so the
event loop is never frozen.

## Heartbeat

A separate asyncio task UPDATEs BackupRecord.heartbeat_at every 60s
(default) using an independent connection so the main lock-holding
connection isn't touched. stale_reaper uses heartbeat_at to detect
crashed executors. The loop is cancelled in the executor's finally
block. Tests disable via heartbeat_enabled=False.

## Injection seams (testing)

`execute_pan_backup` is the public entry — production deps wired in.
`_execute_pan_backup_impl` takes injectable engine / client_factory /
rmtree_fn / r2_delete_fn / heartbeat_enabled so tests can run on SQLite
+ FakeBaiduPanClient + tmp_path without real PG / Baidu / R2.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import sys
import time
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


# sys.path bootstrap so we can `from models import ...` (mirrors other
# gateway modules that work whether loaded under gateway/ or repo root).
_REPO_SRC = Path(__file__).resolve().parent.parent.parent / "src"
for _candidate in (_REPO_SRC, Path("/opt/aivideotrans/app/src")):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))


logger = logging.getLogger(__name__)


# --- public entry ---


async def execute_pan_backup(payload: dict) -> None:
    """Public entry for background_task_queue.

    Payload: {'job_id': str, 'user_id': str(UUID), 'provider': str?}
    """
    from database import engine  # noqa: PLC0415 — lazy: tests stub `database`

    await _execute_pan_backup_impl(
        payload,
        engine=engine,
        client_factory=_default_client_factory,
        rmtree_fn=shutil.rmtree,
        r2_delete_fn=_default_r2_delete,
        heartbeat_enabled=True,
    )


# --- impl with injection seams ---


async def _execute_pan_backup_impl(
    payload: dict,
    *,
    engine: AsyncEngine,
    client_factory: Callable[[], Any],
    rmtree_fn: Callable[[Path], None],
    r2_delete_fn: Callable[[str], None],
    heartbeat_enabled: bool = True,
    heartbeat_interval_s: int = 60,
    projects_root_env: str = 'AIVIDEOTRANS_PROJECTS_DIR',
) -> None:
    """Real backup executor. Plan §7 steps 0..l.

    Raises only for pre-COMMIT-POINT failures. Post-commit step failures
    (rmtree, R2 delete, status='archived' write) are logged and swallowed
    — stale_reaper / residue_cleanup will pick up the residue.
    """
    from models import Job, BackupRecord, PanCredentials
    from gateway.pan.manifest import build_manifest, write_tar_with_manifest
    from gateway.pan.status_mutator import set_archive_status
    from gateway.pan.token_crypto import decrypt_token

    from gateway.pan._lock_keys import pan_lock_key

    job_id: str = payload['job_id']
    user_id: _uuid.UUID = _uuid.UUID(payload['user_id'])
    provider: str = payload.get('provider', 'baidu_pan')
    # Stable cross-process lock key (CodeX P0-1) — Python builtin hash()
    # was randomized per process so multi-worker Gateway couldn't share
    # the advisory lock.
    lock_key = pan_lock_key(user_id, job_id)

    # === Single-connection long-hold ===
    async with engine.connect() as conn:
        # --- Step 0: precondition (short txn) ---
        async with conn.begin():
            job_row = (await conn.execute(
                select(
                    Job.status, Job.edit_generation, Job.project_dir,
                    Job.r2_artifacts,
                ).where(Job.user_id == user_id, Job.job_id == job_id)
            )).one_or_none()
            if job_row is None:
                raise RuntimeError(
                    f"Job not found: user={user_id} job_id={job_id!r}"
                )
            if job_row.status != 'succeeded':
                raise RuntimeError(
                    f"Job status {job_row.status!r}, need 'succeeded' (412)"
                )

            cred_row = (await conn.execute(
                select(
                    PanCredentials.access_token_encrypted,
                    PanCredentials.status,
                ).where(
                    PanCredentials.user_id == user_id,
                    PanCredentials.provider == provider,
                )
            )).one_or_none()
            if cred_row is None:
                raise RuntimeError(
                    f"Pan credentials missing for user={user_id} provider={provider}"
                )
            if cred_row.status != 'active':
                raise RuntimeError(
                    f"Pan credentials status {cred_row.status!r}, need 'active'"
                )

            edit_generation = job_row.edit_generation
            project_dir_str = job_row.project_dir
            r2_artifacts = job_row.r2_artifacts or []
            access_token_enc = cred_row.access_token_encrypted

        if not project_dir_str:
            raise RuntimeError(f"Job {job_id!r} has no project_dir set")
        project_dir = Path(project_dir_str).resolve()
        _verify_project_dir_safety(project_dir, projects_root_env)

        # --- Step a: advisory lock (PG only; SQLite no-op) ---
        await _acquire_advisory_lock(conn, lock_key)

        access_token = decrypt_token(access_token_enc)
        client = client_factory()
        br_id: _uuid.UUID | None = None
        tar_path: Path | None = None
        heartbeat_task: asyncio.Task | None = None

        try:
            # --- Step b: status='archiving' (short txn) ---
            async with conn.begin():
                await set_archive_status(user_id, job_id, 'archiving', conn=conn)

            # --- Step c: INSERT backup_records ---
            async with conn.begin():
                br_id = _uuid.uuid4()
                await conn.execute(
                    BackupRecord.__table__.insert().values(
                        id=br_id,
                        user_id=user_id,
                        job_id=job_id,
                        job_edit_generation=edit_generation,
                        provider=provider,
                        remote_path='',
                        size_bytes=0,
                        sha256='',
                        md5='',
                        manifest_json={},
                        status='uploading',
                        heartbeat_at=datetime.now(timezone.utc),
                        created_at=datetime.now(timezone.utc),
                    )
                )

            # --- Start heartbeat loop (best effort) ---
            if heartbeat_enabled:
                heartbeat_task = asyncio.create_task(
                    _heartbeat_loop(engine, br_id, heartbeat_interval_s)
                )

            try:
                # --- Steps d-f: build manifest + tar.gz + checksums ---
                job_record_snapshot = {
                    'job_id': job_id,
                    'user_id': str(user_id),
                    'status': 'archiving',
                    'edit_generation': edit_generation,
                }
                manifest = await asyncio.to_thread(
                    build_manifest,
                    project_dir=project_dir,
                    job_record=job_record_snapshot,
                    r2_artifacts=list(r2_artifacts),
                )

                tar_path = Path(
                    os.environ.get('TMPDIR', '/tmp')
                ).joinpath(
                    f'pan_backup_{job_id}_{int(time.time())}.tar.gz'
                )
                await asyncio.to_thread(
                    write_tar_with_manifest, tar_path, manifest, project_dir,
                )
                sha256, md5 = await asyncio.to_thread(
                    _compute_tar_checksums, tar_path,
                )

                # --- Step g: upload (cross-border slowest) ---
                remote_path = (
                    f'/apps/AIVideoTrans/backups/'
                    f'{job_id}_{int(time.time())}.tar.gz'
                )
                upload_result = await asyncio.to_thread(
                    client.upload, tar_path, remote_path,
                    access_token=access_token,
                )

                # --- Step h: three gates ---
                local_size = tar_path.stat().st_size
                if upload_result.get('size') != local_size:
                    raise RuntimeError(
                        f"Gate 1 (size) failed: server {upload_result.get('size')} "
                        f"!= local {local_size}"
                    )
                if upload_result.get('md5') != md5:
                    raise RuntimeError(
                        f"Gate 2 (md5) failed: server {upload_result.get('md5')!r} "
                        f"!= local {md5!r}"
                    )
                read_back_ok = await asyncio.to_thread(
                    client.verify_remote_tail, tar_path, remote_path,
                    size=local_size, access_token=access_token,
                )
                if not read_back_ok:
                    raise RuntimeError(
                        "Gate 3 (read-back probe) failed — refuse to delete local"
                    )

                # === COMMIT POINT (step i) ===
                async with conn.begin():
                    await conn.execute(
                        update(BackupRecord)
                        .where(BackupRecord.id == br_id)
                        .values(
                            status='uploaded',
                            remote_path=remote_path,
                            sha256=sha256,
                            md5=md5,
                            size_bytes=local_size,
                            manifest_json=manifest,
                            completed_at=datetime.now(timezone.utc),
                        )
                    )
                logger.info(
                    "pan_backup commit point reached: job=%s br=%s",
                    job_id, br_id,
                )
                # ↑ Beyond this point: failures are LOGGED, not raised.
                # backup_records remains 'uploaded', stale_reaper + residue
                # cleanup will pick up any residual project_dir / R2 / Job
                # state and reconcile.

                # --- Step j: rmtree project_dir (post-commit) ---
                try:
                    await asyncio.to_thread(rmtree_fn, project_dir)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "pan_backup rmtree failed (residue): job=%s path=%s err=%s",
                        job_id, project_dir, exc,
                    )

                # --- Step k: delete R2 artifacts (post-commit) ---
                for artifact in r2_artifacts:
                    r2_key = artifact.get('r2_key') if isinstance(artifact, dict) else None
                    if not r2_key:
                        continue
                    try:
                        await asyncio.to_thread(r2_delete_fn, r2_key)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "pan_backup R2 delete failed (residue): job=%s "
                            "r2_key=%s err=%s",
                            job_id, r2_key, exc,
                        )

                # --- Step l: Job.status='archived' + clear r2_artifacts ---
                try:
                    async with conn.begin():
                        await set_archive_status(
                            user_id, job_id, 'archived', conn=conn,
                        )
                        await conn.execute(
                            update(Job)
                            .where(Job.user_id == user_id, Job.job_id == job_id)
                            .values(r2_artifacts=None)
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "pan_backup status='archived' write failed: job=%s err=%s",
                        job_id, exc,
                    )

            except Exception:
                # Pre-COMMIT-POINT failure path. Mark backup_records=failed
                # and roll Job.status back to 'succeeded' so the row isn't
                # stuck in 'archiving'. THEN re-raise.
                if br_id is not None:
                    try:
                        async with conn.begin():
                            await conn.execute(
                                update(BackupRecord)
                                .where(BackupRecord.id == br_id)
                                .values(
                                    status='failed',
                                    completed_at=datetime.now(timezone.utc),
                                )
                            )
                    except Exception as inner_exc:  # noqa: BLE001
                        logger.error(
                            "pan_backup rollback failed (br=%s): %s",
                            br_id, inner_exc,
                        )
                try:
                    async with conn.begin():
                        await set_archive_status(
                            user_id, job_id, 'succeeded', conn=conn,
                        )
                except Exception as inner_exc:  # noqa: BLE001
                    logger.error(
                        "pan_backup Job.status rollback to 'succeeded' failed: %s",
                        inner_exc,
                    )
                raise

            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except (asyncio.CancelledError, Exception):
                        pass
                if tar_path is not None:
                    try:
                        await asyncio.to_thread(
                            tar_path.unlink, missing_ok=True,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("pan_backup tar cleanup failed: %s", exc)
        finally:
            await _release_advisory_lock(conn, lock_key)


# --- helpers ---


async def _acquire_advisory_lock(conn: AsyncConnection, key: int) -> None:
    """PG session-level advisory lock. No-op on non-PG dialects (SQLite
    in unit tests). Production always runs PG."""
    if conn.dialect.name == 'postgresql':
        await conn.execute(
            text("SELECT pg_advisory_lock(:k)"), {'k': key},
        )


async def _release_advisory_lock(conn: AsyncConnection, key: int) -> None:
    if conn.dialect.name == 'postgresql':
        await conn.execute(
            text("SELECT pg_advisory_unlock(:k)"), {'k': key},
        )


def _verify_project_dir_safety(
    project_dir: Path, projects_root_env: str = 'AIVIDEOTRANS_PROJECTS_DIR',
) -> None:
    """Refuse to operate on a project_dir that's NOT inside projects_root,
    or that IS projects_root itself. Defense against config drift /
    malformed Job.project_dir values that would let rmtree wipe the
    wrong directory.

    No-op (does NOT raise) if projects_root_env is unset — admin still
    has to opt in to backup, so the practical lever is via env wiring.
    """
    root_str = os.environ.get(projects_root_env, '')
    if not root_str:
        return
    projects_root = Path(root_str).resolve()
    if project_dir == projects_root:
        raise RuntimeError(
            f"project_dir {project_dir} equals projects_root — refuse"
        )
    try:
        project_dir.relative_to(projects_root)
    except ValueError:
        raise RuntimeError(
            f"project_dir {project_dir} not inside projects_root {projects_root}"
        )


def _compute_tar_checksums(tar_path: Path) -> tuple[str, str]:
    """Return (sha256, md5) hex digests for the file at tar_path. 1MB chunks."""
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    with tar_path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


async def _heartbeat_loop(
    engine: AsyncEngine, br_id: _uuid.UUID, interval_s: int,
) -> None:
    """Update BackupRecord.heartbeat_at every interval_s. Independent
    connection per tick so the main lock-holding conn is never touched.
    Errors are swallowed — heartbeat is best-effort."""
    from models import BackupRecord
    while True:
        try:
            async with engine.connect() as conn:
                async with conn.begin():
                    await conn.execute(
                        update(BackupRecord)
                        .where(BackupRecord.id == br_id)
                        .values(heartbeat_at=datetime.now(timezone.utc))
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("heartbeat tick failed (will retry): %s", exc)
        await asyncio.sleep(interval_s)


def _default_client_factory():
    """Production factory: real BaiduPanClient from settings."""
    from config import settings  # noqa: PLC0415
    from gateway.pan.baidu_pan_client import BaiduPanClient
    return BaiduPanClient(
        appkey=settings.baidu_pan_appkey,
        appsecret=settings.baidu_pan_appsecret,
    )


def _default_r2_delete(r2_key: str) -> None:
    """Production R2 delete via the shared boto3 client. Idempotent
    (boto3 delete_object returns 204 even on missing keys)."""
    from config import settings  # noqa: PLC0415
    from gateway.storage.r2_client import _get_client  # noqa: PLC0415
    client = _get_client()
    client.delete_object(Bucket=settings.r2_artifacts_bucket, Key=r2_key)
