"""P3e-4a — 智能版免费预览 lane 准入放行 + enter-edit 泄漏闸（钱/安全，默认 inert）.

plan 2026-06-14-p3e2-preview-lane-design.md §7。两件事：

1. **免费用户 smart 预览 entitlement 放行**（gateway create-path）：免费/未获 smart
   entitlement 的登录用户，**仅当**本次为显式预览请求（preview_mode is True）且 admin
   canary 旗 smart_preview_clone_enabled is True 时，才放行进入受限智能版预览 lane
   （3min 水印 teaser、只扣 600 克隆、跳分钟、stream-only —— 全由下游 smart_preview_mode
   服务端强制）。其余 smart 请求一律仍走原 entitlement 403。判定抽在
   `gateway/smart_preview_gate.py::smart_preview_lane_exempt`（纯函数，便于单测）。

2. **enter-edit gate 挡 smart 预览**（src/services/jobs/editing.py + gateway 防御层）：
   smart 预览任务只产 3min teaser（P3e-3b）+ stream-only（P3e-3d）。进入 editing 会经
   editor/segments.json + copy_as_new 暴露完整段落 / TTS draft / 触发完整长度重渲染——
   彻底击穿"只看不交付"契约。`enter_editing` 在 smart_state.smart_preview_mode is True
   时无条件 EditingConflictError；gateway `_enforce_post_edit_access` 同档防御 403。

钱-不变量：
- 默认 inert：smart_preview_clone_enabled 默认 False → 放行恒 False → 字节级不变。
- fail-closed：admin_settings 不可读 → 放行 False（绝不因读配置异常误放行付费克隆）。
- preview_mode=True 换不到完整 smart 产物：下游 smart_preview_mode 强制 teaser/水印/
  跳分钟/stream-only；放行只让免费用户进**受限**预览，不是完整付费 smart。

helper 直接单测（monkeypatch admin_settings.load_settings，逐用例自动还原，不替换
sys.modules）+ gate 接线 source-scan（不 import 重的 job_intercept 避 database-stub 污染，
见 memory feedback_test_database_stub_convention）+ enter_editing 真行为测试。
"""
from __future__ import annotations

import ast
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# config.Settings 在 import admin_settings 时实例化，需要 ≥16 字符的内部 key。
os.environ.setdefault("AVT_INTERNAL_API_KEY", "test_key_at_least_16_chars_long_xxxx")

_REPO = Path(__file__).resolve().parents[1]
_JI = _REPO / "gateway" / "job_intercept.py"

# gateway + src on path；stub ``database`` 使 import 不建真引擎（setdefault，绝不替换
# 已存在的真模块对象，见 memory feedback_test_database_stub_convention）。
for _p in (str(_REPO / "gateway"), str(_REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from smart_preview_gate import smart_preview_lane_exempt  # noqa: E402
import admin_settings  # noqa: E402
import config  # noqa: E402  (gateway config singleton — env smart kill switch)


def _ast_func_src(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _set_gates(monkeypatch, *, preview_clone=True, smart_mode=True, env_smart=True) -> None:
    """同时设置 admin（smart_preview_clone_enabled + smart_mode_enabled）与 env
    （enable_smart_mode）。默认三者皆开（happy path）；各测试按需关单项验证收窄。"""
    monkeypatch.setattr(
        admin_settings,
        "load_settings",
        lambda: SimpleNamespace(
            smart_preview_clone_enabled=preview_clone,
            smart_mode_enabled=smart_mode,
        ),
        raising=True,
    )
    monkeypatch.setattr(config.settings, "enable_smart_mode", env_smart, raising=False)


def _preview_req() -> dict:
    """合法的克隆预览请求体（preview_mode is True + auto_voice_clone is True）。"""
    return {"preview_mode": True, "smart_consent": {"auto_voice_clone": True}}


def _user(uid="u1"):
    return SimpleNamespace(id=uid)


def _build_editing_record(*, job_id="job_pv", status="succeeded",
                          service_mode="smart", project_dir=None, smart_state=None):
    """Minimal JobRecord-like for src/services/jobs/editing.py::enter_editing
    (reads status / service_mode / project_dir / smart_state)."""
    if project_dir is None:
        project_dir = "/fake/projects/job_pv"
    return SimpleNamespace(
        job_id=job_id, status=status, service_mode=service_mode,
        project_dir=project_dir, smart_state=smart_state,
    )


# ===================================================================
# 1. smart_preview_lane_exempt — 放行判定纯函数
# ===================================================================


class TestSmartPreviewLaneExempt:
    def test_false_when_user_none(self, monkeypatch):
        """未登录无 owner 落不了 600 reservation → 不放行（即便全旗开）。"""
        _set_gates(monkeypatch)
        assert smart_preview_lane_exempt(_preview_req(), None) is False

    def test_false_when_request_data_not_dict(self, monkeypatch):
        _set_gates(monkeypatch)
        assert smart_preview_lane_exempt(None, _user()) is False
        assert smart_preview_lane_exempt("preview", _user()) is False

    def test_false_when_preview_mode_absent(self, monkeypatch):
        """非预览 smart 请求一律不放行 → 仍走原 entitlement 403。"""
        _set_gates(monkeypatch)
        assert smart_preview_lane_exempt({"smart_consent": {"auto_voice_clone": True}}, _user()) is False

    def test_false_when_preview_mode_not_strictly_true(self, monkeypatch):
        """严格 is True：truthy 字符串 / 1 / "yes" 都不算 —— 防止客户端用
        非布尔真值蒙混进受限 lane 又被下游 ``is True`` 当成非预览处理。"""
        _set_gates(monkeypatch)
        for val in ("true", "True", 1, "1", "yes", False, None, [True]):
            req = {"preview_mode": val, "smart_consent": {"auto_voice_clone": True}}
            assert smart_preview_lane_exempt(req, _user()) is False, val

    def test_false_when_auto_voice_clone_not_true(self, monkeypatch):
        """对抗性 P1 回归：preview_mode=True 但 auto_voice_clone≠True 一律不放行——
        否则免费用户跳过 600-reserve、不被 stamp 成预览 → 拿到不受限完整 smart 成片。"""
        _set_gates(monkeypatch)
        # 缺 smart_consent / 非 dict
        assert smart_preview_lane_exempt({"preview_mode": True}, _user()) is False
        assert smart_preview_lane_exempt(
            {"preview_mode": True, "smart_consent": "x"}, _user()) is False
        assert smart_preview_lane_exempt(
            {"preview_mode": True, "smart_consent": {}}, _user()) is False
        # auto_voice_clone False / 缺 / 非布尔真值
        for val in (False, None, "true", 1, 0):
            req = {"preview_mode": True, "smart_consent": {"auto_voice_clone": val}}
            assert smart_preview_lane_exempt(req, _user()) is False, val

    def test_false_when_preview_clone_flag_off_default_inert(self, monkeypatch):
        """smart_preview_clone_enabled 默认 False → 放行恒 False（默认 inert）。"""
        _set_gates(monkeypatch, preview_clone=False)
        assert smart_preview_lane_exempt(_preview_req(), _user()) is False

    def test_false_when_general_smart_kill_switch_off_admin(self, monkeypatch):
        """对抗性 HIGH：通用 smart 紧急停（admin smart_mode_enabled=False）必须同时停
        预览 lane（同一管线 + 同一付费克隆 API）。"""
        _set_gates(monkeypatch, smart_mode=False)
        assert smart_preview_lane_exempt(_preview_req(), _user()) is False

    def test_false_when_general_smart_kill_switch_off_env(self, monkeypatch):
        """env AVT_ENABLE_SMART_MODE 关 → 预览也停（两层 kill switch 与 entitlements 同源）。"""
        _set_gates(monkeypatch, env_smart=False)
        assert smart_preview_lane_exempt(_preview_req(), _user()) is False

    def test_true_when_all_conditions_met(self, monkeypatch):
        """全部条件成立（登录 + preview_mode + auto_voice_clone + 通用停开 + 本旗开）
        → 放行 True。"""
        _set_gates(monkeypatch)
        assert smart_preview_lane_exempt(_preview_req(), _user()) is True

    def test_fail_closed_when_admin_unreadable(self, monkeypatch):
        """admin_settings 读取抛异常 → fail-closed 不放行。"""
        def _boom():
            raise RuntimeError("admin store unreadable")
        monkeypatch.setattr(admin_settings, "load_settings", _boom, raising=True)
        monkeypatch.setattr(config.settings, "enable_smart_mode", True, raising=False)
        assert smart_preview_lane_exempt(_preview_req(), _user()) is False

    def test_returns_strict_bool_not_truthy_object(self, monkeypatch):
        """返回真正的 bool（is True / is False），不是 truthy/falsy 对象。"""
        _set_gates(monkeypatch)
        assert smart_preview_lane_exempt(_preview_req(), _user()) is True
        _set_gates(monkeypatch, preview_clone=False)
        assert smart_preview_lane_exempt(_preview_req(), _user()) is False


# ===================================================================
# 2. create-path 两道 entitlement gate 接线（source-scan）
# ===================================================================


class TestEntitlementGateWiring:
    def _src(self) -> str:
        s = _ast_func_src(_JI, "intercept_create_job")
        assert s, "intercept_create_job 未找到"
        return s

    def test_exemption_computed_once_scoped_to_smart(self):
        """放行判定单次计算（_smart_preview_exempt），且赋值处显式限定
        service_mode == "smart"，避免误放行 studio 等其它未授权 mode + 避免两道
        gate 重复读 admin_settings。"""
        src = self._src()
        assert "smart_preview_lane_exempt" in src, "未引用 smart_preview_lane_exempt"
        idx = src.find("_smart_preview_exempt = ")
        assert idx >= 0, "未找到 _smart_preview_exempt 单次赋值"
        window = src[idx:idx + 220]
        assert "smart_preview_lane_exempt" in window, "赋值处未调用放行判定"
        assert 'service_mode == "smart"' in window, (
            "_smart_preview_exempt 计算未限定 service_mode == 'smart'。"
        )

    def test_gate_a_smart_disabled_guarded_by_exemption(self):
        """Gate A（smart kill switch，403 smart_disabled）必须先问放行判定，
        否则免费预览用户在 Gate A 就被 403、永远进不了受限 lane。"""
        src = self._src()
        idx = src.find('"smart_disabled"')
        assert idx >= 0, "smart_disabled 403 分支未找到"
        window = src[max(0, idx - 400):idx]
        assert "_smart_preview_exempt" in window, (
            "Gate A 的 smart_disabled 403 未被 _smart_preview_exempt 守卫——"
            "免费预览放行会在 Gate A 被吞。"
        )

    def test_gate_b_service_mode_not_allowed_guarded_by_exemption(self):
        """Gate B（plan gate，403 service_mode_not_allowed）同样必须问放行判定。"""
        src = self._src()
        idx = src.find('"service_mode_not_allowed"')
        assert idx >= 0, "service_mode_not_allowed 403 分支未找到"
        window = src[max(0, idx - 400):idx]
        assert "_smart_preview_exempt" in window, (
            "Gate B 的 service_mode_not_allowed 403 未被 _smart_preview_exempt 守卫。"
        )

    def test_reserve_failure_rejects_free_preview_not_full_job(self):
        """对抗性 P1：免费预览 exemption 下若 600 预留未成功（余额不足/denied/重放），
        必须显式拒绝（smart_preview_reserve_failed），**不得**继续落成按分钟计费的完整
        smart 任务（免费白嫖完整成片）。"""
        src = self._src()
        assert '"smart_preview_reserve_failed"' in src, (
            "缺少 600 预留失败的免费预览拒绝分支。"
        )
        idx = src.find('"smart_preview_reserve_failed"')
        window = src[max(0, idx - 300):idx]
        assert "_smart_preview_via_exemption" in window, (
            "拒绝分支须以 _smart_preview_via_exemption 为前提（只拒未获 smart 的免费用户，"
            "不阻断 entitled 用户降级）。"
        )
        assert "not _smart_clone_reservation_id" in window, (
            "拒绝分支未检查 600 预留是否成功。"
        )

    def test_reserve_block_revalidates_general_kill_switch_for_exemption(self):
        """CodeX P2：免费 exemption 用户在 reserve 处用 fresh admin+env 重核通用 smart 停
        （防 gate→reserve 之间被翻关的 TOCTOU）。"""
        src = self._src()
        beg = src.find("_smart_clone_reservation_id: str | None = None")
        fwd = src.find("upstream_response = await proxy_request(")
        assert beg >= 0 and fwd > beg, "未定位 reserve 区"
        reserve_region = src[beg:fwd]
        assert "_smart_preview_via_exemption" in reserve_region, (
            "reserve 区未对 exemption 用户做通用停重核。"
        )
        assert "smart_mode_enabled" in reserve_region and "enable_smart_mode" in reserve_region, (
            "reserve 区通用停重核须覆盖 admin smart_mode_enabled + env enable_smart_mode。"
        )


# ===================================================================
# 3. enter-edit gate 挡 smart 预览（editing.py 真行为）
# ===================================================================


class TestEnterEditPreviewGuard:
    def test_smart_preview_job_rejected_from_editing(self, tmp_path):
        """smart_state.smart_preview_mode is True → enter_editing 无条件拒绝
        （即便 status=completed 通过 is_editable_smart_state）。"""
        from services.jobs.editing import EditingConflictError, enter_editing

        record = _build_editing_record(
            service_mode="smart",
            project_dir=str(tmp_path),
            smart_state={"status": "completed", "smart_preview_mode": True},
        )
        # Match a guard-specific token ("stream-only"); NOT "preview" —
        # pytest's tmp_path embeds the test name (which contains "preview"),
        # so a downstream segments-error path would false-match "preview".
        with pytest.raises(EditingConflictError, match="stream-only"):
            enter_editing(record, store=MagicMock())

    def test_smart_non_preview_completed_not_hit_by_preview_guard(self, tmp_path):
        """非预览 smart（无 smart_preview_mode）不应被预览闸拦——下游可能因
        segments baseline 缺失抛别的错，但绝不是 preview 拒绝。"""
        from services.jobs.editing import EditingConflictError, enter_editing

        record = _build_editing_record(
            service_mode="smart",
            project_dir=str(tmp_path),
            smart_state={"status": "completed"},
        )
        try:
            enter_editing(record, store=MagicMock())
        except EditingConflictError as exc:
            # 用 guard 专属 token "stream-only"（非 "preview"，见上 tmp_path 说明）。
            assert "stream-only" not in str(exc).lower(), (
                f"非预览 smart 任务被预览闸误拦: {exc}"
            )
        except Exception:
            pass  # 下游 I/O 错误不在本测试范围

    def test_smart_preview_mode_false_not_hit_by_preview_guard(self, tmp_path):
        """smart_preview_mode 显式 False → 不算预览，不应被预览闸拦。"""
        from services.jobs.editing import EditingConflictError, enter_editing

        record = _build_editing_record(
            service_mode="smart",
            project_dir=str(tmp_path),
            smart_state={"status": "completed", "smart_preview_mode": False},
        )
        try:
            enter_editing(record, store=MagicMock())
        except EditingConflictError as exc:
            assert "stream-only" not in str(exc).lower()
        except Exception:
            pass


# ===================================================================
# 4. gateway _enforce_post_edit_access 防御层（source-scan）
# ===================================================================


class TestGatewayPostEditPreviewGuard:
    def test_enforce_post_edit_access_blocks_smart_preview(self):
        """gateway 侧 enter-edit / commit 共用的 _enforce_post_edit_access 必须
        在 FOR-UPDATE 层同档防御：smart 预览任务读 smart_preview_flag → 403。"""
        src = _ast_func_src(_JI, "_enforce_post_edit_access")
        assert src, "_enforce_post_edit_access 未找到"
        assert "extract_smart_preview_flag" in src, (
            "_enforce_post_edit_access 未读 smart_preview_flag——gateway 层缺防御。"
        )
        assert "403" in src, "smart 预览防御应为 403 拒绝。"


# ===================================================================
# 5. 共享判定 is_editable_smart_state preview-aware（封剪映 draft / runner 泄漏）
# ===================================================================


class TestIsEditableSmartStatePreviewAware:
    """CodeX P1：剪映 draft 生成 gate（src/services/jobs/api.py）与 JianyingDraftRunner
    都经 is_editable_smart_state 判定，旧实现只看 status=completed、忽略 smart_preview_mode
    → smart 预览任务能生成含完整段落文本/音频的剪映 zip。在共享判定单点 preview-aware
    一次封死 enter_editing / 剪映 gate / runner 三处消费者。"""

    def test_preview_completed_not_editable(self):
        from services.smart.state import is_editable_smart_state
        assert is_editable_smart_state(
            {"status": "completed", "smart_preview_mode": True}) is False

    def test_preview_downgraded_not_editable(self):
        from services.smart.state import is_editable_smart_state
        assert is_editable_smart_state(
            {"status": "downgraded_to_studio", "smart_preview_mode": True}) is False

    def test_non_preview_completed_still_editable(self):
        """回归：非预览 completed smart 任务仍可编辑（不误伤正常 Studio 移交）。"""
        from services.smart.state import is_editable_smart_state
        assert is_editable_smart_state({"status": "completed"}) is True
        assert is_editable_smart_state(
            {"status": "completed", "smart_preview_mode": False}) is True
        assert is_editable_smart_state({"status": "downgraded_to_studio"}) is True
