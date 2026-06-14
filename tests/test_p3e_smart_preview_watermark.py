"""P3e-3a — 智能版 3min 预览 teaser 水印（consumer 侧，charging-minutes 安全）.

plan 2026-06-14-p3e2-preview-lane-design.md §4 P3e-3。智能版预览 teaser 复用最严
策略档 ``"anonymous_preview"``（恒水印 + stream-only），由 pipeline 读
``smart_state.smart_preview_mode`` 驱动。**默认 inert**（create 未 stamp marker
前 job_smart_preview=False → 既有 smart 行为不变）；本切片仍扣分钟（无漏收），
跳分钟是后续 P3e-3b（必须在 3min teaser 之后，否则免费完整任务）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from services.r2_publisher_lib.downloadable_keys import (  # noqa: E402
    effective_policy_mode,
)
from utils.free_watermark import free_watermark_text_for  # noqa: E402

_PROCESS_PY = _SRC / "pipeline" / "process.py"


# ---------------------------------------------------------------------------
# effective_policy_mode：smart_preview → 最严档（恒水印 + stream-only）
# ---------------------------------------------------------------------------


def test_smart_preview_maps_to_strictest_policy_mode():
    """🔥 smart_preview=True → "anonymous_preview" 最严档（恒水印/stream-only）。"""
    assert effective_policy_mode("smart", None, smart_preview=True) == "anonymous_preview"
    # 即便 service_mode 是其它值，smart_preview 也强制最严档
    assert effective_policy_mode("studio", False, smart_preview=True) == "anonymous_preview"


def test_smart_preview_default_false_is_inert():
    """🔥 默认 smart_preview=False → 行为字节级不变（passthrough / 匿名优先）。"""
    # 非预览：透传 service_mode（既有行为）
    assert effective_policy_mode("express", None) == "express"
    assert effective_policy_mode("express", False, smart_preview=False) == "express"
    # 匿名优先级高于 smart_preview（都为真时仍匿名档）
    assert effective_policy_mode("smart", True, smart_preview=True) == "anonymous_preview"


def test_smart_preview_gets_watermark_via_mode():
    """smart_preview 经 "anonymous_preview" 档 → free_watermark_text_for 命中水印。"""
    mode = effective_policy_mode("smart", None, smart_preview=True)
    assert free_watermark_text_for(mode)  # 非 None = 有水印
    # 对照：普通 smart（非预览）无水印
    assert free_watermark_text_for(effective_policy_mode("smart", None)) is None


# ---------------------------------------------------------------------------
# source-scan：pipeline 读 smart_state.smart_preview_mode + 传给水印
# ---------------------------------------------------------------------------


def _proc_src() -> str:
    return _PROCESS_PY.read_text(encoding="utf-8")


def test_pipeline_reads_smart_preview_from_smart_state():
    """pipeline 从 smart_state 字典读 smart_preview_mode（区别 is_anonymous_preview
    字段——smart 预览保留克隆结算只跳分钟）。"""
    src = _proc_src()
    flat = " ".join(src.split())
    assert "job_smart_preview = bool(" in flat
    assert "_job_smart_state_snap.get('smart_preview_mode') is True" in flat


def test_pipeline_passes_smart_preview_to_watermark():
    """水印调用传 smart_preview=job_smart_preview。"""
    src = _proc_src()
    flat = " ".join(src.split())
    assert "smart_preview=job_smart_preview" in flat
