# UI 导航重构：视频翻译主页 + 新建弹窗 (V2)

> 日期：2026-04-16
> 状态：方案待实施（V2-final，综合 Gemini/CodeX/GPT 三方审核 + CodeX 二审修订）
> 前置依赖：`2026-04-16-video-output-subtitles-player-plan.md`（播放器卡片）

---

## 1. 目标

当前"工作流"分散在 3 个页面（新建翻译 / 当前任务 / 我的项目），用户需要频繁跳转。目标是将"视频翻译"作为核心主页，整合新建、进度、结果三个阶段到同一个视图。

### 1.1 当前页面结构

| 页面 | 路由 | LOC | 功能 |
|------|------|-----|------|
| 新建翻译 | `/translations/new` | 402 | 表单 + 并发守卫 + entitlement + 上传 + 提交后跳 workspace |
| 当前任务 | `/tasks/current` | 83 | 纯跳转页，`selectCurrentTaskJob` → redirect workspace |
| 我的项目 | `/projects` | 127 | 项目列表 + 状态 + 删除，仅 mount 时加载一次 |
| 项目详情 | `/projects/[jobId]` | 78 | 下载 + 进入工作台（职责与主页重叠，将降级） |
| 工作台 | `/workspace/[jobId]` | 294 | 进度 + 审核交互 + 下载（轮询刷新） |

### 1.2 改后结构

| 页面 | 路由 | 功能 |
|------|------|------|
| **视频翻译**（主页） | `/projects` | 项目卡片列表 + 播放器/下载 + 右上角新建/当前任务按钮 + 轮询刷新 |
| 新建翻译弹窗 | Dialog 覆盖层 | 表单 → 提交成功 → 短暂确认 → 自动关闭 → 主页刷新 |
| 工作台 | `/workspace/[jobId]` | **不变**，审核交互仍用全页 |

---

## 2. 各模块设计

### 2.1 侧边栏导航重构

**文件**: `frontend-next/src/components/app-shell.tsx`

改前：
```
工作流
  ⊕ 新建翻译    → /translations/new
  ≡ 当前任务    → /tasks/current
  ▢ 我的项目    → /projects
```

改后：
```
工作流
  🎬 视频翻译   → /projects        ← 主入口
  🎤 我的音色   → /voices

资源
  📊 用量统计   → /usage
```

**导航激活态变更**：
- 当前 `/workspace/*` 高亮归属在 `/tasks/current`
- 改后 `/workspace/*` 和 `/projects/*` 统一高亮归属到"视频翻译"（`/projects`）
- 具体改动：`isActive` 判断中，`/projects` 项额外匹配 `pathname.startsWith("/workspace/")` 和 `pathname.startsWith("/projects/")`

### 2.2 "视频翻译"主页

**文件**: 改造 `frontend-next/src/app/(app)/projects/page.tsx`

```
┌─────────────────────────────────────────────────────────────────┐
│ 视频翻译                              [当前任务] [＋ 新建翻译]   │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│ ┌─ 项目卡片（展开）─────────────────────────────────────────────┐ │
│ │ CNN 新闻翻译          Studio · 已完成 · 2026/4/15 21:23     │ │
│ │ ┌──────────────────┬──────────────────────────────────────┐ │ │
│ │ │ ▶ 视频播放器       │ ↓ 配音视频  ↓ 配音音频  ↓ 素材包     │ │ │
│ │ └──────────────────┴──────────────────────────────────────┘ │ │
│ └────────────────────────────────────────────────────────────┘ │
│                                                                 │
│ ┌─ 项目卡片（展开）─────────────────────────────────────────────┐ │
│ │ Fox 访谈翻译          Express · 处理中 · 2026/4/15 20:22    │ │
│ │ ┌──────────────────────────────────────────────────────────┐ │ │
│ │ │ 阶段：S3 翻译审核   ████████░░ 60%   [进入工作台]          │ │ │
│ │ └──────────────────────────────────────────────────────────┘ │ │
│ └────────────────────────────────────────────────────────────┘ │
│                                                                 │
│ ┌─ 项目卡片（收起）─────────────────────────────────────────────┐ │
│ │ 旧项目标题             Studio · 已失败 · 2026/4/14   [删除]  │ │
│ └────────────────────────────────────────────────────────────┘ │
│                                                                 │
│ 显示更多...                                                      │
└─────────────────────────────────────────────────────────────────┘
```

**每个项目卡片根据状态显示不同内容**：

| 状态 | 卡片展开内容 | 操作 |
|------|------------|------|
| `succeeded` | ResultMediaCard（播放器+下载） | 下载/删除 |
| `running` | 进度条 + 当前阶段 | 进入工作台/取消 |
| `waiting_for_review` | 提示"需要审核" + 审核类型 | 进入工作台 |
| `failed` | 错误摘要 | 重新创建/删除 |
| `queued` | "排队中" | 取消 |

> **注**：`failed` 操作为"重新创建"，不是"重试"——仓库没有 retry API。
>
> **"重新创建"能力边界**：
> - `youtube_url` 来源：可预填 `sourceRef`（URL）和 `speakers`（说话人数）
> - `local_video` 来源：不可恢复已上传文件，打开空白表单并提示重新上传
> - `service_mode` / `transcriptionMethod`：当前 `JobSummary` 不含这些字段，不做预填
> - 本质是"用旧参数快捷新建"，不是 job clone

**默认展开规则（含上限）**：

最多默认展开 **3 张**卡片，优先级从高到低：
1. 最近 1 个 `waiting_for_review` 任务
2. 最近 1 个 `running` / `queued` 任务
3. 最近 1 个 `succeeded` 任务

其余全部收起，用户可手动展开。

**卡片数据分层**：
- **列表层**：只用 `listJobs()` 返回的 `JobSummary` 渲染基础卡片（标题/状态/时间）
- **展开层**：仅对展开的 `succeeded` 卡片挂载 `ResultMediaCard`（触发 `materials-availability` 请求）
- **收起时**：不挂载 `ResultMediaCard`，不发请求
- **running 卡片**：进度信息从 `JobSummary` 已有的 `currentStage` / `progressMessage` 字段取，不额外请求详情

**数据刷新策略**：
- 页面有活跃任务（`running` / `queued` / `waiting_for_review`）时：启用 `usePollingTask` 轮询 `listJobs()`，间隔 4 秒
- 无活跃任务时：停止轮询
- 新建/删除/取消操作后：强制 `loadJobs()` 立即刷新
- Dialog 关闭后：强制刷新一次

**手机端**：
- 卡片全宽竖排
- 播放器 16:9 全宽
- 下载按钮堆叠

### 2.3 新建翻译弹窗

#### 2.3.1 TranslationForm 组件契约

**文件**: 新建 `frontend-next/src/components/workspace/TranslationForm.tsx`

从 `/translations/new/page.tsx` 抽取表单逻辑为独立组件，关键设计：

```typescript
interface TranslationFormProps {
  /** 创建成功回调，由容器决定后续行为 */
  onCreated: (job: { id: string; title: string }) => void
  /** 页面模式下成功后跳转 workspace，弹窗模式下由容器处理 */
  mode: 'page' | 'dialog'
}
```

**职责边界**：
- TranslationForm 负责：表单 UI、输入校验、并发守卫展示、上传逻辑、调用 `submitTranslationJob()`
- TranslationForm **不负责**：提交成功后的路由跳转、弹窗关闭、主页刷新
- 提交成功后：调用 `onCreated(job)` 回调，由容器（Page 或 Dialog）决定后续行为

**并发守卫**：
- 表单内部继续负责拉取 `entitlements` 和 `activeJobs` 判断并发限制
- 达到上限时：表单内展示提示，禁用提交按钮
- 主页的"新建翻译"按钮不做前置拦截（交给表单内部统一处理，避免两处维护）

#### 2.3.2 NewTranslationDialog

**文件**: 新建 `frontend-next/src/components/workspace/NewTranslationDialog.tsx`

- 触发：主页右上角"＋ 新建翻译"按钮，或空态 CTA
- 内容：`<TranslationForm mode="dialog" onCreated={handleCreated} />`
- 尺寸：`max-w-lg max-h-[90vh] overflow-y-auto`
- **提交后行为（简化）**：
  1. 表单提交成功 → `onCreated` 触发
  2. Dialog 内短暂显示"任务已创建"（1.5 秒）
  3. 自动关闭 Dialog
  4. 主页 `loadJobs()` 刷新 → 新任务卡片出现在列表中
- 再次打开 Dialog → 回到空白新建表单（不恢复上次任务状态）

> **不做**：不在 Dialog 内做持续进度轮询。弹窗是"创建入口"，不是"迷你工作台"。

#### 2.3.3 旧页面保持兼容

`/translations/new/page.tsx` 改为引用 `TranslationForm`：
```typescript
<TranslationForm 
  mode="page" 
  onCreated={(job) => router.push(`/workspace/${job.id}`)} 
/>
```

行为与当前完全一致（提交后跳 workspace），只是表单逻辑不再重复。

### 2.4 "当前任务"按钮

**新增 selector**（不复用 `selectCurrentTaskJob`）：

```typescript
// frontend-next/src/features/jobs/selectors.ts
export function selectActiveTaskJob(jobs: JobSummary[]): JobSummary | null {
  // 只认真正活跃的状态，不 fallback 到"最近 1 小时"
  const active = jobs.filter(j => 
    ['running', 'queued', 'waiting_for_review'].includes(j.status)
  )
  if (active.length === 0) return null
  // 优先 waiting_for_review，其次 running，最后 queued
  const priorityOrder = ['waiting_for_review', 'running', 'queued']
  active.sort((a, b) => {
    const pa = priorityOrder.indexOf(a.status)
    const pb = priorityOrder.indexOf(b.status)
    if (pa !== pb) return pa - pb
    // 注意：前端 JobSummary 字段是 camelCase（updatedAt），不是 snake_case
    return new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime()
  })
  return active[0]
}
```

右上角"当前任务"按钮行为：
- `selectActiveTaskJob(jobs)` 返回非 null → 按钮可用，点击跳转 `/workspace/{jobId}`
- 返回 null → 按钮隐藏
- 有 `waiting_for_review` 任务 → 按钮显示红点
- 旧 `/tasks/current` 页面保留 `selectCurrentTaskJob`（含"最近 1 小时" fallback），不改

### 2.5 审核交互保持全页

TranslationReviewPanel (543 LOC) 和 VoiceSelectionPanel (895 LOC) 交互复杂度太高，不适合放在弹窗/卡片内。

**保持现状**：需要审核时，用户从项目卡片点"进入工作台"跳转 `/workspace/[jobId]`，审核完成后回到视频翻译主页查看结果。

---

## 3. 导航迁移真值表

现有多处硬编码了 `/translations/new`，需要统一处理：

| 位置 | 当前行为 | 改后行为 |
|------|---------|---------|
| `app-shell.tsx` 侧边栏 | "新建翻译" → `/translations/new` | 移除，改为主页右上角按钮 |
| `app-shell.tsx` 侧边栏 | "当前任务" → `/tasks/current` | 移除，改为主页右上角按钮 |
| `app-shell.tsx` 激活态 | `/workspace/*` 高亮 `/tasks/current` | `/workspace/*` 高亮 `/projects` |
| `workspace/[jobId]` 取消后跳转 | `router.push("/translations/new")` | `router.push("/projects?new=1")` |
| `workspace/[jobId]` 空态 CTA | `href="/translations/new"` | `href="/projects?new=1"` |
| `projects/[jobId]` "新建翻译" | `href="/translations/new"` | `href="/projects?new=1"` |
| `projects/page.tsx` 空态 | `actionTo="/translations/new"` | 改为按钮触发 NewTranslationDialog（或 `href="/projects?new=1"`） |
| `not-found.tsx` CTA | `href="/translations/new"` | `href="/projects?new=1"` |

> **`/projects?new=1` 约定**：主页检测到 `searchParams.new === "1"` 时自动打开 NewTranslationDialog，
> 保持"点新建翻译就直接进入新建"的语义一致性，无需改 EmptyState 组件。

### `/projects/[jobId]` 详情页角色

**降级为兼容页**：
- 保留可访问（深链接、书签兼容）
- 不在主路径强调——主页卡片直接展示结果/进入工作台，不需要中间的详情页
- 后续可考虑移除，但本轮不删

---

## 4. 实施步骤

### Phase 1：表单抽取 + 弹窗（低风险）

| 文件 | 改动 |
|------|------|
| `frontend-next/src/components/workspace/TranslationForm.tsx` | **新** — 抽取表单，定义 `onCreated` / `mode` 契约 |
| `frontend-next/src/components/workspace/NewTranslationDialog.tsx` | **新** — Dialog 容器，提交成功 → 短暂确认 → 自动关闭 |
| `frontend-next/src/app/(app)/translations/new/page.tsx` | 改 — 改为 `<TranslationForm mode="page" onCreated={...}>` |

### Phase 2：主页改造 + 导航重组（中风险）

| 文件 | 改动 |
|------|------|
| `frontend-next/src/app/(app)/projects/page.tsx` | 改 — 标题改"视频翻译"，右上角按钮，卡片化，轮询 |
| `frontend-next/src/components/app-shell.tsx` | 改 — 侧边栏重组 + 激活态变更 |
| `frontend-next/src/components/workspace/ProjectCard.tsx` | **新** — 按状态自适应的项目卡片 |
| `frontend-next/src/features/jobs/selectors.ts` | 改 — 新增 `selectActiveTaskJob` |
| `frontend-next/src/app/(app)/workspace/[jobId]/page.tsx` | 改 — 取消/空态跳转改为 `/projects` |
| `frontend-next/src/app/(app)/projects/[jobId]/page.tsx` | 改 — "新建翻译"链接改为 `/projects` |
| `frontend-next/src/app/not-found.tsx` | 改 — CTA 改为 `/projects` |

### Phase 3：播放器集成（依赖前置方案）

| 文件 | 改动 |
|------|------|
| `frontend-next/src/components/workspace/ResultMediaCard.tsx` | 来自前置方案 — 嵌入 succeeded 状态的 ProjectCard（仅展开时挂载） |

---

## 5. 过渡策略

| 旧入口 | 处理 |
|--------|------|
| `/translations/new` 页面 | 保留可访问，从侧边栏移除，页面改用 `TranslationForm` 组件 |
| `/tasks/current` 页面 | 保留可访问，从侧边栏移除，逻辑不改（保留 legacy selector） |
| `/projects/[jobId]` 详情页 | 降级为兼容页，主路径不再经过它 |
| `/workspace/[jobId]` 工作台 | **不变**，审核交互的核心页面，导航高亮改归 `/projects` |

**完全移除旧页面的条件**：新 UI 线上稳定运行 2 周 + 无用户反馈问题。

---

## 6. 改动文件清单

| 文件 | 类型 | 说明 |
|------|------|------|
| `frontend-next/src/components/workspace/TranslationForm.tsx` | **新** | 表单组件，`onCreated` + `mode` 契约 |
| `frontend-next/src/components/workspace/NewTranslationDialog.tsx` | **新** | 弹窗容器，提交 → 确认 → 自动关闭 |
| `frontend-next/src/components/workspace/ProjectCard.tsx` | **新** | 按状态自适应的项目卡片 |
| `frontend-next/src/features/jobs/selectors.ts` | 改 | 新增 `selectActiveTaskJob`（严格只认活跃状态） |
| `frontend-next/src/app/(app)/projects/page.tsx` | 改 | 升级为"视频翻译"主页 + 轮询 |
| `frontend-next/src/app/(app)/translations/new/page.tsx` | 改 | 改为引用 TranslationForm |
| `frontend-next/src/components/app-shell.tsx` | 改 | 侧边栏重组 + `/workspace/*` 激活态归属 |
| `frontend-next/src/app/(app)/workspace/[jobId]/page.tsx` | 改 | 取消/空态跳转改 `/projects` |
| `frontend-next/src/app/(app)/projects/[jobId]/page.tsx` | 改 | "新建翻译"链接改 `/projects` |
| `frontend-next/src/app/not-found.tsx` | 改 | CTA 改 `/projects` |

**不改动**：后端、审核面板组件（TranslationReviewPanel / VoiceSelectionPanel / VoiceReviewPanel）

---

## 7. 与播放器方案的关系

| 播放器方案（先做） | 本方案（后做） |
|-------------------|---------------|
| 新建 `ResultMediaCard` 组件 | 在 `ProjectCard` 的 succeeded 状态中嵌入 `ResultMediaCard`（仅展开时） |
| workspace 页替换下载区 | projects 页也使用同一组件 |
| 后端字幕 + 流式端点 | 前端消费这些端点 |

执行顺序：播放器方案 → 本方案 Phase 1 → Phase 2 → Phase 3

---

## 8. 验证

### 功能验收

1. 侧边栏：只显示"视频翻译"入口
2. `/projects` 页面标题为"视频翻译"，右上角有"新建翻译"和"当前任务"按钮
3. 点"新建翻译" → 弹窗出现 → 填写 URL → 提交成功 → 短暂确认 → 弹窗自动关闭 → 主页列表出现新卡片
4. "当前任务"按钮：有待审核任务时显示红点，点击跳 workspace；无活跃任务时隐藏
5. 项目卡片：succeeded 展开显示播放器，running 显示进度条，waiting 显示审核提示
6. 进度卡片无需手动刷新：running → waiting → succeeded 自动更新
7. 点"进入工作台" → 跳转 workspace 全页，审核交互正常
8. workspace 取消任务后 → 回到 `/projects`（不是 `/translations/new`）
9. `/workspace/[jobId]` 侧边栏高亮"视频翻译"

### 兼容性验收

10. 旧路由 `/translations/new` 直接访问 → 页面可用，表单可提交
11. 旧路由 `/tasks/current` 直接访问 → 正常跳转
12. `/projects` 空态 → 显示新建翻译引导（按钮打开 Dialog）
13. 并发上限命中 → 弹窗和页面模式提示一致

### 工程验收

14. `npm run build` 通过
15. `npm run lint` 通过
16. 手机端：弹窗 `max-h-[90vh] overflow-y-auto`，输入框聚焦时不被键盘遮挡
17. 默认展开卡片数 ≤ 3，首屏 `materials-availability` 请求 ≤ 1

---

## 9. 三方审核修订记录

### 采纳项

| # | 要点 | 来源 | 修订位置 |
|---|------|------|---------|
| 1 | TranslationForm 组件契约：`onCreated` + `mode` 开关 | 三方共识 | §2.3.1 |
| 2 | "当前任务"按钮新增严格 selector | 三方共识 | §2.4 |
| 3 | `/projects` 主页加轮询刷新策略 | 三方共识 | §2.2 数据刷新策略 |
| 4 | ResultMediaCard 仅展开时挂载 | 三方共识 | §2.2 卡片数据分层 |
| 5 | `/workspace/*` 导航高亮归属改到 `/projects` | CodeX + GPT | §2.1 + §3 |
| 6 | 弹窗简化：成功 → 短暂确认 → 自动关闭 | Gemini + GPT | §2.3.2 |
| 7 | `failed` 操作改"重新创建/删除" | CodeX | §2.2 状态表 |
| 8 | 迁移文件清单扩大 | CodeX | §3 导航迁移真值表 + §6 文件清单 |
| 9 | 默认展开上限 3 张 | GPT + Gemini | §2.2 默认展开规则 |
| 10 | `/projects/[jobId]` 降级为兼容页 | GPT | §3 |
| 11 | 验证补充 lint + 路由高亮 + 移动端 | 三方共识 | §8 |

### 部分采纳

| # | 要点 | 来源 | 处理 |
|---|------|------|------|
| 12 | 表单拆 3 层 Composer/FormView/Dialog | GPT | 拆 2 层够了（TranslationForm + 容器），不加 Composer 中间层 |
| 13 | 移动端用 Sheet 替代 Dialog | Gemini | Dialog + `max-h-[90vh] overflow-y-auto` 已足够 |
| 14 | EmptyState 加 `onAction` 回调 | CodeX | 不改组件，空态 CTA 用按钮触发 Dialog |

### 不采纳

| # | 要点 | 来源 | 理由 |
|---|------|------|------|
| 15 | 单独 Phase 1.5 数据层改造 | GPT | 轮询在 Phase 2 改页面时一起做 |
| 16 | 多视频互斥播放 | Gemini | 过度设计，用户极少同时展开多个视频 |
| 17 | 前端组件单测/snapshot | GPT | 项目无前端测试基础设施，导航重构不是引入测试框架的时机 |

### CodeX 二审补充（V2 → V2-final）

| # | P2 问题 | 修复 |
|---|---------|------|
| 18 | 伪代码字段名用 snake_case（`updated_at`/`stage`/`progress`），与前端 camelCase 不一致 | §2.4 selector 改为 `updatedAt`；§2.2 改为 `currentStage` / `progressMessage` |
| 19 | "重新创建"预填边界不明确（local_video 无法恢复、缺 service_mode） | §2.2 补充能力边界表 |
| 20 | 兼容入口改为纯 `/projects` 语义变弱，用户需多点一次 | 迁移真值表统一改为 `/projects?new=1` 自动打开 Dialog |
