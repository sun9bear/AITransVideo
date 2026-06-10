"""T6 验收测试：gateway/anonymous_preview_policy.py 薄 adapter 守卫。

测试分四组：
1. mode 四分支 decision 矩阵 — 经薄层透传后与直接调契约逐字段相等（C5 核心守卫）。
2. teaser 时长边界 — >max 拒绝；边界值正确透传。
3. AST 守卫 — 本模块无独立决策常量（数字字面量 / 决策分支）、无禁止 import。
4. artifact_policy → StreamGate 翻译完整性 — 每个锁定字段有对应且值相等。
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.services.anonymous_preview_admission import (
    AdmissionDecision,
    AnonymousPreviewAdmissionConfig,
    AnonymousPreviewArtifactPolicy,
    VoiceStrategy,
    evaluate_anonymous_preview_admission,
)

# ── import under test ───────────────────────────────────────────────────────
from gateway.anonymous_preview_policy import (
    FreePreviewAdmissionResult,
    StreamGate,
    admit_for_free_preview,
    stream_gate_from_artifact_policy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(max_seconds: int = 180) -> MagicMock:
    s = MagicMock()
    s.anonymous_preview_max_seconds = max_seconds
    return s


def _contract_result(mode: str, duration: float, max_seconds: int = 180):
    """Call contract directly — used as the reference for parity assertions."""
    cfg = AnonymousPreviewAdmissionConfig(
        max_preview_duration_seconds=float(max_seconds),
        anonymous_express_cosyvoice_clone_enabled=False,
    )
    return evaluate_anonymous_preview_admission(
        config=cfg,
        mode=mode,
        source_duration_seconds=duration,
    )


# ---------------------------------------------------------------------------
# Group 1: mode 四分支 decision 矩阵 + 逐字段等于契约（C5 守卫）
# ---------------------------------------------------------------------------

class TestModeFourBranchDecisionMatrix:
    """经薄层 admit_for_free_preview 的 mode="free" 路径 + 对比契约四分支。

    admit_for_free_preview 强制 mode="free"，所以本函数不测试
    express/smart/studio 的路由——那些分支经由 stream_gate_from_artifact_policy
    + 对比直调契约 evaluate_anonymous_preview_admission 的 mode 参数来覆盖。
    """

    def test_free_admitted_decision_equals_contract(self):
        """Free 档 duration≤180 → admitted，薄层返回值与契约完全相同。"""
        duration = 120.0
        via_adapter = admit_for_free_preview(duration, _make_settings(180))
        direct = _contract_result("free", duration, 180)

        assert via_adapter.decision == direct.decision
        assert via_adapter.decision == AdmissionDecision.ADMITTED

    def test_free_admitted_duration_passes_through(self):
        duration = 120.0
        via_adapter = admit_for_free_preview(duration, _make_settings(180))
        direct = _contract_result("free", duration, 180)
        assert via_adapter.preview_duration_seconds == direct.preview_duration_seconds

    def test_free_admitted_voice_strategy_passes_through(self):
        via_adapter = admit_for_free_preview(90.0, _make_settings(180))
        direct = _contract_result("free", 90.0, 180)
        assert via_adapter.voice_strategy == direct.voice_strategy
        assert via_adapter.voice_strategy == VoiceStrategy.PRESET_ONLY

    def test_free_admitted_artifact_policy_passes_through(self):
        via_adapter = admit_for_free_preview(60.0, _make_settings(180))
        direct = _contract_result("free", 60.0, 180)
        assert via_adapter.artifact_policy == direct.artifact_policy

    def test_free_admitted_reason_passes_through(self):
        via_adapter = admit_for_free_preview(60.0, _make_settings(180))
        direct = _contract_result("free", 60.0, 180)
        assert via_adapter.reason == direct.reason

    # ── contract 四分支对比（直接调契约，验证薄层未改写 decision 语义）──

    def test_contract_express_admitted(self):
        """Express 模式 → admitted（契约直调，不经薄层——薄层只开 free 档）。"""
        direct = _contract_result("express", 60.0)
        assert direct.decision == AdmissionDecision.ADMITTED

    def test_contract_smart_login_required(self):
        direct = _contract_result("smart", 60.0)
        assert direct.decision == AdmissionDecision.LOGIN_REQUIRED

    def test_contract_studio_not_anonymous_funnel(self):
        direct = _contract_result("studio", 60.0)
        assert direct.decision == AdmissionDecision.NOT_ANONYMOUS_FUNNEL

    def test_all_four_decision_fields_equal_contract(self):
        """逐字段相等守卫：FreePreviewAdmissionResult 五字段与契约全部对齐。"""
        duration = 100.0
        via = admit_for_free_preview(duration, _make_settings(180))
        direct = _contract_result("free", duration, 180)

        assert via.decision == direct.decision
        assert via.preview_duration_seconds == direct.preview_duration_seconds
        assert via.voice_strategy == direct.voice_strategy
        assert via.artifact_policy == direct.artifact_policy
        assert via.reason == direct.reason


# ---------------------------------------------------------------------------
# Group 2: teaser 时长边界
# ---------------------------------------------------------------------------

class TestTeaserDurationBoundary:

    def test_duration_zero_admitted(self):
        via = admit_for_free_preview(0.0, _make_settings(180))
        assert via.decision == AdmissionDecision.ADMITTED
        assert via.preview_duration_seconds == 0.0

    def test_duration_exactly_max_admitted(self):
        via = admit_for_free_preview(180.0, _make_settings(180))
        assert via.decision == AdmissionDecision.ADMITTED
        assert via.preview_duration_seconds == 180.0

    def test_duration_below_max_passes_through(self):
        via = admit_for_free_preview(179.0, _make_settings(180))
        direct = _contract_result("free", 179.0, 180)
        assert via.preview_duration_seconds == direct.preview_duration_seconds

    def test_duration_above_max_capped_not_rejected(self):
        """契约行为：超出 max 时 duration 被 cap 为 max，decision 仍 ADMITTED。"""
        via = admit_for_free_preview(181.0, _make_settings(180))
        direct = _contract_result("free", 181.0, 180)
        assert via.decision == direct.decision
        assert via.preview_duration_seconds == direct.preview_duration_seconds
        assert via.preview_duration_seconds == 180.0

    def test_duration_180_04_capped(self):
        """方案 AD-1：180.04s teaser → cap 到 180，不拒绝。"""
        via = admit_for_free_preview(180.04, _make_settings(180))
        assert via.decision == AdmissionDecision.ADMITTED
        assert via.preview_duration_seconds == pytest.approx(180.0)

    def test_duration_nan_fail_closed(self):
        via = admit_for_free_preview(float("nan"), _make_settings(180))
        assert via.decision == AdmissionDecision.FAILED

    def test_duration_inf_fail_closed(self):
        via = admit_for_free_preview(float("inf"), _make_settings(180))
        assert via.decision == AdmissionDecision.FAILED

    def test_duration_negative_fail_closed(self):
        via = admit_for_free_preview(-1.0, _make_settings(180))
        assert via.decision == AdmissionDecision.FAILED

    def test_duration_bool_fail_closed(self):
        via = admit_for_free_preview(True, _make_settings(180))
        assert via.decision == AdmissionDecision.FAILED

    def test_settings_max_seconds_used_verbatim(self):
        """settings.anonymous_preview_max_seconds 透传到 config，不硬编码 180。"""
        via_90 = admit_for_free_preview(85.0, _make_settings(90))
        via_300 = admit_for_free_preview(85.0, _make_settings(300))
        assert via_90.preview_duration_seconds == pytest.approx(85.0)
        assert via_300.preview_duration_seconds == pytest.approx(85.0)

    def test_settings_max_seconds_caps_correctly(self):
        via = admit_for_free_preview(200.0, _make_settings(90))
        assert via.decision == AdmissionDecision.ADMITTED
        assert via.preview_duration_seconds == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# Group 3: AST 守卫
# ---------------------------------------------------------------------------

def _get_policy_module_source() -> str:
    import gateway.anonymous_preview_policy as _mod
    return inspect.getsource(_mod)


def _parse_policy_ast() -> ast.Module:
    return ast.parse(_get_policy_module_source())


class TestASTGuards:
    """本模块源码结构守卫（C5：薄 adapter 禁止新增决策规则）。"""

    def test_no_numeric_literal_decision_constants(self):
        """模块内不得出现独立数字字面量决策常量（数字 180 等不能在本模块出现）。

        允许的唯一数字：settings 字段引用和 False/0 等布尔/零值不计。
        """
        tree = _parse_policy_ast()
        numeric_literals: list[int | float] = []
        for node in ast.walk(tree):
            # ast.Constant covers int, float, complex, str, bytes, None, bool
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                # bool is subclass of int — skip True/False
                if isinstance(node.value, bool):
                    continue
                # 0 / 0.0 are allowed (e.g., empty list, slice, range)
                if node.value == 0:
                    continue
                numeric_literals.append(node.value)
        assert numeric_literals == [], (
            f"anonymous_preview_policy.py 内出现数字字面量决策常量 {numeric_literals}；"
            "所有数值必须来自 settings 字段引用，不得在薄 adapter 内写死（C5）。"
        )

    def test_no_import_minimax_or_cosyvoice(self):
        """模块的 import 语句不得引入 TTS/clone provider 模块。

        检查 AST Import/ImportFrom 节点的 module 名，而非全文字符串，
        避免误判配置字段名（如 anonymous_express_cosyvoice_clone_enabled）。
        """
        tree = _parse_policy_ast()
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.append(alias.name.lower())
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.append(node.module.lower())

        forbidden_module_fragments = [
            "minimax",
            "voice_clone",
            "tts_generator",
            "tts_provider",
            "volcengine",
            "mimo_tts",
            "assemblyai",
            # cosyvoice as a provider module (not config field name)
            "cosyvoice_voice",
            "cosyvoice_provider",
            "cosyvoice_tts",
        ]
        for fragment in forbidden_module_fragments:
            for mod in imported_modules:
                assert fragment not in mod, (
                    f"anonymous_preview_policy.py 不得 import provider 模块 '{mod}'（含 '{fragment}'，C5 import 黑名单）。"
                )

    def test_no_import_services_jobs(self):
        """模块不得 import services.jobs（pydub 传染，AD-3 约束）。"""
        src = _get_policy_module_source()
        assert "services.jobs" not in src, (
            "anonymous_preview_policy.py 不得 import services.jobs（pydub 传染）。"
        )

    def test_no_import_gateway_job_intercept(self):
        """模块不得 import gateway.job_intercept（避免循环依赖和职责扩散）。"""
        src = _get_policy_module_source()
        assert "job_intercept" not in src

    def test_no_independent_decision_if_branches(self):
        """模块内不得出现与契约枚举无关的独立 if-decision 分支。

        允许的 if：isinstance 检查、not policy.allow_* 属性透传、None 检查。
        禁止的：直接判断 mode=="free" 然后 return 不同 decision 值。
        """
        tree = _parse_policy_ast()
        violations: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            # 检查是否是对字符串字面量 "free"/"express"/"smart"/"studio" 的比较
            test = node.test
            if isinstance(test, ast.Compare):
                for comparator in test.comparators:
                    if isinstance(comparator, ast.Constant) and comparator.value in (
                        "free", "express", "smart", "studio",
                        "admitted", "login_required", "not_anonymous_funnel",
                        "rejected", "failed",
                    ):
                        violations.append(ast.dump(node.test))
        assert violations == [], (
            f"anonymous_preview_policy.py 含独立决策字符串比较 {violations}；"
            "所有 mode/decision 判断必须在契约模块内（C5）。"
        )

    def test_module_docstring_mentions_c5(self):
        """模块 docstring 需声明薄 adapter 约束（C5）。"""
        import gateway.anonymous_preview_policy as _mod
        doc = _mod.__doc__ or ""
        assert "C5" in doc or "薄 adapter" in doc or "thin adapter" in doc.lower(), (
            "模块 docstring 需显式声明薄 adapter 约束（方案 C5）。"
        )


# ---------------------------------------------------------------------------
# Group 4: artifact_policy → StreamGate 翻译完整性
# ---------------------------------------------------------------------------

class TestArtifactPolicyToStreamGate:
    """stream_gate_from_artifact_policy 逐字段与契约等值守卫。"""

    def _default_policy(self) -> AnonymousPreviewArtifactPolicy:
        return AnonymousPreviewArtifactPolicy()

    def test_stream_only_required_passes_through(self):
        gate = stream_gate_from_artifact_policy(self._default_policy())
        assert gate.stream_only_required == self._default_policy().stream_only_required
        assert gate.stream_only_required is True

    def test_watermark_required_passes_through(self):
        gate = stream_gate_from_artifact_policy(self._default_policy())
        assert gate.watermark_required == self._default_policy().watermark_required
        assert gate.watermark_required is True

    def test_artifact_ttl_required_passes_through(self):
        gate = stream_gate_from_artifact_policy(self._default_policy())
        assert gate.artifact_ttl_required == self._default_policy().artifact_ttl_required
        assert gate.artifact_ttl_required is True

    def test_low_priority_required_passes_through(self):
        gate = stream_gate_from_artifact_policy(self._default_policy())
        assert gate.low_priority_required == self._default_policy().low_priority_required
        assert gate.low_priority_required is True

    def test_download_forbidden_keys_covers_all_false_allow_fields(self):
        """默认 policy 七个 allow_* 字段全 False → 七个 key 全在禁止集。"""
        policy = self._default_policy()
        gate = stream_gate_from_artifact_policy(policy)
        expected_forbidden = {
            "download_url",
            "subtitle_export",
            "jianying_draft_export",
            "provider_voice_id",
            "clone_artifact",
            "payment_fields",
            "editable_assets",
        }
        assert expected_forbidden.issubset(gate.download_forbidden_keys), (
            f"StreamGate.download_forbidden_keys 缺少字段：{expected_forbidden - gate.download_forbidden_keys}"
        )

    def test_allow_download_url_true_removes_from_forbidden(self):
        """allow_download_url=True 时 download_url 不在禁止集。"""
        from dataclasses import replace
        policy = replace(self._default_policy(), allow_download_url=True)
        gate = stream_gate_from_artifact_policy(policy)
        assert "download_url" not in gate.download_forbidden_keys

    def test_all_allow_true_yields_empty_forbidden(self):
        """所有 allow_* 全 True → download_forbidden_keys 为空。"""
        from dataclasses import replace
        policy = replace(
            self._default_policy(),
            allow_download_url=True,
            allow_subtitle_export=True,
            allow_jianying_draft_export=True,
            allow_payment_fields=True,
            allow_provider_voice_id=True,
            allow_clone_artifact=True,
            allow_editable_assets=True,
        )
        gate = stream_gate_from_artifact_policy(policy)
        assert len(gate.download_forbidden_keys) == 0

    def test_gate_from_admission_artifact_policy_equals_direct(self):
        """经薄层 admit_for_free_preview 取到的 artifact_policy 再经 stream_gate
        翻译后，与直调契约取到的 policy 翻译结果相同（端到端 C5 守卫）。"""
        via = admit_for_free_preview(60.0, _make_settings(180))
        direct = _contract_result("free", 60.0, 180)

        gate_via = stream_gate_from_artifact_policy(via.artifact_policy)
        gate_direct = stream_gate_from_artifact_policy(direct.artifact_policy)

        assert gate_via == gate_direct

    def test_stream_gate_is_namedtuple(self):
        gate = stream_gate_from_artifact_policy(self._default_policy())
        assert isinstance(gate, StreamGate)

    def test_policy_field_count_matches_gate_derivation(self):
        """AnonymousPreviewArtifactPolicy 的 allow_* 字段数量与
        stream_gate_from_artifact_policy 覆盖的数量一致，防止将来新增 allow_* 字段
        但薄层未同步。"""
        import dataclasses
        policy_allow_fields = [
            f.name for f in dataclasses.fields(AnonymousPreviewArtifactPolicy)
            if f.name.startswith("allow_")
        ]
        # stream_gate_from_artifact_policy 应覆盖全部 allow_* 字段
        # 验证方式：全 False policy 的 forbidden 集大小 == allow_* 字段数
        gate = stream_gate_from_artifact_policy(self._default_policy())
        assert len(gate.download_forbidden_keys) == len(policy_allow_fields), (
            f"stream_gate_from_artifact_policy 覆盖 {len(gate.download_forbidden_keys)} 个 allow 字段，"
            f"但 AnonymousPreviewArtifactPolicy 有 {len(policy_allow_fields)} 个 allow_* 字段："
            f"{policy_allow_fields}。请同步更新薄 adapter。"
        )


# ---------------------------------------------------------------------------
# Group 5: FreePreviewAdmissionResult 字段完整性（防止字段遗漏）
# ---------------------------------------------------------------------------

class TestResultFieldCompleteness:

    def test_result_has_all_five_required_fields(self):
        via = admit_for_free_preview(60.0, _make_settings(180))
        assert hasattr(via, "decision")
        assert hasattr(via, "preview_duration_seconds")
        assert hasattr(via, "voice_strategy")
        assert hasattr(via, "artifact_policy")
        assert hasattr(via, "reason")

    def test_result_does_not_have_forbidden_fields(self):
        """FreePreviewAdmissionResult 不得含有 FORBIDDEN_ADMISSION_FIELDS 中的字段。"""
        from src.services.anonymous_preview_admission import FORBIDDEN_ADMISSION_FIELDS
        via = admit_for_free_preview(60.0, _make_settings(180))
        for field in FORBIDDEN_ADMISSION_FIELDS:
            assert not hasattr(via, field), (
                f"FreePreviewAdmissionResult 含有禁止字段 '{field}'（FORBIDDEN_ADMISSION_FIELDS）。"
            )
