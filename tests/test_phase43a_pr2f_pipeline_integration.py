"""Phase 4.3a PR2-F — process.py 接线 + 真实依赖装配守卫。

覆盖（Codex PR2-F 边界）：

- ``maybe_run_express_auto_clone`` 进入闸：admin 主开关默认 OFF / 无 consent /
  worker env 未启用 / allowlist 排除 → **不调** ``run_express_auto_clone``
- 全闸通过 + clone 成功 → ``speaker_voices`` 注入 clone voice_id +
  ``speaker_routing`` 注入 worker routing（**关键防回归：不是 longshuo_v3**）
- register-smart payload 带齐 cosyvoice worker routing 自洽 8 字段
- upload 走 multipart + X-Internal-Key
- worker clone / delete_voice 适配器映射
- process.py 在 express 分支调 ``maybe_run_express_auto_clone`` 且传 routing dict
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from services.express import pipeline_clients as pc  # noqa: E402
from services.express.auto_clone import (  # noqa: E402
    CloneResult,
    ExpressAutoCloneClients,
    RegisterResult,
    ReserveResult,
    SamplePrep,
    UploadResult,
)
from services.express.pipeline_clients import (  # noqa: E402
    _K_ALLOWLIST,
    _K_ALLOWLIST_ENABLED,
    _K_ENABLED,
    _K_MIN_LINES,
    _K_MIN_RATIO,
    _K_SAMPLE_MAX_SECONDS,
    _K_TARGET_MODEL,
)

_UNSET = object()  # distinct from None so tests can pass an explicit null allowlist

_CONSENT_OK = {
    "auto_voice_clone": True,
    "server_confirmed_at": "2026-05-28T03:44:51.345Z",
    "client_confirmed_at": "2026-05-28T03:44:50.000Z",
}
_TRANSCRIPT = [{"speaker_id": "speaker_a"}] * 8 + [{"speaker_id": "speaker_b"}] * 2


class _FakeResp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload


def _all_success_clients() -> ExpressAutoCloneClients:
    return ExpressAutoCloneClients(
        prepare_sample=lambda **k: SamplePrep(sample_path="/tmp/s.wav", duration_s=14.0, segment_ids=()),
        reserve=lambda **k: ReserveResult(ok=True, reservation_id="resv-1"),
        upload=lambda **k: UploadResult(ok=True, presigned_get_url="https://oss/x", sha256="abc123"),
        clone=lambda **k: CloneResult(ok=True, voice_id="cosyvoice-v3.5-flash-CLONE", worker_request_id="wr-1"),
        register=lambda **k: RegisterResult(ok=True),
        delete_voice=lambda *a, **k: True,
        consume=lambda *a, **k: True,
        release=lambda *a, **k: True,
    )


def _invoke(
    monkeypatch,
    *,
    enabled=True,
    worker=True,
    consent=_CONSENT_OK,
    allowlist_enabled=True,
    allowlist=_UNSET,
    user_id="user-1",
    real_run=False,
    clients=None,
    project_dir="/tmp/express_pr2f",
):
    # allowlist is fail-closed: default sentinel None → allowlist the test user
    # so "all gates pass" cases proceed; explicit values exercise the gate.
    effective_allowlist = [user_id] if allowlist is _UNSET else allowlist
    settings_map = {
        _K_ENABLED: enabled,
        _K_ALLOWLIST_ENABLED: allowlist_enabled,
        _K_ALLOWLIST: effective_allowlist,
        _K_TARGET_MODEL: "cosyvoice-v3.5-flash",
        _K_MIN_RATIO: 0.30,
        _K_MIN_LINES: 5,
        _K_SAMPLE_MAX_SECONDS: 20,
    }
    monkeypatch.setattr(pc, "_admin", lambda key, default=None: settings_map.get(key, default))
    monkeypatch.setattr(
        "services.mainland_worker.client_factory.is_worker_enabled_in_env", lambda: worker
    )
    run_calls = {"n": 0}
    if not real_run:
        def _stub_run(**kwargs):
            run_calls["n"] += 1
            return None
        monkeypatch.setattr(pc, "run_express_auto_clone", _stub_run)
        monkeypatch.setattr(pc, "build_express_auto_clone_clients", lambda **k: object())
    elif clients is not None:
        monkeypatch.setattr(pc, "build_express_auto_clone_clients", lambda **k: clients)

    sv: dict = {}
    sr: dict = {}
    outcome = pc.maybe_run_express_auto_clone(
        user_id=user_id,
        job_id="job-1",
        project_dir=project_dir,
        source_audio_path="/tmp/a.wav",
        transcript_lines=_TRANSCRIPT,
        speaker_voices=sv,
        speaker_routing=sr,
        express_consent=consent,
    )
    return outcome, sv, sr, run_calls


# ===========================================================================
# 进入闸：失败 → 不调 run_express_auto_clone
# ===========================================================================


def test_gate_admin_disabled_default_is_noop(monkeypatch):
    """admin 主开关默认 OFF → no-op，绝不调 auto_clone（DoD #8）。"""
    outcome, sv, sr, run_calls = _invoke(monkeypatch, enabled=False)
    assert outcome is None and run_calls["n"] == 0
    assert sv == {} and sr == {}


def test_gate_no_consent_skips(monkeypatch):
    outcome, sv, _, run_calls = _invoke(monkeypatch, consent={"auto_voice_clone": False})
    assert outcome is None and run_calls["n"] == 0 and sv == {}


def test_gate_consent_missing_server_confirmed_at_skips(monkeypatch):
    outcome, _, _, run_calls = _invoke(monkeypatch, consent={"auto_voice_clone": True})
    assert outcome is None and run_calls["n"] == 0


def test_gate_worker_env_disabled_skips(monkeypatch):
    outcome, _, _, run_calls = _invoke(monkeypatch, worker=False)
    assert outcome is None and run_calls["n"] == 0


def test_gate_allowlist_excludes_user_skips(monkeypatch):
    outcome, _, _, run_calls = _invoke(monkeypatch, allowlist=["someone-else"], user_id="user-1")
    assert outcome is None and run_calls["n"] == 0


def test_gate_allowlist_includes_user_proceeds(monkeypatch):
    outcome, _, _, run_calls = _invoke(monkeypatch, allowlist=["user-1"], user_id="user-1")
    assert run_calls["n"] == 1, "user 在 allowlist 内应进入 auto_clone"


def test_gate_empty_allowlist_skips(monkeypatch):
    """fail-closed：空 allowlist = 没人能用（等效 admin flag off），不是全员放行
    （Codex PR2-F-fix；与 PR1 availability + spec 一致）。"""
    outcome, _, _, run_calls = _invoke(monkeypatch, allowlist=[])
    assert outcome is None and run_calls["n"] == 0, "空 allowlist 必须 skip（fail-closed）"


def test_gate_allowlist_disabled_empty_allowlist_proceeds(monkeypatch):
    outcome, _, _, run_calls = _invoke(
        monkeypatch,
        allowlist_enabled=False,
        allowlist=[],
    )
    assert run_calls["n"] == 1, "allowlist gate disabled should not block eligible users"


def test_gate_malformed_allowlist_skips(monkeypatch):
    """allowlist 非 list（string / dict / None）→ fail-closed skip。"""
    for bad in ("user-1", {"user-1": True}, None, 123):
        outcome, _, _, run_calls = _invoke(monkeypatch, allowlist=bad)
        assert outcome is None and run_calls["n"] == 0, (
            f"malformed allowlist {bad!r} 必须 fail-closed skip"
        )


def test_gate_malformed_allowlist_enabled_still_enforces_allowlist(monkeypatch):
    outcome, _, _, run_calls = _invoke(
        monkeypatch,
        allowlist_enabled="false",
        allowlist=[],
    )
    assert outcome is None and run_calls["n"] == 0


def test_gate_missing_identity_skips(monkeypatch):
    outcome, _, _, run_calls = _invoke(monkeypatch, user_id="")
    assert outcome is None and run_calls["n"] == 0


def test_all_gates_pass_invokes_run(monkeypatch):
    outcome, _, _, run_calls = _invoke(monkeypatch)
    assert run_calls["n"] == 1


# ===========================================================================
# 关键防回归：clone 成功 → 注入 clone routing（不是 longshuo_v3）
# ===========================================================================


def test_clone_success_injects_clone_voice_and_worker_routing(monkeypatch):
    """Codex PR2-F 强制防回归：Express auto-clone 成功后，主说话人的 TTS 必须
    走 clone voice_id + requires_worker，**绝不**回落官方预设 longshuo_v3。"""
    outcome, sv, sr, _ = _invoke(monkeypatch, real_run=True, clients=_all_success_clients())
    assert outcome is not None and outcome.cloned is True
    # speaker_voices 注入 clone voice_id（不是 longshuo_v3 预设）
    assert sv.get("speaker_a") == "cosyvoice-v3.5-flash-CLONE"
    assert sv.get("speaker_a") != "longshuo_v3"
    # worker routing 注入 → 下游 TTS dispatch 到武汉 worker
    assert sr.get("speaker_a") == {
        "requires_worker": True,
        "worker_target_model": "cosyvoice-v3.5-flash",
    }


def test_clone_failure_leaves_preset_path(monkeypatch):
    """clone 失败 → speaker_voices 不变（回预设），不注入 routing。"""
    clients = _all_success_clients()
    clients = ExpressAutoCloneClients(
        prepare_sample=clients.prepare_sample,
        reserve=clients.reserve,
        upload=clients.upload,
        clone=lambda **k: CloneResult(ok=False, error="worker_5xx"),
        register=clients.register,
        delete_voice=clients.delete_voice,
        consume=clients.consume,
        release=clients.release,
    )
    outcome, sv, sr, _ = _invoke(monkeypatch, real_run=True, clients=clients)
    assert outcome is not None and outcome.cloned is False
    assert sv == {} and sr == {}, "失败必须保持预设路径（空 → 下游 preset 匹配）"


def test_clone_only_fans_out_clone_voice_to_unassigned_speakers():
    """zh->en CosyVoice clone-only must not let secondary speakers fall
    back into the zh-only preset matcher.  Any still-unassigned speaker should
    use the cloned worker voice for this Express run."""
    from pipeline.process import _fan_out_express_clone_to_unassigned_speakers

    class _Outcome:
        cloned = True
        voice_id = "cosyvoice-v3.5-flash-avtspeak-main"
        main_speaker_id = "speaker_b"

    speaker_voices = {
        "speaker_a": "auto",
        "speaker_b": "cosyvoice-v3.5-flash-avtspeak-main",
        "speaker_c": "existing-concrete-voice",
    }
    speaker_routing = {
        "speaker_b": {
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-flash",
        }
    }

    filled = _fan_out_express_clone_to_unassigned_speakers(
        speaker_voices,
        speaker_routing,
        _Outcome(),
    )

    assert filled == ["speaker_a"]
    assert speaker_voices["speaker_a"] == "cosyvoice-v3.5-flash-avtspeak-main"
    assert speaker_routing["speaker_a"] == {
        "requires_worker": True,
        "worker_target_model": "cosyvoice-v3.5-flash",
    }
    assert speaker_voices["speaker_c"] == "existing-concrete-voice"
    assert "speaker_c" not in speaker_routing


# ===========================================================================
# register-smart payload + upload multipart 适配器
# ===========================================================================


def test_clone_only_fans_out_clone_voice_over_non_worker_preset_speakers():
    """zh->en clone-only jobs must not preserve concrete CosyVoice presets.

    A concrete preset without worker routing still reaches the zh-only
    CosyVoice matcher for an English target and fails closed.  In clone-only
    Express runs, every non-worker speaker should reuse the cloned worker voice.
    """
    from pipeline.process import _fan_out_express_clone_to_unassigned_speakers

    class _Outcome:
        cloned = True
        voice_id = "cosyvoice-v3.5-flash-avtspeak-main"
        main_speaker_id = "speaker_b"

    speaker_voices = {
        "speaker_a": "auto",
        "speaker_b": "cosyvoice-v3.5-flash-avtspeak-main",
        "speaker_c": "longshuo_v3",
    }
    speaker_routing = {
        "speaker_b": {
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-flash",
        }
    }

    filled = _fan_out_express_clone_to_unassigned_speakers(
        speaker_voices,
        speaker_routing,
        _Outcome(),
    )

    assert filled == ["speaker_a", "speaker_c"]
    assert speaker_voices["speaker_a"] == "cosyvoice-v3.5-flash-avtspeak-main"
    assert speaker_voices["speaker_c"] == "cosyvoice-v3.5-flash-avtspeak-main"
    assert speaker_routing["speaker_a"] == {
        "requires_worker": True,
        "worker_target_model": "cosyvoice-v3.5-flash",
    }
    assert speaker_routing["speaker_c"] == {
        "requires_worker": True,
        "worker_target_model": "cosyvoice-v3.5-flash",
    }


def test_register_payload_has_required_cosyvoice_fields(monkeypatch):
    captured: dict = {}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResp(200, {"ok": True, "voice_id": "v1", "user_id": "u1"})

    monkeypatch.setenv("AVT_INTERNAL_API_KEY", "internal-key-1234567890")
    monkeypatch.setattr(pc.requests, "post", _fake_post)

    res = pc._http_register_smart(
        voice_id="v1",
        speaker_id="speaker_a",
        job_id="job-1",
        user_id="user-1",
        target_model="cosyvoice-v3.5-flash",
        temporary_expires_at="2026-06-04T03:44:51.345Z",
    )
    assert res.ok
    p = captured["json"]
    assert p["provider"] == "cosyvoice_voice_clone"
    assert p["tts_provider"] == "cosyvoice"
    assert p["platform"] == "dashscope_mainland"
    assert p["requires_worker"] is True
    assert p["target_model"] == "cosyvoice-v3.5-flash"
    assert p["is_temporary"] is True
    assert p["temporary_expires_at"] == "2026-06-04T03:44:51.345Z"
    assert p["created_from"] == "express_auto"
    assert captured["headers"].get("X-Internal-Key") == "internal-key-1234567890"
    assert captured["url"].endswith("/api/internal/user-voices/register-smart")


def test_register_non_200_returns_not_ok(monkeypatch):
    monkeypatch.setattr(pc.requests, "post", lambda *a, **k: _FakeResp(500, {"error": "boom"}))
    res = pc._http_register_smart(
        voice_id="v1", speaker_id="speaker_a", job_id="j", user_id="u",
        target_model="cosyvoice-v3.5-flash", temporary_expires_at="2026-06-04T00:00:00Z",
    )
    assert not res.ok and res.detail


def test_upload_sends_multipart_with_internal_key(monkeypatch, tmp_path):
    sample = tmp_path / "speaker_a.wav"
    sample.write_bytes(b"RIFFfake-wav-bytes")
    captured: dict = {}

    def _fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["files"] = dict(files or {})
        captured["data"] = dict(data or {})
        return _FakeResp(200, {"ok": True, "presigned_get_url": "https://oss/x", "sha256": "abc"})

    monkeypatch.setenv("AVT_INTERNAL_API_KEY", "internal-key-1234567890")
    monkeypatch.setattr(pc.requests, "post", _fake_post)

    res = pc._http_upload_sample(
        sample_path=str(sample), user_id="user-1", job_id="job-1", speaker_id="speaker_a"
    )
    assert res.ok and res.presigned_get_url == "https://oss/x" and res.sha256 == "abc"
    assert "sample" in captured["files"], "必须 multipart 上传 sample 文件"
    assert captured["data"]["user_id"] == "user-1"
    assert captured["data"]["job_id"] == "job-1"
    assert captured["data"]["speaker_id"] == "speaker_a"
    assert captured["headers"].get("X-Internal-Key") == "internal-key-1234567890"
    assert captured["url"].endswith("/api/internal/cosyvoice/express-sample-upload")


def test_upload_uses_production_safe_timeout_by_default(monkeypatch, tmp_path):
    sample = tmp_path / "speaker_a.wav"
    sample.write_bytes(b"RIFFfake-wav-bytes")
    captured: dict = {}

    def _fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured["timeout"] = timeout
        return _FakeResp(200, {"ok": True, "presigned_get_url": "https://oss/x", "sha256": "abc"})

    monkeypatch.delenv("AVT_EXPRESS_SAMPLE_UPLOAD_TIMEOUT_S", raising=False)
    monkeypatch.setattr(pc.requests, "post", _fake_post)

    res = pc._http_upload_sample(
        sample_path=str(sample), user_id="user-1", job_id="job-1", speaker_id="speaker_a"
    )

    assert res.ok
    assert captured["timeout"] >= 90.0


def test_upload_malformed_200_is_malformed(monkeypatch, tmp_path):
    sample = tmp_path / "s.wav"
    sample.write_bytes(b"x")
    monkeypatch.setattr(
        pc.requests, "post", lambda *a, **k: _FakeResp(200, {"ok": True})  # 缺 url/sha
    )
    res = pc._http_upload_sample(sample_path=str(sample), user_id="u", job_id="j", speaker_id="speaker_a")
    assert not res.ok and res.error == "malformed_upload_response"


# ===========================================================================
# worker clone / delete 适配器映射
# ===========================================================================


class _FakeWorkerResp:
    voice_id = "cosyvoice-v3.5-flash-W"
    worker_request_id = "wr-9"
    provider_request_id = "pr-9"


class _FakeWorker:
    def __init__(self):
        self.cloned = None
        self.deleted = None

    def clone(self, req):
        self.cloned = req
        return _FakeWorkerResp()

    def delete_voice(self, voice_id, req):
        self.deleted = (voice_id, req)
        return object()


def test_build_clients_clone_maps_worker_response(monkeypatch):
    fake = _FakeWorker()
    monkeypatch.setattr(
        "services.mainland_worker.client_factory.build_client_from_env", lambda: fake
    )
    clients = pc.build_express_auto_clone_clients(
        user_id="u", job_id="j", project_dir="/tmp/p", source_audio_path="/tmp/a.wav",
        target_model="cosyvoice-v3.5-flash", sample_max_seconds=20.0,
        temporary_expires_at="2026-06-04T00:00:00Z",
    )
    res = clients.clone(
        sample_url="https://oss/x", sample_sha256="abc", speaker_id="speaker_a",
        job_id="j", user_id="u", consent_at="2026-05-28T00:00:00Z",
    )
    assert res.ok and res.voice_id == "cosyvoice-v3.5-flash-W"
    assert res.worker_request_id == "wr-9"
    # WorkerCloneRequest 自洽：download_url + sha256 + consent confirmed
    assert fake.cloned.sample.kind == "download_url"
    assert fake.cloned.sample.sha256 == "abc"
    assert fake.cloned.consent.voice_clone_confirmed is True


def test_build_clients_delete_voice_best_effort(monkeypatch):
    fake = _FakeWorker()
    monkeypatch.setattr(
        "services.mainland_worker.client_factory.build_client_from_env", lambda: fake
    )
    clients = pc.build_express_auto_clone_clients(
        user_id="u", job_id="j", project_dir="/tmp/p", source_audio_path="/tmp/a.wav",
        target_model="cosyvoice-v3.5-flash", sample_max_seconds=20.0,
        temporary_expires_at="2026-06-04T00:00:00Z",
    )
    assert clients.delete_voice("voice-xyz", reason="express_register_failed") is True
    assert fake.deleted[0] == "voice-xyz"


def test_clone_returns_not_configured_when_worker_none(monkeypatch):
    monkeypatch.setattr("services.mainland_worker.client_factory.build_client_from_env", lambda: None)
    clients = pc.build_express_auto_clone_clients(
        user_id="u", job_id="j", project_dir="/tmp/p", source_audio_path="/tmp/a.wav",
        target_model="cosyvoice-v3.5-flash", sample_max_seconds=20.0,
        temporary_expires_at="2026-06-04T00:00:00Z",
    )
    res = clients.clone(
        sample_url="u", sample_sha256="s", speaker_id="speaker_a", job_id="j",
        user_id="u", consent_at="t",
    )
    assert not res.ok and res.error == "worker_not_configured"


# ===========================================================================
# process.py 接线守卫（静态扫，不 import process.py）
# ===========================================================================


def test_process_wires_express_auto_clone_in_express_branch():
    src = (_SRC / "pipeline" / "process.py").read_text(encoding="utf-8")
    assert "from services.express.pipeline_clients import" in src
    assert "maybe_run_express_auto_clone" in src
    # 仅 express 分支进入（service_mode gate）
    assert 'job_service_mode == "express"' in src
    # 传入两个 routing dict（注入目标）
    assert "speaker_voices=_speaker_voices" in src
    assert "speaker_routing=_speaker_voice_routing" in src
    # 传 consent（无 consent → 内部 skip）
    assert 'express_consent=_snap("express_consent", None)' in src


def test_process_express_clone_only_failure_is_fatal():
    """CosyVoice Express clone-only jobs must fail closed instead of falling back to presets."""
    src = (_SRC / "pipeline" / "process.py").read_text(encoding="utf-8")
    assert "express_clone_required" in src
    assert "Express auto-clone required but not completed" in src


def test_process_express_clone_failure_is_non_fatal():
    """process.py 调用点包 try/except（失败不炸 pipeline，回预设）。"""
    src = (_SRC / "pipeline" / "process.py").read_text(encoding="utf-8")
    assert "Express auto-clone 异常" in src, "调用点必须 try/except 兜底（回预设）"
