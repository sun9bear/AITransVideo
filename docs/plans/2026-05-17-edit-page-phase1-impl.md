# 视频修改页重排版 · Phase 1 实现计划

> **配套设计文档**：[2026-05-17-edit-page-redesign.md](2026-05-17-edit-page-redesign.md)（875 行，5 轮 review 后冻结）
> 本文档只列**实现步骤**；设计决策 / 状态映射 / 视觉规则全部去查设计文档。
> **For agentic workers**：每步是 checkbox（`- [ ]`），按顺序勾，不跳。

**Goal**：把 `/workspace/{jobId}/edit` 重排为「左视频 + 右段落联动 + 按钮即状态」，路径不动后端，只重排前端。

**Architecture**：从现有 2127 行 page.tsx 抽出 3 个新组件（SegmentRow / CurrentSegmentOpsPanel / SplitSegmentDialog），page.tsx 瘦到 < 1200 行只负责状态编排 + grid 布局；token-only 视觉，跟随 AppShell `data-theme="ink"` ↔ `"ink-dark"`。

**Tech Stack**：Next.js 16 + React 19 + TypeScript strict + Tailwind v4 + shadcn/ui Dialog；后端不动；测试用 pytest（Python AST 守卫）。

**Phase 1 不做的（去看 [设计文档 §8.2](2026-05-17-edit-page-redesign.md)）**：source-video stream / split-many 后端 / 智能预填 / R2 push 策略。

**Phase 1 测试约束**：项目无前端测试框架（vitest / playwright / jest 都没装），用 **Python AST 契约测试** + **类型检查 (tsc) + lint (eslint)** + **手测 smoke checklist** 替代。

---

## Task 1 · SegmentVirtualList 加 stickyOffset prop

> 解决设计文档 §8b.1：移动端 sticky 视频遮挡 active 段。

**Files**：
- Modify: `frontend-next/src/components/workspace/segments/SegmentVirtualList.tsx`

- [ ] **Step 1.1：扩接口**

在 `SegmentVirtualListRef` 加 `stickyOffset?` 参数：

```ts
export interface SegmentVirtualListRef {
  scrollToId(
    id: string,
    opts?: {
      align?: "center" | "start"
      stickyOffset?: number  // px - 调用方告知上方 sticky 元素占去的像素
    },
  ): void
}
```

- [ ] **Step 1.2：在 scrollToId 内部用 stickyOffset**

修改第 192-209 行 `useImperativeHandle.scrollToId`：

```ts
scrollToId(id, opts) {
  const container = scrollContainerRef.current
  if (!container) return
  const idx = items.findIndex((it) => getId(it) === id)
  if (idx < 0) return
  const offset = offsets[idx]
  const itemHeight = heightMap[id] ?? estimatedItemHeight
  const align = opts?.align ?? "center"
  const stickyOffset = opts?.stickyOffset ?? 0
  let target = offset
  if (align === "center") {
    target = offset - (container.clientHeight - itemHeight) / 2
  }
  // sticky 区遮挡补偿：上推 target，使 item 在 sticky 区下方
  target -= stickyOffset
  target = Math.max(0, Math.min(target, totalHeight - container.clientHeight))
  container.scrollTo({
    top: target,
    behavior: prefersReducedMotion() ? "auto" : "smooth",
  })
},
```

- [ ] **Step 1.3：activeSegmentId 自动滚动也补偿**

修改第 213-248 行 `useEffect`：在 target 计算前减去 stickyOffset。**但 hook 内部不知道 stickyOffset**，所以 prop 上扩 `stickyOffsetForAutoScroll?`：

```ts
interface SegmentVirtualListProps<T> {
  // ... 现有
  /** 自动滚动 active item 时上推的 px（避开 sticky video 等遮挡）。默认 0 */
  stickyOffsetForAutoScroll?: number
}
```

在 useEffect 里用 `props.stickyOffsetForAutoScroll ?? 0`。

- [ ] **Step 1.4：类型检查**

```bash
cd frontend-next && npx tsc --noEmit
```

Expected: 0 errors

- [ ] **Step 1.5：提交**

```bash
git add frontend-next/src/components/workspace/segments/SegmentVirtualList.tsx
git commit -m "feat(edit): add stickyOffset to SegmentVirtualList.scrollToId

For Phase 1 mobile layout — sticky video at top can occlude auto-scrolled
active segment. Caller passes stickyOffset (e.g. video element height on
mobile) to shift target up. Default = 0 preserves existing desktop behavior.

Plan ref: docs/plans/2026-05-17-edit-page-redesign.md §8b.1"
```

---

## Task 2 · 新组件 SegmentRow（视觉，先不接入）

> 解决设计文档 §4.1 双语行 + 按钮即状态。本 task 只创建文件，不动 page.tsx。

**Files**：
- Create: `frontend-next/src/components/workspace/edit/SegmentRow.tsx`

- [ ] **Step 2.1：摸清现有 SegmentCard 的 props 形状**

读 `frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx:1315-1928`（SegmentCard 内联实现），抄出 `SegmentCardProps` 接口 + 关键 callbacks。

- [ ] **Step 2.2：创建 SegmentRow.tsx**

新文件骨架（具体 JSX 看设计文档 §4 + mockup `.superpowers/brainstorm/.../row-density-v4.html`）：

```tsx
"use client"

import { useState, type ChangeEvent } from "react"
import { cn } from "@/lib/utils"
import type { EditingSegment, SegmentStatus, EditingSpeaker } from "@/types/editing"

export interface SegmentRowProps {
  jobId: string
  index: number
  segment: EditingSegment
  status: SegmentStatus
  isSaving: boolean
  isRegenerating: boolean
  isActive: boolean
  isBatchRegenerating: boolean  // 批量进行时单段按钮 disable（设计文档 §13）
  availableSpeakerIds: string[]
  editingSpeakers: EditingSpeaker[]
  speakerNameMap: Record<string, string>
  onTextChange(segmentId: string, cn_text: string): void
  onSourceTextChange(segmentId: string, source_text: string): void
  onSpeakerChange(segmentId: string, speaker_id: string): void
  onRegenerate(segmentId: string): void
  onAcceptDraft(segmentId: string): void
  onDiscardDraft(segmentId: string): void
  onSeek(segmentId: string): void
  onSplit(segmentId: string): void
}

export function SegmentRow(props: SegmentRowProps) {
  // ...
}
```

关键视觉规则（设计文档 §4 + §6）：
- 行 grid: `grid-template-columns: 100px 1fr 230px`
- 左列：段号 + 时间码（horizontal），下方说话人 chip dropdown
- 中列：英文 muted + 中文 foreground + 草稿面板（tts_dirty 时）
- 右列：拆分 button（常驻）+ 重合成 button（按 5 态变文案颜色）
- active row: `border-l-2 border-primary` + `bg-primary/[0.06]`
- **不写颜色字面量**，全用 `var(--xxx)` tokens（被 Task 9 AST 守卫扫）

按钮文案矩阵（设计文档 §6.1）：

| 状态 | 文案 | className |
|------|------|-----------|
| accepted | 重合成 | ghost gray (border-border text-muted-foreground) |
| text_dirty / voice_dirty | 待合成（X秒） | bg-primary text-primary-foreground |
| tts_loading | 合成中… | bg-[color:var(--ochre)]/10 text-[color:var(--ochre)] + spinner |
| tts_dirty | 草稿待审 ↓ | bg-[color:var(--ochre)]/10 text-[color:var(--ochre)] |
| tts_failed | 重试合成 | text-destructive border-destructive |

`isBatchRegenerating === true` 时：拆分 + 重合成都 disable + tooltip「正在批量合成…」（设计文档 §13）。

- [ ] **Step 2.3：类型检查 + lint**

```bash
cd frontend-next && npx tsc --noEmit && npm run lint
```

Expected: 0 errors / 0 warnings on the new file（旧文件历史警告忽略）

- [ ] **Step 2.4：提交**

```bash
git add frontend-next/src/components/workspace/edit/SegmentRow.tsx
git commit -m "feat(edit): add SegmentRow component (not yet wired)

Bilingual row + 5-state button + persistent split/regen actions. Mirrors
existing SegmentCard logic from page.tsx but with new compact visual.
Not yet imported — wired in Task 3.

Plan ref: docs/plans/2026-05-17-edit-page-redesign.md §4 + §6.1"
```

---

## Task 3 · 接入 SegmentRow，删除旧 SegmentCard

**Files**：
- Modify: `frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx`

- [ ] **Step 3.1：import SegmentRow**

页面顶部 import：

```ts
import { SegmentRow } from "@/components/workspace/edit/SegmentRow"
```

- [ ] **Step 3.2：替换 SegmentVirtualList renderItem**

找到 `renderItem={(seg, idx) => (<div className="pb-3"><SegmentCard ... /></div>)}` 块（约 1231-1255 行），替换 `<SegmentCard ...>` 为 `<SegmentRow ...>`，prop 透传 + 新增 `isBatchRegenerating={isBatchRegenerating}`。

- [ ] **Step 3.3：删除 page.tsx 末尾的 `function SegmentCard(...)`**

删除第 1367-1928 行（整个 SegmentCard 内联实现）。同时删它专用的 `interface SegmentCardProps`（第 1315-1366 行）+ `function StatusChip` 如果只被 SegmentCard 用。

- [ ] **Step 3.4：类型检查 + 行数验证**

```bash
cd frontend-next && npx tsc --noEmit
wc -l src/app/\(app\)/workspace/\[jobId\]/edit/page.tsx
```

Expected:
- tsc 0 errors
- page.tsx 行数应该从 2127 降到 ~1500（删除 SegmentCard 约 560 行）

- [ ] **Step 3.5：手测 smoke**

```bash
cd frontend-next && npm run dev
```

打开浏览器 `http://localhost:3000/workspace/<某 succeeded job>/edit`：
- 段落列表正常渲染（双语行 + 按钮）
- 点段：变 active + 视频 seek
- 改文本：变红「待合成」按钮
- 重合成单段：能跑通 → 草稿面板出现
- 接受 / 丢弃草稿：能跑通

- [ ] **Step 3.6：提交**

```bash
git add frontend-next/src/app/\(app\)/workspace/\[jobId\]/edit/page.tsx
git commit -m "feat(edit): wire SegmentRow + delete inline SegmentCard

page.tsx: 2127 → ~1500 lines after extracting SegmentRow. All segment
list rows now render via new component (bilingual, 5-state button).

Plan ref: docs/plans/2026-05-17-edit-page-redesign.md §1.1, §12 step 5"
```

---

## Task 4 · 新组件 CurrentSegmentOpsPanel

> 设计文档 §3.2：左侧视频下方的当前段操作区。

**Files**：
- Create: `frontend-next/src/components/workspace/edit/CurrentSegmentOpsPanel.tsx`

- [ ] **Step 4.1：创建组件**

骨架（具体内容看设计文档 §3.2 状态矩阵）：

```tsx
"use client"

import { cn } from "@/lib/utils"
import type { EditingSegment, SegmentStatus } from "@/types/editing"

export interface CurrentSegmentOpsPanelProps {
  jobId: string
  segment: EditingSegment | null
  status: SegmentStatus | null
  isRegenerating: boolean
  isBatchRegenerating: boolean
  speakerName: string | null  // 友好名（从 speakerNameMap 解析）
  onRegenerate(segmentId: string): void
  onAcceptDraft(segmentId: string): void
  onDiscardDraft(segmentId: string): void
  onPreviewSource(segmentId: string): void  // 试听原音
}

export function CurrentSegmentOpsPanel(props: CurrentSegmentOpsPanelProps) {
  if (!props.segment) {
    // 占位：「点选段落开始修改」
    return (
      <div className="surface-card p-4 text-sm text-muted-foreground">
        点选段落开始修改
      </div>
    )
  }
  // 状态分支：accepted / dirty / loading / draft / failed
  // 设计文档 §3.2 矩阵
}
```

**callback 共用约束（设计文档 §8a3.1）**：`onRegenerate` / `onAcceptDraft` / `onDiscardDraft` 必须是从 page.tsx 传下来的同一组 handler（与 SegmentRow 共用），**不可** 在本组件内部再 implementCONCAT 平行实现。

- [ ] **Step 4.2：类型检查**

```bash
cd frontend-next && npx tsc --noEmit
```

- [ ] **Step 4.3：提交**

```bash
git add frontend-next/src/components/workspace/edit/CurrentSegmentOpsPanel.tsx
git commit -m "feat(edit): add CurrentSegmentOpsPanel (not yet wired)

Left-side operation panel that mirrors the activeSegmentId's operations.
Shares callbacks with SegmentRow (single source of truth in page.tsx).

Plan ref: docs/plans/2026-05-17-edit-page-redesign.md §3.2 + §8a3.1"
```

---

## Task 5 · 重排 page.tsx 主布局（核心 task）

> 设计文档 §2.1（桌面） + §8a.1（响应式 4 断点）。

**Files**：
- Modify: `frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx`

- [ ] **Step 5.1：定位现有 JSX return**

在 page.tsx `VideoEditPage()` 函数的 `return (` 处（约 1021 行起），现状是纵向堆叠（Header / video aside / Tab nav / main panel）。

- [ ] **Step 5.2：改 wrapper 为左右 grid**

```tsx
return (
  <div className="space-y-4 max-w-7xl mx-auto px-3 sm:px-0">
    {/* Header bar — 见设计文档 §2.5 */}
    <section className="surface-card p-3 flex items-center gap-3 sticky top-2 z-20">
      {/* 取消修改 button (left) | status pill (center) | 一键合成 + 确认修改 (right) */}
    </section>

    {/* 双列：左 video+ops / 右 tab+list */}
    <div className="grid grid-cols-1 lg:grid-cols-[minmax(360px,400px)_1fr] gap-4">
      {/* 左列 */}
      <aside className="lg:sticky lg:top-[calc(var(--header-height)+1rem)] lg:self-start space-y-3">
        <div data-sticky-video>
          <video
            ref={videoRef}
            className="w-full aspect-video rounded-md bg-black object-contain"
            controls
            preload="metadata"
            src={buildStreamUrl(jobId, "video")}
            poster={buildStreamUrl(jobId, "poster")}
            aria-label="译制视频"
          />
        </div>
        <CurrentSegmentOpsPanel ... />
      </aside>

      {/* 右列 */}
      <div className="space-y-3">
        {/* Tab nav — 见 §7，移到右列上方 */}
        <nav className="flex items-center gap-1 border-b border-border" role="tablist">
          {/* 翻译修改 / 音色修改 */}
        </nav>

        {activeTab === "text" ? (
          <main id="panel-text" role="tabpanel">
            {/* 批量操作 + 异常摘要 */}
            <section className="surface-card p-4 ...">{/* 同现有 */}</section>
            {/* 段落列表（虚拟） */}
            <section aria-label="段落编辑区">
              <SegmentVirtualList
                ref={virtualListRef}
                items={resource.segments}
                getId={(s) => s.segment_id}
                activeSegmentId={activeSegmentId}
                stickyOffsetForAutoScroll={isMobile ? videoHeight : 0}
                renderItem={(seg, idx) => <SegmentRow ... />}
              />
            </section>
          </main>
        ) : (
          <main id="panel-voice" role="tabpanel">
            <VoiceModifyTab ... />
          </main>
        )}
      </div>
    </div>

    {/* Modals */}
    {commitModalOpen && <CommitModal ... />}
    {audioSyncConflict && <AudioSyncConflictModal ... />}
    <EditPageSpeakerCreateDialog ... />
  </div>
)
```

- [ ] **Step 5.3：实现 isMobile + videoHeight**

```ts
const [videoHeight, setVideoHeight] = useState(0)
const [isMobile, setIsMobile] = useState(false)
const stickyVideoRef = useRef<HTMLDivElement | null>(null)

useEffect(() => {
  const mq = window.matchMedia("(max-width: 1023px)")
  const update = () => setIsMobile(mq.matches)
  update()
  mq.addEventListener("change", update)
  return () => mq.removeEventListener("change", update)
}, [])

useEffect(() => {
  if (!isMobile) return
  const el = stickyVideoRef.current
  if (!el) return
  const obs = new ResizeObserver(() => setVideoHeight(el.offsetHeight))
  obs.observe(el)
  return () => obs.disconnect()
}, [isMobile])
```

把 `data-sticky-video` div ref 改为 `stickyVideoRef`。

- [ ] **Step 5.4：实现首次进入自动定位首段 dirty（§2.4）**

在 loadData 完成 + activeSegmentId 还是 null 时：

```ts
useEffect(() => {
  if (!resource || activeSegmentId) return
  const firstDirty = resource.segments.find((s) => {
    const st = resource.segment_status[s.segment_id]
    return st === "text_dirty" || st === "voice_dirty" || st === "tts_failed" || st === "tts_dirty"
  })
  const target = firstDirty?.segment_id ?? resource.segments[0]?.segment_id
  if (target && videoRef.current) {
    virtualListRef.current?.scrollToId(target, { align: "center", stickyOffset: isMobile ? videoHeight : 0 })
    const seg = resource.segments.find((s) => s.segment_id === target)
    if (seg) videoRef.current.currentTime = seg.start_ms / 1000
  }
}, [resource, isMobile, videoHeight])  // activeSegmentId 故意不在 deps，避免循环
```

- [ ] **Step 5.5：实现取消修改 + 批量 race（§8b.2）**

修改 handleCancelEditing：

```ts
const handleCancelEditing = useCallback(async () => {
  if (batchTaskId !== null) {
    const ok = window.confirm("正在批量合成，取消修改会丢弃当前进度。继续？")
    if (!ok) return
    setIsCancellingBatch(true)
    try {
      await cancelBatchRegenerate(jobId, batchTaskId)
      // 等 batch 退出（轮询 task status，10s 上限）
      await waitForBatchExit(jobId, batchTaskId, 10_000)
    } catch (err) {
      // 超时也强制继续 — 后端处理 orphan (§11 risk)
      console.warn("batch cancel wait timed out", err)
    } finally {
      setIsCancellingBatch(false)
    }
  }
  await editingCancel(jobId)
  router.push(`/workspace/${jobId}`)
}, [jobId, batchTaskId, router])
```

`waitForBatchExit` 是新 helper（lib/api/editing.ts 加），轮询 task status until status != running。

- [ ] **Step 5.6：类型检查 + lint**

```bash
cd frontend-next && npx tsc --noEmit && npm run lint
wc -l src/app/\(app\)/workspace/\[jobId\]/edit/page.tsx
```

Expected: tsc 0 errors，page.tsx 行数 < 1200（设计文档 §9.1 守卫上限）

- [ ] **Step 5.7：桌面手测 smoke**

```bash
cd frontend-next && npm run dev
```

测试清单（设计文档 §9.3）：
- [ ] Light + Dark 主题切换都不破
- [ ] 左右分栏正常显示
- [ ] sticky video 滚动列表时锚定不动
- [ ] 进入页面自动定位首段 dirty + 视频 seek
- [ ] 点段：active 高亮 + 视频 seek + 左侧 ops panel 跟随
- [ ] 改文本 → 待合成按钮变红
- [ ] 重合成单段 → 草稿面板出现 → 接受/丢弃 OK
- [ ] Tab 切换：翻译 ↔ 音色，视频不变

- [ ] **Step 5.8：移动端手测（Chrome DevTools 模拟 iPhone）**

- [ ] 视频 sticky 顶部 30vh
- [ ] 列表自动滚到首段 dirty，不被视频遮
- [ ] 段落行竖向堆叠 OK

- [ ] **Step 5.9：提交**

```bash
git add frontend-next/src/app/\(app\)/workspace/\[jobId\]/edit/page.tsx \
        frontend-next/src/lib/api/editing.ts
git commit -m "feat(edit): rearrange to left-video + right-list layout

- Desktop ≥ 1024px: 2-col grid (sticky video left, list right)
- Mobile < 1024px: stacked (sticky video top 30vh + list below)
- Auto-locate first dirty on mount + scroll with stickyOffset
- Cancel-editing serializes batch cancel first (§8b.2)
- Tab moved into right column (video stays visible across tabs)

page.tsx: 1500 → ~1100 lines.
Plan ref: docs/plans/2026-05-17-edit-page-redesign.md §2.1, §2.4, §8a, §8b.2"
```

---

## Task 6 · 新组件 SplitSegmentDialog（Phase 1 单切点）

> 设计文档 §5 + §5.5。Phase 1 maxCuts=1，UX hint「多说话人多段拆分将在下个版本支持」。

**Files**：
- Create: `frontend-next/src/app/(app)/workspace/[jobId]/edit/SplitSegmentDialog.tsx`

- [ ] **Step 6.1：用 shadcn Dialog 写骨架**

```tsx
"use client"

import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog"
import type { EditingSegment } from "@/types/editing"

export interface SplitSegmentDialogProps {
  open: boolean
  segment: EditingSegment | null
  availableSpeakers: string[]
  maxCuts?: number  // Phase 1 硬传 1；Phase 2 可不传 = 不限
  onClose(): void
  onSubmit(payload: { split_source_index: number; split_cn_index: number; speaker_a: string; speaker_b: string }): Promise<void>
}

export function SplitSegmentDialog(props: SplitSegmentDialogProps) {
  // ...
}
```

主要交互：
1. 显示英文 + 中文文本
2. 用户点击文本中位置插入切点（每个字符间隙都可点）
3. 英文切点 → 按字符比例映射到中文（粗略）
4. Phase 1：maxCuts=1，第二次点会先清掉旧切点
5. 预览区：拆分后的 2 段（① 时间 / 说话人下拉 / 内容预览）
6. 底部 hint（设计文档 §5.5 灰字）：「目前支持拆分为 2 段。一段内出现多个说话人时，多段一次性拆分将在下个版本支持。」
7. 提交按钮：「拆分为 2 段」disabled until 切点已选

- [ ] **Step 6.2：接入 page.tsx**

在 page.tsx 加 state：

```ts
const [splitDialogSegmentId, setSplitDialogSegmentId] = useState<string | null>(null)
```

SegmentRow 的 `onSplit` 改成 `(sid) => setSplitDialogSegmentId(sid)`。

底部加：

```tsx
<SplitSegmentDialog
  open={splitDialogSegmentId !== null}
  segment={resource.segments.find((s) => s.segment_id === splitDialogSegmentId) ?? null}
  availableSpeakers={availableSpeakerIds}
  maxCuts={1}
  onClose={() => setSplitDialogSegmentId(null)}
  onSubmit={async (payload) => {
    await handleSplitSegment(splitDialogSegmentId!, payload)
    setSplitDialogSegmentId(null)
  }}
/>
```

- [ ] **Step 6.3：删除现有内联 split 交互**

找 page.tsx 里现有的 handleSplitSegment + UI（如果有 inline split selector，整段删掉，全部走 modal）。

- [ ] **Step 6.4：类型检查 + lint + 手测**

```bash
cd frontend-next && npx tsc --noEmit && npm run lint
```

手测：
- 点拆分按钮 → modal 弹出
- 在英文文本里点中间 → 红色切点 + 中文映射
- 提交 → API 调通 → 列表刷新出 2 段
- 取消 → modal 关闭，状态未变

- [ ] **Step 6.5：提交**

```bash
git add frontend-next/src/app/\(app\)/workspace/\[jobId\]/edit/{SplitSegmentDialog.tsx,page.tsx}
git commit -m "feat(edit): add SplitSegmentDialog (Phase 1 single-cut)

Modal-based split UI replacing the inline selector. maxCuts=1 hard cap
+ explicit hint for users about Phase 2 multi-cut. Reuses existing
backend POST /jobs/{id}/segments/{sid}/split (no backend change).

Plan ref: docs/plans/2026-05-17-edit-page-redesign.md §5 + §5.5"
```

---

## Task 7 · Python AST 契约守卫（设计文档 §9.1）

**Files**：
- Create: `tests/test_edit_page_redesign_guards.py`

- [ ] **Step 7.1：写测试骨架**

```python
"""Phase 1 redesign contract guards — AST scans, no UI framework needed."""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EDIT_DIR = REPO_ROOT / "frontend-next" / "src" / "app" / "(app)" / "workspace" / "[jobId]" / "edit"
EDIT_PAGE = EDIT_DIR / "page.tsx"
COMPONENTS_DIR = REPO_ROOT / "frontend-next" / "src" / "components" / "workspace" / "edit"
VL_PATH = REPO_ROOT / "frontend-next" / "src" / "components" / "workspace" / "segments" / "SegmentVirtualList.tsx"


def test_page_tsx_under_line_threshold():
    """Phase 1 守卫：page.tsx 必须从 2127 行降到 < 1200。"""
    text = EDIT_PAGE.read_text(encoding="utf-8")
    n = text.count("\n") + 1
    assert n < 1200, f"page.tsx is {n} lines; expected < 1200 after extracting SegmentRow + CurrentSegmentOpsPanel + SplitSegmentDialog"


def test_new_components_exist_and_export_default():
    """三个新组件必须都存在 + export."""
    targets = [
        (COMPONENTS_DIR / "SegmentRow.tsx", "SegmentRow"),
        (COMPONENTS_DIR / "CurrentSegmentOpsPanel.tsx", "CurrentSegmentOpsPanel"),
        (EDIT_DIR / "SplitSegmentDialog.tsx", "SplitSegmentDialog"),
    ]
    for path, name in targets:
        assert path.is_file(), f"missing {path}"
        text = path.read_text(encoding="utf-8")
        assert re.search(rf"export\s+(function|const)\s+{name}\b", text), \
            f"{path.name} does not export {name}"


def test_page_imports_segment_row():
    """page.tsx 必须 import SegmentRow（验证已接入）."""
    text = EDIT_PAGE.read_text(encoding="utf-8")
    assert "SegmentRow" in text, "page.tsx must import SegmentRow"
    assert re.search(r"import\s*\{[^}]*SegmentRow[^}]*\}\s*from", text), \
        "SegmentRow must be named-imported"


def test_page_no_inline_segment_card():
    """page.tsx 不应再有 1300+ 行的内联 SegmentCard function."""
    text = EDIT_PAGE.read_text(encoding="utf-8")
    # 允许在注释里提及 SegmentCard（如 commit message 历史 / 旧字段名）
    # 但不允许 `function SegmentCard(...)`  declaration
    assert not re.search(r"^\s*function\s+SegmentCard\s*\(", text, re.MULTILINE), \
        "Inline `function SegmentCard` should be removed in Phase 1"


def test_segment_row_no_hex_color_literals():
    """SegmentRow 不应硬写 hex 颜色字面量（必须用 var(--xxx) tokens）."""
    text = (COMPONENTS_DIR / "SegmentRow.tsx").read_text(encoding="utf-8")
    # 找 #RRGGBB 或 #RGB（class 名里的 # 不算，限 quoted string 内）
    hex_matches = re.findall(r'#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b', text)
    assert not hex_matches, f"SegmentRow.tsx contains hex color literals: {hex_matches}; use var(--xxx) tokens instead"


def test_segment_virtual_list_has_sticky_offset():
    """SegmentVirtualList.scrollToId 接受 stickyOffset 参数（Task 1）."""
    text = VL_PATH.read_text(encoding="utf-8")
    assert "stickyOffset" in text, "SegmentVirtualList missing stickyOffset support"
```

- [ ] **Step 7.2：跑测试**

```bash
python -m pytest tests/test_edit_page_redesign_guards.py -v
```

Expected: 6 passed

如果挂掉某项 → 回头补对应代码 / 调上面 task 直到通过。

- [ ] **Step 7.3：提交**

```bash
git add tests/test_edit_page_redesign_guards.py
git commit -m "test(edit): AST contract guards for Phase 1 redesign

Six checks:
- page.tsx < 1200 lines (was 2127)
- 3 new components exist + export
- page.tsx imports SegmentRow + no inline SegmentCard
- SegmentRow uses CSS vars (no hex literals)
- SegmentVirtualList has stickyOffset

Plan ref: docs/plans/2026-05-17-edit-page-redesign.md §9.1"
```

---

## Task 8 · 状态映射回归测试

**Files**：
- Create: `tests/test_edit_page_state_mapping.py`

- [ ] **Step 8.1：写测试**

```python
"""Phase 1 守卫：6 内部状态 → 5 视觉表达映射不漂移。

设计文档 §6.1 矩阵：
  accepted        → "重合成"     ghost
  text_dirty      → "待合成"     primary
  voice_dirty     → "待合成"     primary  (同 text_dirty)
  tts_loading     → "合成中…"   warn + spinner
  tts_dirty       → "草稿待审 ↓" warn
  tts_failed      → "重试合成"   danger
"""

from services.jobs.editing_segments import SUPPORTED_SEGMENT_STATUSES

# 期望的全集（与 setting.py SEGMENT_STATUS_* 常量对齐）
EXPECTED_STATUSES = frozenset({
    "accepted",
    "text_dirty",
    "tts_loading",
    "tts_dirty",
    "tts_failed",
    "voice_dirty",
})


def test_status_vocabulary_unchanged():
    """前端 5 态文案依赖这 6 个状态全集 — 任何后端新增/删除状态都要先改前端映射。"""
    assert SUPPORTED_SEGMENT_STATUSES == EXPECTED_STATUSES, (
        f"Segment status vocab changed. "
        f"Before adding/removing, update frontend SegmentRow button mapping. "
        f"Expected {EXPECTED_STATUSES}, got {SUPPORTED_SEGMENT_STATUSES}"
    )
```

- [ ] **Step 8.2：跑测试**

```bash
python -m pytest tests/test_edit_page_state_mapping.py -v
```

Expected: 1 passed

- [ ] **Step 8.3：提交**

```bash
git add tests/test_edit_page_state_mapping.py
git commit -m "test(edit): regression guard for segment status vocab

Frontend SegmentRow maps 6 backend statuses to 5 visual buttons. If
backend status vocab drifts (status added/removed), frontend mapping
silently misrenders. This test fails fast on any vocab change.

Plan ref: docs/plans/2026-05-17-edit-page-redesign.md §6.1 + §9.0 (1)"
```

---

## Task 9 · 完整手测 smoke + 截图比对

**Files**：无新文件，纯验证。

- [ ] **Step 9.1：跑设计文档 §9.3 全套 smoke checklist**

```bash
cd frontend-next && npm run dev
```

测试场景（每条挂掉就回到上面对应 task 补）：

- [ ] **桌面 Chrome**：进修改页 → 首段 dirty 自动定位 → 改文本 → 待合成 → 接受草稿
- [ ] **桌面 Firefox**：同上
- [ ] **移动端**（DevTools 模拟 iPhone 14 Pro）：堆叠布局 + 视频 sticky + scrollToId 不被遮
- [ ] **Light + Dark 主题切换**：颜色无 hard-coded 残留（Task 7 守卫 + 视觉确认）
- [ ] **拆分 modal**（Phase 1 单切点）：切点交互 + 提交 + 关闭 → 状态正确
- [ ] **批量合成 + 中途取消**：单段按钮在批量期间 disable + 取消后恢复
- [ ] **取消修改 + 批量 in-flight**：confirm dialog + 序列化 cancel 生效

- [ ] **Step 9.2：截图存档（可选）**

在 `D:\Claude\temp\` 下截图保存：
- desktop-light-edit-page.png
- desktop-dark-edit-page.png
- mobile-light-edit-page.png

（项目无 visual regression 工具，这步只是档案，不入 commit）

- [ ] **Step 9.3：跑 lint + type check 最终一次**

```bash
cd frontend-next && npx tsc --noEmit && npm run lint && npm run build
```

Expected: 0 errors，build 成功。

- [ ] **Step 9.4：跑全套 Python 测试**

```bash
python -m pytest tests/test_edit_page_redesign_guards.py tests/test_edit_page_state_mapping.py -v
```

Expected: all passed

- [ ] **Step 9.5：可选 — 整理 Phase 1 commit 历史**

如果中途有 fixup commit 可以 squash。但项目偏好独立 commit + 不 rebase published，所以默认保留。

---

## Phase 1 完成验收

全部 task 后：

| 验收项 | Pass 标准 |
|--------|----------|
| page.tsx 行数 | < 1200（从 2127）|
| 3 个新组件文件 | 都存在 + export default |
| Python 守卫测试 | 7 个全 pass |
| 桌面 light + dark 主题 | 视觉一致，无 hex 残留 |
| 移动端响应式 | < 1024px 自动堆叠 + sticky 视频不遮列表 |
| 拆分 modal | 单切点 Phase 1 可用 + Phase 2 hint 显示 |
| 取消修改 race | 批量进行时弹 confirm + 序列化处理 |
| 批量进行时单段 disable | tooltip 提示 |
| 自动定位首段 dirty | 进入页面立即定位 + 视频 seek |
| 主路径手测 | 改文本 → 合成 → 接受草稿全链路通 |

Phase 1 落地后接 Phase 2（split-many 后端 + source-video stream + 智能预填 + 产物普查），单独 plan 文档。

---

## 备注

- 每个 task **独立 commit**（项目偏好原子 commit）
- 每个 task 完成后跑 `python -m pytest tests/test_edit_page_redesign_guards.py -v` 看守卫态势
- 如果遇到设计文档没覆盖的边界 → 先停下来回查 [设计文档](2026-05-17-edit-page-redesign.md)，无定义则提问，不擅自决定
- 付费 API 不会被这 Phase 触发（不动后端 + 不调 TTS / clone）
