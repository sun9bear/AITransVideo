"""Phase 1 redesign contract guards — AST scans, no UI framework needed.

Plan ref: docs/plans/2026-05-17-edit-page-redesign.md §9.1
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EDIT_DIR = REPO_ROOT / "frontend-next" / "src" / "app" / "(app)" / "workspace" / "[jobId]" / "edit"
EDIT_PAGE = EDIT_DIR / "page.tsx"
COMPONENTS_DIR = REPO_ROOT / "frontend-next" / "src" / "components" / "workspace" / "edit"
VL_PATH = REPO_ROOT / "frontend-next" / "src" / "components" / "workspace" / "segments" / "SegmentVirtualList.tsx"

# page.tsx still hosts: state hooks, ~500 lines of mutation handlers,
# CommitModal, AudioSyncConflictModal, derived selectors. Realistic
# post-extraction size baseline is ~1560 (Task 5 commit 1c6c9f4); guard
# threshold leaves headroom for minor follow-ups but catches any
# regression that re-inlines a large component.
PAGE_TSX_MAX_LINES = 1700


def test_page_tsx_under_line_threshold():
    """Phase 1 守卫: page.tsx 必须 < {threshold} 行 (was 2127 baseline)."""
    text = EDIT_PAGE.read_text(encoding="utf-8")
    n = text.count("\n") + 1
    assert n < PAGE_TSX_MAX_LINES, (
        f"page.tsx is {n} lines; expected < {PAGE_TSX_MAX_LINES}. "
        f"If you added a large inline component, extract it to a separate file."
    )


def test_new_components_exist_and_export():
    """3 个新组件必须都存在 + export."""
    targets = [
        (COMPONENTS_DIR / "SegmentRow.tsx", "SegmentRow"),
        (COMPONENTS_DIR / "CurrentSegmentOpsPanel.tsx", "CurrentSegmentOpsPanel"),
        (EDIT_DIR / "SplitSegmentDialog.tsx", "SplitSegmentDialog"),
    ]
    for path, name in targets:
        assert path.is_file(), f"missing {path}"
        text = path.read_text(encoding="utf-8")
        assert re.search(rf"export\s+(function|const)\s+{name}\b", text), (
            f"{path.name} does not export `{name}`"
        )


def test_page_imports_new_components():
    """page.tsx 必须 import 三个新组件 (验证已接入)."""
    text = EDIT_PAGE.read_text(encoding="utf-8")
    for name in ("SegmentRow", "CurrentSegmentOpsPanel", "SplitSegmentDialog"):
        assert re.search(
            rf"import\s+(?:\{{[^}}]*{name}[^}}]*\}}|\b{name}\b)\s+from",
            text,
        ), f"page.tsx must import {name}"


def test_page_no_inline_segment_card_or_status_chip():
    """page.tsx 不应再有 1300+ 行的内联 SegmentCard 或 StatusChip."""
    text = EDIT_PAGE.read_text(encoding="utf-8")
    assert not re.search(r"^\s*function\s+SegmentCard\s*\(", text, re.MULTILINE), (
        "Inline `function SegmentCard` should be removed in Phase 1 (extracted to SegmentRow.tsx)"
    )
    assert not re.search(r"^\s*function\s+StatusChip\s*\(", text, re.MULTILINE), (
        "Inline `function StatusChip` should be removed in Phase 1 (status now expressed via button)"
    )


def test_segment_row_no_hex_color_literals():
    """SegmentRow 不应硬写 hex 颜色字面量 (必须用 var(--xxx) tokens)."""
    text = (COMPONENTS_DIR / "SegmentRow.tsx").read_text(encoding="utf-8")
    # Hex like #RRGGBB or #RGB inside any quoted string.
    # Strip CSS comments first (none typical but be safe) — naive check.
    hex_matches = re.findall(r'#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b', text)
    assert not hex_matches, (
        f"SegmentRow.tsx contains hex color literals: {hex_matches}; "
        f"use var(--xxx) tokens instead so theme switching works."
    )


def test_current_segment_ops_panel_no_hex_color_literals():
    """CurrentSegmentOpsPanel: 同样的 token-only 要求."""
    text = (COMPONENTS_DIR / "CurrentSegmentOpsPanel.tsx").read_text(encoding="utf-8")
    hex_matches = re.findall(r'#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b', text)
    assert not hex_matches, (
        f"CurrentSegmentOpsPanel.tsx contains hex color literals: {hex_matches}"
    )


def test_segment_virtual_list_has_sticky_offset():
    """SegmentVirtualList.scrollToId 接受 stickyOffset 参数 (Task 1)."""
    text = VL_PATH.read_text(encoding="utf-8")
    assert "stickyOffset" in text, (
        "SegmentVirtualList missing stickyOffset support — "
        "Task 1 of Phase 1 must add it before Task 5 layout can pass it."
    )
    # The new prop on the SegmentVirtualListProps interface for auto-scroll
    assert "stickyOffsetForAutoScroll" in text, (
        "SegmentVirtualList missing stickyOffsetForAutoScroll prop"
    )


# ---------- Codex round-6 P1+P2 fix verification ----------

def test_page_wires_sticky_offset_to_virtual_list():
    """Codex round-6 P2: stickyOffset 必须从 page.tsx 真正接入 SegmentVirtualList。
    plan §8b.1 调用方约定 — 只加 prop 不 wire 等于没修。"""
    text = EDIT_PAGE.read_text(encoding="utf-8")
    # 至少 prop 传给 SegmentVirtualList
    assert "stickyOffsetForAutoScroll" in text, (
        "page.tsx must pass stickyOffsetForAutoScroll to <SegmentVirtualList>"
    )
    # scrollToId 命令式调用也要带 stickyOffset
    # （非空：page 至少在某处用了 align 同时带 stickyOffset）
    assert re.search(r"scrollToId\([^)]*stickyOffset", text, re.DOTALL), (
        "page.tsx scrollToId calls must pass stickyOffset for mobile-occluder compensation"
    )


def test_page_video_sticky_on_mobile_too():
    """Codex round-6 P2: §2.2 mobile 视频也要 sticky。
    检查 video 容器有 `sticky top-` 类（不是只有 `lg:sticky`）。"""
    text = EDIT_PAGE.read_text(encoding="utf-8")
    # Look for the video sticky container — `sticky top-[...]` without `lg:` prefix.
    # The data-sticky-video element should carry a base `sticky` class.
    pattern = re.compile(r'data-sticky-video[^>]*className="[^"]*\bsticky\b')
    assert pattern.search(text), (
        "Sticky-video container must carry base `sticky` class so mobile gets "
        "occlusion behavior (plan §2.2 + §8b.1)."
    )


def test_segment_row_responsive_grid():
    """Codex round-6 P2: <768px 不能用 `100px_1fr_230px` 固定 3 列，
    否则 360px 屏幕横向溢出。必须是响应式（默认窄列 + sm 加宽）。"""
    text = (COMPONENTS_DIR / "SegmentRow.tsx").read_text(encoding="utf-8")
    # 默认（mobile）应是 2 列 grid，sm 才升到 3 列
    assert "grid-cols-[70px_1fr]" in text, (
        "SegmentRow mobile grid must use a compact 2-col layout (e.g. 70px_1fr); "
        "found no narrow mobile grid template"
    )
    assert "sm:grid-cols-[100px_1fr_230px]" in text, (
        "SegmentRow desktop grid (sm:) must use 100px_1fr_230px"
    )


def test_segment_row_disables_split_during_regen():
    """Codex round-6 P1: 单段 TTS 进行中点拆分会产生 orphan draft。
    拆分按钮必须在 isRegenerating / status==='tts_loading' / isSaving 时 disabled。"""
    text = (COMPONENTS_DIR / "SegmentRow.tsx").read_text(encoding="utf-8")
    # Look for the split button's disabled clause — must include all three guards.
    # Use a multiline-aware search of the source for the literal expressions.
    for guard, label in [
        ("isRegenerating", "isRegenerating"),
        ('status === "tts_loading"', 'status === "tts_loading"'),
        ("isSaving", "isSaving"),
    ]:
        assert guard in text, (
            f"SegmentRow must disable split when {label} (race protection per Codex round-6 P1)"
        )


def test_current_segment_ops_panel_disables_split_during_regen():
    """Codex round-6 P1: 同上，CurrentSegmentOpsPanel 拆分按钮也要守护."""
    text = (COMPONENTS_DIR / "CurrentSegmentOpsPanel.tsx").read_text(encoding="utf-8")
    assert "isRegenerating" in text, (
        "CurrentSegmentOpsPanel must access isRegenerating to gate split"
    )
    assert 'status === "tts_loading"' in text, (
        "CurrentSegmentOpsPanel must gate split on tts_loading status"
    )


# ---------- 5-state visual mapping ----------

def test_segment_row_implements_five_visual_states():
    """前端 SegmentRow 必须按 plan §6.1 提供 5 个 regen 按钮文案/颜色变体。
    Codex round-6 P2: 守卫不能仅停在后端 vocab —— 前端映射本身也要 lock 住。"""
    text = (COMPONENTS_DIR / "SegmentRow.tsx").read_text(encoding="utf-8")
    # accepted → "重合成"
    assert "重合成" in text, "Missing accepted-state label '重合成'"
    # text_dirty / voice_dirty → "待合成"
    assert "待合成" in text, "Missing dirty-state label '待合成'"
    # tts_loading → "合成中…" (Unicode ellipsis)
    assert "合成中" in text, "Missing loading-state label '合成中'"
    # tts_dirty → "草稿待审 ↓"
    assert "草稿待审" in text, "Missing draft-pending label '草稿待审'"
    # tts_failed → "重试合成"
    assert "重试合成" in text, "Missing failed-state label '重试合成'"
    # 5 distinct status arms inside regenVisual / similar branch
    # crude check — should see status === literals for each
    for s in ("tts_loading", "tts_dirty", "tts_failed", "text_dirty", "voice_dirty"):
        assert f'"{s}"' in text or f"'{s}'" in text, (
            f"SegmentRow does not branch on status '{s}' — visual mapping incomplete"
        )
