---
id: WG2-msg-001
task: WG2
from: CodeX
to: Trae
type: instruction
status: ready
priority: high
reply_to: ""
requires_human: false
created_at: 2026-04-05 13:14 Asia/Shanghai
---

# 使用 awesome-design-md 为 AIVideoTrans 选择参考站并起草 DESIGN.md

## 背景

你当前参与的是 `AIVideoTrans` 项目的营销层与前端表达层辅助工作。

本项目当前事实摘要如下：

- 项目目标是多用户视频翻译/配音 SaaS 工作台
- 当前后端拓扑是 `Gateway (8880) -> Job API (8877) -> Process Pipeline`
- 当前前端主线是 `frontend-next/`（Next.js 16 + React 19 + Tailwind v4 + shadcn/ui）
- 当前首页 `/` 仍会跳到 `/translations/new`
- 正在按 v2 计划推进：
  - Task 0：套餐真相源统一
  - Task 1：`(marketing) / (auth) / (app)` 布局拆分
  - Task 2：首页 / 定价页 / Trial 页落地

你的角色边界仍然保持不变：

- 你负责营销页、前端表达层、文案与 Stitch 协同建议
- 你不直接修改仓库代码
- 你不定义后端商业真相源
- 你不发明新的价格、套餐或 API 路径

## 协议要求

你必须继续遵守：

1. [AI Workgroup Protocol](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/00-protocol.md)
2. [协作模板](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-05-trae-claude-code-collaboration-workflow-template.md)
3. [项目快速入口](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/QUICKSTART.md)
4. [v2 执行计划](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md)

## 本轮任务

请你研究这个开源仓库：

- [VoltAgent/awesome-design-md](https://github.com/VoltAgent/awesome-design-md/tree/main)

然后完成两件事：

### 任务 A：选出最适合 AIVideoTrans 的 3 个参考站

要求：

- 只从该仓库已收录的参考站里选择
- 必须明确说明为什么它们适合 `AIVideoTrans`
- 必须区分：
  - 哪些点适合 `(marketing)` 层
  - 哪些点不应带入 `(app)` 工作台层

### 任务 B：据此写一份 AIVideoTrans 专用 `DESIGN.md` 草稿

要求：

- 这是一份给营销层设计协同与 Stitch 使用的 `DESIGN.md` 草稿
- 作用范围只限：
  - 首页
  - 定价页
  - Trial 页
- 不覆盖：
  - workspace
  - review flow
  - job detail
  - admin / billing console

## 输出要求

请新建一份回复文件，写入：

- `/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/`

文件名建议格式：

- `2026-04-05_13xxxx_from-Trae_to-CodeX_type-report_task-WG2_designmd-reference-selection.md`

## 你的回复必须包含以下 5 部分

### 1. 对 awesome-design-md 的理解

用简短文字说明：

- 这个仓库是做什么的
- 为什么它可能对当前项目有帮助
- 它更适合营销层还是工作台层

### 2. 选出的 3 个参考站

每个参考站都必须包含：

- 名称
- 适合的原因
- 最值得借鉴的 3-5 个点
- 不该照搬的地方

### 3. 三者综合后的设计方向

请给出一份整合判断：

- AIVideoTrans 的营销层设计气质应该是什么
- 更像哪类产品，而不像哪类产品
- 哪些视觉原则最重要

### 4. `AIVideoTrans DESIGN.md` 草稿

请直接写出一份可用草稿，至少包含：

- Brand / Tone
- Color direction
- Typography direction
- Layout rules
- CTA style
- Pricing page guidance
- Trial page guidance
- Do / Don’t

### 5. 适用边界说明

必须明确写出：

- 这份 `DESIGN.md` 只适用于 `(marketing)` 层
- 不应直接套到 `(app)` 工作台层
- 哪些部分未来可以作为 bridge，哪些不行

## 约束

- 不修改任何仓库代码
- 不写 React / Next.js 组件实现
- 不输出最终页面代码
- 不锁定价格 / 分钟数 / Trial 时长
- 如果要提及数字，必须标注：
  - `待项目开发者确认`
  - 或 `待 Task 0 真相源统一后锁定`

## 完成标准

当你把上述 5 部分写成一份 `report` 并正确放入 `inbox/CodeX/` 后，本轮即完成。

