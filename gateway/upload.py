"""Gateway-native video upload endpoint.

Handles multipart/form-data directly in the Gateway process,
using the authenticated user context for path isolation.
Does NOT proxy to 8876.
"""
import asyncio
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9_\-]")
_UNSAFE_FILENAME_CHARS = re.compile(r"[^a-zA-Z0-9_\-.]")


def _safe_segment(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return "anonymous"
    result = _UNSAFE_CHARS.sub("_", cleaned)
    while ".." in result:
        result = result.replace("..", "_")
    return result


def _sanitize_filename(filename: str) -> str:
    cleaned = filename.strip()
    if not cleaned:
        return "unnamed"
    result = _UNSAFE_FILENAME_CHARS.sub("_", cleaned)
    while ".." in result:
        result = result.replace("..", "_")
    return result


def _build_upload_path(user_id: str, upload_id: str, filename: str) -> str:
    """Build user-isolated upload path: uploads/<user_id>/<upload_id>_<safe_name>."""
    return f"uploads/{_safe_segment(user_id)}/{_safe_segment(upload_id)}_{_sanitize_filename(filename)}"


def _resolve_project_root() -> Path:
    return Path(
        os.environ.get("AIVIDEOTRANS_PROJECTS_DIR", "")
        or os.environ.get("AIVIDEOTRANS_PROJECT_ROOT", "")
        or "/opt/aivideotrans/app"
    ).resolve(strict=False)


async def handle_upload_video(
    request: Request,
    user: Optional[object] = None,
) -> JSONResponse:
    """Receive multipart video upload, write to user-isolated path.

    The `user` parameter is injected by the gateway route registration
    via Depends(require_auth). In tests, it can be passed directly.
    """
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise HTTPException(status_code=400, detail="Content-Type must be multipart/form-data")

    content_length = int(request.headers.get("content-length", "0"))
    if content_length > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {_MAX_UPLOAD_BYTES // (1024**3)} GB)")

    form = await request.form()
    file_field = form.get("file")
    if file_field is None:
        raise HTTPException(status_code=400, detail="Missing 'file' field in form data")

    # Determine file size via seek instead of reading entire content into memory.
    # Starlette already spools large uploads to a temp file on disk;
    # we copy from that temp file to the final path in 1 MB chunks.
    file_obj = file_field.file
    file_obj.seek(0, 2)
    file_size = file_obj.tell()
    file_obj.seek(0)

    if file_size == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if file_size > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {_MAX_UPLOAD_BYTES // (1024**3)} GB)")

    filename = getattr(file_field, "filename", None) or "unnamed_upload"
    upload_id = uuid.uuid4().hex[:12]
    project_root = _resolve_project_root()

    user_id = getattr(user, "id", None) if user is not None else None
    if user_id is not None:
        relative_path = _build_upload_path(
            user_id=str(user_id),
            upload_id=upload_id,
            filename=filename,
        )
        output_path = project_root / relative_path
    else:
        safe_name = re.sub(r"[^a-zA-Z0-9_.\-]", "_", filename)
        output_path = project_root / "uploads" / f"{upload_id}_{safe_name}"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as out:
        # P1-12c (audit 2026-05-07, H-6): copyfileobj is sync; offload to
        # thread so a 2GB upload doesn't block the event loop for 30-60s
        # and starve all other async requests on the gateway worker.
        # to_thread takes positional args, so the buffer size is passed
        # positionally (third arg of shutil.copyfileobj signature).
        await asyncio.to_thread(shutil.copyfileobj, file_obj, out, 1024 * 1024)

    file_size_mb = round(file_size / (1024 * 1024), 2)

    return JSONResponse(
        status_code=200,
        content={
            "file_path": str(output_path),
            "file_name": filename,
            "file_size_mb": file_size_mb,
        },
    )
