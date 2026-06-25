# TU-10 · 前端编辑页 route shell 化

- **目标 / 价值**：`VideoEditPage`（`page.tsx`）当前 1,975 行、74 处 hook 调用、35+ `useState`，既是路由 shell 又内嵌了全部业务逻辑，极难阅读与测试。本单元将 page.tsx 降到约 400 行纯 shell（组合 hooks + 组合组件），同时修复 FE-002 根因（`TranslationForm` 内 3 处 `set-state-in-effect` → `handleSubmit` 条件求值），并将 eslint 的 4 条 `react-hooks/*` 规则从 `warn` 恢复为 `error`，使 lint 能在 CI 阻断同类退化。
- **关联发现**：FE-001（page.tsx 1,975 行超限、轮询逻辑重复）、FE-002（`TranslationForm.tsx` 3 处 `set-state-in-effect`、eslint rules 降级为 warn）
- **前置依赖**：无（可并行）
- **建议分支**：`quality/edit-page-shell`
- **预估工时**：L（建议分段：Step 1–3 一个 PR ≈ M，Step 4–6 一个 PR ≈ M）

> **命令环境**：默认 Git Bash / CI Linux（仓库已配 Bash 工具）；PowerShell 执行者改用等价命令（`grep` → `Select-String`、`wc -l` → `(Get-Content <file> | Measure-Object -Line).Lines`、`test -f` → `Test-Path`、避免 `<(...)` 进程替换）。

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **consent 清除移入事件 handler**：保留"离开 free mode 时自动清除授权勾选"的产品语义，但实现从 `useEffect` 改为模式切换事件 handler。新增 `selectServiceMode(next: ServiceMode)` helper，在 `next !== "free"` 时调用 `setFreeVoiceRightsConfirmed(false)`（类似逻辑对 express/smart consent 同理），所有方案卡片点击改调该 helper 而非直接 `setServiceMode`。
- **不再使用 `effective*` 局部常量方案**：Step 1 不采用"在 `handleSubmit` 内以 `effective*` 局部常量屏蔽旧 state"的做法；而是在用户切换模式时即时清除对应 consent state，`handleSubmit` 直接读取真实 state，无需 `effective*` 包装层。
- **三处 `useEffect` 均替换为 handler 调用**：1a（express consent）、1b（free consent）、1c（smart consent）三处 `useEffect` 块全部删除，对应的 `setXxx(false)` 调用移入 `selectServiceMode` helper；payload 构建和 `validationError` 逻辑无需变更。
- **eslint 升级前必须先全仓扫描**：`react-hooks/*` 规则从 `warn` 改 `error` 前，先执行 `npm run lint 2>&1 | grep "react-hooks/"` 统计全仓遗留违规数量；若仍有违规，本次保持 `warn` 并输出追踪清单，不得用 `// eslint-disable-next-line` 大量掩盖；清零后再单独 PR 升为 `error`。
- **不引入新依赖**：提取的 hooks 和组件不引入 axios / react-query / zustand / jotai 等重量级库，维持现有薄 fetch wrapper 约定。

---

## 不在本单元范围（out-of-scope）

- **Python 后端**改动：本单元只动 `frontend-next/` 下的文件。
- **SegmentRow / CurrentSegmentOpsPanel** 内部重构：这两个组件本单元不改逻辑，仅保留现有接口。
- **新增业务功能**（如拆分段落、批量导出等）：本单元不新增功能，只迁移已有逻辑。
- **`TranslationForm` 的其他重构**：仅修 FE-002 中明确列出的 3 处 `set-state-in-effect`；`TranslationForm` 其余 1,200+ 行不动。
- **E2E / Playwright 测试补全**：不在本单元范围，后续独立任务。
- **`usePollingTask` 本身**重写：现有 hook 接口不变，仅复用它。

---

## 必守不变量

1. **付费 API 红线**：`VideoEditPage` 及其拆出的 hooks/组件一律不在 `catch`/fallback/`useEffect` 自动触发 MiniMax clone、TTS、LLM、ASR 付费调用。re-TTS 只能由用户显式点击按钮触发。
2. **Alignment / DSP-first**：本单元不接触 `alignment`、`publish` 相关 pipeline 代码；前端 commit 流程只调用已有 `commitEditing` API，不绕过。
3. **剪映 draft 不得外露**：`editing/commit` 的 `copy_as_new` / `overwrite` 策略选择逻辑不在本单元改变，原样保留。
4. **Gateway 是 plan/pricing/entitlement 唯一真源**：前端读取 entitlement/credits 的 API 调用路径不变，hooks 只是封装调用，不引入本地缓存或绕过 Gateway 的判断。
5. **TypeScript strict + 薄 fetch wrapper**：拆出的 hooks 和组件必须通过 `tsc --noEmit`，不引入 axios / react-query / zustand / jotai 等重量级库。
6. **process.py Option B 约定**（前端侧对应）：前端组件结构改变不得破坏后端 `editing/commit` 的幂等契约；`commitEditing` 调用点保持单一。
7. **默认测试不接真实外部服务**：提取的 hooks 中若含异步 fetch，测试层用 `jest.fn()`/`vi.fn()` mock，不发真实 HTTP 请求。

---

## Step 0 · 确认现状

```bash
# 建分支
git switch -c quality/edit-page-shell

# 确认 page.tsx 行数（spec 写 1,975，以实际为准）
wc -l frontend-next/src/app/\(app\)/workspace/\[jobId\]/edit/page.tsx
# 预期输出：1975 ...

# 确认 TranslationForm.tsx 行数（spec 写 1,278，以实际为准）
wc -l frontend-next/src/components/workspace/TranslationForm.tsx
# 预期输出：1278 ...

# 确认 3 处 set-state-in-effect 的行号（以实际为准）
grep -n "useEffect" frontend-next/src/components/workspace/TranslationForm.tsx
# 预期：行 293、301、307 是三处目标 useEffect（serviceMode → consent reset）

# 确认 eslint.config.mjs 中 4 条规则当前为 warn
grep -n "react-hooks" frontend-next/eslint.config.mjs
# 预期：set-state-in-effect/preserve-manual-memoization/purity/immutability 均 "warn"

# 确认 lib/react/ 下现有 hooks
ls frontend-next/src/lib/react/
# 预期：useBackgroundTask.ts  useIsMountedRef.ts  usePlayerSegmentSync.ts  usePollingTask.ts

# 确认编辑页子组件目录
ls frontend-next/src/app/\(app\)/workspace/\[jobId\]/edit/
ls frontend-next/src/components/workspace/edit/
# 预期 edit/ 已有：SplitSegmentDialog.tsx  VoiceModifyTab.tsx  page.tsx（route目录）
#              components/workspace/edit/：CurrentSegmentOpsPanel.tsx  SegmentRow.tsx

# 记录 VideoEditPage 内 useState 数量（基线：35）
grep -c "useState" frontend-next/src/app/\(app\)/workspace/\[jobId\]/edit/page.tsx
# 预期：35

# 记录 hook 调用总数（基线：74）
grep -c "useState\|useEffect\|useCallback\|useRef\|useMemo" \
  frontend-next/src/app/\(app\)/workspace/\[jobId\]/edit/page.tsx
# 预期：74
```

> ⚠️ **行号说明**：spec 给出的 `TranslationForm.tsx:293-311` 经实际核查对应行 293（express consent reset）、301（free consent reset）、307（smart consent reset）共 3 个 `useEffect`。eslint.config.mjs 4 条规则位于当前第 14–17 行。以 `grep -n` 实际输出为准。

---

## Step 1 · FE-002 根因修复：删除 TranslationForm 中 3 处 set-state-in-effect

**背景**：`TranslationForm.tsx` 中 3 处 `useEffect` 仅因 `serviceMode` 变化而强制将 consent checkbox 重置为 `false`。这是典型的 `set-state-in-effect` 模式：effect 的唯一作用是"根据另一个 state 派生出当前 state"，等价于在渲染时条件求值。React compiler lint 标记此为反模式（extra re-render + 执行顺序难以推理）。

**修改文件**：`frontend-next/src/components/workspace/TranslationForm.tsx`

**改法**（以下四步顺序执行）：

**1a. 新增 `selectServiceMode` helper**（在三处 `useEffect` **之前**，约第 290 行附近插入）：

```ts
// 替代直接调用 setServiceMode 的统一入口
const selectServiceMode = useCallback((next: typeof serviceMode) => {
  setServiceMode(next)
  // 离开对应 mode 时即时清除对应 consent state，保留产品语义
  if (next !== "express" || !expressAutoCloneAvailable) {
    setExpressAutoVoiceClone(false)
  }
  if (next !== "free") {
    setFreeVoiceRightsConfirmed(false)
  }
  if (next !== "smart" || voiceCloneCostCredits == null) {
    setSmartPaidCloneAccepted(false)
  }
}, [expressAutoCloneAvailable, voiceCloneCostCredits])
```

**1b. 删除 express consent reset effect（当前约第 293–299 行）：**

删除整个 `useEffect` 块：
```ts
// 删除以下 7 行
useEffect(() => {
  if (serviceMode !== "express" || !expressAutoCloneAvailable) {
    setExpressAutoVoiceClone(false)
  }
}, [serviceMode, expressAutoCloneAvailable])
```

**1c. 删除 free consent reset effect（当前约第 301–305 行）：**

删除整个 `useEffect` 块：
```ts
// 删除以下 5 行
useEffect(() => {
  if (serviceMode !== "free") {
    setFreeVoiceRightsConfirmed(false)
  }
}, [serviceMode])
```

**1d. 删除 smart consent reset effect（当前约第 307–311 行）：**

删除整个 `useEffect` 块：
```ts
// 删除以下 5 行
useEffect(() => {
  if (serviceMode !== "smart" || voiceCloneCostCredits == null) {
    setSmartPaidCloneAccepted(false)
  }
}, [serviceMode, voiceCloneCostCredits])
```

**1e. 把所有方案卡片点击处的 `setServiceMode(...)` 替换为 `selectServiceMode(...)`**：

在 JSX 中搜索所有 `setServiceMode(`（用于响应卡片点击 / radio 选择的调用），全部改为 `selectServiceMode(`。`handleSubmit` 内部无需修改——直接读取真实 state 即可，UI 层切换时已即时清除不匹配的 consent state。

> ✅ **已决策（CodeX 2026-06-25）**：保留"离开 free mode 自动清除授权勾选"的产品语义，清除逻辑从 `useEffect` 移入模式切换事件 handler（`selectServiceMode`）。`handleSubmit` 直接读取真实 state，不引入 `effective*` 局部常量。`validationError`（约第 141–145 行）的渲染路径不变，checkbox 视觉状态在用户切换模式时即时清除，行为与原先一致且无额外 re-render。

**该步验收**：
```bash
# 1. 3 处目标 useEffect 已删除（useEffect 总数比 Step 0 基线减少 3）
grep -c "useEffect" frontend-next/src/components/workspace/TranslationForm.tsx
# 原基线 5 处 useEffect，删除 3 处后应为 2

# 2. selectServiceMode helper 存在
grep -c "selectServiceMode" \
  frontend-next/src/components/workspace/TranslationForm.tsx
# 预期：>= 2（定义 1 处 + 至少 1 处调用）

# 3. JSX 中无裸 setServiceMode 调用（卡片点击处已替换）
grep -n "setServiceMode(" \
  frontend-next/src/components/workspace/TranslationForm.tsx \
  | grep -v "useState\|const \[" | grep -v "selectServiceMode"
# 预期：0 行（仅 useState 声明行，无裸调用）

# 4. TypeScript 编译通过
cd frontend-next && npx tsc --noEmit 2>&1 | grep -c "error TS" ; cd ..
# 预期：0
```

---

## Step 2 · eslint react-hooks 规则升级为 error（前置全仓扫描）

**修改文件**：`frontend-next/eslint.config.mjs`（当前第 14–17 行）

**改法（两阶段，以全仓扫描结果决定本次操作）**：

**2a. 先执行全仓扫描**（在修改 eslint.config.mjs 之前）：
```bash
cd frontend-next
# 以当前 warn 配置扫全仓，统计遗留违规
npm run lint 2>&1 | grep "react-hooks/" | grep -v "TranslationForm"
# 若输出 0 行 → 本步可直接升为 error（见 2b-升级路径）
# 若输出 > 0 行 → 本步保持 warn，输出违规清单追踪（见 2b-追踪路径）
cd ..
```

**2b-升级路径（扫描结果为 0 遗留违规时）**：将 4 条规则从 `"warn"` 改为 `"error"`：
```js
"react-hooks/set-state-in-effect": "error",
"react-hooks/preserve-manual-memoization": "error",
"react-hooks/purity": "error",
"react-hooks/immutability": "error",
```

**2b-追踪路径（扫描结果仍有遗留违规时）**：规则本次保持 `"warn"`，在 PR 描述中附上违规清单，规划单独清零 PR；**不得**用 `// eslint-disable-next-line` 大量掩盖遗留违规。升级为 `"error"` 作为后续独立任务，在清零后再单独提 PR。

> ✅ **已决策（CodeX 2026-06-25）**：eslint 从 warn 改 error 前必须先全仓扫描；若仍有遗留违规，保持 warn 并建立追踪清单，不用大量 `disable` 注释掩盖。清零后再单独 PR 升级。Step 1 完成（TranslationForm 的 `selectServiceMode` handler 替换 3 处 useEffect）之后执行本步。

**该步验收**：
```bash
cd frontend-next

# 1. 全仓扫描遗留违规数（Step 1 后执行）
npm run lint 2>&1 | grep "react-hooks/" | grep -v "TranslationForm" | wc -l
# 输出 0 → 升级路径；输出 > 0 → 追踪路径（记录到 PR 描述）

# 2a. 若走升级路径：确认规则为 error
grep "react-hooks/set-state-in-effect" eslint.config.mjs | grep -c '"error"'
# 预期：1

# 2b. 若走升级路径：lint 通过，react-hooks error 无新增报告
npm run lint 2>&1 | grep -c "react-hooks/set-state-in-effect"
# 预期：0

cd ..
```

---

## Step 3 · 提取 useEditingJobSync hook

将 page.tsx 中负责「加载任务+进入 editing 状态」的逻辑（约第 119–380 行中，`isLoading` / `pageError` / `job` / `resource` / `voiceMap` / `speakerNameMap` 相关 state + `loadData` + `enterAttemptedRef` + 初始化 `useEffect`）提取到新文件。

**新文件**：`frontend-next/src/lib/react/useEditingJobSync.ts`

**Hook 签名（目标）**：
```ts
export interface EditingJobSyncState {
  job: JobSummary | null
  resource: EditingSegmentsResponse | null
  voiceMap: Record<string, VoiceMapEntry>
  speakerNameMap: Record<string, string>
  isLoading: boolean
  pageError: string | null
  reload: () => Promise<void>
  patchResource: (updater: (prev: EditingSegmentsResponse) => EditingSegmentsResponse) => void
}

export function useEditingJobSync(jobId: string): EditingJobSyncState
```

**迁移范围（按 page.tsx 行号，以 Step 0 grep 结果为准）**：
- state: `job`（122）、`resource`（123）、`isLoading`（124）、`pageError`（125）、`voiceMap`（150）、`speakerNameMap`（156）
- ref: `enterAttemptedRef`（338）
- callback: `loadData`（219–269）
- effect: 初始化 useEffect（约第 339–377，包含 `enterEditing` 调用逻辑）

**约定**：
- `loadData` 重命名为 `reload` 并暴露到返回值。
- `patchResource` 是可选快捷方式，供 segment 操作后做乐观 patch；内部调用 `setResource`。
- hook 内部不做 `toast`——错误通过 `pageError` 返回，toast 在 page.tsx 消费。
- `isMountedRef` 通过 `useIsMountedRef()` 在 hook 内部自建，不从外部传入。

**该步验收**：
```bash
# 1. 新文件存在
test -f frontend-next/src/lib/react/useEditingJobSync.ts && echo "exists"
# 预期：exists

# 2. page.tsx 行数下降（删除约 120 行 state+effect+callback）
wc -l frontend-next/src/app/\(app\)/workspace/\[jobId\]/edit/page.tsx
# 预期：< 1860（原 1975 - 迁出约 120 行）

# 3. TypeScript 编译通过
cd frontend-next && npx tsc --noEmit 2>&1 | grep -c "error TS" ; cd ..
# 预期：0
```

---

## Step 4 · 提取 useEditingSegments + useBulkReplace hook

将 page.tsx 中 segment 操作 handler（handleTextChange / handleSpeakerChange / handleSourceTextChange / handleSplitSegmentMany / handlePreviewSource / handleRegenerate / handleAcceptDraft / handleDiscardDraft）和批量替换（bulkReplaceOpen / bulkReplaceFind / bulkReplaceValue / bulkReplacePreview 相关 state + handleBulkReplacePreview / handleBulkReplaceApply）分两个 hook 提取。

**新文件 A**：`frontend-next/src/lib/react/useEditingSegments.ts`

迁移范围（按 page.tsx 行号，以 Step 0 grep 结果为准）：
- state: `savingSegmentIds`（126）、`regeneratingSegmentIds`（127）
- callbacks: `handleTextChange`（378）、`handleSpeakerChange`（414）、`handleSourceTextChange`（470）、`handleSplitSegmentMany`（503）、`handlePreviewSource`（565）、`handleRegenerate`（597）、`handleAcceptDraft`（630）、`handleDiscardDraft`（644）
- derived: `availableSpeakerIds`（582）

Hook 依赖入参：`jobId: string`、`resource: EditingSegmentsResponse | null`、`patchResource: (...) => void`、`editingSpeakersRef: React.MutableRefObject<EditingSpeaker[]>`

**新文件 B**：`frontend-next/src/lib/react/useBulkReplace.ts`

迁移范围：
- state: `bulkReplaceOpen`（131）、`bulkReplaceFind`（132）、`bulkReplaceValue`（133）、`bulkReplacePreview`（134）、`isBulkReplacePreviewing`（135）、`isBulkReplaceApplying`（136）
- callbacks: `handleBulkReplacePreview`（809）、`handleBulkReplaceApply`（832–958，含内联轮询循环）

Hook 依赖入参：`jobId: string`、`isBatchRegenerating: boolean`、`setIsBatchRegenerating: (v: boolean) => void`、`setBatchTaskId: (id: string | null) => void`、`setIsCancellingBatch: (v: boolean) => void`、`isMountedRef`、`loadData: () => Promise<void>`

> **关于 handleBulkReplaceApply 内联轮询**：该函数内部（约第 875–930 行）有一段与 `handleBatchRegenerate`（约第 680–761 行）结构近似的轮询循环。两者在此步骤中分别被提取到 `useBulkReplace` 和下一步的 `useCommitFlow` 内，内联代码保持不变。待 Step 6 验收 page.tsx 行数后，可选做 DRY 提取（单独小 PR），不作为本单元 DoD 要求。

**该步验收**：
```bash
# 1. 两个新文件存在
test -f frontend-next/src/lib/react/useEditingSegments.ts && echo "A ok"
test -f frontend-next/src/lib/react/useBulkReplace.ts && echo "B ok"
# 预期：A ok / B ok

# 2. page.tsx 行数继续下降（Step 3 基础上再减约 350 行 handler + state）
wc -l frontend-next/src/app/\(app\)/workspace/\[jobId\]/edit/page.tsx
# 预期：< 1510（Step 3 后约 1860 - 迁出约 350 行）

# 3. TypeScript 编译通过
cd frontend-next && npx tsc --noEmit 2>&1 | grep -c "error TS" ; cd ..
# 预期：0
```

---

## Step 5 · 提取 useCommitFlow hook

将 commit/abandon 相关 state + handler 提取到新 hook。

**新文件**：`frontend-next/src/lib/react/useCommitFlow.ts`

迁移范围（按 page.tsx 行号，以 Step 0 grep 结果为准）：
- state: `isBatchRegenerating`（128）、`batchTaskId`（137）、`isCancellingBatch`（141）、`isCommitting`（142）、`commitModalOpen`（144）、`commitStrategy`（145）、`copyDisplayName`（146）、`audioSyncConflict`（147）、`isResolvingAudioSync`（148）
- ref: `commitInFlightRef`（143）
- callbacks: `handleBatchRegenerate`（660）、`handleCancelBatch`（786）、`handleAbandon`（959）、`handleOpenCommitModal`（975）、`commitCurrentOptions`（1005）、`handleCommit`（1020）、`handleRegenerateConflictAndCommit`（1043）、`handleRevertConflictTextAndCommit`（1069）

Hook 返回还需包含 `isBatchRegenerating` / `batchTaskId` / `isCancellingBatch` 供 `useBulkReplace` 读取（两个 hook 共享批量 TTS 状态，使用 prop drilling 方式传入 useBulkReplace，不引入 context）。

Hook 依赖入参：`jobId: string`、`resource: EditingSegmentsResponse | null`、`audioSyncConflict: UnsyncedTextSegment[] | null`、`isMountedRef`、`loadData: () => Promise<void>`、`confirm: ConfirmFn`、`router: AppRouterInstance`

**该步验收**：
```bash
# 1. 新文件存在
test -f frontend-next/src/lib/react/useCommitFlow.ts && echo "exists"
# 预期：exists

# 2. page.tsx 行数继续下降（Step 4 基础上再减约 450 行）
wc -l frontend-next/src/app/\(app\)/workspace/\[jobId\]/edit/page.tsx
# 预期：< 1060（Step 4 后约 1510 - 迁出约 450 行）

# 3. TypeScript 编译通过
cd frontend-next && npx tsc --noEmit 2>&1 | grep -c "error TS" ; cd ..
# 预期：0
```

---

## Step 6 · 提取 EditingToolbar 组件 + 合并 VideoEditPage 为 shell

**子任务 6a：提取 EditingToolbar 组件**

**新文件**：`frontend-next/src/components/workspace/edit/EditingToolbar.tsx`

将 page.tsx JSX 中顶部 sticky header section（约第 1264–1313 行，包含「返回」链接、任务标题、脏段计数 badge、「放弃修改」/「确认修改」按钮）提取为独立展示组件。

```ts
interface EditingToolbarProps {
  jobId: string
  jobTitle: string
  editGeneration: number
  dirtyCount: number
  isCommitting: boolean
  onAbandon: () => void
  onOpenCommitModal: () => void
}
export function EditingToolbar(props: EditingToolbarProps): React.ReactElement
```

**子任务 6b：拼装 VideoEditPage shell**

page.tsx 完成后应仅包含：
1. Feature flag 检查 + 早返回（当前约第 107–113 行，保留不动）
2. 各 hook 的调用（`useEditingJobSync` / `useEditingSegments` / `useBulkReplace` / `useCommitFlow` / `usePlayerSegmentSync` / `useEditingSpeakers` 等）
3. 少量派生计算（`activeSegment`、`activeSpeakerName`、`forceDspSegments` 等 useMemo）
4. 组合 JSX（`<EditingToolbar>`、`<SegmentVirtualList>`、`<CurrentSegmentOpsPanel>` 等）
5. 三个 modal 组件（`BulkReplaceModal`、`AudioSyncConflictModal`、`CommitModal`）可保留在 page.tsx 底部或迁到 `components/workspace/edit/` 子目录

**该步验收**：
```bash
# 1. EditingToolbar 组件文件存在
test -f frontend-next/src/components/workspace/edit/EditingToolbar.tsx && echo "exists"
# 预期：exists

# 2. page.tsx 最终行数目标
wc -l frontend-next/src/app/\(app\)/workspace/\[jobId\]/edit/page.tsx
# 预期：≤ 600（含三个 modal 组件内联时上限；若 modal 迁出则 ≤ 400）

# 3. useState 数量（在 VideoEditPage 函数内）大幅下降
grep -c "useState" frontend-next/src/app/\(app\)/workspace/\[jobId\]/edit/page.tsx
# 预期：≤ 5（仅剩 page 自身需要的临时 UI state；主要 state 已迁入 hooks）

# 4. TypeScript 全量编译通过（最终验证）
cd frontend-next && npx tsc --noEmit 2>&1 | grep -c "error TS" ; cd ..
# 预期：0

# 5. eslint 通过（含 react-hooks error 恢复后）
cd frontend-next && npm run lint 2>&1 | grep -c "error" ; cd ..
# 预期：0（或仅有豁免注释以外的 0 条新报错）

# 6. 端到端基准：dev build 可启动（功能不回归）
cd frontend-next && npm run build 2>&1 | tail -5 ; cd ..
# 预期：最后一行包含 "Route (app)" 或 "Build succeeded"，无 "Error:"
```

---

## 测试计划（新增 / 回归）

### 新增单元测试（建议）

为提取的 hooks 各写一个 `*.test.ts`，使用 `vitest` / `@testing-library/react` + `renderHook`，所有 API 调用用 `vi.fn()` mock：

| 文件 | 测试重点 |
|---|---|
| `useEditingJobSync.test.ts` | `pageError` 在 API 失败时正确赋值；`reload()` 调用后 `resource` 更新 |
| `useEditingSegments.test.ts` | `handleTextChange` 调用 `patchSegmentText` 并更新 savingSegmentIds |
| `useBulkReplace.test.ts` | `handleBulkReplacePreview` 在 find 为空时不调用 API；轮询在 `stage=completed` 时退出 |
| `useCommitFlow.test.ts` | `handleAbandon` 在用户取消 confirm 时不调用 `cancelEditing` |

> **注意**：这些测试属于「建议」而非本 TU DoD 的阻断项——如果时间紧，可在单独 follow-up PR 补充。但若这些 hooks 在提取过程中引入 bug，缺少测试将难以发现。建议同步写。

### 回归

以下回归在每个 Step 后执行：

```bash
# TypeScript 编译（每步必跑）
cd frontend-next && npx tsc --noEmit ; cd ..

# ESLint（Step 2 之后每步必跑）
cd frontend-next && npm run lint ; cd ..

# Next.js build smoke（Step 6 之后必跑一次）
cd frontend-next && npm run build ; cd ..
```

Python 后端测试不受本单元影响，但可作为 CI 基础护栏：
```bash
python -m pytest tests/ -q --timeout=30 -x 2>&1 | tail -5
# 预期：通过（本单元不改任何 Python 代码）
```

---

## 回滚方案

- **文件边界**：本单元全部改动限于 `frontend-next/` 下，与后端完全解耦。
- **按 Step 回滚**：每个 Step 独立 commit，回滚粒度为单 commit：
  - Step 1：`git revert <commit-step1>` 恢复 TranslationForm 3 处 useEffect、删除 `selectServiceMode` helper、恢复卡片点击处的直接 `setServiceMode` 调用
  - Step 2：`git revert <commit-step2>` 恢复 eslint.config.mjs 4 条为 warn
  - Step 3–5：直接删除新 hook 文件、恢复 page.tsx（git revert 或 git checkout -- <file>）
  - Step 6：删除 EditingToolbar.tsx、恢复 page.tsx
- **不影响生产部署**：前端构建产物独立，回滚不需要重启后端或 DB migration。

---

## 完成定义（DoD）

- [ ] Step 1 完成：`TranslationForm.tsx` 中 3 处 `set-state-in-effect` useEffect 已删除，新增 `selectServiceMode` helper 承接 consent 清除逻辑，所有卡片点击处改调 `selectServiceMode`（**不引入 `effective*` 局部常量，不使用 `handleSubmit` 内条件屏蔽方案**）
- [ ] Step 2 完成：已执行全仓 `react-hooks/*` 扫描；若无遗留违规则 4 条规则升为 `"error"`；若有遗留违规则保持 `"warn"` 并在 PR 描述中附清单（**不得用 `eslint-disable-next-line` 大量掩盖**）
- [ ] Step 3 完成：`useEditingJobSync.ts` 已提取，包含 `job`/`resource`/`voiceMap`/`speakerNameMap`/`isLoading`/`pageError`/`reload`/`patchResource`
- [ ] Step 4 完成：`useEditingSegments.ts` 和 `useBulkReplace.ts` 已提取
- [ ] Step 5 完成：`useCommitFlow.ts` 已提取
- [ ] Step 6 完成：`EditingToolbar.tsx` 已提取；`page.tsx` 行数 ≤ 600
- [ ] 全量 `tsc --noEmit` 通过（`grep -c "error TS" == 0`）
- [ ] `npm run lint` 通过（0 条 react-hooks error，或仅保留 warn 状态时 0 条新报错）
- [ ] `npm run build` 通过（Next.js standalone 构建无 Error）
- [ ] `page.tsx` 内 `useState` 调用数 ≤ 5（`grep -c "useState" page.tsx ≤ 5`）
- [ ] 各步独立 commit、显式 pathspec（`git commit -- <files>`）、未 `git add .`
- [ ] 提取的 hooks 和组件不引入 axios / react-query / zustand / jotai 等重量级库
