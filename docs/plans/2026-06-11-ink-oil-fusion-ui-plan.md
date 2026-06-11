# 水墨×油画 UI 融合方案（Ink × Oil Fusion）v3

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在已落地的水墨主题（ink / ink-dark）之上补齐"西方油画"维度，让中西双文化成为产品叙事：源文=朱砂、译文=群青的双色语义，墨→彩的流水线进度隐喻，成片页的画廊装裱。

**Architecture:** 纯前端表现层改造。所有颜色经 `globals.css` 的 ink / ink-dark 两个 `data-theme` scope 注入新 token（`--ultramarine` 等），组件只消费 `var(--*)`，不出现裸色值。分三个优先级（P0 token+语义 / P1 质感 / P2 字体），按依赖顺序交付（见"回滚顺序"）。

**Tech Stack:** Tailwind v4 `@theme inline` + CSS custom properties（oklch）、shadcn/ui、Next.js 16 App Router。

**版本记录:** v1 2026-06-11 初稿 → 同日三轮外部评审（独立子代理 + Codex GPT-5.5 xhigh ×2）→ v2 修正 7 P1 + 5 P2 → v3 修正复审新发现 2 P1 + 2 P2（Task 6 faux-bold、Task 2 豁免收窄、specificity、token fallback）。

---

## 0. 背景与现状（评审者/实施者必读）

- 水墨主题已于 2026-04-29 落地（`docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md`）：
  - `[data-theme="ink"]`（`frontend-next/src/app/globals.css:118`）：宣纸米底 + 墨黑 + **朱砂为唯一暖色/CTA**。
  - `[data-theme="ink-dark"]`（`globals.css:187`）：chroma=0 炭灰 + 宣纸噪点 `::before` overlay。
  - ink-dark 选择器带 `:not(.__never__)` specificity hack（压过 legacy `.dark`）；新增同优先级规则必须沿用此模式。
- **⚠️ scope 真相（Codex 核验，v2 修正）：`data-theme="ink"` 不是营销层专属**——营销 layout 挂在自己的 `<div>`（`(marketing)/layout.tsx:25`），auth layout 也挂（`(auth)/layout.tsx:35`），AppShell 浅色模式也用（`app-shell.tsx:344`）；`.ink-display`/`.ink-heading` 被 auth/app/admin 页面复用。**任何"只想影响营销层"的改动不得直接写进 `[data-theme="ink"]` scope**，必须用营销专用选择器。
- root `<html>` 默认带 `.dark`（`app/layout.tsx:66`）且 root layout 已全站 `<link>` 加载 Noto Serif SC 600/900（`layout.tsx:71`）。portal 到 body 的浮层不继承营销 div 的 ink token。
- `StageProgress` 唯一消费方是工作区详情页 `(app)/workspace/[jobId]/page.tsx:242`（列表页不用）。
- `ResultMediaCard` 无 job status prop，可播判定只有 `hasVideo = availability?.dubbed_video`（`ResultMediaCard.tsx:136`）。
- `TranslationReviewPanel` 是带 textarea/拆分/试听/提交的**核心审校编辑面**，不是展示卡片。

### 设计红线（任何 Task 不得违反）

1. **朱砂仍是唯一 CTA 色**。群青只做"译文/目标语言"身份标识（非交互装饰：边线、标签底、文字色），不上按钮、不做大面积背景。
2. **不堆中国风符号**：无祥云/灯笼/龙纹/毛笔字。
3. **数据密集面与交互件不碰**：表格、表单控件、focus/hover/badge/button 样式、admin 全部保持现状。Task 2 对审校面的改动**仅限非交互装饰性左边线**（显式豁免，见 Task 2）。
4. **对比度 fail-closed，按真实落点测**：不只测 base background——`bg-card`、`bg-muted/30`、soft 底 badge 等每个实际落点都要 ≥4.5:1（正文）/ ≥3:1（大字号图形）。达不到调 L 值，不得豁免。
5. 动效仅 transform/opacity，150–300ms，尊重 `prefers-reduced-motion`。

### 回滚顺序（v2 新增，评审 P2）

依赖链：Task 2 依赖 Task 1（token）；Task 5 依赖 Task 4（装裱容器）。**回滚必须逆序**：先回滚消费方（2/5），再回滚被依赖方（1/4）。Task 3/6 独立。

---

## P0 — 颜料体系与双色语义

### Task 1: 群青 + 装裱 token 注入双主题

**Files:**
- Modify: `frontend-next/src/app/globals.css`（ink block extras 区 ~L163-177、ink-dark block extras 区 ~L253-267）

- [ ] **Step 1: ink（宣纸底）scope 追加**

```css
  /* Ultramarine — Western pigment counterpart to cinnabar.
     Semantic: target-language / translated-text identity. NOT a CTA color. */
  --ultramarine: oklch(0.40 0.13 265);          /* 深群青，落宣纸底，L=0.40 留余量 */
  --ultramarine-soft: oklch(0.40 0.13 265 / 0.08);
  --gallery-vignette: rgba(0, 0, 0, 0.18);      /* Task 4 装裱内阴影，token 化 */
```

- [ ] **Step 2: ink-dark（炭灰底）scope 追加**

```css
  --ultramarine: oklch(0.70 0.11 265);          /* 提亮群青，落炭灰底 */
  --ultramarine-soft: oklch(0.70 0.11 265 / 0.12);
  --gallery-vignette: rgba(0, 0, 0, 0.30);      /* 深色底加深 */
```

- [ ] **Step 3: 对比度验证——按真实落点矩阵测，不许眼估**

用 oklch 对比计算器（如 oklch.com / APCA）实测下列组合并把比值写入 commit message：
ink 底：ultramarine on `--background` / `--card` / `--muted`；ink-dark 底：ultramarine on `--background`(0.22) / `--card`(0.27) / `--muted`(0.32)。任一组 <4.5:1 → 调 L（ink 往 0.38、ink-dark 往 0.72 方向），不动 C/H。

- [ ] **Step 4: Commit**（message 标注：群青 H=265 与 legacy `.dark` H=252 的隔离边界依赖 ink scope 覆盖，portal 浮层场景见 Task 2 约束）

```bash
git commit -m "feat(ui): ink/ink-dark 注入 --ultramarine/--gallery-vignette token" -- frontend-next/src/app/globals.css
```

### Task 2: 翻译审校双色语义（源文=朱砂 / 译文=群青）

**Files:**
- Modify: `frontend-next/src/components/workspace/TranslationReviewPanel.tsx`（~539 行；定位源文/译文文本块渲染处）

**红线豁免声明（v3 收窄，复审 P1）：** 本面板是核心审校编辑面，按红线 3 默认不可碰。本 Task 获得**最窄豁免**：只允许给源文展示块与译文编辑区的**外层容器**加非交互装饰性左边线（3px）。**文字色一律不动**——译文是可编辑 `textarea`（~L393），改其文字色即触碰输入控件语义。textarea 本体、focus/hover、badge、button、拆分/试听/提交控件一律不动。任何超出此范围的改动视为违规。

- [ ] **Step 1:** 源文文本块 `borderLeft: "3px solid var(--cinnabar)"`；译文块同法用 `var(--ultramarine)`。**portal 浮层（Dialog/Popover 内的预览等）不做双色标识**——portal 到 body 后不继承 ink token，会漏到 `.dark` 钢蓝 token，宁可不标。
- [ ] **Step 2:** 仅当面板内已有语言方向标签时同步映射颜色（源=`--cinnabar-soft` 底、译=`--ultramarine-soft` 底）；没有则不新增元素（YAGNI）。
- [ ] **Step 3:** `npm run lint && npm run build`；ink-dark 下截图确认两栏一眼可辨、文字对比度达标（按 Task 1 Step 3 矩阵）。
- [ ] **Step 4: Commit**（显式 pathspec）

### Task 3: 墨→彩流水线进度（connector-only）

**Files:**
- Modify: `frontend-next/src/components/stage-progress.tsx`（`connectorStyle` ~L49）

**设计（两轮评审裁决合并）:** dot 全部保留现有 state 语义（complete=bamboo、current=cinnabar ring、error 不变、upcoming=muted）——dot 是状态锚点，统一色保证可扫读性。墨→彩渐进**只做 connector**。

- [ ] **Step 1:** `connectorStyle` 增加 connector 序号入参。**⚠️ 按 connector 总数算比例，不是 stage 总数（评审 P1 off-by-one）**：N 个 stage 只有 `N-1` 根 connector，第 `i` 根（0-based）的比例为
  `ratio = connectorCount > 1 ? i / (connectorCount - 1) : 1`，complete 态背景
  `color-mix(in oklab, var(--cinnabar) ${Math.round(20 + 70 * ratio)}%, var(--ink-gray-2))`。
  这样末根 connector 必达 90% 满彩；仅 2 个 stage（1 根 connector）时直接满彩。
- [ ] **Step 2:** dot 样式零改动；截图确认 complete dot 仍为 bamboo、与渐进 connector 协调。
- [ ] **Step 3:** lint + build + 截图。**消费方只有工作区详情页 `(app)/workspace/[jobId]`（v2 修正：列表页不消费本组件，无需截）。**
- [ ] **Step 4: Commit**

---

## P1 — 质感与装裱

### Task 4: 成片卡片画廊装裱

**Files:**
- Modify: `frontend-next/src/components/workspace/ResultMediaCard.tsx`（播放器容器，`hasVideo` 分支 ~L136 起）

**触发条件措辞修正（v2，评审 P1）：** 组件无 job status prop，不引入新 prop——装裱条件就是现有 `hasVideo`（配音视频可播即视为成品态，语义上即"过程素面、成品装裱"）。

- [ ] **Step 1:** `hasVideo` 为真时，播放器外层容器加装裱：细双线框（`border` + `outline`，色 `var(--border)` 与 `color-mix(in oklab, var(--cinnabar) 25%, transparent)`）+ vignette `box-shadow: inset 0 0 48px var(--gallery-vignette, rgba(0, 0, 0, 0.18))`（token 见 Task 1；**带 fallback 值兜底**——脱离 ink scope 的场景不丢阴影，复审 P2）。
- [ ] **Step 2:** **实测点（评审裁决）：** 若装裱容器或其父层有 `overflow: hidden`，`outline` 会被裁剪——届时改双 `box-shadow`（`0 0 0 1px` ×2）实现双线。vignette 的 inset box-shadow 不参与 z-index、不挡 pointer-events，与播放控件无冲突。
- [ ] **Step 3:** Express/Free/Studio 三模式 + 处理中/可播两状态截图回归。
- [ ] **Step 4: Commit**

### Task 5: 亚麻画布纹理（西侧面）

**Files:**
- Modify: `frontend-next/src/app/globals.css`（新增 `.canvas-texture` utility）
- Modify: Task 4 装裱容器 + 营销层成果展示组件——**实施时先确认挂载目标是 `frontend-next/src/components/marketing/featured-demos-client.tsx` 还是 `product-proof.tsx`（评审 P2：两者都像"成果展示"，跟项目主确认后只挂一处）**

- [ ] **Step 1:** 参照宣纸噪点的实现模式（data-URI 内联 SVG、`mix-blend-mode: overlay`、opacity ≤0.05），用横平纹 weave pattern 做 **`.canvas-texture::after`**（必须用 `::after`：避免与 ink-dark 根级 `::before` 噪点及组件自身 `::before` 冲突——评审要求）。
- [ ] **Step 2:** 只挂 Task 4 装裱容器 + 上述确认的营销组件；工作台其余面不挂。
- [ ] **Step 3:** 双主题下验证不抬饱和度、不伤文字对比度。
- [ ] **Step 4: Commit**

---

## P2 — 字体对仗（v2 按 Codex P1×3 重写）

### Task 6: EB Garamond × 既有 Noto Serif SC 标题对（营销专用 scope）

**Files:**
- Modify: `frontend-next/src/app/(marketing)/layout.tsx`（外层 div ~L25）
- Modify: `frontend-next/src/app/globals.css`（`.ink-display` / `.ink-heading` ~L538 一带）

**v2 关键修正（Codex 核验推翻 v1 假设）：**
1. **Noto Serif SC 不需要重复引入**——root layout 已全站 `<link>` 加载（600/900，`layout.tsx:71`），`.ink-display`（weight 900）/`.ink-heading`（weight 600）已硬编码宋体栈。v1 计划用 next/font 再装 500/700 是重复加载 + 字重对不上（会 faux-bold）。本 Task 只新增 **EB Garamond（600 + 700，匹配既有字重档）**。
2. **不得写进 `[data-theme="ink"]` scope**——该 scope 被 auth/AppShell 浅色复用（见 §0），直接改会泄漏到非营销层。必须用**营销专用 scope**。
3. CSS 变量只需在使用点祖先链上，不强求"同一个 div"；约束是：营销 layout 不能改 `<html>`，所以变量挂营销外层 div（header/footer/main 都在其内）。

- [ ] **Step 1:** `next/font/google` 引入 `EB_Garamond`（**仅 weight 600**，`latin` subset），`.variable` class 挂到 `(marketing)/layout.tsx` 外层 div，并给该 div 加 `marketing-root` class（与 `data-theme="ink"` 同一节点，满足祖先链要求）。
- [ ] **Step 2:** globals.css 新增**营销专用**规则。**v3 双修正（复审 P1+P2）：** ① Garamond **只套 `.ink-heading`（600）**——`.ink-display` 是 weight 900，EB Garamond 最高 800，套上必 faux-bold，故 display 保持 Noto Serif SC 900 不动（中文主导的 hero 标题本来就该宋体压阵，东西对仗在 heading 层完成）；② 选择器用 `.marketing-root[data-theme="ink"]`（specificity (0,3,0)）稳压既有 `[data-theme="ink"] .ink-heading` 类规则（(0,2,0)），不依赖源码顺序：

```css
  /* Marketing-scoped East-West heading pair: Garamond for Latin headings,
     Noto Serif SC fallback for CJK. .ink-display (weight 900) is deliberately
     NOT included — EB Garamond caps at 800 and would faux-bold (review v3 P1).
     (0,3,0) specificity beats the shared [data-theme="ink"] rules regardless
     of source order (review v3 P2). */
  .marketing-root[data-theme="ink"] .ink-heading {
    font-family: var(--font-eb-garamond), "Noto Serif SC", serif;
  }
```
- [ ] **Step 3:** 验收：auth / app / admin 页面标题字体**零变化**（回归截图）；营销页西文标题为 Garamond。Lighthouse 查 CLS：>0.1 则 `font-display` 改 optional 或预加载 critical weight。
- [ ] **Step 4:** 双端（375/1440）+ 双主题截图评审后 Commit。

---

## 验证与回滚

- 每 Task 独立 commit（显式 pathspec，禁 `git add .`）；回滚按"回滚顺序"一节逆序执行。
- 全局回归：`cd frontend-next && npm run lint && npm run build`；浏览器走查 375/768/1440 × ink/ink-dark。
- 无后端改动、无 feature flag。

## 评审记录

**第一轮（2026-06-11，独立子代理 fresh context）：Approved**。裁决：Q1 connector-only（采纳）、Q2 不混淆不扩 scope、Q3 vignette 安全 + overflow/outline 实测点；必修项：字体变量挂载、除零护卫。

**第三轮（2026-06-11，Codex GPT-5.5 xhigh 复审 v2）：GATE FAIL（新 2 P1 + 2 P2）→ 已修入 v3。** 12 条旧账判定 10 FIXED / 1 PARTIAL / 1 NOT_FIXED。新发现与修正：① Task 6 faux-bold 残留——EB Garamond 最高 800 对不上 `.ink-display` 900 → Garamond 只套 `.ink-heading`，display 保持宋体 900；② Task 2 豁免不够窄——译文是可编辑 textarea → 收窄为仅外层容器左边线、文字色一律不动；③ `.marketing-root .ink-heading` 与既有规则 specificity 同级会静默失效 → 改 `.marketing-root[data-theme="ink"]` (0,3,0)；④ `--gallery-vignette` 无兜底 → 消费点加 fallback 值。

**第二轮（2026-06-11，Codex GPT-5.5 xhigh，实读代码核验 v1）：GATE FAIL（7 P1 / 5 P2）→ 已全部修入 v2。** 要点：
- 推翻第一轮两个结论：① `data-theme="ink"` 非营销专属（auth/AppShell 复用），Task 6 改为营销专用 scope；② "ink 体系下与 `.dark` 不共存"不准确——root html 恒带 `.dark`，portal 到 body 的浮层不继承 div 级 ink token（→ Task 2 禁止对 portal 浮层做双色标识）。
- 修正：Task 3 connector off-by-one（按 connectorCount 算比例）；Task 4 触发条件改 `hasVideo`（组件无 status prop）+ vignette token 化；Task 2 红线冲突显式窄豁免；Task 1 对比度按真实落点矩阵测；Task 6 字重/重复加载问题重写；Task 5 挂载目标待确认（featured-demos vs product-proof）；回滚顺序补依赖链；StageProgress 验收范围修正（仅详情页）。
