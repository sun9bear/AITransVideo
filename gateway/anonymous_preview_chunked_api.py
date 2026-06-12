"""匿名档分片上传路由 A1-A6 — plan 2026-06-11 §9（B 方案，r1 已并入 CodeX 评审）。

设计原则：分片只是传输层替换。合并产物 move 到 ``uploads/anonymous/`` 同款
落点后走与 ``POST /gateway/anonymous-preview/upload`` 完全相同的 intake 管线
（``run_intake_and_save``），complete 响应与 /upload 同形
（``{preview_id, status, status_reason, mode, admission_decision}``）。

与注册档（chunked_upload_api.py R1-R6）的差异（§9.1）：

* 身份 = ``anon:{session_id_hash}``（store 字符串键；清 cookie 丢续传）。
* gate 三与门：env ``enable_anonymous_preview`` AND admin
  ``anonymous_free_preview_enabled`` AND admin ``chunked_upload_anonymous_enabled``
  —— A1/A2/A3 任一关同形 404；A4/A5 同 R4/R5 不被 chunked 开关 gate（留给
  进行中客户端查询/清理）。
* A1 init：AD-8 peek 预检（与 /upload 共享 ``ad8_peek_precheck``）+
  per-session active=1（硬编码）+ per-IP in-flight gate（store reserve 锁内
  原子检查，复用 ``anonymous_preview_cap_per_ip`` 旋钮）。
* A3 complete：**一次性消费**（无 ready 滞留 / claim / opaque ref）——
  整段 merge→move→intake→consume 在 per-upload 文件锁内执行；intake +
  audit 持久化共用**同一个 sync session 同一次 commit**（单事务成功边界，
  r1 P1）；成功转 ``consumed`` 态并把响应体存进 state.json，complete 重试
  原样返回（幂等）；intake 失败 rollback + 删媒体 + 整目录清盘。

Import constraints
------------------
* 不 import ``services.jobs`` / ``src.pipeline``（gateway 容器无 pydub）。
* 不出现 R2 / presigned 字样（前端零感知存储后端）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

import chunked_upload_store as store
from anonymous_preview_api import (
    _make_sync_intake_session,  # noqa: F401 — 同包共享（/upload 同款 sync session 工厂）
    _redact_reason,
    ad8_peek_precheck,
)
from anonymous_preview_intake_wiring import build_scope_hasher, run_intake_and_save
from anonymous_preview_limits import resolve_apf_limits
from anonymous_preview_policy import admit_for_free_preview
from anonymous_preview_prescreen import prescreen_filename
from anonymous_preview_probe import build_intake_probe_fn, teaser_dest_for
from anonymous_preview_quota import shanghai_today
from anonymous_preview_upload import extract_client_ip
from anonymous_session import (
    AnonymousSessionContext,
    get_or_create_anonymous_session,
    require_anonymous_session,
)
from chunked_upload_api import (
    SINGLE_REQUEST_THRESHOLD_MB,
    _status_payload,
    receive_part,
    resolve_chunked_limits,
)
from chunked_upload_store import ChunkedLimits, ChunkedUploadError
from config import settings
from csrf import require_same_origin_state_change
from database import get_db
from services._file_lock import file_lock

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/gateway/anonymous-preview/chunked",
    tags=["anonymous-preview-chunked"],
)

# 同形 404 —— 与注册档 chunked_upload_api 逐字节一致。
_NOT_FOUND_BODY = {"error": "not_found"}

_IDENTITY_PREFIX = "anon:"


def _identity(session_id_hash: str) -> str:
    return f"{_IDENTITY_PREFIX}{session_id_hash}"


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content=dict(_NOT_FOUND_BODY))


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    if status_code == 404:
        return _not_found()
    return JSONResponse(status_code=status_code, content={"error": code, "message": message})


def _from_store_error(exc: ChunkedUploadError) -> JSONResponse:
    return _error(exc.status_code, exc.code, exc.message)


def _csrf_reject(request: Request) -> Optional[JSONResponse]:
    """CSRF 同 /upload 手动 try/except（§9.1 A1）。"""
    try:
        require_same_origin_state_change(request)
    except Exception:
        return JSONResponse(status_code=403, content={"error": "csrf_origin_rejected"})
    return None


def _carry_session_cookies(out: JSONResponse, response: Response) -> JSONResponse:
    """FastAPI 不合并依赖注入 response 的 header 到 handler 显式返回的
    Response —— 新匿名会话的 avt_anon Set-Cookie 必须手动搬运
    （/upload 2026-06-11 e2e 冒烟教训，漏斗级 P0）。"""
    for _sc in response.headers.getlist("set-cookie"):
        out.headers.append("set-cookie", _sc)
    return out


def three_gates_open() -> bool:
    """三与门（§9.1 A1）：env flag AND admin 匿名预览 AND admin 匿名分片。

    admin 读取任何异常 → fail-closed False。
    """
    if not settings.enable_anonymous_preview:
        return False
    try:
        from admin_settings import load_settings

        adm = load_settings()
        return bool(adm.anonymous_free_preview_enabled) and bool(
            adm.chunked_upload_anonymous_enabled
        )
    except Exception:  # noqa: BLE001 — fail-closed
        logger.warning(
            "anon_chunked: admin settings unavailable, gate closed", exc_info=True
        )
        return False


def resolve_anonymous_chunked_limits() -> ChunkedLimits:
    """匿名档 ChunkedLimits 快照（§9.1 A1）。

    * max_file_mb = ``anonymous_preview_max_upload_mb``（200MB，不是 2GB）。
    * per_user_active = 1（per-session 硬编码，不设旋钮——真锚点是 store
      reserve 锁内的 per-IP gate）。
    * chunk_mb / global_inflight / disk_floor 与注册档**共享**同一套旋钮
      （in-flight 汇总同池）。
    * ttl_hours = ``chunked_upload_anonymous_ttl_hours``（r1 评审默认 6h）。
    * daily_per_user_gb = 1：per-session 每日声明配额（弱约束——session 可
      重置；保留它只为 init-spam 单会话兜底）。
    """
    reg = resolve_chunked_limits()  # 读失败自身 fail-closed 回默认数值
    apf = resolve_apf_limits()
    anon_ttl = 6
    try:
        from admin_settings import load_settings

        anon_ttl = int(load_settings().chunked_upload_anonymous_ttl_hours)
    except Exception:  # noqa: BLE001
        pass
    return ChunkedLimits(
        enabled=three_gates_open(),
        max_file_mb=max(1, apf.anonymous_preview_max_upload_bytes // (1024 * 1024)),
        chunk_mb=reg.chunk_mb,
        per_user_active=1,
        per_user_inflight_gb=1,
        global_inflight_gb=reg.global_inflight_gb,
        daily_per_user_gb=1,
        disk_floor_gb=reg.disk_floor_gb,
        ttl_hours=anon_ttl,
        ready_ttl_hours=reg.ready_ttl_hours,
    )


# ---------------------------------------------------------------------------
# A6 — GET /limits（必须注册在 /{upload_id}/* 动态路由之前）
# ---------------------------------------------------------------------------


@router.get("/limits")
async def anon_chunked_limits() -> JSONResponse:
    """前端选路只读端点。env flag 关 → 404（同其它匿名端点）；admin 任一关
    → 200 + ``enabled:false``（前端据此隐藏分片入口、回单请求路径）。"""
    if not settings.enable_anonymous_preview:
        return _not_found()
    limits = resolve_anonymous_chunked_limits()
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
# A1 — POST /init
# ---------------------------------------------------------------------------


@router.post("/init")
async def anon_chunked_init(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    csrf = _csrf_reject(request)
    if csrf is not None:
        return csrf
    if not three_gates_open():
        return _not_found()

    session_ctx = await get_or_create_anonymous_session(request, response, db)
    if isinstance(session_ctx, Response):
        # gate race（env/admin 在三与门检查后被关）——保持同形 404。
        return _not_found()
    assert isinstance(session_ctx, AnonymousSessionContext)

    # AD-8 body-before peek（与 /upload 共享；429/503 fail-closed）。
    apf_limits = resolve_apf_limits()
    peek = await ad8_peek_precheck(db, request, apf_limits)
    if peek is not None:
        return _carry_session_cookies(peek, response)

    try:
        body = await request.json()
    except Exception:
        return _carry_session_cookies(
            _error(400, "invalid_body", "请求体必须是 JSON"), response
        )
    if not isinstance(body, dict):
        return _carry_session_cookies(
            _error(400, "invalid_body", "请求体必须是 JSON 对象"), response
        )
    try:
        size = int(body.get("size"))
        chunk_size = int(body.get("chunk_size"))
    except (TypeError, ValueError):
        return _carry_session_cookies(
            _error(422, "invalid_body", "size / chunk_size 必须为整数"), response
        )
    sha256 = str(body.get("sha256") or "")
    file_name = str(body.get("file_name") or "unnamed")

    limits = resolve_anonymous_chunked_limits()
    # per-IP gate 的 IP 哈希：与 AD-8 权威计数 key 同源推导
    # （hash_scope_key("ip:"+ip)），不落 raw IP。
    hasher = build_scope_hasher(settings.anonymous_preview_hash_secret)
    client_ip = extract_client_ip(request) or ""
    ip_hash = hasher("ip", client_ip)

    try:
        state = await asyncio.to_thread(
            store.init_upload,
            user_id=_identity(session_ctx.session_id_hash),
            declared_size=size,
            declared_sha256=sha256,
            chunk_size=chunk_size,
            file_name=file_name,
            limits=limits,
            owner_scope=store.OWNER_SCOPE_ANONYMOUS,
            client_ip_hash=ip_hash,
            per_ip_active=int(apf_limits.anonymous_preview_cap_per_ip),
        )
    except ChunkedUploadError as exc:
        return _carry_session_cookies(_from_store_error(exc), response)

    return _carry_session_cookies(
        JSONResponse(
            status_code=200,
            content={
                "upload_id": state["upload_id"],
                "chunk_size": state["chunk_size"],
                "total_parts": state["total_parts"],
                "received_parts": store.received_part_indices(state),
                "resumed": bool(state.get("resumed")),
            },
        ),
        response,
    )


# ---------------------------------------------------------------------------
# A2 — PUT /{upload_id}/part/{n}
# ---------------------------------------------------------------------------


@router.put("/{upload_id}/part/{part_index}")
async def anon_chunked_part(
    upload_id: str,
    part_index: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    csrf = _csrf_reject(request)
    if csrf is not None:
        return csrf
    if not three_gates_open():
        return _not_found()
    session_ctx = await require_anonymous_session(request, db)
    if isinstance(session_ctx, Response):
        return session_ctx  # 401（env/admin 关已被三与门拦成 404）
    assert isinstance(session_ctx, AnonymousSessionContext)
    return await receive_part(
        request,
        identity=_identity(session_ctx.session_id_hash),
        upload_id=upload_id,
        part_index=part_index,
    )


# ---------------------------------------------------------------------------
# A3 — POST /{upload_id}/complete（merge → move → intake → consume，单锁单事务）
# ---------------------------------------------------------------------------


def _uploads_anonymous_dir(session_id_hash: str) -> Path:
    """与 handle_anonymous_upload 同款落点：uploads/anonymous/{session_seg}/。"""
    from anonymous_preview_upload import _resolve_project_root, _safe_segment

    return _resolve_project_root() / "uploads" / "anonymous" / _safe_segment(session_id_hash)


def _run_intake_single_commit(
    *,
    media_path: Path,
    state: dict[str, Any],
    session_id_hash: str,
    client_ip: str,
    apf_limits,
) -> dict[str, Any]:
    """intake + ORM audit 持久化，单 sync session 单 commit（§9 A3 r1 P1）。

    /upload 的两段提交（intake commit → async session 二次 commit audit）在
    分片路径上不复刻——post-commit audit 失败会留下"record/计数已 commit、
    接口 5xx、媒体被删"的配额燃烧路径。这里 record + 计数 + audit 同事务：
    任何一步失败整体 rollback，调用方删媒体清目录，重传可从 init 重来。

    返回 /upload 同形响应 payload（status≠ready_for_mode 也照样 200 返回，
    与 /upload 语义一致——拒绝原因在 status_reason/admission_decision 里）。
    """
    from services.anonymous_preview_backend_adapter import RequestFacts, UploadFacts
    from services.anonymous_preview_intake import SourceType
    from sqlalchemy import select as _sa_select

    from models import AnonymousPreviewRecord

    probe_fn = build_intake_probe_fn(settings)

    def _prescreen_fn(probe_result) -> object:  # noqa: ANN001
        return prescreen_filename(media_path.name)

    request_facts = RequestFacts(
        raw_session_id=session_id_hash,
        raw_ip=client_ip,
        raw_device_cookie=session_id_hash,  # AD-5: device key = avt_anon token
        source_type=SourceType.LOCAL_UPLOAD,
        is_free_user=True,
        day_key=shanghai_today(),
    )
    upload_facts = UploadFacts(
        file_name=media_path.name,
        byte_length=int(state["declared_size"]),
        duration_seconds=0.0,  # probe_fn fills this in during handle_intake
        # store.complete_upload 已校验合并文件 sha256 == declared_sha256
        source_hash=str(state["declared_sha256"]),
        stored_path=media_path,
        # is_chunked 保持默认 False（§9.3 r1 钉死）：合并后就是单文件；
        # True 会被纯 intake 契约 single_request_upload_only 直接 REJECTED。
    )

    sync_db = _make_sync_intake_session()
    try:
        record = run_intake_and_save(
            db_session=sync_db,
            request_facts=request_facts,
            upload_facts=upload_facts,
            probe_fn=probe_fn,
            prescreen_fn=_prescreen_fn,
        )
        orm_row = sync_db.execute(
            _sa_select(AnonymousPreviewRecord).where(
                AnonymousPreviewRecord.preview_id == record.record_id
            )
        ).scalar_one_or_none()
        if orm_row is None:
            # intake 刚返回 record 却查不到行 = 持久化层断裂；fail-loud，
            # rollback 连同计数一起回滚（/upload 同语义、但单事务版）。
            raise RuntimeError(
                f"anon_chunked: record {record.record_id} not found in sync "
                "session after intake save — persistence broken"
            )
        merged_audit = dict(orm_row.audit or {})
        merged_audit["stored_upload_path"] = str(media_path)
        merged_audit["teaser_path"] = str(teaser_dest_for(media_path))
        merged_audit["teaser_duration_seconds"] = float(
            getattr(record, "duration_seconds", 0.0) or 0.0
        )
        merged_audit["transport"] = "chunked"  # 排障标记，不进契约 record
        orm_row.audit = merged_audit
        # run_intake_and_save 契约："the caller commits/rolls back"。漏 commit
        # = 静默回滚 → /create 恒 404（/upload 2026-06-11 教训）。
        sync_db.commit()
    except BaseException:
        sync_db.rollback()
        raise
    finally:
        sync_db.close()

    admission_decision = None
    try:
        teaser_dur = float(getattr(record, "duration_seconds", 0.0) or 0.0)
        admission = admit_for_free_preview(teaser_dur, apf_limits)
        d = admission.decision
        admission_decision = d.value if hasattr(d, "value") else str(d)
    except Exception as exc:  # noqa: BLE001 — admission 失败不阻断（同 /upload）
        logger.warning("anon_chunked: admit_for_free_preview error: %s", exc)

    record_status = record.status
    status_str = record_status.value if hasattr(record_status, "value") else str(record_status)
    return {
        "preview_id": record.record_id,
        "status": status_str,
        "status_reason": _redact_reason(record.status_reason),
        "mode": "free",
        "admission_decision": admission_decision,
    }


def _complete_consume_sync(
    *,
    identity: str,
    session_id_hash: str,
    upload_id: str,
    limits: ChunkedLimits,
    client_ip: str,
    apf_limits,
) -> dict[str, Any]:
    """A3 全段（merge → move → intake → consume）持 per-upload 锁串行执行。

    file_lock 进程内 reentrant + 跨进程互斥：并发二次 complete 阻塞到第一次
    结束，随后看到 consumed 原样返回已存响应——intake 恰好执行一次。
    同步阻塞（200MB 合并 + ffprobe 数秒）——路由层必须 to_thread 调用。
    """
    with file_lock(store._lock_path(upload_id)):
        snapshot = store.load_state(identity, upload_id)
        if snapshot is None:
            raise ChunkedUploadError(404, "not_found", "not_found")
        if snapshot.get("state") == store.STATE_CONSUMED:
            saved = snapshot.get("consumed_response")
            if isinstance(saved, dict) and saved.get("preview_id"):
                return dict(saved)
            # consumed 却无响应体 = 不变量破坏；清盘让用户重来。
            store.remove_upload_dir(identity, upload_id)
            raise ChunkedUploadError(404, "not_found", "not_found")

        # 合并（reentrant 锁内复用注册档实现；READY 幂等返回）。
        state = store.complete_upload(user_id=identity, upload_id=upload_id, limits=limits)

        # move 到 /upload 同款落点（§9.3）。崩溃恢复：final_path 不在了但
        # dest 在 → 上次 move 后崩，直接续用 dest。
        dest_dir = _uploads_anonymous_dir(session_id_hash)
        dest = dest_dir / f"{upload_id[:12]}_{state['safe_file_name']}"
        final_path = Path(str(state.get("final_path") or ""))
        if final_path.is_file():
            dest_dir.mkdir(parents=True, exist_ok=True)
            os.replace(final_path, dest)
        elif not dest.is_file():
            # 终文件两头都不在：状态损坏，清盘重来。
            store.remove_upload_dir(identity, upload_id)
            raise ChunkedUploadError(404, "not_found", "not_found")

        try:
            payload = _run_intake_single_commit(
                media_path=dest,
                state=state,
                session_id_hash=session_id_hash,
                client_ip=client_ip,
                apf_limits=apf_limits,
            )
        except BaseException:
            # intake 失败：record/计数已随 rollback 回滚 → 删媒体 + 整目录
            # 清盘（分片已被 complete 删除，续传无意义；重传从 init 重来）。
            try:
                dest.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                logger.warning("anon_chunked: failed to delete media %s", dest)
            store.remove_upload_dir(identity, upload_id)
            raise

        store.consume_upload(
            user_id=identity, upload_id=upload_id, response_payload=payload
        )
        return payload


@router.post("/{upload_id}/complete")
async def anon_chunked_complete(
    upload_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    csrf = _csrf_reject(request)
    if csrf is not None:
        return csrf
    if not three_gates_open():
        return _not_found()
    session_ctx = await require_anonymous_session(request, db)
    if isinstance(session_ctx, Response):
        return session_ctx
    assert isinstance(session_ctx, AnonymousSessionContext)
    if not store.UPLOAD_ID_RE.match(upload_id or ""):
        return _not_found()

    apf_limits = resolve_apf_limits()
    limits = resolve_anonymous_chunked_limits()
    client_ip = extract_client_ip(request) or ""

    try:
        payload = await asyncio.to_thread(
            _complete_consume_sync,
            identity=_identity(session_ctx.session_id_hash),
            session_id_hash=session_ctx.session_id_hash,
            upload_id=upload_id,
            limits=limits,
            client_ip=client_ip,
            apf_limits=apf_limits,
        )
    except ChunkedUploadError as exc:
        if exc.status_code == 202:
            # 崩溃残留 completing：客户端轮询 A4 后重试 complete。
            return JSONResponse(
                status_code=202,
                content={"upload_id": upload_id, "state": store.STATE_COMPLETING},
            )
        return _from_store_error(exc)
    except Exception as exc:  # noqa: BLE001 — intake/move 意外失败
        logger.exception("anon_chunked: complete failed upload=%.8s: %s", upload_id, exc)
        return JSONResponse(status_code=500, content={"error": "intake_failed"})

    return JSONResponse(status_code=200, content=payload)


# ---------------------------------------------------------------------------
# A4 — GET /{upload_id}/status
# ---------------------------------------------------------------------------


@router.get("/{upload_id}/status")
async def anon_chunked_status(
    upload_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    session_ctx = await require_anonymous_session(request, db)
    if isinstance(session_ctx, Response):
        return session_ctx
    assert isinstance(session_ctx, AnonymousSessionContext)
    if not store.UPLOAD_ID_RE.match(upload_id or ""):
        return _not_found()
    state = store.load_state(_identity(session_ctx.session_id_hash), upload_id)
    if state is None:
        return _not_found()
    payload = _status_payload(state)
    # 匿名档无 opaque ref 概念（§9.1 A3）；consumed 暴露 preview_id 供前端
    # 在 complete 响应丢失时兜底接轮询。
    payload.pop("upload_ref", None)
    if state.get("state") == store.STATE_CONSUMED:
        saved = state.get("consumed_response") or {}
        if isinstance(saved, dict) and saved.get("preview_id"):
            payload["preview_id"] = saved["preview_id"]
    return JSONResponse(status_code=200, content=payload)


# ---------------------------------------------------------------------------
# A5 — DELETE /{upload_id}
# ---------------------------------------------------------------------------


@router.delete("/{upload_id}")
async def anon_chunked_abort(
    upload_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    csrf = _csrf_reject(request)
    if csrf is not None:
        return csrf
    session_ctx = await require_anonymous_session(request, db)
    if isinstance(session_ctx, Response):
        return session_ctx
    assert isinstance(session_ctx, AnonymousSessionContext)
    if not store.UPLOAD_ID_RE.match(upload_id or ""):
        return _not_found()
    try:
        await asyncio.to_thread(
            store.abort_upload,
            user_id=_identity(session_ctx.session_id_hash),
            upload_id=upload_id,
        )
    except ChunkedUploadError as exc:
        return _from_store_error(exc)
    return JSONResponse(status_code=200, content={"upload_id": upload_id, "state": "aborted"})
