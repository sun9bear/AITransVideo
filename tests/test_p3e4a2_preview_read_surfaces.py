"""P3e-4a-2 — 智能版预览只读检视面 stream-only 闸（封 review-state 译文 / speaker-audio
源音 / report 字幕 cue 泄漏）.

plan 2026-06-14-p3e2-preview-lane-design.md §8。CodeX P3e-4a 第二轮标：P3e-3d 已把
download / stream / draft-audio / generate-video / jianying 贯通 stream-only 闸，但
**只读检视面**仍漏——smart 预览任务（登录免费用户，P3e-4a 放进来后可达）能直接调：
  - GET /jobs/{id}/review-state                      → transcript/translation items
                                                        的 source_text / **cn_text(译文)**
  - GET /jobs/{id}/speaker-audio/{spk}[/{seg}.wav]   → 源文 + **源音字节**
  - GET /jobs/{id}/reports/{name}                    → subtitle_width_report 等 cue 文本

本切片用同一 `_policy_mode_for(record) == "anonymous_preview"` 闸覆盖（与 P3e-3d 同档，
strict is True 经 extract_smart_preview_flag）。默认 inert：非预览任务 _policy_mode_for
!= anonymous_preview → 闸不触发 → 字节级不变。

源码级守卫（镜像 test_p3e3d_smart_preview_stream_only.py 的 anchor-window 模式：api.py
的端点处理器都在一个大 do_GET 里，无 per-endpoint 函数可 AST 抽取）。
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_GATE = '_policy_mode_for(record) == "anonymous_preview"'


def _flat() -> str:
    src = (_REPO / "src" / "services" / "jobs" / "api.py").read_text(encoding="utf-8")
    return " ".join(src.split())


def _gate_between(flat: str, anchor: str, serving: str) -> bool:
    """闸必须出现在端点 anchor 与其首个数据交付调用 serving 之间。serving 自 anchor
    位置起搜（避开函数定义与相邻端点块的同名闸，杜绝窗口式假阳性）。"""
    a = flat.find(anchor)
    assert a != -1, f"anchor 未找到: {anchor!r}"
    s = flat.find(serving, a)
    assert s != -1 and s > a, f"serving 未找到: {serving!r}（在 {anchor!r} 之后）"
    return _GATE in flat[a:s]


def test_review_state_gated_for_preview():
    """🔥 review-state 暴露 cn_text(译文)——预览任务须 403（防免费白嫖译文，损害转化）。"""
    assert _gate_between(
        _flat(), 'path_parts[2] == "review-state"', "_build_review_state_for_job(record"
    )


def test_reports_file_gated_for_preview():
    """report 文件流（subtitle_width_report 含字幕 cue 文本）——预览任务须 403。"""
    assert _gate_between(_flat(), "report_name = path_parts[3]", "_resolve_job_report_path(")


def test_speaker_audio_list_gated_for_preview():
    """speaker-audio 列表（source_text + 音频 URL）——预览任务须 403。"""
    assert _gate_between(
        _flat(), 'path_parts[2] == "speaker-audio"', "get_speaker_audio_segments("
    )


def test_speaker_audio_wav_gated_for_preview():
    """speaker-audio WAV（源音字节）——预览任务须 403。"""
    assert _gate_between(
        _flat(), "seg_filename = path_parts[4]", "extract_speaker_audio_segment("
    )


def test_gate_uses_central_policy_helper_not_inline():
    """闸统一走中心 _policy_mode_for（读 smart_state.smart_preview_mode），非内联判断。"""
    flat = _flat()
    # P3e-4a-2 至少新增 4 处该闸（review-state + reports + speaker-audio×2），叠加 P3e-3d
    # 既有（download/stream/draft-audio/generate-video/...）→ 总数应显著 > 4。
    assert flat.count(_GATE) >= 8, (
        f"_policy_mode_for anonymous_preview 闸出现次数偏少({flat.count(_GATE)})，"
        "P3e-4a-2 的 4 处只读面闸可能漏接。"
    )
