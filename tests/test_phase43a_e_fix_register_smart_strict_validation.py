"""Phase 4.3a PR1-E-fix — register-smart 严格 bool + target_model 必填校验。

Codex E review P1：register-smart endpoint 对 worker routing 字段太宽松：

- ``bool(body.get("requires_worker") or False)`` 把 ``"false"`` / ``"0"``
  字符串当 truthy → 意外 True → TTS 静默错路由
- ``requires_worker=True`` 但 ``target_model`` 空时 row 注册成功，但
  ``lookup_clone_voice_routing_metadata`` 的 ``target_model != ""`` 条件
  查不到它 → segment TTS 回退官方音色

E-fix：
1. requires_worker / is_temporary 必须严格 bool（非 bool → 400）
2. provider==cosyvoice_voice_clone 或 requires_worker=True 时 target_model
   必须非空 string，否则 400
3. Smart MiniMax 旧 caller 不传这些字段仍默认 200

测试两层：
- ``_strict_optional_bool`` 纯函数行为
- register-smart endpoint 真实 handler 行为（构造合规 fake Request，
  monkeypatch internal key + add_user_voice，断言 400 / 200）
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

_REPO = Path(__file__).resolve().parents[1]
_GATEWAY = _REPO / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ===========================================================================
# Layer 1: _strict_optional_bool 纯函数
# ===========================================================================


def test_strict_optional_bool_missing_returns_false():
    from user_voice_api import _strict_optional_bool
    assert _strict_optional_bool({}, "requires_worker") == (False, None)


def test_strict_optional_bool_none_returns_false():
    from user_voice_api import _strict_optional_bool
    assert _strict_optional_bool({"requires_worker": None}, "requires_worker") == (False, None)


def test_strict_optional_bool_true_false_accepted():
    from user_voice_api import _strict_optional_bool
    assert _strict_optional_bool({"x": True}, "x") == (True, None)
    assert _strict_optional_bool({"x": False}, "x") == (False, None)


def test_strict_optional_bool_string_false_rejected():
    """关键：``"false"`` 字符串必须被拒（不能 bool("false")==True）。"""
    from user_voice_api import _strict_optional_bool
    value, err = _strict_optional_bool({"requires_worker": "false"}, "requires_worker")
    assert value is None
    assert err == "requires_worker_must_be_bool"


def test_strict_optional_bool_string_zero_and_int_rejected():
    from user_voice_api import _strict_optional_bool
    for bad in ["0", "true", "1", 0, 1, 1.0, []]:
        value, err = _strict_optional_bool({"is_temporary": bad}, "is_temporary")
        assert value is None, f"{bad!r} 不应被接受为 bool"
        assert err == "is_temporary_must_be_bool"


# ===========================================================================
# Layer 2: register-smart endpoint 真实 handler 行为
# ===========================================================================


_TEST_INTERNAL_KEY = "phase43a-test-internal-key"


def _make_request(body: dict):
    """构造一个能通过 _internal_access_error + _read_body 的 fake Request。"""
    raw = json.dumps(body).encode("utf-8")

    async def _body():
        return raw

    return SimpleNamespace(
        headers={"X-Internal-Key": _TEST_INTERNAL_KEY},
        client=SimpleNamespace(host="127.0.0.1"),
        body=_body,
    )


def _call_register_smart(body: dict, monkeypatch, *, add_user_voice_mock=None):
    """调真 internal_register_smart_clone handler，返回 (status_code, json_body)。

    - monkeypatch config.settings.internal_api_key 让 internal auth 通过
    - monkeypatch add_user_voice（仅 200 path 需要；400 path 不会触及）
    """
    import config
    monkeypatch.setattr(config.settings, "internal_api_key", _TEST_INTERNAL_KEY, raising=False)

    import user_voice_api
    if add_user_voice_mock is not None:
        monkeypatch.setattr(user_voice_api, "add_user_voice", add_user_voice_mock, raising=True)

    request = _make_request(body)
    db = AsyncMock()
    resp = _run(user_voice_api.internal_register_smart_clone(request, db=db))
    status = resp.status_code
    parsed = json.loads(resp.body)
    return status, parsed


# 合法 cosyvoice clone 的最小 body（带 express_auto created_from + target_model）
def _valid_cosyvoice_body(**overrides) -> dict:
    base = {
        "user_id": "00000000-0000-0000-0000-0000000000aa",
        "voice_id": "cosyvoice-v3.5-flash-test",
        "label": "Test",
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


def test_register_smart_rejects_string_false_requires_worker(monkeypatch):
    """requires_worker="false" → 400 requires_worker_must_be_bool。"""
    body = _valid_cosyvoice_body(requires_worker="false")
    status, parsed = _call_register_smart(body, monkeypatch)
    assert status == 400
    assert parsed["error"] == "requires_worker_must_be_bool"


def test_register_smart_rejects_string_true_is_temporary(monkeypatch):
    """is_temporary="true" → 400 is_temporary_must_be_bool。"""
    body = _valid_cosyvoice_body(is_temporary="true")
    status, parsed = _call_register_smart(body, monkeypatch)
    assert status == 400
    assert parsed["error"] == "is_temporary_must_be_bool"


def test_register_smart_rejects_requires_worker_true_missing_target_model(monkeypatch):
    """requires_worker=True + missing target_model → 400。"""
    body = _valid_cosyvoice_body()
    body.pop("target_model")
    status, parsed = _call_register_smart(body, monkeypatch)
    assert status == 400
    assert parsed["error"] == "target_model_required_for_worker_clone"


def test_register_smart_rejects_requires_worker_true_empty_target_model(monkeypatch):
    """requires_worker=True + empty target_model → 400。"""
    body = _valid_cosyvoice_body(target_model="   ")
    status, parsed = _call_register_smart(body, monkeypatch)
    assert status == 400
    assert parsed["error"] == "target_model_required_for_worker_clone"


def test_register_smart_cosyvoice_requires_worker_false_rejected(monkeypatch):
    """P2-1（Codex GitHub PR #17 review）：provider=cosyvoice_voice_clone +
    requires_worker=False → 400 cosyvoice_clone_requires_worker_true。

    这是 P2-1 核心修复：之前 E-fix 只校验 target_model，允许
    {provider: cosyvoice_voice_clone, requires_worker: false, target_model: ...}
    写入，但 lookup_clone_voice_routing_metadata (requires_worker IS TRUE)
    查不到 → TTS 回退官方音色（线上症状）。
    """
    body = _valid_cosyvoice_body(requires_worker=False)
    status, parsed = _call_register_smart(body, monkeypatch)
    assert status == 400
    assert parsed["error"] == "cosyvoice_clone_requires_worker_true"


def test_register_smart_cosyvoice_wrong_tts_provider_rejected(monkeypatch):
    """P2-1：cosyvoice provider + tts_provider != cosyvoice → 400。"""
    body = _valid_cosyvoice_body(tts_provider="minimax_tts")
    status, parsed = _call_register_smart(body, monkeypatch)
    assert status == 400
    assert parsed["error"] == "cosyvoice_clone_tts_provider_mismatch"


def test_register_smart_cosyvoice_default_tts_provider_rejected(monkeypatch):
    """P2-1：cosyvoice provider 但不传 tts_provider（默认 minimax_tts）→ 400。"""
    body = _valid_cosyvoice_body()
    body.pop("tts_provider")  # 默认 minimax_tts → 与 cosyvoice provider 不自洽
    status, parsed = _call_register_smart(body, monkeypatch)
    assert status == 400
    assert parsed["error"] == "cosyvoice_clone_tts_provider_mismatch"


def test_register_smart_cosyvoice_wrong_platform_rejected(monkeypatch):
    """P2-1：cosyvoice provider + platform != dashscope_mainland → 400。"""
    body = _valid_cosyvoice_body(platform="minimax_domestic")
    status, parsed = _call_register_smart(body, monkeypatch)
    assert status == 400
    assert parsed["error"] == "cosyvoice_clone_platform_mismatch"


def test_register_smart_cosyvoice_missing_target_model_rejected(monkeypatch):
    """P2-1：cosyvoice provider（其它字段自洽）+ 无 target_model → 400。"""
    body = _valid_cosyvoice_body()
    body.pop("target_model")
    status, parsed = _call_register_smart(body, monkeypatch)
    assert status == 400
    assert parsed["error"] == "target_model_required_for_worker_clone"


def test_register_smart_valid_cosyvoice_clone_succeeds(monkeypatch):
    """合法 cosyvoice clone body → 200。"""
    fake_voice = SimpleNamespace(
        voice_id="cosyvoice-v3.5-flash-test",
        user_id="00000000-0000-0000-0000-0000000000aa",
    )
    mock = AsyncMock(return_value=fake_voice)
    body = _valid_cosyvoice_body()
    status, parsed = _call_register_smart(body, monkeypatch, add_user_voice_mock=mock)
    assert status == 200
    assert parsed["ok"] is True
    # 验证 add_user_voice 被调时 requires_worker/is_temporary 是真 bool
    _, kwargs = mock.call_args
    assert kwargs["requires_worker"] is True
    assert kwargs["is_temporary"] is True
    assert kwargs["target_model"] == "cosyvoice-v3.5-flash"


def test_register_smart_minimax_legacy_caller_unchanged(monkeypatch):
    """Smart MiniMax 旧 caller（不传 routing / temporary 字段）→ 200 +
    add_user_voice 收到默认值（requires_worker=False, is_temporary=False,
    target_model=None）。backward-compat 字节级。
    """
    fake_voice = SimpleNamespace(
        voice_id="vt_minimax_legacy",
        user_id="00000000-0000-0000-0000-0000000000bb",
    )
    mock = AsyncMock(return_value=fake_voice)
    # 旧 caller 的 minimal body：只 user_id + voice_id（provider 默认 minimax）
    body = {
        "user_id": "00000000-0000-0000-0000-0000000000bb",
        "voice_id": "vt_minimax_legacy",
        "label": "Legacy",
        "source_speaker_id": "speaker_a",
        # 不传 provider（默认 minimax_voice_clone）
        # 不传 created_from（默认 smart_auto）
        # 不传 requires_worker / is_temporary / target_model
    }
    status, parsed = _call_register_smart(body, monkeypatch, add_user_voice_mock=mock)
    assert status == 200, f"Smart MiniMax 旧 caller 应 200，实际 {status}: {parsed}"
    _, kwargs = mock.call_args
    assert kwargs["provider"] == "minimax_voice_clone"
    assert kwargs["created_from"] == "smart_auto"
    assert kwargs["requires_worker"] is False
    assert kwargs["is_temporary"] is False
    assert kwargs["target_model"] is None


def test_register_smart_minimax_caller_not_blocked_by_target_model_check(monkeypatch):
    """守卫：minimax 旧 caller（requires_worker=False + 无 target_model）
    **不**被 target_model 必填校验拦住（target_model 只在 cosyvoice provider
    或 requires_worker=True 时必填）。
    """
    fake_voice = SimpleNamespace(voice_id="vt_mm", user_id="x")
    mock = AsyncMock(return_value=fake_voice)
    body = {
        "user_id": "00000000-0000-0000-0000-0000000000cc",
        "voice_id": "vt_mm",
        "provider": "minimax_voice_clone",
        # requires_worker 缺省 → False；target_model 缺省 → 不必填
    }
    status, _ = _call_register_smart(body, monkeypatch, add_user_voice_mock=mock)
    assert status == 200
