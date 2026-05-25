"""Gateway 侧 mainland_voice_worker 接入层。

plan 2026-05-24 Phase 1.5：把 ``MainlandWorkerClient`` 接到 Gateway 的
配置 + 工厂层，让武汉 ECS 上跑的 mock worker 能被 admin 探活，并为
Phase 2/4 业务路径（voice clone / segment regenerate）预留单一构造入口。

**严格不做的事**（Phase 1.5 范围之外，留给 Phase 2/4）：

- 不接通 voice clone / segment regenerate 的真实调用路径。
- 不动 voice library / `requires_worker` schema。
- 不暴露任何 worker 路径给前端用户。
- 不在 admin response 里返 HMAC secret 实体。

设计点：

1. **Secret 单一存放点**：``settings.mainland_voice_worker_hmac_secret``
   只从 env 读，不进 admin_settings.json、不进 API response、不打日志。
   守卫测试覆盖每条路径。
2. **Fail-graceful 降级**：``validate_mainland_voice_worker_config()`` 在
   secret 缺失时返 False，gateway 启动不挂；admin 看到 effective_enabled=False
   就能定位问题。
3. **工厂返 ``None`` 而非抛**：调用方（未来 voice clone 路径）显式判 None
   做 "worker degraded mode" 降级（plan §Worker Degraded Mode）。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Reuse the same src/ injection pattern as admin_settings.py: in Docker the
# gateway runs in /opt/gateway while ``services.mainland_worker`` lives under
# /opt/aivideotrans/app/src. In local dev, repo_root/src is the right path.
for _candidate in [
    Path(__file__).resolve().parent.parent / "src",  # repo_root/src
    Path("/opt/aivideotrans/app/src"),                # Docker
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from admin_settings import _require_admin
from config import GatewaySettings
from models import User

# 必须延迟到 sys.path 注入之后再 import；否则 mainland_worker 在容器里找不到
from services.mainland_worker.client import (  # noqa: E402
    MainlandWorkerClient,
    WorkerCredentials,
    WorkerError,
    WorkerNetworkError,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_mainland_voice_worker_client(
    settings: GatewaySettings,
) -> MainlandWorkerClient | None:
    """从 ``GatewaySettings`` 构造 ``MainlandWorkerClient`` 或返 ``None``。

    返 ``None`` 的情况（plan §Worker Degraded Mode 的依据）：

    - ``mainland_voice_worker_enabled=False``（默认或被 startup_checks 降级）
    - url / hmac_key_id / hmac_secret 任一缺失（防御性二次校验，理论上
      startup_checks 已经把这种情况降级到 enabled=False，这里再校验一次
      是为了让调用方就算绕过 startup 也能拿到 None 而不是无效 client）

    返非 None 的 client 实例在调用方负责 ``close()``，或者用 ``with`` 语法。
    """
    if not settings.mainland_voice_worker_enabled:
        return None

    url = (settings.mainland_voice_worker_url or "").strip()
    key_id = (settings.mainland_voice_worker_hmac_key_id or "").strip()
    secret = settings.mainland_voice_worker_hmac_secret or ""

    if not (url and key_id and secret):
        logger.warning(
            "build_mainland_voice_worker_client: enabled=True but config incomplete "
            "(url=%r key_id_set=%s secret_set=%s); returning None",
            url,
            bool(key_id),
            bool(secret),
        )
        return None

    return MainlandWorkerClient(
        base_url=url,
        credentials=WorkerCredentials(key_id=key_id, secret=secret),
    )


# ---------------------------------------------------------------------------
# Admin router — 仅 status + healthz；不暴露 secret
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/api/admin/mainland-voice-worker",
    tags=["admin", "mainland-voice-worker"],
)


@router.get("/status")
async def get_status(
    user: User | None = Depends(get_current_user),
) -> dict:
    """读取 mainland_voice_worker 当前配置状态。

    **永远不返 secret 实体**，只返：

    - ``effective_enabled``: 经 startup_checks 降级后的实际状态
    - ``url``: worker 入口（这个不算 secret，部署文档已经公开）
    - ``hmac_key_id``: 当前 key id（运维用，不是 secret）
    - ``has_hmac_secret``: bool，标识 secret 是否被配置

    Phase 2 / 4 时若加更多业务配置（target_model / allowed_plan_codes 等），
    也只在此处加只读字段，绝不返 secret。
    """
    _require_admin(user)

    # 直接从 module-level settings 单例读，确保和实际 client factory 看到的一致
    from config import settings as gw_settings

    return {
        "effective_enabled": bool(gw_settings.mainland_voice_worker_enabled),
        "url": gw_settings.mainland_voice_worker_url,
        "hmac_key_id": gw_settings.mainland_voice_worker_hmac_key_id,
        "has_hmac_secret": bool(gw_settings.mainland_voice_worker_hmac_secret),
    }


@router.get("/healthz")
async def get_worker_healthz(
    user: User | None = Depends(get_current_user),
) -> dict:
    """从 Gateway 主动探活武汉 worker。

    成功路径：返 worker 自己的 /healthz 响应（``ok / worker / region / providers``），
    不附加任何 secret。

    失败路径：
    - worker 未启用 → 503 ``worker_disabled``
    - 网络不通 / 5xx → 502 ``worker_unreachable``
    - 签名拒绝（key 配错）→ 502 ``worker_signature_rejected``

    所有错误返回都不带 secret 实体，只带 code + 简短 message。
    """
    _require_admin(user)

    from config import settings as gw_settings
    client = build_mainland_voice_worker_client(gw_settings)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "worker_disabled",
                "message": "mainland_voice_worker is disabled or misconfigured "
                           "(see /api/admin/mainland-voice-worker/status)",
            },
        )

    try:
        try:
            health = client.health()
        except WorkerNetworkError as exc:
            logger.warning("[gateway] mainland worker healthz unreachable: %s", exc)
            raise HTTPException(
                status_code=502,
                detail={"code": "worker_unreachable", "message": str(exc)},
            ) from exc
        except WorkerError as exc:
            # 签名错误、4xx 等
            code = "worker_signature_rejected" if exc.http_status == 401 else "worker_error"
            logger.warning(
                "[gateway] mainland worker healthz error: status=%d code=%s",
                exc.http_status, exc.code,
            )
            raise HTTPException(
                status_code=502,
                detail={"code": code, "message": f"worker {exc.http_status} {exc.code}"},
            ) from exc

        return {
            "ok": health.ok,
            "worker": health.worker,
            "region": health.region,
            "providers": {
                name: {"configured": p.configured, "mode": p.mode}
                for name, p in health.providers.items()
            },
        }
    finally:
        client.close()
