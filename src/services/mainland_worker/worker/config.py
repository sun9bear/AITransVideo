"""Worker 运行时配置 — 从 env 读取。

env 变量约定（plan §Secret Management 注入方式）：

================================  ============================================
``WORKER_MODE``                   ``"mock"``（默认）或 ``"live"``
``WORKER_HMAC_KEYS``              ``"{key_id}:{secret}[,{key_id}:{secret}...]"``
``WORKER_HMAC_DEPRECATED_KEYS``   ``"{key_id}:{secret}:{deprecated_at}[,...]"``
``WORKER_AUDIT_LOG_PATH``         JSONL audit log 路径
``WORKER_ARTIFACT_DIR``           artifact zip / wav 临时存放目录
``WORKER_REGION``                 default ``"cn-wuhan"``
``WORKER_NAME``                   default ``"aivideotrans-mainland-worker"``
================================  ============================================

测试代码可以直接用 ``WorkerConfig(...)`` 构造，不必走 env — 这是为了
保持单元测试无副作用。

注意：本模块**不读取 DashScope API Key**。Phase 4 真实 provider 时，
``providers.real_cosyvoice`` 自己从 env 读取，确保 mock 路径下 worker
启动不依赖任何付费 API 凭证（CLAUDE.md 付费 API 硬约束）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from services.mainland_worker.hmac_auth import HmacKey


logger = logging.getLogger(__name__)


WORKER_MODE_MOCK = "mock"
WORKER_MODE_LIVE = "live"

DEFAULT_WORKER_NAME = "aivideotrans-mainland-worker"
DEFAULT_WORKER_REGION = "cn-wuhan"


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Worker 运行时配置。"""
    mode: str  # "mock" | "live"
    hmac_keys: tuple[HmacKey, ...]
    audit_log_path: Path
    artifact_dir: Path
    worker_name: str = DEFAULT_WORKER_NAME
    worker_region: str = DEFAULT_WORKER_REGION


def _parse_active_keys(raw: str) -> list[HmacKey]:
    """``"key1:secret1,key2:secret2"`` → [HmacKey(...)]。"""
    keys: list[HmacKey] = []
    if not raw:
        return keys
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 1)
        if len(parts) != 2:
            raise ValueError(
                f"WORKER_HMAC_KEYS entry must be 'key_id:secret', got {item!r}"
            )
        key_id, secret = parts[0].strip(), parts[1].strip()
        if not key_id or not secret:
            raise ValueError(
                f"WORKER_HMAC_KEYS entry has empty field: {item!r}"
            )
        keys.append(HmacKey(key_id=key_id, secret=secret))
    return keys


def _parse_deprecated_keys(raw: str) -> list[HmacKey]:
    """``"key1:secret1:1700000000,..."`` → [HmacKey(deprecated_at=...)]。"""
    keys: list[HmacKey] = []
    if not raw:
        return keys
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 2)
        if len(parts) != 3:
            raise ValueError(
                f"WORKER_HMAC_DEPRECATED_KEYS entry must be 'key_id:secret:deprecated_at', got {item!r}"
            )
        key_id, secret, ts_raw = (p.strip() for p in parts)
        try:
            deprecated_at = int(ts_raw)
        except ValueError as exc:
            raise ValueError(
                f"WORKER_HMAC_DEPRECATED_KEYS deprecated_at not int: {ts_raw!r}"
            ) from exc
        keys.append(HmacKey(key_id=key_id, secret=secret, deprecated_at=deprecated_at))
    return keys


def load_from_env(env: dict[str, str] | None = None) -> WorkerConfig:
    """从 env 读取，构造 ``WorkerConfig``。

    env 缺失时按下列默认值兜底：

    - ``WORKER_MODE``: ``"mock"``
    - ``WORKER_HMAC_KEYS``: 必填，否则 ``ValueError``
    - ``WORKER_AUDIT_LOG_PATH``: ``/data/aivideotrans-mainland-worker/audit/worker-audit.jsonl``
    - ``WORKER_ARTIFACT_DIR``: ``/data/aivideotrans-mainland-worker/artifacts``
    """
    src = env if env is not None else os.environ

    mode = (src.get("WORKER_MODE") or WORKER_MODE_MOCK).strip()
    if mode not in (WORKER_MODE_MOCK, WORKER_MODE_LIVE):
        raise ValueError(
            f"WORKER_MODE must be 'mock' or 'live', got {mode!r}"
        )

    active_keys = _parse_active_keys(src.get("WORKER_HMAC_KEYS", ""))
    deprecated_keys = _parse_deprecated_keys(src.get("WORKER_HMAC_DEPRECATED_KEYS", ""))
    all_keys = active_keys + deprecated_keys
    if not all_keys:
        raise ValueError(
            "WORKER_HMAC_KEYS must contain at least one 'key_id:secret' entry"
        )

    audit_log_path = Path(
        src.get(
            "WORKER_AUDIT_LOG_PATH",
            "/data/aivideotrans-mainland-worker/audit/worker-audit.jsonl",
        )
    )
    artifact_dir = Path(
        src.get(
            "WORKER_ARTIFACT_DIR",
            "/data/aivideotrans-mainland-worker/artifacts",
        )
    )

    return WorkerConfig(
        mode=mode,
        hmac_keys=tuple(all_keys),
        audit_log_path=audit_log_path,
        artifact_dir=artifact_dir,
        worker_name=src.get("WORKER_NAME", DEFAULT_WORKER_NAME),
        worker_region=src.get("WORKER_REGION", DEFAULT_WORKER_REGION),
    )
