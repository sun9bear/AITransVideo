"""Materials pack download — legacy Gateway-native sync endpoint.

Streams a zip of user-selected materials in-request using a SpooledTemporaryFile.
Kept as a compatibility fallback alongside the new async task-based flow
(``gateway/background_task_api.py``), which persists the zip to disk and
serves it via a task-scoped download endpoint.

Endpoint:
- POST /api/jobs/{job_id}/materials-pack
- GET  /api/jobs/{job_id}/materials-pack?items=...

New code should prefer the task-based flow. This endpoint will stay until
all frontend callers migrate.
"""
from __future__ import annotations

import logging
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_auth
from csrf import require_same_origin_state_change
from database import get_db
from materials_pack_common import (
    MAX_ZIP_SIZE_BYTES,
    collect_files_for_items,
    load_artifact_index,
)
from models import Job, User

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_same_origin_state_change)])


class MaterialsPackRequest(BaseModel):
    items: list[str]


@router.post("/api/jobs/{job_id}/materials-pack")
@router.get("/api/jobs/{job_id}/materials-pack")
async def materials_pack_endpoint(
    job_id: str,
    request: Request,
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    items: str | None = None,
) -> StreamingResponse:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    # Parse items from query param (GET) or JSON body (POST)
    item_list: list[str] = []
    if request.method == "GET" and items:
        # GET: ?items=source_video,dubbed_video,subtitles
        item_list = [s.strip() for s in items.split(",") if s.strip()]
    elif request.method == "POST":
        try:
            body_data = await request.json()
            raw_items = body_data.get("items", [])
            if isinstance(raw_items, list):
                item_list = [str(s).strip() for s in raw_items if s]
        except Exception:
            raise HTTPException(status_code=400, detail="请求格式错误")

    if not item_list:
        raise HTTPException(status_code=400, detail="请选择至少一项素材")

    # Verify job ownership
    result = await db.execute(select(Job).where(Job.job_id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.user_id != user.id and getattr(user, "role", "user") != "admin":
        raise HTTPException(status_code=403, detail="无权访问")

    project_dir_str = job.project_dir
    if not project_dir_str:
        raise HTTPException(status_code=404, detail="项目目录不存在")
    project_dir = Path(project_dir_str)
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail="项目目录不存在")

    artifact_index = load_artifact_index(project_dir)

    files_to_pack, total_size = collect_files_for_items(
        project_dir=project_dir,
        artifact_index=artifact_index,
        item_list=item_list,
    )

    if not files_to_pack:
        raise HTTPException(status_code=404, detail="没有可打包的文件")

    if total_size > MAX_ZIP_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="素材包过大，请减少选择项")

    # Write zip to SpooledTemporaryFile (memory < 10MB, disk beyond)
    spool = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, file_path in files_to_pack:
            zf.write(file_path, arcname)
    spool.seek(0)

    zip_filename = f"materials_{job_id[:12]}.zip"

    def _iter_spool():
        try:
            while True:
                chunk = spool.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            spool.close()

    return StreamingResponse(
        _iter_spool(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_filename}"',
        },
    )
