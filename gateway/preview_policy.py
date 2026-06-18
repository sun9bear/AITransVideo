"""P3e-3d：智能版预览 stream-only 策略门 gateway 共享助手.

``smart_preview_mode`` 存在 ``Job.smart_state`` JSONB（create 时由
``intercept_create_job`` stamp）。本模块把它读出并经 ``effective_policy_mode``
收敛到最严档 ``"anonymous_preview"``（恒水印 / 零下载 key / 仅 stream video /
不进 R2），与匿名预览一致——预览只看、不交付，看完转完整正式流程才下载。

为何 gateway 侧需新增门（不能只镜像匿名）：匿名预览是**登出**用户，
materials / background-task 等 ``require_auth`` 端点直接被 401 挡，无门可复用；
smart 预览是**登录**免费用户，能到达这些端点 → 必须显式新增 stream-only 门，
否则干净配音音频 / 字幕 / 素材 / 剪映 zip 会被白嫖。

gateway-safe：只 import ``services.r2_publisher_lib.downloadable_keys``（纯策略
常量 + 函数，无 pydub / ``services.jobs`` 传染链；与 ``gateway/job_intercept.py``
既有 ``effective_policy_mode`` import 同源）。
"""
from __future__ import annotations

from typing import Any

from services.r2_publisher_lib.downloadable_keys import effective_policy_mode


def extract_smart_preview_flag(smart_state: Any) -> bool:
    """从 ``smart_state``（dict | None | 坏数据）读 ``smart_preview_mode``。

    严格 ``is True``：非 dict / 缺键 / 非布尔真值（如 JSON 反序列化的 ``"true"``
    字符串）一律 False（fail-safe，绝不把非预览任务误判成预览而误锁下载）。
    """
    return isinstance(smart_state, dict) and smart_state.get("smart_preview_mode") is True


def job_policy_mode(job: Any) -> str | None:
    """Job ORM → 策略档（经 effective_policy_mode 单一真源）。

    同时读 ``is_anonymous_preview``（ORM 列）与 ``smart_state.smart_preview_mode``
    （JSONB），二者任一为真 → 最严档 ``"anonymous_preview"``；否则透传
    ``service_mode``（非预览任务零变化）。
    """
    return effective_policy_mode(
        getattr(job, "service_mode", None),
        bool(getattr(job, "is_anonymous_preview", False)),
        smart_preview=extract_smart_preview_flag(getattr(job, "smart_state", None)),
    )


def job_is_stream_only_preview(job: Any) -> bool:
    """该任务是否为 stream-only 预览（匿名预览或智能版预览）。

    materials / background-task 端点用它 fail-closed 拒绝预览任务的导出 / 下载。
    """
    return job_policy_mode(job) == "anonymous_preview"


__all__ = [
    "extract_smart_preview_flag",
    "job_policy_mode",
    "job_is_stream_only_preview",
]
