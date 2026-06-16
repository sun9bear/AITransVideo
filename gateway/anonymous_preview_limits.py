"""APF 限制解析层 — admin 热配置优先，env settings fail-safe fallback（2026-06-11）。

把 7 个原 env-only 的匿名预览限制（上传大小 / 预览长度 / 源上传时长 / 四个每日 cap）
统一收口到 ``resolve_apf_limits()``：

* admin_settings.json 读取成功 → 用 admin 值（``anonymous_preview_max_upload_mb``
  在此处统一 ×1024×1024 转字节）。
* 读取/解析/字段访问**任何异常** → 回落 env ``GatewaySettings`` 值（出厂默认），
  并 WARNING 日志。限制读取层故障不能让漏斗整体不可用（fail-safe，非 fail-closed
  ——这里只是数值边界，主开关 fail-closed 逻辑在 ``_get_admin_enabled``）。

``ApfLimits`` 字段名与 ``GatewaySettings`` 的 env 字段**完全同名**
（``anonymous_preview_max_upload_bytes`` 等），消费方可以把它当 settings
直接传——例如 ``admit_for_free_preview(dur, limits)`` 无需改 policy 模块。

热生效：本模块不做任何缓存，每次调用重读 admin_settings.json
（``load_settings`` 本身就是每次重读文件），admin 后台改完即时生效。

Import constraints
------------------
* ``admin_settings`` 在函数内 lazy import（模块较重：httpx / models / database），
  且 import 失败本身也要走 env fallback。
* 不 import ``services.jobs`` / ``src.pipeline``（pydub guard）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config import settings as _env_settings

logger = logging.getLogger(__name__)

__all__ = ["ApfLimits", "resolve_apf_limits"]


@dataclass(frozen=True)
class ApfLimits:
    """匿名预览运行时限制快照。

    字段名与 ``GatewaySettings`` env 字段逐一同名——契约守卫见
    ``tests/test_anonymous_preview_limits_knobs.py``，改名会 red。
    """

    anonymous_preview_max_upload_bytes: int
    anonymous_preview_max_seconds: int
    # 源视频上传时长上限（秒）——与预览长度解耦（2026-06-16）。intake 据此拒超长源。
    anonymous_preview_max_source_seconds: int
    anonymous_preview_cap_global_per_day: int
    anonymous_preview_cap_per_ip: int
    anonymous_preview_cap_per_device: int
    anonymous_preview_cap_per_source: int


def _limits_from_env(env_settings) -> ApfLimits:
    """从 env GatewaySettings 构造（fallback 落点 = 出厂默认）。"""
    return ApfLimits(
        anonymous_preview_max_upload_bytes=int(env_settings.anonymous_preview_max_upload_bytes),
        anonymous_preview_max_seconds=int(env_settings.anonymous_preview_max_seconds),
        anonymous_preview_max_source_seconds=int(env_settings.anonymous_preview_max_source_seconds),
        anonymous_preview_cap_global_per_day=int(env_settings.anonymous_preview_cap_global_per_day),
        anonymous_preview_cap_per_ip=int(env_settings.anonymous_preview_cap_per_ip),
        anonymous_preview_cap_per_device=int(env_settings.anonymous_preview_cap_per_device),
        anonymous_preview_cap_per_source=int(env_settings.anonymous_preview_cap_per_source),
    )


def resolve_apf_limits(env_settings=None) -> ApfLimits:
    """Resolve 当前生效的 APF 限制（admin 优先 → env fallback）。

    ``env_settings`` 仅供测试注入；生产恒用 ``config.settings``。
    注意 ``load_settings`` 自身对 JSON 解析失败已返回 Pydantic 默认值
    （与 env 默认严格一致），所以走到 except 分支的是更深的故障
    （import 失败 / 文件系统异常 / 字段被外部写坏到 validator 都救不了）。
    """
    env = env_settings if env_settings is not None else _env_settings
    try:
        from admin_settings import load_settings

        adm = load_settings()
        return ApfLimits(
            anonymous_preview_max_upload_bytes=int(adm.anonymous_preview_max_upload_mb) * 1024 * 1024,
            anonymous_preview_max_seconds=int(adm.anonymous_preview_max_seconds),
            anonymous_preview_max_source_seconds=int(adm.anonymous_preview_max_source_seconds),
            anonymous_preview_cap_global_per_day=int(adm.anonymous_preview_cap_global_per_day),
            anonymous_preview_cap_per_ip=int(adm.anonymous_preview_cap_per_ip),
            anonymous_preview_cap_per_device=int(adm.anonymous_preview_cap_per_device),
            anonymous_preview_cap_per_source=int(adm.anonymous_preview_cap_per_source),
        )
    except Exception as exc:  # noqa: BLE001 — 任何异常都回落 env（fail-safe）
        logger.warning(
            "resolve_apf_limits: admin settings unavailable, falling back to env: %s",
            exc,
        )
        return _limits_from_env(env)
