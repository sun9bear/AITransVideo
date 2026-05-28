"""Phase 4.3a PR2-C — reservation endpoints（reserve/consume/release）行为测试。

薄封装 express_reservation_service 的 3 个 internal endpoints。测试覆盖：
- X-Internal-Key 鉴权（403）
- 输入校验（user_id / job_id / speaker_id / target_model → 400）
- admin_settings 读 cap/ttl；unavailable → fail-closed 503
- service outcome → HTTP 映射（reserved 200 / denied 409 / user_not_found 404 /
  consume-release conflict 409，不吞成 200）

策略：直接调 async handler + fake Request + monkeypatch
express_reservation_service（验 endpoint 层映射 + admin 读取，不重测 service
DB 逻辑——那在 PR2-B）。并发原子性留 PR2-C-pg。
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

import express_reservation_service as svc  # noqa: E402


_TEST_KEY = "phase43a-pr2c-key"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _request(body: dict, *, internal_key: str = _TEST_KEY):
    raw = json.dumps(body).encode("utf-8")

    async def _body():
        return raw

    return SimpleNamespace(
        headers={"X-Internal-Key": internal_key},
        client=SimpleNamespace(host="127.0.0.1"),
        body=_body,
    )


def _setup(monkeypatch, *, caps=(5, 3, 30), caps_unavailable=False):
    import config
    monkeypatch.setattr(config.settings, "internal_api_key", _TEST_KEY, raising=False)
    import user_voice_api
    if caps_unavailable:
        monkeypatch.setattr(user_voice_api, "_load_express_reservation_caps",
                            lambda: None, raising=True)
    else:
        monkeypatch.setattr(user_voice_api, "_load_express_reservation_caps",
                            lambda: caps, raising=True)
    return user_voice_api


_VALID_BODY = {
    "user_id": "00000000-0000-0000-0000-0000000000c3",
    "job_id": "job_c3",
    "speaker_id": "speaker_a",
    "target_model": "cosyvoice-v3.5-flash",
}


# ---------------------------------------------------------------------------
# reserve endpoint
# ---------------------------------------------------------------------------


def _call_reserve(mod, body, monkeypatch, *, outcome=None, internal_key=_TEST_KEY):
    if outcome is not None:
        monkeypatch.setattr(svc, "reserve", AsyncMock(return_value=outcome), raising=True)
        import user_voice_api
        monkeypatch.setattr(user_voice_api, "_reservation_svc", svc, raising=False)
    req = _request(body, internal_key=internal_key)
    resp = _run(mod.internal_express_reservation_reserve(req, db=AsyncMock()))
    return resp.status_code, json.loads(resp.body)


def test_reserve_403_wrong_key(monkeypatch):
    mod = _setup(monkeypatch)
    status, _ = _call_reserve(mod, _VALID_BODY, monkeypatch, internal_key="WRONG")
    assert status == 403


def test_reserve_400_invalid_user_id(monkeypatch):
    mod = _setup(monkeypatch)
    status, parsed = _call_reserve(mod, {**_VALID_BODY, "user_id": "nope"}, monkeypatch)
    assert status == 400 and parsed["error"] == "invalid_user_id"


def test_reserve_400_invalid_job_id(monkeypatch):
    mod = _setup(monkeypatch)
    status, parsed = _call_reserve(mod, {**_VALID_BODY, "job_id": "Bad Job!"}, monkeypatch)
    assert status == 400 and parsed["error"] == "invalid_job_id"


def test_reserve_400_invalid_speaker_id(monkeypatch):
    mod = _setup(monkeypatch)
    status, parsed = _call_reserve(mod, {**_VALID_BODY, "speaker_id": "SPK"}, monkeypatch)
    assert status == 400 and parsed["error"] == "invalid_speaker_id"


def test_reserve_400_invalid_target_model(monkeypatch):
    mod = _setup(monkeypatch)
    status, parsed = _call_reserve(mod, {**_VALID_BODY, "target_model": "minimax"}, monkeypatch)
    assert status == 400 and parsed["error"] == "invalid_target_model"


# --- PR2-C-fix（Codex P1）：regex 放宽接受真实 job/speaker ---


def _reserve_reaches_service(mod, body, monkeypatch):
    """跑 reserve，返回 (status, parsed, service_called)。service.reserve mock
    成功；若 endpoint 因校验失败提前返回，service 不被调。"""
    reserve_mock = AsyncMock(return_value=svc.ReserveOutcome(status="reserved", reservation_id="r"))
    monkeypatch.setattr(svc, "reserve", reserve_mock, raising=True)
    req = _request(body)
    resp = _run(mod.internal_express_reservation_reserve(req, db=AsyncMock()))
    return resp.status_code, json.loads(resp.body), reserve_mock.await_count > 0


def test_reserve_accepts_uppercase_youtube_like_job_id(monkeypatch):
    """YouTube-id-like job_id（含大写）应被接受，不再 400。"""
    mod = _setup(monkeypatch)
    status, _, called = _reserve_reaches_service(
        mod, {**_VALID_BODY, "job_id": "orQKfIXMiA8"}, monkeypatch)
    assert status == 200 and called, "含大写的 job_id 不应被 400"


def test_reserve_accepts_uuid_like_job_id(monkeypatch):
    mod = _setup(monkeypatch)
    status, _, called = _reserve_reaches_service(
        mod, {**_VALID_BODY, "job_id": "job_a1b2-c3d4-e5f6"}, monkeypatch)
    assert status == 200 and called, "UUID-like（含连字符）job_id 不应被 400"


def test_reserve_accepts_speaker_10(monkeypatch):
    """speaker_10（数字）应被接受（pipeline 真源 ^speaker_[a-z0-9_]+$）。"""
    mod = _setup(monkeypatch)
    status, _, called = _reserve_reaches_service(
        mod, {**_VALID_BODY, "speaker_id": "speaker_10"}, monkeypatch)
    assert status == 200 and called, "speaker_10 不应被 400"


def test_reserve_accepts_speaker_a_1(monkeypatch):
    mod = _setup(monkeypatch)
    status, _, called = _reserve_reaches_service(
        mod, {**_VALID_BODY, "speaker_id": "speaker_a_1"}, monkeypatch)
    assert status == 200 and called, "speaker_a_1 不应被 400"


def test_reserve_still_rejects_illegal_job_id(monkeypatch):
    """非法 job_id（空格 / 感叹号 / path traversal）仍 400。"""
    mod = _setup(monkeypatch)
    for bad in ["bad job", "job!", "../etc/passwd", "job/../x", "a" * 65]:
        status, parsed, called = _reserve_reaches_service(
            mod, {**_VALID_BODY, "job_id": bad}, monkeypatch)
        assert status == 400 and not called, f"非法 job_id {bad!r} 应 400 不调 service"


def test_reserve_still_rejects_illegal_speaker_id(monkeypatch):
    """非法 speaker_id（非 speaker_ 前缀 / 大写 / 超长）仍 400。"""
    mod = _setup(monkeypatch)
    for bad in ["SPEAKER_A", "notspeaker_a", "speaker_A", "speaker_" + "a" * 57]:
        status, parsed, called = _reserve_reaches_service(
            mod, {**_VALID_BODY, "speaker_id": bad}, monkeypatch)
        assert status == 400 and not called, f"非法 speaker_id {bad!r} 应 400 不调 service"


def test_reserve_503_admin_settings_unavailable(monkeypatch):
    """fail-closed：admin_settings 读不到 cap → 503，不进 service.reserve。"""
    mod = _setup(monkeypatch, caps_unavailable=True)
    # service.reserve 不应被调
    reserve_mock = AsyncMock()
    monkeypatch.setattr(svc, "reserve", reserve_mock, raising=True)
    status, parsed = _call_reserve(mod, _VALID_BODY, monkeypatch)
    assert status == 503 and parsed["error"] == "admin_settings_unavailable"
    assert reserve_mock.await_count == 0, "admin unavailable 时不应调 service.reserve"


def test_reserve_200_reserved(monkeypatch):
    from datetime import datetime, timezone
    mod = _setup(monkeypatch)
    out = svc.ReserveOutcome(
        status="reserved", reservation_id="res-1",
        expires_at=datetime(2026, 6, 4, tzinfo=timezone.utc), idempotent_hit=False,
    )
    status, parsed = _call_reserve(mod, _VALID_BODY, monkeypatch, outcome=out)
    assert status == 200
    assert parsed["ok"] is True and parsed["reservation_id"] == "res-1"
    assert parsed["status"] == "reserved"
    assert parsed["idempotent_hit"] is False


def test_reserve_404_user_not_found(monkeypatch):
    mod = _setup(monkeypatch)
    out = svc.ReserveOutcome(status="user_not_found")
    status, parsed = _call_reserve(mod, _VALID_BODY, monkeypatch, outcome=out)
    assert status == 404 and parsed["error"] == "user_not_found"


def test_reserve_409_daily_cap(monkeypatch):
    mod = _setup(monkeypatch)
    out = svc.ReserveOutcome(status="denied", deny_reason="daily_cap_exceeded")
    status, parsed = _call_reserve(mod, _VALID_BODY, monkeypatch, outcome=out)
    assert status == 409 and parsed["deny_reason"] == "daily_cap_exceeded"


def test_reserve_409_active_temp_cap(monkeypatch):
    mod = _setup(monkeypatch)
    out = svc.ReserveOutcome(status="denied", deny_reason="active_temp_cap_exceeded")
    status, parsed = _call_reserve(mod, _VALID_BODY, monkeypatch, outcome=out)
    assert status == 409 and parsed["deny_reason"] == "active_temp_cap_exceeded"


def test_reserve_passes_admin_caps_to_service(monkeypatch):
    """endpoint 从 admin_settings 读 cap/ttl 传给 service（caller 不传 cap）。

    PR2-C-fix：单次调用（原版调了两次只取最后 call_args，易踩 mock count 坑）。
    """
    mod = _setup(monkeypatch, caps=(7, 4, 45))
    reserve_mock = AsyncMock(return_value=svc.ReserveOutcome(status="reserved", reservation_id="r"))
    monkeypatch.setattr(svc, "reserve", reserve_mock, raising=True)
    req = _request(_VALID_BODY)
    _run(mod.internal_express_reservation_reserve(req, db=AsyncMock()))
    assert reserve_mock.await_count == 1, "endpoint 应只调一次 service.reserve"
    _, kwargs = reserve_mock.call_args
    assert kwargs["daily_cap"] == 7
    assert kwargs["active_temp_cap"] == 4
    assert kwargs["ttl_minutes"] == 45


# ---------------------------------------------------------------------------
# consume endpoint
# ---------------------------------------------------------------------------


def _call_consume(mod, reservation_id, body, monkeypatch, *, outcome=None, internal_key=_TEST_KEY):
    if outcome is not None:
        monkeypatch.setattr(svc, "consume", AsyncMock(return_value=outcome), raising=True)
    req = _request(body, internal_key=internal_key)
    resp = _run(mod.internal_express_reservation_consume(reservation_id, req, db=AsyncMock()))
    return resp.status_code, json.loads(resp.body)


def test_consume_403_wrong_key(monkeypatch):
    mod = _setup(monkeypatch)
    status, _ = _call_consume(mod, "r1", {"voice_id": "v1"}, monkeypatch, internal_key="WRONG")
    assert status == 403


def test_consume_400_voice_id_required(monkeypatch):
    mod = _setup(monkeypatch)
    status, parsed = _call_consume(mod, "r1", {}, monkeypatch)
    assert status == 400 and parsed["error"] == "voice_id_required"


def test_consume_200_ok(monkeypatch):
    mod = _setup(monkeypatch)
    out = svc.TransitionOutcome(ok=True, status="consumed")
    status, parsed = _call_consume(mod, "r1", {"voice_id": "v1"}, monkeypatch, outcome=out)
    assert status == 200 and parsed["ok"] is True and parsed["status"] == "consumed"


def test_consume_409_conflict_not_swallowed(monkeypatch):
    """conflict 不吞成 200（保留状态机语义）。"""
    mod = _setup(monkeypatch)
    out = svc.TransitionOutcome(ok=False, status="released", conflict_reason="reservation_not_reservable")
    status, parsed = _call_consume(mod, "r1", {"voice_id": "v1"}, monkeypatch, outcome=out)
    assert status == 409
    assert parsed["ok"] is False and parsed["conflict_reason"] == "reservation_not_reservable"


# ---------------------------------------------------------------------------
# release endpoint
# ---------------------------------------------------------------------------


def _call_release(mod, reservation_id, body, monkeypatch, *, outcome=None, internal_key=_TEST_KEY):
    if outcome is not None:
        monkeypatch.setattr(svc, "release", AsyncMock(return_value=outcome), raising=True)
    req = _request(body, internal_key=internal_key)
    resp = _run(mod.internal_express_reservation_release(reservation_id, req, db=AsyncMock()))
    return resp.status_code, json.loads(resp.body)


def test_release_403_wrong_key(monkeypatch):
    mod = _setup(monkeypatch)
    status, _ = _call_release(mod, "r1", {"reason": "x"}, monkeypatch, internal_key="WRONG")
    assert status == 403


def test_release_200_ok(monkeypatch):
    mod = _setup(monkeypatch)
    out = svc.TransitionOutcome(ok=True, status="released")
    status, parsed = _call_release(mod, "r1", {"reason": "worker_failed"}, monkeypatch, outcome=out)
    assert status == 200 and parsed["status"] == "released"


def test_release_409_already_consumed_not_swallowed(monkeypatch):
    mod = _setup(monkeypatch)
    out = svc.TransitionOutcome(ok=False, status="consumed", conflict_reason="reservation_already_consumed")
    status, parsed = _call_release(mod, "r1", {"reason": "x"}, monkeypatch, outcome=out)
    assert status == 409 and parsed["conflict_reason"] == "reservation_already_consumed"


def test_release_idempotent_returns_200(monkeypatch):
    """service release 幂等（已 released）→ ok=True → endpoint 200。"""
    mod = _setup(monkeypatch)
    out = svc.TransitionOutcome(ok=True, status="released")
    status, parsed = _call_release(mod, "r1", {}, monkeypatch, outcome=out)
    assert status == 200 and parsed["ok"] is True


# ---------------------------------------------------------------------------
# endpoint 注册 sanity
# ---------------------------------------------------------------------------


def test_reservation_endpoints_registered_on_internal_router():
    import user_voice_api
    paths = {r.path for r in user_voice_api.internal_router.routes}
    assert "/api/internal/express-auto-clone-reservations/reserve" in paths
    assert "/api/internal/express-auto-clone-reservations/{reservation_id}/consume" in paths
    assert "/api/internal/express-auto-clone-reservations/{reservation_id}/release" in paths
