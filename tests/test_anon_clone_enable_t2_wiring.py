"""P2 — 匿名/快捷 CosyVoice 克隆接线测试.

plan 2026-06-14-anonymous-express-cosyvoice-clone-enable §3.1/§3.4.

锁定新接线的安全边界：
- maybe_run_express_auto_clone 匿名分支（L1' 匿名主开关 / 跳过 L3 allowlist /
  L4 consent / thread is_anonymous）。
- 登录态 express 路径**行为不变**（仍用 express 主开关 + allowlist）。
- 🔥 任一闸不过 / 任何失败 → return None → 回 CosyVoice 预设，**绝不** MiniMax。
- reservation_client + endpoint 的 is_anonymous → 全局 cap 选择。
- payload_spec 白名单放行 express_consent；FORBIDDEN 仍全禁。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO / "src"), str(_REPO / "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import services.express.pipeline_clients as pc  # noqa: E402


# ---------------------------------------------------------------------------
# maybe_run_express_auto_clone 匿名分支 gating
# ---------------------------------------------------------------------------


def _patch_common(monkeypatch, *, admin_values: dict, worker_enabled: bool = True):
    """monkeypatch _admin（按 key 返回）+ worker env + 捕获 build/run。"""
    monkeypatch.setattr(pc, "_admin", lambda key, default=None: admin_values.get(key, default))

    import services.mainland_worker.client_factory as cf
    monkeypatch.setattr(cf, "is_worker_enabled_in_env", lambda: worker_enabled)

    captured: dict = {}

    def _fake_build(**kwargs):
        captured["build_kwargs"] = kwargs
        return object()  # opaque clients sentinel

    def _fake_run(**kwargs):
        captured["run_kwargs"] = kwargs
        return pc.ExpressAutoCloneOutcome(cloned=True, decision="cloned", reason_code="cloned")

    monkeypatch.setattr(pc, "build_express_auto_clone_clients", _fake_build)
    monkeypatch.setattr(pc, "run_express_auto_clone", _fake_run)
    return captured


_CONSENT_OK = {"auto_voice_clone": True, "server_confirmed_at": "2026-06-14T00:00:00+00:00"}


def _call(**overrides):
    kwargs = dict(
        user_id="11111111-1111-1111-1111-111111111111",
        job_id="job_abc",
        project_dir="/tmp/proj",
        source_audio_path="/tmp/a.wav",
        transcript_lines=[],
        speaker_voices={},
        speaker_routing={},
        express_consent=_CONSENT_OK,
    )
    kwargs.update(overrides)
    return pc.maybe_run_express_auto_clone(**kwargs)


def test_anon_branch_uses_anon_master_switch_not_express(monkeypatch):
    """匿名分支只看 anonymous_express_cosyvoice_clone_enabled；express 主开关
    为 True 也不能放行匿名克隆（隔离防误开）。"""
    cap = _patch_common(
        monkeypatch,
        admin_values={
            "express_cosyvoice_auto_clone_enabled": True,   # 登录态开关开
            "anonymous_express_cosyvoice_clone_enabled": False,  # 匿名开关关
        },
    )
    out = _call(anonymous_preview=True)
    assert out is None
    assert "build_kwargs" not in cap  # 没构造 client、没付费动作


def test_anon_branch_enabled_reaches_orchestration_with_is_anonymous(monkeypatch):
    """匿名开关开 + consent + worker → 进编排，且 build_clients 收到
    is_anonymous=True（→ reservation 走全局 cap + sentinel owner）。"""
    cap = _patch_common(
        monkeypatch,
        admin_values={"anonymous_express_cosyvoice_clone_enabled": True},
    )
    out = _call(anonymous_preview=True)
    assert out is not None and out.cloned is True
    assert cap["build_kwargs"]["is_anonymous"] is True


def test_anon_branch_skips_allowlist(monkeypatch):
    """匿名分支**跳过** allowlist（匿名无 user role）：即使 allowlist 为空
    （登录态会 fail-closed skip），匿名仍放行（靠全局 cap 兜底）。"""
    cap = _patch_common(
        monkeypatch,
        admin_values={
            "anonymous_express_cosyvoice_clone_enabled": True,
            "express_cosyvoice_auto_clone_allowlist_enabled": True,
            "express_cosyvoice_auto_clone_user_allowlist": [],  # 空：登录态会 skip
        },
    )
    out = _call(anonymous_preview=True)
    assert out is not None  # 匿名不被空 allowlist 挡
    assert cap["build_kwargs"]["is_anonymous"] is True


def test_anon_no_consent_skips(monkeypatch):
    """🔥 匿名 + 开关开 + 无 consent（未勾选 opt-in）→ None，绝不进编排。"""
    cap = _patch_common(
        monkeypatch,
        admin_values={"anonymous_express_cosyvoice_clone_enabled": True},
    )
    out = _call(anonymous_preview=True, express_consent=None)
    assert out is None
    assert "build_kwargs" not in cap


def test_anon_worker_disabled_skips(monkeypatch):
    """匿名 + 开关开 + consent 但 worker env 未就绪 → None（回预设）。"""
    cap = _patch_common(
        monkeypatch,
        admin_values={"anonymous_express_cosyvoice_clone_enabled": True},
        worker_enabled=False,
    )
    out = _call(anonymous_preview=True)
    assert out is None
    assert "build_kwargs" not in cap


def test_logged_in_path_unchanged_uses_express_switch(monkeypatch):
    """登录态（anonymous_preview=False）仍只看 express 主开关：anon 开关 True
    不能放行登录态克隆。"""
    cap = _patch_common(
        monkeypatch,
        admin_values={
            "express_cosyvoice_auto_clone_enabled": False,  # 登录态关
            "anonymous_express_cosyvoice_clone_enabled": True,  # 匿名开（无关）
        },
    )
    out = _call(anonymous_preview=False)
    assert out is None
    assert "build_kwargs" not in cap


def test_logged_in_still_enforces_allowlist(monkeypatch):
    """登录态空 allowlist 仍 fail-closed skip（行为不变）。"""
    cap = _patch_common(
        monkeypatch,
        admin_values={
            "express_cosyvoice_auto_clone_enabled": True,
            "express_cosyvoice_auto_clone_allowlist_enabled": True,
            "express_cosyvoice_auto_clone_user_allowlist": [],
        },
    )
    out = _call(anonymous_preview=False)
    assert out is None
    assert "build_kwargs" not in cap


# ---------------------------------------------------------------------------
# reservation_client + endpoint helper：is_anonymous → 全局 cap
# ---------------------------------------------------------------------------


def test_reservation_client_threads_is_anonymous(monkeypatch):
    """reserve(is_anonymous=True) → POST payload 带 is_anonymous=True。"""
    import services.express.reservation_client as rc

    captured: dict = {}

    def _fake_post(path, payload, *, timeout=5.0):
        captured["payload"] = payload
        return 200, {"ok": True, "reservation_id": "r1"}

    monkeypatch.setattr(rc, "_post_json", _fake_post)
    rc.reserve(user_id="u", job_id="j", speaker_id="speaker_a",
               target_model="cosyvoice-v3.5-flash", is_anonymous=True)
    assert captured["payload"]["is_anonymous"] is True


def test_reservation_client_default_not_anonymous(monkeypatch):
    """默认 is_anonymous=False（登录态行为不变）。"""
    import services.express.reservation_client as rc

    captured: dict = {}
    monkeypatch.setattr(rc, "_post_json", lambda p, pl, *, timeout=5.0: (captured.setdefault("payload", pl), (200, {"ok": True, "reservation_id": "r1"}))[1])
    rc.reserve(user_id="u", job_id="j", speaker_id="speaker_a", target_model="cosyvoice-v3.5-flash")
    assert captured["payload"]["is_anonymous"] is False


def test_endpoint_anonymous_caps_helper(monkeypatch, tmp_path):
    """endpoint 的匿名 caps helper 读 anonymous_clone_* 旋钮（全局 cap）。"""
    import admin_settings as adm
    import user_voice_api as uva

    settings_file = tmp_path / "admin_settings.json"
    monkeypatch.setattr(adm, "SETTINGS_FILE", settings_file)
    adm.save_settings(adm.AdminSettings(
        anonymous_clone_daily_global_cap=77,
        anonymous_clone_active_cap=9,
    ))
    # user_voice_api 的 load_settings 也读同一 SETTINGS_FILE（monkeypatch 生效）
    daily, active, ttl = uva._load_anonymous_clone_reservation_caps()
    assert daily == 77
    assert active == 9
    assert ttl >= 5  # reservation_ttl 复用 express 默认


# ---------------------------------------------------------------------------
# payload_spec 白名单 / FORBIDDEN
# ---------------------------------------------------------------------------


def test_payload_spec_allows_express_consent():
    from anonymous_preview_payload_spec import (
        ANONYMOUS_PREVIEW_PAYLOAD_SPEC,
        FORBIDDEN_PAYLOAD_FIELDS,
        validate_create_payload,
    )

    assert "express_consent" in ANONYMOUS_PREVIEW_PAYLOAD_SPEC
    # FORBIDDEN 仍全禁（MiniMax/MiMo-voiceclone 字段）
    assert "voice_clone" in FORBIDDEN_PAYLOAD_FIELDS
    assert "voiceclone_reference_path" in FORBIDDEN_PAYLOAD_FIELDS
    # 含 express_consent 的合法 express payload 不违规
    payload = {
        "job_type": "localize_video",
        "source": {"type": "local_video", "value": "/x.mp4"},
        "user_id": "u",
        "output_target": "editor",
        "service_mode": "express",
        "requires_review": False,
        "voice_strategy": "preset_mapping",
        "tts_provider": "cosyvoice",
        "source_content_hash": "h",
        "anonymous_preview": True,
        "express_consent": {"auto_voice_clone": True, "server_confirmed_at": "x"},
    }
    assert validate_create_payload(payload) == []
    # 夹带 voice_clone 仍被拒
    bad = dict(payload, voice_clone={"x": 1})
    assert "voice_clone" in validate_create_payload(bad)
