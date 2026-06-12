"""APF 匿名 Express T5 — Pass 3 按 lane + artifact 失败判定 + 诚实失败/配额退还.

plan docs/plans/2026-06-12-anonymous-express-preview-plan.md §E：

* ``_should_run_pass3`` 按 lane 分流：free 匿名跳过不变；express 匿名必跑。
* artifact 判定（``s2_pass3_result.json`` 缺失或 profiles 为空）是唯一可信
  信号；调用点 concurrent.futures 120s 超时；失败 → 任务 terminal failed
  （AnonymousExpressPass3Failed，绝不降级出片）。
* 配额退还：intake 时 LaneAwareCounterStore 记录可退的 per-scope per-mode
  行 → record audit ``quota_mode_rows`` → 终态镜像（mirror 单一入口）按
  ``[SMART_STATE] anon_pass3_failed`` marker 精确退还；global 总闸与
  express 子闸不退；幂等（audit 标记）。
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
_GATEWAY = str(_REPO / "gateway")
_SRC = str(_REPO / "src")
for _p in (_GATEWAY, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pipeline.process import (  # noqa: E402
    ANON_EXPRESS_PASS3_TIMEOUT_SECONDS,
    AnonymousExpressPass3Failed,
    _anon_express_pass3_artifact_ok,
    _should_run_pass3,
)


# ---------------------------------------------------------------------------
# A. _should_run_pass3 按 lane 分流
# ---------------------------------------------------------------------------


class TestShouldRunPass3:
    STYLES = {"speaker_a": {"gender": "male"}}

    def test_logged_in_jobs_unchanged(self):
        for mode in ("studio", "express", "smart", "free", ""):
            assert _should_run_pass3(self.STYLES, False, mode) is True

    def test_anonymous_free_skips(self):
        assert _should_run_pass3(self.STYLES, True, "free") is False

    def test_anonymous_express_runs(self):
        """D6 必选：express 匿名必须跑 Pass 3（CosyVoice 选音依赖 gender）。"""
        assert _should_run_pass3(self.STYLES, True, "express") is True

    def test_empty_styles_never_runs(self):
        assert _should_run_pass3({}, False, "studio") is False
        assert _should_run_pass3({}, True, "express") is False

    def test_legacy_two_arg_call_keeps_free_skip(self):
        """既有两参调用（service_mode 缺省）保持匿名跳过——向后兼容。"""
        assert _should_run_pass3(self.STYLES, True) is False
        assert _should_run_pass3(self.STYLES, False) is True


# ---------------------------------------------------------------------------
# B. artifact 判定（唯一可信信号）
# ---------------------------------------------------------------------------


class TestArtifactVerdict:
    def test_missing_file_false(self, tmp_path):
        assert _anon_express_pass3_artifact_ok(tmp_path / "missing.json") is False

    def test_empty_profiles_false(self, tmp_path):
        p = tmp_path / "s2_pass3_result.json"
        p.write_text(json.dumps({"speaker_profiles": {}}), encoding="utf-8")
        assert _anon_express_pass3_artifact_ok(p) is False

    def test_malformed_json_false(self, tmp_path):
        p = tmp_path / "s2_pass3_result.json"
        p.write_text("{not json", encoding="utf-8")
        assert _anon_express_pass3_artifact_ok(p) is False

    def test_profiles_wrong_type_false(self, tmp_path):
        p = tmp_path / "s2_pass3_result.json"
        p.write_text(json.dumps({"speaker_profiles": ["x"]}), encoding="utf-8")
        assert _anon_express_pass3_artifact_ok(p) is False

    def test_valid_profiles_true(self, tmp_path):
        p = tmp_path / "s2_pass3_result.json"
        p.write_text(
            json.dumps({"speaker_profiles": {"speaker_a": {"gender": "male"}}}),
            encoding="utf-8",
        )
        assert _anon_express_pass3_artifact_ok(p) is True

    def test_partial_speaker_coverage_false(self, tmp_path):
        """CodeX 第三轮 P2：多说话人任务的部分产物必须判失败——缺失的
        说话人无 gender 进音色匹配会回落错误默认音色。"""
        p = tmp_path / "s2_pass3_result.json"
        p.write_text(
            json.dumps({"speaker_profiles": {"speaker_a": {"gender": "male"}}}),
            encoding="utf-8",
        )
        assert _anon_express_pass3_artifact_ok(
            p, expected_speaker_ids=["speaker_a", "speaker_b"]
        ) is False

    def test_full_speaker_coverage_true(self, tmp_path):
        p = tmp_path / "s2_pass3_result.json"
        p.write_text(
            json.dumps({
                "speaker_profiles": {
                    "speaker_a": {"gender": "male"},
                    "speaker_b": {"gender": "female"},
                }
            }),
            encoding="utf-8",
        )
        assert _anon_express_pass3_artifact_ok(
            p, expected_speaker_ids=["speaker_a", "speaker_b"]
        ) is True

    def test_extra_profiles_beyond_expected_ok(self, tmp_path):
        p = tmp_path / "s2_pass3_result.json"
        p.write_text(
            json.dumps({
                "speaker_profiles": {
                    "speaker_a": {"gender": "male"},
                    "speaker_b": {"gender": "female"},
                }
            }),
            encoding="utf-8",
        )
        assert _anon_express_pass3_artifact_ok(
            p, expected_speaker_ids=["speaker_a"]
        ) is True


# ---------------------------------------------------------------------------
# C. 调用点源码钉子（run() 不可单测，钉关键结构）
# ---------------------------------------------------------------------------


class TestCallSiteSourceGuards:
    def _src(self) -> str:
        return (_REPO / "src" / "pipeline" / "process.py").read_text(
            encoding="utf-8"
        )

    def test_timeout_budget_is_120s(self):
        assert ANON_EXPRESS_PASS3_TIMEOUT_SECONDS == 120

    def test_daemon_thread_timeout_at_call_site(self):
        """CodeX 复审 P2：ThreadPoolExecutor 超时后非 daemon worker 停不
        下来且 atexit join 会拖住进程退出——必须 daemon 线程 + join(timeout)
        （超时放弃；进程退出时 daemon 线程随之终止，挂死的付费调用不延命）。"""
        src = self._src()
        assert "daemon=True" in src
        assert "join(timeout=ANON_EXPRESS_PASS3_TIMEOUT_SECONDS)" in src
        assert 'name="anon-express-pass3"' in src
        # 旧形状（futures 包裹 + shutdown）不得回潮
        assert "shutdown(wait=False, cancel_futures=True)" not in src

    def test_honest_failure_raises_and_emits_marker(self):
        src = self._src()
        assert 'emit_smart_state_marker({"anon_pass3_failed": True})' in src
        assert "raise AnonymousExpressPass3Failed(" in src
        # 判定走 artifact helper（带说话人覆盖校验），不信内部返回值
        compact = src.replace("\n", "").replace(" ", "")
        assert (
            "_anon_express_pass3_artifact_ok(_pass3_cache_path,expected_speaker_ids="
            in compact
        )

    def test_timeout_is_hard_failure_independent_of_artifact(self):
        """CodeX 第四轮 P2：超时本身就是失败条件——daemon 线程超时后可能
        在 artifact 判定前恰好写出产物，但 profiles 没 merge 回
        _review_speaker_styles，放行=错误音色预览。判定必须
        `timed_out OR not artifact_ok`。"""
        src = self._src()
        compact = src.replace("\n", "").replace(" ", "")
        assert "_anon_express_pass3_timed_out=True" in compact
        assert (
            "if_anon_express_pass3and(_anon_express_pass3_timed_out"
            "ornot_anon_express_pass3_artifact_ok(" in compact
        )

    def test_verdict_applies_even_when_pass3_not_attempted(self):
        """speaker styles 为空导致 Pass 3 根本没跑时，artifact 判定仍然
        生效（验收：express 匿名必产 s2_pass3_result.json，否则诚实失败）。
        AST 钉死：verdict if 语句在 _should_run_pass3 块之外（同级）。"""
        src = self._src()
        tree = ast.parse(src)
        # 找到 run 方法里的 verdict If 节点：test 为
        # `_anon_express_pass3 and not _anon_express_pass3_artifact_ok(...)`
        found_top_level_verdict = False
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test_dump = ast.dump(node.test)
                if (
                    "_anon_express_pass3" in test_dump
                    and "_anon_express_pass3_artifact_ok" in test_dump
                ):
                    found_top_level_verdict = True
        assert found_top_level_verdict


# ---------------------------------------------------------------------------
# D. LaneAwareCounterStore 可退行清单
# ---------------------------------------------------------------------------


class TestRefundableRowTracking:
    def _mem(self):
        import test_anonymous_express_t2_quota_layers as t2

        return t2._MemStore()

    def test_tracks_per_scope_rows_not_global_subgate(self):
        sys.path.insert(0, str(_REPO / "tests"))
        from anonymous_preview_intake_wiring import (
            LaneAwareCounterStore,
            express_subgate_key,
            mode_scope_key,
        )

        store = self._mem()
        w = LaneAwareCounterStore(store, store, lane="express", express_global_cap=50)
        w.try_acquire("global:2026-06-12", 500)
        w.try_acquire("ip:h:2026-06-12", 3)
        w.try_acquire("device:d:2026-06-12", 1)
        assert w.acquired_mode_scope_keys == [
            mode_scope_key("ip:h:2026-06-12", "express"),
            mode_scope_key("device:d:2026-06-12", "express"),
        ]
        assert express_subgate_key("2026-06-12") not in w.acquired_mode_scope_keys

    def test_decrement_removes_from_refund_list(self):
        sys.path.insert(0, str(_REPO / "tests"))
        from anonymous_preview_intake_wiring import LaneAwareCounterStore

        store = self._mem()
        w = LaneAwareCounterStore(store, store, lane="free", express_global_cap=0)
        w.try_acquire("ip:h:2026-06-12", 3)
        w.decrement("ip:h:2026-06-12")
        assert w.acquired_mode_scope_keys == []

    def test_wiring_persists_quota_mode_rows_into_audit_meta(self, monkeypatch):
        """fake adapter 经 wrapper 取额 → record 审计元数据带 quota_mode_rows。"""
        import anonymous_preview_intake_wiring as wiring
        from anonymous_preview_quota import shanghai_today
        from datetime import datetime, timezone
        from services.anonymous_preview_intake import (
            PreviewRecord,
            PreviewStatus,
            SourceType,
        )
        from datetime import timedelta

        base_record = PreviewRecord(
            record_id="prv_t5",
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

        captured: dict = {}

        class _FakeAdapter:
            def __init__(self, **kwargs):
                captured["counter_store"] = kwargs["counter_store"]

            def handle_intake(self, request_facts, upload_facts):
                # 模拟 adapter 的 _enforce_rate_limits 取额序列
                cs = captured["counter_store"]
                cs.try_acquire("global:DAY", 500)
                cs.try_acquire("ip:h:DAY", 3)
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
        monkeypatch.setattr(wiring, "resolve_express_global_cap", lambda: 50)

        mem = self._mem()
        wiring.run_intake_and_save(
            db_session=MagicMock(),
            request_facts=MagicMock(),
            upload_facts=None,
            counter_store_factory=lambda scope: mem,
            mode="express",
        )
        meta = saved[0].compliance_audit_metadata
        rows = meta["quota_mode_rows"]
        assert rows == [
            {
                "scope_key": "ip:h:DAY:mode:express",
                "mode": "express",
                "day": shanghai_today(),
            }
        ]


# ---------------------------------------------------------------------------
# E. 配额退还（mirror 单一终态入口）
# ---------------------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestQuotaRefund:
    def _record(self, audit=None):
        rec = MagicMock()
        rec.preview_id = "prv_t5"
        rec.mode = "express"
        rec.audit = audit if audit is not None else {
            "quota_mode_rows": [
                {"scope_key": "ip:h:D:mode:express", "mode": "express", "day": "2026-06-12"},
                {"scope_key": "device:d:D:mode:express", "mode": "express", "day": "2026-06-12"},
            ]
        }
        return rec

    def _db(self, record):
        db = MagicMock()
        calls: list = []

        async def _execute(stmt, params=None):
            calls.append((str(stmt), dict(params or {})))
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=record)
            return result

        db.execute = _execute
        db._calls = calls
        return db

    def _job(self, smart_state=None, job_id="job-t5"):
        return SimpleNamespace(
            job_id=job_id,
            smart_state=smart_state if smart_state is not None else {"anon_pass3_failed": True},
        )

    @pytest.fixture(autouse=True)
    def _logs_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))
        self._tmp = tmp_path

    def test_refund_decrements_each_row_and_marks_audit(self):
        from anonymous_preview_quota_refund import refund_pass3_failed_quota

        record = self._record()
        db = self._db(record)
        changed = _run(refund_pass3_failed_quota(db, self._job()))
        assert changed is True
        # 第一条是 record select；其后两条 UPDATE 带正确参数
        updates = [c for c in db._calls if "UPDATE" in c[0].upper()]
        assert len(updates) == 2
        assert updates[0][1]["key"] == "ip:h:D:mode:express"
        assert updates[0][1]["mode"] == "express"
        assert updates[0][1]["day"] == "2026-06-12"
        assert record.audit["pass3_quota_refund"] == "done"
        assert record.audit["pass3_failed"] is True
        # 审计 JSONL 落盘（mode + voice_profile_missing —— plan §G）
        lines = (self._tmp / "anonymous_preview_audit.jsonl").read_text(
            encoding="utf-8"
        ).strip().splitlines()
        row = json.loads(lines[-1])
        assert row["kind"] == "anon_pass3_quota_refund"
        assert row["mode"] == "express"
        assert row["voice_profile_missing"] is True

    def test_refund_idempotent(self):
        from anonymous_preview_quota_refund import refund_pass3_failed_quota

        record = self._record(
            audit={"pass3_quota_refund": "done", "quota_mode_rows": [
                {"scope_key": "ip:h:D:mode:express", "mode": "express", "day": "2026-06-12"}
            ]}
        )
        db = self._db(record)
        changed = _run(refund_pass3_failed_quota(db, self._job()))
        assert changed is False
        assert [c for c in db._calls if "UPDATE" in c[0].upper()] == []

    def test_no_marker_no_refund(self):
        from anonymous_preview_quota_refund import refund_pass3_failed_quota

        record = self._record()
        db = self._db(record)
        changed = _run(refund_pass3_failed_quota(db, self._job(smart_state={})))
        assert changed is False
        assert db._calls == []

    def test_no_keys_marks_skipped(self):
        from anonymous_preview_quota_refund import refund_pass3_failed_quota

        record = self._record(audit={})
        db = self._db(record)
        changed = _run(refund_pass3_failed_quota(db, self._job()))
        assert changed is True
        assert record.audit["pass3_quota_refund"] == "skipped_no_keys"

    def test_mirror_anonymous_branch_wires_refund(self):
        """终态结算单一入口守卫：mirror 匿名分支在 failed 时调退还。"""
        src = (_REPO / "gateway" / "job_terminal_mirror.py").read_text(
            encoding="utf-8"
        )
        assert "refund_pass3_failed_quota" in src
        assert 'upstream_status == "failed"' in src
