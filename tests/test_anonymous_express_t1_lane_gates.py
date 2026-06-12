"""APF 匿名 Express T1 — lane resolver + gate 拓扑重构 + mode 落盘守卫.

plan docs/plans/2026-06-12-anonymous-express-preview-plan.md §A：

* resolve_anonymous_lane：express 优先 / free 回落 / 全关 None / 读失败
  fail-closed None / mimo 防御纵深（§E②）。
* Master gate 只 gate 新 intake（session 创建、upload、chunked init、create）；
  生命周期端点（status/stream、chunked status/delete）对 lane 开关零感知
  （R2 #4：切开关不得杀旧 record）。
* lane 锁定：普通上传 = intake 时写 record.mode；chunked = init 时写
  upload state，part/complete 读 state 不重新 resolve。
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
_GATEWAY = str(_REPO / "gateway")
_SRC = str(_REPO / "src")
for _p in (_GATEWAY, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import anonymous_lane  # noqa: E402
import anonymous_preview_api as api  # noqa: E402
import anonymous_session as anon_session_mod  # noqa: E402
from anonymous_lane import resolve_anonymous_lane  # noqa: E402


def _adm(express: bool = False, free: bool = False, provider: str = "cosyvoice"):
    return SimpleNamespace(
        anonymous_express_enabled=express,
        anonymous_free_preview_enabled=free,
        express_tts_provider=provider,
    )


# ---------------------------------------------------------------------------
# A. resolve_anonymous_lane 矩阵
# ---------------------------------------------------------------------------


class TestResolveAnonymousLane:
    def test_express_priority_over_free(self):
        assert resolve_anonymous_lane(_adm(express=True, free=True)) == "express"

    def test_express_only(self):
        assert resolve_anonymous_lane(_adm(express=True, free=False)) == "express"

    def test_free_fallback(self):
        assert resolve_anonymous_lane(_adm(express=False, free=True)) == "free"

    def test_both_off_returns_none(self):
        assert resolve_anonymous_lane(_adm(express=False, free=False)) is None

    def test_admin_read_failure_fail_closed(self, monkeypatch):
        import admin_settings as adm_mod

        def _boom():
            raise RuntimeError("settings unavailable")

        monkeypatch.setattr(adm_mod, "load_settings", _boom)
        assert resolve_anonymous_lane() is None

    def test_mimo_defense_falls_back_to_free(self):
        """§E② runtime 防御纵深：手改 JSON 出现 express+mimo 组合 →
        拒绝 express lane，回落 free。"""
        assert (
            resolve_anonymous_lane(_adm(express=True, free=True, provider="mimo"))
            == "free"
        )

    def test_mimo_defense_falls_back_to_none(self):
        assert (
            resolve_anonymous_lane(_adm(express=True, free=False, provider="mimo"))
            is None
        )

    def test_mimo_defense_case_insensitive(self):
        assert (
            resolve_anonymous_lane(_adm(express=True, free=False, provider=" MiMo "))
            is None
        )

    def test_unsupported_provider_rejected(self):
        """CodeX 第三轮 P2：lane 开关与 create 共用 provider 白名单——
        minimax / 未知值同样不开 express（否则上传锁进 express 而
        /create 拒绝，preview 永远无法 create 还烧配额）。"""
        assert (
            resolve_anonymous_lane(_adm(express=True, free=True, provider="minimax"))
            == "free"
        )
        assert (
            resolve_anonymous_lane(_adm(express=True, free=False, provider="minimax"))
            is None
        )
        assert (
            resolve_anonymous_lane(_adm(express=True, free=False, provider=""))
            is None
        )

    def test_whitelist_single_source_with_create(self):
        """白名单唯一真源：lane resolver 与 create payload 解析共用同一
        frozenset（防两侧漂移再次产生"lane 开 create 拒"组合）。"""
        import anonymous_preview_api as api_mod

        assert (
            api_mod._VALID_ANON_EXPRESS_TTS_PROVIDERS
            is anonymous_lane.VALID_ANON_EXPRESS_TTS_PROVIDERS
        )
        assert anonymous_lane.VALID_ANON_EXPRESS_TTS_PROVIDERS == frozenset(
            {"cosyvoice", "volcengine"}
        )


# ---------------------------------------------------------------------------
# B. session 层 gate：intake 走任一 lane 开；lifecycle 零感知
# ---------------------------------------------------------------------------


class TestSessionGates:
    def test_admin_flag_true_when_any_lane_open(self, monkeypatch):
        """_get_admin_flag（intake gate）= 任一 lane 开启。free 关 express 开
        仍放行——这是 free 下线后 express 漏斗可用的根。"""
        monkeypatch.setattr(
            anonymous_lane,
            "_load_admin_settings",
            lambda: _adm(express=True, free=False),
        )
        assert anon_session_mod._get_admin_flag() is True

    def test_admin_flag_false_when_both_off(self, monkeypatch):
        monkeypatch.setattr(
            anonymous_lane,
            "_load_admin_settings",
            lambda: _adm(express=False, free=False),
        )
        assert anon_session_mod._get_admin_flag() is False

    @pytest.mark.asyncio
    async def test_require_session_is_lane_agnostic(self, monkeypatch):
        """生命周期端点（status/stream/chunked status/delete 共用
        require_anonymous_session）不查任何 lane 开关：admin 读取直接
        抛错也照样放行（仅 env + session 本身）——R2 #4 回滚语义。"""
        monkeypatch.setattr(
            anon_session_mod.settings, "enable_anonymous_preview", True,
            raising=False,
        )
        monkeypatch.setattr(
            anon_session_mod.settings, "anonymous_preview_hash_secret", "x" * 32,
            raising=False,
        )
        # admin 全坏也不影响 lifecycle
        import admin_settings as adm_mod

        def _boom():
            raise RuntimeError("admin settings unavailable")

        monkeypatch.setattr(adm_mod, "load_settings", _boom)

        row = MagicMock()
        row.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        async def _fake_lookup(db, session_id_hash):
            return row

        monkeypatch.setattr(anon_session_mod, "_lookup_session", _fake_lookup)
        fake_req = MagicMock()
        fake_req.cookies = {"avt_anon": "tok"}
        result = await anon_session_mod.require_anonymous_session(
            fake_req, AsyncMock()
        )
        assert isinstance(result, anon_session_mod.AnonymousSessionContext)

    @pytest.mark.asyncio
    async def test_require_session_env_flag_off_404(self, monkeypatch):
        monkeypatch.setattr(
            anon_session_mod.settings, "enable_anonymous_preview", False,
            raising=False,
        )
        result = await anon_session_mod.require_anonymous_session(
            MagicMock(), AsyncMock()
        )
        from fastapi.responses import JSONResponse

        assert isinstance(result, JSONResponse)
        assert result.status_code == 404


# ---------------------------------------------------------------------------
# C. upload 端点：admin_enabled = lane 解析非 None（free 关 express 开可用）
# ---------------------------------------------------------------------------


class TestUploadLaneSemantics:
    @pytest.mark.asyncio
    async def test_upload_passes_admin_enabled_when_express_only(self, monkeypatch):
        from anonymous_preview_upload import UploadRejected

        monkeypatch.setattr(api.settings, "enable_anonymous_preview", True, raising=False)
        monkeypatch.setattr(api, "_resolve_active_lane", lambda: "express")
        # free admin flag 关（不再被 upload 路径消费）
        monkeypatch.setattr(api, "_get_admin_enabled", lambda: False)
        monkeypatch.setattr(api, "require_same_origin_state_change", lambda r: None)

        ctx = anon_session_mod.AnonymousSessionContext(
            session_id_hash="h" * 64, raw_token=None, is_new=False
        )

        async def _fake_session(request, response, db):
            return ctx

        monkeypatch.setattr(api, "get_or_create_anonymous_session", _fake_session)
        monkeypatch.setattr(
            api, "resolve_apf_limits",
            lambda: SimpleNamespace(
                anonymous_preview_max_upload_bytes=200 * 1024 * 1024,
                anonymous_preview_max_seconds=180,
                anonymous_preview_cap_global_per_day=500,
                anonymous_preview_cap_per_ip=3,
            ),
        )

        async def _noop_peek(db, request, limits, lane="free"):
            return None

        monkeypatch.setattr(api, "ad8_peek_precheck", _noop_peek)

        recorded: dict = {}

        async def _recording_upload(**kwargs):
            recorded.update(kwargs)
            raise UploadRejected(404, "short_circuit", "test short circuit")

        monkeypatch.setattr(api, "handle_anonymous_upload", _recording_upload)

        resp = await api.anonymous_upload(MagicMock(), MagicMock(), AsyncMock())
        assert resp.status_code == 404
        # 核心断言：free 关、express 开 → admin_enabled=True（lane 非 None）
        assert recorded["admin_enabled"] is True

    @pytest.mark.asyncio
    async def test_upload_admin_disabled_when_no_lane(self, monkeypatch):
        from anonymous_preview_upload import UploadRejected

        monkeypatch.setattr(api.settings, "enable_anonymous_preview", True, raising=False)
        monkeypatch.setattr(api, "_resolve_active_lane", lambda: None)
        monkeypatch.setattr(api, "require_same_origin_state_change", lambda r: None)
        ctx = anon_session_mod.AnonymousSessionContext(
            session_id_hash="h" * 64, raw_token=None, is_new=False
        )

        async def _fake_session(request, response, db):
            return ctx

        monkeypatch.setattr(api, "get_or_create_anonymous_session", _fake_session)
        monkeypatch.setattr(
            api, "resolve_apf_limits",
            lambda: SimpleNamespace(
                anonymous_preview_max_upload_bytes=200 * 1024 * 1024,
                anonymous_preview_max_seconds=180,
                anonymous_preview_cap_global_per_day=500,
                anonymous_preview_cap_per_ip=3,
            ),
        )

        async def _noop_peek(db, request, limits, lane="free"):
            return None

        monkeypatch.setattr(api, "ad8_peek_precheck", _noop_peek)
        recorded: dict = {}

        async def _recording_upload(**kwargs):
            recorded.update(kwargs)
            raise UploadRejected(404, "flag_disabled", "disabled")

        monkeypatch.setattr(api, "handle_anonymous_upload", _recording_upload)
        resp = await api.anonymous_upload(MagicMock(), MagicMock(), AsyncMock())
        assert resp.status_code == 404
        assert recorded["admin_enabled"] is False


# ---------------------------------------------------------------------------
# D. record store / 契约 record 的 mode 落盘
# ---------------------------------------------------------------------------


class TestModePersistence:
    def _record(self, mode: Optional[str] = None):
        from services.anonymous_preview_intake import (
            PreviewRecord,
            PreviewStatus,
            SourceType,
        )

        kwargs = dict(
            record_id="prv_t1_test",
            session_id_hash="s" * 64,
            source_hash="a" * 64,
            upload_hash="a" * 64,
            source_type=SourceType.LOCAL_UPLOAD,
            status=PreviewStatus.READY_FOR_MODE,
            status_reason="",
            duration_seconds=10.0,
            audio_present=True,
            compliance_status=None,
            compliance_audit_metadata={},
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        if mode is not None:
            kwargs["mode"] = mode
        return PreviewRecord(**kwargs)

    def test_preview_record_mode_defaults_to_free(self):
        assert self._record().mode == "free"

    def test_to_orm_threads_record_mode(self):
        from anonymous_preview_record_store import _to_orm

        orm = _to_orm(self._record(mode="express"))
        assert orm.mode == "express"

    def test_to_orm_default_mode_free(self):
        from anonymous_preview_record_store import _to_orm

        assert _to_orm(self._record()).mode == "free"

    def test_run_intake_and_save_threads_mode(self, monkeypatch):
        """wiring mode 入参 → 持久化 record.mode（plan §A record/intake 改动点）。"""
        import anonymous_preview_intake_wiring as wiring

        base_record = self._record()

        class _FakeAdapter:
            def __init__(self, **kwargs):
                pass

            def handle_intake(self, request_facts, upload_facts):
                return base_record

        saved: list = []

        class _FakeStore:
            def __init__(self, session):
                pass

            def save_record(self, record):
                saved.append(record)

        monkeypatch.setattr(wiring, "AnonymousPreviewBackendAdapter", _FakeAdapter)
        monkeypatch.setattr(wiring, "PgPreviewRecordStore", _FakeStore)
        monkeypatch.setattr(
            wiring, "build_intake_config", lambda upload_root=None: MagicMock()
        )

        result = wiring.run_intake_and_save(
            db_session=MagicMock(),
            request_facts=MagicMock(),
            upload_facts=None,
            counter_store_factory=lambda scope: MagicMock(),
            mode="express",
        )
        assert result.mode == "express"
        assert saved and saved[0].mode == "express"

    def test_run_intake_and_save_default_mode_free(self, monkeypatch):
        import anonymous_preview_intake_wiring as wiring

        base_record = self._record()

        class _FakeAdapter:
            def __init__(self, **kwargs):
                pass

            def handle_intake(self, request_facts, upload_facts):
                return base_record

        saved: list = []

        class _FakeStore:
            def __init__(self, session):
                pass

            def save_record(self, record):
                saved.append(record)

        monkeypatch.setattr(wiring, "AnonymousPreviewBackendAdapter", _FakeAdapter)
        monkeypatch.setattr(wiring, "PgPreviewRecordStore", _FakeStore)
        monkeypatch.setattr(
            wiring, "build_intake_config", lambda upload_root=None: MagicMock()
        )
        result = wiring.run_intake_and_save(
            db_session=MagicMock(),
            request_facts=MagicMock(),
            upload_facts=None,
            counter_store_factory=lambda scope: MagicMock(),
        )
        assert result.mode == "free"
        assert saved[0].mode == "free"


# ---------------------------------------------------------------------------
# E. create 双门按 record.mode 分流
# ---------------------------------------------------------------------------


class TestCreateModeGate:
    def test_free_mode_keeps_legacy_double_gate(self, monkeypatch):
        """free record：enable_free_tier env + free admin flag 两门不变。"""
        monkeypatch.setattr(api.settings, "enable_free_tier", True, raising=False)
        monkeypatch.setattr(api, "_get_admin_enabled", lambda: True)
        assert api._create_mode_gate("free") is None

    def test_free_mode_env_off_403(self, monkeypatch):
        monkeypatch.setattr(api.settings, "enable_free_tier", False, raising=False)
        monkeypatch.setattr(api, "_get_admin_enabled", lambda: True)
        resp = api._create_mode_gate("free")
        assert resp is not None and resp.status_code == 403

    def test_free_mode_admin_off_403(self, monkeypatch):
        monkeypatch.setattr(api.settings, "enable_free_tier", True, raising=False)
        monkeypatch.setattr(api, "_get_admin_enabled", lambda: False)
        resp = api._create_mode_gate("free")
        assert resp is not None and resp.status_code == 403

    def test_express_mode_gate_open(self, monkeypatch):
        monkeypatch.setattr(
            anonymous_lane,
            "_load_admin_settings",
            lambda: _adm(express=True, free=False),
        )
        # free 门全关也不影响 express record
        monkeypatch.setattr(api.settings, "enable_free_tier", False, raising=False)
        monkeypatch.setattr(api, "_get_admin_enabled", lambda: False)
        assert api._create_mode_gate("express") is None

    def test_express_mode_switch_off_403(self, monkeypatch):
        monkeypatch.setattr(
            anonymous_lane,
            "_load_admin_settings",
            lambda: _adm(express=False, free=True),
        )
        resp = api._create_mode_gate("express")
        assert resp is not None and resp.status_code == 403

    def test_express_mode_mimo_defense_403(self, monkeypatch):
        """§E②：express record + 运行时 provider=mimo（手改 JSON）→ 拒绝。"""
        monkeypatch.setattr(
            anonymous_lane,
            "_load_admin_settings",
            lambda: _adm(express=True, free=False, provider="mimo"),
        )
        resp = api._create_mode_gate("express")
        assert resp is not None and resp.status_code == 403

    def test_unknown_mode_fail_closed_403(self):
        resp = api._create_mode_gate("smart")
        assert resp is not None and resp.status_code == 403


# ---------------------------------------------------------------------------
# F. /limits 增返 active_lane / master_open（§G）
# ---------------------------------------------------------------------------


class TestLimitsLaneFields:
    @pytest.mark.asyncio
    async def test_limits_returns_active_lane_and_master_open(self, monkeypatch):
        import json

        monkeypatch.setattr(api.settings, "enable_anonymous_preview", True, raising=False)
        monkeypatch.setattr(api, "_resolve_active_lane", lambda: "express")
        monkeypatch.setattr(
            api, "resolve_apf_limits",
            lambda: SimpleNamespace(
                anonymous_preview_max_upload_bytes=200 * 1024 * 1024,
                anonymous_preview_max_seconds=180,
            ),
        )
        resp = await api.anonymous_preview_limits()
        body = json.loads(resp.body)
        assert body["active_lane"] == "express"
        assert body["master_open"] is True

    @pytest.mark.asyncio
    async def test_limits_both_lanes_off(self, monkeypatch):
        import json

        monkeypatch.setattr(api.settings, "enable_anonymous_preview", True, raising=False)
        monkeypatch.setattr(api, "_resolve_active_lane", lambda: None)
        monkeypatch.setattr(
            api, "resolve_apf_limits",
            lambda: SimpleNamespace(
                anonymous_preview_max_upload_bytes=200 * 1024 * 1024,
                anonymous_preview_max_seconds=180,
            ),
        )
        resp = await api.anonymous_preview_limits()
        body = json.loads(resp.body)
        assert body["active_lane"] is None
        assert body["master_open"] is False


# ---------------------------------------------------------------------------
# G. chunked store：lane 随 state 持久化（init 锁 lane）
# ---------------------------------------------------------------------------


class TestChunkedLaneLock:
    def _limits(self):
        from chunked_upload_store import ChunkedLimits

        return ChunkedLimits(
            enabled=True, max_file_mb=200, chunk_mb=64, per_user_active=2,
            per_user_inflight_gb=1, global_inflight_gb=20, daily_per_user_gb=5,
            disk_floor_gb=0, ttl_hours=6, ready_ttl_hours=6,
        )

    def test_init_upload_persists_lane(self, tmp_path, monkeypatch):
        import chunked_upload_store as store

        monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
        monkeypatch.delenv("AIVIDEOTRANS_PROJECT_ROOT", raising=False)
        state = store.init_upload(
            user_id="anon:t1lane",
            declared_size=8,
            declared_sha256="a" * 64,
            chunk_size=8,
            file_name="v.mp4",
            limits=self._limits(),
            owner_scope=store.OWNER_SCOPE_ANONYMOUS,
            lane="express",
        )
        assert state["lane"] == "express"
        # 落盘后的 state 也带 lane（part/complete 读 state 不重新 resolve）
        loaded = store.load_state("anon:t1lane", state["upload_id"])
        assert loaded is not None and loaded["lane"] == "express"

    def test_resume_keeps_original_lane(self, tmp_path, monkeypatch):
        """续传命中既有 receiving upload：lane 维持 init 时锁定值，
        不被第二次 init 的新 lane 覆盖。"""
        import chunked_upload_store as store

        monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
        monkeypatch.delenv("AIVIDEOTRANS_PROJECT_ROOT", raising=False)
        first = store.init_upload(
            user_id="anon:t1resume",
            declared_size=8,
            declared_sha256="b" * 64,
            chunk_size=8,
            file_name="v.mp4",
            limits=self._limits(),
            owner_scope=store.OWNER_SCOPE_ANONYMOUS,
            lane="express",
        )
        second = store.init_upload(
            user_id="anon:t1resume",
            declared_size=8,
            declared_sha256="b" * 64,
            chunk_size=8,
            file_name="v.mp4",
            limits=self._limits(),
            owner_scope=store.OWNER_SCOPE_ANONYMOUS,
            lane="free",
        )
        assert second["resumed"] is True
        assert second["upload_id"] == first["upload_id"]
        assert second["lane"] == "express"


# ---------------------------------------------------------------------------
# H. 源码级守卫：生命周期端点不挂 lane gate
# ---------------------------------------------------------------------------


def _func_calls(tree: ast.AST, func_name: str) -> set[str]:
    """Return the set of names called inside the named async/sync function."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            calls: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    if isinstance(f, ast.Name):
                        calls.add(f.id)
                    elif isinstance(f, ast.Attribute):
                        calls.add(f.attr)
            return calls
    raise AssertionError(f"function {func_name} not found")


class TestLifecycleSourceGuards:
    def test_stream_handler_no_admin_lane_recheck(self):
        """stream 是生命周期端点：不得调用 _get_admin_enabled /
        _resolve_active_lane（R2 #4——切 lane 开关不得杀旧 record stream）。"""
        src = (Path(_GATEWAY) / "anonymous_preview_api.py").read_text(encoding="utf-8")
        calls = _func_calls(ast.parse(src), "anonymous_preview_stream")
        assert "_get_admin_enabled" not in calls
        assert "_resolve_active_lane" not in calls

    def test_status_handler_no_admin_lane_recheck(self):
        src = (Path(_GATEWAY) / "anonymous_preview_api.py").read_text(encoding="utf-8")
        calls = _func_calls(ast.parse(src), "anonymous_preview_status")
        assert "_get_admin_enabled" not in calls
        assert "_resolve_active_lane" not in calls

    def test_chunked_part_complete_no_gate_recheck(self):
        """chunked part/complete 读 state 的 lane：不得重新 resolve、
        不得再查 three_gates_open（plan §A R3 验收）。"""
        src = (Path(_GATEWAY) / "anonymous_preview_chunked_api.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        for fn in ("anon_chunked_part", "anon_chunked_complete",
                   "anon_chunked_status", "anon_chunked_abort"):
            calls = _func_calls(tree, fn)
            assert "three_gates_open" not in calls, f"{fn} 不得查 three_gates_open"
            assert "_resolve_active_lane" not in calls, f"{fn} 不得重新 resolve lane"

    def test_chunked_init_locks_lane(self):
        """init 必须 resolve lane 并传入 store.init_upload（锁进 state）。"""
        src = (Path(_GATEWAY) / "anonymous_preview_chunked_api.py").read_text(
            encoding="utf-8"
        )
        calls = _func_calls(ast.parse(src), "anon_chunked_init")
        assert "_resolve_active_lane" in calls
