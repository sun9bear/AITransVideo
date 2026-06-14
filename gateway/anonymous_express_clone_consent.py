"""匿名 Express 克隆 opt-in consent 验证器（SOFT gate）。

plan 2026-06-14-anonymous-express-cosyvoice-clone-enable §3.1/§4.3.

与 ``anonymous_consent.py``（内容/声音权利，HARD 403 gate）**职责分离**：

- ``anonymous_consent`` = 内容/声音权利确认（``voice_rights_confirmed``）。缺失
  → create 403。所有匿名预览（含纯预设）都要求。
- ``express_consent`` = **独立**的音色克隆 opt-in（``auto_voice_clone``）。这是
  "是否克隆我的音色"的显式勾选，**不是** 403 硬闸：未勾选/缺失 → 不注入
  payload → express 走 CosyVoice 预设音色（soft），不阻断预览创建。

为什么不复用 ``anonymous_consent``：它只含 ``voice_rights_confirmed``，
docstring 明写 "v1 只用预设音色（不克隆任何人声）"。把克隆 opt-in 混进去
会把"内容权利确认"错误推断成"同意自动克隆"（CodeX P1-a）。

strict-bool（拒 1 / "true" coercion 造假，与 ``free_consent`` / ``anonymous_consent``
同模式）。返回 dict 形状必须满足 pipeline 侧
``services.express.pipeline_clients._has_consent``：``auto_voice_clone is True``
+ 调用方盖的 ``server_confirmed_at``。
"""
from __future__ import annotations

from typing import Any


def validate_anonymous_express_clone_consent(
    raw: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the ``express_consent`` voice-clone opt-in (SOFT).

    Returns ``({"auto_voice_clone": True}, None)`` ONLY when ``raw`` is a
    dict with ``auto_voice_clone is True`` (strict bool). Otherwise
    ``(None, reason)``:

    - ``express_clone_consent_missing_or_invalid_type``: not a dict
    - ``auto_voice_clone_not_bool``: present but not a bool (rejects 1 / "true")
    - ``auto_voice_clone_not_confirmed``: field absent, or present and not ``True``

    SOFT semantics: a ``None`` return is **not** an error — it means the user
    did not opt into clone. The caller MUST NOT 403; it simply skips injecting
    ``express_consent`` so the Express lane uses a CosyVoice preset voice.

    调用方负责盖权威 ``server_confirmed_at``（验证器不盖，避免把客户端时间
    当权威）。盖戳前 ``_has_consent`` 为 False，盖戳后为 True。
    """
    if not isinstance(raw, dict):
        return None, "express_clone_consent_missing_or_invalid_type"

    if "auto_voice_clone" not in raw:
        return None, "auto_voice_clone_not_confirmed"
    confirmed = raw["auto_voice_clone"]
    if not isinstance(confirmed, bool):
        return None, "auto_voice_clone_not_bool"
    if confirmed is not True:
        return None, "auto_voice_clone_not_confirmed"

    return {"auto_voice_clone": True}, None


__all__ = ["validate_anonymous_express_clone_consent"]
