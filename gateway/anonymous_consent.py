"""APF P0 T8 — 匿名预览内容权利 consent 验证器（HARD-fail）。

照 ``gateway/free_consent.py`` 三件套模式（plan 2026-06-10 AD-7）：

1. strict-bool 纯函数验证（拒 1 / "true" coercion 造假）；
2. 调用方在验证通过后盖权威 ``server_confirmed_at``；
3. 转发 Job API 前 pop 客户端夹带值——本 surface 的 create payload
   白名单（``ANONYMOUS_PREVIEW_PAYLOAD_SPEC``）本身不含 consent 字段，
   consent 只进 preview record 的 audit，不进 Job API payload。

匿名预览虽然 v1 只用预设音色（不克隆任何人声），但产物会复刻源视频
的内容与人声语义（翻译配音），上传者必须确认对源内容握有权利。
"""
from __future__ import annotations

from typing import Any


def validate_anonymous_consent(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the ``anonymous_consent`` payload (HARD).

    Returns ``(payload, None)`` ONLY when ``raw`` is a dict with
    ``voice_rights_confirmed is True``. Otherwise ``(None, reason)``:

    - ``anonymous_consent_missing_or_invalid_type``: not a dict
    - ``voice_rights_not_confirmed``: field absent, or present and not ``True``
    - ``voice_rights_confirmed_not_bool``: present but not a bool (rejects 1 / "true")
    - ``client_confirmed_at_not_string``: present but not a string

    调用方负责盖权威 ``server_confirmed_at``。
    """
    if not isinstance(raw, dict):
        return None, "anonymous_consent_missing_or_invalid_type"

    if "voice_rights_confirmed" not in raw:
        return None, "voice_rights_not_confirmed"
    confirmed = raw["voice_rights_confirmed"]
    if not isinstance(confirmed, bool):
        return None, "voice_rights_confirmed_not_bool"
    if confirmed is not True:
        return None, "voice_rights_not_confirmed"

    client_confirmed_at: str | None = None
    if "client_confirmed_at" in raw and raw["client_confirmed_at"] is not None:
        if not isinstance(raw["client_confirmed_at"], str):
            return None, "client_confirmed_at_not_string"
        normalized = raw["client_confirmed_at"].strip()
        if normalized:
            client_confirmed_at = normalized

    return {
        "voice_rights_confirmed": True,
        "client_confirmed_at": client_confirmed_at,
    }, None


__all__ = ["validate_anonymous_consent"]
