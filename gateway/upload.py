"""Gateway-native video upload endpoint.

Handles multipart/form-data directly in the Gateway process,
using the authenticated user context for path isolation.
Does NOT proxy to 8876.
"""
import os
import re
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

    file_content = await file_field.read()
    if not file_content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(file_content) > _MAX_UPLOAD_BYTES:
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
    output_path.write_bytes(file_content)

    file_size_mb = round(len(file_content) / (1024 * 1024), 2)

    return JSONResponse(
        status_code=200,
        content={
            "file_path": str(output_path),
            "file_name": filename,
            "file_size_mb": file_size_mb,
        },
    )
