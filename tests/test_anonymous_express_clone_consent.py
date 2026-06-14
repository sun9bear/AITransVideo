"""P1 — 匿名 Express 克隆 opt-in consent 验证器（SOFT gate）.

plan 2026-06-14-anonymous-express-cosyvoice-clone-enable §3.1/§4.3.

与 ``anonymous_consent.py``（内容/声音权利，HARD 403 gate）**分离**：
- ``anonymous_consent`` 只含 ``voice_rights_confirmed``，缺失 → create 403。
- ``express_consent`` 是**独立**的克隆 opt-in（``auto_voice_clone``），缺失/
  未勾选 → **不报错**、不注入 payload、express 走 CosyVoice 预设音色（soft）。

strict-bool（拒 1 / "true" coercion 造假）；返回 dict 形状必须满足
``maybe_run_express_auto_clone._has_consent``：``auto_voice_clone is True``
+ 调用方盖的 ``server_confirmed_at``。
"""
from __future__ import annotations

import pytest

from anonymous_express_clone_consent import validate_anonymous_express_clone_consent


def test_valid_opt_in_returns_consent_dict():
    payload, reason = validate_anonymous_express_clone_consent(
        {"auto_voice_clone": True}
    )
    assert reason is None
    assert payload == {"auto_voice_clone": True}


def test_missing_field_is_soft_optout_not_error():
    """缺 auto_voice_clone → (None, reason)，调用方据此不注入 → 预设（非 403）。"""
    payload, reason = validate_anonymous_express_clone_consent({})
    assert payload is None
    assert reason == "auto_voice_clone_not_confirmed"


def test_false_is_soft_optout():
    payload, reason = validate_anonymous_express_clone_consent(
        {"auto_voice_clone": False}
    )
    assert payload is None
    assert reason == "auto_voice_clone_not_confirmed"


def test_strict_bool_rejects_coercion():
    """strict-bool：1 / "true" / "1" / "on" 不得被当作 True。"""
    for bad in (1, "true", "1", "on", "True"):
        payload, reason = validate_anonymous_express_clone_consent(
            {"auto_voice_clone": bad}
        )
        assert payload is None, f"{bad!r} 不应被解析为已同意克隆"
        assert reason == "auto_voice_clone_not_bool"


def test_non_dict_is_soft_optout():
    for raw in (None, "yes", 1, [], True):
        payload, reason = validate_anonymous_express_clone_consent(raw)
        assert payload is None
        assert reason == "express_clone_consent_missing_or_invalid_type"


def test_returned_dict_satisfies_has_consent_after_stamp():
    """返回 dict + 调用方盖 server_confirmed_at 后，必须满足 pipeline 侧
    maybe_run_express_auto_clone._has_consent 的形状契约。"""
    from services.express.pipeline_clients import _has_consent

    payload, _ = validate_anonymous_express_clone_consent({"auto_voice_clone": True})
    assert payload is not None
    # 验证器不盖时间戳（调用方职责）→ 盖之前 _has_consent 为 False
    assert _has_consent(payload) is False
    payload["server_confirmed_at"] = "2026-06-14T00:00:00+00:00"
    assert _has_consent(payload) is True
