"""Phase 4.3a PR1 review-fix-2 — GitHub Codex PR #17 复审 2 条 P2。

P2-1：非 Express job 不得保留客户端夹带的 forged express_consent。
  - _apply_validated_express_consent 转发前无条件 pop，只写回 validated 值
P2-2：is_temporary=true 必须有合法 temporary_expires_at，否则 400。
  - 防"临时但永不过期"row（占 active_temp_cap + sweeper 扫不到）
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

_GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ===========================================================================
# P2-1: _apply_validated_express_consent
# ===========================================================================


def test_studio_forged_express_consent_stripped():
    """Studio/Smart job（express_consent_payload=None）夹带 forged
    express_consent → 转发体必须没有它。"""
    from job_intercept import _apply_validated_express_consent
    request_data = {
        "service_mode": "studio",
        # 客户端 forge 的 express_consent（含伪造的 server_confirmed_at）
        "express_consent": {
            "auto_voice_clone": True,
            "server_confirmed_at": "2020-01-01T00:00:00Z",  # 伪造
        },
        "express_consent_parse_error": "forged",
    }
    _apply_validated_express_consent(
        request_data,
        express_consent_payload=None,   # 非 Express path → None
        express_consent_parse_error=None,
    )
    assert "express_consent" not in request_data, (
        "Studio job 的 forged express_consent 必须被清掉"
    )
    assert "express_consent_parse_error" not in request_data


def test_smart_forged_express_consent_stripped():
    from job_intercept import _apply_validated_express_consent
    request_data = {
        "service_mode": "smart",
        "express_consent": {"auto_voice_clone": True},
        "smart_consent": {"auto_voice_clone": True},  # 合法 smart 字段不动
    }
    _apply_validated_express_consent(
        request_data,
        express_consent_payload=None,
        express_consent_parse_error=None,
    )
    assert "express_consent" not in request_data
    # smart_consent 不受此函数影响
    assert request_data["smart_consent"] == {"auto_voice_clone": True}


def test_express_validated_payload_replaces_forged():
    """Express validated path：原始 forged express_consent 被替换为
    validated payload（含后端 server_confirmed_at）。"""
    from job_intercept import _apply_validated_express_consent
    request_data = {
        "service_mode": "express",
        # 客户端原始传的（已被 validator 读过；这里应被 validated 值覆盖）
        "express_consent": {
            "auto_voice_clone": True,
            "client_confirmed_at": "2026-05-28T03:00:00Z",
            "server_confirmed_at": "2020-01-01T00:00:00Z",  # 伪造，必须被覆盖
        },
    }
    validated = {
        "auto_voice_clone": True,
        "client_confirmed_at": "2026-05-28T03:00:00Z",
        "server_confirmed_at": "2026-05-28T03:00:01.234567+00:00",  # 后端生成
    }
    _apply_validated_express_consent(
        request_data,
        express_consent_payload=validated,
        express_consent_parse_error=None,
    )
    assert request_data["express_consent"] == validated
    # 伪造的 server_confirmed_at 被后端值覆盖
    assert request_data["express_consent"]["server_confirmed_at"] == (
        "2026-05-28T03:00:01.234567+00:00"
    )


def test_express_parse_error_written_back():
    """Express path consent 解析失败 → parse_error 写回（forged 先清）。"""
    from job_intercept import _apply_validated_express_consent
    request_data = {"express_consent": {"forged": True}}
    _apply_validated_express_consent(
        request_data,
        express_consent_payload=None,  # 解析失败时 payload=None
        express_consent_parse_error="auto_voice_clone_not_bool",
    )
    assert "express_consent" not in request_data
    assert request_data["express_consent_parse_error"] == "auto_voice_clone_not_bool"


def test_no_express_consent_in_clean_request_stays_absent():
    """干净 request（无 express_consent）→ pop 无副作用，仍无该字段。"""
    from job_intercept import _apply_validated_express_consent
    request_data = {"service_mode": "studio"}
    _apply_validated_express_consent(
        request_data,
        express_consent_payload=None,
        express_consent_parse_error=None,
    )
    assert "express_consent" not in request_data
    assert "express_consent_parse_error" not in request_data


# ===========================================================================
# P2-2: register-smart is_temporary=true 强制合法 expiry
# ===========================================================================


_TEST_KEY = "phase43a-rf2-internal-key"


def _make_request(body: dict):
    raw = json.dumps(body).encode("utf-8")

    async def _body():
        return raw

    return SimpleNamespace(
        headers={"X-Internal-Key": _TEST_KEY},
        client=SimpleNamespace(host="127.0.0.1"),
        body=_body,
    )


def _call_register_smart(body: dict, monkeypatch, *, add_user_voice_mock=None):
    import config
    monkeypatch.setattr(config.settings, "internal_api_key", _TEST_KEY, raising=False)
    import user_voice_api
    if add_user_voice_mock is not None:
        monkeypatch.setattr(user_voice_api, "add_user_voice", add_user_voice_mock, raising=True)
    request = _make_request(body)
    db = AsyncMock()
    resp = _run(user_voice_api.internal_register_smart_clone(request, db=db))
    return resp.status_code, json.loads(resp.body)


def _temp_cosyvoice_body(**overrides) -> dict:
    base = {
        "user_id": "00000000-0000-0000-0000-0000000000a2",
        "voice_id": "cosyvoice-v3.5-flash-temp",
        "label": "Temp",
        "provider": "cosyvoice_voice_clone",
        "tts_provider": "cosyvoice",
        "platform": "dashscope_mainland",
        "created_from": "express_auto",
        "requires_worker": True,
        "target_model": "cosyvoice-v3.5-flash",
        "is_temporary": True,
        "temporary_expires_at": "2026-06-04T12:00:00Z",
    }
    base.update(overrides)
    return base


def test_temporary_missing_expiry_rejected(monkeypatch):
    """is_temporary=true + 无 temporary_expires_at → 400。"""
    body = _temp_cosyvoice_body()
    body.pop("temporary_expires_at")
    status, parsed = _call_register_smart(body, monkeypatch)
    assert status == 400
    assert parsed["error"] == "temporary_expires_at_required_for_temporary_voice"


def test_temporary_null_expiry_rejected(monkeypatch):
    """is_temporary=true + temporary_expires_at=null → 400。"""
    body = _temp_cosyvoice_body(temporary_expires_at=None)
    status, parsed = _call_register_smart(body, monkeypatch)
    assert status == 400
    assert parsed["error"] == "temporary_expires_at_required_for_temporary_voice"


def test_temporary_malformed_expiry_rejected(monkeypatch):
    """is_temporary=true + 格式坏的 temporary_expires_at → 400。"""
    body = _temp_cosyvoice_body(temporary_expires_at="not-a-date")
    status, parsed = _call_register_smart(body, monkeypatch)
    assert status == 400
    assert parsed["error"] == "temporary_expires_at_required_for_temporary_voice"


def test_temporary_empty_string_expiry_rejected(monkeypatch):
    body = _temp_cosyvoice_body(temporary_expires_at="   ")
    status, parsed = _call_register_smart(body, monkeypatch)
    assert status == 400
    assert parsed["error"] == "temporary_expires_at_required_for_temporary_voice"


def test_temporary_valid_expiry_succeeds(monkeypatch):
    """is_temporary=true + 合法 expiry → 200。"""
    fake = SimpleNamespace(voice_id="cosyvoice-v3.5-flash-temp", user_id="x")
    mock = AsyncMock(return_value=fake)
    body = _temp_cosyvoice_body()
    status, parsed = _call_register_smart(body, monkeypatch, add_user_voice_mock=mock)
    assert status == 200
    _, kwargs = mock.call_args
    assert kwargs["is_temporary"] is True
    assert kwargs["temporary_expires_at"] is not None


def test_non_temporary_with_expiry_passes_and_cleared_downstream(monkeypatch):
    """is_temporary=false 即使传 expiry → 不 400（endpoint 不拦）；
    add_user_voice 内部强制清 None（E §6.3.1 已测，这里验 endpoint 不拦）。"""
    fake = SimpleNamespace(voice_id="cosyvoice-v3.5-flash-longterm", user_id="x")
    mock = AsyncMock(return_value=fake)
    body = _temp_cosyvoice_body(
        voice_id="cosyvoice-v3.5-flash-longterm",
        is_temporary=False,
        created_from="studio_manual",
        temporary_expires_at="2026-06-04T12:00:00Z",  # 传了但应被下游清
    )
    status, parsed = _call_register_smart(body, monkeypatch, add_user_voice_mock=mock)
    assert status == 200, f"非临时音色不应被 expiry 校验拦住: {parsed}"
    _, kwargs = mock.call_args
    assert kwargs["is_temporary"] is False
    # endpoint 把解析后的 ts 传给 add_user_voice；add_user_voice 内部
    # （is_temporary=False 时）强制清 None —— 该清空行为由 E 阶段
    # test_add_user_voice_non_temp_forces_expires_at_none_even_if_caller_passes_ts
    # 覆盖。这里只验 endpoint 层不 400。


def test_minimax_legacy_no_expiry_still_passes(monkeypatch):
    """Smart MiniMax 旧 caller（is_temporary=false 默认 + 无 expiry）→ 200。"""
    fake = SimpleNamespace(voice_id="vt_mm", user_id="x")
    mock = AsyncMock(return_value=fake)
    body = {
        "user_id": "00000000-0000-0000-0000-0000000000bb",
        "voice_id": "vt_mm",
        "provider": "minimax_voice_clone",
        # 不传 is_temporary（默认 False）/ temporary_expires_at
    }
    status, _ = _call_register_smart(body, monkeypatch, add_user_voice_mock=mock)
    assert status == 200
