"""Chunked upload HTTP routes (R1-R6) — plan 2026-06-11 §3.1.

薄路由层：auth / CSRF / 流式接收在这里；状态机与锁在
``chunked_upload_store.py``。

约定（plan §3.1 / §3.5 / §3.6）：

* 全部路由要求登录态（``require_auth`` + user 非 None 显式检查——
  这些端点没有匿名语义，``auth_required=False`` 的 dev 环境同样拒绝）。
* 全部状态变更方法（POST/PUT/DELETE）过 ``require_same_origin_state_change``
  （router-level dependency；GET 在 guard 内部直接放行）。
* ownership 按 ``upload_id AND user_id``——不存在与不属于本人返回
  **逐字节同形** 404（``_NOT_FOUND_BODY``），无侧信道。
* R2 用 raw ``PUT`` + ``request.stream()`` 直写 tmp（**禁 request.form()**，
  CodeX P2）；流式计数超协议长度 + 1KB 容差立即断流删 tmp。
* feature gate：``chunked_upload_enabled=False`` 时 R1/R2/R3 返回同形 404
  （kill-switch 同时止住新增字节与合并 IO）；R4 status / R5 delete 仍可用
  （留给进行中的客户端查询/清理）；R6 返回 ``enabled:false``（200）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from auth import require_auth
from csrf import require_same_origin_state_change
from models import User

import chunked_upload_store as store
from chunked_upload_store import ChunkedLimits, ChunkedUploadError

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/gateway/uploads/chunked",
    tags=["chunked-upload"],
    dependencies=[Depends(require_same_origin_state_change)],
)

# 前端选路阈值（MB）：≤ 此值走现有单请求路径（CF 免费版边缘 100MB，
# 留余量取 95）。经 R6 下发，前端不硬编码。
SINGLE_REQUEST_THRESHOLD_MB = 95

# 流式接收容差（plan §3.6）：超 协议长度 + 1KB 即断流。
_STREAM_TOLERANCE_BYTES = 1024

# 同形 404 响应体——所有 "不存在 / 非本人 / 功能关闭" 路径必须逐字节一致。
_NOT_FOUND_BODY = {"error": "not_found"}


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content=dict(_NOT_FOUND_BODY))


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    if status_code == 404:
        return _not_found()
    return JSONResponse(status_code=status_code, content={"error": code, "message": message})


def _from_store_error(exc: ChunkedUploadError) -> JSONResponse:
    return _error(exc.status_code, exc.code, exc.message)


# ---------------------------------------------------------------------------
# Limits resolve（admin 热配置；任何异常 → 默认值 = enabled False，fail-closed）
# ---------------------------------------------------------------------------


def resolve_chunked_limits() -> ChunkedLimits:
    try:
        from admin_settings import load_settings

        adm = load_settings()
        return ChunkedLimits(
            enabled=bool(adm.chunked_upload_enabled),
            max_file_mb=int(adm.chunked_upload_max_file_mb),
            chunk_mb=int(adm.chunked_upload_chunk_mb),
            per_user_active=int(adm.chunked_upload_per_user_active),
            per_user_inflight_gb=int(adm.chunked_upload_per_user_inflight_gb),
            global_inflight_gb=int(adm.chunked_upload_global_inflight_gb),
            daily_per_user_gb=int(adm.chunked_upload_daily_per_user_gb),
            disk_floor_gb=int(adm.chunked_upload_disk_floor_gb),
            ttl_hours=int(adm.chunked_upload_ttl_hours),
            ready_ttl_hours=int(adm.chunked_upload_ready_ttl_hours),
        )
    except Exception:  # noqa: BLE001 — 配置读取故障必须 fail-closed
        logger.warning(
            "resolve_chunked_limits: admin settings unavailable, chunked upload disabled",
            exc_info=True,
        )
        return ChunkedLimits(enabled=False)


def _user_id_or_none(user: Optional[User]) -> Optional[str]:
    uid = getattr(user, "id", None) if user is not None else None
    return str(uid) if uid is not None else None


def _status_payload(state: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "upload_id": state["upload_id"],
        "state": state["state"],
        "received_parts": store.received_part_indices(state),
        "bytes_received": sum(
            int(p.get("size", 0)) for p in (state.get("parts") or {}).values()
        ),
        "total_parts": state["total_parts"],
        "declared_size": state["declared_size"],
        "chunk_size": state["chunk_size"],
    }
    if state["state"] == store.STATE_READY:
        payload["upload_ref"] = f"{store.CHUNKED_SOURCE_PREFIX}{state['upload_id']}"
    if state.get("failure_reason"):
        payload["failure_reason"] = state["failure_reason"]
    return payload


# ---------------------------------------------------------------------------
# R6 — GET /limits（必须注册在 /{upload_id}/* 动态路由之前）
# ---------------------------------------------------------------------------


@router.get("/limits")
async def chunked_upload_limits(
    user: Optional[User] = Depends(require_auth),
) -> JSONResponse:
    """只读：前端选路/切片参数。``enabled:false`` 时仍 200（前端据此隐藏入口）。"""
    if _user_id_or_none(user) is None:
        return _error(401, "auth_required", "未登录")
    limits = resolve_chunked_limits()
    return JSONResponse(
        status_code=200,
        content={
            "enabled": limits.enabled,
            "threshold_mb": SINGLE_REQUEST_THRESHOLD_MB,
            "max_file_mb": limits.max_file_mb,
            "chunk_mb": limits.chunk_mb,
        },
    )


# ---------------------------------------------------------------------------
# R1 — POST /init
# ---------------------------------------------------------------------------


@router.post("/init")
async def chunked_upload_init(
    request: Request,
    user: Optional[User] = Depends(require_auth),
) -> JSONResponse:
    user_id = _user_id_or_none(user)
    if user_id is None:
        return _error(401, "auth_required", "未登录")
    limits = resolve_chunked_limits()
    if not limits.enabled:
        return _not_found()

    try:
        body = await request.json()
    except Exception:
        return _error(400, "invalid_body", "请求体必须是 JSON")
    if not isinstance(body, dict):
        return _error(400, "invalid_body", "请求体必须是 JSON 对象")

    try:
        size = int(body.get("size"))
        chunk_size = int(body.get("chunk_size"))
    except (TypeError, ValueError):
        return _error(422, "invalid_body", "size / chunk_size 必须为整数")
    sha256 = str(body.get("sha256") or "")
    file_name = str(body.get("file_name") or "unnamed")

    try:
        state = await asyncio.to_thread(
            store.init_upload,
            user_id=user_id,
            declared_size=size,
            declared_sha256=sha256,
            chunk_size=chunk_size,
            file_name=file_name,
            limits=limits,
        )
    except ChunkedUploadError as exc:
        return _from_store_error(exc)

    return JSONResponse(
        status_code=200,
        content={
            "upload_id": state["upload_id"],
            "chunk_size": state["chunk_size"],
            "total_parts": state["total_parts"],
            "received_parts": store.received_part_indices(state),
            "resumed": bool(state.get("resumed")),
        },
    )


# ---------------------------------------------------------------------------
# R2 — PUT /{upload_id}/part/{n}
# ---------------------------------------------------------------------------


@router.put("/{upload_id}/part/{part_index}")
async def chunked_upload_part(
    upload_id: str,
    part_index: int,
    request: Request,
    user: Optional[User] = Depends(require_auth),
) -> JSONResponse:
    user_id = _user_id_or_none(user)
    if user_id is None:
        return _error(401, "auth_required", "未登录")
    limits = resolve_chunked_limits()
    if not limits.enabled:
        return _not_found()
    if not store.UPLOAD_ID_RE.match(upload_id or ""):
        return _not_found()

    # 读 state（无锁快照——commit_part 持锁后会复检）确定协议长度。
    state = store.load_state(user_id, upload_id)
    if state is None:
        return _not_found()
    if state.get("state") != store.STATE_RECEIVING:
        return _error(409, "wrong_state", f"当前状态 {state.get('state')} 不接受分片")
    if not (0 <= part_index < int(state["total_parts"])):
        return _not_found()
    expected = store.expected_part_size(state, part_index)

    raw_cl = request.headers.get("content-length")
    if raw_cl is None:
        return _error(411, "content_length_required", "必须携带 Content-Length")
    try:
        content_length = int(raw_cl)
    except (TypeError, ValueError):
        return _error(400, "invalid_content_length", "Content-Length 非法")
    if content_length > expected:
        return _error(
            413, "part_too_large",
            f"第 {part_index} 片协议长度为 {expected} 字节",
        )
    if content_length != expected:
        return _error(
            422, "part_size_mismatch",
            f"第 {part_index} 片协议长度为 {expected} 字节，Content-Length 为 {content_length}",
        )

    declared_hash = (request.headers.get("x-chunk-sha256") or "").strip().lower()
    if not store.SHA256_HEX_RE.match(declared_hash):
        return _error(422, "chunk_sha256_required", "必须携带合法的 X-Chunk-SHA256 头")

    # 流式落盘到唯一 tmp（并发同片重传互不踩踏；commit 时 os.replace 原子改名）。
    updir = store.upload_dir(user_id, upload_id)
    updir.mkdir(parents=True, exist_ok=True)
    tmp_path = updir / f"part_{part_index:05d}.{uuid.uuid4().hex[:8]}.tmp"
    digest = hashlib.sha256()
    bytes_written = 0
    hard_cap = expected + _STREAM_TOLERANCE_BYTES
    try:
        with open(tmp_path, "wb") as out:
            async for chunk in request.stream():
                bytes_written += len(chunk)
                if bytes_written > hard_cap:
                    raise ChunkedUploadError(
                        413, "part_too_large",
                        f"第 {part_index} 片超过协议长度 {expected} 字节",
                    )
                digest.update(chunk)
                await asyncio.to_thread(out.write, chunk)
    except ChunkedUploadError as exc:
        _cleanup_tmp(tmp_path)
        return _from_store_error(exc)
    except Exception:
        _cleanup_tmp(tmp_path)
        logger.warning(
            "chunked_upload: stream write failed upload=%.8s part=%d",
            upload_id, part_index, exc_info=True,
        )
        return _error(500, "stream_failed", "分片接收失败，请重试")

    if bytes_written != expected:
        _cleanup_tmp(tmp_path)
        return _error(
            422, "part_size_mismatch",
            f"第 {part_index} 片实收 {bytes_written} 字节，协议长度 {expected} 字节",
        )
    if digest.hexdigest() != declared_hash:
        # 片哈希不符 → 拒收删 tmp（r3 per-part 完整性）。
        _cleanup_tmp(tmp_path)
        return _error(422, "part_hash_mismatch", f"第 {part_index} 片哈希校验失败")

    try:
        new_state = await asyncio.to_thread(
            store.commit_part,
            user_id=user_id,
            upload_id=upload_id,
            part_index=part_index,
            tmp_path=tmp_path,
            actual_size=bytes_written,
            actual_sha256=digest.hexdigest(),
        )
    except ChunkedUploadError as exc:
        return _from_store_error(exc)

    return JSONResponse(
        status_code=200,
        content={
            "upload_id": upload_id,
            "part": part_index,
            "received_parts": store.received_part_indices(new_state),
            "total_parts": new_state["total_parts"],
        },
    )


# ---------------------------------------------------------------------------
# R3 — POST /{upload_id}/complete
# ---------------------------------------------------------------------------


@router.post("/{upload_id}/complete")
async def chunked_upload_complete(
    upload_id: str,
    user: Optional[User] = Depends(require_auth),
) -> JSONResponse:
    user_id = _user_id_or_none(user)
    if user_id is None:
        return _error(401, "auth_required", "未登录")
    limits = resolve_chunked_limits()
    if not limits.enabled:
        return _not_found()
    if not store.UPLOAD_ID_RE.match(upload_id or ""):
        return _not_found()

    try:
        # 2GB 合并数十秒——整段（锁 + 合并 + 校验 + 改名）下放线程，
        # 不阻塞 gateway 事件循环。
        state = await asyncio.to_thread(
            store.complete_upload,
            user_id=user_id,
            upload_id=upload_id,
            limits=limits,
        )
    except ChunkedUploadError as exc:
        if exc.status_code == 202:
            # completing 幂等语义：202 in_progress，客户端轮询 R4。
            return JSONResponse(
                status_code=202,
                content={"upload_id": upload_id, "state": store.STATE_COMPLETING},
            )
        return _from_store_error(exc)

    return JSONResponse(
        status_code=200,
        content={
            "upload_id": upload_id,
            "state": state["state"],
            # opaque upload ref（§3.10）：不是文件路径。job create 时
            # source.value 原样传这个字符串。
            "upload_ref": f"{store.CHUNKED_SOURCE_PREFIX}{upload_id}",
            "file_name": state.get("file_name"),
        },
    )


# ---------------------------------------------------------------------------
# R4 — GET /{upload_id}/status
# ---------------------------------------------------------------------------


@router.get("/{upload_id}/status")
async def chunked_upload_status(
    upload_id: str,
    user: Optional[User] = Depends(require_auth),
) -> JSONResponse:
    user_id = _user_id_or_none(user)
    if user_id is None:
        return _error(401, "auth_required", "未登录")
    if not store.UPLOAD_ID_RE.match(upload_id or ""):
        return _not_found()
    state = store.load_state(user_id, upload_id)
    if state is None:
        return _not_found()
    return JSONResponse(status_code=200, content=_status_payload(state))


# ---------------------------------------------------------------------------
# R5 — DELETE /{upload_id}
# ---------------------------------------------------------------------------


@router.delete("/{upload_id}")
async def chunked_upload_abort(
    upload_id: str,
    user: Optional[User] = Depends(require_auth),
) -> JSONResponse:
    user_id = _user_id_or_none(user)
    if user_id is None:
        return _error(401, "auth_required", "未登录")
    if not store.UPLOAD_ID_RE.match(upload_id or ""):
        return _not_found()
    try:
        await asyncio.to_thread(store.abort_upload, user_id=user_id, upload_id=upload_id)
    except ChunkedUploadError as exc:
        return _from_store_error(exc)
    return JSONResponse(status_code=200, content={"upload_id": upload_id, "state": "aborted"})


def _cleanup_tmp(tmp_path: Path) -> None:
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("chunked_upload: failed to delete tmp %s", tmp_path)
