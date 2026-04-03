"""Label task queue — DB-backed async labeling with progress tracking.

Tasks are persisted in the ``label_tasks`` PostgreSQL table. Survives
Gateway restarts.  On startup, any ``running`` tasks are marked ``failed``
(stale) since the background coroutine that was executing them is gone.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from label_task_models import LabelTask

logger = logging.getLogger(__name__)


async def create_task(
    db: AsyncSession,
    task_type: str,
    voice_ids: list[str],
    label_type: str,
) -> str:
    """Create a new task row and return its ID."""
    task_id = uuid.uuid4().hex[:12]
    task = LabelTask(
        id=task_id,
        task_type=task_type,
        label_type=label_type,
        voice_ids=voice_ids,
        status="pending",
        progress_completed=0,
        progress_total=len(voice_ids),
        current_batch=0,
    )
    db.add(task)
    await db.flush()
    return task_id


async def get_task(db: AsyncSession, task_id: str) -> dict[str, Any] | None:
    result = await db.execute(select(LabelTask).where(LabelTask.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        return None
    return _serialize(task)


async def list_tasks(db: AsyncSession, limit: int = 20) -> list[dict[str, Any]]:
    result = await db.execute(
        select(LabelTask).order_by(LabelTask.created_at.desc()).limit(limit)
    )
    return [_serialize(t) for t in result.scalars().all()]


async def update_progress(db: AsyncSession, task_id: str, completed: int, current_batch: int) -> None:
    await db.execute(
        update(LabelTask)
        .where(LabelTask.id == task_id)
        .values(
            status="running",
            progress_completed=completed,
            current_batch=current_batch,
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def complete_task(db: AsyncSession, task_id: str, result: dict[str, Any]) -> None:
    await db.execute(
        update(LabelTask)
        .where(LabelTask.id == task_id)
        .values(
            status="completed",
            result=result,
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def fail_task(db: AsyncSession, task_id: str, error: str) -> None:
    await db.execute(
        update(LabelTask)
        .where(LabelTask.id == task_id)
        .values(
            status="failed",
            error=error,
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def recover_stale_tasks(db: AsyncSession) -> int:
    """Mark any 'running' or 'pending' tasks as 'failed' on startup.

    These are leftovers from a Gateway restart mid-execution.
    """
    result = await db.execute(
        update(LabelTask)
        .where(LabelTask.status.in_(["running", "pending"]))
        .values(
            status="failed",
            error="Gateway 重启，任务中断",
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return result.rowcount  # type: ignore[return-value]


def _serialize(task: LabelTask) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "task_type": task.task_type,
        "label_type": task.label_type,
        "voice_ids": task.voice_ids,
        "status": task.status,
        "progress": {
            "completed": task.progress_completed,
            "total": task.progress_total,
            "current_batch": task.current_batch,
        },
        "result": task.result,
        "error": task.error,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }
