"""Gateway-native video upload endpoint.

Handles multipart/form-data directly in the Gateway process,
using the authenticated user context for path isolation.
Does NOT proxy to 8876.
"""
import asyncio
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
_DEFAULT_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
_MIN_UPLOAD_CHUNK_BYTES = 256 * 1024
_MAX_UPLOAD_CHUNK_BYTES = 64 * 1024 * 1024
_UPLOAD_ASSET_TTL_DAYS = 7
_UPLOAD_SESSION_DIR_NAME = ".sessions"
_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9_\-]")
_UNSAFE_FILENAME_CHARS = re.compile(r"[^a-zA-Z0-9_\-.]")
_UPLOAD_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{8,64}$")


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


def _user_id_from_user(user: Optional[object]) -> Optional[str]:
    user_id = getattr(user, "id", None) if user is not None else None
    return str(user_id) if user_id is not None else None


def _normalize_upload_id(value: object) -> str:
    upload_id = str(value or "").strip()
    if not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise HTTPException(status_code=400, detail="Invalid upload_id")
    return upload_id


def _build_upload_destination(
    project_root: Path,
    *,
    user_id: Optional[str],
    upload_id: str,
    filename: str,
) -> tuple[str, Path]:
    if user_id is not None:
        relative_path = _build_upload_path(
            user_id=user_id,
            upload_id=upload_id,
            filename=filename,
        )
    else:
        relative_path = f"uploads/{_safe_segment(upload_id)}_{_sanitize_filename(filename)}"
    return relative_path, project_root / relative_path


def _build_session_dir(project_root: Path, *, user_id: Optional[str], upload_id: str) -> Path:
    user_segment = _safe_segment(user_id or "anonymous")
    return project_root / "uploads" / _UPLOAD_SESSION_DIR_NAME / user_segment / _normalize_upload_id(upload_id)


def _session_meta_path(session_dir: Path) -> Path:
    return session_dir / "session.json"


def _chunk_part_path(session_dir: Path, chunk_index: int) -> Path:
    return session_dir / f"{chunk_index:08d}.part"


def _resolve_project_root() -> Path:
    return Path(
        os.environ.get("AIVIDEOTRANS_PROJECTS_DIR", "")
        or os.environ.get("AIVIDEOTRANS_PROJECT_ROOT", "")
        or "/opt/aivideotrans/app"
    ).resolve(strict=False)


def _copy_upload_with_sha256(file_obj, output_path: Path) -> str:
    digest = hashlib.sha256()
    with open(output_path, "wb") as out:
        while True:
            chunk = file_obj.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def _copy_upload_chunk(file_obj, output_path: Path) -> None:
    tmp_path = output_path.with_name(f"{output_path.name}.tmp")
    with open(tmp_path, "wb") as out:
        while True:
            chunk = file_obj.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    tmp_path.replace(output_path)


def _write_upload_asset_sidecar(output_path: Path, payload: dict[str, object]) -> None:
    sidecar_path = output_path.with_name(f"{output_path.name}.asset.json")
    tmp_path = output_path.with_name(f"{output_path.name}.asset.json.tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(sidecar_path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _load_session(session_dir: Path) -> dict[str, Any]:
    meta_path = _session_meta_path(session_dir)
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Upload session not found")
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Upload session metadata is corrupted") from exc


def _received_chunks(session_dir: Path) -> list[int]:
    indexes: list[int] = []
    for part_path in session_dir.glob("*.part"):
        try:
            indexes.append(int(part_path.stem))
        except ValueError:
            continue
    return sorted(indexes)


def _session_status_payload(session_dir: Path, session: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "upload_id": session["upload_id"],
        "file_name": session["file_name"],
        "file_size": session["size_bytes"],
        "chunk_size": session["chunk_size"],
        "total_chunks": session["total_chunks"],
        "received_chunks": _received_chunks(session_dir),
        "completed": bool(session.get("completed")),
        "expires_at": session.get("expires_at"),
    }
    if isinstance(session.get("asset"), dict):
        payload["asset"] = session["asset"]
    return payload


def _parse_positive_int(payload: dict[str, Any], key: str) -> int:
    try:
        value = int(payload.get(key, 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {key}") from exc
    if value <= 0:
        raise HTTPException(status_code=400, detail=f"Invalid {key}")
    return value


def _build_upload_asset_payload(
    *,
    output_path: Path,
    relative_path: str,
    upload_id: str,
    user_id: Optional[str],
    filename: str,
    file_size: int,
    sha256: str,
    now: datetime,
) -> dict[str, object]:
    expires_at = now + timedelta(days=_UPLOAD_ASSET_TTL_DAYS)
    return {
        "upload_id": upload_id,
        "user_id": user_id,
        "file_path": str(output_path),
        "relative_path": relative_path,
        "file_name": filename,
        "size_bytes": file_size,
        "sha256": f"sha256:{sha256}",
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }


def _upload_asset_response(asset_payload: dict[str, object]) -> dict[str, object]:
    size_bytes = int(asset_payload.get("size_bytes") or 0)
    return {
        "upload_id": asset_payload["upload_id"],
        "file_path": asset_payload["file_path"],
        "file_name": asset_payload["file_name"],
        "file_size_mb": round(size_bytes / (1024 * 1024), 2),
        "sha256": asset_payload["sha256"],
        "expires_at": asset_payload["expires_at"],
    }


def _combine_chunks_with_sha256(
    *,
    session_dir: Path,
    output_path: Path,
    total_chunks: int,
    expected_size: int,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(f"{output_path.name}.tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    total_size = 0
    with open(tmp_path, "wb") as out:
        for chunk_index in range(total_chunks):
            part_path = _chunk_part_path(session_dir, chunk_index)
            with open(part_path, "rb") as part:
                while True:
                    chunk = part.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total_size += len(chunk)
                    out.write(chunk)

    if total_size != expected_size:
        tmp_path.unlink(missing_ok=True)
        raise ValueError(f"Combined upload size mismatch: expected {expected_size}, got {total_size}")

    tmp_path.replace(output_path)
    return digest.hexdigest(), total_size


def _cleanup_completed_chunks(session_dir: Path) -> None:
    for part_path in session_dir.glob("*.part"):
        part_path.unlink(missing_ok=True)


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
    user_id = _user_id_from_user(user)
    relative_path, output_path = _build_upload_destination(
        project_root,
        user_id=user_id,
        upload_id=upload_id,
        filename=filename,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sha256 = await asyncio.to_thread(_copy_upload_with_sha256, file_obj, output_path)

    now = datetime.now(timezone.utc)
    asset_payload = _build_upload_asset_payload(
        output_path=output_path,
        relative_path=relative_path,
        upload_id=upload_id,
        user_id=user_id,
        filename=filename,
        file_size=file_size,
        sha256=sha256,
        now=now,
    )
    await asyncio.to_thread(_write_upload_asset_sidecar, output_path, asset_payload)

    return JSONResponse(
        status_code=200,
        content=_upload_asset_response(asset_payload),
    )


async def handle_upload_video_session(
    request: Request,
    user: Optional[object] = None,
) -> JSONResponse:
    """Create or resume a resumable local-video upload session."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    filename = str(payload.get("file_name") or payload.get("filename") or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="Missing file_name")

    file_size = _parse_positive_int(payload, "file_size")
    if file_size > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {_MAX_UPLOAD_BYTES // (1024**3)} GB)")

    try:
        requested_chunk_size = int(payload.get("chunk_size") or _DEFAULT_UPLOAD_CHUNK_BYTES)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid chunk_size") from exc
    chunk_size = max(_MIN_UPLOAD_CHUNK_BYTES, min(_MAX_UPLOAD_CHUNK_BYTES, requested_chunk_size))
    total_chunks = max(1, (file_size + chunk_size - 1) // chunk_size)

    upload_id = _normalize_upload_id(payload.get("upload_id") or uuid.uuid4().hex[:12])
    project_root = _resolve_project_root()
    user_id = _user_id_from_user(user)
    session_dir = _build_session_dir(project_root, user_id=user_id, upload_id=upload_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    meta_path = _session_meta_path(session_dir)

    now = datetime.now(timezone.utc)
    if meta_path.exists():
        session = _load_session(session_dir)
        if int(session.get("size_bytes") or 0) != file_size:
            raise HTTPException(status_code=409, detail="Upload session file_size mismatch")
        if session.get("file_name") != filename:
            raise HTTPException(status_code=409, detail="Upload session file_name mismatch")
    else:
        relative_path, output_path = _build_upload_destination(
            project_root,
            user_id=user_id,
            upload_id=upload_id,
            filename=filename,
        )
        expires_at = now + timedelta(days=_UPLOAD_ASSET_TTL_DAYS)
        session = {
            "upload_id": upload_id,
            "user_id": user_id,
            "file_name": filename,
            "relative_path": relative_path,
            "file_path": str(output_path),
            "size_bytes": file_size,
            "chunk_size": chunk_size,
            "total_chunks": total_chunks,
            "completed": False,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        await asyncio.to_thread(_atomic_write_json, meta_path, session)

    return JSONResponse(status_code=200, content=_session_status_payload(session_dir, session))


async def handle_upload_video_session_status(
    upload_id: str,
    user: Optional[object] = None,
) -> JSONResponse:
    project_root = _resolve_project_root()
    user_id = _user_id_from_user(user)
    session_dir = _build_session_dir(project_root, user_id=user_id, upload_id=upload_id)
    session = _load_session(session_dir)
    return JSONResponse(status_code=200, content=_session_status_payload(session_dir, session))


async def handle_upload_video_chunk(
    request: Request,
    user: Optional[object] = None,
) -> JSONResponse:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise HTTPException(status_code=400, detail="Content-Type must be multipart/form-data")

    form = await request.form()
    upload_id = _normalize_upload_id(form.get("upload_id"))
    try:
        chunk_index = int(str(form.get("chunk_index", "")))
        total_chunks = int(str(form.get("total_chunks", "")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid chunk metadata") from exc
    if chunk_index < 0 or total_chunks <= 0:
        raise HTTPException(status_code=400, detail="Invalid chunk metadata")

    project_root = _resolve_project_root()
    user_id = _user_id_from_user(user)
    session_dir = _build_session_dir(project_root, user_id=user_id, upload_id=upload_id)
    session = _load_session(session_dir)
    expected_total_chunks = int(session.get("total_chunks") or 0)
    if total_chunks != expected_total_chunks:
        raise HTTPException(status_code=409, detail="Upload session total_chunks mismatch")
    if chunk_index >= expected_total_chunks:
        raise HTTPException(status_code=400, detail="chunk_index out of range")
    if session.get("completed"):
        raise HTTPException(status_code=409, detail="Upload session already completed")

    file_field = form.get("chunk")
    if file_field is None:
        raise HTTPException(status_code=400, detail="Missing 'chunk' field in form data")
    file_obj = file_field.file
    file_obj.seek(0, 2)
    chunk_size = file_obj.tell()
    file_obj.seek(0)
    if chunk_size == 0:
        raise HTTPException(status_code=400, detail="Uploaded chunk is empty")
    if chunk_size > int(session["chunk_size"]):
        raise HTTPException(status_code=413, detail="Uploaded chunk is larger than session chunk_size")

    part_path = _chunk_part_path(session_dir, chunk_index)
    await asyncio.to_thread(_copy_upload_chunk, file_obj, part_path)
    return JSONResponse(
        status_code=200,
        content={
            "upload_id": upload_id,
            "chunk_index": chunk_index,
            "received_chunks": _received_chunks(session_dir),
        },
    )


async def handle_upload_video_complete(
    request: Request,
    user: Optional[object] = None,
) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    upload_id = _normalize_upload_id(payload.get("upload_id"))
    total_chunks = _parse_positive_int(payload, "total_chunks")
    project_root = _resolve_project_root()
    user_id = _user_id_from_user(user)
    session_dir = _build_session_dir(project_root, user_id=user_id, upload_id=upload_id)
    session = _load_session(session_dir)

    asset = session.get("asset")
    if session.get("completed") and isinstance(asset, dict):
        output_path = Path(str(asset.get("file_path", "")))
        if output_path.exists():
            return JSONResponse(status_code=200, content=_upload_asset_response(asset))

    expected_total_chunks = int(session.get("total_chunks") or 0)
    if total_chunks != expected_total_chunks:
        raise HTTPException(status_code=409, detail="Upload session total_chunks mismatch")

    received = set(_received_chunks(session_dir))
    missing = [index for index in range(expected_total_chunks) if index not in received]
    if missing:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Upload session is missing chunks",
                "missing_chunks": missing,
                "received_chunks": sorted(received),
            },
        )

    output_path = Path(str(session["file_path"]))
    expected_size = int(session["size_bytes"])
    try:
        sha256, file_size = await asyncio.to_thread(
            _combine_chunks_with_sha256,
            session_dir=session_dir,
            output_path=output_path,
            total_chunks=expected_total_chunks,
            expected_size=expected_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    now = datetime.now(timezone.utc)
    asset_payload = _build_upload_asset_payload(
        output_path=output_path,
        relative_path=str(session["relative_path"]),
        upload_id=upload_id,
        user_id=user_id,
        filename=str(session["file_name"]),
        file_size=file_size,
        sha256=sha256,
        now=now,
    )
    await asyncio.to_thread(_write_upload_asset_sidecar, output_path, asset_payload)

    session["completed"] = True
    session["completed_at"] = now.isoformat()
    session["asset"] = _upload_asset_response(asset_payload)
    await asyncio.to_thread(_atomic_write_json, _session_meta_path(session_dir), session)
    await asyncio.to_thread(_cleanup_completed_chunks, session_dir)

    return JSONResponse(status_code=200, content=session["asset"])
