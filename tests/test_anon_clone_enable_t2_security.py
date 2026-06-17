"""P2 安全硬化测试（CodeX P2 外审修复）.

plan 2026-06-14 §3.4。三处硬化：
1. strict-bool：pipeline 侧 _admin 读 raw JSON，master 开关用 ``is True``——
   malformed 字符串 "false"/"true"/数字一律 fail-closed skip（不被 bool() 误开）。
2. job_intercept 公共 create 路径无条件 strip 客户端夹带的 ``anonymous_preview``
   （server-only 信任标记，防登录用户提权到匿名克隆分支）。
3. reserve endpoint 对 ``is_anonymous=True`` 强校验 sentinel owner（纵深防御：
   即使 anonymous_preview 被夹带，非 sentinel reserve 一律 403）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO / "src"), str(_REPO / "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import services.express.pipeline_clients as pc  # noqa: E402


_CONSENT_OK = {"auto_voice_clone": True, "server_confirmed_at": "2026-06-14T00:00:00+00:00"}


def _call(monkeypatch, *, admin_values, anonymous_preview):
    monkeypatch.setattr(pc, "_admin", lambda key, default=None: admin_values.get(key, default))
    import services.mainland_worker.client_factory as cf
    monkeypatch.setattr(cf, "is_worker_enabled_in_env", lambda: True)
    reached = {"v": False}

    def _fake_build(**kwargs):
        reached["v"] = True
        return object()

    monkeypatch.setattr(pc, "build_express_auto_clone_clients", _fake_build)
    monkeypatch.setattr(
        pc, "run_express_auto_clone",
        lambda **k: pc.ExpressAutoCloneOutcome(cloned=True, decision="cloned", reason_code="cloned"),
    )
    out = pc.maybe_run_express_auto_clone(
        user_id="11111111-1111-1111-1111-111111111111",
        job_id="job_abc", project_dir="/tmp", source_audio_path="/tmp/a.wav",
        transcript_lines=[], speaker_voices={}, speaker_routing={},
        express_consent=_CONSENT_OK, anonymous_preview=anonymous_preview,
    )
    return out, reached["v"]


def test_anon_master_switch_strict_bool_rejects_truthy_string(monkeypatch):
    """🔥 匿名主开关 = 字符串 "true"/"false"/"1" 一律不放行（strict is True）。"""
    for bad in ("true", "false", "1", "on", 1):
        out, reached = _call(
            monkeypatch,
            admin_values={"anonymous_express_cosyvoice_clone_enabled": bad},
            anonymous_preview=True,
        )
        assert out is None, f"{bad!r} 不应放行匿名克隆"
        assert reached is False


def test_express_master_switch_strict_bool_rejects_truthy_string(monkeypatch):
    """登录态 express 主开关同样 strict（同一行受益）。"""
    for bad in ("true", "1", 1):
        out, reached = _call(
            monkeypatch,
            admin_values={"express_cosyvoice_auto_clone_enabled": bad},
            anonymous_preview=False,
        )
        assert out is None
        assert reached is False


def test_anon_master_switch_true_bool_passes(monkeypatch):
    """真 Python True 仍正常放行（不误杀正常路径）。"""
    out, reached = _call(
        monkeypatch,
        admin_values={"anonymous_express_cosyvoice_clone_enabled": True},
        anonymous_preview=True,
    )
    assert out is not None
    assert reached is True


def test_job_intercept_strips_client_anonymous_preview():
    """source-pin：公共 create 路径 intercept_create_job 无条件 pop 客户端
    夹带的 anonymous_preview（防提权）。"""
    src = (_REPO / "gateway" / "job_intercept.py").read_text(encoding="utf-8")
    assert 'request_data.pop("anonymous_preview"' in src, (
        "intercept_create_job 必须 strip 客户端 anonymous_preview"
    )


def test_reserve_endpoint_enforces_sentinel_owner_for_anonymous():
    """source-pin：reserve endpoint 对 is_anonymous=True 强校验 sentinel owner，
    非 sentinel → 403。"""
    src = (_REPO / "gateway" / "user_voice_api.py").read_text(encoding="utf-8")
    assert "_ANON_PREVIEW_SENTINEL_EMAIL" in src
    assert "anonymous_reserve_requires_sentinel_owner" in src
    # 校验在 is_anonymous 分支内
    assert "if is_anonymous:" in src
