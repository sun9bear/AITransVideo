"""APF 匿名 Express T4 — create/PG mirror payload 一致性 + 防 clone 双守卫.

plan docs/plans/2026-06-12-anonymous-express-preview-plan.md §D/§E：

* create payload 与 PG Job 行按 record.mode 同步写：express →
  service_mode="express" + tts_provider=admin express_tts_provider 白名单
  解析值（mimo 拒绝）；free 路径逐字节不变。sentinel /
  plan_code_snapshot / role_snapshot 语义不变。
* create 幂等扩展：仅 job 诚实失败终态（failed/cancelled/已清理）可重入
  （复用 preview_id 不重新上传），原子抢占条件改 job_id == 旧 failed id，
  audit 记 retry_chain。
* 防 clone 钉死：anonymous+express 组合 voice_strategy 恒 preset_mapping
  （AST 守卫 + payload 集成断言 + pipeline 第三道防线源码钉子）。

复用 test_anonymous_preview_t8_create 的打桩件（直接调端点函数）。
"""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
_GATEWAY = str(_REPO / "gateway")
_SRC = str(_REPO / "src")
_TESTS = str(Path(__file__).resolve().parent)
for _p in (_GATEWAY, _SRC, _TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_anonymous_preview_t8_create as t8  # noqa: E402

api = t8.api
probe_mod = t8.probe_mod


def _admin_stub(**overrides):
    base = dict(
        anonymous_preview_max_in_flight=2,
        express_tts_provider="cosyvoice",
        anonymous_express_enabled=True,
        anonymous_free_preview_enabled=False,
    )
    base.update(overrides)
    return types.SimpleNamespace(load_settings=lambda: SimpleNamespace(**base))


@pytest.fixture()
def wired_express(monkeypatch, tmp_path):
    """t8.wired 的 express 变体：record.mode=express + express lane 开。"""
    record = t8._record(tmp_path, mode="express")
    _models_mod = sys.modules.get("models")
    if _models_mod is None:
        _models_mod = types.ModuleType("models")
        monkeypatch.setitem(sys.modules, "models", _models_mod)
    monkeypatch.setattr(_models_mod, "Job", t8._FakeJobModel, raising=False)
    monkeypatch.setattr(_models_mod, "User", t8._FakeUserModel, raising=False)
    monkeypatch.setattr(api, "AnonymousPreviewRecord", t8._FakeRecordModel)
    monkeypatch.setattr(api, "require_same_origin_state_change", lambda r: None)
    monkeypatch.setattr(
        api, "require_anonymous_session", AsyncMock(return_value=t8._ctx())
    )
    monkeypatch.setattr(
        api, "_get_record_for_session", AsyncMock(side_effect=[record, record])
    )
    # express record 不依赖 free 双门——free env/admin 全关也要能 create
    monkeypatch.setattr(api.settings, "enable_free_tier", False, raising=False)
    monkeypatch.setattr(api, "_get_admin_enabled", lambda: False)
    monkeypatch.setattr(
        probe_mod,
        "probe_source",
        lambda p: {"ok": True, "duration_seconds": 170.0, "has_audio": True,
                   "container_format": "mp4", "failure_reason": None},
    )
    monkeypatch.setitem(sys.modules, "admin_settings", _admin_stub())
    monkeypatch.setitem(
        sys.modules,
        "quota",
        types.SimpleNamespace(
            TERMINAL_STATUSES=frozenset({"succeeded", "failed", "cancelled", "purged"})
        ),
    )
    job_api_resp = MagicMock(status_code=202)
    job_api_resp.json.return_value = {"job_id": "job-exp-1"}
    client = MagicMock()
    client.post = AsyncMock(return_value=job_api_resp)
    monkeypatch.setattr(api, "get_client", lambda: client)
    reset_spy = AsyncMock()
    monkeypatch.setattr(api, "_reset_create_claim", reset_spy)
    return {
        "record": record,
        "client": client,
        "reset": reset_spy,
        "monkeypatch": monkeypatch,
        "tmp_path": tmp_path,
    }


def _call(db):
    return t8._run(api.anonymous_preview_create("p1", t8._request(), db))


# ---------------------------------------------------------------------------
# A. express create：payload / PG 行 / record 三处 mode 一致
# ---------------------------------------------------------------------------


class TestExpressPayloadConsistency:
    def test_express_happy_path_four_way_mode_consistency(self, wired_express):
        from anonymous_preview_payload_spec import validate_create_payload

        db = t8._db()
        resp = _call(db)
        assert resp.status_code == 202

        _, kwargs = wired_express["client"].post.call_args
        payload = kwargs["json"]
        # ① record.mode（fixture 注入 express）→ ② payload
        assert payload["service_mode"] == "express"
        assert payload["tts_provider"] == "cosyvoice"
        assert payload["anonymous_preview"] is True
        # 防 clone 第一道防线：恒 preset_mapping + 白名单零违规
        assert payload["voice_strategy"] == "preset_mapping"
        assert validate_create_payload(payload) == []
        assert "voice_clone" not in payload
        assert "voiceclone_reference_path" not in payload

        # ③ PG Job 行与 payload 同值；sentinel/plan/role 快照语义不变
        job_row = db.add.call_args[0][0]
        assert job_row.service_mode == "express"
        assert job_row.tts_provider == "cosyvoice"
        assert job_row.is_anonymous_preview is True
        assert job_row.voice_clone_enabled is False
        assert job_row.voice_strategy == "preset_mapping"
        assert job_row.plan_code_snapshot == "free"
        assert job_row.role_snapshot == "user"

        # ④ record 回写真实 job_id
        assert wired_express["record"].job_id == "job-exp-1"

    def test_express_provider_volcengine_threads_through(self, wired_express):
        wired_express["monkeypatch"].setitem(
            sys.modules, "admin_settings", _admin_stub(express_tts_provider="volcengine")
        )
        db = t8._db()
        resp = _call(db)
        assert resp.status_code == 202
        _, kwargs = wired_express["client"].post.call_args
        assert kwargs["json"]["tts_provider"] == "volcengine"

    def test_express_mimo_provider_rejected_503(self, wired_express):
        """create 侧第三层 mimo 拒绝：mode gate 被绕过（直接钉 None）也
        不放行——payload provider 解析白名单刻意剔除 mimo。"""
        wired_express["monkeypatch"].setattr(api, "_create_mode_gate", lambda m: None)
        wired_express["monkeypatch"].setitem(
            sys.modules, "admin_settings", _admin_stub(express_tts_provider="mimo")
        )
        db = t8._db()
        resp = _call(db)
        assert resp.status_code == 503
        wired_express["client"].post.assert_not_awaited()

    def test_express_unknown_provider_rejected_503(self, wired_express):
        wired_express["monkeypatch"].setattr(api, "_create_mode_gate", lambda m: None)
        wired_express["monkeypatch"].setitem(
            sys.modules, "admin_settings", _admin_stub(express_tts_provider="minimax")
        )
        db = t8._db()
        resp = _call(db)
        assert resp.status_code == 503
        wired_express["client"].post.assert_not_awaited()

    def test_resolver_unit_matrix(self, monkeypatch):
        for provider, expected in [
            ("cosyvoice", "cosyvoice"),
            ("VolcEngine", "volcengine"),
            ("mimo", None),
            ("minimax", None),
            ("", None),
        ]:
            monkeypatch.setitem(
                sys.modules, "admin_settings",
                _admin_stub(express_tts_provider=provider),
            )
            assert api._resolve_express_payload_tts_provider() == expected

    def test_resolver_admin_read_failure_fail_closed(self, monkeypatch):
        monkeypatch.setitem(
            sys.modules,
            "admin_settings",
            types.SimpleNamespace(
                load_settings=lambda: (_ for _ in ()).throw(RuntimeError())
            ),
        )
        assert api._resolve_express_payload_tts_provider() is None


# ---------------------------------------------------------------------------
# B. 重试重入（§E：仅诚实失败终态可重入，复用 preview_id）
# ---------------------------------------------------------------------------


def _job_status_client(status_code=200, status="failed"):
    client = MagicMock()
    get_resp = MagicMock(status_code=status_code)
    get_resp.json.return_value = {"status": status}
    client.get = AsyncMock(return_value=get_resp)
    post_resp = MagicMock(status_code=202)
    post_resp.json.return_value = {"job_id": "job-exp-2"}
    client.post = AsyncMock(return_value=post_resp)
    return client


class TestFailedRetryReentry:
    def test_failed_job_allows_recreate_and_records_chain(self, wired_express):
        wired_express["record"].job_id = "job-exp-failed"
        client = _job_status_client(status="failed")
        wired_express["monkeypatch"].setattr(api, "get_client", lambda: client)
        db = t8._db()
        resp = _call(db)
        assert resp.status_code == 202
        assert wired_express["record"].job_id == "job-exp-2"
        assert wired_express["record"].audit["retry_chain"] == ["job-exp-failed"]

    def test_cleaned_up_job_404_allows_recreate(self, wired_express):
        wired_express["record"].job_id = "job-gone"
        client = _job_status_client(status_code=404)
        wired_express["monkeypatch"].setattr(api, "get_client", lambda: client)
        resp = _call(t8._db())
        assert resp.status_code == 202

    def test_running_job_blocks_recreate(self, wired_express):
        wired_express["record"].job_id = "job-running"
        client = _job_status_client(status="running")
        wired_express["monkeypatch"].setattr(api, "get_client", lambda: client)
        resp = _call(t8._db())
        assert resp.status_code == 409
        client.post.assert_not_awaited()

    def test_succeeded_job_blocks_recreate(self, wired_express):
        wired_express["record"].job_id = "job-done"
        client = _job_status_client(status="succeeded")
        wired_express["monkeypatch"].setattr(api, "get_client", lambda: client)
        resp = _call(t8._db())
        assert resp.status_code == 409

    def test_status_check_error_fail_closed_409(self, wired_express):
        wired_express["record"].job_id = "job-x"
        client = MagicMock()
        client.get = AsyncMock(side_effect=RuntimeError("upstream down"))
        wired_express["monkeypatch"].setattr(api, "get_client", lambda: client)
        resp = _call(t8._db())
        assert resp.status_code == 409

    def test_creating_sentinel_blocks_recreate(self, wired_express):
        wired_express["record"].job_id = api._CREATING_SENTINEL
        resp = _call(t8._db())
        assert resp.status_code == 409

    def test_concurrent_retry_claim_race_409(self, wired_express):
        """并发双重试：原子抢占（job_id == 旧 failed id）只有一个赢。"""
        wired_express["record"].job_id = "job-exp-failed"
        client = _job_status_client(status="failed")
        wired_express["monkeypatch"].setattr(api, "get_client", lambda: client)
        resp = _call(t8._db(claim_rows=0))
        assert resp.status_code == 409
        client.post.assert_not_awaited()


# ---------------------------------------------------------------------------
# C. 防 clone 守卫（AST + pipeline 第三道防线源码钉子）
# ---------------------------------------------------------------------------


class TestNoCloneGuards:
    def test_api_voice_strategy_literal_is_always_preset_mapping(self):
        """anonymous_preview_api.py 中任何 voice_strategy 赋值/键值必须是
        字面量 "preset_mapping"——杜绝未来给匿名 lane 接 clone 策略。"""
        src = (Path(_GATEWAY) / "anonymous_preview_api.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        found = 0
        for node in ast.walk(tree):
            # dict 字面量 {"voice_strategy": <v>}
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if (
                        isinstance(k, ast.Constant)
                        and k.value == "voice_strategy"
                    ):
                        found += 1
                        assert (
                            isinstance(v, ast.Constant)
                            and v.value == "preset_mapping"
                        ), f"voice_strategy 字面量必须是 preset_mapping，行 {v.lineno}"
            # 关键字实参 voice_strategy=<v>
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "voice_strategy":
                        found += 1
                        assert (
                            isinstance(kw.value, ast.Constant)
                            and kw.value.value == "preset_mapping"
                        ), f"voice_strategy 关键字必须是 preset_mapping，行 {kw.value.lineno}"
        assert found >= 2, "payload 与 PG 行两处 voice_strategy 都应存在"

    def test_api_has_no_clone_imports(self):
        """import 黑名单（防 clone 第三道防线的 gateway 侧）：匿名预览
        模块不得 import 任何 clone provider / user voice 模块。"""
        banned = ("voice_clone", "user_voice", "minimax", "clone")
        for fname in ("anonymous_preview_api.py", "anonymous_preview_chunked_api.py"):
            tree = ast.parse(
                (Path(_GATEWAY) / fname).read_text(encoding="utf-8")
            )
            for node in ast.walk(tree):
                mods: list[str] = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    mods = [node.module or ""]
                for m in mods:
                    assert not any(b in m.lower() for b in banned), (
                        f"{fname} 不得 import clone 相关模块: {m}"
                    )

    def test_pipeline_third_defense_pinned(self):
        """process.py 防 clone 第三道防线：匿名任务 voice_strategy 强制
        preset_mapping，且判断不挂 service_mode（anonymous+express 同样
        生效）。源码钉子——改动该行必须同步评审本守卫。"""
        src = (_REPO / "src" / "pipeline" / "process.py").read_text(
            encoding="utf-8"
        )
        assert (
            "if job_anonymous_preview and job_voice_strategy != 'preset_mapping':"
            in src
        )

    def test_express_payload_never_carries_forbidden_fields(self):
        from anonymous_preview_payload_spec import (
            FORBIDDEN_PAYLOAD_FIELDS,
            ANONYMOUS_PREVIEW_PAYLOAD_SPEC,
        )

        assert "voice_clone" in FORBIDDEN_PAYLOAD_FIELDS
        assert "voiceclone_reference_path" in FORBIDDEN_PAYLOAD_FIELDS
        assert "tts_provider" in ANONYMOUS_PREVIEW_PAYLOAD_SPEC
        assert "service_mode" in ANONYMOUS_PREVIEW_PAYLOAD_SPEC
