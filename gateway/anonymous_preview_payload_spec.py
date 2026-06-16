"""APF 匿名 Free 预览 — create payload 字段白名单常量。

T1 共享常量。T3 / T5 / T8 引用同一份，禁止各处自定义副本。

AD-7 (plan 2026-06-10): create payload 字段白名单 — 只允许最小必要字段，
明确禁止 voice_clone / voiceclone_reference_path 等付费 clone 字段，
确保匿名预览全程零 clone provider 调用。
"""

from __future__ import annotations

# 匿名预览 create 允许的 payload 字段白名单。
# T3/T5/T8 构造 create payload 时引用此集合；payload 内其他字段一律拒绝。
ANONYMOUS_PREVIEW_PAYLOAD_SPEC: frozenset[str] = frozenset(
    {
        "job_type",
        "source_type",
        "source_ref",
        "output_target",
        "service_mode",
        "requires_review",
        "voice_strategy",
        "tts_provider",
        "source_content_hash",
        "anonymous_preview",  # G3 标记字段，匿名 lane 穿透用
    }
)

# 明确禁止的字段——即使客户端夹带，也必须拒绝（付费 clone 路径防误触）。
# AD-7 §"payload 字段白名单"：不得出现以下字段，否则 validate_create_payload 报错。
FORBIDDEN_PAYLOAD_FIELDS: frozenset[str] = frozenset(
    {
        "voice_a",
        "voice_b",
        "voice_clone",
        "voiceclone_reference_path",
        "free_consent",
    }
)


def validate_create_payload(payload: dict) -> list[str]:
    """校验匿名预览 create payload 合规性。

    返回违规字段名列表；空列表 = 合法，可以继续创建 job。

    两类违规：
    1. 命中 FORBIDDEN_PAYLOAD_FIELDS（付费 clone / 未授权字段）。
    2. 不在 ANONYMOUS_PREVIEW_PAYLOAD_SPEC 白名单内的其他字段。

    纯 stdlib，无副作用，可在任意上下文调用。
    """
    violations: list[str] = []
    for field in payload:
        if field in FORBIDDEN_PAYLOAD_FIELDS:
            violations.append(field)
        elif field not in ANONYMOUS_PREVIEW_PAYLOAD_SPEC:
            violations.append(field)
    return violations
