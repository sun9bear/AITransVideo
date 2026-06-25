# TU-11 · 前端语音选择共享模块

- **目标 / 价值**：`VoiceModifyTab.tsx`（1,519 行）与 `VoiceSelectionPanel.tsx`（1,222 行）存在大量重复：`SpeakerPayload` 接口各自独立定义且已发生分叉（字段差异见下文）；`PROVIDER_TAB_ORDER`/`PROVIDER_SHORT_LABELS` 常量逐字重复；`formatVoiceOptionLabel`/`matchScopeBadge`/`formatCandidateSourceHint` 三个纯函数逐字重复；候选加载逻辑（`getVoiceCandidates` 批量 `Promise.allSettled`）几乎逐行重复约 30 行。抽出 `src/features/voice/types.ts`（统一类型）、`src/features/voice/utils.ts`（纯函数+常量）、`src/features/voice/useVoiceCandidates.ts`（加载 hook），令两组件 import 共享模块。收口后：任何类型或工具函数变更只需改一处，`tsc --noEmit` 0 错误，两文件中不再有本地 `SpeakerPayload`/`AvailableVoice`/`ProviderInfo` 定义，也不再有本地 `matchScopeBadge`/`formatCandidateSourceHint` 副本。
- **关联发现**：FE-004（`SpeakerPayload` 接口分叉）、FE-009（重复加载逻辑与纯函数）、TS-08（统一类型定义点缺失）
- **前置依赖**：无（可并行，不依赖其他 TU）
- **建议分支**：`quality/voice-select-shared`
- **预估工时**：L（类型分叉需逐字段对齐后再迁，预计 4–6 天分 5 步执行；每步独立 commit）

> **命令环境**：默认 Git Bash / CI Linux（仓库已配 Bash 工具）；PowerShell 执行者改用等价命令（`grep`→`Select-String`、`wc -l`→`(Get-Content file | Measure-Object -Line).Lines`、`test -f`→`Test-Path`、避免 `<(...)`）。

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **hook 职责边界**：`useVoiceCandidates` 只负责加载与缓存 candidate map，不负责下游 draft / voice state 初始化；两个组件各自的 draft 初始化逻辑留在组件内部，避免共享 hook 变成业务大杂烩。
- **共享类型基准**：以 `VoiceSelectionPanel` 的完整版 `SpeakerPayload` 为基准，`VoiceModifyTab` 缺失的字段加可选 `?`，不硬替换映射逻辑，不要求 `VoiceModifyTab` 端必须填入这些字段。
- **`SmartOfferedCandidate` 不提取到共享模块**：该类型仅 `VoiceModifyTab` 使用，属专有类型，保留在组件内；即便 `VoiceSelectionPanel` 内部也定义了同名类型，两者不合并（各自独立，超出本单元范围）。
- **组件内 draft 初始化不迁入 hook**：`loadCandidates` 返回后各组件自行用 `candidateMap` 初始化 draft/voice state，差异逻辑不进入 hook，hook 只 `return { voiceCandidates, loadCandidates, refetchForSpeaker, setCandidates }`。
- **可选字段以 `?` 标注、不改运行时路径**：`VoiceModifyTab` 映射处对缺失字段补 `?? undefined` 或留空，不新增业务字段的赋值语义。

---

## 不在本单元范围（out-of-scope）

- `SpeakerVoiceState`（`VoiceSelectionPanel.tsx:109`）与 `SpeakerDraftState`（`VoiceModifyTab.tsx:113`）：两个状态类型虽相似但字段存在差异（`VoiceModifyTab` 多一个 `minimaxModel` 字段，语义一致；`VoiceSelectionPanel` 多 `isCloning`/`cloneError` 字段），且它们是组件内部状态、不跨文件传递。本单元暂不合并，避免引入错误。
- `SmartOfferedCandidate` 类型（仅 `VoiceModifyTab` 使用）：为 Smart 预览流程专有，暂不提取。
- `ProbeText` 接口（仅 `VoiceSelectionPanel` 使用）：字段已内联入 `VoiceModifyTab.tsx:92`，统一后以 `VoiceSelectionPanel` 版本为准；提取至 `types.ts` 但两组件仍可各自保留 `SmartOfferedCandidate`/`SpeakerDraftState` 等专有类型。
- `minimaxModelKey` 函数：仅 `VoiceModifyTab` 使用（行 159），不提取。
- 组件渲染 JSX 的去重（VoiceProviderTabs / VoiceCandidateList 组件化）：这是另一层重构，当前字段分叉未对齐前贸然提取 JSX 容易引入 prop 类型不一致；**本单元只做基础层（类型+常量+纯函数+候选 hook）**，JSX 组件化留给后续 TU。
- `minimaxModel` 字段计费语义扩展：本单元不改业务逻辑，只迁移类型。
- 付费 API 调用路径（MiniMax 克隆 / CosyVoice 克隆）：本单元不涉及。
- Python 后端任何文件：本单元纯前端。

---

## 必守不变量

- **付费 API 硬约束**：`getVoiceCandidates` 是只读 registry 查询（Gateway 端点，无付费调用），`useVoiceCandidates` hook 内部不得新增任何付费 API 调用路径；hook 失败必须静默降级（`catch` 后继续，不触发付费 fallback）。
- **Alignment DSP-first、retiming 数学确定性不迁 LLM**：本单元不接触 `targetCharsPerSecond` 计算逻辑，该字段由后端下发，前端只存储显示；类型迁移时保留 `targetCharsPerSecond: number | null` 字段，不改其读取路径。
- **Gateway 是 plan/pricing/entitlement 唯一事实源**：`useVoiceCandidates` 不得绕过 Gateway 直连 Job API 或直连外部服务。
- **默认测试不接真实外部服务**：新增 Vitest 单元测试必须 mock `@/lib/api/voiceSelection`，不发真实网络请求。
- **process.py 走 Option B**：本单元不触及 Python 任何文件。
- **迁移原则**：先建共享模块并在共享模块内编写测试，确认测试绿后再逐文件迁移；迁移期间两组件可临时 import 共享模块（不删本地类型）；待两文件均迁移完成后统一删除本地副本；每小步一个 commit，显式 pathspec。

---

## Step 0 · 确认现状

```bash
# 建分支
git switch -c quality/voice-select-shared

# 1. 确认两文件行数（spec：1519 / 1222，行数可能因并行改动漂移）
wc -l \
  frontend-next/src/app/\(app\)/workspace/\[jobId\]/edit/VoiceModifyTab.tsx \
  frontend-next/src/components/workspace/VoiceSelectionPanel.tsx
# 期望：两文件行数与 spec 接近（±50 行内视为正常并行漂移）

# 2. 确认 SpeakerPayload 各自定义位置（两处均为局部 interface，不 export）
grep -n "^interface SpeakerPayload" \
  "frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx" \
  "frontend-next/src/components/workspace/VoiceSelectionPanel.tsx"
# 期望（以实际行号为准）：
#   VoiceModifyTab.tsx:81:   interface SpeakerPayload {
#   VoiceSelectionPanel.tsx:59:  interface SpeakerPayload {

# 3. 确认字段分叉：VoiceModifyTab 多哪些字段
grep -n "speakerRole\|speakerRoleLabel\|speakerReviewHint\|targetCharsPerSecond\|smartOfferedCandidates" \
  "frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx" | head -10
grep -n "speakerRole\|speakerRoleLabel\|speakerReviewHint\|targetCharsPerSecond\|smartOfferedCandidates" \
  "frontend-next/src/components/workspace/VoiceSelectionPanel.tsx" | head -10
# 期望：VoiceSelectionPanel 含 speakerRole/speakerRoleLabel/speakerReviewHint/targetCharsPerSecond/smartOfferedCandidates
#       VoiceModifyTab 的 SpeakerPayload 缺少上述字段（分叉点）

# 4. 确认重复常量位置
grep -n "PROVIDER_TAB_ORDER\|PROVIDER_SHORT_LABELS" \
  "frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx" \
  "frontend-next/src/components/workspace/VoiceSelectionPanel.tsx"
# 期望（以实际行号为准）：
#   VoiceModifyTab.tsx:142:   const PROVIDER_TAB_ORDER
#   VoiceModifyTab.tsx:143:   const PROVIDER_SHORT_LABELS
#   VoiceSelectionPanel.tsx:124: const PROVIDER_TAB_ORDER
#   VoiceSelectionPanel.tsx:125: const PROVIDER_SHORT_LABELS

# 5. 确认重复纯函数位置
grep -n "^function formatVoiceOptionLabel\|^function matchScopeBadge\|^function formatCandidateSourceHint" \
  "frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx" \
  "frontend-next/src/components/workspace/VoiceSelectionPanel.tsx"
# 期望（以实际行号为准）：
#   VoiceModifyTab.tsx:149:   function formatVoiceOptionLabel
#   VoiceModifyTab.tsx:165:   function matchScopeBadge
#   VoiceModifyTab.tsx:180:   function formatCandidateSourceHint
#   VoiceSelectionPanel.tsx:132: function formatVoiceOptionLabel
#   VoiceSelectionPanel.tsx:147: function matchScopeBadge
#   VoiceSelectionPanel.tsx:164: function formatCandidateSourceHint

# 6. 确认共享目标目录不存在（如已存在需核查再继续）
test -d frontend-next/src/features/voice && echo "ALREADY_EXISTS" || echo "NOT_EXISTS"

# 7. 确认 tsconfig paths alias
grep -A2 '"paths"' frontend-next/tsconfig.json
# 期望：  "@/*": ["./src/*"]
```

---

## Step 1 · 对齐 SpeakerPayload 字段差异，起草合并版类型

**背景**：`VoiceModifyTab.tsx` 与 `VoiceSelectionPanel.tsx` 的 `SpeakerPayload` 已分叉。`VoiceSelectionPanel` 的版本更完整（多 `speakerRole`/`speakerRoleLabel`/`speakerReviewHint`/`targetCharsPerSecond`/`smartOfferedCandidates`/`ProbeText` 接口）。`VoiceModifyTab` 的版本缺少这些字段。**合并方向：以 `VoiceSelectionPanel` 版本为基础，补全为超集，对可选字段加 `?` 而非硬改两个组件的映射逻辑。**

**动作**：

1. 打开 `frontend-next/src/components/workspace/VoiceSelectionPanel.tsx`，阅读 `SpeakerPayload`（约行 59–90）和 `ProbeText`（约行 44–49）的完整定义。
2. 打开 `frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx`，阅读 `SpeakerPayload`（约行 81–94）和 `SmartOfferedCandidate`（约行 50–58）的定义，记录缺失字段。
3. 起草合并版（写在注释里或 scratch 文件中，**此步不新建正式文件**）。合并规则：
   - `VoiceSelectionPanel` 有、`VoiceModifyTab` 无的字段 → 加 `?` 可选（`VoiceModifyTab` 映射时暂未填入这些字段）
   - `VoiceModifyTab` 有、`VoiceSelectionPanel` 无的字段 → 同样加 `?`（当前 `VoiceSelectionPanel` 不使用）
   - 两者均有的字段 → 取类型更宽松的那个（如 `autoMatchedByProvider` 的 `matchConfidence` 字段）
4. 确认 `AvailableVoice`、`ProviderInfo` 字段完全一致（两文件均有，字段相同）。

**具体 file:line**：

| 位置 | 行号（以实际为准） |
|------|------------------|
| `VoiceSelectionPanel.tsx` `ProbeText` | 约 44–49 |
| `VoiceSelectionPanel.tsx` `SmartOfferedCandidate` | 约 50–57 |
| `VoiceSelectionPanel.tsx` `SpeakerPayload` | 约 59–90 |
| `VoiceModifyTab.tsx` `SpeakerPayload` | 约 81–94 |
| `VoiceSelectionPanel.tsx` `AvailableVoice` | 约 91–102 |
| `VoiceModifyTab.tsx` `AvailableVoice` | 约 95–104 |
| `VoiceSelectionPanel.tsx` `ProviderInfo` | 约 103–107 |
| `VoiceModifyTab.tsx` `ProviderInfo` | 约 105–109 |

**该步验收**（不需要 commit，是分析步）：

```bash
# 确认 VoiceSelectionPanel 有而 VoiceModifyTab 无的字段
grep -c "speakerRoleLabel\|speakerReviewHint\|targetCharsPerSecond\|smartOfferedCandidates" \
  "frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx"
# 期望：0（说明这些字段在 VoiceModifyTab 的 SpeakerPayload 中缺失，需补可选字段）

# 确认 AvailableVoice 在两文件中完全一致（字段名列表相同）
grep -A8 "^interface AvailableVoice" \
  "frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx"
grep -A10 "^interface AvailableVoice" \
  "frontend-next/src/components/workspace/VoiceSelectionPanel.tsx"
# 期望：两段输出字段名一致（voiceId/label/gender/provider/charsPerSecond/speedCalibratedAt）
```

---

## Step 2 · 创建 `features/voice/` 共享模块：类型文件与工具函数文件

**动作**：创建以下两个新文件（使用 Write 工具）：

### 2a. `frontend-next/src/features/voice/types.ts`

内容要点：
- 导出 `ProbeText`（从 `VoiceSelectionPanel:44` 原样提取，加 `export`）
- 导出 `SpeakerPayload`（✅ 已决策（CodeX 2026-06-25）：以 `VoiceSelectionPanel` 完整版为基准，`VoiceModifyTab` 缺失字段加 `?` 可选；不要求两端必须赋值）
- 导出 `AvailableVoice`（从任一文件提取，字段相同）
- 导出 `ProviderInfo`（从任一文件提取，字段相同）
- 文件头注释：`// 共享语音选择类型，由 VoiceSelectionPanel 和 VoiceModifyTab 共用。`
- **不** 导出 `SpeakerDraftState`/`SpeakerVoiceState`（组件内部状态，见 out-of-scope）
- **不** 导出 `SmartOfferedCandidate`（✅ 已决策（CodeX 2026-06-25）：该类型仅 `VoiceModifyTab` 专用，不提取到共享文件；`VoiceSelectionPanel` 内如有同名定义亦保留在组件内，两者不合并）

> 执行时前置动作（已定方向）：提取前 `grep -n "SmartOfferedCandidate"` 确认两文件各自使用范围，以验证不跨文件传递再继续。

### 2b. `frontend-next/src/features/voice/utils.ts`

内容要点：
- `export const PROVIDER_TAB_ORDER = ['minimax', 'cosyvoice', 'volcengine'] as const`
- `export type VoiceProvider = typeof PROVIDER_TAB_ORDER[number]`
- `export const PROVIDER_SHORT_LABELS: Record<string, string> = { minimax: 'MiniMax', cosyvoice: 'CosyVoice', volcengine: '豆包' }`
- `export function formatVoiceOptionLabel(v: AvailableVoice): string`（从 `VoiceSelectionPanel:132` 原样提取，import `AvailableVoice` from `./types`）
- `export function matchScopeBadge(scope: VoiceMatchScope): string`（从任一文件原样提取，import `VoiceMatchScope` from `@/lib/api/voiceSelection`）
- `export function formatCandidateSourceHint(candidate: VoiceCandidate): string`（从任一文件原样提取，import `VoiceCandidate` from `@/lib/api/voiceSelection`）
- **不** 提取 `minimaxModelKey`（仅 `VoiceModifyTab` 使用，见 out-of-scope）

**具体 file:line 参照**（提取时以 grep 确认的实际行号为准）：

| 函数/常量 | `VoiceModifyTab.tsx` 行号 | `VoiceSelectionPanel.tsx` 行号 |
|-----------|--------------------------|-------------------------------|
| `PROVIDER_TAB_ORDER` | 约 142 | 约 124 |
| `PROVIDER_SHORT_LABELS` | 约 143 | 约 125 |
| `formatVoiceOptionLabel` | 约 149 | 约 132 |
| `matchScopeBadge` | 约 165 | 约 147 |
| `formatCandidateSourceHint` | 约 180 | 约 164 |

**该步验收**：

```bash
# 两文件必须存在
test -f frontend-next/src/features/voice/types.ts && echo "types.ts OK" || echo "MISSING types.ts"
test -f frontend-next/src/features/voice/utils.ts && echo "utils.ts OK" || echo "MISSING utils.ts"

# types.ts 必须 export SpeakerPayload / AvailableVoice / ProviderInfo / ProbeText
grep -c "^export interface SpeakerPayload\|^export interface AvailableVoice\|^export interface ProviderInfo\|^export interface ProbeText" \
  frontend-next/src/features/voice/types.ts
# 期望：4

# utils.ts 必须 export 三个函数 + 两个常量
grep -c "^export function formatVoiceOptionLabel\|^export function matchScopeBadge\|^export function formatCandidateSourceHint\|^export const PROVIDER_TAB_ORDER\|^export const PROVIDER_SHORT_LABELS" \
  frontend-next/src/features/voice/utils.ts
# 期望：5

# tsc 必须 0 错误（共享模块本身不报错）
cd frontend-next && npx tsc --noEmit 2>&1 | grep -c "error TS" || true
# 期望：0（若有错误则逐行修复后再继续）
```

**commit**（此步完成后独立 commit）：

```bash
git add -- frontend-next/src/features/voice/types.ts frontend-next/src/features/voice/utils.ts
git commit -m "feat: add shared voice types and utils module (features/voice)"
```

---

## Step 3 · 创建 `useVoiceCandidates` 共享 hook

**背景**：两组件各自在 `useEffect` 内通过 `Promise.allSettled` 批量调用 `getVoiceCandidates`，并各自管理 `voiceCandidates` / `refetchCandidatesForSpeaker` 状态与回调。这段逻辑约 30–40 行完全重复。将其提取为独立 hook。

**动作**：创建 `frontend-next/src/features/voice/useVoiceCandidates.ts`，内容如下：

```typescript
// useVoiceCandidates.ts — 批量加载 personal-voice 候选，供
// VoiceSelectionPanel 和 VoiceModifyTab 共用。
// No paid API calls: getVoiceCandidates 是只读 registry 查询。

import { useCallback, useRef, useState } from 'react'
import {
  getVoiceCandidates,
  type VoiceCandidatesResponse,
} from '@/lib/api/voiceSelection'

interface SpeakerStub {
  speakerId: string
  speakerName: string
}

interface UseVoiceCandidatesReturn {
  voiceCandidates: Record<string, VoiceCandidatesResponse>
  /** 批量加载，需传入说话人列表和当前 selectedProvider。
   *  返回 candidateMap（同时更新内部状态）。
   *  调用失败的 speaker 会被 best-effort 跳过（不抛出）。 */
  loadCandidates: (
    jobId: string,
    speakers: SpeakerStub[],
    defaultProvider: string,
  ) => Promise<Record<string, VoiceCandidatesResponse>>
  /** 重新加载单个说话人的候选（用于克隆完成后刷新）。
   *  失败时静默，不更新状态。 */
  refetchForSpeaker: (
    jobId: string,
    speakerId: string,
    speakerName: string,
    defaultProvider: string,
  ) => Promise<void>
  setCandidates: React.Dispatch<React.SetStateAction<Record<string, VoiceCandidatesResponse>>>
}

export function useVoiceCandidates(): UseVoiceCandidatesReturn {
  const [voiceCandidates, setVoiceCandidates] = useState<
    Record<string, VoiceCandidatesResponse>
  >({})

  const loadCandidates = useCallback(
    async (
      jobId: string,
      speakers: SpeakerStub[],
      defaultProvider: string,
    ): Promise<Record<string, VoiceCandidatesResponse>> => {
      const candidateMap: Record<string, VoiceCandidatesResponse> = {}
      await Promise.allSettled(
        speakers.map(async (sp) => {
          try {
            const result = await getVoiceCandidates({
              jobId,
              speakerId: sp.speakerId,
              speakerName: sp.speakerName,
              selectedProvider: defaultProvider,
            })
            candidateMap[sp.speakerId] = result
          } catch (err) {
            // Best-effort: skip this speaker on failure. No paid API calls.
            console.warn('getVoiceCandidates failed for speaker', sp.speakerId, err)
          }
        }),
      )
      setVoiceCandidates(candidateMap)
      return candidateMap
    },
    [],
  )

  const refetchForSpeaker = useCallback(
    async (
      jobId: string,
      speakerId: string,
      speakerName: string,
      defaultProvider: string,
    ): Promise<void> => {
      try {
        const result = await getVoiceCandidates({
          jobId,
          speakerId,
          speakerName,
          selectedProvider: defaultProvider,
        })
        setVoiceCandidates((prev) => ({ ...prev, [speakerId]: result }))
      } catch (err) {
        console.warn('getVoiceCandidates refetch failed for speaker', speakerId, err)
      }
    },
    [],
  )

  return { voiceCandidates, loadCandidates, refetchForSpeaker, setCandidates: setVoiceCandidates }
}
```

> ✅ 已决策（CodeX 2026-06-25）：hook 只负责加载和存储候选 map，不负责下游的 draft 初始化。两组件在 `loadCandidates` 返回后各自不同的"用 candidateMap 初始化 draft/voice state"逻辑（约 40–80 行）保留在组件内部，避免共享 hook 变成业务大杂烩。

**该步验收**：

```bash
test -f frontend-next/src/features/voice/useVoiceCandidates.ts && echo "hook OK" || echo "MISSING"

# hook 必须 export useVoiceCandidates
grep -c "^export function useVoiceCandidates" \
  frontend-next/src/features/voice/useVoiceCandidates.ts
# 期望：1

# tsc 0 错误
cd frontend-next && npx tsc --noEmit 2>&1 | grep -c "error TS" || true
# 期望：0

# hook 内部不含任何付费 API 引用
grep -c "minimax\|cosyvoice_clone\|voice_clone\|CosyVoiceClone\|MiniMax" \
  frontend-next/src/features/voice/useVoiceCandidates.ts
# 期望：0
```

**commit**：

```bash
git add -- frontend-next/src/features/voice/useVoiceCandidates.ts
git commit -m "feat: add useVoiceCandidates shared hook (features/voice)"
```

---

## Step 4 · 将 `VoiceSelectionPanel.tsx` 迁移到共享模块

本步将 `VoiceSelectionPanel.tsx` 从本地副本切换到 `features/voice/` 共享模块。迁移遵循**先添加 import、再删本地定义**的顺序，以便 tsc 在删除前后各 check 一次。

**动作（逐小步）**：

**4a. 替换类型 import**：

在 `VoiceSelectionPanel.tsx` 的 import 区块（约行 1–42）：
- 在现有 import 列表后追加（`SmartOfferedCandidate` 不从共享模块 import，保留组件内局部定义）：
  ```typescript
  import type {
    SpeakerPayload,
    AvailableVoice,
    ProviderInfo,
    ProbeText,
  } from '@/features/voice/types'
  import {
    PROVIDER_TAB_ORDER,
    PROVIDER_SHORT_LABELS,
    formatVoiceOptionLabel,
    matchScopeBadge,
    formatCandidateSourceHint,
  } from '@/features/voice/utils'
  import { useVoiceCandidates } from '@/features/voice/useVoiceCandidates'
  ```
- 运行 `tsc --noEmit`，期望出现"duplicate identifier"错误（因为本地定义还未删）。
- 删除本地重复定义（`ProbeText`/`SpeakerPayload`/`AvailableVoice`/`ProviderInfo` 接口，约行 44–108；`PROVIDER_TAB_ORDER`/`PROVIDER_SHORT_LABELS` 常量；`formatVoiceOptionLabel`/`matchScopeBadge`/`formatCandidateSourceHint` 函数）。`SmartOfferedCandidate` 若在 `VoiceSelectionPanel` 中有本地定义，保留不删（已决策：不提取到共享模块）。
- 删除 `voiceCandidates` state、`loadCandidates`-相关的 `Promise.allSettled` 代码块、单个 refetch 代码块（三处），改为调用 `useVoiceCandidates` hook：
  ```typescript
  const { voiceCandidates, loadCandidates, refetchForSpeaker, setCandidates: setVoiceCandidates } =
    useVoiceCandidates()
  ```
  然后在原来批量加载的 `useEffect` 中把 `candidateMap`/`setVoiceCandidates` 替换为 `await loadCandidates(jobId, loadedSpeakers, loadedDefaultProvider)`；在单个 refetch 位置调用 `await refetchForSpeaker(jobId, speakerId, speakerName, defaultProvider)`。

**具体 file:line**（以 Step 0 grep 实际结果为准）：

| 删除内容 | `VoiceSelectionPanel.tsx` 行号范围 |
|----------|-----------------------------------|
| `ProbeText` 接口 | 约 44–49 |
| `SmartOfferedCandidate` 接口 | 约 50–57（**保留**，不删；已决策不提取到共享模块） |
| `SpeakerPayload` 接口 | 约 59–90 |
| `AvailableVoice` 接口 | 约 91–102 |
| `ProviderInfo` 接口 | 约 103–108 |
| `PROVIDER_TAB_ORDER` 常量 | 约 124 |
| `PROVIDER_SHORT_LABELS` 常量 | 约 125–129 |
| `formatVoiceOptionLabel` 函数 | 约 132–143 |
| `matchScopeBadge` 函数 | 约 147–162 |
| `formatCandidateSourceHint` 函数 | 约 164–169 |
| `voiceCandidates` state | 约 184 |
| 批量 `getVoiceCandidates` 块 | 约 394–415 |
| 单个 refetch 块 | 约 513–522 |

**该步验收**：

```bash
# tsc 0 错误
cd frontend-next && npx tsc --noEmit 2>&1 | grep -c "error TS" || true
# 期望：0

# VoiceSelectionPanel 不再有本地 SpeakerPayload 定义
grep -c "^interface SpeakerPayload\|^interface AvailableVoice\|^interface ProviderInfo\|^interface ProbeText" \
  frontend-next/src/components/workspace/VoiceSelectionPanel.tsx
# 期望：0

# VoiceSelectionPanel 不再有本地函数副本
grep -c "^function formatVoiceOptionLabel\|^function matchScopeBadge\|^function formatCandidateSourceHint\|^const PROVIDER_TAB_ORDER\|^const PROVIDER_SHORT_LABELS" \
  frontend-next/src/components/workspace/VoiceSelectionPanel.tsx
# 期望：0

# VoiceSelectionPanel 正确 import 共享模块
grep -c "from '@/features/voice/types'\|from '@/features/voice/utils'\|from '@/features/voice/useVoiceCandidates'" \
  frontend-next/src/components/workspace/VoiceSelectionPanel.tsx
# 期望：3
```

**commit**：

```bash
git add -- "frontend-next/src/components/workspace/VoiceSelectionPanel.tsx"
git commit -m "refactor: migrate VoiceSelectionPanel to shared voice module"
```

---

## Step 5 · 将 `VoiceModifyTab.tsx` 迁移到共享模块，并补齐其 SpeakerPayload 可选字段

本步将 `VoiceModifyTab.tsx` 从本地副本切换到 `features/voice/` 共享模块，同时在其 speaker 映射逻辑中补齐 Step 1 确认的缺失可选字段（填 `undefined`/默认值，不改下游渲染逻辑）。

**动作（逐小步）**：

**5a. 替换 import**：与 Step 4 相同模式，追加共享 import，删本地重复定义。

**具体 file:line**（以 Step 0 grep 实际结果为准）：

| 删除内容 | `VoiceModifyTab.tsx` 行号范围 |
|----------|------------------------------|
| `SpeakerPayload` 接口 | 约 81–94 |
| `AvailableVoice` 接口 | 约 95–104 |
| `ProviderInfo` 接口 | 约 105–109 |
| `PROVIDER_TAB_ORDER` 常量 | 约 142 |
| `PROVIDER_SHORT_LABELS` 常量 | 约 143–147 |
| `formatVoiceOptionLabel` 函数 | 约 149–158 |
| `matchScopeBadge` 函数 | 约 165–178 |
| `formatCandidateSourceHint` 函数 | 约 180–186 |
| `voiceCandidates` state | 约 228 |
| 批量 `getVoiceCandidates` 块 | 约 462–485 |
| 单个 refetch 块 | 约 652–668 |

> 注意：`VoiceModifyTab` 保留 `SpeakerDraftState`、`SmartOfferedCandidate`、`minimaxModelKey` 这些组件专有定义，不删除。

**5b. 在 speaker 映射处补填缺失可选字段**：找到 `VoiceModifyTab.tsx` 中构建 `SpeakerPayload` 对象的 `.map()` 代码块（约行 351–403）。在返回对象中追加缺失字段（这些字段在 `VoiceModifyTab` 的原始 API payload 中可能不存在，用 `?? undefined` 读取或直接填 `undefined`）：
```typescript
// 缺失字段填入（只要共享 SpeakerPayload 声明为可选即不报错）
speakerRole: String(s.speaker_role ?? ''),
speakerRoleLabel: String(s.speaker_role_label ?? ''),
speakerReviewHint: String(s.speaker_review_hint ?? ''),
targetCharsPerSecond: s.target_chars_per_second != null ? Number(s.target_chars_per_second) : null,
```
（`smartOfferedCandidates` 字段在 `VoiceModifyTab` 已有，无需额外补填。）

**该步验收**：

```bash
# tsc 0 错误
cd frontend-next && npx tsc --noEmit 2>&1 | grep -c "error TS" || true
# 期望：0

# VoiceModifyTab 不再有本地 SpeakerPayload/AvailableVoice/ProviderInfo 定义
grep -c "^interface SpeakerPayload\|^interface AvailableVoice\|^interface ProviderInfo" \
  "frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx"
# 期望：0

# VoiceModifyTab 不再有本地函数副本
grep -c "^function formatVoiceOptionLabel\|^function matchScopeBadge\|^function formatCandidateSourceHint\|^const PROVIDER_TAB_ORDER\|^const PROVIDER_SHORT_LABELS" \
  "frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx"
# 期望：0

# VoiceModifyTab 正确 import 共享模块
grep -c "from '@/features/voice/types'\|from '@/features/voice/utils'\|from '@/features/voice/useVoiceCandidates'" \
  "frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx"
# 期望：3

# 全局唯一 SpeakerPayload 定义点
grep -rn "^export interface SpeakerPayload\|^interface SpeakerPayload" \
  frontend-next/src/ --include="*.ts" --include="*.tsx"
# 期望：只有 features/voice/types.ts 一处（不含 VoiceModifyTab.tsx / VoiceSelectionPanel.tsx）
```

**commit**：

```bash
git add -- \
  "frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx" \
  frontend-next/src/features/voice/types.ts
git commit -m "refactor: migrate VoiceModifyTab to shared voice module; unify SpeakerPayload"
```

---

## 测试计划（新增 / 回归）

### 新增 Vitest 单元测试

文件：`frontend-next/src/features/voice/__tests__/utils.test.ts`

覆盖内容：
1. `formatVoiceOptionLabel`：`charsPerSecond = null` → 返回纯 label；`cps = 3.0` → 包含 `慢`；`cps = 4.0` → 包含 `中`；`cps = 5.0` → 包含 `快`
2. `matchScopeBadge`：各 case 返回固定字符串（`'★ 强匹配'`/`'● 同视频同名'` 等，对照两文件原有逻辑确认一致）
3. `formatCandidateSourceHint`：`evidence.sourceVideoTitle = null` → 返回 `''`；有 title → 返回 ` · ${title}`
4. `PROVIDER_TAB_ORDER`：长度为 3，包含 `'minimax'`/`'cosyvoice'`/`'volcengine'`

文件：`frontend-next/src/features/voice/__tests__/useVoiceCandidates.test.ts`

覆盖内容（mock `@/lib/api/voiceSelection`）：
1. `loadCandidates` 批量成功：返回 `candidateMap`，每个 speakerId 有对应 entry
2. `loadCandidates` 其中一个 speaker 失败：其他 speaker 正常返回，失败 speaker 不在 map 中（best-effort 降级）
3. `refetchForSpeaker` 成功：状态中对应 speakerId 被更新
4. `refetchForSpeaker` 失败：状态不变，无异常抛出

**验收命令**：

```bash
cd frontend-next && npx vitest run src/features/voice/__tests__/
# 期望：所有测试 PASS，exit code 0
```

### 回归验证

```bash
# 完整 tsc
cd frontend-next && npx tsc --noEmit 2>&1 | grep "error TS"
# 期望：无输出（0 错误）

# SpeakerPayload 全局唯一定义点
grep -rn "^interface SpeakerPayload\|^export interface SpeakerPayload" \
  frontend-next/src/ --include="*.ts" --include="*.tsx" | grep -v "node_modules"
# 期望：仅 features/voice/types.ts 一行

# matchScopeBadge / formatCandidateSourceHint 全局唯一函数实现
grep -rn "^function matchScopeBadge\|^export function matchScopeBadge\|^function formatCandidateSourceHint\|^export function formatCandidateSourceHint" \
  frontend-next/src/ --include="*.ts" --include="*.tsx" | grep -v "node_modules"
# 期望：仅 features/voice/utils.ts 各一行

# 行数下降验证（迁移后两文件应各减少约 80–120 行）
wc -l \
  "frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx" \
  frontend-next/src/components/workspace/VoiceSelectionPanel.tsx
# 期望：VoiceModifyTab < 1430 行、VoiceSelectionPanel < 1130 行（较原始各减 ≥ 80 行）
```

---

## 回滚方案

- 本单元所有变更在 `quality/voice-select-shared` 分支，**未合入 main 前**可直接删分支回滚：
  ```bash
  git checkout main
  git branch -D quality/voice-select-shared
  ```
- 若已合入 main，回滚范围为以下文件/commit（按步骤边界）：
  - Step 2 commit：删除 `frontend-next/src/features/voice/types.ts`、`utils.ts`
  - Step 3 commit：删除 `frontend-next/src/features/voice/useVoiceCandidates.ts`
  - Step 4 commit：恢复 `VoiceSelectionPanel.tsx`（`git checkout HEAD~1 -- frontend-next/src/components/workspace/VoiceSelectionPanel.tsx`）
  - Step 5 commit：恢复 `VoiceModifyTab.tsx`（同上），并删除 `types.ts` 中因合并新增的可选字段
- 每步独立 commit 保证可单步 revert。
- 回滚后 `tsc --noEmit` 必须仍通过（原始两文件各含完整类型，不依赖共享模块）。

---

## 完成定义（DoD）

- [ ] `frontend-next/src/features/voice/types.ts` 已创建，导出 `SpeakerPayload`（合并版超集）/`AvailableVoice`/`ProviderInfo`/`ProbeText`
- [ ] `frontend-next/src/features/voice/utils.ts` 已创建，导出 `PROVIDER_TAB_ORDER`/`PROVIDER_SHORT_LABELS`/`formatVoiceOptionLabel`/`matchScopeBadge`/`formatCandidateSourceHint`
- [ ] `frontend-next/src/features/voice/useVoiceCandidates.ts` 已创建，导出 `useVoiceCandidates` hook，无付费 API 调用
- [ ] `VoiceSelectionPanel.tsx` 中本地 `SpeakerPayload`/`AvailableVoice`/`ProviderInfo`/`ProbeText` 定义已删除，改为 import 共享模块
- [ ] `VoiceModifyTab.tsx` 中本地 `SpeakerPayload`/`AvailableVoice`/`ProviderInfo` 定义已删除，改为 import 共享模块
- [ ] 两文件中 `PROVIDER_TAB_ORDER`/`PROVIDER_SHORT_LABELS`/`formatVoiceOptionLabel`/`matchScopeBadge`/`formatCandidateSourceHint` 本地副本已删除
- [ ] `grep -rn "^interface SpeakerPayload" frontend-next/src/ --include="*.ts" --include="*.tsx"` 只返回 `features/voice/types.ts` 一行
- [ ] `npx tsc --noEmit` 0 错误（在 `frontend-next/` 目录执行）
- [ ] 新增 Vitest 单元测试全部 PASS（`npx vitest run src/features/voice/__tests__/`）
- [ ] `VoiceModifyTab.tsx` 行数 < 1,430（较 1,519 减少 ≥ 80 行）
- [ ] `VoiceSelectionPanel.tsx` 行数 < 1,130（较 1,222 减少 ≥ 80 行）
- [ ] 各步独立 commit、显式 pathspec（`git commit -- <files>`）、未 `git add .`
