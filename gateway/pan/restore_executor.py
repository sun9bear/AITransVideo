"""Pan restore executor (Phase 5b T5.11).

Plan §8. Pulls the latest 'uploaded' BackupRecord for an 'archived' job
(with matching job_edit_generation per plan §8), downloads + verifies
the tar.gz, safe-extracts to staging, verifies the file inventory
entry-by-entry, then atomically moves into place at the original
project_dir.

## BackupRecord lifecycle (CodeX P1-1)

  uploaded → restoring → restored   (happy)
            → uploaded             (failure → revert; retryable)

heartbeat_at is updated every interval_s while in 'restoring' so
stale_reaper can detect dead restores via the same mechanism as
backup_executor.

## State machine

  archived
      │  (admin triggers restore)
      ▼
  restoring          ← set by set_archive_status
      │  ┌──── any failure ──→ Job.status back to 'archived'
      │  │                     staging dir cleaned up
      ▼  │
  [download + sha256 + safe_extract + verify inventory]
      │
      ▼   ← move staging → project_dir (atomic)
      │
  succeeded          ← set by set_archive_status

R2 artifacts are NOT re-uploaded on restore — they were intentionally
deleted at backup time (Job.r2_artifacts cleared to NULL). The restored
project_dir contains everything needed; if R2 publishing is desired
again, a separate publish flow handles it.

## Shared design with backup_executor

Same single-AsyncConnection long-hold + advisory lock pattern. Same
asyncio.to_thread wrapping for all blocking I/O. Same injection seam
shape (engine + client_factory + extras for testing). safe_extract_tar
(T5.11.5) is the security perimeter.

## Failure semantics

ALL failures are pre-commit (there's no single commit point in restore —
the atomic step is shutil.move into project_dir). Cleanup on failure:
  - staging dir → rmtree
  - Job.status → rolled back to 'archived'
  - BackupRecord stays as-is (still 'uploaded' → re-runnable)
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
from typing import Any, Callable

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


_REPO_SRC = Path(__file__).resolve().parent.parent.parent / "src"
for _candidate in (_REPO_SRC, Path("/opt/aivideotrans/app/src")):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))


logger = logging.getLogger(__name__)


# Phase 9 §T9.4 (CodeX 2026-05-19 P1b): pan JSONL emitter shared with
# backup_executor / residue_cleanup / auth (gateway/pan/_events.py).
from pan._events import emit_pan_event_safe as _emit_pan_event_safe  # noqa: E402


# --- public entry ---


async def execute_pan_restore(payload: dict) -> None:
    """Public entry for background_task_queue.

    Payload: {'job_id': str, 'user_id': str(UUID), 'provider': str?}
    """
    from database import engine  # noqa: PLC0415

    await _execute_pan_restore_impl(
        payload,
        engine=engine,
        client_factory=_default_client_factory,
        heartbeat_enabled=True,
    )


# --- impl with injection seams ---


async def _execute_pan_restore_impl(
    payload: dict,
    *,
    engine: AsyncEngine,
    client_factory: Callable[[], Any],
    staging_root: Path | None = None,
    heartbeat_enabled: bool = True,
    heartbeat_interval_s: int = 60,
) -> None:
    """Real restore executor. Plan §8 steps.

    `staging_root` defaults to ``$TMPDIR/pan_restore_{job_id}_{ts}/``. Tests
    can pin it under tmp_path for inspection.
    """
    from models import Job, BackupRecord, PanCredentials
    from pan.manifest import (
        read_manifest_from_tar,
        safe_extract_tar,
    )
    from pan.status_mutator import set_archive_status
    from pan.token_crypto import decrypt_token
    from pan.backup_executor import (
        _acquire_advisory_lock,
        _release_advisory_lock,
        _heartbeat_loop,
    )

    from pan._lock_keys import pan_lock_key

    job_id: str = payload['job_id']
    user_id: _uuid.UUID = _uuid.UUID(payload['user_id'])
    provider: str = payload.get('provider', 'baidu_pan')
    lock_key = pan_lock_key(user_id, job_id)  # stable across processes (CodeX P0-1)

    async with engine.connect() as conn:
        # --- advisory lock FIRST (CodeX P0-2) ---
        # Read Job/Credentials/BackupRecord only AFTER acquiring the lock.
        # Pre-lock read would let a concurrent restore see a stale snapshot
        # and corrupt state on the failure path.
        await _acquire_advisory_lock(conn, lock_key)

        try:
            # --- precondition (POST-lock — authoritative snapshot) ---
            async with conn.begin():
                job_row = (await conn.execute(
                    select(Job.status, Job.project_dir, Job.edit_generation)
                    .where(Job.user_id == user_id, Job.job_id == job_id)
                )).one_or_none()
                if job_row is None:
                    raise RuntimeError(
                        f"Job not found: user={user_id} job_id={job_id!r}"
                    )
                if job_row.status != 'archived':
                    raise RuntimeError(
                        f"Job status {job_row.status!r}, need 'archived' (412)"
                    )
                project_dir_str = job_row.project_dir
                if not project_dir_str:
                    raise RuntimeError(
                        f"Job {job_id!r} has no project_dir set (cannot restore)"
                    )
                job_edit_generation = job_row.edit_generation

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

                # Pick the LATEST uploaded BackupRecord for this job WITH
                # matching job_edit_generation (plan §8 + CodeX P1-1). If
                # Job got edited between archive and restore (unlikely but
                # possible), we must NOT restore a stale generation onto
                # a newer Job.
                br_row = (await conn.execute(
                    select(
                        BackupRecord.id, BackupRecord.remote_path,
                        BackupRecord.sha256, BackupRecord.md5,
                        BackupRecord.size_bytes, BackupRecord.manifest_json,
                        BackupRecord.job_edit_generation,
                    ).where(
                        BackupRecord.user_id == user_id,
                        BackupRecord.job_id == job_id,
                        BackupRecord.status == 'uploaded',
                        BackupRecord.job_edit_generation == job_edit_generation,
                    ).order_by(desc(BackupRecord.created_at))
                    .limit(1)
                )).one_or_none()
                if br_row is None:
                    # Distinguish "no matching gen" from "no uploaded row".
                    any_uploaded = (await conn.execute(
                        select(BackupRecord.job_edit_generation)
                        .where(
                            BackupRecord.user_id == user_id,
                            BackupRecord.job_id == job_id,
                            BackupRecord.status == 'uploaded',
                        ).order_by(desc(BackupRecord.created_at)).limit(1)
                    )).scalar_one_or_none()
                    if any_uploaded is None:
                        raise RuntimeError(
                            f"No 'uploaded' BackupRecord found for "
                            f"job_id={job_id!r}"
                        )
                    raise RuntimeError(
                        f"No 'uploaded' BackupRecord with "
                        f"job_edit_generation={job_edit_generation} for "
                        f"job_id={job_id!r} (latest uploaded has gen="
                        f"{any_uploaded}). Refusing to restore mismatched "
                        f"generation."
                    )

                access_token_enc = cred_row.access_token_encrypted
                br_id = br_row.id

            project_dir = Path(project_dir_str).resolve()
            # CodeX P0 unification: same safe-roots whitelist that backup
            # and residue_cleanup use. Restore writes to project_dir via
            # os.replace, so if project_dir is somehow pointed outside the
            # trusted roots we refuse before any side effect.
            from pan._safe_paths import verify_project_dir_safe
            verify_project_dir_safe(project_dir)
            remote_path = br_row.remote_path
            expected_sha = br_row.sha256
            manifest_persisted = br_row.manifest_json
            if isinstance(manifest_persisted, str):
                import json as _json
                manifest_persisted = _json.loads(manifest_persisted)

            access_token = decrypt_token(access_token_enc)
            client = client_factory()

            # Staging area. Default: SIBLING of project_dir so the final
            # move into place is os.replace() (atomic rename, same FS).
            # CodeX P1-2: $TMPDIR default risks crossing filesystem
            # boundaries, which would degrade shutil.move to copy+delete
            # with partial dst on failure.
            if staging_root is None:
                staging_root = (
                    project_dir.parent
                    / f'.pan_restore_staging_{job_id}_{int(time.time())}'
                )
            else:
                staging_root = Path(staging_root)
            tar_path: Path | None = None
            heartbeat_task: asyncio.Task | None = None
            # CodeX P1: after _move_into_place succeeds, project_dir is
            # restored on disk — past this point the user-visible "did
            # restore happen" answer is YES. Any subsequent DB finalize
            # failure must NOT roll Job.status back to 'archived',
            # otherwise the next restore attempt refuses ("project_dir
            # already exists") and the operator is stuck. moved=True
            # signals "leave status='restoring' for stale_reaper
            # forward-resolve."
            moved = False

            try:
                # --- Flip BackupRecord.status to 'restoring' (lifecycle
                # tracking per CodeX P1-1) + Job.status to 'restoring' ---
                async with conn.begin():
                    await set_archive_status(
                        user_id, job_id, 'restoring', conn=conn,
                    )
                    await conn.execute(
                        update(BackupRecord)
                        .where(BackupRecord.id == br_id)
                        .values(
                            status='restoring',
                            heartbeat_at=datetime.now(timezone.utc),
                        )
                    )

                # --- Start heartbeat loop (so stale_reaper can detect
                # dead restores via the same mechanism as backup) ---
                if heartbeat_enabled:
                    heartbeat_task = asyncio.create_task(
                        _heartbeat_loop(engine, br_id, heartbeat_interval_s)
                    )

                # Phase 9 §T9.4: emit started event for observability.
                _emit_pan_event_safe(
                    job_id=job_id,
                    event_type='pan.restore.started',
                    message=(
                        f"pan restore started: user={user_id} "
                        f"br={br_id}"
                    ),
                    payload={
                        'user_id': str(user_id),
                        'backup_id': str(br_id),
                        'provider': provider,
                        'remote_path': remote_path,
                    },
                )

                # --- download tar.gz ---
                staging_root.mkdir(parents=True, exist_ok=True)
                tar_path = staging_root / 'backup.tar.gz'
                download_result = await asyncio.to_thread(
                    client.download, remote_path, tar_path,
                    access_token=access_token,
                )

                # --- verify sha256 vs BackupRecord.sha256 ---
                actual_sha = download_result.get('sha256', '')
                if actual_sha != expected_sha:
                    raise RuntimeError(
                        f"Restore sha256 mismatch: download={actual_sha!r} "
                        f"BackupRecord.sha256={expected_sha!r}"
                    )

                # --- read manifest (sanity check + source of file_inventory) ---
                manifest_tar = await asyncio.to_thread(
                    read_manifest_from_tar, tar_path,
                )
                if manifest_tar.get('backup_format_version') != 1:
                    raise RuntimeError(
                        f"Unknown backup_format_version: "
                        f"{manifest_tar.get('backup_format_version')!r}"
                    )

                # Use the in-tar manifest's file_inventory (authoritative —
                # the PG row could have been edited, the tar can't).
                file_inventory = manifest_tar.get('file_inventory', [])

                # --- safe extract to staging ---
                extract_root = staging_root / 'extracted'
                await asyncio.to_thread(
                    safe_extract_tar, tar_path, extract_root,
                )

                # --- verify file inventory entry-by-entry ---
                staged_project = extract_root / project_dir.name
                await asyncio.to_thread(
                    _verify_inventory, staged_project, file_inventory,
                )

                # --- atomic move into place ---
                await asyncio.to_thread(
                    _move_into_place, staged_project, project_dir,
                )
                # Past this point the restore is observably complete:
                # the user's project_dir is on disk with verified contents.
                # Any failure below is finalize-only (DB writes) — do NOT
                # roll back via the except branch.
                moved = True

                # --- status='succeeded' + BackupRecord.status='restored' ---
                async with conn.begin():
                    await set_archive_status(
                        user_id, job_id, 'succeeded', conn=conn,
                    )
                    await conn.execute(
                        update(BackupRecord)
                        .where(BackupRecord.id == br_id)
                        .values(
                            status='restored',
                            completed_at=datetime.now(timezone.utc),
                        )
                    )
                logger.info("pan_restore succeeded: job=%s br=%s", job_id, br_id)

                # Phase 9 §T9.4: emit succeeded event. Past this point
                # both data on disk + DB finalize are complete.
                _emit_pan_event_safe(
                    job_id=job_id,
                    event_type='pan.restore.succeeded',
                    message=(
                        f"pan restore succeeded: user={user_id} "
                        f"br={br_id}"
                    ),
                    payload={
                        'user_id': str(user_id),
                        'backup_id': str(br_id),
                        'provider': provider,
                    },
                )

            except Exception as primary_exc:
                if moved:
                    # CodeX P1: hidden commit point — data is restored on
                    # disk. DB finalize failed but rolling back to
                    # 'archived' would create "restored on disk + DB
                    # says archived" stuck state that the next restore
                    # attempt refuses (project_dir already exists).
                    # Leave Job + BackupRecord at 'restoring' for stale
                    # reaper forward-resolve in Phase 8.
                    logger.error(
                        "pan_restore data was restored to disk but DB "
                        "finalize failed (job=%s br=%s). project_dir is "
                        "present and verified. Leaving status='restoring' "
                        "for stale_reaper forward-resolve. Manual "
                        "finalize: UPDATE jobs SET status='succeeded' "
                        "WHERE job_id=%r; UPDATE backup_records SET "
                        "status='restored', completed_at=NOW() WHERE "
                        "id=%r;", job_id, br_id, job_id, str(br_id),
                    )
                else:
                    # Pre-move failure: revert BOTH Job.status (→ 'archived')
                    # AND BackupRecord.status (→ 'uploaded' so the next
                    # attempt can re-pick this row). staging cleanup in finally.
                    try:
                        async with conn.begin():
                            await set_archive_status(
                                user_id, job_id, 'archived', conn=conn,
                            )
                            await conn.execute(
                                update(BackupRecord)
                                .where(BackupRecord.id == br_id)
                                .values(status='uploaded')
                            )
                    except Exception as inner_exc:  # noqa: BLE001
                        logger.error(
                            "pan_restore status rollback failed "
                            "(job=%s br=%s): %s",
                            job_id, br_id, inner_exc,
                        )
                # Phase 9 §T9.4: emit failed event for BOTH moved=True and
                # moved=False branches. Even when data is on disk, finalize
                # failure is a real failure from the observability POV.
                reason = str(primary_exc)[:200]
                _emit_pan_event_safe(
                    job_id=job_id,
                    event_type='pan.restore.failed',
                    message=f"pan restore failed: {reason}",
                    payload={
                        'user_id': str(user_id),
                        'backup_id': str(br_id) if br_id else None,
                        'provider': provider,
                        'reason': reason,
                        'moved': moved,
                    },
                    level='error',
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
                        logger.debug("pan_restore tar unlink failed: %s", exc)
                # Clean up staging root entirely (extract_root + leftovers).
                if staging_root.exists():
                    try:
                        await asyncio.to_thread(
                            shutil.rmtree, staging_root, True,  # ignore_errors=True
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "pan_restore staging cleanup failed: %s", exc,
                        )
        finally:
            await _release_advisory_lock(conn, lock_key)


# --- helpers ---


def _verify_inventory(staged_dir: Path, file_inventory: list[dict]) -> None:
    """Stream sha256 + size check for each entry. Raises on first mismatch.

    Path resolution: each entry['path'] is RELATIVE to staged_dir (the path
    contract from CodeX P2 — tar entries are under {project_dir.name}/ and
    inventory entries are NOT, so staged_dir IS the dir whose name matches
    project_dir.name)."""
    for entry in file_inventory:
        rel = entry['path']
        target = staged_dir / rel
        if not target.is_file():
            raise RuntimeError(
                f"Inventory verify: file missing post-extract: {rel!r}"
            )
        expected_size = entry['size']
        actual_size = target.stat().st_size
        if actual_size != expected_size:
            raise RuntimeError(
                f"Inventory verify: size mismatch for {rel!r}: "
                f"expected {expected_size} got {actual_size}"
            )
        sha = hashlib.sha256()
        with target.open('rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                sha.update(chunk)
        if sha.hexdigest() != entry['sha256']:
            raise RuntimeError(
                f"Inventory verify: sha256 mismatch for {rel!r}"
            )


def _move_into_place(staged_dir: Path, project_dir: Path) -> None:
    """Atomically rename staged_dir → project_dir on the same filesystem.

    Uses os.replace, which is atomic on POSIX and Windows when both paths
    are on the same filesystem (the caller's responsibility — the default
    staging_root computation puts staging next to project_dir to ensure
    this).

    Pre-condition: project_dir must NOT exist. Backup deletes project_dir
    before flipping to 'archived', and restore precondition requires
    Job.status='archived'. If project_dir exists here, something is off
    (manual recovery in progress, parallel restore, etc.) and forcing
    the operator to intervene is safer than blind overwrite.

    CodeX P1-2: previous shutil.move could degrade to copy+delete across
    filesystems with a partial-destination window on failure.
    """
    if project_dir.exists():
        raise RuntimeError(
            f"refuse to restore: project_dir already exists at "
            f"{project_dir} — backup should have removed it. Inspect and "
            f"clean up manually before retrying."
        )
    project_dir.parent.mkdir(parents=True, exist_ok=True)
    # os.replace is atomic when src and dst are on the same FS. Caller
    # ensures this via the default staging_root next to project_dir.parent.
    import os as _os
    _os.replace(staged_dir, project_dir)


def _default_client_factory():
    """Production factory: real BaiduPanClient from settings."""
    from config import settings  # noqa: PLC0415
    from pan.baidu_pan_client import BaiduPanClient
    return BaiduPanClient(
        appkey=settings.baidu_pan_appkey,
        appsecret=settings.baidu_pan_appsecret,
    )
