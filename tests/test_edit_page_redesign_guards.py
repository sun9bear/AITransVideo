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
