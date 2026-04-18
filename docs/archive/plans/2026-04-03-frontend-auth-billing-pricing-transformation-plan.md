# 前端 + 注册 + 支付 + 收费体系改造方案

> **Status:** completed (implemented)  
> **Last updated:** 2026-04-03  
> **Role:** 战略蓝图 / 目标态设计（执行由 `implementation-plan-v2.md` 落地）  
> **Implemented-by:** V2 + V3 商业化批次  
> **Archived at:** 2026-04-17 legacy cleanup

> **文档角色说明（2026-04-03）：** 本文档是前端、注册、支付与收费体系改造的**战略蓝图 / 目标态设计**，用于说明"为什么这样改、最终想走到哪里"。当前更贴近仓库现状的执行顺序，请以 `docs/archive/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md` 为主。

> **定位：** 这是一份面向当前仓库的详细改造方案，覆盖 `frontend-next`、`gateway`、注册登录、国内支付、自动续费、试用与收费模型，以及 Stitch 协同设计方式。  
> **目标：** 在不破坏现有工作台主流程的前提下，把项目从“可用的内部/早期工作台”升级为“可对外获客、可转化、可续费、可控反滥用”的 SaaS 产品。

**适用时间：** 2026-04-03  
**适用代码基线：** 当前 `frontend-next + gateway + jobs service` 架构  
**支付范围：** 自助支付仅支持中国大陆渠道：`支付宝`、`微信支付`  
**注册范围：** 以 `手机号短信验证` 为主，`微信绑定/微信登录` 为辅  

---

## 1. 现状与问题

### 1.1 当前前端现状

当前主前端已经收敛到 `frontend-next`：

- 框架：`Next.js 16 + React 19 + App Router + Tailwind CSS 4`
- 已有业务页面：
  - `src/app/translations/new`
  - `src/app/projects`
  - `src/app/workspace/[jobId]`
  - `src/app/usage`
  - `src/app/settings`
  - `src/app/admin/*`
- 已有 UI 基础：
  - `src/components/ui/*`
  - `src/components/app-shell.tsx`
  - `src/app/globals.css`

当前问题：

1. 首页 `src/app/page.tsx` 直接跳到 `/translations/new`，没有官网首页、定价页、试用页、转化入口。
2. `src/app/layout.tsx` 统一包裹 `AppShell`，不利于营销页和工作台页分离。
3. 当前认证仍是邮箱 + 密码模式，不适合国内首发，也不利于反薅羊毛。
4. 当前计费仍是早期 `payment_orders + fake provider` 思路，适合一次性升级，不适合真正的订阅、自动续费和用量计费。

### 1.2 当前网关/计费现状

当前 `gateway` 已具备雏形：

- `gateway/auth.py`：邮箱密码注册、登录、session cookie
- `gateway/models.py`：`User`、`PaymentOrder`、`PaymentWebhookEvent`、`Session`
- `gateway/billing.py`：创建订单、假支付、Webhook、升级 `plan_code`

当前问题：

1. `User` 仍以 `email + password_hash` 为中心，缺少手机号、微信身份、绑定状态。
2. `PaymentOrder` 只适合“一次性支付订单”，不是“订阅对象 + 周期账单 + 代扣授权”的模型。
3. 当前 `plan_code` 还是用户表上的简单字段，不足以承载：
   - trial
   - active subscription
   - renewals
   - grace period
   - downgrade
   - top-up
4. 目前没有统一的 source minutes 使用台账，也没有“增量重生成”层面的账务基础。

### 1.3 当前产品形态与商业化矛盾

你的产品核心是：

- 上传视频
- 转录
- 翻译审校
- 配音与音色选择
- 时间轴对齐
- 交付剪映草稿

这意味着它不是单纯的“工具页”，而是：

- 有审校工作流
- 有多人协作潜力
- 有重 AI 成本
- 有持续使用而非一次性买断特征

因此收费不能只按项目数，也不适合简单做成“无限包月”。

---

## 2. 总体产品决策

### 2.1 结论摘要

这轮改造建议采用以下产品原则：

1. **注册主路径：手机号短信验证**
2. **辅助登录/绑定：微信**
3. **试用策略：7 天 Plus 试用，不绑卡，20 源分钟，一次性发放**
4. **Free 改为低消耗保留层，不再承担主要体验额度**
5. **收费模式：订阅 + 用量 + 席位**
6. **自助支付仅接支付宝 / 微信支付**
7. **Plus / Pro 支持自动续费**
8. **前端采用营销页与工作台分层**
9. **Stitch 只用于营销页/注册转化页设计，不直接生成工作台业务页**

### 2.2 为什么要把“免费体验额度”集中到 Trial

如果你的主要目标是避免薅羊毛，那么不建议继续采用“Free 每月固定赠送较多可导出额度”的模式。更合理的方式是：

- 把主要体验额度集中到一次性的 `7 天 Plus Trial`
- Trial 结束后回落到 `Free`
- Free 主要负责：
  - 保留项目
  - 查看历史
  - 做少量预览
  - 引导升级

这样比“每月给 Free 重置分钟”更稳。

---

## 3. 推荐套餐与收费体系

## 3.1 套餐结构

### Trial（一次性试用，不售卖）

- 名称：`Plus Trial`
- 时长：`7 天`
- 额度：`20 源分钟`
- 功能：
  - 完整单人工作流
  - 导出剪映草稿
  - 1 个目标语言
  - 1 个完整席位
- 限制：
  - 不支持团队协作
  - 不支持共享额度
  - 不支持 API

### Free

- 价格：`¥0`
- 建议定位：保留层，不是主要体验层
- 建议能力：
  - 查看已有项目
  - 少量预览
  - 不建议提供完整可导出分钟月赠送

### Plus

- 价格：`¥99/月`
- 额度：`40 源分钟 / 月`
- 对象：轻度创作者
- 能力：
  - 完整单人工作流
  - 完整配音与对齐
  - 导出剪映草稿
  - 支持加购分钟

### Pro

- 价格：`¥299/月`
- 额度：`120 源分钟 / 月`
- 对象：稳定出片创作者
- 能力：
  - 多项目并行
  - 最多 3 个目标语言
  - 增量重生成
  - 2 个审校席位

### Team

- 价格：`¥899/月`
- 额度：`400 源分钟共享池`
- 对象：工作室 / 小团队
- 能力：
  - 3 个完整席位
  - 10 个审校席位
  - 共享额度
  - 集中账单
  - 团队权限

### Enterprise

- 价格：定制
- 对象：中大型团队
- 能力：
  - 定制额度
  - 定制席位
  - 采购、发票、合同
  - SSO / 审批 / 更强支持

## 3.2 计费单位

建议统一用 `source minutes` 作为核心计费单位。

推荐规则：

1. 上传一个视频，先记录源视频分钟。
2. 每个目标语言消耗对应的 dubbing/processing 分钟。
3. 增量重生成只记增量，不重扣整项目。
4. 未来如果加入 lip-sync、publish 增强，可作为更高倍率 add-on。

## 3.3 自助支付与企业支付边界

### 本期自助支付

- 支付宝
- 微信支付

### 后续企业线

虽然本期自助支付只做支付宝和微信，但企业客户未来仍可能需要：

- 合同采购
- 发票
- 年付
- 对公转账

建议将“企业线下支付”保留为销售流程，不纳入这一期的自助支付实现。

---

## 4. 注册、登录与反滥用方案

## 4.1 账号体系建议

### 主账号标识

推荐以 `user.id (UUID)` 为内部主键，以 `手机号` 作为主登录标识。

### 登录方式

1. **手机号短信验证码登录 / 注册**
2. **微信登录**
3. **微信绑定手机号**

不建议继续把邮箱密码作为主登录路径。邮箱可以保留为：

- 可选资料
- 发票邮箱
- 通知邮箱

## 4.2 推荐登录产品策略

### 方案 A（推荐）

**手机号为主，微信为辅**

- 默认入口：手机号 + 短信验证码
- 微信扫码/微信内登录作为快捷入口
- 领取 Trial 的前提：必须完成手机号验证

好处：

- 防薅效果最好
- 账号体系最清晰
- 续费、通知、找回账号都更稳定

### 方案 B

**微信登录为主，手机号补绑**

- 适合强微信生态产品
- 但对 Web SaaS 不如手机号主路径稳

本项目更推荐方案 A。

## 4.3 Trial 发放规则（反薅核心）

建议把 Trial 发放与实名级别更强的身份绑定：

1. 一个中国大陆手机号仅允许一个 Trial
2. 一个微信 `unionid` 仅允许一个 Trial
3. 一个设备 / 浏览器指纹在短周期内超过阈值时进入人工/机器风控
4. 一个 IP 段在短时间内大量注册时触发限流
5. SMS 发送做频率限制：
   - 单手机号 1 分钟 1 次
   - 1 小时 5 次
   - 24 小时 10 次

## 4.4 推荐反滥用分层

### 硬门槛

- Trial 必须手机号验证成功
- 微信登录用户如未绑手机，不发放 Trial

### 软风控

- 新账号 24 小时内最大创建项目数
- 新 Trial 账号首日最大导出次数
- 异常高频操作进入二次验证

### 审计留痕

需要记录：

- `phone_hash`
- `wechat_unionid_hash`
- `device_fingerprint_hash`
- `trial_granted_at`
- `risk_score`

---

## 5. 支付与订阅方案（仅支付宝 / 微信支付）

## 5.1 总体建议

### 自助订阅支付支持

- `Plus`：月付自动续费
- `Pro`：月付自动续费
- `Team`：可支持月付自动续费，但建议同时保留“联系销售”

### 自动续费方式

- 支付宝：签约代扣 / 商家扣款
- 微信支付：委托代扣 / 自动续费

> 说明：微信支付官方文档明确将自动续费放在委托代扣模式下，并说明续费扣款存在“通知后 24 小时自动扣费”等模式；支付宝/Antom 官方文档也明确存在“先授权、后续自动扣款”的代扣模式。  
> 参考：  
> [微信支付委托扣款模式](https://pay.wechatpay.cn/doc/v2/merchant/4012205799)  
> [微信支付合作伙伴接入说明（自动续费说明）](https://pay.wechatpay.cn/doc/v2/partner/4011988361)  
> [Antom 代扣产品总览](https://docs.antom.com/ac/autodebit_cn/overview)  
> [Antom 代扣支付说明](https://docs.antom.com/ac/autodebit_cn/pay)

## 5.2 支付产品设计原则

不管走支付宝还是微信，产品层必须遵守这几件事：

1. 显示清楚：
   - 套餐名
   - 每月金额
   - 扣费周期
   - 自动续费说明
   - 取消路径
2. 签约前提示清楚：
   - 当前 Trial 是否结束
   - 升级后何时开始计费
   - 是否立即扣款
3. 扣费前提醒：
   - App 内通知
   - 短信提醒
   - 微信服务通知（如后续接入）
4. 必须支持用户自主解约

## 5.3 订阅模型建议

不要再把订阅状态塞进 `users.plan_code` 作为唯一真相。推荐改成：

### `users`

保留缓存字段：

- `current_plan_code`
- `current_subscription_status`

但不再作为唯一计费真相。

### `subscriptions`

新增订阅表，作为真相源：

- `id`
- `user_id`
- `workspace_id`
- `plan_code`
- `billing_period`
- `status`
  - `trialing`
  - `active`
  - `past_due`
  - `grace`
  - `canceled`
  - `expired`
- `trial_ends_at`
- `current_period_start`
- `current_period_end`
- `cancel_at_period_end`
- `renewal_provider`
  - `alipay`
  - `wechatpay`
- `renewal_mandate_id`

### `subscription_mandates`

记录自动续费签约关系：

- `id`
- `user_id`
- `provider`
- `provider_contract_id`
- `status`
  - `active`
  - `revoked`
  - `expired`
- `signed_at`
- `revoked_at`
- `metadata_json`

### `billing_invoices`

每个周期一张账单：

- `id`
- `subscription_id`
- `period_start`
- `period_end`
- `amount_cny`
- `status`
  - `pending`
  - `paid`
  - `failed`
  - `void`
- `provider`
- `provider_order_id`

### `payment_attempts`

记录每次支付尝试与代扣结果：

- `invoice_id`
- `attempt_no`
- `provider`
- `status`
- `provider_event_id`
- `raw_payload`

### `usage_ledger`

记录 source minutes、top-up、消耗、回滚：

- `user_id`
- `workspace_id`
- `kind`
  - `trial_grant`
  - `subscription_grant`
  - `topup_grant`
  - `job_reserve`
  - `job_commit`
  - `job_release`
- `minutes_delta`
- `job_id`
- `created_at`

## 5.4 自动续费建议流程

### 首次购买

1. 用户选 `Plus` 或 `Pro`
2. 选择支付方式：支付宝 / 微信
3. 完成支付 + 代扣签约
4. 创建：
   - subscription
   - mandate
   - first invoice
5. 激活套餐与额度

### 月度续费

1. 在 `current_period_end - N天` 创建待续费 invoice
2. 发出续费提醒
3. 发起代扣
4. Webhook 更新 invoice/subscription
5. 成功则进入新周期
6. 失败则进入 `past_due/grace`

### 建议的失败处理

- 第 1 次失败：立即通知
- 第 2 次失败：24 小时后重试
- 第 3 次失败：72 小时后重试
- 超过 7 天仍失败：
  - 订阅进入 `expired`
  - 账号降级到 Free
  - 保留项目，不删除数据

## 5.5 微信支付的实现注意点

微信自动续费不是“商家在当下发起就当下扣到”，而是可能存在扣款前通知与扣款延迟。  
因此系统上要：

1. 提前创建续费账单
2. 有 `renewal_pending` / `charge_requested` 状态
3. 不能假设扣款请求发送后立刻成功

## 5.6 支付宝的实现注意点

支付宝代扣流程本质也是：

1. 用户授权
2. 平台拿到有效支付令牌/签约关系
3. 后续自动发起扣款

系统也必须把“签约关系”和“单次订单”分开建模，不能只保留一次性的 `PaymentOrder`。

---

## 6. 前端改造方案（适配当前 frontend-next）

## 6.1 改造原则

1. 不推翻当前 `frontend-next`
2. 不让 Stitch 直接生成整套业务前端
3. 营销页与工作台页分层
4. 业务页继续沿用现有 `AppShell`

## 6.2 推荐路由分层

当前问题在于 `src/app/layout.tsx` 全局使用 `AppShell`，且 `/` 直接跳 `/translations/new`。

建议改成：

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
    compare/page.tsx

  (auth)/
    layout.tsx
    auth/
      page.tsx
      callback/
        wechat/page.tsx
      bind-phone/page.tsx
      bind-wechat/page.tsx
      onboarding/page.tsx

  (app)/
    layout.tsx
    translations/new/page.tsx
    projects/page.tsx
    projects/[jobId]/page.tsx
    workspace/[jobId]/page.tsx
    tasks/current/page.tsx
    voices/page.tsx
    usage/page.tsx
    settings/page.tsx
    settings/billing/page.tsx
    settings/subscription/page.tsx
    settings/payment-methods/page.tsx
    billing/success/page.tsx
    billing/result/page.tsx
    admin/...
```

## 6.3 Layout 职责划分

### `src/app/layout.tsx`

只保留：

- `<html>`
- `<body>`
- 全局字体
- `Toaster`
- 全局 Providers

不要在这里直接包 `AppShell`。

### `src/app/(marketing)/layout.tsx`

负责：

- 营销导航
- Footer
- 无工作台侧边栏

### `src/app/(auth)/layout.tsx`

负责：

- 纯净认证布局
- 无 AppShell
- 更偏转化与验证流程

### `src/app/(app)/layout.tsx`

负责：

- `AppShell`
- 当前工作台导航
- 登录态守卫

## 6.4 组件目录建议

```text
frontend-next/src/components/
  app-shell.tsx

  marketing/
    hero.tsx
    pricing-grid.tsx
    trial-banner.tsx
    compare-table.tsx
    faq.tsx
    footer.tsx

  auth/
    phone-login-form.tsx
    sms-code-input.tsx
    wechat-login-card.tsx
    bind-phone-form.tsx
    onboarding-form.tsx

  billing/
    subscription-card.tsx
    plan-selector.tsx
    payment-method-selector.tsx
    invoice-table.tsx
    usage-summary-card.tsx
    topup-card.tsx

  workspace/
    ...

  ui/
    ...
```

## 6.5 配置数据建议

新增前端静态配置：

```text
frontend-next/src/lib/
  pricing/
    plans.ts
    trial.ts
  auth/
    guards.ts
    session.ts
  billing/
    format.ts
    entitlements.ts
```

### `plans.ts`

维护：

- 套餐名
- 月付价
- 年付折扣
- CTA 文案
- 功能矩阵

### `trial.ts`

维护：

- 7 天试用
- 20 源分钟
- 试用 eligibility 文案

---

## 7. Stitch 协同设计方案

## 7.1 Stitch 的正确定位

Stitch 在本项目里应该定位为：

- **营销页设计器**
- **注册/试用转化页设计器**
- **信息架构生成器**

而不应该是：

- 工作台业务页代码生成器
- 审校面板生成器
- 复杂状态流页面的最终实现来源

## 7.2 适合用 Stitch 的页面

1. 官网首页
2. 定价页
3. 试用页
4. 注册/登录页
5. 升级页
6. 支付结果页

## 7.3 不适合直接交给 Stitch 的页面

1. `/workspace/[jobId]`
2. `/projects/[jobId]`
3. 审校工作台
4. 管理后台
5. 复杂 billing console

## 7.4 Stitch 协同工作流

### Step 1：先产出文案与信息架构

由产品/前端先固定：

- 页面目标
- 主 CTA
- 套餐信息
- 试用规则
- 主要模块顺序

### Step 2：用 Stitch 生成 2-3 套方向

建议出：

1. 偏 SaaS 商业化
2. 偏创作者工具
3. 偏团队协作 / B2B

### Step 3：只接收这几类产物

- 高保真视觉稿
- section 顺序
- 组件层级建议
- 响应式布局参考

不要直接接收整页代码作为最终产物。

### Step 4：映射到现有 design system

将 Stitch 稿映射到现有：

- `globals.css` token
- `ui/button.tsx`
- `ui/card.tsx`
- `ui/badge.tsx`

### Step 5：在 `frontend-next` 中手工落地

实现方式应是：

- Stitch 给参考
- 你现有项目给真实可维护代码

## 7.5 Stitch 交付物约束

每个营销页设计稿都应输出：

1. 桌面版
2. 移动版
3. section list
4. CTA hierarchy
5. color/token mapping

---

## 8. 主要页面规划

## 8.1 官网首页 `/`

### 目标

- 讲清楚产品是什么
- 引导试用
- 引导定价页

### 模块

1. Hero
2. 价值主张
3. 为什么是剪映草稿
4. 工作流说明
5. 套餐入口
6. FAQ
7. CTA

## 8.2 定价页 `/pricing`

### 目标

- 让用户理解 Trial / Plus / Pro / Team
- 把用户导向 7 天 Plus 试用

### 模块

1. Hero
2. Trial 横幅
3. 为什么这样收费
4. 套餐卡
5. 功能对比表
6. 加购与席位
7. FAQ
8. CTA

## 8.3 试用页 `/trial`

### 目标

- 解释 Trial 规则
- 降低试用焦虑
- 引导注册

### 模块

1. Trial 是什么
2. 包含什么
3. 不包含什么
4. 试用结束后会怎样
5. 开始试用按钮

## 8.4 认证页 `/auth`

### 目标

- 用一个入口同时承接：
  - 手机号登录/注册
  - 微信登录

### 推荐交互

- Tab 1：手机号验证码
- Tab 2：微信扫码登录

### 不建议

- 继续维持“邮箱登录页”和“邮箱注册页”作为主入口

## 8.5 首次登录后的 Onboarding

### 页面

`/auth/onboarding`

### 目标

- 采集最少必要资料
- 发放 Trial
- 引导进入首个项目创建

### 建议采集字段

- 昵称
- 使用目的（创作者 / 团队 / 企业）
- 预计月度分钟需求（分档）
- 是否绑定微信

## 8.6 Billing 页面

### `/settings/billing`

展示：

- 当前套餐
- Trial 剩余时间
- 续费状态
- 账单记录

### `/settings/subscription`

展示：

- 当前订阅状态
- 自动续费开关
- 到期时间
- 升级 / 降级 / 取消

### `/settings/payment-methods`

展示：

- 已签约支付宝
- 已签约微信支付
- 当前默认续费方式
- 更换 / 解绑

## 8.7 结算页

### `/billing/checkout`

展示：

- 当前套餐
- 金额
- 周期
- 自动续费说明
- 支付方式选择

### `/billing/success`

展示：

- 付款成功
- 当前套餐已生效
- 下一次续费时间
- 去开始新建项目

### `/billing/result`

用于处理：

- 支付处理中
- 回跳失败
- 待确认

---

## 9. 后端改造建议（gateway）

## 9.1 认证域

建议新增/拆分：

```text
gateway/
  auth_phone.py
  auth_wechat.py
  auth_sessions.py
  sms_provider.py
  risk_control.py
```

### 新增表建议

- `user_identities`
- `sms_verification_codes`
- `wechat_oauth_states`
- `risk_events`

## 9.2 订阅域

建议新增：

```text
gateway/
  subscriptions.py
  invoices.py
  usage_metering.py
  plan_catalog.py
```

### 新增表建议

- `subscriptions`
- `subscription_mandates`
- `billing_invoices`
- `payment_attempts`
- `usage_ledger`

## 9.3 支付域

在当前 `payment_providers.py` 架构上继续扩展，但只保留：

- `alipay`
- `wechatpay`
- `fake`（开发环境）

建议新增：

```text
gateway/payment_providers/
  alipay.py
  wechatpay.py
```

### 注意

不要把“下单支付”与“自动续费代扣”混成一套简单订单状态机。  
必须区分：

1. 首次支付
2. 签约成功
3. 周期账单
4. 自动扣款
5. 失败重试

## 9.4 配额域

当前 `jobs` 已经有：

- `source_duration_seconds`
- `quota_cost`
- `plan_code_snapshot`

这是好基础。下一步应把它们接到 `usage_ledger`：

- job 创建时 reserve
- job 完成时 commit
- job 取消时 release

---

## 10. 推荐实施阶段

## Phase 0：信息架构与设计冻结

### 输出

- 套餐最终版
- Trial 最终版
- 注册策略最终版
- Stitch 设计稿

### 不做

- 不写支付代码
- 不改业务页

## Phase 1：前端营销层改造

### 目标

- 建立 `(marketing)` 与 `(app)` 双布局
- 上线首页 / 定价页 / 试用页

### 涉及文件

- `frontend-next/src/app/layout.tsx`
- `frontend-next/src/app/(marketing)/*`
- `frontend-next/src/app/(app)/*`
- `frontend-next/src/components/marketing/*`

## Phase 2：认证改造

### 目标

- 手机短信登录/注册
- 微信登录 / 绑定
- Trial 发放逻辑

### 涉及文件

- `gateway/auth.py`（拆分）
- 新 auth provider files
- `frontend-next/src/components/auth/*`

## Phase 3：订阅与账单域

### 目标

- 从 `PaymentOrder` 升级到 `subscriptions + invoices + mandates`
- 打通 Plus / Pro 月付自动续费

### 涉及文件

- `gateway/models.py`
- `gateway/billing.py`
- `gateway/payment_providers.py`
- 新 subscriptions/invoices files

## Phase 4：支付接入

### 目标

- 支付宝首次签约 + 支付
- 微信首次签约 + 支付
- 周期代扣
- Webhook 与重试

## Phase 5：Billing UI 与用量台账

### 目标

- `/settings/billing`
- `/settings/subscription`
- `/settings/payment-methods`
- `/usage` 与分钟台账联动

## Phase 6：团队与审校席位

### 目标

- workspace 成员
- 审校席位
- 共享额度池

---

## 11. 风险与关键决策

## 11.1 最大风险

1. 自动续费不是“支付接入”这么简单，而是“订阅系统”
2. 如果继续沿用邮箱密码主账号，会和国内支付/手机号运营逻辑脱节
3. 如果 Stitch 越界到工作台业务页，会造成前端维护成本飙升

## 11.2 必须先定的产品决策

1. Free 是否保留每月刷新额度
   - **建议：不保留**
2. Trial 是否必须手机号验证
   - **建议：必须**
3. 微信登录后是否必须绑手机才能领 Trial
   - **建议：必须**
4. Team 是否支持自助月付
   - **建议：支持，但保留联系销售**

---

## 12. 最终建议（可直接执行的版本）

如果只选一条最稳的路线，建议这样做：

1. **前端先做双布局：marketing / app**
2. **先用 Stitch 做首页、定价页、试用页、认证页设计稿**
3. **认证切到手机号短信主路径**
4. **微信作为登录与绑定手段，不作为 Trial 发放唯一依据**
5. **把 Trial 做成 7 天一次性 Plus 试用**
6. **自助支付只做支付宝 + 微信**
7. **Plus / Pro 做自动续费**
8. **把 `PaymentOrder` 升级为 `subscriptions + invoices + mandates + usage_ledger`**
9. **Free 只保留低消耗能力，不再承担主要体验额度**

一句话总结：

> 这轮不是“做几个新页面”，而是把当前工作台补齐成一个中国市场可转化、可续费、可控滥用的 SaaS 商业化前台。
