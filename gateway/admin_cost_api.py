"""PR#3C-P3-b — Gateway admin endpoint that serves smart_cost_summary.json.

Per decision log §2 (docs/plans/2026-05-15-smart-mvp-p3-decisions.md):
admin-only display of per-job cost data via
``GET /api/admin/jobs/{job_id}/cost``. User-facing workspace MUST
NOT show this data; this admin route is the single authoritative
read path.

The cost summary JSON is written by ``pipeline.process._emit_smart_cost_summary``
at smart-job terminal. This endpoint just reads the file from the
job's project_dir and serves it unchanged.

Codex Q2 (admin-only display) + decision log §2 (no cost data on
user-facing workspace) are pinned by:
  - This endpoint living on the ``/api/admin`` prefix (Caddy block)
  - ``_require_admin`` role check
  - AST guard test ``test_no_cost_data_leak_in_workspace_frontend``
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from admin_auth import _require_admin
from auth import get_current_user
from database import get_db
from models import Job, User


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin-cost"])

_COST_SUMMARY_FILENAME = "smart_cost_summary.json"


def _json(status_code: int, body: dict) -> Response:
    return Response(
        content=json.dumps(body, ensure_ascii=False),
        status_code=status_code,
        headers={"content-type": "application/json"},
    )


@router.get("/jobs/{job_id}/cost")
async def get_smart_cost_summary(
    job_id: str,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Return the contents of ``audit/smart_cost_summary.json`` for
    the given job. Admin-only.

    Returns:
      200 + JSON payload (verbatim file contents) on success
      404 ``job_not_found`` when job_id doesn't exist
      404 ``cost_summary_not_found`` when the file isn't on disk
          (non-smart job, pre-P3-b job, or job that handed off
          before reaching terminal)
      403 (via _require_admin raising HTTPException) for non-admin
    """
    _require_admin(user)

    # Look up job to get project_dir.
    result = await db.execute(
        select(Job).where(Job.job_id == job_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        return _json(404, {"error": "job_not_found", "job_id": job_id})

    project_dir = getattr(job, "project_dir", None)
    if not project_dir:
        return _json(
            404,
            {"error": "cost_summary_not_found", "reason": "project_dir_missing"},
        )

    cost_path = Path(project_dir) / "audit" / _COST_SUMMARY_FILENAME
    if not cost_path.exists():
        return _json(
            404,
            {
                "error": "cost_summary_not_found",
                "reason": "file_not_written",
                "job_id": job_id,
            },
        )

    try:
        payload = json.loads(cost_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.exception(
            "Failed to read cost_summary for job %s: %s", job_id, exc,
        )
        return _json(
            500,
            {"error": "cost_summary_read_failed", "detail": str(exc)[:200]},
        )

    return _json(200, payload)
