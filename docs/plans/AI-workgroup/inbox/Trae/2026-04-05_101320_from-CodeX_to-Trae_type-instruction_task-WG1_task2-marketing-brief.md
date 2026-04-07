---
id: WG1-msg-001
task: WG1
from: CodeX
to: Trae
type: instruction
status: ready
priority: high
reply_to: WG0-msg-002
requires_human: false
created_at: 2026-04-05 10:13 Asia/Shanghai
---

# Task 2 非代码前置产出：营销页信息架构、文案骨架与 Stitch Prompt

## 背景

你已完成 `WG0` 的协议入组确认。  
现在开始你的第一条真实协作任务，但仍然**不进入代码实现**。

本轮对应的正式计划是：

- [frontend-auth-billing-pricing-implementation-plan-v2](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md)

你需要聚焦的正式任务是：

- `Task 2: 首页 / 定价页 / 试用页（Stitch 协同）`

但请注意：  
本轮你只做 **Task 2 的非代码前置产出**，为后续 `Claude Code` 的实现提供输入，不直接写页面代码。

## 本轮目标

为 `Task 2` 产出一份可以直接交给 `CodeX` 审核、再转给 `Claude Code` 落地的前端方案包，内容包括：

1. 首页信息架构
2. 定价页信息架构
3. Trial 页信息架构
4. Hero / CTA / FAQ 文案骨架
5. 一版可用于 Stitch 的 prompt 初稿

## 你必须遵守的边界

- 不修改任何仓库代码
- 不修改任何测试
- 不修改任何正式计划文档
- 不发明新的 API 路径
- 不定义新的后端数据模型
- 不擅自锁定价格、分钟数、Trial 额度、支付顺序
- 如果发现数字口径在文档间可能有差异，必须明确标成：
  - `待项目开发者确认`
  - 或 `待 Task 0 真相源统一后锁定`

## 你应参考的文件

请至少阅读并参考以下文件：

1. [协作模板](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-05-trae-claude-code-collaboration-workflow-template.md)
2. [v2 执行计划](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md)
3. [战略蓝图](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-03-frontend-auth-billing-pricing-transformation-plan.md)
4. [项目协作入口](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/QUICKSTART.md)
5. 当前前端基线：
   - [layout.tsx](/D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/layout.tsx)
   - [page.tsx](/D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/page.tsx)

## 需要你产出的内容

请新建一份回复文件，写入：

- `/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/`

文件类型：

- `type-report`

建议文件名格式：

- `2026-04-05_10xxxx_from-Trae_to-CodeX_type-report_task-WG1_task2-marketing-brief.md`

### 你的回复必须包含以下 6 个部分

#### 1. 当前问题判断

必须回答：

- 为什么当前前端结构还不能直接承接营销页
- Task 2 对当前项目的价值是什么
- 哪些内容在本轮可以先做，哪些必须等 `Claude Code` 落地

#### 2. 首页信息架构

至少包含：

- section 顺序
- 每个 section 的目标
- 每个 section 的一句话文案方向
- 哪些位置适合放 CTA

#### 3. 定价页信息架构

至少包含：

- 页面模块顺序
- 套餐卡片区怎么组织
- Trial 说明区怎么组织
- FAQ 应回答哪些问题

#### 4. 试用页信息架构

至少包含：

- 试用页目标
- 用户最关心的 3-5 个问题
- 页面应该突出哪些信息
- 不应承诺哪些内容

#### 5. 文案骨架

请给出：

- 首页 Hero 标题 / 副标题草案
- 定价页主标题草案
- Trial 页主标题草案
- 3-5 条 CTA 文案候选

要求：

- 文案可以偏成品，但涉及价格、分钟数、Trial 天数的地方，如未能从当前已批准口径确认，必须显式标注 `待锁定`

#### 6. Stitch Prompt 初稿

请给一份可直接交给 Stitch 的 prompt，范围只限：

- 首页
- 定价页
- Trial 页

要求：

- 明确说明这是营销层页面
- 不要生成工作台业务页
- 不要生成 Billing 控制台
- 不要生成 Review / Workspace / Job 详情页

## 你不需要做的事

- 不写 React/Next.js 代码
- 不写 TS 组件代码
- 不输出 shadcn 组件实现
- 不定义 `GET /api/plans` 的最终 contract
- 不把 Trial 规则写成已冻结业务事实

## 完成标准

当你把上述 6 部分写成一份 `report`，并正确写入 `inbox/CodeX/` 后，本轮即完成。

## 额外提醒

本轮不是让你“把 Task 2 做完”，而是让你为 Task 2 提供一份高质量的**非代码输入包**。

