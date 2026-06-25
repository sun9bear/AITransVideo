"""Shared type contracts for gateway job policy (TU-07 / TS-05).

Lives in its own module so ``gateway/job_intercept.py`` (a whitelisted large
file) imports the shape rather than growing past its size-ratchet baseline.
"""

from __future__ import annotations

from typing import TypedDict


class JobPolicy(TypedDict):
    """``compute_job_policy`` 的返回形状。

    所有分支（smart / free×2 / studio / express）返回同一 flat dict。
    ``tts_model`` 为 ``str | None`` —— studio + volcengine（豆包 2.0 公共音色）
    时为 None；其余分支为具体模型名。其它字段恒为对应标量类型。
    """

    service_mode: str
    tts_provider: str
    tts_model: str | None
    requires_review: bool
    voice_clone_enabled: bool
    voice_strategy: str
    plan_code_snapshot: str
    role_snapshot: str
    quality_tier: str
