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


def _extract_split_button_disabled_clause(text: str) -> str:
    """Find the JSX block for the split button and return its disabled={...} body.

    Locates `onClick={() => onSplit(...)` in the source, then scans forward
    a small window (≤ 600 chars) for the matching `disabled={` and returns
    the brace-balanced expression inside. Raises AssertionError with a
    debug excerpt if either landmark is missing.
    """
    onclick_match = re.search(r"onClick=\{[^}]*onSplit\(", text)
    assert onclick_match, "no onClick={... onSplit(...} found in source"
    # Search within the next ~600 chars (covers the typical Button JSX block)
    window = text[onclick_match.start() : onclick_match.start() + 800]
    dis_match = re.search(r"disabled=\{", window)
    assert dis_match, f"split button missing disabled={{...}} clause:\n{window[:400]}"
    # Brace-balanced capture starting at the `{` after `disabled=`
    body_start = dis_match.end()
    depth = 1
    i = body_start
    while i < len(window) and depth > 0:
        c = window[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    assert depth == 0, "unbalanced braces inside disabled={...}"
    return window[body_start : i - 1]


def test_segment_row_disables_split_during_regen():
    """Codex round-6 P1 + round-7 P2 #3: 用 regex 锁定 disabled 块内必须含 3 guards.
    避免误判 — 仅看文件全文是否含字符串无法证明 guard 真的在 split 按钮上."""
    text = (COMPONENTS_DIR / "SegmentRow.tsx").read_text(encoding="utf-8")
    clause = _extract_split_button_disabled_clause(text)
    for guard in ("isRegenerating", 'status === "tts_loading"', "isSaving"):
        assert guard in clause, (
            f"SegmentRow split-button disabled clause missing `{guard}`. "
            f"Actual clause:\n{clause}"
        )


def test_current_segment_ops_panel_disables_split_during_regen():
    """Codex round-6 P1 + round-7 P2 #2/#3: panel 拆分按钮 disabled 块也要锁定."""
    text = (COMPONENTS_DIR / "CurrentSegmentOpsPanel.tsx").read_text(encoding="utf-8")
    clause = _extract_split_button_disabled_clause(text)
    for guard in ("isRegenerating", 'status === "tts_loading"', "isSaving"):
        assert guard in clause, (
            f"CurrentSegmentOpsPanel split-button disabled clause missing `{guard}`. "
            f"Actual clause:\n{clause}"
        )


def test_current_segment_ops_panel_receives_is_saving():
    """Codex round-7 P2 #2: page.tsx 必须 pass isSaving prop 到 panel,
    panel interface 必须声明 isSaving."""
    panel = (COMPONENTS_DIR / "CurrentSegmentOpsPanel.tsx").read_text(encoding="utf-8")
    page = EDIT_PAGE.read_text(encoding="utf-8")
    assert re.search(r"isSaving\s*:\s*boolean", panel), (
        "CurrentSegmentOpsPanelProps must declare `isSaving: boolean`"
    )
    # page.tsx must pass isSaving={...savingSegmentIds...} on the panel JSX
    assert re.search(
        r"<CurrentSegmentOpsPanel[^/]*isSaving=\{[^}]*savingSegmentIds",
        page,
        re.DOTALL,
    ), "page.tsx must pass isSaving={... savingSegmentIds ...} to CurrentSegmentOpsPanel"


def test_split_dialog_supports_multi_cut_phase_2a():
    """Phase 2a unlock: SplitSegmentDialog must track cuts as an ARRAY
    (not single source/cn pos) and submit via the new split-many endpoint."""
    dialog_path = EDIT_DIR / "SplitSegmentDialog.tsx"
    text = dialog_path.read_text(encoding="utf-8")
    # State shape must be cuts[] instead of split{Source,Cn}Pos scalars
    assert re.search(r"cuts.*Array<", text) or re.search(r"cuts\s*:\s*Array<", text), (
        "SplitSegmentDialog must store cuts as an array (Phase 2a multi-cut)"
    )
    # Old single-cut field names must be gone (plan §5.5 → §5.6 migration)
    assert "splitSourcePos" not in text, (
        "Phase 2a removed splitSourcePos scalar state"
    )
    assert "splitCnPos" not in text, (
        "Phase 2a removed splitCnPos scalar state"
    )
    # speaker_ids array (instead of speaker_a / speaker_b)
    assert "speakerIds" in text, "Multi-cut needs speakerIds array"
    # onSubmit payload shape switched to {cuts, speaker_ids}
    assert "speaker_ids" in text, "Submit body must carry speaker_ids array"


def test_page_uses_split_many_api():
    """page.tsx must import + call splitEditingSegmentMany for Phase 2a
    (the dialog submits multi-cut payload; old splitEditingSegment is
    kept for backward compat but the new dialog should not route to it)."""
    text = EDIT_PAGE.read_text(encoding="utf-8")
    assert "splitEditingSegmentMany" in text, (
        "page.tsx must import + use splitEditingSegmentMany (Phase 2a)"
    )
    assert "handleSplitSegmentMany" in text, (
        "page.tsx must define handleSplitSegmentMany handler"
    )


def test_imperative_scrolls_use_sticky_offset_ref():
    """Codex round-7 P2 #1: handleSplitSegment / scrollToSegment 使用 ref,
    避免 useCallback 闭包捕获初始 0."""
    text = EDIT_PAGE.read_text(encoding="utf-8")
    # ref must exist
    assert re.search(r"stickyOffsetRef\s*=\s*useRef", text), (
        "page.tsx must declare stickyOffsetRef to avoid stale closure capture"
    )
    # All scrollToId calls outside JSX should read ref.current (not raw stickyOffsetPx
    # which would be captured by useCallback deps).
    # Count both safe and unsafe uses.
    unsafe = re.findall(r"scrollToId\([^)]*stickyOffset\s*:\s*stickyOffsetPx\b", text)
    assert not unsafe, (
        f"imperative scrollToId callsites must use stickyOffsetRef.current, "
        f"not raw stickyOffsetPx (stale closure). Found {len(unsafe)} unsafe usage(s)."
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
