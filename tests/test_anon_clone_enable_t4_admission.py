"""P4 — 防线① admission 契约 + adapter 运行时回归.

plan 2026-06-14 §3.3。CosyVoice 放行 / MiniMax 拦截的契约诚实性：
- adapter ``admit_for_free_preview`` 现 mode/flag-aware：express + admin 主开关
  开 → admission.voice_strategy = EXPRESS_TEMPORARY_CLONE_GATE（契约信号）；
  free 或 flag 关 → PRESET_ONLY。
- 改 adapter **只改 voice_strategy**（create 不消费）；decision / duration /
  artifact_policy 对 free vs express 完全一致——零回归到 create 消费的字段。
- ``raise_clone_provider_boundary`` **仍恒抛**（保护契约模块不变成 clone 执行器；
  真克隆在 pipeline，见 P2）。
- 旧调用方（无 mode）字节级不变（默认 mode="free"）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO / "src"), str(_REPO / "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from anonymous_preview_policy import admit_for_free_preview  # noqa: E402
from services.anonymous_preview_admission import (  # noqa: E402
    AdmissionDecision,
    AnonymousPreviewMode,
    VoiceStrategy,
    raise_clone_provider_boundary,
)


def _settings(max_seconds=180):
    return SimpleNamespace(anonymous_preview_max_seconds=max_seconds)


def test_express_with_flag_emits_clone_gate():
    res = admit_for_free_preview(
        60.0, _settings(), mode="express", express_clone_enabled=True
    )
    assert res.decision is AdmissionDecision.ADMITTED
    assert res.voice_strategy is VoiceStrategy.EXPRESS_TEMPORARY_CLONE_GATE


def test_express_without_flag_is_preset_only():
    res = admit_for_free_preview(
        60.0, _settings(), mode="express", express_clone_enabled=False
    )
    assert res.voice_strategy is VoiceStrategy.PRESET_ONLY


def test_free_forces_preset_even_if_flag_passed():
    """非 express 档：clone flag 强制 False（防误把 free 当 express 克隆）。"""
    res = admit_for_free_preview(
        60.0, _settings(), mode="free", express_clone_enabled=True
    )
    assert res.voice_strategy is VoiceStrategy.PRESET_ONLY


def test_backward_compat_default_mode_free():
    """旧调用方（无 mode kwarg）= mode='free' / PRESET_ONLY，字节级不变。"""
    res = admit_for_free_preview(60.0, _settings())
    assert res.decision is AdmissionDecision.ADMITTED
    assert res.voice_strategy is VoiceStrategy.PRESET_ONLY


def test_consumed_fields_identical_free_vs_express():
    """create 消费的字段（decision / duration / artifact_policy）对 free vs
    express 完全一致——改 adapter 只动 voice_strategy，零回归。"""
    free = admit_for_free_preview(90.0, _settings(180), mode="free")
    expr = admit_for_free_preview(
        90.0, _settings(180), mode="express", express_clone_enabled=True
    )
    assert free.decision == expr.decision == AdmissionDecision.ADMITTED
    assert free.preview_duration_seconds == expr.preview_duration_seconds
    assert free.artifact_policy == expr.artifact_policy  # frozen dataclass eq


def test_duration_cap_still_applies_for_express():
    res = admit_for_free_preview(
        500.0, _settings(180), mode="express", express_clone_enabled=True
    )
    assert res.preview_duration_seconds == 180.0  # capped


def test_clone_provider_boundary_still_raises():
    """🔥 boundary helper 仍恒抛（保护契约模块不变 clone 执行器）。"""
    for m in (AnonymousPreviewMode.EXPRESS, AnonymousPreviewMode.FREE, "express", "bogus"):
        with pytest.raises(NotImplementedError):
            raise_clone_provider_boundary(m)
