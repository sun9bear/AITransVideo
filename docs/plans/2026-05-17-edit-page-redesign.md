# 视频修改页重排版 + 多段拆分 — 设计文档

**日期**：2026-05-17
**作者**：sun9bear + Claude
**状态**：设计稿（待 plan-design-review）
**范围**：`/workspace/{jobId}/edit` 页面重排版 + 多段拆分能力

## 0. TL;DR

1. **排版**：从「上 sticky 视频 + Tab + 厚卡片虚拟列表」改为「左视频 + 右段落联动」的左右分栏，行内紧凑双语；移动端折叠为上下堆叠。
2. **状态表达**：删除独立状态 chip 列，全靠重合成按钮的文案 + 颜色表达 5 种用户可见状态。
3. **拆分**：新增弹窗式拆分 UI；Phase 1 沿用现有 2 段拆分后端，Phase 2 新增 `split_many` + 智能预填（基于 word-level speaker_label + 中文标点）。
4. **视频源**：Phase 2 新增 `stream/source-video` kind，左侧视频上方 segmented control 切换原视频/中文视频。
5. **主题**：完全 token-only，跟随 AppShell 用户开关在 `data-theme="ink"` ↔ `data-theme="ink-dark"` 间切换。

视觉决策来源（mockup 文件 git-ignored，保留在 `.superpowers/brainstorm/`）：
- 主题方向 — token-only，确认两套都用
- 段落行 v4 — 双语行 + 按钮即状态 + 紧凑草稿条
- 拆分 modal Option A — 弹窗 + 智能预填

---

## 1. 目标与范围

### 1.1 改的部分

- 路由：`frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx` 整体重排
- 子组件：`SegmentCard` 重写为紧凑 `SegmentRow`（保留逻辑、收窄视觉）
- 新组件：`SplitSegmentDialog`（弹窗式拆分 UI）
- 新组件：`CurrentSegmentOpsPanel`（左侧视频下的当前段操作区）
- 后端（Phase 2 only）：
  - `GET /jobs/{id}/stream/source-video` — 新增 kind，复用现有 stream 框架
  - `POST /jobs/{id}/segments/{sid}/split-many` — 新接口，取代多次调用现有 split
  - `services/jobs/editing_segments.py::split_editing_segment_many()` — 新内核函数
  - `services/transcript.py::detect_speaker_changes_in_segment()` — Phase 2 智能预填的检测函数

### 1.2 不改的部分

- 后端 segment CRUD（`patch_editing_segment`、`mark_segment_status` 等）
- `usePlayerSegmentSync` hook（已经支持 timeupdate + 二分查找）
- `SegmentVirtualList`（已经支持 scrollToId + activeSegmentId 自动滚动 + 当 textarea focused 时不抢滚）
- `VoiceModifyTab`（音色修改 Tab，保持现状）
- 现有 6 种 segment 内部状态语义（`accepted` / `text_dirty` / `tts_loading` / `tts_dirty` / `tts_failed` / `voice_dirty`）—— 只改前端展示，不改后端 state 词表
- enter-edit / editing/cancel / editing/commit 三个状态机端点
- 批量重合成 + 取消批量合成的 task 编排

### 1.3 显式排除

- **音色修改流程不重做**：保留 `VoiceModifyTab` 整块。仅在段落行的说话人下拉里允许「换说话人归属」，音色微调（克隆、引擎切换、试听音色）一律走音色 Tab。
- **不引入新依赖**（项目 14 个 runtime deps 上限维持）。
- **不在 Phase 1 改后端**（沿用现有 split / 不切原视频）。
- **不做 keyboard shortcuts 系统**（J/K/Space 等）—— 留作后续 PR。

---

## 2. 整体布局

### 2.1 桌面（≥ 1024px）

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Sticky Header — 取消修改 │ ⓘ 修改概览 │ 一键合成所有未合成 N 段 │ 确认修改 ↗ │
├──────────────────────────┬─────────────────────────────────────────────────┤
│                          │ Tab: ⊙ 翻译修改 ┊ 音色修改                       │
│  ┌──────────────────┐    ├─────────────────────────────────────────────────┤
│  │                  │    │ 段落列表（虚拟滚动，maxHeight: calc(100vh-...) │
│  │  视频 16:9       │    │   ┌──────────────────────────────────────┐    │
│  │  (sticky top)    │    │   │ 01 · 00:12  | 双语行 | [拆分][重合成]│    │
│  │                  │    │   │ 02 · 00:18  | 双语行 | [拆分][待合成]│    │
│  └──────────────────┘    │   │ 03 · 00:26  ★active双语 |  ...        │    │
│  Phase 2 segmented:      │   │   ↳ 草稿面板（紧贴文本）              │    │
│  ⓐ 原视频  ⓑ 中文视频 ✓  │   │ 04 · 00:32  | ...                     │    │
│                          │   │ ...                                    │    │
│  当前段操作区:           │   └──────────────────────────────────────┘    │
│   00:26 · 说话人 A       │                                                 │
│   ▶ 试听原音             │                                                 │
│   ▶ 试听新草稿（如有）   │                                                 │
│   [接受 / 丢弃 / 重合成] │                                                 │
└──────────────────────────┴─────────────────────────────────────────────────┘
```

CSS 网格（Tailwind）：

```
container: grid-cols-1 lg:grid-cols-[minmax(360px,400px)_1fr]
left col: sticky top-{头部高度} h-fit (自然占据 video + ops panel)
right col: 自然滚动，virtual list 占主要高度
```

### 2.2 移动端（< 1024px）

```
┌────────────────────────────────────┐
│ Header（堆叠 / 折叠按钮）           │
├────────────────────────────────────┤
│ Sticky 视频区（max-height: 30vh）   │
│ (Phase 2: 原/中文 segmented)        │
├────────────────────────────────────┤
│ Tab: 翻译 / 音色                    │
├────────────────────────────────────┤
│ 段落列表（每行操作按钮内嵌）        │
│  - 默认折叠左侧 ops panel          │
│  - 拆分 modal 占整屏（drawer 形态）│
└────────────────────────────────────┘
```

### 2.3 视频 sticky 锚定

- 桌面：左列 `sticky top-{headerHeight}`，滚动时视频不动、列表滚动 → 与 `usePlayerSegmentSync` 的当前段联动配合
- 移动：视频 `sticky top-{headerHeight}`，自身在视口顶部，列表下滚

### 2.4 首次进入行为（Pass 1 决策）

页面打开 / 刷新时：

1. 加载段落 + 状态完成后，定位**首段 dirty**（按 segment_status 中 `text_dirty` / `voice_dirty` / `tts_failed` / `tts_dirty` 中 segment_id 最小者）
2. 自动滚动该段进入视图（沿用 `SegmentVirtualList.scrollToId` + `center` align）
3. 视频 `currentTime = activeSegment.startMs / 1000`（不自动 play —— 让用户决定是否听）
4. Sticky header 显示 status pill「N 段待处理」（N = 所有非 accepted 段数）
5. **没有 dirty 段时**（用户回访 / 看完了）：默认选中首段，pill 显示「全部已通过」
6. **0 段时**（异常 / 任务尚未生成）：占位「正在加载段落…」骨架屏，10s 后仍 0 段则报错「段落加载失败」

> 选 A "自动定位到首段 dirty" 的理由：用户进修改页 90% 是来续上次中断 / 处理新发现的问题，预填焦点能省 1-2 次手动滚动。

### 2.5 Header action bar 布局（默认设计，未询问，可在 review 后调整）

```
| 取消修改 |       | N 段待处理 · K 段草稿待审 |    | 一键合成 N 段 |    | 确认修改 ↗ |
↑ 左对齐    ↑ flex-grow ↑ 中部 status pill         ↑ 主要操作（按需）  ↑ 右对齐
```

- 左：「取消修改」（ghost）
- 中：状态 pill — 由 N 段待处理 / K 段草稿待审 / 全部已通过 三态切换
- 右组：「一键合成 N 段」（N=0 时隐藏） + 「确认修改 ↗」（primary）
- 移动端：「取消」+「确认」保留在顶部 inline；批量合成 + status pill 下移到视频上方一行

### 2.6 空状态 IA

| 场景 | 段落列表 | 左侧 ops panel |
|------|---------|----------------|
| 加载中 | 3-5 个段卡 skeleton（虚拟列表占位） | 视频 placeholder + 「正在加载…」 |
| 0 段（异常） | 「未找到段落 — 任务可能未走完识别阶段」+ 跳转按钮 | 视频可播 + 「无段落可修改」占位 |
| 所有段 accepted | 列表正常显示 + header pill「全部已通过」 | 选中首段，显示「可继续修改任意段」 |
| 全部 dirty | 列表正常 + header pill「N 段待处理」 | 选中首段 dirty，显示该段操作 |

---

## 2a. 用户旅程（Pass 3 决策）

### 2a.1 四个典型场景

#### 场景 A · 首次进入（刚做完任务）

| 步骤 | 用户做什么 | 用户感受 | UI 怎么支持 |
|------|----------|---------|------------|
| 1 | 点击 succeeded 任务的「修改」按钮 | 好奇 / 期待 | enter-edit 期间显示「正在准备修改环境…」（仅 > 10s 时） |
| 2 | 进入页面 | 信任 / 评估 | 自动定位首段 dirty；若无 dirty 则首段 + pill「全部已通过」 |
| 3 | 播放视频 | 听到中文配音 | 视频 controls 默认 paused，用户主动点 ▶ |
| 4 | 觉得某段不对 → 点段落 | 准确 / 受控 | 段落高亮 + 视频 seek 到该段；点中文进入 inline 编辑 |
| 5 | 改完 → 点击「待合成」 | 安心 | 按钮变「合成中…」+ 旁边拆分按钮 disabled，避免误操作 |

#### 场景 B · 续上次中断（高频）

| 步骤 | 用户做什么 | 用户感受 | UI 怎么支持 |
|------|----------|---------|------------|
| 1 | 进修改页 | 「我上次改到哪了」 | 首段 dirty 自动滚到视图 + 高亮 + 视频 seek，pill「N 段待处理」 |
| 2 | 看红色待合成按钮 | 「就是这里」 | 红色 primary 是唯一高对比按钮，无干扰 |
| 3 | 一键合成所有 | 「批量省事」 | 顶部 N 段待处理按钮 + 进度条 + 单段失败不阻塞 |
| 4 | 草稿一段段听 + 接受/丢弃 | 「逐段质检」 | 草稿面板单行紧凑，▶ 直接试听不打断滚动 |

#### 场景 C · 定向修改某一段

| 步骤 | 用户做什么 | 用户感受 | UI 怎么支持 |
|------|----------|---------|------------|
| 1 | 心里有特定时间点 / 关键词要修改 | 急迫 | 段落列表支持 Cmd+F 浏览器原生搜索（双语文本可见即可被搜） |
| 2 | 跳到该段 | 准确 | 段号 + 时间码同行，扫读效率高 |
| 3 | 改 / 拆分 / 重合成 | 直接 | 行内常驻 拆分 + 重合成 两按钮，0 学习成本 |

#### 场景 D · 全部 OK，准备提交

| 步骤 | 用户做什么 | 用户感受 | UI 怎么支持 |
|------|----------|---------|------------|
| 1 | 滚到底 | 「都看完了」 | header pill「全部已通过」 + 「确认修改 ↗」按钮 enabled |
| 2 | 点确认 | 「但合并到原任务会不会丢草稿」 | 确认 dialog：overwrite vs copy_as_new 二选一 + 草稿对齐警告（如存在 unsynced） |
| 3 | 等任务跑 alignment + publish | 「会很久吗」 | 跳出修改页，回任务详情看进度（commit 已在后端 plan 落地，不重做） |

### 2a.2 三时间尺度（Norman）

- **5 秒 visceral**：进页面看到「左视频 / 右段落表」+ ink 主题（cinnabar 红 = "焦点" / 灰 = "已完成"）→ 第一感受 **专业、有秩序、紧凑**，不是杂乱表单
- **5 分钟 behavioral**：连续修改 5 段后，用户应感到**省力**——按钮位置稳定（每段行内右侧）、状态视觉一致、双语对照不用切换、键盘 Tab 在段间跳转流畅（Pass 6 a11y 补）
- **5 年 reflective**：用户记住的是「中文视频翻译里**修改流程最清爽**的产品」，区别于竞品的「上传等结果 black box」—— 这条要求 ink 主题坚持 + 减法 + 中文优先排版（DESIGN.md §4.2 inherit）

### 2a.3 反向：哪些情绪要避免

- ❌ 进页面 5 秒内看不到任何「下一步是什么」 → 用 status pill + 红色 primary 按钮明示
- ❌ 改完一段以后不知道是否已保存 → toast + 状态 chip 即时回显
- ❌ 一键合成开始后没法取消 → §6.4 提供「取消批量合成」（项目里 D39 已实现）
- ❌ 拆分误删切点没法 undo → modal 提供「↺ 重置切点」按钮（§5.6 已含）

---

## 3. 左侧：视频 + 当前段操作区

### 3.1 视频区

- 容器：现有 `<video>` 改为 `width: 100%; aspect-ratio: 16/9`；`object-contain` 保持
- src（Phase 1）：始终是 `buildStreamUrl(jobId, 'video')`（即 publish.dubbed_video）
- src（Phase 2）：根据 segmented control 状态切换 `video` ↔ `source-video`
- 上方（Phase 2 only）：

```tsx
<div className="flex gap-1 rounded-md bg-card p-1">
  <button className={cn(isSource && "bg-primary text-primary-foreground")}>
    原视频
  </button>
  <button className={cn(!isSource && "bg-primary text-primary-foreground")}>
    中文视频
  </button>
</div>
```

- 切换时记住 currentTime，新视频 onLoadedMetadata 后 seek 回原位置

### 3.2 当前段操作区（CurrentSegmentOpsPanel）

跟 `activeSegmentId`（来自 `usePlayerSegmentSync`）绑定。activeSegmentId 为 null 时显示「点选段落开始修改」占位。

内容（按当前段状态分支）：

| 状态 | 显示内容 |
|------|---------|
| accepted | 段时间码 / 说话人 / ▶ 试听原音 |
| text_dirty / voice_dirty | + 红色「待合成」按钮（与右侧行内按钮联动，二选一点都行） |
| tts_loading | 「合成中…」+ spinner |
| tts_dirty | 草稿试听 ▶ + 时长偏差信息 + 接受 / 丢弃按钮 |
| tts_failed | 红色错误条 + 重试按钮 |

> **左右冗余怎么避免**：左侧 ops panel 显示「该段所有可执行操作的语义全集」，右侧行内按钮是「快捷方式」。两边点击效果完全一致 — 用户点哪个都行，UI 状态实时同步。

### 3.3 草稿面板放哪

按 Q2 v4 决策：**草稿面板只在右侧行内展开**（紧贴段落文本，单行紧凑）。左侧 ops panel 也镜像显示草稿的「试听/接受/丢弃」，但视觉以行内为主。

> 为什么不只在左侧？因为用户可能同时有多个 tts_dirty 段，左侧只能聚焦一个；右侧行内能并行展示，列表里一眼看到「N 段草稿待审」。

### 3.4 试听原音的渲染位置（2026-05-17 实施时澄清）

**问题**：原方案里 §3.2 写左 ops panel「▶ 试听原音」，但段落行也有 ▶ 按钮。两边都渲染 `<audio controls>` 会导致 SegmentVirtualList 的高度测量出错（行内 `<audio>` 元素加载 metadata 之前高度 ≈ 0，加载后 ~40px，但虚拟列表 cache 的是初始高度 → 下一行重叠当前行）。

**澄清结论**（按 §3.2 "左侧 ops panel = 操作全集，右侧行内按钮 = 快捷方式" 的原则细化）：

| 入口 | 渲染什么 |
|------|---------|
| 左侧 `CurrentSegmentOpsPanel` | **完整 `<audio controls>`** — 用户可以 scrub / pause / seek，宽屏空间充足，无虚拟列表问题 |
| 右侧 `SegmentRow` 的 ▶ 按钮 | **不渲染 `<audio>` 元素**。点击 → offscreen `new Audio(url)` + `.play()` + 监听 play/pause/ended 切换图标。再点暂停。无可见 controls，无行高变化 |

实施细节（`SegmentRow`）：
- `audioInstanceRef = useRef<HTMLAudioElement | null>`
- 第一次点：`onPreviewSource` 拿 url → `new Audio(url)` → 缓存 ref → `play()`
- 再次点（playing）：`pause()` + 图标变 ▶
- 再次点（paused）：`currentTime = 0` + `play()` + 图标变 ⏸
- unmount：`pause()` + 释放 src 避免 leak

**草稿试听**（tts_dirty 状态）仍按 §3.3 行内 `<audio controls>` 渲染——草稿是任务可见状态的一部分，初始 mount 就在 DOM 里，虚拟列表能正确测量。SegmentVirtualList 同步加 per-item ResizeObserver 作为兜底（防止后续动态内容触发同类问题）。

---

## 4. 右侧：段落列表

### 4.1 行结构（双语行）

每行三列 Grid：

```
grid-template-columns: 100px 1fr 230px
gap: 12px
```

- **左列**（100px）：
  - 第一行：段号 + 时间（横排）`01  00:12`
  - 第二行：说话人下拉（chip 样式，名字 only，无音色特征后缀）
- **中列**（1fr）：
  - 英文原文（小一号，muted-foreground 色）
  - 中文译文（正文色）
  - （tts_dirty 时）下方紧贴「草稿单行面板」
  - （tts_failed 时）下方紧贴「错误提示行」
- **右列**（230px，flex justify-end）：
  - 拆分 button（常驻）
  - 重合成 button（常驻，文案/颜色按状态变化）

### 4.2 active row 视觉

- `border-left: 2px solid var(--primary)` + `margin-left: -2px` + `padding-left` 补偿
- `background: rgba(primary, 0.06)`（token: `--primary` + alpha）

> **AI slop 防御注解**：cinnabar 左 border 在通用 slop blacklist 第 8 条「colored left-border on cards」。本设计里它是**功能性 active 指示**，唯一负责回答「现在视频播到哪段」/「我点击的是哪段」。比 font-weight 调粗或 icon 标记更扫读，且与视频联动（`usePlayerSegmentSync.activeSegmentId`）实时映射，**不是装饰**。reviewer 不应把它当成 slop 标记。

### 4.3 编辑入口

- 点击英文 → 进入英文 inline 编辑（textarea, autosize）
- 点击中文 → 进入中文 inline 编辑
- 离焦保存（debounced 300ms）
- 后端响应回 segment_status，前端镜像

### 4.4 说话人下拉

- 触发：点击 chip 展开 dropdown menu
- 选项：所有 editing-mode speakers + 「+ 新增说话人」
- 选中后调用 `patchSegmentText(jobId, segmentId, { speaker_id })`
- toast 提示「已改为说话人 X；重合成时将使用其音色」
- **「+ 新增说话人」复用现有 `EditPageSpeakerCreateDialog` + `listEditingSpeakers` / `retryEditingSpeakerProfile`**（已在当前 [edit/page.tsx](frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx) 实现，本计划不重做后端）
- **不在这里调用音色 API**（避免付费 API 自动触发）

---

## 5. 拆分 Modal

### 5.1 触发

点击右侧行的「拆分」按钮 → 打开 `SplitSegmentDialog`（受控 `<Dialog>`，shadcn/ui 已有）。

### 5.2 Modal 结构

```
┌─────────────────────────────────────────────┐
│ 拆分段落 02 · 00:18–00:26                  │
├─────────────────────────────────────────────┤
│ 智能提示条（Phase 2 才有）                  │
│  智能预填 · 检测到 X 个说话人切点 + Y 个标点 │
├─────────────────────────────────────────────┤
│ 英文原文（红虚线 = 切点，✕ 可删除）         │
├─────────────────────────────────────────────┤
│ 中文译文（红虚线，跟英文切点联动）          │
├─────────────────────────────────────────────┤
│ 拆分预览：将拆分为 N 段                     │
│  ① ... [说话人 A ▾]                         │
│  ② ... [说话人 A ▾]                         │
│  ③ ... [说话人 B ▾]                         │
├─────────────────────────────────────────────┤
│ [↺ 重置]    [取消]    [拆分为 N 段]         │
└─────────────────────────────────────────────┘
```

### 5.3 切点交互

- 用户在英文或中文文本里点击字符间隙 → 插入红色虚线竖条
- 虚线竖条上方有 ✕ 删除按钮
- 点击英文切点 → 自动计算中文对应位置（按字符比例 + 已有的 word timing 微调）
- 点击中文切点 → 同理映射到英文
- Phase 1 限制：最多 1 个切点（产出 2 段）
- Phase 2：无切点数量上限（产出 N 段）

### 5.4 智能预填（Phase 2）

**性能约束（Eng review §4 + Codex 二审 #4）**：
- 不前端拉全文件（30min+ 视频 raw_assemblyai 可达 50MB，浏览器 JSON.parse 慢/卡）
- **新加后端端点** `GET /jobs/{job_id}/segments/{sid}/word-context` → 返该段时间范围内的 words（最多 500 KB）。modal 打开时调一次即可
- modal 关闭后释放内存
- 端点失败 / words 无 speaker_label → 智能提示条文案降级为「检测到 X 个标点切点（未识别说话人）」，不显示「说话人切点」

打开 modal 时：

1. 调 `GET /jobs/{job_id}/segments/{sid}/word-context`，拿当前段时间范围内的 words
2. 检测 speaker_label 切换点 → 加入候选切点列表
3. 检测中文标点（。！？） → 加入候选切点列表
4. 去重 + 合并相邻切点（容差 < 200ms）
5. 切点上限 5 个（避免噪声）；< 1 时不提示
6. 预填 modal 时间轴，speaker dropdown 按 speaker_label 分配
7. 智能提示条显示「检测到 X 个说话人切点 + Y 个标点切点」

### 5.5 Phase 1 简化

- 不显示智能提示条
- 切点数量硬限制为 1
- 提交时调用现有 `split_editing_segment(jobId, sid, { split_source_index, split_cn_index, speaker_a, speaker_b })`
- 预览区只显示 2 个 piece
- **Phase 1 modal 底部加 UX hint**（Eng review #3 缺口）：
  > 「目前支持拆分为 2 段。一段内出现多个说话人时，多段一次性拆分将在下个版本支持。」
  小字 muted-foreground 色，紧靠「拆分为 2 段」按钮上方。避免用户期望落空。

### 5.6 Phase 2 多段提交

- 新接口 `POST /jobs/{id}/segments/{sid}/split-many`（**注意路径**：用 `segments/{sid}/{action}` 模式，**不是** `editing/segments/...`）
- **Gateway 路由 + Post-Edit gate 集成（Codex 三审 P1 #3 修正）**：现有 [_POST_EDIT_SEGMENT_ACTIONS](gateway/job_intercept.py:2607) = `{update, status, regenerate-tts, accept-draft, discard-draft, split, preview-source}`，matcher 期望 `segments/{sid}/{action}` 3 段路径。新 action 必须：
  1. 添加 `"split-many"` 到 `_POST_EDIT_SEGMENT_ACTIONS` frozenset（保留显式 allowlist 风格）
  2. 验证 `_is_post_edit_mutation_subpath` 自动识别新 action（基于 frozenset 不需要额外改动）
  3. 自动获得：feature flag 检查 + editing 状态 gate + ownership 校验 + lock dispatch（全套 post-edit 套路）
  4. 同步加入 [docs/plans/2026-04-18-studio-post-edit-plan.md](docs/plans/2026-04-18-studio-post-edit-plan.md) 端点白名单表（CLAUDE.md §"Studio 视频修改工作流"也列了端点清单，应一并更新）
  5. **`word-context` 读端点的 Gateway gate（Codex 四审 P1 #2 修正）**：现状 Gateway 只对 POST 走 `_is_post_edit_mutation_subpath` ([job_intercept.py:1616](gateway/job_intercept.py:1616)) —— `_POST_EDIT_SIMPLE_GET_SUBPATHS` **不存在**。现有 GET `editing/segments` / `editing/voice-map` 也是走通用 proxy，不带 post-edit feature flag gate（这是已有惯例）。所以：
     - `GET /jobs/{id}/segments/{sid}/word-context` 走通用 proxy。**架构边界（Codex 五审 P2 #1 修正）**：
       - **Gateway 通用 proxy 入口**：负责 ownership 检查（已有行为，[_verify_job_ownership](gateway/job_intercept.py) 对所有 jobs/{id}/* 路径都跑）—— Job API 不具备用户身份，不能自己做 ownership
       - **Job API handler 职责**：只检查 `record.status == "editing"` + `project_dir` 有效 + segment_id 在当前 editing segments 中存在
       - **feature flag parity**：不在 Job API 做（Gateway/frontend 才看得到 `settings.enable_post_edit`）。如果未来要 GET 也带 feature flag gate（与 POST 一致），**新增 Gateway GET gate 分支**（加 `_POST_EDIT_SIMPLE_GET_SUBPATHS` frozenset + 镜像 line 1616 的 POST 分支）—— 但本计划范围内**不做**：前端 NEXT_PUBLIC_ENABLE_POST_EDIT gate 已经保证 modal 入口不渲染，攻击面足够低
     - 前端只在 modal 打开时调（modal 入口已被 NEXT_PUBLIC_ENABLE_POST_EDIT gate 住，feature off 时压根不渲染拆分按钮）
- 入参：

```json
{
  "cuts": [
    {"source_index": 17, "cn_index": 7, "speaker_id": "A"},
    {"source_index": 42, "cn_index": 18, "speaker_id": "B"}
  ],
  "trailing_speaker_id": "A"
}
```

- 后端内核：`split_editing_segment_many()`（新函数，模拟现有 single split 的语义但一次成 N 段，复用 word timing 切音频）
- segment_id 命名：`<base>_a` / `<base>_b` / `<base>_c` / ... 顺序后缀，冲突时叠加数字
- audit 事件：单个 `split_many` 事件（不是 N 个 split 事件）+ payload 含完整切点
- **Draft wav 清理（继承 P1-16 模式）**：拆分后**删除** parent segment_id 对应的 `editor/editing/tts_segments_draft/{parent_sid}.wav`。N+1 个新 sub-segment 都标 `text_dirty`，用户重新发起合成。现有 `split_editing_segment()` [editing_segments.py:1179](src/services/jobs/editing_segments.py:1179) 已建立此模式（P1-16 audit 2026-05-07），split_many 必须等效继承。
- **voice_map 迁移（继承 P0-8 模式 · Codex 二审 P1 #2）**：parent segment_id 在 `editor/editing/voice_map.json` 里如有 voice override 条目，**复制到所有 N+1 个新 sub-segment**（不只是前两个）。现有 single split 已建立此模式 [editing_segments.py:1145-1167](src/services/jobs/editing_segments.py:1145)，N 段拆分必须对每个 sub-seg key 都写入相同 override，由用户后续在音色 Tab 单独调整某段。回归测试覆盖：parent 有 override → 拆 3 段后 voice_map 含 3 个 sub-key 都指向同一 voice_id。
- **原子性（Eng review #1.1 + Codex 二审 P1 #1 决策）**：**best-effort 双文件 + ordering + recovery**
  - 在 `file_lock(project_dir / "editor" / "editing" / "segments.json")` 保护下：
    1. 校验所有 cuts（任一切点越界 / 产生空段 / 切点重合 → 抛 422，不改 baseline）
    2. 在内存里构造完整新 segments list（N+1 段）+ 完整 segment_status 映射 + 完整 voice_map override
    3. 写 tmp 文件 `segments.json.tmp` + `segment_status.json.tmp` + `voice_map.json.tmp`
    4. **三文件 rename 不是 POSIX 事务**（Codex 三审 P1 #1 修正）：现有 `load_segment_status` [editing_segments.py:235](src/services/jobs/editing_segments.py:235) 不做 orphan filter，原样返回 dict；`editing_batch.py:73` 也直接遍历；前端 dirtyCount [page.tsx:969](frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx:969) 直接读 status。**所以不能依赖现有 filter 兜底**，必须把恢复做成可执行代码。
    5. **方案：write-ahead journal + 幂等 reconcile**（替代二审的"ordering + 现有 filter"假设；进一步收紧 Codex 四审 P1 #1 风险）：
       a. 步骤 3 之前先写 `.split_journal_{parent_sid}.json` 单文件 — 含完整新 segments + 新 status + 新 voice_map，os.replace 原子落地
       b. 三文件 rename 之前 journal 必须已经落盘
       c. 改三个 loader 加 reconciler：`load_editing_segments` / `load_segment_status` / `load_voice_map` 入口先 scan `editing/` 目录是否有 `.split_journal_*.json` → 走 reconcile
       d. **reconcile 三态判断（Codex 四审 P1 #1 修复）**——避免 stale journal 覆盖用户后续编辑：
          - **State A**：segments.json 仍含 parent_sid（split 尚未提交）→ journal 是新鲜的，按 journal 强制对齐三文件 + 删 journal
          - **State B**：parent_sid 已不在 segments.json + 所有 sub-segment_ids 都已在 segments.json（split 已提交，仅 journal 删除失败）→ journal 是 stale，**只补缺失的 status / voice_map 条目**（如果 sub-seg 在 status 里缺，写 text_dirty；如果 voice_map 缺，按 journal 的 override 补），**不覆盖现有 segment 内容**（避免 stale journal 覆盖用户后续编辑）→ 然后删 journal
          - **State C**：状态不一致（既不是 A 也不是 B —— 比如 segments 部分含 sub-segs 部分含 parent）→ 抛 `EditingCorruptionError`，让用户/运维介入；不自动恢复，避免数据丢失风险
       e. 三文件全部 replace 成功后**也删 journal**（happy path）
       f. **同步改批量入口** [editing_batch.py:73](src/services/jobs/editing_batch.py:73)：必须经过 `load_segment_status`（已带 reconcile），不能用其它绕过路径
  - 单元测试覆盖（Codex 二+三+五审 P1 #1 强化，落到 `tests/test_split_many_kernel.py`）：
    - **基础原子性**：
      - 校验失败：assert baseline 三文件未变 + tmp 全清 + journal 不存在
      - 模拟三文件 rename 中任一步失败 → assert journal 仍在 → 下次 load_editing_segments / load_segment_status 自动 reconcile + 删 journal
    - **State A**（journal 新鲜，split 未提交）：写 journal + 不动三文件 → load 时 reconcile 把三文件按 journal 对齐 → 删 journal → 再 load assert 状态一致
    - **State B**（Codex 五审 P2 #2）：写 journal + 三文件全 replace 成功 + 模拟 journal 删除失败 → **用户编辑某 sub-seg 的 cn_text** → 下次 load 看到 stale journal → reconcile 只补缺失（如果有）/ **不能覆盖刚编辑的 cn_text** → assert 用户编辑得到保留 + journal 被删
    - **State C**（Codex 五审 P2 #2）：人工构造"既有 parent 又有 sub-seg"的混乱状态 + 留 journal → load 抛 `EditingCorruptionError`（不自动恢复）→ assert error message 包含「不一致状态需运维介入」+ assert 三文件 / journal 都未被改动
    - **批量合成入口**：assert [editing_batch.py:73](src/services/jobs/editing_batch.py:73) 经过 reconciler，不会拿到 orphan status 计入 dirtyCount

---

## 6. 状态机映射

### 6.1 6 内部状态 → 5 视觉表达

| 内部状态 | 重合成按钮文案 | 按钮颜色 | 其他视觉 |
|---------|--------------|---------|---------|
| accepted | 重合成 | ghost gray | — |
| text_dirty | 待合成（X秒） | primary red | — |
| voice_dirty | 待合成（X秒） | primary red | — |
| tts_loading | 合成中… | warn yellow + spinner | 拆分按钮 disabled |
| tts_dirty | 草稿待审 ↓ | warn yellow | 行内展开草稿面板 |
| tts_failed | 重试合成 | danger red outline | 中文下方错误提示行 |

> **不在按钮上写"X 点"**（Codex 二审 P1 #4）：项目里 Gateway `metering_snapshot` 是计费唯一真源，前端硬写"1 点"会让前端变成 pricing source-of-truth + 跨 provider 单价不同（MiniMax / VolcEngine / CosyVoice 单价差异大）+ 实际扣点在 task 完成后由后端 ledger 决定。按钮只展示**时长**（用户可感知），扣点以 task 完成后的 toast / 用户中心账单为准。如果以后要预估，加 `GET /jobs/{id}/quote/regenerate-segment` Gateway 端点。

> **「修改中 / 已通过 / 待合成」三个 chip 不再存在**。"当前正在编辑/聚焦"的语义由 active row 高亮 + 视频联动表达。

### 6.2 active 与状态的解耦

- active row：当前用户聚焦/视频播放到的段，**与内部状态无关**
- 状态：完全由按钮颜色 + 文案表达
- 同一个段可以同时是 active + text_dirty —— 视觉是 红色 left-border + 红色 primary 按钮

### 6.3 批量按钮联动

顶部「一键合成所有未合成 N 段」按钮的 N = (text_dirty + voice_dirty + tts_failed 段数)。

### 6.x Typography（Pass 5 决策 · 跟项目现有）

跟 `frontend-next/src/app/globals.css` 全局变量，**不新增字体资源**：

- `--font-sans`: Inter / PingFang SC / HarmonyOS Sans SC / Microsoft YaHei / Noto Sans SC（中文按操作系统就近）
- `--font-mono`: JetBrains Mono / Fira Code
- 数值型 / 时间码用 `font-variant-numeric: tabular-nums`（已在 v4 mockup 用了）

surface 级字号表（rem 基准 16px = 1rem）：

| Surface | font-size | line-height | weight | 备注 |
|---------|-----------|-------------|--------|------|
| Header 标题（任务名 / breadcrumb） | 0.875rem (14px) | 1.4 | 500 | 不用 hero level |
| Status pill | 0.75rem (12px) | 1.4 | 500 | tabular-nums |
| 段落行 — 段号 + 时间码 | 0.6875rem (11px) | 1.4 | 600 (idx) / 400 (tc) | tabular-nums |
| 段落行 — 说话人 chip | 0.625rem (10px) | 1.4 | 400 | — |
| 段落行 — 英文原文 | 0.6875rem (11px) | 1.5 | 400 | muted-foreground 色 |
| 段落行 — 中文译文 | 0.75rem (12px) | 1.6 | 400 | foreground 色（高对比） |
| 段落行 — 按钮 | 0.6875rem (11px) | 1.4 | 400 / 500（primary） | — |
| 拆分 modal — 标题 | 1rem (16px) | 1.4 | 600 | — |
| 拆分 modal — 智能提示条 | 0.6875rem (11px) | 1.4 | 400 | warn 色 |
| Toast | 0.875rem (14px) | 1.5 | 500 (title) / 400 (desc) | — |

**DESIGN.md §2.3 援引**：中文「不用 ultra-light」（最低 400）+「comfortable line-height」（中文行高 1.5+）+「长解释拆 bullet」（这条体现在双语行的英/中分两行）。

**不规定的事**：H1/H2/H3 不出现在修改页（无 marketing 风格 hero）；不引新字体；不动 `--font-heading` token。

### 6.4 完整交互状态矩阵（Pass 2 决策）

5 个高频 feature × 5 状态。每格描述用户**看见什么**（非后端行为）：

| Feature | Loading | Empty | Error | Success | Partial |
|---------|---------|-------|-------|---------|---------|
| **拆分 modal** | modal 内容半透明 + 中央 spinner + 按钮 disabled | 未选切点：「拆分为 N 段」disabled + hint「在文本中点击添加切点」 | modal 顶部红色 banner「切点位置产生空段，请调整」+ 越界切点 ✕ 高亮闪烁 | modal 关闭 + toast「已拆为 N 段」+ 自动 scrollToId 拆后首段 | N/A（原子操作） |
| **说话人切换** | chip 内嵌 spinner 替换 ▾，dropdown disabled | editing speakers = 0 时只显示「+ 新增说话人」单项 | toast「改说话人失败：[原因]」+ chip 文案回滚 | 静默成功 + toast「已改为说话人 X，重合成时将使用其音色」 | N/A |
| **视频 source 切换**（Phase 2） | video 元素 readyState < 3 时 segmented control 显示骨架 | 原视频 artifact 不存在：「原视频」按钮 disabled + tooltip「该任务未保留原视频」 | stream/source-video 返 5xx → toast「原视频加载失败」+ 自动回退中文视频 + 同位置 currentTime | 静默切换，currentTime 保留，无 toast | 切换中再次切回：取消上一个请求，按最后一次操作生效 |
| **批量合成（一键合成 N 段）** | 按钮变「合成中 (k / N) …」+ 旁边「取消批量」按钮 + 顶部全局进度条 + **所有单段重合成按钮 disabled，tooltip 「正在批量合成 (k / N)」** | N = 0 时按钮整体隐藏 | 单段失败：toast「第 k 段合成失败：[原因]」+ 该段标 tts_failed + 继续后续段 | toast「N 段已合成，K 段草稿待审」+ 状态 pill 更新 | 用户取消：toast「已合成 k 段，剩余 N-k 段未处理」+ 剩余段保持 dirty + 单段按钮恢复 enabled |
| **单段重合成**（行内按钮 / 左 ops panel） | 按钮变「合成中…」+ spinner + 拆分按钮 disabled | accepted 段点击：直接发起合成（无 confirm，单击快速迭代；扣点由 Gateway ledger 在 task 完成后结算 + 用户中心账单展示，按钮只显示时长不显示点数） | toast「合成失败：[原因]」+ 按钮文案 →「重试合成」红描边 + 中文下方错误行 | 按钮变「草稿待审 ↓」+ 行内展开草稿面板 | N/A · 批量进行中时此按钮 disabled |

**通用 toast 约定**：
- 成功：绿色 / 2.5s auto-dismiss / 单行
- 失败：红色 / 5s auto-dismiss / 双行（标题 + 原因摘要 + 「详情」展开）
- 加载：黄色 / 不 auto-dismiss / 配 spinner（仅长任务，如批量合成）

---

## 7. 顶部 Tab

- 位置：**仅在右列上方**（不占整行）
- 内容：「翻译修改」（默认） / 「音色修改」
- 切到「音色修改」时：左侧视频不变（用户还能播），右列变成 `VoiceModifyTab`
- 切到「音色修改」时段落列表的滚动位置应保留（用 ref 缓存）

> 这条比现状（Tab 在视频和列表之间占整行）多了一个好处：音色修改时视频不被遮挡，用户可以一边听一边调音色。

---

## 8. Phase 1 / Phase 2 边界

### 8.1 Phase 1（前端重构，无后端改动）

- ✅ 左右分栏布局（桌面）+ 上下堆叠（移动）
- ✅ 段落行 v4（双语 + 持久按钮 + 按钮即状态）
- ✅ 草稿面板紧凑行内化
- ✅ 当前段操作区（左侧）
- ✅ 拆分 modal（单切点，沿用现有 split_editing_segment）
- ✅ 顶部 Tab 移到右列
- ✅ Theme token-only 检查 + 修复杂色硬编码

Phase 1 不动后端，意味着：
- 视频源始终是中文视频（`buildStreamUrl(jobId, 'video')`）
- 拆分只能拆 2 段
- 没有 word-level 智能预填

### 8.2 Phase 2（后端 + 前端解锁）

**Phase 2 前置任务（Codex 二审 #5 修正：用脚本而非 SQL）**：

> 项目里 manifest 不在 DB（`jobs` 表只有 `r2_artifacts` / `metering_snapshot` JSON 字段，不含 artifact 路径），物料事实在 `{project_dir}/manifest.json`（[manifest_reader.py:87](src/services/manifest_reader.py:87)）。所以普查脚本而非 SQL。

```python
# scripts/phase2_artifact_survey.py — 走 projects/ 目录，读每个 manifest.json
# 输出：
#   total_jobs: int
#   has_source_video: int  # source.original_video artifact 存在
#   raw_words_available: int  # transcript/raw_assemblyai.json (or 同名) 存在
#   raw_words_with_speaker_label: int  # JSON 里 words[].speaker_label != null
#   coverage_ratio = has_source_video / total_jobs
```

- 跑法：staging / 生产挑一个有代表性的子集（最近 30 天任务）
- 普及率 < 70% → 对应 Phase 2 feature 降级为 opt-in 或 hint-only
- 把 Gateway DB `jobs.r2_artifacts` JSONB key 也扫一遍交叉验证（若部分任务已搬 R2 不在本地 projects/）

Phase 2 features：

- ⏳ `GET /jobs/{id}/stream/source-video` 新增 stream kind（前置：source-video 普查 ≥ 70%，否则 segmented control 默认隐藏）
  - **必须同步改的代码点（Codex 二审 P1 #3 完整清单）**：
    1. **Gateway regex** [job_intercept.py:2574](gateway/job_intercept.py:2574)：`_STREAM_KIND_RE = re.compile(r"^stream/(?P<kind>video|audio|poster|source-video)$")`
    2. **Gateway helper** [job_intercept.py:1932](gateway/job_intercept.py:1932)：`artifact_key_for_stream_kind` + `stream_kinds_for` 都要加 `source-video`
    3. **Gateway R2 stream kind map**：搜 `stream_kinds_for` 函数本体，把 `source-video → source.original_video` 加入返回集
    4. **Job API allowlist** [api.py:538](src/services/jobs/api.py:538)：把 `("video", "audio", "poster")` 改为 `("video", "audio", "poster", "source-video")`
    5. **Job API kind 映射** [api.py:552](src/services/jobs/api.py:552)：加 `elif kind == "source-video": artifact_key = "source.original_video"; content_type = "video/mp4"`
    5a. **Job API resolver 分支**（Codex 三审 P1 #2 修正）：现有 [api.py:563](src/services/jobs/api.py:563) 只让 `poster` 走 `resolve_manifest_artifact_path`，其余 kind 走 `_resolve_download_path` —— 但后者会查 [PUBLIC_RESULT_DOWNLOAD_KEYS](src/services/web_ui/constants.py:30) 白名单，`source.original_video` 不在里面 → happy path 直接 404。**正确做法**：让 `source-video` 也走 manifest resolver 路径（和 poster 同分支），**不要**把 `source.original_video` 加进 PUBLIC_RESULT_DOWNLOAD_KEYS（原视频不是公开下载产物，加白名单会让 download 路径也暴露）。修改 `if kind == "poster":` 为 `if kind in ("poster", "source-video"):`
    6. **Express filter**：检查 `EXPRESS_ALLOWED_STREAM_KINDS` 是否要加 source-video（Express 任务通常没有 source artifact，不加 → 403 forbidden 自然降级）
    7. **回归测试**（新增 `tests/test_source_video_stream.py` 覆盖）：
       - happy: source.original_video artifact 存在 → 200 + Range
       - 404: artifact 不存在
       - 403: Express 任务 → forbidden（不在 EXPRESS_ALLOWED_STREAM_KINDS）
       - **Gateway 路由层面**: 不在 allowlist 的旧 kind (e.g. `stream/foo`) → regex 不匹配 → 不进 stream handler（Codex 二审验证：未知 kind 不能被 fallback 当 poster 处理）
- ⏳ `buildStreamUrl(jobId, 'source-video')` 前端 helper 扩展（`kind: 'video' | 'audio' | 'poster' | 'source-video'`）
- ⏳ 左侧视频 segmented control 切换 原/中文
- ⏳ `POST /jobs/{id}/segments/{sid}/split-many` 新后端接口（核心，无前置）
- ⏳ `split_editing_segment_many()` 新内核函数
- ⏳ `GET /jobs/{id}/segments/{sid}/word-context` 后端 word 上下文端点（替代前端拉 raw_*.json）
- ⏳ `detect_speaker_changes_in_segment()` 智能预填检测（前置：speaker_label 覆盖率检查；不足时降级为标点-only）
- ⏳ 拆分 modal 解锁多切点 + 智能提示条（降级文案备用）

### 8.3 Phase 1 → Phase 2 的迁移路径

- Phase 1 的 `SplitSegmentDialog` 设计成「cut count = N」泛化结构，Phase 1 在 props 里硬传 `maxCuts={1}` 限制
- Phase 2 直接 `maxCuts={undefined}` 解锁 + 启用智能预填
- 不需要重写 modal

---

## 8a. Responsive & Accessibility（Pass 6 决策）

### 8a.1 三断点

| Viewport | 行为 |
|----------|------|
| ≥ 1280px | 左右分栏 grid `[400px_1fr]`；视频区 sticky，list 自然滚 |
| 1024-1279px | 左右分栏 grid `[360px_1fr]`；视频 max-height 35vh（不让视频挤掉操作区） |
| 768-1023px | **Tablet 中间态**：上下堆叠（同移动端），但视频区上限 40vh + 段落行保持 100px 左列（不压缩说话人下拉） |
| < 768px | 上下堆叠，视频 30vh，段落行左列压缩至 70px，按钮组下移到中列下方（行变 2 行高） |

### 8a.2 a11y baseline

**键盘 nav**（必须 · Codex 二审 #6：与 §1.3 「不做快捷键系统」对齐 — 只保留 a11y baseline + HTML5 标准，不加 vim 风格 J/K）：
- Tab 焦点顺序：header buttons → 视频 controls → 当前段 ops panel → 段落列表（每行内部 Tab：英文 → 中文 → 说话人 → 拆分 → 重合成）
- Up/Down arrow：列表内段间移动（已选段聚焦视频 seek） —— ARIA listitem 标准
- Space：当前段视频 controls focus 时播放/暂停 —— HTML5 video 默认
- Enter（段聚焦时）：进入 inline 编辑（焦点落 textarea）
- Esc（modal 内）：关闭 modal（不提交）
- Esc（inline 编辑中）：取消编辑，恢复原文本（焦点退回段）

> 显式排除：J/K（vim 风格）、G/Shift+G（跳首尾）、? 帮助面板。这些是「快捷键系统」专项 PR 的范畴，本计划不引入。

**Focus management**：
- 拆分 modal 打开时焦点落第一个可交互（智能提示条 「应用建议」 / 文本第一个切点 / 「取消」按钮）—— 复用 shadcn Dialog 默认 focus trap
- modal 关闭后焦点回到「拆分」按钮（触发源）
- 段落 inline 编辑离焦保存后，焦点回到段（不跳走）

**触控目标**：
- 所有可点击元素 `min-h-[44px]` 在 mobile/tablet（< 1024px）
- 桌面 keep 现有 32px（鼠标精度高）
- 行内紧凑按钮组在 < 1024px 时变为 `flex-col` 并扩高

**对比度**：
- 普通文本 ≥ 4.5:1（WCAG AA），大文本 / 图标 ≥ 3:1
- 必检对：
  - ink theme：英文 muted-foreground (#6E6A65) on card (#EDE6D6) → 实测 4.4:1 ⚠️ 边缘，可能要调 muted-foreground 到 #5F5C58（5.0:1）
  - ink-dark theme：英文 muted-foreground (#A8A8A8) on card (#3A3A3A) → 4.9:1 ✅
  - cinnabar primary 上的白字（按钮文字）→ 4.6:1 ✅
- 实施时跑一次 axe-core 或 Chrome DevTools Lighthouse 对比度审计，超 1 处不通过就调 token（写入 globals.css 不再 ad-hoc）

**ARIA / 屏幕阅读器**（最小集）：
- `<video>` 加 `aria-label="译制视频"`（Phase 2 切换 source 时改 label）
- 段落列表用 `role="list"` + 每段 `role="listitem"`
- active 段加 `aria-current="true"`
- 状态变化的 toast 用 sonner 默认的 `role="status"` + `aria-live="polite"`
- 拆分 modal 用 shadcn Dialog 默认 `role="dialog"` + `aria-modal="true"`
- 按钮文案足够自解释，**不另加 aria-label**（避免和可见文案冗余）

**不做的事**：
- 不加 skip-link（项目其他页也没有）
- 不加 landmark 解构（`<main>` / `<nav>` / `<aside>`）超出现有 AppShell 提供的层级
- 不做 screen reader full audit（项目无明确视障用户用例 + 无 axe-core CI gate）

---

## 8a3. 代码质量决议（Eng review）

### 8a3.1 左侧 ops panel 和右侧行内按钮的 handler 共用

左侧「当前段操作区」和右侧行内的「重合成 / 拆分 / 接受草稿 / 丢弃草稿」**必须共用同一组 callback**，避免 ad-hoc 复制：

```ts
// page.tsx 内单一定义，传给两边
const handleRegenerate = useCallback(...)
const handleAcceptDraft = useCallback(...)
const handleDiscardDraft = useCallback(...)
const handleSplit = useCallback(...)

<CurrentSegmentOpsPanel onRegenerate={handleRegenerate} ... />
<SegmentRow onRegenerate={handleRegenerate} ... />
```

不允许在 `CurrentSegmentOpsPanel` 内部再定义平行实现 —— 状态会漂移。

### 8a3.2 ASCII 注释维护

新写 `split_editing_segment_many()` 时**必须**在函数顶上加配套 ASCII：

```
# split_editing_segment_many — N cuts → N+1 segments
#
# Input:  original segment with source/cn text + N cuts (source_idx, cn_idx, speaker)
# Output: N+1 segments, ids = <base>_a, <base>_b, ... (collision: <base>_aa)
#
# Atomicity (Eng review #1.1):
#   ┌─────────────┐  validate    ┌────────────┐  os.replace  ┌──────────┐
#   │ baseline    │ ──cuts──→    │ tmp files  │ ────────────→│  new     │
#   │ segments.   │  ✗ → 422     │ segments.  │   (atomic    │ baseline │
#   │ json        │  unchanged   │ json.tmp   │    rename)   │          │
#   └─────────────┘              └────────────┘              └──────────┘
#
# Time alignment: cuts must fall on word boundaries from raw_assemblyai.json
#                 (off-boundary → snap to nearest word end ms)
```

同样适用于 `CurrentSegmentOpsPanel` 顶部注释（说明它和 SegmentRow 共享 callback、与 activeSegmentId 联动）。

### 8a3.3 现有过时 ASCII 检查

新 plan 不影响现有 ASCII，但实施时需要扫一遍 `services/jobs/editing.py` 和 `editing_segments.py` 顶部的状态图注释，确认还准确（不准确即更新）。

---

## 8b. 架构决议（Eng review）

### 8b.1 SegmentVirtualList.scrollToId 加 stickyOffset prop

现状 `SegmentVirtualList.scrollToId(id, opts)` 在列表 scroll container 内部居中。但**移动端**视频 sticky top 占 30vh，居中位置可能被视频遮住。

修改：
```ts
scrollToId(id: string, opts?: {
  align?: "center" | "start"
  stickyOffset?: number  // px - 调用方告知上方被 sticky 元素占去的像素
})
```

调用方（edit 页）传入 `stickyOffset = viewport.matchMedia('(max-width: 1023px)').matches ? document.querySelector('[data-sticky-video]').offsetHeight : 0`。

桌面端 stickyOffset = 0（视频在左列，不在列表上方）。

### 8b.2 取消修改与批量合成的取消序列

`handleCancelEditing()` 改为：

```
if (batchTaskId !== null):
  confirm("正在批量合成 (k/N)，取消修改会丢弃当前进度。继续？")
  if confirmed:
    await cancelBatch(batchTaskId)
    await waitForBatchExit({ timeout: 10s })  // 监听 task cancelled 事件 / 轮询 task status
  else:
    return  // 用户改主意
await editingCancel()
router.push(`/workspace/${jobId}`)
```

理由：避免 editing/cancel 删 editor/editing/ 后批量 task 还往里写产生 orphan。`waitForBatchExit` 超时 10s 后强制继续 editing/cancel（容忍 task runner 慢一拍，最差情况是 audit 多一条 orphan 记录，不影响数据正确性）。

---

## 9. 回归守卫

新增测试（按 Eng review §3 coverage diagram 补足）：

### 9.0 后端测试（pytest）

1. **`tests/test_edit_page_state_mapping.py`** — 6 内部状态 → 5 按钮文案的映射表，每对一行 assert
2. **`tests/test_split_many_kernel.py`**（Phase 2）— `split_editing_segment_many` 单元测试：
   - happy: 2 切点 → 3 段 ✓
   - 切点越界（source_index ≥ len）→ 422，baseline 段数未变
   - 切点产生空段（cn_index = 0）→ 422，baseline 未变
   - 2 切点重合 → 422 或合并行为（按实现选）
   - segment_id 冲突 `<base>_a` 已存在 → 命名升级 `<base>_aa`
   - **★ 原子性**：mock 第 3 个 cut 在写盘阶段 raise → `assert baseline_segments == before` + `assert not tmp_files_exist`
   - **★ word boundary snap**：切点不在 word 边界 → 自动 snap 到最近 word end
   - **★ file_lock 锁定期间异常**：模拟 disk full / file_lock 抛 → baseline 未损坏
3. **`tests/test_source_video_stream.py`**（Phase 2）— `GET /stream/source-video`：
   - happy: artifact 存在 → 200 + Range-aware bytes
   - 404: artifact 不存在
   - **★ 不同任务 artifact 路径解析**：project_dir 含中文 / 含空格 → 不破
4. **`tests/test_speaker_change_detection.py`**（Phase 2）— `detect_speaker_changes_in_segment`：
   - **★** 单 speaker 段 → 返 []
   - **★** 双 speaker 段 → 返 1 个切点（在 speaker 切换 word boundary）
   - **★** speaker label 抖动（A B A A B）+ 容差 200ms → 合并相邻切点
   - **★** 上限 5 切点 — 段内有 8 个切换点时只返回 5 个最大 confidence

### 9.1 前端 contract 测试（项目历史无 UI 测试框架，仅这两条 Python 守卫）

5. **`tests/test_edit_page_redesign_guards.py`** — AST 扫前端文件 contract（**Codex 二审 #6 修正**：原"不再 import SegmentCard"vacuous，因为 SegmentCard 现状是 page.tsx 内联 function 不是 import）：
   - **页面体积阈值**：`edit/page.tsx` 重构后行数 < **1700**（基线 2127；实际从 ~1560 起步，留 ~140 行余量给后续小修补。**1200 在实际落地里不现实** —— page.tsx 还要承载 CommitModal + AudioSyncConflictModal + ~500 行 handler/state/derived。Codex 三审 #2 修正：把"1200"叙述对齐到实际守卫阈值）
   - **新组件存在**：检查 `SegmentRow.tsx` / `CurrentSegmentOpsPanel.tsx` / `SplitSegmentDialog.tsx` 三个文件都存在 + 都 export default
   - **page.tsx import SegmentRow**：AST 扫到 `import { SegmentRow } from ...` 或 default import
   - **page.tsx 不再含内联 `function SegmentCard`**：AST 检测 page.tsx 不应该再有 1300+ 行的 SegmentCard function declaration
   - **`SegmentVirtualList.scrollToId` 签名**：AST 检 ts 接口包含 `stickyOffset?: number`
   - **颜色 token 检查**：`SegmentRow.tsx` source 不出现 `#C73E3A` / `#EDE6D6` 等 hex 字面量（应该用 `var(--primary)` / `var(--card)`）

### 9.2 不写测试的部分（无框架支持）

- modal 切点交互（前端 UI 行为）
- 视频 sticky 行为
- 列表 active row 高亮（视觉）
- inline edit textarea autosize（视觉）
- theme switch 不破

### 9.3 手测 smoke checklist（实施时必跑）

- [ ] 桌面 Chrome：进修改页 → 首段 dirty 自动定位 → 改文本 → 待合成 → 接受草稿
- [ ] 桌面 Firefox：同上
- [ ] 移动端（iOS Safari 模拟）：堆叠布局 + 视频 sticky + scrollToId 不被遮
- [ ] Light / Dark 主题切换：颜色无 hard-coded 残留
- [ ] 拆分 modal（Phase 1 单切点）：切点交互 + 提交 + 关闭 → 状态正确
- [ ] 批量合成 + 中途取消 + 重新批量 → 段状态正确

---

## 10. 不动 / 已验证的运行时基础设施

| 项 | 当前位置 | 重用方式 |
|----|---------|---------|
| 进度联动 | `frontend-next/src/lib/react/usePlayerSegmentSync.ts` | 直接复用，无需修改 |
| 虚拟列表 | `frontend-next/src/components/workspace/segments/SegmentVirtualList.tsx` | 直接复用；新 SegmentRow 作为 renderItem 传入 |
| 段落 CRUD | `services/jobs/editing_segments.py` | 现有 patch / status / split 函数 |
| 草稿试听端点 | `GET /jobs/{id}/segments/{sid}/draft-audio` | 行内草稿面板的 ▶ 按钮 src |
| stream kind | `GET /jobs/{id}/stream/{video|audio|poster}` | Phase 2 加 `source-video` 第四种 |
| Tab 切换 | `useState<'text'|'voice'>` | 保留，位置调整 |
| 主题切换 | `AppShell` `data-theme` | 不动 |

---

## 11. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 左右分栏在 1024px 边界附近窄屏布局崩溃 | 测试 1024 / 1280 / 1440 / 1920 四个断点；< 1024 强制堆叠 |
| 草稿面板在虚拟列表里高度突变导致滚动跳 | SegmentVirtualList 已经有 ResizeObserver 测真实高度，无需新改 |
| Phase 2 source-video 在旧任务可能不存在（产物没保留） | 后端 stream 端点 404 时前端 segmented control 提示「该任务未保留原视频」，自动 fallback 到中文视频 |
| 智能预填检测说话人切点过敏（每句都切） | 容差 200ms 合并相邻 + 上限 5 切点 + 切点必须落在 ≥ 0.5s 的连续 speaker_label 段才认 |
| 多切点拆分后段间音频边界出现 click 噪声 | 切点必须落在 word 边界，强制对齐到 raw_assemblyai word `start`/`end` ms |
| **raw_assemblyai.json 跨 provider 覆盖**：MiMo Omni / smart-mode 多 provider 可能不写该文件或不带 speaker_label | **Phase 2 前置：开发分支做产物普查**。`_load_split_words_data` 已处理 None；新加「words 存在但无 speaker_label」中间态识别 → 智能提示条改文案「检测到 X 个标点切点（未识别说话人）」而非「检测到说话人切点」 |
| **source-video artifact 普及率**：老任务可能 < 50% 有 `source.original_video` | **Phase 2 前置**：跑 `scripts/phase2_artifact_survey.py`（walk projects/ 读 manifest.json + 交叉 `jobs.r2_artifacts` JSONB，详见 §8.2 前置任务）。若 < 70%，segmented control 改为 opt-in（默认隐藏，只在任务详情页有 artifact 时才显示）；若 ≥ 70% 才默认显示 |
| **批量合成中段标记为 dirty 还是 loading**：§2.4 「首段 dirty」自动定位时，tts_loading 段算不算 dirty | **不算**：dirty 集 = {text_dirty, voice_dirty, tts_failed}（排除 tts_loading 和 tts_dirty —— 前者后端在跑、后者用户应处理草稿）。若全部 dirty 都在 loading 中，pill 显示「N 段合成中」+ 选首段但 ops panel disabled |
| **30min+ 长视频 raw_assemblyai 30-50MB**（Phase 2）| 新增**后端 word 上下文端点** `GET /jobs/{id}/segments/{sid}/word-context` → 后端只返该段时间范围内的 words（500 KB 上限），不再前端一次拉全文件。修订 §5.4 |
| **批量合成 + 取消修改 race 后果**：waitForBatchExit 超时强制 cancel 后 task runner 写 IOError → task 标 failed | 后端 batch task **必须**捕 IOError on missing editor/editing/ → 静默 noop 并写 audit「editing_cancelled_mid_run」事件，不标 task failed。修订 §8b.2 |

---

## 12. 实施顺序（Phase 1）

1. 抽 `SegmentRow` 组件（从现有 `SegmentCard` 拆分），按 v4 改视觉
2. 抽 `CurrentSegmentOpsPanel` 组件
3. 重排 `edit/page.tsx`：grid 左右分栏 + Tab 移位
4. 抽 `SplitSegmentDialog` 组件（cut count = 1 硬限制）
5. 替换页面里旧 `SegmentCard` 引用为 `SegmentRow`
6. 跑 type check / lint / 关键路径手测
7. 状态映射回归测试
8. 桌面 + 移动两套断点截图比对（人工）

---

## 13. 已决开放问题（Pass 1 + Pass 7）

| 原 open question | 决议 | 落点 |
|------------------|------|------|
| 顶部 sticky header 按钮布局 | 「取消修改」左 · status pill 中 · 「一键合成 N 段」+「确认修改 ↗」右；移动端拆分 | §2.5 |
| 左侧 ops panel active 段 = null 显示什么 | 占位「点选段落开始修改」+ 全局 dirty/loading 统计 chip | §2.6 |
| 一键批量合成期间，单段重合成按钮怎么处理 | **disable** + tooltip「正在批量合成 (k / N)」。避免 race / draft 覆盖 | §6.4 (新增条目) |

---

## 13a. 项目级 TODO（不在本计划内但应跟进）

1. **DESIGN.md §2.2 颜色方向漂移** — DESIGN.md 写 "deep blue / steel cyan / signal teal"，实际部署是 ink theme（cinnabar 红 + 米色/炭灰）。两边对齐建议把 DESIGN.md 拉到 ink-cinnabar 主色 + bamboo / ochre / paper 副色。**不属于修改页范畴**，单开 PR 处理。
2. **a11y CI gate** — 项目尚未引入 axe-core / Lighthouse CI。本计划用静态对比度检查 + 手动 keyboard test，长期应有 CI gate 防回归。**不属于本计划范畴**。

---

## 13b. Eng review · NOT in scope（显式 deferred）

| 候选 | 不做的理由 |
|------|----------|
| 引入 Playwright / Vitest 前端测试框架 | 用户明确拒绝（单人项目 + CI 简单 + 学习成本）。Phase 1 用 AST contract + 手测 checklist 替代 |
| 重做 `EditingSpeakers` / 「+ 新增说话人」流程 | 现有实现已经够用（`listEditingSpeakers` + `EditPageSpeakerCreateDialog` 已部署），本计划只引用不重写 |
| keyboard shortcuts 系统（J/K/Space/G 等） | 留作后续专项 PR。本计划仅做基础 Tab nav |
| 音色修改 Tab 重写 | `VoiceModifyTab` 1237 行已稳定，本计划只调位置不动内容 |
| Phase 2 source-video R2 push 策略 | **明确 local-only（Codex 四审 P2 #4 修正）**：现有 [EAGER_PUSH_TO_R2_KEYS_STUDIO](src/services/r2_publisher_lib/downloadable_keys.py:73) 不含 `source.original_video`，R2 sweeper / cleanup 不会把原视频推到 R2。所以 `stream/source-video` 是 **local-only** —— 任务 R2 cleanup 之后**就不可用**，segmented control 必须自动检测 + disable + 提示「该任务已归档，原视频已清理」。如果未来要支持归档后仍可对比原视频，需要把 `source.original_video` 加入 EAGER_PUSH set + 评估 R2 出站流量成本 + TTL 策略 + Phase 3 单独立项。本计划范围内：**接受老任务 fallback 率高 + 归档任务无原视频** 的限制 |
| DESIGN.md §2.2 颜色方向更新（写 blue/cyan 但实际部署 cinnabar） | 跨页面 / 跨项目级问题，单开 PR |
| axe-core CI a11y gate | 项目历史无 a11y 工具链，单开技术债票处理 |

---

## 14. 参考

- 视觉 mockup（git-ignored）：`.superpowers/brainstorm/1921-1778983858/`
  - `theme.html` — 主题方向
  - `row-density-v4.html` — 段落行最终版
  - `split-ux.html` — 拆分 modal UX
- 现状代码：
  - 主页面 — `frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx`
  - 段卡 — 同上 `SegmentCard`
  - 音色 Tab — `frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx`
- 后端：
  - segment CRUD — `src/services/jobs/editing_segments.py`
  - split 现状 — `split_editing_segment()` in 同文件
  - stream kind 现状 — `src/services/jobs/api.py:552` `kind == "video"`
- 既有交接文档：
  - 现状 post-edit 计划 — `docs/plans/2026-04-18-studio-post-edit-plan.md`
  - γ publish-only resume — `feedback_compose_env_file_recreate.md` + `project_gamma_publish_only_resume.md`

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 5 | CLEAR | R1 (Claude): 3+5. R2 (Codex): 4+2. R3 (Codex): 3+1. R4 (Codex): 3+1. R5 (Codex): 0+2 — word-context gate 架构边界（ownership 是 Gateway 职责）/ Journal State B/C 测试补齐。**All 24 findings absorbed into plan. R5 已无 P1，进入精修阶段。** |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 3 arch issues + 3 code quality + 6 test gaps + 1 perf, all resolved |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | CLEAR | score 7/10 → 9/10, 7 decisions |

**OUTSIDE VOICE:** Claude adversarial subagent surfaced provider-coverage / artifact-availability / draft-wav-cleanup gaps → Phase 2 前置普查 + draft cleanup 模式继承都已写入 plan。
**UNRESOLVED:** 0 across all reviews.
**VERDICT:** DESIGN + ENG CLEARED — ready to implement (Phase 1 first).
