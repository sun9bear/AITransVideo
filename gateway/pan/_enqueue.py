"""Shared enqueue+launch helper for pan BackgroundTasks (CodeX P0-1).

CodeX 2026-05-19 found that archive_scanner and stale_reaper (Pass 2)
only called `queue.create_task(...)` — they never fired
`asyncio.create_task(executor(...))`. The dispatcher path in
`background_task_api._launch_executor` IS the launch step; without
it, BackgroundTask rows sat at status='pending' and would be marked
'failed' by recover_stale on the next gateway restart. The archive
automation loop was effectively broken.

This module exposes a single helper used by:
  - gateway/pan/admin_api.py (admin endpoints)
  - gateway/pan/archive_scanner.py (daily cron)
  - gateway/pan/stale_reaper.py (Pass 2 residue_cleanup enqueue)

All three callers go through the same path: create the BackgroundTask
row, commit, then `asyncio.create_task(executor(...))` so the dispatcher
fires immediately.
"""
from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


logger = logging.getLogger(__name__)


# Module-level reference so tests can monkeypatch this single
# attribute to inspect (without patching the global `asyncio.create_task`,
# which would affect unrelated code under test).
def _launch_coroutine(coro, name: str):
    """asyncio.create_task wrapper. Tests monkeypatch this to record
    launches without actually scheduling on the event loop."""
    return asyncio.create_task(coro, name=name)


async def enqueue_pan_task(
    db: AsyncSession,
    *,
    user_id: _uuid.UUID,
    job_id: str,
    task_type: str,
    extra_params: dict[str, Any] | None = None,
) -> str:
    """Create a BackgroundTask row AND launch the executor coroutine.

    Returns task_id. Caller's session is committed inside (the task row
    needs to be visible to the spawned coroutine, which uses its own
    session).

    Raises ValueError on unknown task_type. Admin API callers translate
    that to HTTPException 400; scanner/reaper just log + skip.
    """
    # Lazy imports — TASK_EXECUTORS references heavy modules that we
    # don't want loaded at module-import time (especially for tests).
    from background_task_executors import TASK_EXECUTORS
    import background_task_queue as queue

    if task_type not in TASK_EXECUTORS:
        raise ValueError(f"unknown pan task_type: {task_type!r}")

    params: dict[str, Any] = {'user_id': str(user_id)}
    if extra_params:
        params.update(extra_params)

    task_id, _ = await queue.create_task(
        db, job_id=job_id, user_id=user_id,
        task_type=task_type, params=params,
    )
    await db.commit()

    executor = TASK_EXECUTORS[task_type]
    # Pan executors re-read project_dir from the Job row inside the
    # executor, so Path('/') here is a placeholder.
    _launch_coroutine(
        executor(
            task_id=task_id,
            job_id=job_id,
            project_dir=Path('/'),
            params=params,
        ),
        name=f"bgtask-{task_type}-{task_id}",
    )
    logger.info(
        "pan_enqueue: created+launched task=%s type=%s job=%s user=%s",
        task_id, task_type, job_id, user_id,
    )
    return task_id
