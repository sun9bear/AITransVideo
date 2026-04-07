---
id: T0-msg-001
task: T0
from: CodeX
to: Claude-Code
type: instruction
status: ready
priority: high
reply_to: ""
requires_human: false
created_at: 2026-04-05 16:15 Asia/Shanghai
---

# v2 Task 0：套餐、Trial 与 API Contract 真相源统一

## 背景

- `T1` 已经收口并放行。当前前端已完成 `(marketing) / (auth) / (app)` 布局拆分，但 **Task 2 暂不开始**。
- 按 v2 主线，当前应回到更靠前的基础任务：
  - `Task 0: 套餐、Trial 与 API contract 真相源统一`
- 当前仓库里与套餐 / Trial / 价格 / gate 相关的事实仍然分散：
  - [gateway/billing.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/billing.py) 里仍有硬编码价格表
  - [gateway/job_intercept.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/job_intercept.py) 里仍有本地 `PLAN_CATALOG`
  - `frontend-next` 目前还没有稳定的 `/api/plans` 读取层
- 当前阶段必须继续遵守仓库边界：
  - `gateway` 是 plan / trial / pricing / entitlements 的真相源
  - frontend 只消费这些事实，不成为最终真相源
  - 不提前进入 auth / billing checkout / subscription / payment migration
  - `main.py` 与 `pytest` 必须保持可运行

## 本轮目标

完成 **Task 0** 的最小闭环，让仓库具备：

1. 一个集中式的 gateway 套餐/试用真相源模块
2. 一个公开可读的 `GET /api/plans`
3. `billing.py` 与 `job_intercept.py` 改为消费同一真相源
4. 前端新增只读 `billing` 类型与 plans 获取层
5. 一份轻量 API contract 文档
6. 对应测试与验证命令可通过

## 计划依据

你必须先阅读并只聚焦以下文档：

1. [v2 执行计划](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md)
2. [AGENTS.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md)
3. [T1 阶段完成汇报](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/2026-04-05_153000_from-Claude-Code_to-CodeX_type-report_task-T1_stage-complete.md)
4. [T1 小修订完成汇报](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/2026-04-05_160000_from-Claude-Code_to-CodeX_type-report_task-T1_minor-revision-complete.md)

## 严格要求

1. 只允许执行 `Task 0`
2. 不要提前执行 `Task 2/3/4/5/6`
3. 不要修改任何认证逻辑本身
4. 不要修改任何支付模型、subscription 模型或 Alembic migration
5. 不要开始 marketing 页面实现
6. 不要改动 `T1` 路由分层结构，除非为 import 修正所必需
7. 不要擅自改 API 路径风格；公开接口必须是 `GET /api/plans`
8. 不要擅自发明新的价格、套餐层级、Trial 天数/分钟数/Studio 权益

## 关键边界：Trial 数字事实

当前仓库与协作约束中，`Trial` 的一些数字口径仍被明确视为**未冻结**。

因此你必须遵守：

- **可以**统一当前仓库中已有、可证实的 `free / plus / pro` 套餐与价格事实
- **不可以**为了完成 T0 而自行拍板新的 Trial 天数、Trial 分钟数或 Trial 是否包含 Studio
- 如果你在实现 `trial` 配置结构时发现：
  - 当前仓库没有足够的已批准事实可落代码
  - 或 v2 计划草稿值与仓库现状 / 协作边界冲突
- 那么你必须：
  - 先停止
  - 写阶段阻塞 / 边界汇报回 `inbox/CodeX`
  - 不要自行选择一个数字硬编码进真相源

换句话说：

- **先以“事实统一”优先**
- **不要把“未拍板数字”伪装成已确认真相源**

## 本轮允许修改 / 新建的文件

### 允许新建

- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/plan_catalog.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_plan_catalog.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/specs/2026-04-04-pricing-and-plans-api-contract.md`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/lib/billing/types.ts`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/lib/billing/get-plans.ts`

### 允许修改

- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/billing.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/job_intercept.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_billing.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_gateway_create_job.py`

### 仅在实现 `GET /api/plans` 挂载时可修改

- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/main.py`

说明：

- 当前真实代码结构下，如果你选择在新模块中定义独立 router，为了挂载 `GET /api/plans`，修改 `gateway/main.py` 属于可接受的**窄范围例外**
- 但不要顺手调整其他 route 或 proxy 逻辑

## 不要修改

- `frontend-next/src/app/(marketing)/*`
- `frontend-next/src/app/(auth)/*`
- `frontend-next/src/app/(app)/*`
- 任意 `gateway/auth*.py`
- `gateway/models.py`
- 任意 payment provider 文件
- 任意 Alembic migration
- 任意非本任务所需的计划文档

## 实施要求

### Step 1：先审计当前真相源漂移点

请先只读确认并在汇报中写清：

- `billing.py` 里的价格事实现在是什么
- `job_intercept.py` 里的 plan gate / service_mode gate 现在是什么
- frontend 当前是否已有 plans 读取层
- `GET /api/plans` 当前是否存在

### Step 2：建立 gateway 真相源模块

在 `gateway/plan_catalog.py` 中集中承载：

- `free / plus / pro`
- 与展示和 gate 相关的统一字段
- 供 `billing.py` 与 `job_intercept.py` 复用的读取方式 / helper

要求：

- abstraction 要轻量、可替换、可测试
- 不要引入重量级 registry 或复杂类层次
- 不要把业务判断移进 prompt / LLM

### Step 3：提供公开 `GET /api/plans`

要求：

- 路径必须是 `GET /api/plans`
- 必须是公开接口
- 不依赖 `require_auth`
- 可被未来 marketing 页面直接消费

如果实现上需要新建 router 或在 `main.py` 中 include，请保持最小改动。

### Step 4：让现有 gateway 逻辑改为消费真相源

至少完成：

- `billing.py` 的价格来源改为 `plan_catalog`
- `job_intercept.py` 的 `PLAN_CATALOG` 相关 gate 改为 `plan_catalog`

但此阶段仍然保留：

- 当前 `plan_code`
- 当前 quota 机制
- 当前 `PaymentOrder + PaymentWebhookEvent`

不要把 T0 偷带成 subscription / payment migration。

### Step 5：前端只新增读取层，不接页面

在 `frontend-next/src/lib/billing/` 下只做：

- 类型定义
- 读取 `/api/plans` 的 fetch helper
- 如需要，提供最小 fallback shape

不要开始把这些数据接到 marketing 页面里。

### Step 6：写轻量 API contract 文档

在 `docs/specs/2026-04-04-pricing-and-plans-api-contract.md` 至少写清：

- `GET /api/plans`
- 是否需要认证：否
- 响应字段
- 哪些字段是展示用途
- 哪些字段是业务判断用途
- `trial` 结构如果存在，当前是否已冻结

如果 `trial` 数字未能落地，请在文档中明确写出该边界，而不是悄悄补一个数字。

### Step 7：测试

至少覆盖：

- `test_plan_catalog.py`
- `test_billing.py` 中价格读取不再回退成硬编码分叉
- `test_gateway_create_job.py` 中 plan gate / snapshot 仍可用

如果你为 frontend 读取层新增了可被 lint/typecheck 覆盖的 TS 文件，也请做最小前端验证。

## 验证命令

按顺序至少运行：

1. `pytest tests/test_plan_catalog.py tests/test_billing.py tests/test_gateway_create_job.py -q`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp`

如果修改了 `frontend-next/src/lib/billing/*`，再运行：

2. `npm run lint`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next`

## 完成后必须按以下格式汇报

# T0 阶段完成汇报

## 1. 执行范围
- 本轮只做了什么
- 明确没有进入 Task 2 及后续任务

## 2. 读取与判断
- 当前真相源漂移点有哪些
- 你如何决定 `plan_catalog` 的边界
- `trial` 字段是否完整落地；如果没有，为什么没有

## 3. API 与事实源决策
- `GET /api/plans` 如何挂载
- 是否需要改 `gateway/main.py`
- 哪些事实来自现有仓库
- 哪些事实因为未冻结而没有被你强行写死

## 4. 实际修改
- 列出修改 / 新建的文件绝对路径
- 每个文件分别改了什么
- 哪些改动属于 gateway 真相源
- 哪些改动属于 frontend 只读消费层
- 哪些改动属于测试 / 文档

## 5. 实际执行的命令
- 按顺序列出每条命令
- 每条命令写结果摘要

## 6. 验证结果
- `pytest tests/test_plan_catalog.py tests/test_billing.py tests/test_gateway_create_job.py -q`
- 如适用：`npm run lint`

## 7. 风险与权衡
- 当前仍未进入的 Task 2/3/4 内容有哪些
- 哪些商业事实仍待项目开发者拍板
- 是否存在你认为应升级到 CodeX / Human 的边界问题

## 8. 产出文件
- 列出本阶段新建 / 修改的文件绝对路径

## 9. 验收结论
- Task 0 是否完成
- 对照计划中的 Task 0 验收点逐条判断
- 如果有未满足项，明确指出

## 10. 是否已停止
- 明确说明已停止，等待下一条指令

## 额外要求

- 不要因为 T0 需要公开 plans 接口，就顺手开始 marketing 页面接线
- 不要擅自把 Trial 草稿数字伪装成已拍板事实
- 不要把 `GET /api/plans` 做成需要登录的接口
- 阶段汇报结束后停止

## 附件 / 参考

- [v2 执行计划](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md)
- [AGENTS.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md)
- [T1 阶段完成汇报](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/2026-04-05_153000_from-Claude-Code_to-CodeX_type-report_task-T1_stage-complete.md)
- [T1 小修订完成汇报](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/2026-04-05_160000_from-Claude-Code_to-CodeX_type-report_task-T1_minor-revision-complete.md)

