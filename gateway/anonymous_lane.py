"""APF 匿名预览 lane resolver（plan 2026-06-12 anonymous-express-preview §A/§E）.

单点解析当前匿名漏斗的活动 lane：

* ``"express"`` — anonymous_express_enabled=True 且 express_tts_provider
  非 mimo（§E② runtime 防御纵深）；优先级最高。
* ``"free"``    — anonymous_free_preview_enabled=True 且 express 不可用。
* ``None``      — 两 lane 都关（master gate 关）或 admin 读取失败（fail-closed）。

消费方（gate 拓扑 §A）：

* **新 intake**（session 创建、upload、chunked init、create）：master gate =
  env ``enable_anonymous_preview`` AND ``resolve_anonymous_lane() is not None``。
* **生命周期端点**（status/stream/TTL、chunked status/delete）对 lane 开关
  **零感知**——只看 env flag + record/state 存在性，绝不 import 本模块做 gate
  （R2 #4：切 lane 开关不得杀旧 record）。

Import constraints
------------------
* 不 import ``services.jobs`` / ``src.pipeline``（gateway 容器无 pydub）。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

LANE_EXPRESS = "express"
LANE_FREE = "free"

# 匿名 express 的 tts_provider 白名单——lane 开关判定与 create payload
# 解析共用的唯一真源（CodeX 第三轮 P2：只拒 mimo 不够，minimax 等不支持
# 的 provider 会让 lane 照开、上传锁进 express、而 /create 又拒绝同一
# provider → 被接收的 preview 永远无法 create 还烧配额）。
# 刻意不含 mimo：恒定单音色违背"免费触点必须真实管线效果"最高原则（§E）。
VALID_ANON_EXPRESS_TTS_PROVIDERS = frozenset({"cosyvoice", "volcengine"})


def _load_admin_settings():
    """惰性读 admin settings（独立函数便于测试注入；调用方自行兜底异常）。"""
    from admin_settings import load_settings

    return load_settings()


def _express_lane_open(adm) -> bool:
    """express lane 是否可用：主开关开 + provider 白名单（§E② 防御纵深）。

    admin 保存校验（validate_anonymous_express_tts_exclusion，T0）已在 POST
    /settings 双向 422 拦截 express+mimo 组合；这里兜手改 admin_settings.json
    / 旧 full-body POST 残留的情形。CodeX 第三轮 P2 收紧：判定与 create 的
    payload provider 解析共用 VALID_ANON_EXPRESS_TTS_PROVIDERS——任何白名单
    外的 provider（mimo / minimax / 未知值）都不开 express lane（回落
    free/None），否则上传会被锁进 express 而 /create 拒绝同一 provider，
    被接收的 preview 永远无法 create 还消耗配额。
    """
    if not bool(getattr(adm, "anonymous_express_enabled", False)):
        return False
    provider = str(getattr(adm, "express_tts_provider", "") or "").strip().lower()
    if provider not in VALID_ANON_EXPRESS_TTS_PROVIDERS:
        logger.warning(
            "anonymous_lane: express lane enabled but express_tts_provider=%r "
            "不在匿名白名单 %s — 拒绝 express lane（回落 free/None）；请检查 "
            "admin 配置", provider, sorted(VALID_ANON_EXPRESS_TTS_PROVIDERS),
        )
        return False
    return True


def resolve_anonymous_lane(adm=None) -> Optional[str]:
    """解析当前活动 lane："express" | "free" | None。

    * express 优先（D1）；free 开关保持开时切到 express 不需要先关 free。
    * ``adm`` 缺省时惰性读 admin settings；读取任何异常 → fail-closed None。
    * 返回值即 master gate 的「任一 lane 开启」分量；新 intake 的 lane 锁定
      值也以本函数返回为准（普通上传写 record.mode，chunked init 写 state）。
    """
    if adm is None:
        try:
            adm = _load_admin_settings()
        except Exception as exc:  # noqa: BLE001 — fail-closed
            logger.warning(
                "anonymous_lane: failed to read admin_settings — fail-closed "
                "(no lane): %s", exc,
            )
            return None
    if _express_lane_open(adm):
        return LANE_EXPRESS
    if bool(getattr(adm, "anonymous_free_preview_enabled", False)):
        return LANE_FREE
    return None


def express_lane_open(adm=None) -> bool:
    """express lane 单独判定（create 端 record.mode=="express" 的门）。

    与 resolve 的区别：不回落 free——express record 的 create 只关心 express
    lane 本身是否仍开（含 mimo 防御纵深）。admin 读取异常 fail-closed False。
    """
    if adm is None:
        try:
            adm = _load_admin_settings()
        except Exception as exc:  # noqa: BLE001 — fail-closed
            logger.warning(
                "anonymous_lane: failed to read admin_settings — fail-closed "
                "(express gate): %s", exc,
            )
            return False
    return _express_lane_open(adm)
