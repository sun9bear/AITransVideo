# 前端、注册、支付与收费体系改造 Implementation Plan

> **Status:** superseded  
> **Last updated:** 2026-04-03  
> **Superseded-by:** `docs/archive/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md`  
> **Archived at:** 2026-04-17 legacy cleanup

> **状态说明（2026-04-03）：** 本文档已被 v2 版替代，保留为历史执行稿与范围演化记录，不建议继续作为当前主执行计划使用。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在当前 `frontend-next + gateway + jobs service` 架构上，分阶段落地营销页、手机号/微信注册登录、支付宝/微信支付、自动续费订阅、分钟计费与团队席位基础能力。

**Architecture:** 采用“营销层 `(marketing)` + 认证层 `(auth)` + 工作台层 `(app)`”的前端分层，同时将 `gateway` 的简单 `PaymentOrder` 升级为 `subscriptions + invoices + mandates + usage_ledger` 体系。支付侧只接入中国大陆自助支付：支付宝与微信支付；账号侧以手机号验证码为主，微信登录/绑定为辅，并将 Trial 发放与手机号/微信身份绑定以控制薅羊毛。

**Tech Stack:** Next.js 16, React 19, App Router, Tailwind CSS 4, FastAPI gateway, SQLAlchemy, PostgreSQL, 支付宝代扣/周期扣款, 微信支付委托代扣/自动续费, 短信验证码服务（抽象层 + fake provider）

---

## 1. 文件结构与职责划分

### 1.1 前端文件结构（目标态）

```text
frontend-next/src/app/
  layout.tsx
  globals.css

  (marketing)/
    layout.tsx
    page.tsx
    pricing/page.tsx
    trial/page.tsx
    contact-sales/page.tsx

  (auth)/
    layout.tsx
    auth/page.tsx
    auth/callback/wechat/page.tsx
    auth/bind-phone/page.tsx
    auth/bind-wechat/page.tsx
    auth/onboarding/page.tsx

  (app)/
    layout.tsx
    translations/new/page.tsx
    projects/page.tsx
    projects/[jobId]/page.tsx
    workspace/[jobId]/page.tsx
    usage/page.tsx
    settings/page.tsx
    settings/billing/page.tsx
    settings/subscription/page.tsx
    settings/payment-methods/page.tsx
    billing/success/page.tsx
    billing/result/page.tsx

frontend-next/src/components/
  marketing/*
  auth/*
  billing/*
  ui/*
  workspace/*
```

### 1.2 gateway 文件结构（目标态）

```text
gateway/
  auth.py                      # 兼容入口，逐步转发到子模块
  auth_phone.py
  auth_wechat.py
  auth_sessions.py
  sms_provider.py
  risk_control.py

  billing.py                   # 兼容入口，逐步转发到子模块
  subscriptions.py
  invoices.py
  usage_metering.py
  plan_catalog.py

  payment_providers.py         # 注册表/兼容入口
  payment_providers/
    fake.py
    alipay.py
    wechatpay.py
```

### 1.3 数据模型（目标态）

需要新增或扩展：

- `users`
- `user_identities`
- `sms_verification_codes`
- `wechat_oauth_states`
- `risk_events`
- `subscriptions`
- `subscription_mandates`
- `billing_invoices`
- `payment_attempts`
- `usage_ledger`

---

## 2. 实施原则

- 先拆营销层与工作台层，再接认证和支付
- 先做 fake/stub 验证，再接真实 provider
- 付款、签约、续费、额度发放必须拆开建模
- Trial 发放必须绑定手机号验证成功
- 不在这一轮引入国际支付
- Stitch 只负责营销页/注册转化页设计，不直接生成工作台业务代码
- 每个任务结束都必须：
  - 运行对应测试
  - 运行 `python main.py --help`
  - 保证不破坏现有 accepted baselines

---

## 3. 任务分解

### Task 0: 设计冻结与配置真相源

**Files:**
- Create: `frontend-next/src/lib/pricing/plans.ts`
- Create: `frontend-next/src/lib/pricing/trial.ts`
- Create: `frontend-next/src/lib/pricing/faq.ts`
- Modify: `docs/plans/2026-04-03-frontend-auth-billing-pricing-transformation-plan.md`
- Reference: `docs/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan.md`

- [ ] **Step 1: 固定套餐与试用配置真相源**

在 `plans.ts` / `trial.ts` 中固定：

- `Free / Plus / Pro / Team / Enterprise`
- `Plus = ¥99/月`
- `Trial = 7天 + 20 源分钟 + 无需绑卡`
- Plus / Pro / Team 的分钟额度、席位配置、CTA 文案

- [ ] **Step 2: 将官网文案中的价格与 FAQ 拆成前端常量**

将已确认的定价文案拆成：

- Hero 文案
- Plan card 文案
- Trial 文案
- FAQ 文案

- [ ] **Step 3: 明确 Stitch 设计输入**

在计划附录或单独的内部注释中固定 Stitch 设计输入：

- 首页
- 定价页
- 试用页
- 认证页

并明确不让 Stitch 直接生成工作台业务页。

- [ ] **Step 4: 验证 TypeScript 常量可被前端消费**

Run: `npm run lint`
Workdir: `frontend-next`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend-next/src/lib/pricing/plans.ts frontend-next/src/lib/pricing/trial.ts frontend-next/src/lib/pricing/faq.ts
git commit -m "feat: add pricing and trial configuration source"
```

---

### Task 1: 营销层与工作台层路由分离

**Files:**
- Modify: `frontend-next/src/app/layout.tsx`
- Create: `frontend-next/src/app/(marketing)/layout.tsx`
- Create: `frontend-next/src/app/(app)/layout.tsx`
- Create: `frontend-next/src/app/(marketing)/page.tsx`
- Modify: `frontend-next/src/app/page.tsx`
- Modify: `frontend-next/src/components/app-shell.tsx`

- [ ] **Step 1: 写最小结构 smoke 测试清单**

先定义本任务验收：

- `frontend-next` 能 `lint`
- `frontend-next` 能 `build`
- `/` 不再强制跳到 `/translations/new`
- 业务页仍能通过 `(app)` 布局显示 `AppShell`

- [ ] **Step 2: 把根布局改成 providers-only**

`src/app/layout.tsx` 仅保留：

- html/body
- 全局字体
- Toaster
- 全局 providers

不要继续直接包 `AppShell`。

- [ ] **Step 3: 新增 `(marketing)` 与 `(app)` 布局**

实现：

- `(marketing)/layout.tsx`: 轻量导航 + footer
- `(app)/layout.tsx`: 继续挂 `AppShell`

- [ ] **Step 4: 把当前首页改成营销首页**

将 `src/app/page.tsx` 改为营销首页入口，或在 `(marketing)/page.tsx` 中承载首页，并确保根路径正确渲染营销层。

- [ ] **Step 5: 跑前端静态回归**

Run: `npm run lint`
Workdir: `frontend-next`
Expected: PASS

Run: `npm run build`
Workdir: `frontend-next`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add frontend-next/src/app/layout.tsx frontend-next/src/app/page.tsx frontend-next/src/app/(marketing) frontend-next/src/app/(app) frontend-next/src/components/app-shell.tsx
git commit -m "refactor: split marketing and app layouts"
```

---

### Task 2: 首页、定价页、试用页落地（Stitch 协同）

**Files:**
- Create: `frontend-next/src/components/marketing/hero.tsx`
- Create: `frontend-next/src/components/marketing/trial-banner.tsx`
- Create: `frontend-next/src/components/marketing/pricing-grid.tsx`
- Create: `frontend-next/src/components/marketing/compare-table.tsx`
- Create: `frontend-next/src/components/marketing/faq.tsx`
- Create: `frontend-next/src/components/marketing/final-cta.tsx`
- Create: `frontend-next/src/app/(marketing)/pricing/page.tsx`
- Create: `frontend-next/src/app/(marketing)/trial/page.tsx`
- Modify: `frontend-next/src/app/(marketing)/page.tsx`
- Modify: `frontend-next/src/app/globals.css`

- [ ] **Step 1: 先把 Stitch 设计稿转成 section 结构，不直接复制代码**

为每个页面固定：

- section 顺序
- 标题/副标题
- CTA hierarchy
- 移动端折叠顺序

- [ ] **Step 2: 用现有 UI 组件重写营销组件**

优先复用：

- `ui/button.tsx`
- `ui/card.tsx`
- `ui/badge.tsx`

避免引入第二套设计系统。

- [ ] **Step 3: 实现首页与定价页**

页面至少包含：

- Hero
- Trial banner
- Pricing cards
- FAQ
- Final CTA

- [ ] **Step 4: 实现试用说明页**

说明：

- 7 天 Plus 试用
- 20 源分钟
- 不自动扣费
- 试用结束后回到 Free

- [ ] **Step 5: 跑前端回归**

Run: `npm run lint`
Workdir: `frontend-next`
Expected: PASS

Run: `npm run build`
Workdir: `frontend-next`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add frontend-next/src/components/marketing frontend-next/src/app/(marketing)
git commit -m "feat: add marketing pricing and trial pages"
```

---

### Task 3: 手机号验证码注册/登录主路径

**Files:**
- Modify: `gateway/models.py`
- Modify: `gateway/auth.py`
- Create: `gateway/auth_phone.py`
- Create: `gateway/auth_sessions.py`
- Create: `gateway/sms_provider.py`
- Create: `gateway/risk_control.py`
- Create: `tests/test_auth_phone.py`
- Create: `tests/test_risk_control.py`
- Create: `frontend-next/src/app/(auth)/layout.tsx`
- Create: `frontend-next/src/app/(auth)/auth/page.tsx`
- Create: `frontend-next/src/components/auth/phone-login-form.tsx`
- Create: `frontend-next/src/components/auth/sms-code-input.tsx`

- [ ] **Step 1: 给 gateway 新增手机号身份数据模型**

扩展或新增表：

- `user_identities`
- `sms_verification_codes`
- `risk_events`

`users` 增加缓存字段：

- `phone_number`（或迁移到 identity 表）
- `phone_verified_at`
- `current_plan_code`

- [ ] **Step 2: 写 failing tests**

至少覆盖：

- 发送验证码频率限制
- 验证码登录注册一体化
- 首次登录自动创建用户
- 已验证手机号重复试用拦截

Run: `pytest tests/test_auth_phone.py -q`
Expected: FAIL

- [ ] **Step 3: 实现 fake SMS provider + 风控层**

Sprint 约束下先做：

- fake 发送
- 内存/数据库频控
- 后续可切真实 provider

- [ ] **Step 4: 实现 `/auth` 手机号登录页**

前端页面结构：

- 手机号输入
- 验证码发送
- 验证码登录
- 可选微信登录入口

- [ ] **Step 5: 跑后端与前端回归**

Run: `pytest tests/test_auth_phone.py tests/test_risk_control.py -q`
Expected: PASS

Run: `pytest tests/test_main_cli.py tests/test_job_read_surface.py -q`
Expected: existing PASS baseline

Run: `npm run lint`
Workdir: `frontend-next`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add gateway/auth.py gateway/auth_phone.py gateway/auth_sessions.py gateway/sms_provider.py gateway/risk_control.py gateway/models.py tests/test_auth_phone.py tests/test_risk_control.py frontend-next/src/app/(auth) frontend-next/src/components/auth
git commit -m "feat: add phone-based auth and trial-safe verification flow"
```

---

### Task 4: 微信登录与绑定（辅路径）

**Files:**
- Create: `gateway/auth_wechat.py`
- Modify: `gateway/models.py`
- Create: `tests/test_auth_wechat.py`
- Create: `frontend-next/src/app/(auth)/auth/callback/wechat/page.tsx`
- Create: `frontend-next/src/app/(auth)/auth/bind-phone/page.tsx`
- Create: `frontend-next/src/app/(auth)/auth/bind-wechat/page.tsx`
- Create: `frontend-next/src/components/auth/wechat-login-card.tsx`
- Create: `frontend-next/src/components/auth/bind-phone-form.tsx`

- [ ] **Step 1: 定义微信身份数据模型**

新增 identity 类型：

- `wechat_openid`
- `wechat_unionid`
- `is_bound_to_phone`

- [ ] **Step 2: 写 failing tests**

至少覆盖：

- 微信登录用户未绑定手机时不能领取 Trial
- 同一 `unionid` 不可重复领 Trial
- 已绑定用户可直接登录

Run: `pytest tests/test_auth_wechat.py -q`
Expected: FAIL

- [ ] **Step 3: 先做 fake/stub 微信 OAuth**

实现：

- state 校验
- callback 归一化接口
- fake provider for local tests

- [ ] **Step 4: 实现前端绑定流程**

用户路径：

- 微信登录成功
- 若未绑定手机 → 跳 `/auth/bind-phone`
- 绑定完成后发放 Trial 或进入工作台

- [ ] **Step 5: 跑回归**

Run: `pytest tests/test_auth_wechat.py -q`
Expected: PASS

Run: `npm run lint`
Workdir: `frontend-next`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add gateway/auth_wechat.py gateway/models.py tests/test_auth_wechat.py frontend-next/src/app/(auth)/auth/callback/wechat frontend-next/src/app/(auth)/auth/bind-phone frontend-next/src/app/(auth)/auth/bind-wechat frontend-next/src/components/auth/wechat-login-card.tsx frontend-next/src/components/auth/bind-phone-form.tsx
git commit -m "feat: add wechat login and phone binding flow"
```

---

### Task 5: Trial 与订阅真相源建模

**Files:**
- Modify: `gateway/models.py`
- Create: `gateway/plan_catalog.py`
- Create: `gateway/subscriptions.py`
- Create: `gateway/invoices.py`
- Create: `gateway/usage_metering.py`
- Create: `tests/test_subscriptions.py`
- Create: `tests/test_usage_metering.py`
- Modify: `tests/test_billing.py`

- [ ] **Step 1: 增加订阅/账单/额度表**

新增：

- `subscriptions`
- `subscription_mandates`
- `billing_invoices`
- `payment_attempts`
- `usage_ledger`

- [ ] **Step 2: 写 failing tests**

至少覆盖：

- Trial 发放只发一次
- Trial 到期后回落 Free
- active subscription 周期推进
- usage reserve/commit/release 账本一致

Run: `pytest tests/test_subscriptions.py tests/test_usage_metering.py -q`
Expected: FAIL

- [ ] **Step 3: 实现 plan catalog 与 entitlements 读取**

从硬编码用户 `plan_code` 迁移为：

- plan catalog
- subscription truth source
- user cache fields

- [ ] **Step 4: 将 job quota 快照与 usage ledger 接口对齐**

保证：

- 预留 minutes
- 完成时提交
- 中断时释放

- [ ] **Step 5: 跑回归**

Run: `pytest tests/test_subscriptions.py tests/test_usage_metering.py tests/test_billing.py -q`
Expected: PASS

Run: `pytest tests/test_gateway_entitlements.py tests/test_gateway_quota.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add gateway/models.py gateway/plan_catalog.py gateway/subscriptions.py gateway/invoices.py gateway/usage_metering.py tests/test_subscriptions.py tests/test_usage_metering.py tests/test_billing.py
git commit -m "refactor: add subscription invoice and usage ledger models"
```

---

### Task 6: 支付宝 / 微信支付首次支付 + 自动续费签约

**Files:**
- Modify: `gateway/payment_providers.py`
- Create: `gateway/payment_providers/alipay.py`
- Create: `gateway/payment_providers/wechatpay.py`
- Modify: `gateway/billing.py`
- Create: `tests/test_alipay_provider.py`
- Create: `tests/test_wechatpay_provider.py`
- Modify: `tests/test_billing.py`

- [ ] **Step 1: 写 failing tests（fake payload + stub contract）**

至少覆盖：

- 首次创建 checkout
- 签约成功后创建 mandate
- webhook 幂等
- 自动续费回调不会重复升级
- 签名不通过不结算

Run: `pytest tests/test_alipay_provider.py tests/test_wechatpay_provider.py -q`
Expected: FAIL

- [ ] **Step 2: 实现 provider 结构**

保持：

- `fake` 用于本地/CI
- `alipay`、`wechatpay` 先按真实接口抽象
- 创建签约 + 支付 + webhook parse/verify contract

- [ ] **Step 3: billing 逻辑升级为 subscription-aware**

`billing.py` 不再只做“升级用户 plan_code”，而要：

- 创建 invoice
- 记录 mandate
- 激活/续期 subscription

- [ ] **Step 4: 跑后端支付回归**

Run: `pytest tests/test_alipay_provider.py tests/test_wechatpay_provider.py tests/test_billing.py -q`
Expected: PASS

Run: `pytest tests/test_main_cli.py tests/test_job_read_surface.py -q`
Expected: existing PASS baseline

- [ ] **Step 5: Commit**

```bash
git add gateway/payment_providers.py gateway/payment_providers/alipay.py gateway/payment_providers/wechatpay.py gateway/billing.py tests/test_alipay_provider.py tests/test_wechatpay_provider.py tests/test_billing.py
git commit -m "feat: add alipay and wechatpay subscription payment adapters"
```

---

### Task 7: 前端 Billing UI 与支付方式管理

**Files:**
- Create: `frontend-next/src/app/(app)/settings/billing/page.tsx`
- Create: `frontend-next/src/app/(app)/settings/subscription/page.tsx`
- Create: `frontend-next/src/app/(app)/settings/payment-methods/page.tsx`
- Create: `frontend-next/src/app/(app)/billing/success/page.tsx`
- Create: `frontend-next/src/app/(app)/billing/result/page.tsx`
- Create: `frontend-next/src/components/billing/subscription-card.tsx`
- Create: `frontend-next/src/components/billing/plan-selector.tsx`
- Create: `frontend-next/src/components/billing/payment-method-selector.tsx`
- Create: `frontend-next/src/components/billing/invoice-table.tsx`
- Create: `frontend-next/src/components/billing/usage-summary-card.tsx`
- Create: `frontend-next/src/components/billing/topup-card.tsx`

- [ ] **Step 1: 先接静态配置和 mock data**

页面先基于已确认 plan/trial/subscription shape 渲染，不等真实支付接通。

- [ ] **Step 2: 实现 billing 主页面**

至少展示：

- 当前套餐
- Trial 剩余时间
- 自动续费状态
- 本月额度与已用分钟
- 升级入口

- [ ] **Step 3: 实现 payment methods 页面**

至少展示：

- 已签约支付宝
- 已签约微信
- 默认续费方式
- 更换/解绑动作

- [ ] **Step 4: 实现支付结果页**

处理：

- success
- processing
- failed

- [ ] **Step 5: 跑前端回归**

Run: `npm run lint`
Workdir: `frontend-next`
Expected: PASS

Run: `npm run build`
Workdir: `frontend-next`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add frontend-next/src/app/(app)/settings/billing frontend-next/src/app/(app)/settings/subscription frontend-next/src/app/(app)/settings/payment-methods frontend-next/src/app/(app)/billing frontend-next/src/components/billing
git commit -m "feat: add subscription billing and payment method pages"
```

---

### Task 8: 用量账本接入 job 创建与项目页

**Files:**
- Modify: `gateway/job_intercept.py`
- Modify: `gateway/quota.py`
- Modify: `src/services/jobs/service.py`
- Modify: `frontend-next/src/app/(app)/usage/page.tsx`
- Modify: `frontend-next/src/app/(app)/translations/new/page.tsx`
- Create: `tests/test_usage_ledger_integration.py`

- [ ] **Step 1: 写 failing tests**

覆盖：

- 创建 job 时 reserve minutes
- 失败释放
- 成功 commit
- Trial 与 subscription 的扣减来源区分

Run: `pytest tests/test_usage_ledger_integration.py -q`
Expected: FAIL

- [ ] **Step 2: 在 gateway/job flow 接入 usage ledger**

确保：

- snapshot 与 entitlements 分离
- job snapshot 只读消费套餐快照
- usage ledger 负责资源结算

- [ ] **Step 3: 更新 usage 页面与新建翻译页**

前端显示：

- 当前剩余 minutes
- 当前来源（trial / subscription / top-up）
- 本次估算扣减

- [ ] **Step 4: 跑回归**

Run: `pytest tests/test_usage_ledger_integration.py tests/test_gateway_quota.py tests/test_gateway_create_job.py -q`
Expected: PASS

Run: `npm run lint`
Workdir: `frontend-next`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/job_intercept.py gateway/quota.py src/services/jobs/service.py frontend-next/src/app/(app)/usage/page.tsx frontend-next/src/app/(app)/translations/new/page.tsx tests/test_usage_ledger_integration.py
git commit -m "feat: connect usage ledger to job creation and usage views"
```

---

### Task 9: 团队席位与审校席位基础能力

**Files:**
- Create: `gateway/workspaces.py`
- Create: `gateway/workspace_models.py`
- Create: `tests/test_workspaces.py`
- Modify: `frontend-next/src/app/(app)/settings/page.tsx`
- Create: `frontend-next/src/app/(app)/settings/team/page.tsx`

- [ ] **Step 1: 先实现最小 workspace/member 模型**

至少支持：

- owner
- member
- reviewer

- [ ] **Step 2: 写 failing tests**

至少覆盖：

- Team 套餐可创建 workspace 成员
- reviewer 不消耗完整席位
- Free / Plus 不可添加团队成员

Run: `pytest tests/test_workspaces.py -q`
Expected: FAIL

- [ ] **Step 3: 接通前端 Team 管理页**

允许：

- 邀请成员
- 查看席位占用
- 查看 reviewer 配额

- [ ] **Step 4: 跑回归**

Run: `pytest tests/test_workspaces.py tests/test_gateway_entitlements.py -q`
Expected: PASS

Run: `npm run lint`
Workdir: `frontend-next`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/workspaces.py gateway/workspace_models.py tests/test_workspaces.py frontend-next/src/app/(app)/settings/team/page.tsx frontend-next/src/app/(app)/settings/page.tsx
git commit -m "feat: add workspace seats and reviewer roles"
```

---

## 4. 推荐执行顺序

1. `Task 0` 设计冻结与配置真相源
2. `Task 1` 营销层与工作台层路由分离
3. `Task 2` 首页 / 定价页 / 试用页
4. `Task 3` 手机号验证码注册/登录
5. `Task 4` 微信登录与绑定
6. `Task 5` Trial 与订阅真相源建模
7. `Task 6` 支付宝 / 微信支付首次支付 + 自动续费签约
8. `Task 7` Billing UI
9. `Task 8` 用量账本接入 job
10. `Task 9` 团队席位

原因：

- 先做能看见、能转化、能注册的外层
- 再做账号体系
- 再做订阅真相源与支付
- 最后再做用量和团队化

---

## 5. 阶段性里程碑

### Milestone A：可获客

完成 Task 0-2 后，系统应具备：

- 官网首页
- 定价页
- 试用页
- 清晰的 Trial 与 Plus/Pro 信息架构

### Milestone B：可注册、可反滥用

完成 Task 3-4 后，系统应具备：

- 手机号验证码登录/注册
- 微信登录/绑定
- Trial 发放防薅基本能力

### Milestone C：可收费、可续费

完成 Task 5-7 后，系统应具备：

- Trial / subscription / mandate / invoice 真相源
- 支付宝/微信首次支付
- 自动续费签约
- Billing 页面

### Milestone D：可规模化运营

完成 Task 8-9 后，系统应具备：

- 分钟账本
- 透明额度消耗
- Team / reviewer 席位能力

---

## 6. 关键验收标准

- [ ] `frontend-next` 可同时承载 marketing / auth / app 三层布局
- [ ] 新用户可通过手机号注册并获得一次性 Trial
- [ ] 微信登录用户必须绑定手机号后才能领 Trial
- [ ] `Plus / Pro` 可通过支付宝 / 微信完成首次支付与自动续费签约
- [ ] 订阅真相源不再依赖单一 `users.plan_code`
- [ ] 用量台账能支持 reserve / commit / release
- [ ] `usage` 页面可展示 Trial / subscription / top-up 来源
- [ ] Team 版有 reviewer 席位基础能力
- [ ] `python main.py --help` 仍可运行
- [ ] 既有 accepted baselines 不被破坏

---

## 7. 非目标（这一轮不做）

- 国际支付（Stripe / PayPal）
- 企业对公在线支付
- 复杂财税发票系统
- API 商业化计费
- 私有化部署计费
- lip-sync 独立计费
- 完整 CRM / 销售漏斗系统
