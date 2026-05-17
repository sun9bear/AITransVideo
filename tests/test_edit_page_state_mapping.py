"""Phase 1 守卫: 6 内部状态 → 5 视觉表达映射不漂移.

设计文档 §6.1 矩阵:
  accepted        → "重合成"      ghost
  text_dirty      → "待合成"      primary
  voice_dirty     → "待合成"      primary  (同 text_dirty)
  tts_loading     → "合成中…"    warn + spinner
  tts_dirty       → "草稿待审 ↓"  warn
  tts_failed      → "重试合成"    danger

任何后端新增/删除状态都要先改前端映射 (SegmentRow.tsx + CurrentSegmentOpsPanel.tsx).
"""

from services.jobs.editing_segments import SUPPORTED_SEGMENT_STATUSES

# 期望的全集 (与 setting.py SEGMENT_STATUS_* 常量对齐)
EXPECTED_STATUSES = frozenset({
    "accepted",
    "text_dirty",
    "tts_loading",
    "tts_dirty",
    "tts_failed",
    "voice_dirty",
})


def test_status_vocabulary_unchanged():
    """前端 5 态文案依赖这 6 个状态全集 - 任何后端新增/删除状态都要先改前端映射."""
    assert SUPPORTED_SEGMENT_STATUSES == EXPECTED_STATUSES, (
        f"Segment status vocab changed. "
        f"Before adding/removing, update frontend SegmentRow + CurrentSegmentOpsPanel button mapping. "
        f"Expected {EXPECTED_STATUSES}, got {SUPPORTED_SEGMENT_STATUSES}"
    )
