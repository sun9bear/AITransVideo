"""Admin Pan Backup API endpoints.

Plan 2026-05-14 Phase 7 §T7.1-T7.6. Endpoints under /api/admin/pan/*
(prefix shared with pan/auth.py — different paths, no collision):

Read:
  GET    /api/admin/pan/status            — connection state + quota
  GET    /api/admin/pan/backups           — list BackupRecord with filters
  GET    /api/admin/pan/backups/{id}/manifest

Mutating (enqueue → BackgroundTask + dispatcher):
  POST   /api/admin/pan/backups           {job_id}
  POST   /api/admin/pan/backups/batch     {job_ids[]}
  POST   /api/admin/pan/restores          {job_id}
  DELETE /api/admin/pan/credentials       — disconnect (status='revoked')

Soft-delete with 412 guard (spec §6):
  DELETE /api/admin/pan/backups/{id}      — refuse if unique recoverable
                                            copy of an archived job

All endpoints require admin role. Pan operations target the admin's own
PanCredentials row (single-pan-per-user model — admin is the only user
with pan connected in MVP).
"""
from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from config import settings
from database import get_db
from models import BackupRecord, Job, PanCredentials, User

import background_task_queue as queue
from background_task_executors import TASK_EXECUTORS

from pan.baidu_pan_client import BaiduPanClient
from pan.token_crypto import decrypt_token


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/pan", tags=["admin-pan"])


# --- admin gate (local copy keeps this module loosely coupled) ---


def _require_admin(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if (getattr(user, "role", None) or "user") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# --- pydantic models for request bodies ---


class BackupCreateRequest(BaseModel):
    job_id: str = Field(..., min_length=1, max_length=64)


class BatchBackupRequest(BaseModel):
    job_ids: list[str] = Field(..., min_length=1, max_length=100)

    @field_validator("job_ids")
    @classmethod
    def _dedupe(cls, ids: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for j in ids:
            j = (j or "").strip()
            if not j or len(j) > 64:
                raise ValueError(f"非法 job_id: {j!r}")
            if j not in seen:
                out.append(j)
                seen.add(j)
        return out


class RestoreRequest(BaseModel):
    job_id: str = Field(..., min_length=1, max_length=64)


# --- helpers ---


def _client_factory() -> BaiduPanClient:
    return BaiduPanClient(
        appkey=settings.baidu_pan_appkey,
        appsecret=settings.baidu_pan_appsecret,
    )


def _serialize_backup_record(br: BackupRecord) -> dict[str, Any]:
    """Public-facing JSON serialization. manifest_json is omitted from
    list responses — fetch via /backups/{id}/manifest if needed."""
    return {
        "id": str(br.id),
        "user_id": str(br.user_id),
        "job_id": br.job_id,
        "job_edit_generation": br.job_edit_generation,
        "provider": br.provider,
        "remote_path": br.remote_path,
        "size_bytes": br.size_bytes,
        "sha256": br.sha256,
        "md5": br.md5,
        "status": br.status,
        "heartbeat_at": br.heartbeat_at.isoformat() if br.heartbeat_at else None,
        "created_at": br.created_at.isoformat() if br.created_at else None,
        "completed_at": br.completed_at.isoformat() if br.completed_at else None,
        "error_message": br.error_message,
    }


async def _fetch_admin_credentials(
    db: AsyncSession, user_id: _uuid.UUID, *, require_active: bool,
) -> PanCredentials | None:
    """Fetch the admin's PanCredentials row.

    If `require_active=True`, raise 412 when:
      - no row exists at all (admin hasn't connected pan yet)
      - row exists but status != 'active' (revoked / something else)

    If `require_active=False`, return whatever's there (or None) so the
    caller can branch (e.g. /status reports 'disconnected' when None).
    """
    row = (await db.execute(
        select(PanCredentials).where(
            PanCredentials.user_id == user_id,
            PanCredentials.provider == 'baidu_pan',
        )
    )).scalar_one_or_none()
    if require_active:
        if row is None:
            raise HTTPException(
                status_code=412,
                detail="未连接网盘;请先访问 /api/admin/pan/connect 授权",
            )
        if row.status != 'active':
            raise HTTPException(
                status_code=412,
                detail=f"网盘凭证状态 {row.status!r},需 'active';请重新连接网盘",
            )
    return row


# --- endpoints ---


@router.get("/status")
async def get_pan_status(
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Connection state + remote quota.

    - No PanCredentials row → connected=false, status='disconnected'.
    - status='revoked' → connected=true with revoked flag, no quota fetch.
    - status='active' → fetch quota via BaiduPanClient.get_quota; if the
      remote call fails (network / Baidu down), still return active=true
      with quota=null so the UI shows "connected but cannot read quota"
      rather than blowing up the status endpoint.
    """
    admin = _require_admin(user)
    cred = await _fetch_admin_credentials(db, admin.id, require_active=False)
    if cred is None:
        return {
            "connected": False,
            "status": "disconnected",
            "quota": None,
            "scope": None,
            "last_refreshed_at": None,
            "connected_at": None,
        }

    base = {
        "connected": True,
        "status": cred.status,
        "scope": cred.scope or "",
        "last_refreshed_at": cred.last_refreshed_at.isoformat()
        if cred.last_refreshed_at else None,
        "connected_at": cred.connected_at.isoformat()
        if cred.connected_at else None,
        "quota": None,
    }
    if cred.status != 'active':
        return base

    # Attempt quota fetch — best effort.
    try:
        access_token = decrypt_token(cred.access_token_encrypted)
        client = _client_factory()
        quota = await asyncio.to_thread(client.get_quota, access_token=access_token)
        base["quota"] = quota
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "admin_pan/status: quota fetch failed for user=%s: %s",
            admin.id, exc,
        )
        base["quota_error"] = str(exc)[:200]
    return base


@router.get("/backups")
async def list_backups(
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    # CodeX P1: `status` MUST be Annotated[..., Query()] — without the
    # explicit Query() marker, FastAPI sees `list[str] | None = None`
    # and registers it as a REQUEST BODY param (FastAPI default for
    # list types is body, not query). Result: ?status=uploaded had no
    # effect in production. Annotated[..., Query()] keeps the direct-
    # call default as None (so tests passing user/db work) AND
    # registers `?status=X&status=Y` as a multi-value query.
    #
    # Simpler `str | int` defaults auto-detect as query without the
    # annotation; only list-typed params need it explicitly.
    status: Annotated[list[str] | None, Query()] = None,
    user_id: str | None = None,
    job_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List BackupRecord rows. Admin scope — sees all users' backups.

    Filters: status (multi), user_id, job_id. Pagination via limit/offset.
    Order: created_at DESC."""
    _require_admin(user)
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail='limit must be 1..500')
    if offset < 0:
        raise HTTPException(status_code=400, detail='offset must be >= 0')

    stmt = select(BackupRecord)
    if isinstance(status, list) and status:
        stmt = stmt.where(BackupRecord.status.in_(status))
    if isinstance(user_id, str) and user_id:
        try:
            stmt = stmt.where(BackupRecord.user_id == _uuid.UUID(user_id))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"非法 user_id: {user_id!r}")
    if isinstance(job_id, str) and job_id:
        stmt = stmt.where(BackupRecord.job_id == job_id)

    # Total count for pagination UI (with the same filters).
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(desc(BackupRecord.created_at)).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "items": [_serialize_backup_record(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/backups/{backup_id}/manifest")
async def get_backup_manifest(
    backup_id: str = PathParam(...),
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return manifest_json of a single BackupRecord. Admin can read any.

    manifest_json may be large (file_inventory grows with project_dir
    size); separate endpoint keeps the list response light."""
    _require_admin(user)
    try:
        br_uuid = _uuid.UUID(backup_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"非法 backup_id: {backup_id!r}")

    row = (await db.execute(
        select(
            BackupRecord.id, BackupRecord.manifest_json, BackupRecord.status,
        ).where(BackupRecord.id == br_uuid)
    )).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="BackupRecord 不存在")
    manifest = row.manifest_json
    if isinstance(manifest, str):
        import json as _json
        manifest = _json.loads(manifest)
    return {
        "backup_id": str(row.id),
        "status": row.status,
        "manifest": manifest or {},
    }


# --- enqueue endpoints: POST /backups, /backups/batch, /restores ---


async def _enqueue_pan_task(
    db: AsyncSession,
    *,
    user_id: _uuid.UUID,
    job_id: str,
    task_type: str,
) -> str:
    """Create a BackgroundTask + dispatch the executor coroutine via the
    shared `pan._enqueue.enqueue_pan_task` helper.

    CodeX P0-1 unification: archive_scanner and stale_reaper go through
    the same path so all three callers actually launch executors (not
    just create pending BackgroundTask rows that would be marked
    'failed' by recover_stale on next gateway restart).
    """
    from pan._enqueue import enqueue_pan_task
    try:
        return await enqueue_pan_task(
            db, user_id=user_id, job_id=job_id, task_type=task_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/backups", status_code=202)
async def create_backup(
    body: BackupCreateRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Enqueue a single backup task. Returns 202 + task_id."""
    admin = _require_admin(user)
    # Validate job exists + status=succeeded + admin owns it.
    job = (await db.execute(
        select(Job.status, Job.user_id)
        .where(Job.user_id == admin.id, Job.job_id == body.job_id)
    )).one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {body.job_id!r}")
    if job.status != 'succeeded':
        raise HTTPException(
            status_code=412,
            detail=f"任务状态 {job.status!r},需 'succeeded' 才能备份",
        )
    # Credentials must be active.
    await _fetch_admin_credentials(db, admin.id, require_active=True)
    task_id = await _enqueue_pan_task(
        db, user_id=admin.id, job_id=body.job_id, task_type='pan_backup',
    )
    return {"task_id": task_id, "job_id": body.job_id, "status": "pending"}


@router.post("/backups/batch", status_code=202)
async def create_backup_batch(
    body: BatchBackupRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Enqueue multiple backup tasks. Per-job validation: succeeded jobs
    succeed, others reported in failed[]. Active credentials checked once.
    Each job's executor takes its own advisory lock — no batch txn."""
    admin = _require_admin(user)
    await _fetch_admin_credentials(db, admin.id, require_active=True)

    succeeded: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for jid in body.job_ids:
        job = (await db.execute(
            select(Job.status)
            .where(Job.user_id == admin.id, Job.job_id == jid)
        )).one_or_none()
        if job is None:
            failed.append({"job_id": jid, "reason": "任务不存在"})
            continue
        if job.status != 'succeeded':
            failed.append({
                "job_id": jid,
                "reason": f"状态 {job.status!r},需 'succeeded'",
            })
            continue
        try:
            task_id = await _enqueue_pan_task(
                db, user_id=admin.id, job_id=jid, task_type='pan_backup',
            )
            succeeded.append({"job_id": jid, "task_id": task_id})
        except HTTPException as exc:
            failed.append({"job_id": jid, "reason": str(exc.detail)})
    return {"succeeded": succeeded, "failed": failed}


@router.post("/restores", status_code=202)
async def create_restore(
    body: RestoreRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Enqueue a restore task. Job must be archived, with an 'uploaded'
    BackupRecord at the current edit_generation."""
    admin = _require_admin(user)
    job = (await db.execute(
        select(Job.status, Job.edit_generation)
        .where(Job.user_id == admin.id, Job.job_id == body.job_id)
    )).one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {body.job_id!r}")
    if job.status != 'archived':
        raise HTTPException(
            status_code=412,
            detail=f"任务状态 {job.status!r},需 'archived' 才能恢复",
        )
    # Validate at least one matching backup exists.
    has_backup = (await db.execute(
        select(BackupRecord.id).where(
            BackupRecord.user_id == admin.id,
            BackupRecord.job_id == body.job_id,
            BackupRecord.job_edit_generation == job.edit_generation,
            BackupRecord.status == 'uploaded',
        ).limit(1)
    )).scalar_one_or_none()
    if has_backup is None:
        raise HTTPException(
            status_code=412,
            detail=(
                f"未找到当前 generation={job.edit_generation} 的可恢复 backup_record"
            ),
        )
    await _fetch_admin_credentials(db, admin.id, require_active=True)
    task_id = await _enqueue_pan_task(
        db, user_id=admin.id, job_id=body.job_id, task_type='pan_restore',
    )
    return {"task_id": task_id, "job_id": body.job_id, "status": "pending"}


# --- DELETE credentials (disconnect) ---


@router.delete("/credentials", status_code=204)
async def disconnect_credentials(
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Soft-disconnect: flip PanCredentials.status to 'revoked'. The row
    stays for audit; admin can re-connect via /connect → /callback."""
    admin = _require_admin(user)
    result = await db.execute(
        update(PanCredentials)
        .where(
            PanCredentials.user_id == admin.id,
            PanCredentials.provider == 'baidu_pan',
        )
        .values(status='revoked')
    )
    await db.commit()
    # rowcount == 0 (no creds row) is fine — disconnecting nothing is
    # idempotent. Return 204 regardless.
    return Response(status_code=204)


# --- DELETE backup with 412 guard ---


@router.delete("/backups/{backup_id}")
async def delete_backup(
    backup_id: str = PathParam(...),
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Soft-delete a BackupRecord per spec §6 protection logic.

    Refuses (412) if this is the unique 'uploaded' backup for an
    archived job at the current edit_generation. Otherwise:
      1. Try to delete the remote tar.gz (best-effort; log + continue on
         failure — orphan_cleanup picks it up later).
      2. UPDATE BackupRecord.status='deleted' (soft delete keeps the row
         for audit).
    Idempotent for already-'deleted' rows (returns 204 without remote
    delete).
    """
    return await _delete_backup_impl(
        backup_id=backup_id, user=user, db=db,
        client_factory=_client_factory,
    )


async def _delete_backup_impl(
    *,
    backup_id: str,
    user: User | None,
    db: AsyncSession,
    client_factory,
) -> Response:
    """Real DELETE handler with injection seam for tests."""
    admin = _require_admin(user)
    try:
        br_uuid = _uuid.UUID(backup_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"非法 backup_id: {backup_id!r}")

    br = (await db.execute(
        select(BackupRecord).where(BackupRecord.id == br_uuid)
    )).scalar_one_or_none()
    if br is None:
        raise HTTPException(status_code=404, detail="BackupRecord 不存在")

    if br.status == 'deleted':
        # Idempotent.
        return Response(status_code=204)

    # CodeX P0: refuse DELETE while a backup/restore is in flight.
    # 'uploading' = backup_executor mid-write (no remote tar to delete
    # yet OR a partial tar that's about to be replaced). 'restoring' =
    # restore_executor reading the remote tar; deleting it now would
    # break the in-flight restore + the executor's failure-path
    # rollback to 'uploaded' would leave the row pointing at a
    # remote_path that no longer exists.
    if br.status in ('uploading', 'restoring'):
        raise HTTPException(
            status_code=409,
            detail=(
                f"无法删除处于 {br.status!r} 状态的备份;请等待当前操作结束后再试。"
            ),
        )

    # 412 guard: protect the unique recoverable copy of an archived job.
    if br.status == 'uploaded':
        job = (await db.execute(
            select(Job.status, Job.edit_generation).where(
                Job.user_id == br.user_id, Job.job_id == br.job_id,
            )
        )).one_or_none()
        if (
            job is not None
            and job.status == 'archived'
            and job.edit_generation == br.job_edit_generation
        ):
            sibling_count = (await db.execute(
                select(func.count())
                .select_from(BackupRecord)
                .where(
                    BackupRecord.user_id == br.user_id,
                    BackupRecord.job_id == br.job_id,
                    BackupRecord.job_edit_generation == br.job_edit_generation,
                    BackupRecord.status == 'uploaded',
                    BackupRecord.id != br_uuid,
                )
            )).scalar_one()
            if sibling_count == 0:
                raise HTTPException(
                    status_code=412,
                    detail=(
                        "拒绝:该 backup 是 archived 任务的唯一可恢复副本。"
                        "先 restore 后再 delete,或先备份新副本。"
                    ),
                )

    # Try remote delete first — failure tolerated (orphan_cleanup retries).
    cred = await _fetch_admin_credentials(db, br.user_id, require_active=False)
    if cred is not None and cred.status == 'active' and br.remote_path:
        try:
            access_token = decrypt_token(cred.access_token_encrypted)
            client = client_factory()
            await asyncio.to_thread(
                client.delete, br.remote_path, access_token=access_token,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "admin_pan delete remote tar failed (br=%s, path=%s): %s — "
                "PG soft-delete continues; orphan_cleanup will retry",
                br_uuid, br.remote_path, exc,
            )

    # Soft delete in PG.
    await db.execute(
        update(BackupRecord)
        .where(BackupRecord.id == br_uuid)
        .values(status='deleted')
    )
    await db.commit()
    logger.info("admin_pan: soft-deleted backup_record %s", br_uuid)
    return Response(status_code=204)
