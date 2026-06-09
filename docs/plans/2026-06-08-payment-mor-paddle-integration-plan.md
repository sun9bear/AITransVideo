# Paddle MoR 收款接入方案（无 ICP 破局 + 出海）

> **状态**：🟢 平台已上线（Paddle 账户 live）+ 经 CodeX 审核修订 → 进入 P1 集成
> **版本**：v2（2026-06-09，并入 CodeX 审核 6 点 + Claude 复评 5 点）；初版 v1 2026-06-08
> **作者**：Claude（与项目主对话冻结）；**审核**：CodeX
> **关联**：[微信支付 Native plan](2026-05-22-wechatpay-native-integration-plan.md)（DEFERRED）、[商业化图](../graphs/GITNEXUS_COMMERCIALIZATION_GRAPH.md)
> **关联记忆**：`project_wechatpay_integration`、`feedback_terminal_state_single_entry`、`feedback_compose_env_file_recreate`、`deploy_experience`

---

## 0. 当前进度（2026-06-09）

- ✅ **Paddle 账户已激活、checkout 已开启**（主体：个体工商户 武汉市洪山区九俊电子经营部 / 经营者 孙九雄）。
- ✅ **Wise USD 收款账户就绪**（ACH routing + account number + SWIFT，户名孙九雄，支持平台 payout）；Paddle **payout verification 待首笔交易触发**（1–2 工作日，届时填 USD 账户信息）。
- ✅ 网站 **退款 / 隐私 / 条款页已上线**；运营主体已改为 **个体工商户 + sun9bear@126.com**（消除了 v1 时"网站显示有限公司 + 旧邮箱"与申请主体的矛盾——那是上一次注册被拒的疑似主因）。
- ⏭️ **待办：P1 编码接入**（让 checkout 真正出现在 `aitrans.video` 上）。

## 0.1 v2 修订要点（CodeX 审核 + Claude 复评）

| # | 修订 | 影响章节 |
|---|---|---|
| R1 | `checkout.url` 不是"拿到即可跳的托管页"——Paddle Billing 需自建 **Paddle.js + `_ptxn`** 的 approved-domain 页面才能开结账 | §7.2 §8 |
| R2 | 订单查询兜底当前**只对 alipay 生效**（[billing.py:207]），必须**泛化到任意 provider**；WeChat 延迟捕获(~10min)使其关键 | §7.5 |
| R3 | webhook **只验签不够**：必须 **验签 + 订单事实校验 + 事件过滤** 三道 | §7.3 |
| R4 | 价格漂移守卫不能只靠本地 env，要**从 Paddle API 拉 price 比对** | §6 |
| R5 | Pro 年付**微信兜底降级**：Paddle WeChat 偏 desktop，移动端别当可靠 fallback，主推银行卡 | §5 |
| R6 | 前端当前只用 gateway default provider，**三轨选择 UI 缺失**（P2） | §8 §13 |
| R7 | 补 **退款/拒付事件**安全处理（200-ignore + 规划积分回收） | §7.4 |
| R8 | 补 **订单级"只结算一次"护栏**（event_id 幂等之外） | §7.4 |
| R9 | **Sandbox 先行**：P1 在 Paddle Sandbox 跑，P2 才上生产小额 | §13 |
| R10 | **金额单位**：Paddle CNY 用最小单位（分），与 `plan_catalog` fen 对齐 | §6 §7.3 |
| R11 | `checkout.url` 为空（未配 default payment link）的兜底 | §7.2 |

---

## 1. 背景与问题

项目当前收款链路被 **ICP 备案死结**卡住：

- 域名境外注册、服务器境外，**无法 ICP 备案**。`.video` TLD 大概率也不在 MIIT 可备案列表内。
- 无备案 → **支付宝**网站/WAP 支付只能试用 1 个月，**现已被终止**。
- 无备案 → **微信只能 Native 扫码**（`code_url`→二维码）。桌面用户用手机扫 OK；**移动端 web 用户要用第二台手机扫，体验差**，仅能作为备用。
- 微信 H5 / JSAPI（移动端一键唤起）**需备案**，做不了。

**结论**：在不改变"境外域名 + 境外服务器 + 无备案"现状的前提下，直连支付宝/微信商户这条路在移动端走不通。

## 2. 核心思路：用 MoR 绕开 ICP

**Merchant of Record（记录商户）平台用它自己的跨境支付牌照收款，我们不需要 ICP、不需要自己的支付宝/微信商户。** Paddle 官方明确：开启支付宝/微信 *"You don't need to establish a local entity or sign up for any merchant accounts."*

| 支付方式 | 需 ICP | 移动端体验 | 现状 |
|---|---|---|---|
| 自有微信 Native 扫码 | 否 | 差（需第二台手机） | ✅ 已有，保留为桌面/备用 |
| 自有支付宝网站/WAP | **是** | 好 | ❌ 已终止 |
| 微信 H5/JSAPI | **是** | 好 | ❌ 做不了 |
| **支付宝 via Paddle** | **否** | **好（移动端唤起支付宝 App）** | 🟢 本方案新增 |
| 银行卡/银联/PayPal via Paddle | 否 | 好 | 🟢 本方案新增（出海主力） |

> ⚠️ **诚实边界**：移动端**微信**经 MoR 仍偏二维码（微信封锁站外 H5 唤起）。MoR 给移动端的真正突破口是 **支付宝 deep-link + 银行卡/银联**；移动端微信一键支付长期仍需 ICP（见 §13 长期路线）。

## 3. 决策记录

| # | 决策 | 理由 |
|---|---|---|
| D1 | **主轨 = Paddle**（拍板 2026-06-08，账户已 live 2026-06-09） | 唯一同时满足【接受大陆个体主体免海外公司】+【支付宝&微信均已上线】+【明确无需自有商户/备案】的成熟 MoR |
| D2 | 排除 Lemon Squeezy | 被 Stripe 收购走 Stripe 通道，官方收款国列表无中国大陆，2026 起停收大陆新户 |
| D3 | 排除 Creem / Polar（暂） | 更便宜（3.9–4%）但买家侧支付宝/微信"coming soon"/未确认，当前只能做国际；将来纯海外可再评估 |
| D4 | 排除 FastSpring（暂） | 支付宝/微信已上线但 5.9%+ 更贵、45 天首付款冻结、单笔硬上限 ¥3000 |
| D5 | **一次性商品（one-time），不用 Paddle 订阅** | 现有模型即"按周期一次性付费、不自动续费"（Alipay `page.pay` + `TRIAL_CONFIG.auto_charge=False`）；**且 Paddle 微信支付只支持一次性商品** |
| D6 | **定价币种 = CNY** | 支付宝仅对 CNY 定价 + 中国地址买家展示；微信对中国买家展示；与 `plan_catalog` 一一对应；海外卡由 Paddle 自动换汇 |
| D7 | **三轨混合 + 按场景路由**，不全量走 Paddle | 国内费率 0.6% vs Paddle ~7.5%（12×）；只在直连走不通的场景（移动端、海外）用 Paddle |
| D8 | **采纳 CodeX 审核（R1–R6）+ Claude 复评（R7–R11）** | webhook 三道校验、checkout 需 Paddle.js、query 兜底泛化等是收款安全/可用的硬要求 |

## 4. 三轨混合架构

```
                     ┌─ 桌面 + 国内 + 小额 ─→ Rail 1: 自有微信 Native 扫码 (~0.6%)
用户发起付款 ─路由─┤
                     ├─ 移动 + 国内 ────────→ Rail 2: Paddle（支付宝/银行卡, ~7.5%）
                     └─ 海外用户 ───────────→ Rail 2: Paddle（卡/PayPal/本地支付）
（未来拿到 ICP）────────────────────────────→ Rail 3: 直连支付宝/微信 H5+JSAPI (~0.6%)
```

| 场景（surface × 地域） | 默认推荐 | 备选 |
|---|---|---|
| 桌面 + 国内 | 微信 Native 扫码（最省） | Paddle（支付宝/卡） |
| 移动 web + 国内 | **Paddle 支付宝**（唤起 App） | 微信 Native 扫码（需第二设备） |
| 任意 + 海外 | Paddle（卡/PayPal/本地） | — |

> `checkout_surface`（`pc_web`/`mobile_web`）已有现成判定：`payment_provider_alipay.detect_checkout_surface(user-agent)`，[billing.py:127](../../gateway/billing.py) 下单时已传入。Paddle 复用同一信号。

## 5. 套餐金额 × 支付宝单笔上限分析

现价（[gateway/plan_catalog.py:69](../../gateway/plan_catalog.py)，CNY 分存储，已 frozen）：

| 套餐 | 月 | 季 | 年 |
|---|---|---|---|
| Plus | ¥99 | ¥269 | ¥999 |
| Pro | ¥299 | ¥799 | **¥2999** |

Paddle 支付宝软上限参考 ~¥1600。逐项对照：6 个价位中只有 **Pro 年（¥2999）** 触及上限，其余全部安全。**无需调价。**

**Pro 年（¥2999）兜底（R5 修订）：**
- 移动端 >¥1600 **主推银行卡 / 银联**（无此上限）。
- ⚠️ **不要把 WeChat 当移动端可靠 fallback**——Paddle WeChat 偏 desktop / 二维码，移动端不可靠（实现时对一下官方文档措辞）。
- 产品上可把 **Pro 季 ¥799** 作为"支付宝友好档"主推；桌面端 WeChat 可作为额外选项。

## 6. Paddle 产品 / 价格映射

当前积分随订阅周期发放（`credits_service.ensure_subscription_bucket`），**无独立积分包**。故 Paddle 侧只需 **6 个一次性 price**：

| plan_code | billing_period | CNY | Paddle price |
|---|---|---|---|
| plus | monthly/quarterly/annual | 99/269/999 | `pri_plus_m` / `pri_plus_q` / `pri_plus_a` |
| pro | monthly/quarterly/annual | 299/799/2999 | `pri_pro_m` / `pri_pro_q` / `pri_pro_a` |

- 映射表 `(plan_code, billing_period) → paddle_price_id` 放在 `payment_provider_paddle.py` 常量 + env 覆盖。
- **价格漂移守卫（R4 强化）**：不能只在本地比 `amount_cny` == 本地常量。要在 **启动时或创建 checkout 时从 Paddle API 拉这 6 个 price**，校验 `currency==CNY`、`unit amount` 与 `plan_catalog` 一致、`billing_cycle` 为 one-time。**真源永远是 `plan_catalog.py`，Paddle dashboard 是外部镜像，必须自动发现 drift**（不一致 → 启动告警 / 拒绝创建）。
- **单位（R10）**：Paddle CNY 金额用**最小单位（分）字符串**，与 `plan_catalog` 的 fen 对齐；比对时按分整数比，别栽在小数/单位上。
- 将来若卖积分包，追加一次性 price，同表扩展。

## 7. 后端接入设计（贴合现有抽象，结算入口零改动）

### 7.1 新增 `PaddleProvider`

[gateway/payment_providers.py](../../gateway/payment_providers.py) 实现 `PaymentProvider` Protocol，并在 `_init_registry()`（[payment_providers.py:303](../../gateway/payment_providers.py)）注册 `"paddle"`：

```
create_checkout(*, order_id, amount_cny, target_plan_code, billing_period, checkout_surface) -> CheckoutResult
verify_signature(raw_body, headers) -> bool      # Paddle 用 headers（Paddle-Signature），区别于 Alipay 的 payload 验签
parse_webhook(raw_body) -> NormalizedWebhookEvent
map_status(event_type) -> str                    # transaction.completed/paid→paid；payment_failed→failed；refunded→refunded；其余→pending/ignore
async query_order(*, order_id, provider_order_id) -> ProviderOrderQueryResult | None  # GET /transactions/{id}
```

### 7.2 checkout 创建 + Paddle.js 页面（R1/R11 修订）

⚠️ **Paddle Billing 的 `checkout.url` 不是 Paddle 全托管页**：它依赖你在 dashboard 配的 **default payment link**（一个 approved-domain 上、加载 **Paddle.js** 的页面），Paddle.js 读 URL 里的 `_ptxn={txn_id}` 自动打开结账。仓库当前**没有 Paddle.js / `Paddle.Initialize` / `_ptxn` 逻辑**，必须补：

- 新增前端页 **`/paddle-checkout`**（approved domain `aitrans.video`），加载 Paddle.js：读 `_ptxn` → `Paddle.Checkout.open()`，成功回跳 `/settings/billing?provider=paddle&order_id=...&status=processing`。
- 后端 `create_checkout`：调 Paddle API 建 transaction（`items=[price_id×1]` + `custom_data={order_id}`），把 default payment link 指向 `/paddle-checkout`，返回 `CheckoutResult(checkout_url=<.../paddle-checkout?_ptxn=...>, provider_order_id=txn_id)`。
- **R11**：若 `checkout.url` 为空（default payment link 未配）→ `create_checkout` 报错、provider 视为未就绪，不返回坏链接。
- 备选实现：直接在现有付款页 **inline overlay**（`Paddle.Checkout.open({transactionId})`，不跳页 UX 更好）；但 `/paddle-checkout` 跳转更贴合现有"返回 checkout_url 跳转"的 Protocol，二选一。

### 7.3 Webhook 校验：验签 + 订单事实校验 + 事件过滤（R3，三道缺一不可）

webhook 入口 `POST /api/billing/webhooks/{provider_name}`（[billing.py:562](../../gateway/billing.py)）。Paddle 分支必须做满三道：

1. **验签**：`Paddle-Signature` 头 `ts=...;h1=...`，`HMAC-SHA256(secret, f"{ts}:{raw_body}")` 比对 `h1`，并校验 `ts` 新鲜度（防重放）。**先验签再解析。**
2. **事件过滤**：只在**结算事件**（`transaction.completed` / `transaction.paid`）上动账；`transaction.created/updated/ready`、`subscription.*` 等 → **200 ACK 但不改订单状态**（别让非结算事件写进订单）。
3. **订单事实校验**（验签只证明"来自 Paddle"，不证明"对应本订单/本金额/本 price"；照抄 Alipay 的 `validate_alipay_notify_payload` 模式 [billing.py:620](../../gateway/billing.py)）：
   - `data.id == order.provider_order_id`
   - `data.custom_data.order_id == order.id`
   - `currency == "CNY"` 且 `total`(分) `== order.amount_cny`
   - line item 的 `price_id` ∈ 本地 `(plan_code, period) → price` 映射
   - 任一不符 → 拒绝（记审计，**不发点**）。

### 7.4 结算路径 + 只结算一次 + 退款事件（R7/R8）

- 校验通过 → 喂入**单一结算入口** `_process_payment_event`（[billing.py:686](../../gateway/billing.py)），靠 DB 唯一索引 `(provider, provider_event_id)` 幂等，成功后 `ensure_subscription_bucket` 发套餐+积分（[billing.py:841](../../gateway/billing.py)）。遵守 [[feedback_terminal_state_single_entry]]：**结算只走一个函数**。
- **R8 订单级护栏**：event_id 幂等之外，再加"订单已 `paid` 则忽略"——防同一订单不同事件类型（completed/paid）重复发点。
- **R7 退款/拒付**：`transaction.refunded` / adjustment / chargeback 事件 → 至少**安全 200-ignore**（别报错重试风暴）；并**规划积分回收**逻辑（退款后扣回已发未用点数）作为后续任务，不在 P1 静默发点。

### 7.5 订单查询兜底（R2 修订）

`GET /api/billing/orders/{order_id}`（[billing.py:188](../../gateway/billing.py)）当前**只在 `order.provider == "alipay"` 时 `refresh`**（[billing.py:207](../../gateway/billing.py)）。**必须泛化到任意 provider**（含 paddle），让成功回跳后能 `refresh=true` 查 Paddle transaction 并经 `_process_payment_event` 结算。Paddle **WeChat 延迟捕获 ~10 分钟**，这条是 webhook 迟到/丢失时前端不卡"待确认"的关键兜底。

## 8. 前端接入设计

- **现状**：[checkout-card.tsx:105](../../frontend-next/src/components/billing/checkout-card.tsx) 直接用 `default_provider` 下单；[checkout-config](../../gateway/billing.py) 的 preference/display name 还没有 paddle。
- **P1 必需**：`/paddle-checkout` Paddle.js 页面（§7.2）；`BillingStatusBanner` 支持 `provider=paddle&status=processing` 回跳态。
- **P2（R6，三轨选择）**：付款页加 provider 选择 UI（桌面微信 Native / 移动 Paddle 支付宝·卡），**可用性仍由 Gateway `checkout-config` 输出**，不在前端写死。
- 下单仍走 `POST /api/billing/orders`（[billing.py:84](../../gateway/billing.py)），body 带 `provider="paddle"`；命中升级校验（[billing.py:115](../../gateway/billing.py)）。
- 复用 CSRF 同源保护 + 生产 `is_fake_payment_enabled()` 门禁。
- **守住** [Phase2 约束](2026-04-23-phase2-r2-download-minimal.md)：前端不出现 Paddle API key / webhook secret / 内部 price id。

## 9. 配置、密钥与部署

- env：`AVT_PADDLE_ENABLED` / `AVT_PADDLE_ENV(sandbox|production)` / `AVT_PADDLE_API_KEY` / `AVT_PADDLE_WEBHOOK_SECRET` / `AVT_PADDLE_CLIENT_TOKEN`(Paddle.js 用) / `AVT_PADDLE_PRICE_{PLUS,PRO}_{M,Q,A}` / `AVT_PADDLE_NOTIFY_URL`。
- 密钥放 `/opt/aivideotrans/config/`（不入 git，`:ro` mount）；`startup_checks` 缺配置自动降级。**Wise/payout 账户信息只配在 Paddle dashboard，不写入仓库或 env。**
- gateway/前端是**镜像非 bind mount** → 改动需 rebuild image（[[deploy_experience]] 踩坑 13、5；本次部署已实战验证"覆盖 2 文件 + build next + `--no-deps` recreate"路径）。
- ⚠️ env 变更触发整 project 重插值 → 依赖容器 recreate；部署前 `psql` 检查 in-flight pipeline（[[feedback_compose_env_file_recreate]]）。只改代码不改 env 用 `docker restart`。
- **境外公网服务器对 Paddle 是优势**：webhook `https://aitrans.video/api/billing/webhooks/paddle` 可直达。

## 10. 外汇 / 结汇 / 税务（个体工商户）

- Paddle 打 **USD**。**Wise USD 账户已就绪**（ACH + SWIFT，户名孙九雄）→ payout verification 时填它。
- ⚠️ Wise 收 USD 没问题，但 **USD → 国内 RMB（结汇）** 这一段对中国大陆有限制；量大/要正规结汇凭证（开票、税务）时换 **PingPong / 连连 / Payoneer**（国内持牌跨境收款，结汇到对公账户更顺）。
- 账务性质错位：卖国内用户、收境外 MoR 的 USD，会计上属"服务出口"——提前与代账沟通。
- MoR = **Paddle 是法律卖家**：用户账单显示 Paddle；退款/拒付由 Paddle 仲裁（吸收大部分拒付责任）；拿不到原始卡号。

## 11. 合规与上线门槛（多数已完成）

- ✅ 定价页 / 隐私页 / 条款页 / **退款政策页（中英双语，已部署）** 齐全；运营主体 = 个体工商户 + sun9bear@126.com。
- ✅ 支付宝 Paddle 侧审批、Onfido 实名（孙九雄，与营业执照一致）—— 账户已激活。
- 不碰灰色方案（个人收款码 / 第四方免备案聚合）：封号风险，排除。

## 12. 测试与回归守卫（R3/R7/R8/R9 扩展）

- **验签**：合法 / 篡改 / **过期时间戳（防重放）** 三类。
- **事件过滤**：非结算事件（`transaction.updated` 等）→ 返回 200 且**不改订单**。
- **订单事实校验**：金额 / currency / price_id / `data.id` / `custom_data.order_id` 任一不符 → 拒绝、不发点。
- **幂等 + 只结算一次**：同 `provider_event_id` 重复 → 一次；订单已 `paid` 再来不同事件 → 不重复发点。
- **query_order**：pending → paid 状态翻转（模拟 WeChat 延迟捕获）。
- **退款事件**：`transaction.refunded` → 安全 200，不误发点。
- **价格漂移**：Paddle price 与 `plan_catalog` 不一致 → 启动告警 / 拒绝创建。
- **前端无泄露**：扫 `frontend-next/src/**` 不出现 Paddle key / secret / 内部 price id。
- 目标 80%+ 覆盖（[testing 规则](../../.claude/rules/ecc/python/testing.md)）。**P1 全程在 Paddle Sandbox 跑（R9）。**

## 13. 分阶段交付（P0 清单 / P1 / P2，CodeX 重构）

### P0 — 账号就绪核对清单（Paddle dashboard，多数已完成）
- [x] 账户激活（个体工商户 / 孙九雄）
- [ ] **Alipay 已单独 approved 并开启**
- [ ] **WeChat Pay 已开启**
- [ ] **6 个 CNY one-time prices 已建**（金额对齐 `plan_catalog`）
- [ ] **default payment link domain（`aitrans.video`）已 approved**（§7.2 前提）
- [ ] **notification destination 指向** `https://aitrans.video/api/billing/webhooks/paddle`，记下 webhook secret
- [ ] **API key 具备 transaction read/write**；Paddle.js client token 取好
- [x] Wise 仅配置在 Paddle payout（不写仓库/env）

### P1 — 后端 + 最小 checkout 页 + webhook 校验（Sandbox 先行）
- `payment_provider_paddle.py` + `PaddleProvider` + registry + display name；价格映射 + Paddle API 漂移校验（§6）。
- `/paddle-checkout` Paddle.js 页面（§7.2）；`BillingStatusBanner` 支持 `provider=paddle`。
- Paddle webhook 三道校验（§7.3）+ 只结算一次 + 退款安全处理（§7.4）。
- **通用 provider query refresh**（§7.5，去掉 alipay-only 限制）。
- 测试覆盖 §12 全部用例。**门槛**：Sandbox 跑通"下单 → Paddle.js 结账 → webhook 结算 → 积分到账"，且 §12 测试全绿。

### P2 — 生产小额灰度
- 真实小额：支付宝小额、银行卡小额、**Pro 年走卡**；观察一周。
- 对账一致性：`payment_orders`、`payment_webhook_events`、`billing_invoices`、`subscriptions`、credits bucket。
- **门槛**：无重复入账、无验签异常、金额/积分一致。回滚：`AVT_PADDLE_ENABLED=false`。

### P3 — 路由上线
- 前端三轨选择 UI（§8，R6）+ surface 路由（移动→Paddle 支付宝，桌面→微信 Native）。

### P4 — 出海
- 英文页 + 多语言上线后，Paddle 自动覆盖海外卡/PayPal/本地。

### 长期（可选）— ICP
- 评估另备 `.com/.cn` + 国内云做备案子域 → 直连支付宝/微信 H5+JSAPI（~0.6%），Paddle 收缩为纯海外 + 大额卡。

## 14. 风险与回滚

- Paddle 审核被退：已通过（账户 live），不再是风险；若未来风控异动，备选 FastSpring / Polar / Creem（见 §3）。
- 费率高（~7.5%）：靠路由把国内桌面留在微信 Native（0.6%）控成本；长期靠 ICP/Rail 3。
- **回滚**：`AVT_PADDLE_ENABLED=false` → provider `operational=False` → 前端不展示，秒级降级回微信 Native，结算路径不受影响。

## 15. 待确认事项

1. **checkout 形态**：`/paddle-checkout` 跳转（贴合现有 Protocol）vs inline overlay（不跳页 UX 更好）—— P1 实现前定。
2. **出海定价**：先 CNY 自动换汇（MVP）还是后续上 USD 本地化定价。
3. **退款积分回收**：退款后扣回已发未用点数的口径与实现时机（R7 后续任务）。
4. **结汇通道**：先用 Wise，量大后是否切 PingPong/连连（正规结汇凭证）。

> 已确认/已完成：平台=Paddle（live）；主体=个体工商户；退款政策=中英双语已部署；payout=Wise USD 就绪。
