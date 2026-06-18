"""P3e §2 — 智能版克隆 reservation 收紧闸（pipeline gate）测试.

plan 2026-06-14-p3-smart-clone-600-credit-subplan v3 §2。pipeline 选 provider
时：admin `smart_clone_requires_reservation`=True 且 JobRecord 无有效
`smart_clone_reservation_id` → 一律不接真 MiniMax provider（只 preset/reuse），
封死现在 full smart 无 reservation 调付费的漏收。**默认 False → 既有行为不变**。

钱-critical gate 逻辑抽成纯函数 `_smart_reservation_gate_open` 直接单测；接线
（写进 `_smart_needs_new_clone` AND 链 + 读 flag default False + 读 snapshot
reservation_id）用 source-scan 守卫钉死。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pipeline.process import _smart_reservation_gate_open  # noqa: E402

_PROCESS_PY = _SRC / "pipeline" / "process.py"


# ---------------------------------------------------------------------------
# 纯函数 gate 逻辑（钱-critical）
# ---------------------------------------------------------------------------


def test_gate_open_when_not_requiring_reservation():
    """🔥 默认（不启用收紧）→ 闸恒 open，既有 smart auto-clone 不变。"""
    assert _smart_reservation_gate_open(False, None) is True
    assert _smart_reservation_gate_open(False, "") is True
    assert _smart_reservation_gate_open(False, "rid") is True


def test_gate_closed_when_requiring_but_no_reservation():
    """🔥 启用收紧但无 reservation_id → 闸关（不接真 MiniMax，封死漏收）。"""
    assert _smart_reservation_gate_open(True, None) is False
    assert _smart_reservation_gate_open(True, "") is False
    assert _smart_reservation_gate_open(True, "   ") is False  # 空白视同无


def test_gate_open_when_requiring_and_has_reservation():
    """🔥 启用收紧 + 带 reservation_id → 闸 open（预扣 600 的预览克隆放行）。"""
    assert _smart_reservation_gate_open(True, "11111111-1111-1111-1111-111111111111") is True
    assert _smart_reservation_gate_open(True, " rid ") is True  # 去空白后非空


# ---------------------------------------------------------------------------
# source-scan：接线契约
# ---------------------------------------------------------------------------


def _process_src() -> str:
    return _PROCESS_PY.read_text(encoding="utf-8")


def test_gate_folded_into_effective_clone_enabled():
    """🔥 CodeX P3e-1a P1：闸必须**折进** _smart_effective_clone_enabled
    （= admin_clone_enabled AND gate），使「闸关」=「admin 关克隆」=退预设。
    否则只塞 needs_new_clone 会让闸关时仍 admin_clone_enabled=True → PAUSED
    水线 / 样本抽取 handoff（不是 PRESET）。"""
    src = _process_src()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "_smart_effective_clone_enabled" in targets:
                seg = ast.get_source_segment(src, node) or ""
                if "_smart_admin_clone_enabled" in seg and "_smart_reservation_gate_open_result" in seg:
                    found = True
                    break
    assert found, (
        "_smart_effective_clone_enabled 必须 = _smart_admin_clone_enabled AND "
        "_smart_reservation_gate_open_result"
    )


def test_effective_used_in_all_clone_decision_points():
    """effective 必须用于**所有** clone 决策点：样本抽取 / needs_new_clone /
    外层 if / evaluate_voice_review 入参（任一漏用 → 闸关仍可能触达真 provider
    或 PAUSED 而非 PRESET）。"""
    src = _process_src()
    # needs_new_clone 用 effective
    assert "_smart_needs_new_clone = bool(" in src
    # evaluate_voice_review 入参用 effective（不是 raw admin）
    assert "admin_clone_enabled=_smart_effective_clone_enabled" in src
    assert "admin_clone_enabled=_smart_admin_clone_enabled" not in src, (
        "evaluate_voice_review 必须传 effective（含 reservation 闸），不是 raw admin"
    )
    # 至少 3 处 clone 决策用 effective（样本抽取 + needs_new_clone + 外层 if +
    # evaluate 入参），raw admin 只剩定义 + effective 定义里各 1 次
    assert src.count("_smart_effective_clone_enabled") >= 4


def test_flag_read_strict_is_true():
    """🔥 CodeX P3e-1a P2：read_admin_setting 不做类型转换，bool("false")=True。
    钱核心 flag 必须 `is True` 严格判定（不能用 bool(...)）。"""
    src = _process_src()
    assert '"smart_clone_requires_reservation", default=False' in src, (
        "rollout flag 必须以 default=False 读取（默认不启用收紧）"
    )
    # 必须 is True，不能 bool(read_admin_setting("smart_clone_requires_reservation"...))
    flat = " ".join(src.split())
    assert "is True" in flat
    assert 'bool( read_admin_setting( "smart_clone_requires_reservation"' not in flat, (
        "钱核心 flag 不能用 bool() 读（StrictBool 不护 pipeline 直读 JSON）"
    )


def test_reservation_id_read_from_snapshot():
    """reservation_id 必须从 snapshot 读（create 端 stamp 的 marker）。"""
    src = _process_src()
    assert '_snap("smart_clone_reservation_id")' in src


def test_requires_reservation_uses_smart_state_markers_not_preview_admin_flag():
    """CodeX PR #33：preview admin flag 不能全局改变 full Smart。reservation
    收紧由显式 rollout flag 或 server-stamped smart_state markers 触发：preview
    marker、full-Smart reserved marker、full-Smart deny marker。"""
    src = _process_src()
    flat = " ".join(src.split())
    assert '"smart_clone_requires_reservation", default=False' in flat
    assert '_smart_state_dict.get("smart_preview_mode") is True' in flat
    assert '_smart_state_dict.get("smart_clone_credit_reserved") is True' in flat
    assert 'smart_clone_reservation_deny_reason' in flat
    assert '"smart_preview_clone_enabled", default=False' not in flat


def test_reservation_id_read_from_smart_state_dict():
    """P3e §2：reservation marker 主读位置=smart_state 字典（与 finalizer
    marker-gate 一致；create/Option C 经 submit_job 写进 JobRecord.smart_state）。"""
    src = _process_src()
    assert '_snap("smart_state")' in src
    assert '_smart_state_dict.get("smart_clone_reservation_id")' in src


def test_three_legacy_conditions_preserved():
    """既有三条件 AND 必须保留（P3e 只新增 reservation 闸，不削弱既有）。"""
    src = _process_src()
    for cond in (
        "_smart_consent_allows_clone",
        "_smart_admin_clone_enabled",
        "_smart_speaker_ids_requiring_clone",
    ):
        assert cond in src, f"既有 smart 触发条件 {cond} 不应被移除"
