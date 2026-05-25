"""Gateway 接入 mainland_voice_worker 的回归测试。

plan 2026-05-24 Phase 1.5。覆盖：

1. ``GatewaySettings`` 4 个新字段默认值（全空 / disabled）
2. ``validate_mainland_voice_worker_config()`` 决策矩阵：
   - disabled → False（无视 secret）
   - enabled + 缺 url/key_id/secret → CRITICAL log + 降级 False
   - enabled + 三件套齐 → True
3. **关键安全属性**：validate 的日志输出**永远不含 secret 实体**
4. ``build_mainland_voice_worker_client()``：
   - disabled → None
   - enabled 但缺 secret → None（防御性二次校验）
   - 三件套齐 → 真实 MainlandWorkerClient
5. Admin endpoint：
   - ``/api/admin/mainland-voice-worker/status`` 不返 secret 实体
   - ``/api/admin/mainland-voice-worker/healthz`` worker disabled 时返 503
   - 未登录返 401，普通用户返 403
"""
from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

import pytest


# conftest.py 已经把 src/ 和 gateway/ 加进 sys.path，但容器里 mainland_voice_worker
# 模块在 import 时自己也会再注入 src/。这里测试只 import gateway 侧 module。
from startup_checks import validate_mainland_voice_worker_config


SECRET_VALUE = "do-not-leak-this-secret-deadbeef-1234"


# ---------------------------------------------------------------------------
# Settings 默认值
# ---------------------------------------------------------------------------

def test_settings_defaults_disabled_and_empty(monkeypatch) -> None:
    """干净 env 下 4 个字段全为默认（disabled + empty）。"""
    for key in (
        "AVT_MAINLAND_VOICE_WORKER_ENABLED",
        "AVT_MAINLAND_VOICE_WORKER_URL",
        "AVT_MAINLAND_VOICE_WORKER_HMAC_KEY_ID",
        "AVT_MAINLAND_VOICE_WORKER_HMAC_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)

    from config import GatewaySettings
    s = GatewaySettings()
    assert s.mainland_voice_worker_enabled is False
    assert s.mainland_voice_worker_url == ""
    assert s.mainland_voice_worker_hmac_key_id == ""
    assert s.mainland_voice_worker_hmac_secret == ""


# ---------------------------------------------------------------------------
# validate_mainland_voice_worker_config
# ---------------------------------------------------------------------------

def test_validate_disabled_returns_false() -> None:
    assert validate_mainland_voice_worker_config(
        enabled=False,
        url="http://x",
        hmac_key_id="k",
        hmac_secret="s",
    ) is False


def test_validate_enabled_missing_url_returns_false(caplog) -> None:
    with caplog.at_level(logging.CRITICAL, logger="startup_checks"):
        result = validate_mainland_voice_worker_config(
            enabled=True, url="", hmac_key_id="k1", hmac_secret=SECRET_VALUE,
        )
    assert result is False
    assert any("AVT_MAINLAND_VOICE_WORKER_URL" in r.message for r in caplog.records)


def test_validate_enabled_missing_key_id_returns_false(caplog) -> None:
    with caplog.at_level(logging.CRITICAL, logger="startup_checks"):
        result = validate_mainland_voice_worker_config(
            enabled=True, url="http://x", hmac_key_id="", hmac_secret=SECRET_VALUE,
        )
    assert result is False
    assert any("AVT_MAINLAND_VOICE_WORKER_HMAC_KEY_ID" in r.message for r in caplog.records)


def test_validate_enabled_missing_secret_returns_false(caplog) -> None:
    with caplog.at_level(logging.CRITICAL, logger="startup_checks"):
        result = validate_mainland_voice_worker_config(
            enabled=True, url="http://x", hmac_key_id="k1", hmac_secret="",
        )
    assert result is False
    assert any("AVT_MAINLAND_VOICE_WORKER_HMAC_SECRET" in r.message for r in caplog.records)


def test_validate_enabled_full_config_returns_true(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="startup_checks"):
        result = validate_mainland_voice_worker_config(
            enabled=True,
            url="http://8.148.83.128/internal/voice-clone",
            hmac_key_id="k1",
            hmac_secret=SECRET_VALUE,
        )
    assert result is True


# ---------------------------------------------------------------------------
# 关键安全属性：secret 不进日志
# ---------------------------------------------------------------------------

def test_validate_does_not_log_secret_on_success(caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        validate_mainland_voice_worker_config(
            enabled=True,
            url="http://x",
            hmac_key_id="k1",
            hmac_secret=SECRET_VALUE,
        )
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET_VALUE not in full_log, (
        f"Secret 不应出现在 INFO log；实际：\n{full_log}"
    )


@pytest.mark.parametrize("missing_field", ["url", "key_id", "secret"])
def test_validate_does_not_log_secret_on_critical(caplog, missing_field: str) -> None:
    """secret 在 CRITICAL 路径也不应被打到日志。"""
    kwargs = {
        "enabled": True,
        "url": "http://x",
        "hmac_key_id": "k1",
        "hmac_secret": SECRET_VALUE,
    }
    if missing_field == "url":
        kwargs["url"] = ""
    elif missing_field == "key_id":
        kwargs["hmac_key_id"] = ""
    elif missing_field == "secret":
        kwargs["hmac_secret"] = ""

    with caplog.at_level(logging.DEBUG):
        validate_mainland_voice_worker_config(**kwargs)
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    # 当 secret 字段为空时不存在泄漏问题；当 secret 被传入但 url / key_id 缺时检查
    if missing_field != "secret":
        assert SECRET_VALUE not in full_log, full_log


# ---------------------------------------------------------------------------
# build_mainland_voice_worker_client 工厂
# ---------------------------------------------------------------------------

def _make_settings(enabled: bool, url: str = "", key_id: str = "", secret: str = ""):
    from config import GatewaySettings
    s = GatewaySettings()
    s.mainland_voice_worker_enabled = enabled
    s.mainland_voice_worker_url = url
    s.mainland_voice_worker_hmac_key_id = key_id
    s.mainland_voice_worker_hmac_secret = secret
    return s


def test_factory_returns_none_when_disabled() -> None:
    from mainland_voice_worker import build_mainland_voice_worker_client

    s = _make_settings(enabled=False, url="http://x", key_id="k", secret=SECRET_VALUE)
    assert build_mainland_voice_worker_client(s) is None


@pytest.mark.parametrize("missing", ["url", "key_id", "secret"])
def test_factory_returns_none_when_enabled_but_field_missing(missing: str) -> None:
    from mainland_voice_worker import build_mainland_voice_worker_client

    kwargs = {"enabled": True, "url": "http://x", "key_id": "k", "secret": SECRET_VALUE}
    if missing == "url":
        kwargs["url"] = ""
    elif missing == "key_id":
        kwargs["key_id"] = ""
    elif missing == "secret":
        kwargs["secret"] = ""

    s = _make_settings(**kwargs)
    assert build_mainland_voice_worker_client(s) is None


def test_factory_builds_real_client_when_configured() -> None:
    from mainland_voice_worker import build_mainland_voice_worker_client
    from services.mainland_worker.client import MainlandWorkerClient

    s = _make_settings(
        enabled=True,
        url="http://8.148.83.128/internal/voice-clone",
        key_id="k1",
        secret=SECRET_VALUE,
    )
    client = build_mainland_voice_worker_client(s)
    try:
        assert isinstance(client, MainlandWorkerClient)
    finally:
        if client is not None:
            client.close()


# ---------------------------------------------------------------------------
# Admin endpoint: status
# ---------------------------------------------------------------------------

def test_admin_status_response_never_contains_secret(monkeypatch) -> None:
    """响应 JSON 必须不含 secret 实体，只含 has_hmac_secret bool。"""
    import json
    import config as gw_config
    from mainland_voice_worker import get_status

    # 直接 unit-call handler（不起完整 app），mock User
    monkeypatch.setattr(gw_config.settings, "mainland_voice_worker_enabled", True)
    monkeypatch.setattr(gw_config.settings, "mainland_voice_worker_url", "http://wuhan")
    monkeypatch.setattr(gw_config.settings, "mainland_voice_worker_hmac_key_id", "k1")
    monkeypatch.setattr(gw_config.settings, "mainland_voice_worker_hmac_secret", SECRET_VALUE)

    class FakeAdminUser:
        role = "admin"
        id = 1

    import asyncio
    resp = asyncio.run(get_status(user=FakeAdminUser()))
    body = json.dumps(resp)
    assert SECRET_VALUE not in body, f"secret 出现在 admin status response: {body}"
    assert resp["has_hmac_secret"] is True
    assert resp["url"] == "http://wuhan"
    assert resp["hmac_key_id"] == "k1"
    assert resp["effective_enabled"] is True


def test_admin_status_has_hmac_secret_false_when_unset(monkeypatch) -> None:
    import asyncio
    import config as gw_config
    from mainland_voice_worker import get_status

    monkeypatch.setattr(gw_config.settings, "mainland_voice_worker_enabled", False)
    monkeypatch.setattr(gw_config.settings, "mainland_voice_worker_url", "")
    monkeypatch.setattr(gw_config.settings, "mainland_voice_worker_hmac_key_id", "")
    monkeypatch.setattr(gw_config.settings, "mainland_voice_worker_hmac_secret", "")

    class FakeAdminUser:
        role = "admin"
        id = 1

    resp = asyncio.run(get_status(user=FakeAdminUser()))
    assert resp["effective_enabled"] is False
    assert resp["has_hmac_secret"] is False


def test_admin_status_requires_admin() -> None:
    import asyncio
    from fastapi import HTTPException
    from mainland_voice_worker import get_status

    # 未登录
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_status(user=None))
    assert exc.value.status_code == 401

    class FakeUser:
        role = "user"
        id = 2

    # 普通用户
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_status(user=FakeUser()))
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Admin endpoint: healthz
# ---------------------------------------------------------------------------

def test_admin_healthz_returns_503_when_disabled(monkeypatch) -> None:
    import asyncio
    import config as gw_config
    from fastapi import HTTPException
    from mainland_voice_worker import get_worker_healthz

    monkeypatch.setattr(gw_config.settings, "mainland_voice_worker_enabled", False)

    class FakeAdminUser:
        role = "admin"
        id = 1

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_worker_healthz(user=FakeAdminUser()))
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "worker_disabled"


def test_admin_healthz_requires_admin() -> None:
    import asyncio
    from fastapi import HTTPException
    from mainland_voice_worker import get_worker_healthz

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_worker_healthz(user=None))
    assert exc.value.status_code == 401


def test_admin_healthz_proxies_worker_response(monkeypatch, tmp_path: Path) -> None:
    """启用 + 配齐时，真的调 worker /healthz 并把响应原样转回 admin。

    用一个 in-process mock worker（同 Phase 1 e2e 测试的 _TestClientTransport
    bridging）让 client 调到一个本地 FastAPI app。
    """
    import asyncio
    import httpx
    import config as gw_config
    from fastapi.testclient import TestClient
    from services.mainland_worker.client import (
        MainlandWorkerClient,
        WorkerCredentials,
    )
    from services.mainland_worker.hmac_auth import HmacKey
    from services.mainland_worker.worker.app import create_app
    from services.mainland_worker.worker.audit import InMemoryAuditLogger
    from services.mainland_worker.worker.config import WORKER_MODE_MOCK, WorkerConfig

    key_id = "test-k"
    secret = "test-secret-1234567890"
    cfg = WorkerConfig(
        mode=WORKER_MODE_MOCK,
        hmac_keys=(HmacKey(key_id=key_id, secret=secret),),
        audit_log_path=tmp_path / "audit.jsonl",
        artifact_dir=tmp_path / "artifacts",
    )
    worker_app = create_app(config=cfg, audit_logger=InMemoryAuditLogger())

    # Bridge: TestClient → httpx.BaseTransport
    # TestClient 在 __init__ 内部会构造自己的 httpx.Client，所以这里把它
    # 提前一次性构造好，避免在 BridgeTransport.__init__ 触发递归。
    inner_tc = TestClient(worker_app, base_url="http://wuhan.test")

    class _Bridge(httpx.BaseTransport):
        def handle_request(self, request):
            path = request.url.path
            if request.url.query:
                path = path + "?" + request.url.query.decode("ascii")
            r = inner_tc.request(
                request.method, path,
                headers={k: v for k, v in request.headers.items()},
                content=request.content,
            )
            return httpx.Response(r.status_code, headers=dict(r.headers), content=r.content)

    # 配 settings — 让 get_status / has_secret 路径仍然返 True
    monkeypatch.setattr(gw_config.settings, "mainland_voice_worker_enabled", True)
    monkeypatch.setattr(gw_config.settings, "mainland_voice_worker_url", "http://wuhan.test")
    monkeypatch.setattr(gw_config.settings, "mainland_voice_worker_hmac_key_id", key_id)
    monkeypatch.setattr(gw_config.settings, "mainland_voice_worker_hmac_secret", secret)

    # 直接 patch 工厂返回带 bridge 的 client（比 patch httpx.Client.__init__ 干净，
    # 也不会让 TestClient 内部构造递归）
    def _fake_build(_settings):
        return MainlandWorkerClient(
            base_url="http://wuhan.test",
            credentials=WorkerCredentials(key_id=key_id, secret=secret),
            transport=_Bridge(),
        )

    import mainland_voice_worker as mvw
    monkeypatch.setattr(mvw, "build_mainland_voice_worker_client", _fake_build)

    class FakeAdminUser:
        role = "admin"
        id = 1

    resp = asyncio.run(mvw.get_worker_healthz(user=FakeAdminUser()))
    assert resp["ok"] is True
    assert resp["worker"] == "aivideotrans-mainland-worker"
    assert resp["region"] == "cn-wuhan"
    assert "cosyvoice" in resp["providers"]
    assert resp["providers"]["cosyvoice"]["mode"] == "mock"


# ---------------------------------------------------------------------------
# AST 守卫：admin 模块永远不直接把 hmac_secret 返进 response
# ---------------------------------------------------------------------------

def test_mainland_voice_worker_router_never_returns_raw_secret() -> None:
    """守住一条静态规则：``mainland_voice_worker.py`` 内的 endpoint
    handler 不应直接把 ``hmac_secret`` 字段值放进返回 dict。

    AST 扫所有 return / dict 字面量；如果出现 ``"hmac_secret": settings.mainland_voice_worker_hmac_secret``
    这种形态，立刻红。允许 ``"has_hmac_secret": bool(...)`` 这种形态。
    """
    import ast

    path = Path(__file__).resolve().parents[1] / "gateway" / "mainland_voice_worker.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key_node, val_node in zip(node.keys, node.values):
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                continue
            key_name = key_node.value
            # 允许 "has_hmac_secret"（bool）
            if key_name == "has_hmac_secret":
                continue
            # 禁止 "hmac_secret" / "secret" 等明显字段名
            if "hmac_secret" in key_name.lower() or key_name.lower() == "secret":
                offenders.append(
                    f"line {node.lineno}: dict has key {key_name!r}"
                )
    assert not offenders, (
        f"mainland_voice_worker.py 出现了直接返回 secret 的字段：\n  - "
        + "\n  - ".join(offenders)
    )
