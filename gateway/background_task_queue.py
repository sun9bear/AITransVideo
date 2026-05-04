"""Background task queue — DB-backed async export tasks.

Used by Export Tasks v1 for ``materials_pack`` (zip) and ``generate_video``
(FFmpeg mux coordination). See docs/plans/2026-04-16-background-task-system-plan.md.

Design notes:
- Single-process ``asyncio.create_task`` execution. Gateway restart marks
  pending/running tasks as failed (stale recovery). Users retry manually.
- ``params_fingerprint`` enables two things:
  (1) Dedupe: creating a task with the same (job_id, task_type, fingerprint)
      while an active one exists returns the existing task_id.
  (2) State restore: the ``latest_active`` query returns a running task only
      when the fingerprint matches — different params = different task.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from background_task_models import BackgroundTask

logger = logging.getLogger(__name__)


def compute_params_fingerprint(params: dict[str, Any]) -> str:
    """Stable sha256 hex digest of params.

    Serialization is deterministic AND matches the frontend's
    ``computeParamsFingerprint`` (see ``frontend-next/src/lib/api/downloads.ts``):
    - ``sort_keys=True`` — alphabetical key order
    - ``ensure_ascii=False`` — unicode content passes through
    - ``separators=(',', ':')`` — NO spaces (matches JS ``JSON.stringify``)
    """
    canonical = json.dumps(
        params, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def create_task(
    db: AsyncSession,
    *,
    job_id: str,
    user_id: uuid.UUID,
    task_type: str,
    params: dict[str, Any],
) -> tuple[str, bool]:
    """Create a task, or return an existing active one (same fingerprint).

    Returns ``(task_id, created)`` where ``created`` is False if we returned
    an existing active task.

    Dedupe is guaranteed atomically by the partial unique index
    ``idx_bg_tasks_active`` (see migration 014). We still do an optimistic
    pre-check for the common fast path, but rely on ``IntegrityError`` as
    the correctness barrier against concurrent creators.
    """
    fingerprint = compute_params_fingerprint(params)

    # Fast path: already an active task for same fingerprint
    existing = await _fetch_active(db, job_id=job_id, task_type=task_type, fingerprint=fingerprint)
    if existing is not None:
        return existing.id, False

    task_id = uuid.uuid4().hex[:12]
    task = BackgroundTask(
        id=task_id,
        job_id=job_id,
        user_id=user_id,
        task_type=task_type,
        params=params,
        params_fingerprint=fingerprint,
        status="pending",
    )
    db.add(task)
    try:
        await db.flush()
    except IntegrityError:
        # Another concurrent creator won the race against the partial unique
        # index. Roll back this transaction and return the existing row.
        await db.rollback()
        existing = await _fetch_active(
            db, job_id=job_id, task_type=task_type, fingerprint=fingerprint,
        )
        if existing is None:
            # Unique violation on some other column/constraint — not our race.
            raise
        return existing.id, False
    return task_id, True


async def get_task(
    db: AsyncSession,
    *,
    task_id: str,
    user_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """Fetch a task by id. When ``user_id`` is given, enforces ownership."""
    stmt = select(BackgroundTask).where(BackgroundTask.id == task_id)
    if user_id is not None:
        stmt = stmt.where(BackgroundTask.user_id == user_id)
    result = await db.execute(stmt)
    task = result.scalar_one_or_none()
    return _serialize(task) if task else None


async def get_latest_active(
    db: AsyncSession,
    *,
    job_id: str,
    user_id: uuid.UUID,
    task_type: str,
    params_fingerprint: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest pending/running task only (for polling)."""
    return await _get_latest(
        db,
        job_id=job_id,
        user_id=user_id,
        task_type=task_type,
        params_fingerprint=params_fingerprint,
        statuses=("pending", "running"),
    )


async def get_latest(
    db: AsyncSession,
    *,
    job_id: str,
    user_id: uuid.UUID,
    task_type: str,
    params_fingerprint: str | None = None,
    include_terminal: bool = True,
) -> dict[str, Any] | None:
    """Return the latest task regardless of status (for state restore on page load).

    With ``include_terminal=True`` (default) this returns ``completed`` and
    ``failed`` rows too, so the UI can restore the "素材包可下载" / "打包失败"
    state after the user refreshed or reopened the browser. Pass False to
    get the same semantics as ``get_latest_active``.
    """
    if include_terminal:
        statuses = ("pending", "running", "completed", "failed")
    else:
        statuses = ("pending", "running")
    return await _get_latest(
        db,
        job_id=job_id,
        user_id=user_id,
        task_type=task_type,
        params_fingerprint=params_fingerprint,
        statuses=statuses,
    )


async def _get_latest(
    db: AsyncSession,
    *,
    job_id: str,
    user_id: uuid.UUID,
    task_type: str,
    params_fingerprint: str | None,
    statuses: tuple[str, ...],
) -> dict[str, Any] | None:
    stmt = (
        select(BackgroundTask)
        .where(BackgroundTask.job_id == job_id)
        .where(BackgroundTask.user_id == user_id)
        .where(BackgroundTask.task_type == task_type)
        .where(BackgroundTask.status.in_(statuses))
        .order_by(BackgroundTask.updated_at.desc())
        .limit(1)
    )
    if params_fingerprint is not None:
        stmt = stmt.where(BackgroundTask.params_fingerprint == params_fingerprint)
    result = await db.execute(stmt)
    task = result.scalar_one_or_none()
    return _serialize(task) if task else None


async def _fetch_active(
    db: AsyncSession,
    *,
    job_id: str,
    task_type: str,
    fingerprint: str,
) -> BackgroundTask | None:
    stmt = (
        select(BackgroundTask)
        .where(BackgroundTask.job_id == job_id)
        .where(BackgroundTask.task_type == task_type)
        .where(BackgroundTask.params_fingerprint == fingerprint)
        .where(BackgroundTask.status.in_(["pending", "running"]))
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def mark_running(db: AsyncSession, task_id: str) -> None:
    await db.execute(
        update(BackgroundTask)
        .where(BackgroundTask.id == task_id)
        .values(status="running", updated_at=datetime.now(timezone.utc))
    )
    await db.commit()


async def update_progress(db: AsyncSession, task_id: str, progress: dict[str, Any]) -> None:
    await db.execute(
        update(BackgroundTask)
        .where(BackgroundTask.id == task_id)
        .values(
            status="running",
            progress=progress,
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def mark_completed(db: AsyncSession, task_id: str, result: dict[str, Any]) -> None:
    await db.execute(
        update(BackgroundTask)
        .where(BackgroundTask.id == task_id)
        .values(
            status="completed",
            result=result,
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def mark_failed(db: AsyncSession, task_id: str, error: str) -> None:
    await db.execute(
        update(BackgroundTask)
        .where(BackgroundTask.id == task_id)
        .values(
            status="failed",
            error=error,
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def invalidate_materials_pack_for_job(
    db: AsyncSession,
    *,
    job_id: str,
    reason: str = "视频已修改，旧素材包已失效",
    now: datetime | None = None,
) -> int:
    """Invalidate every non-terminal-stale ``materials_pack`` row for a job.

    Trigger: editing/commit overwrite. Once the source job's video content
    has been re-published with edits, any pre-edit ``materials_pack`` zip
    is stale (it captured the old SRT / old audio / old caption text).
    Resetting state is a UX choice — better to let the user click "重新打包"
    against the freshly published artifacts than to leave a stale "可下载"
    affordance pointing at pre-edit content. Mirrors the Jianying-draft
    invalidation in ``services.jobs.editing_commit._invalidate_jianying_
    draft_on_commit`` (Job-API layer); materials_pack lives in Gateway DB
    so the hook lives here in Gateway.

    Behavior per status:
      - ``'completed'`` → unlink ``result.zip_path`` (if present) and
        transition to ``'expired'``. Matches the
        ``cleanup_expired_pack_zips`` contract; the frontend already
        treats ``'expired'`` as "zip is gone, click to re-pack".
      - ``'pending'`` / ``'running'`` → transition to ``'failed'`` with
        ``error=reason``. The packing in flight was capturing pre-edit
        content; failing it both surfaces "did not finish, retry" to the
        user and unblocks the partial unique index for a fresh pack.
        (Best-effort: an in-flight executor coroutine may still complete
        and call ``mark_completed`` after we flip — that's a benign race;
        the next overwrite hits this helper again and the eventually-
        completed row gets expired then.)
      - ``'failed'`` / ``'expired'`` → leave alone (already terminal in a
        way the UI handles).

    Does NOT call ``await db.commit()`` — caller owns the transaction.
    Designed to be called from inside ``_apply_editing_commit_gateway_
    side`` where ``_editing_transition_with_lock`` commits at the end.

    Returns the number of rows transitioned this pass.
    """
    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        select(BackgroundTask).where(
            BackgroundTask.job_id == job_id,
            BackgroundTask.task_type == "materials_pack",
            BackgroundTask.status.in_(("pending", "running", "completed")),
        )
    )
    tasks = result.scalars().all()
    affected = 0
    for task in tasks:
        if task.status == "completed":
            result_data = task.result or {}
            zip_path_str = (
                result_data.get("zip_path")
                if isinstance(result_data, dict) else None
            )
            if isinstance(zip_path_str, str) and zip_path_str:
                try:
                    Path(zip_path_str).unlink()
                except FileNotFoundError:
                    # Already gone — status flip still proceeds so the
                    # UI reflects reality.
                    pass
                except OSError as exc:
                    # Permission / busy — log but DON'T skip the status
                    # flip; the row would otherwise stay 'completed' and
                    # mislead the user into re-downloading a now-stale zip.
                    # The orphaned file is small relative to total disk,
                    # and the periodic cleanup won't re-claim it (the row
                    # is already 'expired'). Operator-cleanup territory.
                    logger.warning(
                        "invalidate_materials_pack_for_job: failed to "
                        "unlink %s: %s", zip_path_str, exc,
                    )
            task.status = "expired"
        else:
            # 'pending' / 'running'
            task.status = "failed"
            task.error = reason
        task.updated_at = now
        affected += 1
    return affected


async def cleanup_expired_pack_zips(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    retention_hours: int = 24,
) -> int:
    """Delete ``materials_pack`` zip files older than ``retention_hours`` and
    transition their task status to ``'expired'``.

    Disk pressure on US host sits at 82% (118 GB / 150 GB) and each long
    podcast task produces ~3-5 GB of packaged zip. 24h is a reasonable
    middle ground: users have a full day to download, after which the
    zip becomes dead weight. Re-packing is cheap (~seconds for a 3 GB
    source already on local disk) — far cheaper than indefinite storage.

    Contract:
      - Only touches rows where ``task_type='materials_pack'`` AND
        ``status='completed'`` AND ``updated_at < now - retention_hours``.
      - Unlinks ``result.zip_path`` (tolerates missing file — still
        transitions status so the UI reflects reality).
      - Sets status to ``'expired'``; frontend treats this as "zip has
        been deleted, click to re-pack".
      - Idempotent: a second pass finds the already-expired rows in a
        different status and skips them.

    Returns the number of tasks transitioned to ``'expired'`` this pass.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=retention_hours)
    result = await db.execute(
        select(BackgroundTask).where(
            BackgroundTask.task_type == "materials_pack",
            BackgroundTask.status == "completed",
            BackgroundTask.updated_at < cutoff,
        )
    )
    tasks = result.scalars().all()
    expired_count = 0
    for task in tasks:
        result_data = task.result or {}
        zip_path_str = result_data.get("zip_path") if isinstance(result_data, dict) else None
        if isinstance(zip_path_str, str) and zip_path_str:
            try:
                Path(zip_path_str).unlink()
            except FileNotFoundError:
                # Already gone — status transition still proceeds.
                pass
            except OSError as exc:
                # Permission issue / busy file — log and skip the status
                # flip so the next pass retries. Surfaces via repeated
                # entries in the log if the issue persists.
                logger.warning(
                    "cleanup_expired_pack_zips: failed to unlink %s: %s",
                    zip_path_str, exc,
                )
                continue
        task.status = "expired"
        task.updated_at = now
        expired_count += 1
    if expired_count > 0:
        await db.commit()
    return expired_count


async def recover_stale(db: AsyncSession) -> int:
    """Mark running/pending tasks as failed on startup.

    Both states are fatal after restart: ``pending`` means the
    ``asyncio.create_task`` coroutine was never scheduled by the now-dead
    process; ``running`` means it was mid-execution and lost.
    """
    result = await db.execute(
        update(BackgroundTask)
        .where(BackgroundTask.status.in_(["running", "pending"]))
        .values(
            status="failed",
            error="Gateway 重启，任务中断",
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return result.rowcount  # type: ignore[return-value]


def _serialize(task: BackgroundTask) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "job_id": task.job_id,
        "task_type": task.task_type,
        "params": task.params,
        "params_fingerprint": task.params_fingerprint,
        "status": task.status,
        "progress": task.progress,
        "result": task.result,
        "error": task.error,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }
