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
) -> None:
    """Real backup executor. Plan §7 steps 0..l.

    Raises only for pre-COMMIT-POINT failures. Post-commit step failures
    (rmtree, R2 delete, status='archived' write) are logged and swallowed
    — stale_reaper / residue_cleanup will pick up the residue.
    """
    from models import Job, BackupRecord, PanCredentials
    from pan.manifest import build_manifest, write_tar_with_manifest
    from pan.status_mutator import set_archive_status
    from pan.token_crypto import decrypt_token

    from pan._lock_keys import pan_lock_key

    job_id: str = payload['job_id']
    user_id: _uuid.UUID = _uuid.UUID(payload['user_id'])
    provider: str = payload.get('provider', 'baidu_pan')
    # Stable cross-process lock key (CodeX P0-1) — Python builtin hash()
    # was randomized per process so multi-worker Gateway couldn't share
    # the advisory lock.
    lock_key = pan_lock_key(user_id, job_id)

    # === Single-connection long-hold ===
    async with engine.connect() as conn:
        # --- Step a: advisory lock FIRST (PG only; SQLite no-op) ---
        # CodeX P0-2: take the lock BEFORE reading Job state. The previous
        # ordering (read → lock → use stale snapshot) allowed a concurrent
        # worker to archive the same job between read and lock; the failure
        # path would then "roll back" an already-archived Job from
        # 'archived' to 'succeeded'. Lock-then-read closes the TOCTOU window.
        await _acquire_advisory_lock(conn, lock_key)
        # Production 2026-05-19 hotfix: `SELECT pg_advisory_lock(...)`
        # auto-begins a txn under SQLAlchemy. The first `async with
        # conn.begin()` below would then raise
        # "This connection has already initialized a SQLAlchemy
        # Transaction() object via begin() or autobegin". Flush the
        # auto-begin txn now — the advisory lock itself is session-scoped
        # (pg_advisory_lock without _xact_), so commit doesn't release it.
        # Same fix pattern as stale_reaper Phase 8 CodeX P1-3.
        if conn.dialect.name == 'postgresql':
            await conn.commit()

        br_id: _uuid.UUID | None = None
        tar_path: Path | None = None
        heartbeat_task: asyncio.Task | None = None

        try:
            # --- Step 0: precondition (POST-lock — authoritative snapshot) ---
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
                        f"Pan credentials missing for user={user_id} "
                        f"provider={provider}"
                    )
                if cred_row.status != 'active':
                    raise RuntimeError(
                        f"Pan credentials status {cred_row.status!r}, "
                        f"need 'active'"
                    )

                edit_generation = job_row.edit_generation
                project_dir_str = job_row.project_dir
                r2_artifacts = job_row.r2_artifacts or []
                access_token_enc = cred_row.access_token_encrypted

            if not project_dir_str:
                raise RuntimeError(f"Job {job_id!r} has no project_dir set")
            project_dir = Path(project_dir_str).resolve()
            # CodeX P0 unification: reuse project_cleanup safe-root whitelist
            # via gateway/pan/_safe_paths.verify_project_dir_safe instead of
            # the previous env-only guard that no-op'd when the env var was
            # absent. Production now ALWAYS has DEFAULT_SAFE_PROJECT_ROOTS
            # as fallback; AIVIDEOTRANS_PROJECTS_DIR (when set) prepends.
            from pan._safe_paths import verify_project_dir_safe
            verify_project_dir_safe(project_dir)

            access_token = decrypt_token(access_token_enc)
            client = client_factory()

            try:
                # --- Step b: status='archiving' (short txn) ---
                async with conn.begin():
                    await set_archive_status(
                        user_id, job_id, 'archiving', conn=conn,
                    )

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

                # Phase 9 §T9.4: emit started event for observability.
                # Stage='pan' so r2_observability buckets it correctly.
                # Best-effort — write failure must NOT abort the backup.
                _emit_pan_event_safe(
                    job_id=job_id,
                    event_type='pan.backup.started',
                    message=(
                        f"pan backup started: user={user_id} "
                        f"edit_gen={edit_generation}"
                    ),
                    payload={
                        'user_id': str(user_id),
                        'backup_id': str(br_id),
                        'provider': provider,
                        'edit_generation': edit_generation,
                    },
                )

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

                # Gate 2 (md5) — Production 2026-05-19: Baidu's
                # `pan/file?method=create` returns a 32-char `md5` that is
                # NOT always raw hex. Real cases observed:
                #   server: '6d3a845fevc7f34602947bd7d978bef1'  (note the
                #           'v' at position 11 — non-hex)
                #   local:  '500ee15350281f0f459b0cbdf0d06503'  (valid hex)
                # Baidu obfuscates the md5 for some upload paths (e.g.
                # rapid-upload hits, large-file paths, undocumented data-
                # security trigger). Strict equality fails on every such
                # upload even when the content is byte-identical.
                #
                # We still have layered guarantees WITHOUT this gate:
                #  - block_list verification: precreate + per-chunk upload
                #    rely on chunk md5s we computed locally. Baidu's
                #    finalize step rejects (errno != 0) if the merged
                #    content doesn't reconstruct the declared block list.
                #  - Gate 1 (size) ensures byte count matches.
                #  - Gate 3 (read-back probe) re-reads the tail of the
                #    remote file and checks against the local tail.
                #
                # Decision: only enforce Gate 2 when the server md5 looks
                # like raw hex (32 lowercase hex chars). Non-hex returns
                # are logged at INFO and treated as best-effort confirmation;
                # the layered guarantees above carry the safety.
                server_md5_raw = upload_result.get('md5') or ''
                server_md5_looks_hex = (
                    isinstance(server_md5_raw, str)
                    and len(server_md5_raw) == 32
                    and all(c in '0123456789abcdef' for c in server_md5_raw.lower())
                )
                if server_md5_looks_hex:
                    if server_md5_raw.lower() != md5.lower():
                        raise RuntimeError(
                            f"Gate 2 (md5) failed: server {server_md5_raw!r} "
                            f"!= local {md5!r}"
                        )
                else:
                    logger.info(
                        "pan_backup: Gate 2 (md5) — Baidu returned non-hex "
                        "md5=%r, treating as obfuscated (rapid-upload / "
                        "data-security). Skipping strict equality; relying "
                        "on block_list + size + read-back probe gates. "
                        "job=%s br=%s", server_md5_raw, job_id, br_id,
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

                # --- Step j: rmtree project_dir (post-commit, tracked) ---
                rmtree_ok = True
                try:
                    await asyncio.to_thread(rmtree_fn, project_dir)
                except Exception as exc:  # noqa: BLE001
                    rmtree_ok = False
                    logger.warning(
                        "pan_backup rmtree failed: job=%s path=%s err=%s — "
                        "leaving Job at 'archiving' for residue_cleanup retry",
                        job_id, project_dir, exc,
                    )

                # --- Step k: delete R2 artifacts (post-commit, tracked) ---
                r2_failures: list[str] = []
                for artifact in r2_artifacts:
                    r2_key = artifact.get('r2_key') if isinstance(artifact, dict) else None
                    if not r2_key:
                        continue
                    try:
                        await asyncio.to_thread(r2_delete_fn, r2_key)
                    except Exception as exc:  # noqa: BLE001
                        r2_failures.append(r2_key)
                        logger.warning(
                            "pan_backup R2 delete failed: job=%s r2_key=%s "
                            "err=%s — leaving Job at 'archiving' for residue "
                            "cleanup retry", job_id, r2_key, exc,
                        )

                # --- Step l: Job.status='archived' (CONDITIONAL on cleanup OK) ---
                # CodeX P0-3: only finalize if both rmtree + all R2 deletes
                # succeeded. Otherwise Job stays at 'archiving' and
                # r2_artifacts intact — residue_cleanup picks it up later.
                # Finalizing with residue would (a) clear r2_artifacts and
                # destroy our way to find the orphan keys, (b) make
                # residue_cleanup think the job is done.
                if rmtree_ok and not r2_failures:
                    archived_finalized = False
                    try:
                        async with conn.begin():
                            await set_archive_status(
                                user_id, job_id, 'archived', conn=conn,
                            )
                            await conn.execute(
                                update(Job)
                                .where(
                                    Job.user_id == user_id,
                                    Job.job_id == job_id,
                                )
                                .values(r2_artifacts=None)
                            )
                        archived_finalized = True
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "pan_backup status='archived' write failed: "
                            "job=%s err=%s", job_id, exc,
                        )

                    # Phase 9 §T9.4: emit succeeded event only if we
                    # actually finalized. Partial finalize (rmtree+R2 ok
                    # but status flip failed) leaves residue_cleanup to
                    # retry — no event yet because the backup isn't done
                    # from the dashboard's perspective.
                    if archived_finalized:
                        _emit_pan_event_safe(
                            job_id=job_id,
                            event_type='pan.backup.succeeded',
                            message=(
                                f"pan backup succeeded: user={user_id} "
                                f"size={local_size}"
                            ),
                            payload={
                                'user_id': str(user_id),
                                'backup_id': str(br_id),
                                'provider': provider,
                                'remote_path': remote_path,
                                'size_bytes': local_size,
                                'sha256': sha256,
                            },
                        )
                else:
                    logger.info(
                        "pan_backup not finalizing job=%s: rmtree_ok=%s "
                        "r2_failures=%s — residue_cleanup will retry",
                        job_id, rmtree_ok, r2_failures,
                    )

            except Exception as primary_exc:
                # Pre-COMMIT-POINT failure path. Mark backup_records=failed
                # and roll Job.status back to 'succeeded' so the row isn't
                # stuck in 'archiving'. THEN re-raise.
                # Production 2026-05-19: capture primary_exc as
                # error_message — previously left null/'' which made the
                # admin UI's failure column unhelpful. 500-char truncate
                # for Text column safety.
                failure_reason = str(primary_exc)[:500] or 'unknown'
                if br_id is not None:
                    try:
                        async with conn.begin():
                            await conn.execute(
                                update(BackupRecord)
                                .where(BackupRecord.id == br_id)
                                .values(
                                    status='failed',
                                    completed_at=datetime.now(timezone.utc),
                                    error_message=failure_reason,
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
                # Phase 9 §T9.4: emit failed event AFTER rollback so the
                # event accurately reflects post-rollback state. Reason is
                # truncated to 200 chars — notifications_service further
                # constrains via _PAYLOAD_ALLOWLIST.
                reason = str(primary_exc)[:200]
                _emit_pan_event_safe(
                    job_id=job_id,
                    event_type='pan.backup.failed',
                    message=f"pan backup failed: {reason}",
                    payload={
                        'user_id': str(user_id),
                        'backup_id': str(br_id) if br_id else None,
                        'provider': provider,
                        'reason': reason,
                    },
                    level='error',
                )
                # CodeX 2026-05-19 P1d: also surface the failure as a
                # user notification so admins don't have to poll the
                # dashboard. Best-effort — wrapped helper swallows all
                # exceptions; dispatch failure must never block the
                # raise below.
                try:
                    await _dispatch_pan_failure_notification(
                        engine,
                        event_type='pan.backup.failed',
                        user_id=user_id,
                        job_id=job_id,
                        reason=reason,
                    )
                except Exception as note_exc:  # noqa: BLE001
                    # Already swallowed inside the helper, but defensive
                    # second catch keeps the raise below clean.
                    logger.warning(
                        "pan_backup notification dispatch failed: %s",
                        note_exc,
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


# NOTE: _verify_project_dir_safety was removed in favor of
# gateway.pan._safe_paths.verify_project_dir_safe (CodeX P0 unification).
# The new helper reuses project_cleanup._is_safe_project_dir + the same
# DEFAULT_SAFE_PROJECT_ROOTS whitelist that TTL cleanup uses, so all
# destructive operations across the codebase share one path-safety
# contract instead of three reinvented ones.


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
    from pan.baidu_pan_client import BaiduPanClient
    return BaiduPanClient(
        appkey=settings.baidu_pan_appkey,
        appsecret=settings.baidu_pan_appsecret,
    )


def _default_r2_delete(r2_key: str) -> None:
    """Production R2 delete via the shared boto3 client. Idempotent
    (boto3 delete_object returns 204 even on missing keys)."""
    from config import settings  # noqa: PLC0415
    from storage.r2_client import _get_client  # noqa: PLC0415
    client = _get_client()
    client.delete_object(Bucket=settings.r2_artifacts_bucket, Key=r2_key)


# Phase 9 §T9.4 (CodeX 2026-05-19 P1b): pan JSONL emitter shared with
# restore_executor / residue_cleanup / auth (gateway/pan/_events.py).
from pan._events import (  # noqa: E402
    emit_pan_event_safe as _emit_pan_event_safe,
    dispatch_pan_failure_notification as _dispatch_pan_failure_notification,
)
