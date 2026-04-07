# 前端、注册、支付与收费体系改造 v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在当前 `frontend-next + gateway + jobs service` 架构上，用更小的阶段把“能看、能注册、能收费”主链路先跑通，同时避免一次性打穿现有 `plan_code + free quota + PaymentOrder` 基线。

**Architecture:** 本版保留 [2026-04-03-frontend-auth-billing-pricing-transformation-plan.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-03-frontend-auth-billing-pricing-transformation-plan.md) 作为北极星蓝图，但执行上收缩为 5 个阶段：先统一套餐真相源并拆前端布局，再落营销页和手机号登录，然后建立最小订阅真相源与单渠道付费闭环，最后才考虑微信、自动续费、精细账本和团队席位。现有 `email + password_hash + Session cookie` 与 `PaymentOrder + PaymentWebhookEvent` 不直接废弃，而是作为兼容层渐进迁移。

**Tech Stack:** Next.js 16, React 19, App Router, Tailwind CSS 4, FastAPI gateway, SQLAlchemy, PostgreSQL, Alembic, 支付宝（优先）、微信登录/微信支付（后置）、短信验证码、滑块验证码/图形验证码。

---

## 0. 为什么需要 v2

当前仓库的商业化与前端基线仍然是：

- 前端首页直接跳 `/translations/new`，没有营销层首页或定价页；
- 根布局统一包 `AppShell`，不适合营销页与工作台并存；
- 认证仍是 `email + password`；
- 支付模型仍是 `PaymentOrder + PaymentWebhookEvent`；
- 计费仍主要围绕 `user.plan_code` 与 free quota，而不是完整订阅与分钟账本。

代码证据：

- [layout.tsx](/D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/layout.tsx)
- [page.tsx](/D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/page.tsx)
- [auth.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth.py)
- [models.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/models.py)
- [billing.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/billing.py)
- [job_intercept.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/job_intercept.py)

因此，v1 执行稿里把营销层、手机号认证、微信登录、自动续费、完整 usage ledger、Team/reviewer 席位一次性串在一起，风险过高。v2 的核心目标不是否定原蓝图，而是把执行顺序收缩到更符合当前仓库状态的形状。

---

## 1. v2 的总原则

- `gateway` 是套餐、价格、权益、试用规则的真相源；前端只消费，不单独定义最终业务事实。
- `Session` 机制复用；认证方式渐进替换，不做“大爆炸式”切换。
- 先做营销层与注册转化，再做最小收费闭环，再做自动续费和精细账本。
- 支付先跑通一个可收费路径，再扩第二支付渠道。
- `Team / reviewer seats / shared pool` 不进入本轮主线。
- 所有新能力都必须保持：
  - `python main.py --help` 可运行
  - 现有 accepted baselines 不新增失败
  - 测试中不接真实外部 API

### 1.1 非代码前置条件

以下事项不属于代码实现本身，但应尽早启动，否则会阻塞后续支付阶段：

- 支付宝商户资质申请已启动，至少能支撑首个自助支付闭环
- 微信支付商户资质申请已启动
- 微信自动续费 / 委托代扣是否可申请，已经有明确结论
- 短信服务供应商、模板审核与发送资质有可执行方案

> 其中支付宝资质最直接影响当前主线的“首个可收费版本”；微信支付与自动续费可后置，但不应等到 Task 5 之后才开始确认。

---

## 2. v2 相对 v1 的关键调整

### 2.1 保留

- `(marketing) / (auth) / (app)` 三层前端分层
- 手机号短信验证为主路径
- 微信只作为辅登录 / 绑定路径
- `Trial -> Free -> Plus / Pro` 的转化漏斗
- 最终要升级到 `subscriptions + invoices + mandates + usage_ledger`
- Stitch 只用于营销页 / 转化页设计，不直接生成工作台业务代码

### 2.2 延后

- 微信登录（从主线移到后置阶段）
- 微信支付自动续费
- 完整 `usage_ledger` 与 source minutes 计费接入 job 主链路
- Team / reviewer 席位
- Enterprise 采购能力

### 2.3 新增必须补的内容

- 套餐真相源统一策略
- `PaymentOrder` 渐进迁移路径
- 认证存量兼容策略
- API contract 文档化
- Alembic migration 顺序
- 支付 / 订阅告警
- 试用风控中的滑块验证码与虚拟号段策略
- `manual_refund` 与 top-up 扣减优先级

---

## 3. 最终推荐执行顺序

### Milestone A：能看

1. 套餐真相源统一
2. `(marketing) / (auth) / (app)` 布局拆分
3. 首页 / 定价页 / 试用页落地

### Milestone B：能注册

4. 手机号验证码登录 / 注册
5. Trial 发放与反滥用

### Milestone C：能收费

6. 最小订阅真相源建模
7. 单渠道首次支付闭环
8. Billing UI 基础版

### Milestone D：再扩渠道

9. 微信登录 / 绑定
10. 微信支付与自动续费签约（有资质再做）

### Milestone E：再做精细运营

11. `usage_ledger` 接入 job 与增量重生成计费
12. `manual_refund`、top-up 优先级
13. Team / reviewer 席位

> v2 只把 Milestone A-C 作为当前主线计划。Milestone D-E 只保留为后续队列。

---

## 4. 任务拆解（当前主线）

### Task 0: 套餐、试用与 API contract 真相源统一

**目的：** 先消除“前端一份价格、gateway 一份价格、job 拦截一份权益逻辑”的漂移。

**Files:**
- Create: `gateway/plan_catalog.py`
- Create: `tests/test_plan_catalog.py`
- Create: `docs/specs/2026-04-04-pricing-and-plans-api-contract.md`
- Create: `frontend-next/src/lib/billing/types.ts`
- Create: `frontend-next/src/lib/billing/get-plans.ts`
- Modify: `gateway/billing.py`
- Modify: `gateway/job_intercept.py`
- Modify: `docs/plans/2026-04-03-frontend-auth-billing-pricing-transformation-plan.md`

- [ ] **Step 1: 把套餐与试用规则提炼为 gateway 真相源**

在 `gateway/plan_catalog.py` 中集中定义：

- `free / plus / pro`
- 展示价、账单周期、是否支持自助支付
- trial 规则（7 天、20 源分钟、是否需要手机号）
- 当前阶段先不把 `team / enterprise` 作为可执行权益模型

- [ ] **Step 2: 为前端提供只读套餐接口**

新增：

- `GET /api/plans`

该接口必须明确为**公开接口**：

- 未登录用户可访问
- 不依赖 `require_auth`
- 可直接被营销页、定价页、试用页消费

返回最少字段：

```json
{
  "plans": [
    {
      "code": "plus",
      "display_name": "Plus",
      "price_cny_monthly": 9900,
      "features": ["..."],
      "self_serve": true
    }
  ],
  "trial": {
    "days": 7,
    "source_minutes": 20,
    "phone_required": true
  }
}
```

- [ ] **Step 2.5: 写轻量 API Contract 文档**

Create: `docs/specs/2026-04-04-pricing-and-plans-api-contract.md`

该文档至少定义：

- `GET /api/plans`
- 是否需要认证：否
- 响应字段
- 每个字段是“展示用途”还是“业务判断用途”
- `trial` 配置结构

目的不是替代测试，而是给前后端和后续执行 agent 一个稳定的对齐基准。

- [ ] **Step 3: 前端改为消费 `/api/plans`，本地常量仅作类型层**

`frontend-next/src/lib/billing/types.ts` 只保留：

- TypeScript 类型
- fallback mock shape

不要再把价格真相源放在 `plans.ts`。

- [ ] **Step 4: 对齐现有 `billing.py` 与 `job_intercept.py`**

让：

- `billing.py` 的价格来源改为 `plan_catalog`
- `job_intercept.py` 的 plan gate / trial gate 来源改为 `plan_catalog`

但此阶段仍保留当前 `plan_code` 与 quota 机制，不强行上 `usage_ledger`。

- [ ] **Step 5: 写测试**

至少覆盖：

- `test_plan_catalog.py`
- `test_billing.py` 中价格读取来源未回退为硬编码
- `test_gateway_create_job.py` 中计划快照仍可用

- [ ] **Step 6: 验证**

Run: `pytest tests/test_plan_catalog.py tests/test_billing.py tests/test_gateway_create_job.py -q`  
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add gateway/plan_catalog.py gateway/billing.py gateway/job_intercept.py tests/test_plan_catalog.py docs/specs/2026-04-04-pricing-and-plans-api-contract.md frontend-next/src/lib/billing/types.ts frontend-next/src/lib/billing/get-plans.ts
git commit -m "refactor: centralize plan and trial catalog in gateway"
```

---

### Task 1: 营销层 / 认证层 / 工作台层布局拆分

**目的：** 先把前端路由结构调整正确，再接 Stitch 和营销页。

> **执行顺序说明：** 这是整个前端改造的**第一个前端 PR**。在它完成前，不建议开始首页、定价页、试用页的视觉落地或 Stitch 稿件接入。

**Files:**
- Modify: `frontend-next/src/app/layout.tsx`
- Create: `frontend-next/src/app/(marketing)/layout.tsx`
- Create: `frontend-next/src/app/(auth)/layout.tsx`
- Create: `frontend-next/src/app/(app)/layout.tsx`
- Modify: `frontend-next/src/app/page.tsx`
- Modify: `frontend-next/src/components/app-shell.tsx`
- Create: `frontend-next/src/components/providers/session-provider.tsx`

- [ ] **Step 1: 先清点现有业务路由引用**

执行只读搜索，列出：

- `/translations/new`
- `/projects`
- `/workspace`
- `/usage`
- `/settings`
- `/admin`
- `/auth/login`
- `/auth/register`

同时检查两类容易遗漏的位置：

- 前端内部 `router.push`、导航 `href`
- 后端是否存在前端路由硬编码（例如 redirect URL、登录后跳转 URL）

并在本任务里明确一个兼容决策：

- 是保留旧 `/auth/login`、`/auth/register` 并做 redirect
- 还是统一切到新的 `/auth` 入口并废弃旧页面

避免拆 route groups 后遗失 `router.push`、导航链接或后端重定向目标。

- [ ] **Step 2: 把根布局改为 providers-only**

`src/app/layout.tsx` 只保留：

- html / body
- Toaster
- Theme/Session providers

不要继续直接包 `AppShell`。

- [ ] **Step 3: 新增共享登录态 Provider**

创建 `session-provider.tsx`，负责：

- 读取 `/auth/me` 或等价 session endpoint
- 向 `(marketing)` 与 `(app)` 暴露 `user / session / plan` 的基础状态

这一层是 Gemini 提醒里“marketing 与 app 共享登录态”的落地点。

SSR / hydrate 期间的默认行为必须明确：

- 服务端首屏默认按“未登录”渲染 CTA
- 默认显示 `免费开始试用`
- 客户端 hydrate 后再切换到“进入试用 / 进入工作台”

这样可以避免营销页首屏出现明显闪烁或 hydration 不一致。

- [ ] **Step 4: 新增 `(marketing)`、`(auth)`、`(app)` 三层布局**

- `(marketing)`：官网导航 + CTA
- `(auth)`：极简认证壳
- `(app)`：继续承载 `AppShell`

- [ ] **Step 5: 让 `/` 不再重定向到 `/translations/new`**

首页应改为营销页入口。

- [ ] **Step 6: 验证**

Run: `npm run lint`  
Workdir: `frontend-next`  
Expected: PASS

Run: `npm run build`  
Workdir: `frontend-next`  
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add frontend-next/src/app/layout.tsx frontend-next/src/app/page.tsx frontend-next/src/app/(marketing) frontend-next/src/app/(auth) frontend-next/src/app/(app) frontend-next/src/components/app-shell.tsx frontend-next/src/components/providers/session-provider.tsx
git commit -m "refactor: split frontend into marketing auth and app layouts"
```

---

### Task 2: 首页 / 定价页 / 试用页（Stitch 协同）

**目的：** 尽快形成“可对外展示、可转化”的前台，而不碰工作台复杂业务页。

**Files:**
- Create: `frontend-next/src/components/marketing/hero.tsx`
- Create: `frontend-next/src/components/marketing/trial-banner.tsx`
- Create: `frontend-next/src/components/marketing/pricing-grid.tsx`
- Create: `frontend-next/src/components/marketing/faq.tsx`
- Create: `frontend-next/src/components/marketing/final-cta.tsx`
- Create: `frontend-next/src/app/(marketing)/pricing/page.tsx`
- Create: `frontend-next/src/app/(marketing)/trial/page.tsx`
- Modify: `frontend-next/src/app/(marketing)/page.tsx`
- Modify: `frontend-next/src/app/globals.css`

- [ ] **Step 1: 先出 Stitch 视觉稿，不直接落生成代码**

只让 Stitch 负责：

- 首页
- 定价页
- 试用页

输出只用作：

- section 顺序
- 视觉方向
- token mapping

不把 Stitch 输出直接当最终 React 代码。

- [ ] **Step 2: 用现有 UI 组件重写 marketing 组件**

优先复用：

- `ui/button.tsx`
- `ui/card.tsx`
- `ui/badge.tsx`

避免出现第二套设计系统。

- [ ] **Step 3: 定价页必须突出三件事**

- `7 天 Plus Trial`
- `无需绑卡`
- `增量重生成按增量计费`

其中第三点是 Gemini 提到的核心卖点，应在定价页显著放大。

- [ ] **Step 4: 落地 CTA**

- 未登录：`免费开始试用`
- 已登录未订阅：`进入试用`
- 已登录已订阅：`进入工作台`

这依赖 Task 1 的共享 session provider。

- [ ] **Step 5: 验证**

Run: `npm run lint`  
Workdir: `frontend-next`  
Expected: PASS

Run: `npm run build`  
Workdir: `frontend-next`  
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add frontend-next/src/app/(marketing) frontend-next/src/components/marketing frontend-next/src/app/globals.css
git commit -m "feat: add marketing home pricing and trial pages"
```

---

### Task 3: 手机号登录 / Trial 发放 / 基础风控

**目的：** 先建立国内可用的主认证路径，但不在这一阶段强制废弃邮箱密码。

**Files:**
- Modify: `gateway/models.py`
- Create: `gateway/auth_phone.py`
- Create: `gateway/risk_control.py`
- Create: `gateway/sms_provider.py`
- Create: `tests/test_auth_phone.py`
- Create: `tests/test_trial_grant_rules.py`
- Create: `frontend-next/src/app/(auth)/auth/page.tsx`
- Create: `frontend-next/src/components/auth/phone-login-form.tsx`
- Create: `frontend-next/src/components/auth/captcha-gate.tsx`

- [ ] **Step 1: 设计最小数据迁移**

在 `users` 上新增可空字段：

- `phone_number`
- `phone_verified_at`
- `trial_granted_at`
- `trial_ends_at`

如需更规范身份模型，可新增轻量 `user_identities`，但不要一开始就做过度抽象。

- [ ] **Step 1.5: 编写 Alembic migration**

Create: `gateway/alembic/versions/007_add_phone_and_trial_fields.py`

该 migration 至少包含：

- `users.phone_number`
- `users.phone_verified_at`
- `users.trial_granted_at`
- `users.trial_ends_at`

所有新字段都应对旧用户兼容，可空或具备安全默认值。

- [ ] **Step 2: 保留现有 email/password 登录兼容**

当前没有证据证明可以直接删除旧认证，因此：

- `auth.py` 保留现有 `RegisterRequest` / `LoginRequest`
- 新增 `auth_phone.py` 提供手机号验证码路径
- `Session` 表与 cookie 复用

- [ ] **Step 3: 引入发送短信前的人机验证**

吸收 Gemini 建议，在 `send_code` 之前增加滑块/图形验证码闸门。开发期可以用 fake captcha provider，但接口边界要先立住。

- [ ] **Step 4: 风控最小集**

`risk_control.py` 至少支持：

- 单手机号限频
- 单 IP 限频
- 虚拟号段拦截 hook
- Trial 一次性发放校验

- [ ] **Step 5: Trial 发放规则**

只在以下条件满足时发放：

- 手机号验证成功
- 手机号未领过 Trial
- 风控未命中阻断

微信用户即便未来支持登录，也必须补绑手机才能领 Trial。

- [ ] **Step 6: 写测试**

至少覆盖：

- 正常手机号登录
- 重复手机号 Trial 拦截
- 发送短信限频
- 未通过 captcha 不发短信

- [ ] **Step 7: 验证**

Run: `pytest tests/test_auth_phone.py tests/test_trial_grant_rules.py -q`  
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add gateway/models.py gateway/auth_phone.py gateway/risk_control.py gateway/sms_provider.py tests/test_auth_phone.py tests/test_trial_grant_rules.py frontend-next/src/app/(auth)/auth/page.tsx frontend-next/src/components/auth/phone-login-form.tsx frontend-next/src/components/auth/captcha-gate.tsx
git commit -m "feat: add phone auth trial grants and basic risk control"
```

---

### Task 4: 最小订阅真相源与 `PaymentOrder` 渐进迁移

**目的：** 不直接推翻 `PaymentOrder`，而是在它之上建立更稳定的订阅语义。

**Files:**
- Modify: `gateway/models.py`
- Create: `gateway/subscriptions.py`
- Create: `tests/test_subscriptions.py`
- Create: `gateway/alembic/versions/007_add_subscriptions_minimal.py`
- Modify: `gateway/billing.py`
- Modify: `tests/test_billing.py`

- [ ] **Step 1: 建最小订阅模型，不一次性上全量账本**

当前阶段新增：

- `subscriptions`
- `billing_invoices`

先不强制落：

- `usage_ledger` 全量接入
- `subscription_mandates`

但表设计上要预留后续扩展空间。

Trial 与 subscription 的边界在本阶段必须明确：

- Trial 仍由 `users.trial_granted_at / trial_ends_at` 管理
- `subscriptions` 只在**首次付费成功后**创建
- 当前有效计划判定顺序建议为：
  1. 若 `trial_ends_at > now`，则视为 `trialing`
  2. 否则若存在 `subscription.status = active`，则视为付费订阅
  3. 否则为 `free`

这样可以避免在 Trial 阶段同时维护两套状态来源。

- [ ] **Step 2: 明确 `PaymentOrder` 迁移角色**

v2 推荐：

- `PaymentOrder`：继续承担“单次 checkout / webhook 幂等”
- `subscriptions`：承担当前有效套餐状态
- `billing_invoices`：承担账单历史

即：不是立刻废弃 `PaymentOrder`，而是让它退化为支付层兼容壳。

- [ ] **Step 3: 建立状态流**

首次付费成功后：

1. webhook 更新 `PaymentOrder`
2. 写入 / 更新 `billing_invoices`
3. 创建或更新 `subscriptions`
4. 最后才更新用户可见权益

- [ ] **Step 4: 定义基础 API**

至少提供：

- `GET /api/me/subscription`
- `GET /api/billing/history`

- [ ] **Step 5: Alembic migration**

新增 008 migration，并明确：

- 所有新字段对旧用户兼容
- 旧数据无 data migration 也能运行

- [ ] **Step 6: 写测试**

至少覆盖：

- 首次付费后订阅写入
- 重复 webhook 不重复升级
- `PaymentOrder` 与 `subscriptions` 状态一致性

- [ ] **Step 7: 验证**

Run: `alembic upgrade head`  
Workdir: `gateway`  
Expected: PASS

Run: `pytest tests/test_subscriptions.py tests/test_billing.py -q`  
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add gateway/models.py gateway/subscriptions.py gateway/billing.py gateway/alembic/versions/008_add_subscriptions_minimal.py tests/test_subscriptions.py tests/test_billing.py
git commit -m "feat: add minimal subscription source of truth"
```

---

### Task 5: 单渠道首次支付闭环（优先支付宝）

**目的：** 先回答“有没有人愿意付钱”，而不是先把双渠道自动续费做满。

**Files:**
- Modify: `gateway/payment_providers.py`
- Create: `gateway/payment_providers/alipay.py`
- Create: `tests/test_alipay_provider.py`
- Modify: `gateway/billing.py`
- Modify: `frontend-next/src/app/(app)/settings/subscription/page.tsx`
- Create: `frontend-next/src/components/billing/checkout-card.tsx`

- [ ] **Step 1: 明确本阶段支付边界**

本阶段只做：

- 支付宝首次付费
- Plus / Pro 首次升级

不做：

- 微信支付
- 自动续费
- mandate 生命周期

- [ ] **Step 2: 保持 fake provider 测试路径**

真实接入前：

- fake provider 继续覆盖主测试流
- 支付宝 adapter 先做 contract test + stubbed integration

符合仓库“测试不接真实外部 API”的约束。

- [ ] **Step 3: 前端先做 checkout 最小页**

`subscription/page.tsx` 与 `checkout-card.tsx` 只要能完成：

- 当前套餐显示
- 选择 Plus / Pro
- 发起支付
- 查看支付结果

- [ ] **Step 4: 支付回调后的权益更新**

必须保持当前商业化规则：

- 只更新订阅 / 账单 / 用户权益
- 不修改运行中 job snapshot

- [ ] **Step 4.5: 补最小支付告警**

这一阶段不要等到 Milestone C 结束后再补告警。

至少在本任务内补齐：

- webhook 处理异常的 structured log
- webhook 验签失败的 warning / error log
- 重复回调与未匹配订单的明确日志事件

最小目标是：支付回调出问题时，维护者能在日志中第一时间发现，而不是静默失败。

- [ ] **Step 5: 写测试**

至少覆盖：

- checkout request validation
- fake provider success path
- alipay adapter contract
- webhook 幂等

- [ ] **Step 6: 验证**

Run: `pytest tests/test_alipay_provider.py tests/test_billing.py -q`  
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add gateway/payment_providers.py gateway/payment_providers/alipay.py gateway/billing.py tests/test_alipay_provider.py frontend-next/src/app/(app)/settings/subscription/page.tsx frontend-next/src/components/billing/checkout-card.tsx
git commit -m "feat: add first paid checkout path with alipay"
```

---

### Task 6: Billing UI 基础版

**目的：** 让用户能看见当前套餐、试用状态、支付历史，而不是先上复杂支付方式管理。

**Files:**
- Create: `frontend-next/src/app/(app)/settings/billing/page.tsx`
- Create: `frontend-next/src/components/billing/subscription-summary.tsx`
- Create: `frontend-next/src/components/billing/order-history.tsx`
- Create: `frontend-next/src/lib/billing/get-subscription.ts`
- Create: `frontend-next/src/lib/billing/get-order-history.ts`

- [ ] **Step 1: 先做“读”能力，不做“写”能力**

基础 Billing 页显示：

- 当前 plan
- Trial 剩余时间
- 下次续费（若有）
- 支付历史
- 升级入口

- [ ] **Step 2: 页面 CTA 与状态对齐**

- trialing：显示倒计时与升级按钮
- active：显示当前套餐与历史账单
- none / free：显示升级入口

- [ ] **Step 3: 验证**

Run: `npm run lint`  
Workdir: `frontend-next`  
Expected: PASS

Run: `npm run build`  
Workdir: `frontend-next`  
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add frontend-next/src/app/(app)/settings/billing/page.tsx frontend-next/src/components/billing frontend-next/src/lib/billing/get-subscription.ts frontend-next/src/lib/billing/get-order-history.ts
git commit -m "feat: add billing overview ui"
```

---

## 5. 后置阶段（不进入当前主线）

### Deferred A: 微信登录与绑定

进入条件：

- 手机号路径已上线并稳定
- Trial 发放无明显滥用
- 微信开放平台资质与回调域名准备完成

### Deferred B: 微信支付与自动续费

进入条件：

- 支付宝首次付费闭环已验证
- 微信商户号、自动续费/委托代扣权限确认可用
- 支付告警与人工补单流程就绪

### Deferred C: `usage_ledger`、manual refund、top-up 优先级

进入条件：

- 至少一轮真实付费用户验证完成
- 需要开始精细核算 AI 成本

此阶段必须加入：

- `manual_refund`
- `top_up_purchase`
- 先扣当期会过期额度、再扣长期有效 top-up 的规则

### Deferred D: Team / reviewer seats

明确移出当前主线。只有在以下条件满足后再做：

- 单用户付费漏斗已验证
- 至少有真实团队协作需求
- workspace / member / reviewer 权限模型被单独设计并审过

---

## 6. 监控与运维最小要求

这部分不单独拆新阶段，但最小 webhook/支付告警必须在 **Task 5** 内一并落下；其他更完整的告警可在 Milestone C 结束前补齐。

至少需要：

- webhook 失败告警
- 支付重复回调告警
- Trial 滥用峰值告警
- 短信发送异常告警

最小实现可以是：

- structured logs
- 管理员邮件 / 企业微信 / 微信服务号提醒

---

## 7. 成本与定价校准要求

在把 `Plus = ¥99 / 40 源分钟` 真正落到 gateway 真相源前，必须完成一次成本实测。

至少抽样：

- 10 分钟 express
- 10 分钟 studio
- 30 分钟 express
- 30 分钟 studio

记录：

- ASR 成本
- 翻译成本
- TTS 成本
- 存储与带宽
- 平均毛利率

若 `¥99 / 40 分钟` 的毛利率明显偏低，则应优先调整价格或分钟额度，而不是硬把 marketing 文案先发出去。

---

## 8. Milestone 验收口径

### 完成 Task 0-2 后

系统应具备：

- 官网首页
- 定价页
- 试用页
- marketing/auth/app 三层布局
- gateway 真相源提供 plans/trial 展示数据

### 完成 Task 3 后

系统应具备：

- 手机号验证码登录
- Trial 一次性发放
- 风控最小集
- 旧 email/password 会话兼容

### 完成 Task 4-6 后

系统应具备：

- 最小订阅真相源
- 单渠道首次付费
- 基础 Billing UI

这时才算真正达到“可收费 MVP”。

---

## 9. 不在本版直接执行的事项

以下内容保留在蓝图中，但不进入当前 v2 主线：

- 微信登录与绑定
- 微信支付自动续费
- 全量 `usage_ledger`
- Team / reviewer seats
- Enterprise 合同采购
- 复杂 SMS provider 抽象层

其中 SMS 实现建议保持轻量：

- `send_sms(phone, code)` 样式的简单适配层即可
- 通过 `SMS_PROVIDER=fake|real` 控制环境行为

不需要一开始就做复杂 provider registry。

---

## 10. 最终建议

当前仓库最适合的执行顺序是：

1. **先做真相源统一**
2. **再做前端布局拆分和营销页**
3. **再做手机号认证与 Trial**
4. **再做最小订阅与单渠道收费**
5. **最后才考虑微信、自动续费、精细账本与团队席位**

> 这版 v2 的核心不是“把原蓝图砍掉”，而是“让原蓝图按当前仓库能承受的节奏落地”。
