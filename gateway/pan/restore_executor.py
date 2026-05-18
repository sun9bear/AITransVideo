"""Pan restore executor (Phase 5b T5.11).

Plan §8. Pulls the latest 'uploaded' BackupRecord for an 'archived' job,
downloads + verifies the tar.gz, safe-extracts to staging, verifies the
file inventory entry-by-entry, then atomically moves into place at the
original project_dir.

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
    )


# --- impl with injection seams ---


async def _execute_pan_restore_impl(
    payload: dict,
    *,
    engine: AsyncEngine,
    client_factory: Callable[[], Any],
    staging_root: Path | None = None,
) -> None:
    """Real restore executor. Plan §8 steps.

    `staging_root` defaults to ``$TMPDIR/pan_restore_{job_id}_{ts}/``. Tests
    can pin it under tmp_path for inspection.
    """
    from models import Job, BackupRecord, PanCredentials
    from gateway.pan.manifest import (
        read_manifest_from_tar,
        safe_extract_tar,
    )
    from gateway.pan.status_mutator import set_archive_status
    from gateway.pan.token_crypto import decrypt_token
    from gateway.pan.backup_executor import (
        _acquire_advisory_lock,
        _release_advisory_lock,
    )

    from gateway.pan._lock_keys import pan_lock_key

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
                    select(Job.status, Job.project_dir)
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

                # Pick the LATEST uploaded BackupRecord for this job.
                br_row = (await conn.execute(
                    select(
                        BackupRecord.id, BackupRecord.remote_path,
                        BackupRecord.sha256, BackupRecord.md5,
                        BackupRecord.size_bytes, BackupRecord.manifest_json,
                    ).where(
                        BackupRecord.user_id == user_id,
                        BackupRecord.job_id == job_id,
                        BackupRecord.status == 'uploaded',
                    ).order_by(desc(BackupRecord.created_at))
                    .limit(1)
                )).one_or_none()
                if br_row is None:
                    raise RuntimeError(
                        f"No 'uploaded' BackupRecord found for job_id={job_id!r}"
                    )

                access_token_enc = cred_row.access_token_encrypted

            project_dir = Path(project_dir_str).resolve()
            remote_path = br_row.remote_path
            expected_sha = br_row.sha256
            manifest_persisted = br_row.manifest_json
            if isinstance(manifest_persisted, str):
                import json as _json
                manifest_persisted = _json.loads(manifest_persisted)

            access_token = decrypt_token(access_token_enc)
            client = client_factory()

            # Staging area. Default: $TMPDIR/pan_restore_{job}_{ts}/
            if staging_root is None:
                staging_root = Path(
                    os.environ.get('TMPDIR', '/tmp')
                ) / f'pan_restore_{job_id}_{int(time.time())}'
            else:
                staging_root = Path(staging_root)
            tar_path: Path | None = None

            try:
                # --- status='restoring' ---
                async with conn.begin():
                    await set_archive_status(
                        user_id, job_id, 'restoring', conn=conn,
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

                # --- status='succeeded' ---
                async with conn.begin():
                    await set_archive_status(
                        user_id, job_id, 'succeeded', conn=conn,
                    )
                logger.info("pan_restore succeeded: job=%s", job_id)

            except Exception:
                # Failure path: status back to 'archived', staging cleanup.
                try:
                    async with conn.begin():
                        await set_archive_status(
                            user_id, job_id, 'archived', conn=conn,
                        )
                except Exception as inner_exc:  # noqa: BLE001
                    logger.error(
                        "pan_restore status rollback to 'archived' failed: %s",
                        inner_exc,
                    )
                raise

            finally:
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
    """Atomically place the extracted staged_dir at project_dir.

    If project_dir already exists (rare — backup should have rmtree'd it),
    we rm it first. Then shutil.move is the atomic step. Parents of
    project_dir are mkdir-p'd defensively.
    """
    project_dir.parent.mkdir(parents=True, exist_ok=True)
    if project_dir.exists():
        shutil.rmtree(project_dir)
    shutil.move(str(staged_dir), str(project_dir))


def _default_client_factory():
    """Production factory: real BaiduPanClient from settings."""
    from config import settings  # noqa: PLC0415
    from gateway.pan.baidu_pan_client import BaiduPanClient
    return BaiduPanClient(
        appkey=settings.baidu_pan_appkey,
        appsecret=settings.baidu_pan_appsecret,
    )
