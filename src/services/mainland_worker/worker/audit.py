"""Worker 审计日志（plan §审计日志）。

JSONL append-only：每行一个 event，字段如 plan 所列。raw audio bytes、
API key、HMAC secret 永远不进 audit log。

线程安全：用 ``threading.Lock`` 保护 file append。worker 是单进程
uvicorn，多线程 worker 也能正确串行写入。

测试用 ``InMemoryAuditLogger`` 替代 disk JSONL：覆盖 worker handler
行为时可以直接断言 ``logger.events`` 列表内容，不必读盘。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


logger = logging.getLogger(__name__)


# Plan §审计日志 字段集合（用作 emit() 入参 sanitize 的白名单）
#
# Phase 4.0b §A 增加：``segment_id`` 用于按段定位（synthesize_segment 路径），
# 不会泄漏敏感数据，但便于按 segment 对账。``request_id`` 字段语义在 Phase 4.0b
# 重新定义为 "worker_request_id"（worker 端 UUID），audit log 的 ``provider_request_id``
# 字段（plan §账单观测）用于存 DashScope SDK 的真实 request id（real 模式可空）。
_AUDIT_FIELDS = {
    "event_id",
    "request_id",          # worker_request_id alias（历史名）
    "job_id",
    "user_id",
    "speaker_id",
    "segment_id",          # Phase 4.0b: synthesize_segment 落 audit 时按段定位
    "voice_id",
    "operation",
    "provider",
    "target_model",
    "provider_request_id", # Phase 4.0b: DashScope SDK request id（nullable）
    "status",
    "duration_ms",
    "billed_chars",
    "audio_seconds",
    "artifact_bytes",
    "error_code",
    "created_at",
}

# 禁止字段（防止调用方误把敏感数据塞进 audit log）
_FORBIDDEN_FIELDS = {
    "raw_audio",
    "audio_bytes",
    "api_key",
    "dashscope_api_key",
    "hmac_secret",
    "secret",
    "password",
    "token",
}


class AuditLogger(Protocol):
    """Audit logger 协议。"""

    def emit(self, event: dict[str, Any]) -> None:
        """记录一条 audit 事件。"""
        ...


@dataclass
class JsonlAuditLogger:
    """落盘 JSONL 实现（生产路径）。

    每条 event 一行；文件用 ``a`` 模式打开、写入后 flush。``Lock`` 串
    行化 append，避免多线程交错写入。
    """
    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: dict[str, Any]) -> None:
        sanitized = _sanitize_event(event)
        line = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")


@dataclass
class InMemoryAuditLogger:
    """In-memory 实现（单元测试用）。"""
    events: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(_sanitize_event(event))


def _sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    """剔除非白名单字段 / 禁止字段。

    **默认丢弃未知字段**：新审计字段必须先进 ``_AUDIT_FIELDS`` 白名单。
    旧版本曾 passthrough 未知字段，导致像 ``sample_url`` / ``authorization``
    / ``dashscope_response`` 这种潜在敏感字段会被悄悄落盘。改成默认丢
    并 log warning，让漏加白名单的字段在开发期立刻浮现。

    遇到禁止字段 / 未知字段都不抛异常，避免审计写入失败拖垮主流程
    （plan §审计日志 "audit emit 失败不应影响业务"）。
    """
    cleaned: dict[str, Any] = {}
    for key, value in event.items():
        if key in _FORBIDDEN_FIELDS:
            logger.warning(
                "audit.emit dropped forbidden field %r (would leak secret/audio bytes)",
                key,
            )
            continue
        if key not in _AUDIT_FIELDS:
            # 默认 drop：未在白名单的字段一律不落盘，防止悄悄漂移。
            # 如果是合法新字段，请先在 _AUDIT_FIELDS 和 plan §审计日志 同步增列。
            logger.warning(
                "audit.emit dropped unknown field %r — add to _AUDIT_FIELDS first",
                key,
            )
            continue
        cleaned[key] = value
    return cleaned
