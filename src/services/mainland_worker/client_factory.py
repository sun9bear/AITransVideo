"""Env-only MainlandWorkerClient factory（Phase 4.1 D.2, 2026-05-25）。

Pipeline 子进程（``src/services/...``）调武汉 worker 时的**唯一 secret 入口**。
读 ``AVT_MAINLAND_VOICE_WORKER_*`` 三件套 env，返 ``MainlandWorkerClient`` 或
``None``。

为什么不复用 ``gateway/mainland_voice_worker.py::build_mainland_voice_worker_client``：

- Pipeline 子进程跑在 ``src/services`` 命名空间，**不能 import gateway/**。
  Phase 4.1 守卫测试 ``test_services_tts_does_not_import_gateway`` 把这层
  禁令 AST 化（D.7）。
- HMAC secret 必须从 env 直接读，**不允许**经 job spec / runtime_config /
  任何 JSON 文件流入（Codex 2026-05-25 D 重点 #5）。
- ``process_runner.py:236`` 已经用 ``os.environ.copy()`` 把 gateway 容器的
  env 传给子进程，因此 env 是天然可用的传递通道，job spec 不需要带 secret。

返 None 的情况（与 gateway 工厂语义一致）：

- ``AVT_MAINLAND_VOICE_WORKER_ENABLED`` ≠ true
- url / hmac_key_id / hmac_secret 任一缺失

调用方（``TTSGenerator._generate_one_cosyvoice_via_worker`` / E 阶段 segment
producer）拿到 None 必须**显式失败**（``TTSGenerationError("worker unavailable")``）
而**不是** fallback 到 MiniMax / 其它 provider（CLAUDE.md 付费 API 硬约束）。
"""
from __future__ import annotations

import logging
import os

from services.mainland_worker.client import MainlandWorkerClient, WorkerCredentials


logger = logging.getLogger(__name__)


# Env var 名（与 gateway/config.py 的 ``AVT_`` 前缀 + 字段名约定保持一致；
# 不在这里硬编码 fallback 名，避免与 Gateway 端漂移）
ENV_ENABLED = "AVT_MAINLAND_VOICE_WORKER_ENABLED"
ENV_URL = "AVT_MAINLAND_VOICE_WORKER_URL"
ENV_KEY_ID = "AVT_MAINLAND_VOICE_WORKER_HMAC_KEY_ID"
ENV_SECRET = "AVT_MAINLAND_VOICE_WORKER_HMAC_SECRET"


_TRUTHY_LITERALS = frozenset({"1", "true", "yes", "on"})


def _env_truthy(raw: str | None) -> bool:
    """Pydantic-BaseSettings 兼容的 bool 解析（不抛异常的版本）。

    Gateway 端用 pydantic 解析 ``AVT_MAINLAND_VOICE_WORKER_ENABLED``，会接受
    ``"1" / "true" / "yes" / "on"``（不区分大小写）作为 True。这里复制同样的
    解析规则，让 pipeline 子进程看到的 ``enabled`` 与 Gateway 一致。
    """
    if not raw:
        return False
    return raw.strip().lower() in _TRUTHY_LITERALS


def is_worker_enabled_in_env() -> bool:
    """轻量探针：读 ``AVT_MAINLAND_VOICE_WORKER_ENABLED`` 判断武汉 worker 是否启用。

    不构造 client、不 I/O、不读 url/key_id/secret —— 仅看 enable 开关。
    调用方（如 ``pipeline.process._build_voice_selection_review`` 决定前端是否
    渲染 CosyVoice 克隆按钮）需要快速判断"运维是否打开了 mainland 通道"，
    但不需要真造 client。

    返 True 不代表配置完整。完整路径用 ``build_client_from_env()`` 拿真 client。
    """
    return _env_truthy(os.environ.get(ENV_ENABLED))


def build_client_from_env() -> MainlandWorkerClient | None:
    """从 ``os.environ`` 构造 ``MainlandWorkerClient`` 或返 ``None``。

    **不读任何参数 / 任何文件**——secret 单一来源是 env。

    返非 None 的 client 实例在调用方负责 ``close()``，或者用 ``with`` 语法
    （MainlandWorkerClient 支持 context manager）。
    """
    if not _env_truthy(os.environ.get(ENV_ENABLED)):
        # enabled=False 是预期常态（dev / CI / 国际部署），不打 warning
        return None

    url = (os.environ.get(ENV_URL) or "").strip()
    key_id = (os.environ.get(ENV_KEY_ID) or "").strip()
    secret = os.environ.get(ENV_SECRET) or ""

    if not (url and key_id and secret):
        # enabled=True 但配置不全 —— 是运维错误，但**不抛异常**，让调用方
        # 自己判断 None 并显式失败（避免 import 期 / 工厂层抛异常把整个
        # pipeline 子进程拉崩）。
        logger.warning(
            "build_client_from_env: %s=true but config incomplete "
            "(url=%r key_id_set=%s secret_set=%s); returning None",
            ENV_ENABLED, url, bool(key_id), bool(secret),
        )
        return None

    return MainlandWorkerClient(
        base_url=url,
        credentials=WorkerCredentials(key_id=key_id, secret=secret),
    )
