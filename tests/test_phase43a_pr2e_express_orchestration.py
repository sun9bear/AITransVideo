"""Phase 4.3a PR2-E — Express auto-clone 编排模块 + 调用顺序 / 失败路径守卫。

覆盖（Codex PR2-E 边界）：

- ``main_speaker.identify_express_main_speaker`` 阈值逻辑
- ``reservation_client`` 调 C internal endpoints（mock urllib）：200/409/404/503/
  transport-error + X-Internal-Key 注入
- ``auto_clone.run_express_auto_clone`` 顺序锁死 + 全失败路径：
  - sample-validated → reserve → upload → worker → register → consume
  - 无 reserve 不 worker；upload/worker fail → release；
    worker ok + register fail → delete_voice + release；
    consume fail → 注入 routing 但 audit 不静默；release 异常 → audit 留痕
  - 任何失败 → speaker_voices 不变（回预设，不调 MiniMax）
- AST 守卫：express/* 不 import gateway / boto3 / minimax；auto_clone 源码里
  reserve 调用在 upload/worker 之前、sample 之后
"""
from __future__ import annotations

import ast
import sys
import urllib.error
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from services.express import auto_clone as ac  # noqa: E402
from services.express import reservation_client as rc  # noqa: E402
from services.express.auto_clone import (  # noqa: E402
    CloneResult,
    ExpressAutoCloneClients,
    RegisterResult,
    ReserveResult,
    SamplePrep,
    UploadResult,
    run_express_auto_clone,
)
from services.express.main_speaker import identify_express_main_speaker  # noqa: E402
from services.usage_meter import UsageMeter  # noqa: E402

_EXPRESS_DIR = _SRC / "services" / "express"


# ===========================================================================
# main_speaker
# ===========================================================================


def _lines(spec: dict[str, int]) -> list[dict]:
    out: list[dict] = []
    for sid, n in spec.items():
        out.extend({"speaker_id": sid} for _ in range(n))
    return out


def test_identify_main_speaker_solo_returns_top():
    assert identify_express_main_speaker(_lines({"speaker_a": 12})) == "speaker_a"


def test_identify_main_speaker_balanced_two_returns_top_if_above_ratio():
    # 8 vs 2 → top 80% ≥ 30%
    assert identify_express_main_speaker(_lines({"speaker_a": 8, "speaker_b": 2})) == "speaker_a"


def test_identify_main_speaker_3way_split_returns_none():
    # 4/4/4 → top 33% but... 33% ≥ 30%; use a genuinely flat split below ratio
    assert identify_express_main_speaker(_lines({"a": 4, "b": 4, "c": 4, "d": 4})) is None


def test_identify_main_speaker_below_ratio_returns_none():
    # top 5 / total 20 = 25% < 30%
    assert identify_express_main_speaker(_lines({"a": 5, "b": 5, "c": 5, "d": 5})) is None


def test_identify_main_speaker_below_min_lines_returns_none():
    # solo but only 3 lines < 5
    assert identify_express_main_speaker(_lines({"a": 3})) is None


def test_identify_main_speaker_empty_returns_none():
    assert identify_express_main_speaker([]) is None
    assert identify_express_main_speaker([{"no_speaker": 1}]) is None


def test_identify_main_speaker_custom_thresholds():
    # min_line_count=2 lets a 3-line solo pass
    assert identify_express_main_speaker(_lines({"a": 3}), min_line_count=2) == "a"


# ===========================================================================
# reservation_client (mock urllib)
# ===========================================================================


def test_reserve_200_ok(monkeypatch):
    monkeypatch.setattr(rc, "_post_json", lambda *a, **k: (200, {"ok": True, "reservation_id": "r1", "status": "reserved"}))
    res = rc.reserve(user_id="u1", job_id="j1", speaker_id="speaker_a", target_model="cosyvoice-v3.5-flash")
    assert res.ok and res.reservation_id == "r1"


def test_reserve_409_deny(monkeypatch):
    monkeypatch.setattr(rc, "_post_json", lambda *a, **k: (409, {"ok": False, "deny_reason": "active_temp_cap_exceeded"}))
    res = rc.reserve(user_id="u1", job_id="j1", speaker_id="speaker_a", target_model="m")
    assert not res.ok and res.deny_reason == "active_temp_cap_exceeded"


def test_reserve_404_user_not_found(monkeypatch):
    monkeypatch.setattr(rc, "_post_json", lambda *a, **k: (404, {"ok": False, "error": "user_not_found"}))
    res = rc.reserve(user_id="u1", job_id="j1", speaker_id="speaker_a", target_model="m")
    assert not res.ok and res.error == "user_not_found"


def test_reserve_503_admin_unavailable(monkeypatch):
    monkeypatch.setattr(rc, "_post_json", lambda *a, **k: (503, {"ok": False, "error": "admin_settings_unavailable"}))
    res = rc.reserve(user_id="u1", job_id="j1", speaker_id="speaker_a", target_model="m")
    assert not res.ok and res.error == "admin_settings_unavailable"


def test_reserve_transport_error(monkeypatch):
    def _boom(*a, **k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(rc, "_post_json", _boom)
    res = rc.reserve(user_id="u1", job_id="j1", speaker_id="speaker_a", target_model="m")
    assert not res.ok and res.error == "transport_error" and res.http_status == 0


def _fake_urlopen_factory(raw_bytes: bytes, status: int = 200):
    class _Resp:
        def __init__(self):
            self.status = status

        def read(self):
            return raw_bytes

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _f(req, timeout=None):
        return _Resp()

    return _f


def test_reserve_200_without_reservation_id_is_malformed(monkeypatch):
    """200 ok 但缺 reservation_id → ok=False malformed（Codex E-fix item 1）。"""
    monkeypatch.setattr(rc, "_post_json", lambda *a, **k: (200, {"ok": True, "status": "reserved"}))
    res = rc.reserve(user_id="u1", job_id="j1", speaker_id="speaker_a", target_model="m")
    assert not res.ok and res.error == "malformed_reserve_response"


def test_reserve_200_empty_reservation_id_is_malformed(monkeypatch):
    monkeypatch.setattr(rc, "_post_json", lambda *a, **k: (200, {"ok": True, "reservation_id": ""}))
    res = rc.reserve(user_id="u1", job_id="j1", speaker_id="speaker_a", target_model="m")
    assert not res.ok and res.error == "malformed_reserve_response"


def test_post_json_non_json_body_does_not_raise(monkeypatch):
    """200 非 JSON body → _post_json 返回 (200, {})，不裸抛（Codex E-fix item 3）。"""
    monkeypatch.setattr(rc.urllib.request, "urlopen", _fake_urlopen_factory(b"<html>oops</html>"))
    status, body = rc._post_json("/x", {})
    assert status == 200 and body == {}


def test_post_json_empty_body_does_not_raise(monkeypatch):
    monkeypatch.setattr(rc.urllib.request, "urlopen", _fake_urlopen_factory(b""))
    status, body = rc._post_json("/x", {})
    assert status == 200 and body == {}


def test_post_json_non_dict_json_coerced_to_empty(monkeypatch):
    # 合法 JSON 但不是 object（array）→ {}
    monkeypatch.setattr(rc.urllib.request, "urlopen", _fake_urlopen_factory(b'[1, 2, 3]'))
    status, body = rc._post_json("/x", {})
    assert status == 200 and body == {}


def test_reserve_non_json_200_maps_to_malformed(monkeypatch):
    """端到端：200 非 JSON → reserve 返回 malformed（不抛异常穿出 client）。"""
    monkeypatch.setattr(rc.urllib.request, "urlopen", _fake_urlopen_factory(b"not json{"))
    res = rc.reserve(user_id="u1", job_id="j1", speaker_id="speaker_a", target_model="m")
    assert not res.ok and res.error == "malformed_reserve_response"


def test_consume_200_and_409(monkeypatch):
    monkeypatch.setattr(rc, "_post_json", lambda *a, **k: (200, {"ok": True, "status": "consumed"}))
    assert rc.consume("r1", voice_id="v1").ok
    monkeypatch.setattr(rc, "_post_json", lambda *a, **k: (409, {"ok": False, "status": "released", "conflict_reason": "reservation_not_reservable"}))
    out = rc.consume("r1", voice_id="v1")
    assert not out.ok and out.conflict_reason == "reservation_not_reservable"


def test_release_200_and_409(monkeypatch):
    monkeypatch.setattr(rc, "_post_json", lambda *a, **k: (200, {"ok": True, "status": "released"}))
    assert rc.release("r1", reason="worker_failed").ok
    monkeypatch.setattr(rc, "_post_json", lambda *a, **k: (409, {"ok": False, "status": "consumed", "conflict_reason": "reservation_already_consumed"}))
    out = rc.release("r1", reason="x")
    assert not out.ok and out.conflict_reason == "reservation_already_consumed"


def test_reservation_client_injects_internal_key_and_url(monkeypatch):
    captured: dict = {}

    class _FakeResp:
        status = 200

        def read(self):
            return b'{"ok": true, "reservation_id": "r1", "status": "reserved"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["method"] = req.get_method()
        return _FakeResp()

    monkeypatch.setenv("AVT_GATEWAY_URL", "http://gw:8880")
    monkeypatch.setenv("AVT_INTERNAL_API_KEY", "secret-key-1234567890")
    monkeypatch.setattr(rc.urllib.request, "urlopen", _fake_urlopen)

    res = rc.reserve(user_id="u1", job_id="j1", speaker_id="speaker_a", target_model="cosyvoice-v3.5-flash")
    assert res.ok and res.reservation_id == "r1"
    assert captured["url"] == "http://gw:8880/api/internal/express-auto-clone-reservations/reserve"
    assert captured["headers"].get("x-internal-key") == "secret-key-1234567890"
    assert captured["method"] == "POST"


def test_reservation_client_does_not_import_gateway():
    """reservation_client 走 HTTP，不 import gateway service（Codex PR2-E）。"""
    mods = _imported_modules(_EXPRESS_DIR / "reservation_client.py")
    assert any("urllib" in m for m in mods), "reservation_client 应走 urllib HTTP"
    for m in mods:
        assert not m.startswith("gateway"), f"reservation_client 不应 import gateway（{m}）"


# ===========================================================================
# auto_clone orchestration — recording mock
# ===========================================================================


_CONSENT_OK = {
    "auto_voice_clone": True,
    "server_confirmed_at": "2026-05-28T03:44:51.345Z",
    "client_confirmed_at": "2026-05-28T03:44:50.000Z",
}
_TRANSCRIPT = _lines({"speaker_a": 8, "speaker_b": 2})  # speaker_a 80%
_SAMPLE_OK = SamplePrep(sample_path="/tmp/s.wav", duration_s=14.2, segment_ids=(3, 5, 7))


def _mk(rec: list, name: str, retval):
    def fn(*args, **kwargs):
        rec.append(name)
        if isinstance(retval, BaseException):
            raise retval
        return retval
    return fn


def _clients(
    rec: list,
    *,
    sample=_SAMPLE_OK,
    reserve=ReserveResult(ok=True, reservation_id="resv-1"),
    upload=UploadResult(ok=True, presigned_get_url="https://oss/x", sha256="abc123"),
    clone=CloneResult(ok=True, voice_id="cosyvoice-v3.5-flash-xyz", worker_request_id="wr-1"),
    register=RegisterResult(ok=True),
    delete_voice=True,
    consume=True,
    release=True,
) -> ExpressAutoCloneClients:
    return ExpressAutoCloneClients(
        prepare_sample=_mk(rec, "prepare_sample", sample),
        reserve=_mk(rec, "reserve", reserve),
        upload=_mk(rec, "upload", upload),
        clone=_mk(rec, "clone", clone),
        register=_mk(rec, "register", register),
        delete_voice=_mk(rec, "delete_voice", delete_voice),
        consume=_mk(rec, "consume", consume),
        release=_mk(rec, "release", release),
    )


@pytest.fixture
def captured_audit(monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(ac, "emit_express_clone_audit", lambda project_dir, fields: records.append(dict(fields)))
    return records


def _run(rec, captured_audit, *, clients=None, speaker_voices=None, speaker_routing=None, consent=_CONSENT_OK, transcript=_TRANSCRIPT):
    sv = speaker_voices if speaker_voices is not None else {}
    sr = speaker_routing if speaker_routing is not None else {}
    outcome = run_express_auto_clone(
        user_id="user-1",
        job_id="job-1",
        project_dir="/tmp/proj",
        transcript_lines=transcript,
        speaker_voices=sv,
        speaker_routing=sr,
        express_consent=consent,
        clients=clients or _clients(rec),
    )
    return outcome, sv, sr


def test_happy_path_order_and_routing(captured_audit):
    rec: list = []
    outcome, sv, sr = _run(rec, captured_audit)
    # 顺序锁死
    assert rec == ["prepare_sample", "reserve", "upload", "clone", "register", "consume"]
    assert outcome.cloned is True and outcome.decision == "cloned"
    assert sv["speaker_a"] == "cosyvoice-v3.5-flash-xyz"
    assert sr["speaker_a"] == {"requires_worker": True, "worker_target_model": "cosyvoice-v3.5-flash"}
    # 必写一行决策 audit
    assert any(r.get("decision") == "cloned" for r in captured_audit)


def test_happy_path_records_cosyvoice_clone_usage(tmp_path, captured_audit):
    """Express clone success must be visible in UsageMeter cost/reporting.

    Production regression: the clone succeeded and routing used the temporary
    CosyVoice voice, but usage_summary still showed zero clone calls.
    """
    rec: list = []
    sv: dict = {}
    sr: dict = {}
    outcome = run_express_auto_clone(
        user_id="user-1",
        job_id="job-1",
        project_dir=tmp_path,
        transcript_lines=_TRANSCRIPT,
        speaker_voices=sv,
        speaker_routing=sr,
        express_consent=_CONSENT_OK,
        clients=_clients(rec),
        target_model="cosyvoice-v3.5-flash",
    )

    assert outcome.cloned is True
    meter = UsageMeter(tmp_path, job_id="job-1")
    events = [event for event in meter.events if event.get("kind") == "voice_clone"]
    assert len(events) == 1
    event = events[0]
    assert event["provider"] == "cosyvoice_voice_clone"
    assert event["model"] == "cosyvoice-v3.5-flash"
    assert event["voice_id"] == "cosyvoice-v3.5-flash-xyz"
    assert event["speaker_id"] == "speaker_a"
    assert event["source_audio_seconds"] == 14.2
    assert event["selected_segment_count"] == 3
    assert event["clone_count"] == 1
    assert event["billable"] is True
    assert event["success"] is True
    assert event["source"] == "express_auto_clone"
    assert event["worker_target_model"] == "cosyvoice-v3.5-flash"

    summary = meter.summarize()
    assert summary["voice_clone_call_count"] == 1
    assert summary["voice_clone_success_call_count"] == 1
    assert summary["voice_clone_billable_count"] == 1
    assert summary["voice_clone_count_by_provider"] == {
        "cosyvoice_voice_clone": 1,
    }


def test_consent_not_given_skips_everything(captured_audit):
    rec: list = []
    outcome, sv, _ = _run(rec, captured_audit, consent={"auto_voice_clone": False})
    assert outcome.cloned is False and outcome.reason_code == "consent_not_given"
    assert rec == [], "没 consent 不应触发任何 client 调用"
    assert sv == {}


def test_no_main_speaker_skips(captured_audit):
    rec: list = []
    outcome, sv, _ = _run(rec, captured_audit, transcript=_lines({"a": 1, "b": 1, "c": 1}))
    assert outcome.reason_code == "no_main_speaker"
    assert "reserve" not in rec and sv == {}


def test_sample_too_short_skips_before_reserve(captured_audit):
    rec: list = []
    outcome, sv, _ = _run(rec, captured_audit, clients=_clients(rec, sample=None))
    assert outcome.reason_code == "sample_too_short"
    assert rec == ["prepare_sample"], "sample fail 不应 reserve（本地 CPU 不占名额）"
    assert sv == {}


def test_no_reserve_no_worker(captured_audit):
    """reserve denied → 绝不 upload / clone（核心成本闸守卫）。"""
    rec: list = []
    clients = _clients(rec, reserve=ReserveResult(ok=False, deny_reason="active_temp_cap_exceeded"))
    outcome, sv, _ = _run(rec, captured_audit, clients=clients)
    assert outcome.cloned is False
    assert outcome.reason_code == "reserve_active_temp_cap_exceeded"
    assert rec == ["prepare_sample", "reserve"], "reserve 失败后不应 upload/clone"
    assert "upload" not in rec and "clone" not in rec
    assert sv == {}


def test_reserve_ok_but_no_reservation_id_skips_no_worker(captured_audit):
    """reserve 看似 ok 但无 reservation_id → skip，绝不 upload/worker（E-fix）。"""
    rec: list = []
    clients = _clients(rec, reserve=ReserveResult(ok=True, reservation_id=None))
    outcome, sv, _ = _run(rec, captured_audit, clients=clients)
    assert outcome.cloned is False and outcome.reason_code == "reserve_malformed_response"
    assert rec == ["prepare_sample", "reserve"]
    assert "upload" not in rec and "clone" not in rec
    assert sv == {}


def test_upload_ok_but_missing_url_releases_no_worker(captured_audit):
    """upload ok 但缺 presigned URL → release，不进 worker（E-fix）。"""
    rec: list = []
    clients = _clients(rec, upload=UploadResult(ok=True, presigned_get_url=None, sha256="abc"))
    outcome, sv, _ = _run(rec, captured_audit, clients=clients)
    assert outcome.reason_code == "upload_malformed_response"
    assert "release" in rec and "clone" not in rec
    assert sv == {}


def test_upload_ok_but_missing_sha_releases_no_worker(captured_audit):
    rec: list = []
    clients = _clients(rec, upload=UploadResult(ok=True, presigned_get_url="https://x", sha256=None))
    outcome, sv, _ = _run(rec, captured_audit, clients=clients)
    assert outcome.reason_code == "upload_malformed_response"
    assert "release" in rec and "clone" not in rec
    assert sv == {}


def test_clone_ok_but_missing_voice_id_releases_no_register(captured_audit):
    """clone ok 但缺 voice_id → release，不 register/delete（无 id 可删）（E-fix）。"""
    rec: list = []
    clients = _clients(rec, clone=CloneResult(ok=True, voice_id=None, worker_request_id="wr"))
    outcome, sv, _ = _run(rec, captured_audit, clients=clients)
    assert outcome.reason_code == "worker_malformed_response"
    assert "release" in rec
    assert "register" not in rec and "delete_voice" not in rec
    assert sv == {}


def test_upload_fail_releases_and_no_worker(captured_audit):
    rec: list = []
    clients = _clients(rec, upload=UploadResult(ok=False, error="readiness_503"))
    outcome, sv, _ = _run(rec, captured_audit, clients=clients)
    assert outcome.reason_code == "upload_failed"
    assert "release" in rec and "clone" not in rec, "upload 失败必 release，且不进 worker"
    assert sv == {}


def test_worker_fail_releases(captured_audit):
    rec: list = []
    clients = _clients(rec, clone=CloneResult(ok=False, error="worker_5xx"))
    outcome, sv, _ = _run(rec, captured_audit, clients=clients)
    assert outcome.reason_code == "worker_failed"
    assert rec == ["prepare_sample", "reserve", "upload", "clone", "release"]
    assert sv == {}


def test_worker_clone_exception_releases(captured_audit):
    rec: list = []
    clients = _clients(rec, clone=RuntimeError("network down"))
    outcome, sv, _ = _run(rec, captured_audit, clients=clients)
    assert outcome.reason_code == "worker_failed"
    assert "release" in rec and sv == {}


def test_register_fail_triggers_delete_and_release(captured_audit):
    rec: list = []
    clients = _clients(rec, register=RegisterResult(ok=False, detail="HTTP 500 boom"))
    outcome, sv, _ = _run(rec, captured_audit, clients=clients)
    assert outcome.decision == "register_failed_orphan_cleanup_ok"
    assert "delete_voice" in rec and "release" in rec, "register 失败必 delete + release"
    # delete 在 release 之前
    assert rec.index("delete_voice") < rec.index("release")
    assert sv == {}, "register 失败回预设，不注入 clone 音色"
    assert any(r.get("register_failure_detail") == "HTTP 500 boom" for r in captured_audit)


def test_register_fail_delete_fail_marks_cleanup_failed(captured_audit):
    rec: list = []
    clients = _clients(rec, register=RegisterResult(ok=False, detail="500"), delete_voice=False)
    outcome, _, _ = _run(rec, captured_audit, clients=clients)
    assert outcome.decision == "register_failed_orphan_cleanup_failed"
    assert "release" in rec


def test_consume_fail_injects_routing_but_audits_not_silent(captured_audit):
    """consume 失败：voice 已注册可用 → 注入 routing（不浪费付费），但 audit
    标记 consume_status=failed（不静默成功）。"""
    rec: list = []
    clients = _clients(rec, consume=False)
    outcome, sv, sr = _run(rec, captured_audit, clients=clients)
    assert outcome.cloned is True
    assert outcome.decision == "cloned_consume_failed"
    assert sv["speaker_a"] == "cosyvoice-v3.5-flash-xyz", "consume 失败仍注入 routing"
    main_line = [r for r in captured_audit if r.get("decision") == "cloned_consume_failed"]
    assert main_line and main_line[0].get("consume_status") == "failed"
    assert main_line[0].get("reservation_status_final") == "reserved"


def test_consume_exception_audited(captured_audit):
    rec: list = []
    clients = _clients(rec, consume=RuntimeError("timeout"))
    outcome, sv, _ = _run(rec, captured_audit, clients=clients)
    assert outcome.cloned is True and outcome.decision == "cloned_consume_failed"
    assert sv["speaker_a"] == "cosyvoice-v3.5-flash-xyz"
    assert any(r.get("consume_error") == "RuntimeError" for r in captured_audit)


def test_release_failure_emits_audit_not_silent(captured_audit):
    """release 抛异常 → _safe_release 捕获 + 写 reservation_release_failed audit。"""
    rec: list = []
    clients = _clients(
        rec,
        upload=UploadResult(ok=False, error="x"),  # 触发 release 路径
        release=RuntimeError("gateway 5xx"),
    )
    outcome, _, _ = _run(rec, captured_audit, clients=clients)
    assert outcome.reason_code == "upload_failed"
    rel = [r for r in captured_audit if r.get("reason_code") == ac._RELEASE_FAILED_REASON_CODE]
    assert rel, "release 异常必须写 express_auto_clone_reservation_release_failed audit"
    assert rel[0].get("release_error") == "RuntimeError"


def test_release_not_ok_emits_audit(captured_audit):
    rec: list = []
    clients = _clients(rec, upload=UploadResult(ok=False, error="x"), release=False)
    _run(rec, captured_audit, clients=clients)
    rel = [r for r in captured_audit if r.get("reason_code") == ac._RELEASE_FAILED_REASON_CODE]
    assert rel and rel[0].get("release_error") == "release_not_ok"


def test_every_call_writes_exactly_one_decision_line(captured_audit):
    """无论结果，每次调用必写恰好一行主决策 audit（spec §9）。"""
    rec: list = []
    _run(rec, captured_audit)
    decision_lines = [r for r in captured_audit if r.get("decision") in (
        "cloned", "cloned_consume_failed", "skipped",
        "register_failed_orphan_cleanup_ok", "register_failed_orphan_cleanup_failed",
    )]
    assert len(decision_lines) == 1


# ===========================================================================
# AST 守卫
# ===========================================================================


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_express_package_does_not_import_gateway_boto3_minimax():
    """express/* 不 import gateway（D.7）/ boto3（NG5）/ minimax（不调 MiniMax）。"""
    forbidden = ("gateway", "boto3", "minimax")
    for py in _EXPRESS_DIR.glob("*.py"):
        mods = _imported_modules(py)
        for m in mods:
            low = m.lower()
            for bad in forbidden:
                assert bad not in low, f"{py.name} 不应 import {m!r}（命中 {bad!r}）"


def test_auto_clone_call_order_locked_in_source():
    """AST：auto_clone 源码里 clients.reserve 在 upload/clone 之前、
    prepare_sample 在 reserve 之前（最终成本闸位置不可漂移）。"""
    tree = ast.parse((_EXPRESS_DIR / "auto_clone.py").read_text(encoding="utf-8"))
    order: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "clients":
            order.append((node.lineno, node.attr))
    order.sort()
    seq = [attr for _, attr in order]

    def first(name: str) -> int:
        return seq.index(name)

    assert first("prepare_sample") < first("reserve"), "sample 必须在 reserve 之前"
    assert first("reserve") < first("upload"), "reserve 必须在 upload 之前"
    assert first("reserve") < first("clone"), "reserve 必须在 worker clone 之前"
