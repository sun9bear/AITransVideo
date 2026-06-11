"""Chunked upload state store — plan 2026-06-11 §3 (Cloudflare 分片上传主方案).

职责（纯逻辑层，无 FastAPI 依赖；路由薄壳在 ``chunked_upload_api.py``）：

* **目录布局**::

      uploads/_chunked/{user_id}/{upload_id}/state.json     # 状态 + 声明元数据
      uploads/_chunked/{user_id}/{upload_id}/part_00000     # 已收分片
      uploads/_chunked/_locks/{upload_id}                   # per-upload 跨进程锁
      uploads/_chunked/_locks/_reserve                      # init/reserve 全局锁（r3 P1）
      uploads/_chunked/_usage/{YYYY-MM-DD}/{user_id}.json   # 每日声明字节配额

* **状态机（§3.2）**::

      receiving → completing → ready
          ↑           │
          └───(瞬时 IO 错回退)
      completing ──(全文件 sha256 不符)──→ failed_integrity（清空全部分片）
      receiving/failed_integrity ──TTL/DELETE──→ 清盘删除（expired / aborted 不落盘，
      目录直接消失——状态表里它们只是"清盘"动作的名字）

* **锁（§3.3）**：复用 ``src/services/_file_lock.py``（reentrant + 跨进程）。
  锁文件独立于数据目录（``_locks/``），同 R2 upload lock 先例——避免被
  未来文件搬运逻辑误扫。

* **磁盘 reserve（§3.4，r3 原子版）**：init 的"检查 + 注册"整段持
  ``_locks/_reserve`` 全局锁执行，并发双 init 串行化。放大事实：上传层
  峰值 2S（分片 + 合并文件并存），公式见 ``check_and_register_reserve``。

* **claim 闭环（§3.8 r3）**：complete 成功后 state.json 保留
  ``final_path`` + ``claimed_by_job=null``；job create 解析 opaque ref
  成功后回写 job_id；sweeper 对超时未认领的 ready 终文件做删除。

Import constraints
------------------
* 不 import ``services.jobs`` / ``src.pipeline``（gateway 容器无 pydub）。
* 不 import fastapi —— 错误用 ``ChunkedUploadError`` 结构化抛出，路由层映射。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

# Make src/ importable for services._file_lock (same pattern as admin_settings.py).
for _candidate in [
    Path(__file__).resolve().parent.parent / "src",          # local dev: repo_root/src
    Path("/opt/aivideotrans/app/src"),                       # Docker container
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from services._file_lock import file_lock  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / validation
# ---------------------------------------------------------------------------

UPLOAD_ID_RE = re.compile(r"^[a-f0-9]{32}$")
SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")

# 单片硬上限：CF 免费版单请求体 100MB，留余量 80MB（plan §3.1 R1）。
HARD_MAX_CHUNK_BYTES = 80 * 1024 * 1024
# 单片下限：防止恶意 1 字节片把 total_parts 撑爆。
HARD_MIN_CHUNK_BYTES = 1 * 1024 * 1024
# total_parts 上限兜底（2GB / 1MB = 2048；配合上面的片大小边界）。
HARD_MAX_TOTAL_PARTS = 4096

# opaque upload ref 前缀（§3.10）：job create 的 source.value 形态。
CHUNKED_SOURCE_PREFIX = "chunked:"

_STATE_FILENAME = "state.json"
_LOCKS_DIRNAME = "_locks"
_USAGE_DIRNAME = "_usage"
_RESERVE_LOCK_NAME = "_reserve"

_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9_\-]")
_UNSAFE_FILENAME_CHARS = re.compile(r"[^a-zA-Z0-9_\-.]")

# 状态集合（§3.2）。expired/aborted 是"清盘"动作，不作为落盘状态存在。
STATE_RECEIVING = "receiving"
STATE_COMPLETING = "completing"
STATE_READY = "ready"
STATE_FAILED_INTEGRITY = "failed_integrity"

# in-flight = 仍占用磁盘 reserve 预算的状态。
_INFLIGHT_STATES = frozenset({STATE_RECEIVING, STATE_COMPLETING})


class ChunkedUploadError(Exception):
    """结构化错误：路由层按 status_code / code 映射 HTTP 响应。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ChunkedLimits:
    """运行时限制快照（admin 热配置 resolve 后传入；字段与 admin 同名去前缀）。"""

    enabled: bool = False
    max_file_mb: int = 2048
    chunk_mb: int = 64
    per_user_active: int = 2
    per_user_inflight_gb: int = 4
    global_inflight_gb: int = 20
    daily_per_user_gb: int = 8
    disk_floor_gb: int = 20
    ttl_hours: int = 24
    ready_ttl_hours: int = 6


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def resolve_project_root() -> Path:
    """与 gateway/upload.py 的 resolver 一致——分片合并产物必须落在
    现有 uploads/ 约定下，pipeline 零感知。"""
    return Path(
        os.environ.get("AIVIDEOTRANS_PROJECTS_DIR", "")
        or os.environ.get("AIVIDEOTRANS_PROJECT_ROOT", "")
        or "/opt/aivideotrans/app"
    ).resolve(strict=False)


def uploads_root() -> Path:
    return resolve_project_root() / "uploads"


def chunked_root() -> Path:
    return uploads_root() / "_chunked"


def _safe_segment(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return "anonymous"
    result = _UNSAFE_CHARS.sub("_", cleaned)
    while ".." in result:
        result = result.replace("..", "_")
    return result[:64]


def _sanitize_filename(filename: str) -> str:
    cleaned = (filename or "").strip()
    if not cleaned:
        return "unnamed"
    result = _UNSAFE_FILENAME_CHARS.sub("_", cleaned)
    while ".." in result:
        result = result.replace("..", "_")
    return result[:128]


def upload_dir(user_id: str, upload_id: str) -> Path:
    return chunked_root() / _safe_segment(user_id) / upload_id


def _state_path(user_id: str, upload_id: str) -> Path:
    return upload_dir(user_id, upload_id) / _STATE_FILENAME


def _lock_path(upload_id: str) -> Path:
    return chunked_root() / _LOCKS_DIRNAME / upload_id


def _reserve_lock_path() -> Path:
    return chunked_root() / _LOCKS_DIRNAME / _RESERVE_LOCK_NAME


def part_path(user_id: str, upload_id: str, part_index: int) -> Path:
    return upload_dir(user_id, upload_id) / f"part_{part_index:05d}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# State persistence (atomic write; reads tolerate missing)
# ---------------------------------------------------------------------------


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_state(user_id: str, upload_id: str) -> Optional[dict[str, Any]]:
    """读取 state.json；不存在 / 解析失败 → None（路由层映射同形 404）。"""
    if not UPLOAD_ID_RE.match(upload_id or ""):
        return None
    path = _state_path(user_id, upload_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("chunked_upload: unreadable state.json %s", path, exc_info=True)
        return None


def _save_state(user_id: str, upload_id: str, state: dict[str, Any]) -> None:
    state["updated_at"] = _now_iso()
    _write_state(_state_path(user_id, upload_id), state)


def _iter_all_states() -> Iterator[tuple[Path, dict[str, Any]]]:
    """遍历全部 state.json（reserve 汇总 / sweeper 用）。单条坏文件跳过。"""
    root = chunked_root()
    if not root.is_dir():
        return
    for user_dir in root.iterdir():
        if not user_dir.is_dir() or user_dir.name in (_LOCKS_DIRNAME, _USAGE_DIRNAME):
            continue
        for updir in user_dir.iterdir():
            if not updir.is_dir():
                continue
            sp = updir / _STATE_FILENAME
            try:
                yield updir, json.loads(sp.read_text(encoding="utf-8"))
            except FileNotFoundError:
                # 孤儿目录（无 state.json）——交 sweeper 处理，这里跳过。
                continue
            except Exception:
                logger.warning("chunked_upload: skip unreadable state %s", sp, exc_info=True)
                continue


def _bytes_received(state: dict[str, Any]) -> int:
    parts = state.get("parts") or {}
    return sum(int(p.get("size", 0)) for p in parts.values())


def received_part_indices(state: dict[str, Any]) -> list[int]:
    parts = state.get("parts") or {}
    return sorted(int(k) for k in parts.keys())


# ---------------------------------------------------------------------------
# Disk usage (wrapped for test monkeypatching)
# ---------------------------------------------------------------------------


def _disk_free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(str(path)).free
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(str(path)).free


# ---------------------------------------------------------------------------
# Daily usage quota (Asia/Shanghai day key, 与 free_service_quota 口径一致)
# ---------------------------------------------------------------------------

_SHANGHAI_TZ = timezone(timedelta(hours=8))


def _day_key(now: Optional[datetime] = None) -> str:
    dt = now or datetime.now(timezone.utc)
    return dt.astimezone(_SHANGHAI_TZ).strftime("%Y-%m-%d")


def _usage_path(user_id: str, day: str) -> Path:
    return chunked_root() / _USAGE_DIRNAME / day / f"{_safe_segment(user_id)}.json"


def _load_usage_bytes(user_id: str, day: str) -> int:
    try:
        data = json.loads(_usage_path(user_id, day).read_text(encoding="utf-8"))
        return int(data.get("bytes", 0))
    except Exception:
        return 0


def _add_usage_bytes(user_id: str, day: str, delta: int) -> None:
    path = _usage_path(user_id, day)
    current = _load_usage_bytes(user_id, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"bytes": current + delta}), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# R1 — init (resume reuse + atomic reserve, §3.4/§3.5)
# ---------------------------------------------------------------------------


def init_upload(
    *,
    user_id: str,
    declared_size: int,
    declared_sha256: str,
    chunk_size: int,
    file_name: str,
    limits: ChunkedLimits,
) -> dict[str, Any]:
    """声明一次分片上传。返回 state dict（新建或续传命中的既有 upload）。

    整段"检查 + 注册"在全局 reserve 锁内执行（r3 P1：并发双 init 串行化，
    杜绝同时看到"空间足够"双双通过）。所有 gate fail-closed。
    """
    if not user_id:
        raise ChunkedUploadError(401, "auth_required", "未登录")

    declared_sha256 = (declared_sha256 or "").strip().lower()
    if not SHA256_HEX_RE.match(declared_sha256):
        raise ChunkedUploadError(422, "invalid_sha256", "sha256 必须是 64 位十六进制字符串")
    if not isinstance(declared_size, int) or declared_size <= 0:
        raise ChunkedUploadError(422, "invalid_size", "size 必须为正整数字节数")
    if not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ChunkedUploadError(422, "invalid_chunk_size", "chunk_size 必须为正整数字节数")

    max_file_bytes = limits.max_file_mb * 1024 * 1024
    if declared_size > max_file_bytes:
        raise ChunkedUploadError(
            413, "over_limit",
            f"文件大小超过上限 {limits.max_file_mb}MB",
        )
    if chunk_size > HARD_MAX_CHUNK_BYTES:
        raise ChunkedUploadError(
            422, "invalid_chunk_size",
            f"chunk_size 不得超过 {HARD_MAX_CHUNK_BYTES // (1024 * 1024)}MB（CF 单请求体限制）",
        )
    if chunk_size < HARD_MIN_CHUNK_BYTES and declared_size > chunk_size:
        # 小文件（单片即整文件）允许任意 chunk_size，多片才强制下限。
        raise ChunkedUploadError(
            422, "invalid_chunk_size",
            f"多片上传 chunk_size 不得小于 {HARD_MIN_CHUNK_BYTES // (1024 * 1024)}MB",
        )

    total_parts = (declared_size + chunk_size - 1) // chunk_size
    if total_parts > HARD_MAX_TOTAL_PARTS:
        raise ChunkedUploadError(
            422, "invalid_chunk_size",
            f"分片数 {total_parts} 超过上限 {HARD_MAX_TOTAL_PARTS}，请增大 chunk_size",
        )

    root = chunked_root()
    root.mkdir(parents=True, exist_ok=True)

    with file_lock(_reserve_lock_path()):
        # --- 续传复用（§3.5）：四元组 (user, sha256, size, chunk_size)，
        # 只查本人 receiving 态 upload；绝不做跨用户去重。 ---
        user_root = root / _safe_segment(user_id)
        if user_root.is_dir():
            for updir in user_root.iterdir():
                st = load_state(user_id, updir.name)
                if (
                    st is not None
                    and st.get("state") == STATE_RECEIVING
                    and st.get("declared_sha256") == declared_sha256
                    and int(st.get("declared_size", -1)) == declared_size
                    and int(st.get("chunk_size", -1)) == chunk_size
                ):
                    st["resumed"] = True
                    return st

        # --- 限额汇总（per-user active / in-flight GB / global GB）---
        user_active = 0
        user_inflight_bytes = 0
        global_inflight_bytes = 0
        inflight_reserve_remaining = 0
        for _updir, st in _iter_all_states():
            if st.get("state") not in _INFLIGHT_STATES:
                continue
            st_size = int(st.get("declared_size", 0))
            global_inflight_bytes += st_size
            inflight_reserve_remaining += max(0, 2 * st_size - _bytes_received(st))
            if st.get("user_id") == user_id:
                user_active += 1
                user_inflight_bytes += st_size

        if user_active >= limits.per_user_active:
            raise ChunkedUploadError(
                429, "too_many_active",
                f"并发上传数已达上限（{limits.per_user_active}），请先完成或放弃已有上传",
            )
        if user_inflight_bytes + declared_size > limits.per_user_inflight_gb * 1024 ** 3:
            raise ChunkedUploadError(
                429, "user_inflight_exceeded",
                f"进行中上传总量超过 {limits.per_user_inflight_gb}GB 上限",
            )
        if global_inflight_bytes + declared_size > limits.global_inflight_gb * 1024 ** 3:
            raise ChunkedUploadError(
                429, "global_inflight_exceeded",
                "系统上传通道繁忙，请稍后再试",
            )

        # --- 每日配额（声明即计，"曾经发生过即算"，同 express daily cap 口径）---
        day = _day_key()
        used_today = _load_usage_bytes(user_id, day)
        if used_today + declared_size > limits.daily_per_user_gb * 1024 ** 3:
            raise ChunkedUploadError(
                429, "daily_quota_exceeded",
                f"今日上传配额（{limits.daily_per_user_gb}GB）已用完，请明天再试",
            )

        # --- 磁盘 reserve（§3.4）：可用 - 保底 ≥ 2×本次声明 + Σ(in-flight 余量) ---
        free = _disk_free_bytes(uploads_root())
        floor = limits.disk_floor_gb * 1024 ** 3
        need = 2 * declared_size + inflight_reserve_remaining
        if free - floor < need:
            raise ChunkedUploadError(
                507, "insufficient_storage",
                "服务器存储空间不足，请稍后再试或联系管理员",
            )

        # --- 锁内注册（写 state.json = 注册 reserve）---
        upload_id = uuid.uuid4().hex
        state: dict[str, Any] = {
            "upload_id": upload_id,
            "user_id": user_id,
            "state": STATE_RECEIVING,
            "declared_size": declared_size,
            "declared_sha256": declared_sha256,
            "chunk_size": chunk_size,
            "total_parts": total_parts,
            "file_name": file_name or "unnamed",
            "safe_file_name": _sanitize_filename(file_name or "unnamed"),
            "parts": {},
            "failure_reason": None,
            "final_path": None,
            "claimed_by_job": None,
            "completed_at": None,
            "created_at": _now_iso(),
        }
        _save_state(user_id, upload_id, state)
        _add_usage_bytes(user_id, day, declared_size)
        state["resumed"] = False
        return state


# ---------------------------------------------------------------------------
# R2 — part commit（流式落盘由路由层完成；这里做"tmp → 正式片"的原子提交）
# ---------------------------------------------------------------------------


def expected_part_size(state: dict[str, Any], part_index: int) -> int:
    chunk_size = int(state["chunk_size"])
    declared = int(state["declared_size"])
    return min(chunk_size, declared - part_index * chunk_size)


def validate_part_index(state: dict[str, Any], part_index: int) -> None:
    if not (0 <= part_index < int(state["total_parts"])):
        raise ChunkedUploadError(404, "not_found", "not_found")


def commit_part(
    *,
    user_id: str,
    upload_id: str,
    part_index: int,
    tmp_path: Path,
    actual_size: int,
    actual_sha256: str,
) -> dict[str, Any]:
    """持锁把流式落盘完成的 tmp 文件原子提交为正式分片并更新 state。

    调用方（路由层）已完成：流式写 tmp + 长度硬校验 + 流式 SHA256 计算
    与 ``X-Chunk-SHA256`` 比对。这里复检状态后 ``os.replace`` 改名 +
    state.json 记账。同片重传 = 覆盖（仅 receiving 态允许）。
    """
    with file_lock(_lock_path(upload_id)):
        state = load_state(user_id, upload_id)
        if state is None:
            _safe_unlink(tmp_path)
            raise ChunkedUploadError(404, "not_found", "not_found")
        if state.get("state") != STATE_RECEIVING:
            _safe_unlink(tmp_path)
            raise ChunkedUploadError(409, "wrong_state", f"当前状态 {state.get('state')} 不接受分片")
        validate_part_index(state, part_index)
        expected = expected_part_size(state, part_index)
        if actual_size != expected:
            _safe_unlink(tmp_path)
            raise ChunkedUploadError(
                413 if actual_size > expected else 422,
                "part_size_mismatch",
                f"第 {part_index} 片长度 {actual_size} 与协议长度 {expected} 不符",
            )
        dest = part_path(user_id, upload_id, part_index)
        os.replace(tmp_path, dest)
        parts = state.setdefault("parts", {})
        parts[str(part_index)] = {"size": actual_size, "sha256": actual_sha256}
        _save_state(user_id, upload_id, state)
        return state


# ---------------------------------------------------------------------------
# R3 — complete（持锁合并 → 全文件 sha256 → 移入正式 uploads/）
# ---------------------------------------------------------------------------


def complete_upload(
    *,
    user_id: str,
    upload_id: str,
    limits: ChunkedLimits,
) -> dict[str, Any]:
    """合并分片、校验全文件 sha256、移入正式 uploads/ 路径。

    同步阻塞实现（2GB 合并数十秒）——路由层必须 ``asyncio.to_thread`` 调用。
    幂等：ready 态重复调用返回同一 state；completing 态抛 202 语义错误。
    """
    with file_lock(_lock_path(upload_id)):
        state = load_state(user_id, upload_id)
        if state is None:
            raise ChunkedUploadError(404, "not_found", "not_found")
        current = state.get("state")
        if current == STATE_READY:
            return state  # 幂等：返回同一 upload ref
        if current == STATE_COMPLETING:
            raise ChunkedUploadError(202, "in_progress", "正在合并校验中")
        if current != STATE_RECEIVING:
            raise ChunkedUploadError(409, "wrong_state", f"当前状态 {current} 无法完成")

        total_parts = int(state["total_parts"])
        missing = [
            n for n in range(total_parts)
            if str(n) not in (state.get("parts") or {})
        ]
        if missing:
            raise ChunkedUploadError(
                409, "missing_parts",
                f"缺少分片: {missing[:20]}{'...' if len(missing) > 20 else ''}",
            )

        # 合并前磁盘二次预检（init 后磁盘可能被别的任务吃掉，§3.4-2）。
        free = _disk_free_bytes(uploads_root())
        floor = limits.disk_floor_gb * 1024 ** 3
        if free - floor < int(state["declared_size"]):
            raise ChunkedUploadError(
                507, "insufficient_storage",
                "服务器存储空间不足，无法合并，请稍后重试",
            )

        state["state"] = STATE_COMPLETING
        _save_state(user_id, upload_id, state)

        updir = upload_dir(user_id, upload_id)
        merged_tmp = updir / "merged.tmp"
        digest = hashlib.sha256()
        try:
            with open(merged_tmp, "wb") as out:
                for n in range(total_parts):
                    p = part_path(user_id, upload_id, n)
                    expected = expected_part_size(state, n)
                    actual = p.stat().st_size
                    if actual != expected:
                        raise OSError(
                            f"part {n} size {actual} != expected {expected}"
                        )
                    with open(p, "rb") as src:
                        while True:
                            buf = src.read(4 * 1024 * 1024)
                            if not buf:
                                break
                            digest.update(buf)
                            out.write(buf)
        except OSError as exc:
            # 瞬时 IO 错：回 receiving，分片保留，客户端可重试 complete。
            _safe_unlink(merged_tmp)
            state["state"] = STATE_RECEIVING
            state["failure_reason"] = f"merge_io_error: {exc}"
            _save_state(user_id, upload_id, state)
            raise ChunkedUploadError(500, "merge_failed", "合并失败，请重试") from exc

        if digest.hexdigest() != state["declared_sha256"]:
            # r3：片级已有 X-Chunk-SHA256 把关，走到这步 = 声明哈希错或
            # 磁盘损坏 → failed_integrity，清空全部分片（位图保留只会无限 422）。
            _safe_unlink(merged_tmp)
            for n in range(total_parts):
                _safe_unlink(part_path(user_id, upload_id, n))
            state["state"] = STATE_FAILED_INTEGRITY
            state["parts"] = {}
            state["failure_reason"] = "sha256_mismatch"
            _save_state(user_id, upload_id, state)
            raise ChunkedUploadError(
                422, "sha256_mismatch",
                "文件完整性校验失败，已清空分片，请重新上传",
            )

        # 移入正式 uploads/ 路径（与 gateway/upload.py 命名约定一致：
        # uploads/{user_id}/{upload_id 前 12 位}_{safe_name}）。
        final_dir = uploads_root() / _safe_segment(user_id)
        final_dir.mkdir(parents=True, exist_ok=True)
        final_path = final_dir / f"{upload_id[:12]}_{state['safe_file_name']}"
        os.replace(merged_tmp, final_path)

        for n in range(total_parts):
            _safe_unlink(part_path(user_id, upload_id, n))

        state["state"] = STATE_READY
        state["final_path"] = str(final_path)
        state["failure_reason"] = None
        state["completed_at"] = _now_iso()
        _save_state(user_id, upload_id, state)
        return state


# ---------------------------------------------------------------------------
# R5 — abort（用户主动放弃）
# ---------------------------------------------------------------------------


def abort_upload(*, user_id: str, upload_id: str) -> None:
    with file_lock(_lock_path(upload_id)):
        state = load_state(user_id, upload_id)
        if state is None:
            raise ChunkedUploadError(404, "not_found", "not_found")
        if state.get("state") not in (STATE_RECEIVING, STATE_FAILED_INTEGRITY):
            raise ChunkedUploadError(
                409, "wrong_state",
                f"当前状态 {state.get('state')} 不允许放弃",
            )
        remove_upload_dir(user_id, upload_id)


def remove_upload_dir(user_id: str, upload_id: str) -> None:
    shutil.rmtree(upload_dir(user_id, upload_id), ignore_errors=True)


# ---------------------------------------------------------------------------
# §3.10 — opaque upload ref 解析 + claim 闭环
# ---------------------------------------------------------------------------


def parse_chunked_source_value(source_value: str) -> Optional[str]:
    """``chunked:{upload_id}`` → upload_id；非该形态 → None。"""
    raw = (source_value or "").strip()
    if not raw.startswith(CHUNKED_SOURCE_PREFIX):
        return None
    candidate = raw[len(CHUNKED_SOURCE_PREFIX):]
    if not UPLOAD_ID_RE.match(candidate):
        return ""  # 形态匹配但 id 非法——调用方应同形拒绝
    return candidate


def resolve_ready_upload(*, user_id: str, upload_id: str) -> Optional[str]:
    """按 upload_id + 当前登录 user 查 state==ready 的 upload → final_path。

    不存在 / 不属于本人 / 非 ready / 终文件丢失 → None（调用方同形 404）。
    final_path 深度防御：必须仍位于 uploads/ 根内。
    """
    state = load_state(user_id, upload_id)
    if state is None or state.get("state") != STATE_READY:
        return None
    if state.get("user_id") != user_id:
        return None
    final_path = state.get("final_path")
    if not final_path:
        return None
    resolved = Path(final_path).resolve(strict=False)
    try:
        resolved.relative_to(uploads_root().resolve(strict=False))
    except ValueError:
        logger.warning(
            "chunked_upload: final_path escaped uploads root, refusing: %s", final_path
        )
        return None
    if not resolved.is_file():
        return None
    return str(resolved)


def claim_upload(*, user_id: str, upload_id: str, job_id: str) -> bool:
    """job create 成功后回写 claim（§3.8 r3）。幂等：同 job 重复 claim 返回 True。"""
    with file_lock(_lock_path(upload_id)):
        state = load_state(user_id, upload_id)
        if state is None or state.get("state") != STATE_READY:
            return False
        current = state.get("claimed_by_job")
        if current and current != job_id:
            return False
        state["claimed_by_job"] = job_id
        _save_state(user_id, upload_id, state)
        return True


# ---------------------------------------------------------------------------
# Sweeper helpers（loop 在 chunked_upload_sweeper.py）
# ---------------------------------------------------------------------------


def sweep_once(limits: ChunkedLimits, *, now: Optional[datetime] = None) -> dict[str, int]:
    """单轮清扫。返回 stats（调用方写 JSONL 审计）。

    - 非 ready 且超 ttl_hours → 清盘（expired）。
    - 孤儿目录（无 state.json）→ 直接删。
    - ready 且未 claim 且超 ready_ttl_hours → 删 final_path 终文件 + 清 state
      （删除前校验仍在 uploads/ 根内，深度防御）。
    - ready 且已 claim 且超 ready_ttl_hours → 终文件归现有 uploads 生命周期
      管理，仅清 state 残留。
    - _usage/ 下超 7 天的日期目录 → 删。
    """
    stats = {
        "expired_purged": 0,
        "orphan_purged": 0,
        "ready_unclaimed_purged": 0,
        "ready_claimed_state_cleaned": 0,
        "usage_days_purged": 0,
    }
    now_dt = now or datetime.now(timezone.utc)
    ttl = timedelta(hours=limits.ttl_hours)
    ready_ttl = timedelta(hours=limits.ready_ttl_hours)
    root = chunked_root()
    if not root.is_dir():
        return stats

    for user_dir in list(root.iterdir()):
        if not user_dir.is_dir() or user_dir.name in (_LOCKS_DIRNAME, _USAGE_DIRNAME):
            continue
        for updir in list(user_dir.iterdir()):
            if not updir.is_dir():
                continue
            sp = updir / _STATE_FILENAME
            if not sp.exists():
                shutil.rmtree(updir, ignore_errors=True)
                stats["orphan_purged"] += 1
                continue
            try:
                state = json.loads(sp.read_text(encoding="utf-8"))
            except Exception:
                # 坏 state：按孤儿处理（无法判定归属/状态，保守清盘）。
                shutil.rmtree(updir, ignore_errors=True)
                stats["orphan_purged"] += 1
                continue

            updated_at = _parse_iso(state.get("updated_at")) or _parse_iso(
                state.get("created_at")
            )
            if updated_at is None:
                updated_at = now_dt  # 无时间戳：本轮跳过，下轮按新时间计

            st = state.get("state")
            if st == STATE_READY:
                if now_dt - updated_at < ready_ttl:
                    continue
                if not state.get("claimed_by_job"):
                    final_path = state.get("final_path")
                    if final_path:
                        _delete_final_path_guarded(final_path)
                    stats["ready_unclaimed_purged"] += 1
                else:
                    stats["ready_claimed_state_cleaned"] += 1
                shutil.rmtree(updir, ignore_errors=True)
            else:
                if now_dt - updated_at >= ttl:
                    shutil.rmtree(updir, ignore_errors=True)
                    stats["expired_purged"] += 1

    # _usage/ 日期目录保留 7 天
    usage_root = root / _USAGE_DIRNAME
    if usage_root.is_dir():
        cutoff = (now_dt.astimezone(_SHANGHAI_TZ) - timedelta(days=7)).strftime("%Y-%m-%d")
        for day_dir in list(usage_root.iterdir()):
            if day_dir.is_dir() and day_dir.name < cutoff:
                shutil.rmtree(day_dir, ignore_errors=True)
                stats["usage_days_purged"] += 1

    return stats


def _delete_final_path_guarded(final_path: str) -> None:
    """删除 ready 未认领终文件前校验仍在 uploads/ 根内（§3.8 深度防御）。"""
    resolved = Path(final_path).resolve(strict=False)
    try:
        resolved.relative_to(uploads_root().resolve(strict=False))
    except ValueError:
        logger.warning(
            "chunked_upload_sweeper: refusing to delete path outside uploads root: %s",
            final_path,
        )
        return
    try:
        resolved.unlink(missing_ok=True)
    except Exception:
        logger.warning(
            "chunked_upload_sweeper: failed to delete %s", final_path, exc_info=True
        )


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        logger.debug("chunked_upload: failed to unlink %s", path)
