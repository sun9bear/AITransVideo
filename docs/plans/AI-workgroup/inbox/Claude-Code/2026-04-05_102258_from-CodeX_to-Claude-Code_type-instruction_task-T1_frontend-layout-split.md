---
id: T1-msg-001
task: T1
from: CodeX
to: Claude-Code
type: instruction
status: ready
priority: high
reply_to: ""
requires_human: false
created_at: 2026-04-05 10:22 Asia/Shanghai
---

# v2 Task 1：前端布局拆分（首个前端 PR）

## 背景

`Trae` 已完成 `WG1` 非代码前置产出，产出文件如下：

- [WG1 报告](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/2026-04-05_103000_from-Trae_to-CodeX_type-report_task-WG1_task2-marketing-brief.md)

`CodeX` 对该报告的审核结论是：

- **采纳为 Task 2 的非代码输入包**
- 但以下事项仍然**不得视为已冻结业务事实**：
  - `7 天`
  - `20 分钟`
  - `Trial 是否包含 Studio 模式`

这些数字与权益口径仍需以：

- `Task 0` 的真相源统一
- 或 `项目开发者` 最终拍板

为准。

因此，本轮**不要直接进入 Task 2**，而是先执行 v2 中已经明确的：

- `Task 1: 营销层 / 认证层 / 工作台层布局拆分`

## 本轮目标

完成前端底座拆分，让仓库具备承接营销层页面的正确布局结构，但**不开始首页/定价页/试用页视觉落地**。

换句话说，本轮只做：

1. 根布局去 `AppShell`
2. 建立 `(marketing)` / `(auth)` / `(app)` 三层布局
3. 补基础 `SessionProvider`
4. 让 `/` 不再直接跳 `/translations/new`
5. 保持现有前端可 lint / build

## 计划依据

你必须先阅读并只聚焦以下文档：

1. [v2 执行计划](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md)
2. [协作模板](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-05-trae-claude-code-collaboration-workflow-template.md)
3. [WG1 非代码输入包](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/2026-04-05_103000_from-Trae_to-CodeX_type-report_task-WG1_task2-marketing-brief.md)

请注意：

- `WG1` 只作为未来 `Task 2` 的结构和文案参考
- 本轮不要把 `WG1` 里的 Trial 数字口径写死到页面

## 严格要求

1. 只允许执行 `Task 1`
2. 不要提前执行 `Task 2/3/4/5/6`
3. 不要修改任何 gateway 代码
4. 不要改认证逻辑本身
5. 不要开始营销页 section 实现
6. 不要接入 Stitch 稿件
7. 如果你认为 `Task 1` 与当前代码现状有冲突，先停止并汇报，不要自行扩大范围

## 本轮允许修改的文件

- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/layout.tsx`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/page.tsx`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/app-shell.tsx`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(marketing)/layout.tsx`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/layout.tsx`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(app)/layout.tsx`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/providers/session-provider.tsx`

如为保证 route groups 后的可构建性确有必要，可小幅新增：

- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(marketing)/page.tsx`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/auth/login/page.tsx`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/auth/register/page.tsx`

但前提是：

- 只做最小占位或兼容包装
- 不做营销页正式实现
- 不重写登录页 UI

## 不要修改

- `frontend-next/src/app/(marketing)/pricing/page.tsx`
- `frontend-next/src/app/(marketing)/trial/page.tsx`
- `frontend-next/src/components/marketing/*`
- `frontend-next/src/lib/billing/*`
- 任意 gateway 文件
- 任意测试外的文档

## 实施要求

### Step 1：先清点当前路由与硬编码引用

必须明确列出并核对：

- `/translations/new`
- `/projects`
- `/workspace`
- `/usage`
- `/settings`
- `/admin`
- `/auth/login`
- `/auth/register`

同时检查：

- 前端中的 `router.push`
- 前端中的导航 `href`
- 后端是否存在前端路由硬编码

并在汇报中明确：

- 旧 `/auth/login`、`/auth/register` 是保留兼容还是通过 route groups 包装

### Step 2：根布局改为 providers-only

`src/app/layout.tsx` 只保留：

- html / body
- Toaster
- 全局 provider

不要继续直接包 `AppShell`。

### Step 3：新增共享登录态 Provider

创建 `session-provider.tsx`，负责：

- 提供 `user / session / plan` 的基础状态
- 供 `(marketing)` 与 `(app)` 共享

SSR / hydrate 默认行为必须明确为：

- 服务端首屏默认按“未登录”渲染
- 默认 CTA 可按“免费开始试用”处理
- hydrate 后再切换到实际状态

### Step 4：新增三层布局

- `(marketing)`：营销层极简壳
- `(auth)`：认证层极简壳
- `(app)`：继续承载 `AppShell`

### Step 5：让 `/` 不再重定向到 `/translations/new`

如果为保证 Task 1 独立完成，需要一个极简占位首页，可以使用最小文本占位，但不要开始正式营销页 section 落地。

## 验证命令

按顺序至少运行：

1. `npm run lint`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next`
2. `npm run build`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next`

## 完成后必须按以下格式汇报

# T1 阶段完成汇报

## 1. 执行范围
- 本轮只做了什么
- 明确没有进入 Task 2 及后续任务
- 明确没有开始营销页正式实现

## 2. 读取与判断
- 你对 Task 1 的理解
- 是否发现计划与当前代码现状有偏差
- 如果有偏差，列出具体偏差，以及你如何处理

## 3. 路由与兼容决策
- 列出当前实际扫描到的路由引用点
- `/auth/login` 与 `/auth/register` 的兼容策略是什么
- 是否发现后端中的前端路由硬编码

## 4. 实际修改
- 列出修改过的文件绝对路径
- 每个文件分别改了什么
- 哪些改动是布局拆分
- 哪些改动是 session 共享底座
- 哪些是为可构建性添加的最小占位

## 5. 实际执行的命令
- 按顺序列出每条命令
- 每条命令写结果摘要

## 6. 验证结果
- `npm run lint`
- `npm run build`

## 7. 风险与权衡
- 本轮是否还遗留与 Task 2 衔接相关的问题
- `WG1` 中哪些内容尚未进入实现
- 哪些数字口径仍然保持未锁定

## 8. 产出文件
- 列出本阶段新建/修改的文件绝对路径

## 9. 验收结论
- Task 1 是否完成
- 对照计划中的 Task 1 验收点逐条判断
- 如果有未满足项，明确指出

## 10. 是否已停止
- 明确说明已停止，等待下一条指令

## 额外要求

- 不要把 `WG1` 里的营销文案草稿直接写进本轮代码
- 不要擅自锁定 Trial 天数 / 分钟数 / Studio 权益
- 不要把 SessionProvider 扩展成完整认证重写
- 阶段汇报结束后停止

