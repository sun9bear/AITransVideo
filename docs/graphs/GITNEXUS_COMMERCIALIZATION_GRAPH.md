# GitNexus 商业化图

关联总图：`docs/graphs/GITNEXUS_PROJECT_GRAPH.md`

## 1. 范围

这张子图只看商业化相关链路，重点是：

- 营销首页、定价页、试用页、法律页、settings billing center
- Gateway 侧的 pricing runtime、plan catalog、trial、credits、payment
- provider abstraction、provider availability 与 Alipay live path
- SSR 套餐事实如何进入营销页 initial HTML
- 前端如何消费商业事实，而不是重定义商业事实

不展开主流程内部实现，只保留与计费、套餐、权益相关的连接点。

## 2. 商业化主图

```mermaid
graph TD
    PublicRoutes["Public route boundary<br/>middleware.ts"] --> Home["Marketing home / trust surface"]
    PublicRoutes --> PricingPage["Pricing page"]
    PublicRoutes --> TrialPage["Trial page"]
    PublicRoutes --> Legal["Legal / policy pages"]

    Home --> SSRPlans["SSR plans fetch<br/>getPlansSafeServer()"]
    PricingPage --> SSRPlans
    TrialPage --> SSRPlans
    SSRPlans --> PublicPlans["GET /api/plans"]
    PublicPlans --> GatewayTruth

    BillingCenter["Settings / Billing UI"] --> FrontAPI
    Checkout["Checkout / Top-up actions"] --> FrontAPI
    VoicePricing["Voice selection pricing consumer"] --> FrontAPI
    AdminPricing["Admin Pricing UI"] --> AdminAPI

    FrontAPI["Frontend API layer<br/>plans / billing / session"] --> GatewayTruth
    AdminAPI["Admin pricing API"] --> GatewayTruth

    GatewayTruth["Gateway truth layer<br/>pricing_runtime / plan_catalog"] --> TrialRules["Trial rules"]
    GatewayTruth --> Plans["Plan catalog / prices"]
    GatewayTruth --> Credits["credits_service / subscription state"]
    GatewayTruth --> Billing["billing.py"]
    GatewayTruth --> Session["auth / session / entitlements"]

    Billing --> ProviderConfig["get_checkout_config"]
    Billing --> Orders["create_order / history / order refresh"]
    Billing --> Webhooks["provider-dispatched webhooks"]
    Billing --> Providers["payment_providers.py"]
    Providers --> Alipay["payment_provider_alipay.py"]

    Plans --> VoicePricing
    Credits --> Ledger["credits / invoices / entitlements"]
    Webhooks --> Ledger
    Session --> FrontAPI
```

## 3. 真源边界

### 3.1 marketing 前门现在通过 SSR 消费套餐真源

- `frontend-next/src/app/(marketing)/page.tsx` 首页已经稳定组合：
  `Hero`
  `ProductProof`
  `WorkflowShowcase`
  `Features`
  `TrustBanner`
  `PricingPreview`
  `Faq`
  `FinalCta`
- 同文件注释明确：
  `PricingPreview / TrialBanner` 是 async Server Components，prices ship in initial HTML
- `frontend-next/src/app/(marketing)/pricing/page.tsx` 注释写明三档套餐数字来自 `GET /api/plans`
- `frontend-next/src/app/(marketing)/trial/page.tsx` 通过 `getPlansSafeServer()` 读取试用数字事实，并只在 `trial.frozen === true` 时渲染数值
- `frontend-next/src/lib/billing/get-plans.ts` 明确说这是前端学习 plan / pricing / trial runtime facts 的唯一受支持路径

结论：营销前门已不是纯静态 copy surface，而是 Gateway 商业事实的 SSR 消费层。

### 3.2 runtime pricing 仍从 Gateway 启动时装载

- `gateway/main.py:lifespan` 在启动时调用 `get_runtime_pricing(force_reload=True)`
- `gateway/pricing_runtime.py` 使用 `PricingPayload` 承载 runtime pricing，并在缺失时回退到默认 payload
- `gateway/plan_catalog.py` 从 `get_runtime_pricing()` 继续读取 `plans` 与 `trial`

这条链说明套餐、试用规则、价格快照都应继续以 Gateway 为真源。

### 3.3 public route 与 provider availability 都不能由前端自己猜

- `frontend-next/src/middleware.ts` 明确把这些路径放进 public exact paths：
  `/`
  `/pricing`
  `/trial`
  `/auth`
  `/terms`
  `/privacy`
  `/refund`
  `/contact`
- 注释直接说明原因：
  marketing-layer pages 必须在无 session cookie 时可达，否则 conversion surface 会被错误重定向
- `gateway/billing.py:get_checkout_config()` 明确声明“Gateway owns provider availability”
- 该接口返回：
  `default_provider`
  `providers[]`
  `operational`
- 优先顺序当前是：
  `alipay -> wechatpay -> stripe -> fake`

因此前端不能通过 env、常量或 UI 顺序去推断支付渠道是否可用。

## 4. Billing center 已经成形

`frontend-next/src/app/(app)/settings/billing/page.tsx` 当前组合了：

- `SubscriptionSummary`
- `CreditsSummary`
- `CheckoutCard`
- `OrderHistory`

它承担的是“展示和触发”职责，不应成为定价、渠道可用性或 entitlement 的事实源。

## 5. Voice pricing 也是商业事实消费面

- `frontend-next/src/lib/api/voiceSelection.ts` 提供 `getVoiceSelectionPricing()`
- `frontend-next/src/components/workspace/VoiceSelectionPanel.tsx` 会读取 `voice_clone_cost_credits`
- 这个消费面虽然发生在 workspace review 阶段，但它仍然读取 Gateway runtime pricing truth，而不是维护第二套 clone 定价常量

结论：商业化图不只覆盖 marketing 与 billing center，也要覆盖 workspace 内部对价格事实的运行时消费。

## 6. Provider abstraction 与 Alipay live path

### 6.1 provider abstraction

- `gateway/billing.py` 文件头已经明确：
  provider-specific logic 走 adapter
  `_process_payment_event` 保持 provider-agnostic
  payment 只修改 entitlements，不触碰 job snapshot
- `gateway/payment_providers.py` 当前注册：
  `fake`
  `alipay`
  `wechatpay`
  `stripe`

### 6.2 Alipay

- `gateway/payment_provider_alipay.py` 支持：
  `alipay.trade.wap.pay`
  `alipay.trade.page.pay`
- 同文件也负责：
  notify payload 验证
  query payload 验证
  `alipay.trade.query`

结论：Alipay 现在不是单点硬编码，而是 provider registry 中的 live adapter。

## 7. 法律与信任面

当前营销面已经显式包含：

- `frontend-next/src/app/(marketing)/privacy/page.tsx`
- `frontend-next/src/app/(marketing)/refund/page.tsx`
- `frontend-next/src/app/(marketing)/terms/page.tsx`

这意味着商业化图不应只画定价卡片和 checkout，还要把法律页当成面向用户的 trust surface。

## 8. 当前商业化边界

从当前代码组织看，商业化仍然是 staged v2 migration，而不是 big-bang rewrite：

- 真源仍是 `pricing_runtime -> plan_catalog -> billing / credits / entitlements`
- 前端承担 SSR 展示、结账、会话与状态刷新
- admin pricing 是发布面，不是第二套 pricing 系统
- `cost estimator` 或营销 copy 都不能升级成结算事实源

任何让前端重新定义 plan / price / entitlement truth 的改动，都应被视为架构漂移。

## 9. 这张图适合回答什么问题

- 首页、定价页、试用页为什么必须能在无登录状态下访问
- pricing / trial 数字为什么要在 SSR 时就从 Gateway 注入，而不是写死在组件里
- 套餐、试用、价格和 credits 费率究竟谁是最终真源
- 前端该读哪里来决定 provider 是否可用
- workspace 里的 voice clone cost 为什么仍属于商业事实消费面
- Alipay、fake pay、webhook settlement 现在分别在什么层
- 为什么法律页与 settings billing center 也应该算商业化图的一部分
