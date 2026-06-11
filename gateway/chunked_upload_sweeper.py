"""Chunked upload TTL sweeper — plan 2026-06-11 §3.8.

周期后台任务（gateway lifespan，复用 anonymous_preview_sweeper 模式）：

- 非 ready 且 state.json 超 ``chunked_upload_ttl_hours`` → 清盘（expired）。
- 孤儿目录（无 state.json / state 损坏）→ 直接删。
- ready 且未被 job claim 且超 ``chunked_upload_ready_ttl_hours`` →
  删 final_path 终文件 + 清 state（r3 claim 闭环；删除前校验路径仍在
  uploads/ 根内）。
- ready 且已 claim → 终文件归现有 uploads 生命周期管理，仅清 state 残留。
- ``_usage/`` 超 7 天的日期目录删除。

清扫逻辑本体在 ``chunked_upload_store.sweep_once``（可单测）；本模块只做
loop + JSONL 审计。**sweeper 不看 ``chunked_upload_enabled``**——kill-switch
关闭后磁盘残留照样要回收。

边界：

- 单轮异常只 log，绝不崩 gateway（同所有 sweeper 先例）。
- 审计 JSONL 落 bind-mount 的 runtime_logs/（docker logs 随 recreate 丢失，
  见 feedback_docker_logs_ephemeral）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_S = int(os.environ.get("AVT_CHUNKED_UPLOAD_SWEEP_INTERVAL_S", "600"))
INITIAL_DELAY_S = int(os.environ.get("AVT_CHUNKED_UPLOAD_SWEEP_INITIAL_DELAY_S", "150"))

_AUDIT_FILENAME = "chunked_upload_sweeper.jsonl"
_DEFAULT_RUNTIME_LOGS_DIR = "/opt/aivideotrans/data/runtime_logs"


def _runtime_logs_dir() -> Path:
    return Path(
        os.environ.get("AIVIDEOTRANS_RUNTIME_LOGS_DIR", _DEFAULT_RUNTIME_LOGS_DIR)
    )


def _append_audit_jsonl(stats: dict) -> None:
    row = {
        "kind": "chunked_upload_sweeper",
        "ts": datetime.now(timezone.utc).isoformat(),
        **stats,
    }
    try:
        audit_dir = _runtime_logs_dir()
        audit_dir.mkdir(parents=True, exist_ok=True)
        with (audit_dir / _AUDIT_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("chunked_upload_sweeper: failed to append audit JSONL (non-fatal)")


def sweep_chunked_uploads_once() -> dict:
    """单轮清扫（同步；loop 内 to_thread 调用）。"""
    from chunked_upload_api import resolve_chunked_limits
    from chunked_upload_store import sweep_once

    limits = resolve_chunked_limits()
    return sweep_once(limits)


async def _interruptible_sleep(delay_s: float, stop_event: asyncio.Event | None) -> bool:
    """Sleep delay_s seconds; return True if stop_event fires early."""
    if stop_event is None:
        await asyncio.sleep(delay_s)
        return False
    if stop_event.is_set():
        return True
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_s)
        return True
    except asyncio.TimeoutError:
        return False


async def sweeper_loop(*, stop_event: asyncio.Event | None = None) -> None:
    """周期清扫循环。单轮失败只 log；loop 继续。"""
    logger.info(
        "chunked_upload_sweeper started (interval=%ss, initial_delay=%ss)",
        SWEEP_INTERVAL_S, INITIAL_DELAY_S,
    )
    if await _interruptible_sleep(INITIAL_DELAY_S, stop_event):
        return
    while True:
        try:
            stats = await asyncio.to_thread(sweep_chunked_uploads_once)
            if sum(stats.values()) > 0:
                logger.info("chunked_upload_sweeper: %s", stats)
                _append_audit_jsonl(stats)
        except Exception:
            logger.exception("chunked_upload_sweeper tick failed; retrying next interval")
        if await _interruptible_sleep(SWEEP_INTERVAL_S, stop_event):
            return


__all__ = [
    "sweep_chunked_uploads_once",
    "sweeper_loop",
    "SWEEP_INTERVAL_S",
    "INITIAL_DELAY_S",
]
