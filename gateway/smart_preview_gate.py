"""P3e-4a：智能版免费预览 lane 准入判定（gateway create-path 用）.

plan 2026-06-14-p3e2-preview-lane-design.md §7 / §8。

把"免费 / 未获 smart entitlement 的登录用户能否进入受限智能版预览 lane"的判定抽成
独立纯函数：便于单元测试（不 import 重的 ``job_intercept``），并被
``intercept_create_job`` 的两道 entitlement gate（smart kill switch + plan gate）复用。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["smart_preview_lane_exempt"]


def smart_preview_lane_exempt(request_data: Any, user: Any) -> bool:
    """免费 / 未获 smart 的登录用户进入**受限**智能版预览 lane 的放行判定。

    放行 = 以下全部成立：

      (a) 已登录（``user is not None``）—— 预览要落 600 reservation，必须有 owner；
      (b) 本次为显式预览请求（``request_data['preview_mode'] is True``）；
      (c) 本次确为**克隆**预览请求（``smart_consent.auto_voice_clone is True``）；
      (d) 通用 smart kill switch 处于开启（env ``enable_smart_mode`` 且 admin
          ``smart_mode_enabled``）—— 见下"耦合通用停"；
      (e) admin canary 旗 ``smart_preview_clone_enabled is True``。

    放行后该用户**只能**跑受限预览：3min 水印 teaser（P3e-3b）、只扣 600 克隆、跳分钟
    （P3e-3c-1）、stream-only（P3e-3d）——全部由下游 ``smart_state.smart_preview_mode``
    服务端强制；``preview_mode=True`` 换不到完整付费 smart 产物。非预览的 smart 请求一律
    仍走原 entitlement 403。

    **为何条件 (c) 必需**（CodeX/对抗性 P1）：``smart_preview_mode`` 只在 600-reserve 成功
    分支里被 stamp，而 600-reserve 的触发条件正是 ``auto_voice_clone is True``。若放行只看
    ``preview_mode`` 而不看 ``auto_voice_clone``，免费用户可发 ``preview_mode=True`` +
    ``auto_voice_clone=False`` 越过 gate、却跳过 reserve、也不被 stamp 成预览 → 拿到一个
    **不受限的完整 smart 成片**（完整长度 / 无水印 / 可下载 / 按分钟计费）。要求 (c) 与
    600-reserve 触发条件对齐，杜绝该越权。**注**：``auto_voice_clone=True`` 但 600 预留失败
    （余额不足等）→ 仍不该落成完整任务，由 create 路径在 reserve 后显式拒绝兜底。

    **耦合通用停（条件 (d)，对抗性 HIGH）**：预览 lane 跑的是**同一 smart 管线** + **同一
    付费克隆 API**。ops 的紧急停（``smart_mode_enabled=False`` 大红钮，或 env
    ``AVT_ENABLE_SMART_MODE`` 关）若不能同时停预览，则形同虚设——管线级事故（成本失控 /
    质量回归）对预览任务一样有害。故放行先要求通用 kill switch 双层（env+admin）开启；预览
    lane 自己的开关 ``smart_preview_clone_enabled`` 仍**额外**收窄（关它即可单停本 lane，
    不必动通用停）。两层 kill switch 语义与 ``entitlements.get_effective_allowed_service_modes``
    一致（env ``enable_smart_mode`` AND admin ``smart_mode_enabled``）。

    **默认 inert**：``smart_preview_clone_enabled`` 默认 False → 放行恒 False → create
    路径字节级不变。**fail-closed**：``admin_settings`` / ``config`` 读取异常 → 放行 False
    并 ``logger.warning``（绝不因读配置失败而误放行会扣 600 + 调付费克隆的路径）。

    严格 ``is True``：``preview_mode`` / ``auto_voice_clone`` 非布尔真值（如 ``"true"`` /
    ``1``）一律不放行，与下游 ``extract_smart_preview_flag`` 的 fail-safe 语义对齐，杜绝
    "蒙混进 lane 又被下游当非预览处理"的撕裂态。
    """
    if user is None:
        return False
    if not isinstance(request_data, dict):
        return False
    if request_data.get("preview_mode") is not True:
        return False
    consent = request_data.get("smart_consent")
    if not isinstance(consent, dict) or consent.get("auto_voice_clone") is not True:
        return False
    try:
        from admin_settings import load_settings as _load_admin
        from config import settings as _settings

        _admin = _load_admin()
        # 通用 smart kill switch 两层（env AND admin）——见 entitlements.py:80-96。
        env_enabled = bool(getattr(_settings, "enable_smart_mode", False))
        admin_enabled = bool(getattr(_admin, "smart_mode_enabled", False))
        if not (env_enabled and admin_enabled):
            return False
        return getattr(_admin, "smart_preview_clone_enabled", False) is True
    except Exception as exc:  # noqa: BLE001 — fail-closed
        logger.warning(
            "smart_preview_lane_exempt: admin/config unreadable, fail-closed "
            "(放行 False): %s", exc,
        )
        return False
