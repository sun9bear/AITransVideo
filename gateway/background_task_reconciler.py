"""Startup-time pending-task reconciler.

Closes a known architectural gap in the 2-step task enqueue pattern
shared by ``background_task_api.create_task_endpoint`` and
``pan._enqueue.enqueue_pan_task``:

  Step 1: ``queue.create_task(...) + db.commit()`` → BackgroundTask row
          at ``status='pending'``.
  Step 2: ``asyncio.create_task(executor(...))`` → executor actually runs.

If the gateway process crashes between Step 1 and Step 2, the row sits
at ``pending`` forever with no worker. ``background_task_queue.recover_stale``
used to convert those orphans to ``failed`` on next startup; this module
now picks them up FIRST and re-launches their executors so the user's
job actually completes instead of dying silently.

Contract with ``recover_stale`` (CodeX 2026-05-19): the reconciler runs
BEFORE ``recover_stale`` in ``main.py`` lifespan. For each pending row
within the cutoff window, it calls ``queue.mark_running`` BEFORE launching,
which both (a) matches what the executor would do as its first step, and
(b) bumps ``updated_at`` past the startup timestamp so ``recover_stale``'s
``updated_at < cutoff`` filter leaves it alone. Ancient pending rows
(older than cutoff_hours) and any pending rows the reconciler can't
dispatch fall through to ``recover_stale`` for the usual ``failed``
transition.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import background_task_queue as queue
from background_task_models import BackgroundTask
from models import Job

logger = logging.getLogger(__name__)


# Module-level so tests can monkeypatch this single attribute to record
# launches without scheduling on the real event loop. Mirrors the pattern
# in gateway/pan/_enqueue.py::_launch_coroutine.
def _launch_pending_executor(coro, name: str):
    """asyncio.create_task wrapper. Tests monkeypatch this to record
    launches without actually scheduling on the event loop."""
    return asyncio.create_task(coro, name=name)


async def reconcile_pending_tasks(
    db: AsyncSession,
    *,
    cutoff_hours: int = 24,
    now: datetime | None = None,
) -> dict[str, int]:
    """Re-launch executors for pending BackgroundTask rows after restart.

    Selects ``status='pending'`` rows created within the last
    ``cutoff_hours`` (default 24h). For each row:

      1. Defensive duplicate-running check (the partial unique index
         ``idx_bg_tasks_active`` already prevents this state, but if a
         migration / manual SQL ever left one behind, skip the launch).
      2. Validate ``task_type`` is in ``TASK_EXECUTORS``. Unknown types
         get ``mark_failed`` with a Chinese error string (matches the
         UI's tolerance for the existing ``Gateway 重启`` message).
      3. Resolve the Job row to recover ``project_dir``. Missing Job →
         ``mark_failed``.
      4. Atomically ``mark_running`` BEFORE launching. This bumps
         ``updated_at`` past the startup cutoff so the subsequent
         ``recover_stale`` sweep leaves the row alone.
      5. ``asyncio.create_task`` via the module-level launch helper
         (test-monkeypatchable).

    Pending rows older than ``cutoff_hours`` are left alone — ``recover_stale``
    will sweep them in its normal pass.

    Returns ``{'launched', 'failed', 'skipped_duplicate', 'total'}``
    counters so the lifespan logger can summarize the pass.

    The ``now`` kwarg is for deterministic tests; production always uses
    ``datetime.now(timezone.utc)``.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cutoff_hours)

    rows = (
        await db.execute(
            select(BackgroundTask)
            .where(BackgroundTask.status == "pending")
            .where(BackgroundTask.created_at > cutoff)
            .order_by(BackgroundTask.created_at.asc())
        )
    ).scalars().all()

    launched = 0
    failed = 0
    skipped_duplicate = 0

    # Lazy import — TASK_EXECUTORS pulls heavy modules (httpx, pan.*) we
    # don't want loaded at module-import time, especially for the test
    # harness which stubs ``database``.
    from background_task_executors import TASK_EXECUTORS

    for row in rows:
        # 1. Defensive duplicate-running guard. The partial unique index
        # makes this physically impossible, but if it ever happens (manual
        # SQL, migration accident) launching a second executor would race
        # the first one to write the terminal status.
        dup = (
            await db.execute(
                select(BackgroundTask.id).where(
                    BackgroundTask.job_id == row.job_id,
                    BackgroundTask.task_type == row.task_type,
                    BackgroundTask.params_fingerprint == row.params_fingerprint,
                    BackgroundTask.status == "running",
                    BackgroundTask.id != row.id,
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            logger.warning(
                "reconciler: skip task %s — duplicate fingerprint already running (%s)",
                row.id, dup,
            )
            skipped_duplicate += 1
            continue

        # 2. Unknown task_type — terminal failure with a clear reason.
        executor = TASK_EXECUTORS.get(row.task_type)
        if executor is None:
            logger.warning(
                "reconciler: mark task %s failed — unknown task_type %r",
                row.id, row.task_type,
            )
            await queue.mark_failed(db, row.id, f"未知任务类型: {row.task_type}")
            failed += 1
            continue

        # 2b. Pan feature gate (2026-05-26 postmortem P0a v2, Codex
        # 2nd-round finding). The HTTP routers in gateway/pan/admin_api.py
        # and gateway/pan/auth.py now refuse new requests when
        # ``AVT_ENABLE_PAN_BACKUP=false`` (via require_pan_enabled).
        # But the reconciler bypasses the HTTP layer entirely — it walks
        # ``BackgroundTask`` rows directly and re-launches executors via
        # TASK_EXECUTORS. So pending pan_backup / pan_restore /
        # pan_residue_cleanup rows left over from before the flag was
        # turned off would still re-run on every gateway restart.
        #
        # Mark them failed (not skipped/pending) so reconciler doesn't
        # rescan them every restart. If the operator turns the flag back
        # on later, those tasks stay failed by design — they need to be
        # explicitly re-enqueued via the admin UI to reflect a deliberate
        # decision. Auto-resume after a feature was force-disabled defeats
        # the safety value of the flag.
        from config import settings as _settings  # noqa: PLC0415 — lazy
        if (
            not _settings.enable_pan_backup
            and (row.task_type or "").startswith("pan_")
        ):
            logger.warning(
                "reconciler: mark task %s failed — pan feature disabled "
                "(AVT_ENABLE_PAN_BACKUP=false); task_type=%s job=%s",
                row.id, row.task_type, row.job_id,
            )
            await queue.mark_failed(
                db, row.id,
                "网盘备份功能已禁用 (AVT_ENABLE_PAN_BACKUP=false)，"
                f"重启时跳过 pending {row.task_type}",
            )
            failed += 1
            continue

        # 3. Resolve Job → project_dir. Missing Job is fatal; missing
        # project_dir is tolerated (pan executors ignore it; materials_pack
        # / generate_video will mark themselves failed inside the executor
        # if they actually need a real dir).
        job = (
            await db.execute(
                select(Job).where(Job.job_id == row.job_id)
            )
        ).scalar_one_or_none()
        if job is None:
            logger.warning(
                "reconciler: mark task %s failed — Job %s not found",
                row.id, row.job_id,
            )
            await queue.mark_failed(db, row.id, f"Job {row.job_id} 不存在")
            failed += 1
            continue

        project_dir = Path(job.project_dir) if job.project_dir else Path("/")

        # 4. Atomically promote pending → running BEFORE launching, so
        # recover_stale's cutoff filter leaves it alone. The executor's
        # own mark_running call inside the coroutine becomes a no-op
        # re-write of the same status (idempotent).
        try:
            await queue.mark_running(db, row.id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "reconciler: failed to mark task %s running; skipping launch",
                row.id,
            )
            continue

        # 5. Launch executor coroutine.
        coro = executor(
            task_id=row.id,
            job_id=row.job_id,
            project_dir=project_dir,
            params=row.params or {},
        )
        _launch_pending_executor(
            coro, name=f"bgtask-reconcile-{row.task_type}-{row.id}",
        )
        logger.info(
            "reconciler: launched task=%s type=%s job=%s",
            row.id, row.task_type, row.job_id,
        )
        launched += 1

    return {
        "launched": launched,
        "failed": failed,
        "skipped_duplicate": skipped_duplicate,
        "total": len(rows),
    }


__all__ = ["reconcile_pending_tasks", "_launch_pending_executor"]
