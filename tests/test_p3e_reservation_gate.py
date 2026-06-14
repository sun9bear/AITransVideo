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


def test_gate_wired_into_needs_new_clone():
    """闸结果必须接进 `_smart_needs_new_clone` AND 链（闸关→不接真 provider）。"""
    src = _process_src()
    assert "_smart_reservation_gate_open_result" in src
    # 出现在 _smart_needs_new_clone 赋值块里
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "_smart_needs_new_clone" in targets:
                seg = ast.get_source_segment(src, node) or ""
                if "_smart_reservation_gate_open_result" in seg:
                    found = True
                    break
    assert found, "_smart_reservation_gate_open_result 必须出现在 _smart_needs_new_clone 赋值"


def test_flag_read_with_default_false():
    """rollout flag 读取必须 default=False（默认 inert，不改既有行为）。"""
    src = _process_src()
    assert '"smart_clone_requires_reservation", default=False' in src, (
        "rollout flag 必须以 default=False 读取（默认不启用收紧）"
    )


def test_reservation_id_read_from_snapshot():
    """reservation_id 必须从 snapshot 读（create 端 stamp 的 marker）。"""
    src = _process_src()
    assert '_snap("smart_clone_reservation_id")' in src


def test_three_legacy_conditions_preserved():
    """既有三条件 AND 必须保留（P3e 只新增第 4 条件，不削弱既有）。"""
    src = _process_src()
    for cond in (
        "_smart_consent_allows_clone",
        "_smart_admin_clone_enabled",
        "_smart_speaker_ids_requiring_clone",
    ):
        assert cond in src, f"既有 smart 触发条件 {cond} 不应被移除"
