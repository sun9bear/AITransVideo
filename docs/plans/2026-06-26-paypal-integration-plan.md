# PayPal 收款接入方案（国际 PayPal 钱包专轨 · USD 折算轨）

> 决策基线（2026-06-26，项目主拍板）：
> - **货币 = 独立 USD 标价（option c），后台可改**（amount_cny 仍是唯一权威账本单位；USD 价不由汇率折算，而是项目主在 admin 定价页直接设干净营销价，与 CNY 价并排、各设各的）。2026-06-26 项目主明确要"自己在后台改美元价 + 人民币价，放一个设置页"，故从初版即上 option c（不走 option b 单一汇率）。
> - **付费 API "不能静默自动调用" 硬约束不适用于 PayPal capture**：capture 是对买家已显式发起并在 PayPal 页面批准的订单完成收款，不烧用户自己账户额度。支付集成以 **①安全性 ②用户便捷性** 为最高优先级。
> - 本方案进入实施前先经评审；P1 编码 sandbox-first、`AVT_PAYPAL_ENABLED` 默认关。

---

## 审核状态（2026-06-26 对抗性审核后）

> 4-lens 对抗性审核 + 逐条回代码复核完成。**架构稳、正确复用 Paddle 模板、保住 no-fallback 红线、amount_cny 仍权威、sandbox-first；但按原稿不能开 P1**，须先修 4 个 blocker（全为方案文字层、非重构）。详见 §17。
>
> - **B1 CRITICAL（已确认）**：退款 USD 喂进 `amount_cny`(CNY 分) 比较 → 全额退误判部分退 → 跳过权益回收 → 用户全退后仍保留套餐。
> - **B2 HIGH（已确认）**：结算门 capture 时实时读 `get_price_usd`；批准窗口内 admin 改价 → 误拒 → 付了钱没升级。
> - **B3 HIGH**：return 路径"仿 fake_pay_browser"会复制"凭 order_id 结算/无事实门"的不安全语义。
> - **B4 HIGH（已确认）**：checkout-card.tsx 内联中文触发 `uiloc:cjk-guard` CI 阻断。
> - **根治 B1+B2**：建单时把应收 USD 分快照进订单 metadata，结算+退款都对快照按 USD 比，永不碰 amount_cny。
> - 本节修订已并入下文 §5/§7/§8，完整三级清单（B/S/M）见 §17。

## 0. 当前进度（2026-06-26）

- 研究稿已完成（5-agent 并行核查 developer.paypal.com 一手文档 + 货币裁决）。Orders v2 流程、Webhooks、CNY 支持三项高置信；CNY 限制角度 / 账号配置两项研究 agent 失败，结论已被货币裁决覆盖、账号配置步骤待 P0 据实操补全。
- 现有支付层已是干净的可插拔 provider 架构（微信/支付宝/Paddle/fake/stripe-stub），PayPal 走同一条接入路径。
- **凭证状态**：项目主已提供一对 **LIVE** PayPal REST 应用 Client ID + Secret（明文存于本机 `D:\Paypal\API key.txt`，不入 git）。P0 建议先申请 **sandbox** 凭证跑通全链路再上 live。

## 1. 背景与问题

多用户视频翻译/配音 SaaS 已有三条收款轨：微信 Native + 支付宝（中国买家）、Paddle MoR（国际信用卡，绕 ICP）。缺口是**偏好 PayPal 钱包的海外买家**——PayPal 在欧美渗透极高、在中国大陆基本不可用。PayPal 是直连网关（非 MoR），费率低于 Paddle MoR 加价，代价是项目自身成为 merchant of record（税务自理，海外数字服务首版可延后）。

核心难点两条：
1. **货币**：整个结算核心以 CNY 分（fen）为中心（`amount_cny` 列、`plan_catalog` fen 价、各 provider 事实门校验 CNY）。PayPal 主要面向海外/美元。
2. **Capture 模型**：PayPal Orders v2 与微信/Paddle（自动 capture）不同，买家批准后必须服务端显式 capture 才到账。

## 2. 核心思路：USD 折算轨 + 复用 Paddle 模板

- **USD 折算轨**：`amount_cny` 不动，是唯一权威账本/权益单位。新增一层"按 admin 配置汇率把 amount_cny 折算成 USD 去 PayPal 收款"。`_process_payment_event`、subscriptions、credits、退款回收**零改动**（它们只读 amount_cny）。实收 USD + 当时汇率落 `metadata_json` 供对账。
- **复用 Paddle 适配器模板，而非微信/支付宝**：Paddle 早已**不**断言 `total == amount_cny`（MoR 含税/FX），改用"订单身份 + 货币事实门"。PayPal-USD 结构同形：实收金额永远不等于 CNY fen，所以绑**订单身份 + 货币门(USD) + 期望 USD 金额**，而不是与 CNY fen 相等。

## 3. 决策记录

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | 收款货币 | **独立 USD 标价（option c），后台可改** | CNY 在 PayPal "仅限境内账户"且未证实境外可建单/买家按 CNY 扣；USD 普适、海外买家便捷、复用 Paddle 模板。项目主要直接管 USD 价（干净营销价 $14.99 而非汇率折算的 $13.86）→ 一步到位 option c |
| D2 | amount_cny 角色 | **不变，唯一权威单位** | 结算核心/订阅/credits/退款零改动；CNY 与 USD 价独立、互不联动 |
| D3 | Capture 模型 | **回跳即 capture + Webhook 为真相 + APPROVED 兜底** | 便捷(秒级确认)+安全(webhook 权威)+不丢单 |
| D4 | 验签 | **首版在线 `verify-webhook-signature` API** | 标准、最低出错风险；离线 CRC32+证书链作后续优化 |
| D5 | provider 间 fallback | **禁止自动 fallback** | CLAUDE.md 硬约束；PayPal 必须用户显式选择 |
| D6 | 前端凭证 | **前端零 PayPal 凭证** | PayPal 无 browser-safe public token（不同于 Paddle client token），前端不放任何 PayPal 值 |
| D7 | 默认开关 | `AVT_PAYPAL_ENABLED=false`，sandbox-first | 与 Paddle/微信一致的安全姿态 |
| D8 | USD 价管理 | **复用现有运行时定价基建**（pricing_runtime/pricing_admin），admin 定价页加 USD 字段，与 CNY 并排 | 项目已有草稿→校验→发布→快照+历史+跨进程缓存失效；USD 作平行字段零新基建 |

## 4. 四轨混合架构

```
                       checkout-config (gateway 决定可用性 + 推荐)
                                  │
  ┌───────────────┬──────────────┼───────────────┬──────────────┐
  微信 Native      支付宝          Paddle(MoR)      PayPal(本方案)   fake(dev)
  中国/扫码        中国/直连       国际/信用卡      海外/PayPal钱包   测试
  CNY 严格金额门   CNY 严格金额门  CNY 货币+价格门   USD 货币+金额门   —
```

- **路由：按地区推荐（geo-based，2026-06-26 项目主定）**——国内（`Cf-Ipcountry==CN`）推荐**微信**、海外推荐 **PayPal**、**Paddle 两地区共同手动备选**。详见 §7.5。旧的"桌面/移动设备型号"路由被地区路由取代。
- **合规底线（不可破）**：geo 只决定**推荐/预选 + 显示哪些渠道**；用户在选择器里**显式选**，失败时**用户手动改选**，系统**绝不**自动 fallback 到 Paddle 重新下单（CLAUDE.md 硬约束 + 本 plan no-fallback 不变量）。
- PayPal `display_mode = "redirect"`，复用现有 `window.location.assign(checkout_url)`，**无需** Paddle 那种 `/paddle-checkout` 中转页（PayPal 直接跳它自己的托管批准页）。

## 5. 货币与定价设计（独立 USD 标价，后台可改）

USD 价**不由汇率折算**，由项目主在 admin 定价页直接设（与 CNY 价并排、各设各的），复用现有运行时定价基建：

**现有基建（CNY 已可后台改）：**
- `gateway/pricing_admin.py`：admin 端点 `GET /api/admin/pricing`、草稿/校验、`POST .../publish`（写快照）、`GET .../history`；带 `detect_frozen_field_changes` 冻结字段改动检测。
- `gateway/pricing_runtime.py`：发布写 `pricing_runtime.json`，文件 mtime 跨进程缓存失效，所有 gateway 进程下次读价立即生效。
- `gateway/pricing_schema.py`：`PlanConfig.price_cny_fen: PlanPriceConfig{monthly,quarterly,annual}`（CNY 分）。

**USD 接入（平行字段；schema/admin/runtime 层零破坏，但需贯通整条数据链 — S1 纠正"零新基建"）：**
- `pricing_schema.PlanConfig` 加 `price_usd_cents: PlanPriceConfig | None = None`（复用 monthly/quarterly/annual 结构，单位=美分；可选，**不破坏现有 on-disk `pricing_runtime.json` 的 `model_validate`**，现有 pricing 测试不挂）。
- **数据链必须全程贯通（S1：`plan_catalog` 不直接读 `PlanConfig`）**：`_get_runtime_plans()` 把 `PlanConfig→PlanDefinition`（其 `PlanPrice` 只有 CNY-fen 字段）。故除 schema 外还须改 `PlanPrice`/`PlanDefinition` dataclass + `_get_runtime_plans` mapper + `build_default_pricing_payload` + `_plan_to_public_dict`(/api/plans)，`get_price_usd` 才有值可读。仅加 schema 字段会让 `get_price_usd` 返 None → 建单失败。
- `plan_catalog` 加 `get_price_usd(plan_code, billing_period) -> int | None`（美分，runtime-aware），加单测：发布带 USD 的 runtime payload 后断言 `get_price_usd` 读得到（证明桥接通）。
- `build_default_pricing_payload` 种 §5.1 的 USD 默认（admin 后台可改）。
- `pricing_admin.py` 发布流程自动跟随（整体 `PricingPayload.model_validate`+写快照），无需新端点；**但** USD 价默认不在 `detect_frozen_field_changes` 覆盖内（只覆盖 `price_cny_fen`/debit/trial）——鉴于 B2 mid-flight 改价有害，建议把 `plans.*.price_usd_cents` 纳入 frozen-field 审计（M6）。
- 废弃字段 `CostModelConfig.fx_usd_cny=7.0`（admin UI 标"美元兑人民币汇率"、零消费方）会与本独立 USD 模型冲突造成运营困惑——移除/隐藏或加注说明（M2）。

**PayPal 用法（含 B1/B2 根治：USD 快照）：**
- 建单 `value = round(usd_cents/100, 2)`，**直接用配置 USD 价**（无汇率 env）。
- **建单时把应收 USD 分快照进订单**：`order.metadata_json["paypal_expected_usd_cents"] = usd_cents`（B2）。这是结算/退款唯一的 USD 比较基准——immutable 化，等价于 Paddle 的 price_id 不可变性。
- 结算事实门：期望 USD = **订单快照 `paypal_expected_usd_cents`**（**不是** capture 时实时 `get_price_usd`，否则 admin 改价误拒，B2）；校验 `实收 USD == 快照`，容差**只允许极小上浮**（容 PayPal 买家侧换汇行）、**严禁下浮**（防少付，underpayment hole）。
- 落库：capture 后 `metadata_json` 补 `{"paypal_charged_usd":"x.xx","paypal_capture_id":"..."}`，CNY-fen 账本 + USD 实收双记供对账（并入 admin 对账面板，见 §17 M5）。
- **退款按 USD 比，不碰 amount_cny（B1）**：partial-vs-full 退款判定必须用 `退款 USD 分 vs paypal_expected_usd_cents`，**禁止**走 `_process_payment_event` 里 `refund_fen < order.amount_cny` 那条 CNY 路径（USD 分 < CNY 分会把全额退误判成部分退、跳过权益回收）。实现见 §7.3。
- `amount_cny` 仍是订单的权威 CNY 价（账本/订阅/credits 全读它）；CNY 与 USD 解耦，改 CNY 不自动改 USD。

**前端 UI 提示**：UI 现示 ¥99，PayPal 实扣 $X；选 PayPal 时前端读 `/api/plans` 的 `price_usd_cents` 展示"≈ $X，PayPal 按美元结算"（USD 价由后端回传，不在前端折算）。

### 5.1 建议 USD 默认价（seed 进 `build_default_pricing_payload`，admin 可改）

定价原则：PayPal 跨境收款成本远高于国内（国内微信/支付宝 ~0.6%）。每笔叠加 **PayPal 跨境收款费 ~4.4%+$0.30 + USD→CNY 提现换汇点差 ~3–4% + 退款/拒付/汇率缓冲 ~1–2% ≈ 合计 ~9%+$0.30~0.50**。目标：每笔到账 CNY ≥ 对应人民币标价（海外买家承担 PayPal 溢价，商户净收不低于国内同档）。中间价口径 1 USD ≈ 7.1 CNY。

| 套餐 | 周期 | 人民币现价 | **建议 USD 价** | 折算到账 ¥（≈价×0.91×7.1−¥2）| 对比 ¥ 现价 |
|---|---|---|---|---|---|
| **Plus** | 月 | ¥99 | **$16.99** | ≈ ¥108 | +9% |
| | 季 | ¥269 | **$44.99** | ≈ ¥289 | +7% |
| | 年 | ¥999 | **$159.99** | ≈ ¥1032 | +3% |
| **Pro** | 月 | ¥299 | **$49.99** | ≈ ¥321 | +7% |
| | 季 | ¥799 | **$129.99** | ≈ ¥838 | +5% |
| | 年 | ¥2999 | **$469.99** | ≈ ¥3035 | +1% |

每档到账 CNY 均 ≥ 人民币现价，PayPal 溢价全由海外买家承担。USD 价比 CNY 折算价高 ~12–18%，符合 SaaS 地域差异化定价惯例（海外买家几乎看不到 CNY 价）。
注：以上 .99 营销价，admin 定价页可随时调整；费率是账户/地区相关估值，上线后拿 PayPal 实际账单费率再校一轮；优先在 UI 引导 PayPal 买家走年付（摊薄固定费 + 降 churn）。

## 6. PayPal 产品 / API 映射（已核实，来源 developer.paypal.com）

| 步骤 | 端点 / 字段 | 要点 |
|---|---|---|
| OAuth token | `POST /v1/oauth2/token`，`grant_type=client_credentials`，Basic(client_id:secret) | `expires_in`≈8.8h，**按 expires_in 缓存复用**，仅服务端内存 |
| Base URL | sandbox `api-m.sandbox.paypal.com` / live `api-m.paypal.com` | 买家批准页是 `www.paypal.com`（不同主机） |
| 建单 | `POST /v2/checkout/orders`，`intent=CAPTURE` | `purchase_units[].amount.{currency_code:"USD",value:"x.xx"}` + `custom_id=我方order_id`(≤255) |
| 回跳流 | `payment_source.paypal.experience_context.{return_url,cancel_url,user_action=PAY_NOW}` | 返回 `status=PAYER_ACTION_REQUIRED` + `links[rel="payer-action"]`（按 rel 选，**不**按数组下标） |
| Capture | `POST /v2/checkout/orders/{id}/capture` | → `COMPLETED`，capture id 在 `purchase_units[].payments.captures[].id` |
| 幂等 | `PayPal-Request-Id` 头（=order_id） | 重复 capture 返回同一结果，不双扣 |
| Webhook 事件 | `PAYMENT.CAPTURE.COMPLETED`(权威到账) / `.DENIED` / `.REFUNDED` / `.REVERSED` / `CHECKOUT.ORDER.APPROVED`(兜底 capture 触发) | Envelope `resource.{id,status,amount.{currency_code,value},custom_id}` |
| Webhook 验签 | `POST /v1/notifications/verify-webhook-signature` | body: `auth_algo,cert_url,transmission_id,transmission_sig,transmission_time,webhook_id,webhook_event`；对应头 `PAYPAL-AUTH-ALGO/CERT-URL/TRANSMISSION-ID/-SIG/-TIME` |
| ACK | 必须 HTTP 2xx | 否则 3 天内重投至多 25 次 |
| webhook_id | 后台 Apps&Credentials→Webhooks 或 `POST /v1/notifications/webhooks` | live 与 sandbox 各一份，注入 env |

## 7. 后端接入设计（贴合现有抽象，结算入口零改动）

### 7.1 新增 `gateway/payment_provider_paypal.py`（仿 `payment_provider_paddle.py`）

`@dataclass(frozen=True) PayPalConfig`（`from_env`：`AVT_PAYPAL_ENABLED/ENV/CLIENT_ID/SECRET/WEBHOOK_ID/RETURN_URL/CANCEL_URL/CNY_TO_USD_RATE`，缺 client_id/secret/webhook_id 即返 None）+：
- `_get_access_token(config)` — OAuth client_credentials，按 expires_in 进程内缓存（带安全余量），线程安全。
- `create_order(config, *, order_id, target_plan_code, billing_period) -> (checkout_url, paypal_order_id, expected_usd_cents)` — 读 `plan_catalog.get_price_usd(plan,period)` 得美分、`value=usd_cents/100`、`POST /v2/checkout/orders`、取 `links[rel="payer-action"]`。**回传 `expected_usd_cents` 供调用方快照进订单 metadata（B2）**。USD 价未配置或空链接抛 ValueError。
- `capture_order(config, *, paypal_order_id, order_id) -> CaptureResult` — `POST .../capture`，`PayPal-Request-Id=order_id` 幂等。
- `verify_paypal_signature(config, raw_body, headers) -> bool` — 在线 verify-webhook-signature。
- `parse_paypal_webhook(raw_body) -> ParsedPayPalWebhook` — 规整 envelope，`order_id=resource.custom_id`。
- `map_paypal_event_type` / `map_paypal_order_status`（CAPTURE.COMPLETED→paid，REFUNDED→refunded，**REVERSED→refunded（拒付，S4，否则拒付后用户保留套餐）**，DENIED→failed，APPROVED→approved-待 capture）。
- `query_order(config, *, paypal_order_id) -> QueryResult` — `GET /v2/checkout/orders/{id}`（**纯读**，只返状态；capture 动作不在此，由 billing.py paypal 分支调 `capture_order`，见 §7.4 / S2）。
- `validate_paypal_webhook_payload(config, resource, *, order_id, expected_usd_cents, provider_order_id)` — **USD 锚定事实门**：`custom_id==order_id`、`resource.id`/order 绑 `provider_order_id`、`currency_code=="USD"` 硬门、`实收 USD == expected_usd_cents`（容差只上浮不下浮）。**注意签名收的是订单快照 `expected_usd_cents` 而非 amount_cny（B2）**。
- `is_paypal_live_ready()` — enabled 且 config 完整 **且对应套餐 USD 价已在 runtime 发布**（仿 `is_paddle_live_ready` 的"6 价齐全才 operational"，S3，否则 PayPal 显示可用但建单 502）。`AVT_PAYPAL_WEBHOOK_ID` 作硬门（M3）。

### 7.2 `gateway/payment_providers.py`

- 新增 `class PayPalProvider`（仿 `PaddleProvider`，:362）：`name="paypal"`、`operational`→`is_paypal_live_ready`、`create_checkout`→`create_order`、`verify_signature`/`parse_webhook`/`map_status`/`query_order` 委托模块。
- `_init_registry()`(:484) 注册 `"paypal": PayPalProvider()`。（现有 `StripeProvider` 桩 :158 保留或同位替换思路。）

### 7.3 Webhook 校验：验签 + 订单事实校验 + 事件过滤（三道缺一不可）

`gateway/billing.py:receive_webhook`(:688) 加 paypal 分支：
- `_validate_paypal_event_against_order`（仿 `_validate_paddle_event_against_order`:973）：取 `resource` 走 USD 事实门；order 不存在按现有 INVARIANT 返 True（记录 no-op，安全 200-ACK）。
- 退款（**B1 根治 + S4/S5**）：`_is_refund_resource_event`(:857) 加 `PAYMENT.CAPTURE.REFUNDED` **和 `PAYMENT.CAPTURE.REVERSED`（拒付，S4）**；`_resolve_refund_order_id`(:826) 用 `resource.custom_id` 直接绑 order（PayPal 退款 envelope 自带 custom_id；capture-id 反查仅作 custom_id 缺失兜底，S5）。
- **退款金额比较走 USD 专用路径，不复用 `_extract_refund_amount_fen`→`amount_cny` 那条（B1 CRITICAL）**：`_process_payment_event` 现有 `is_known_partial_refund = refund_fen < order.amount_cny` 是 CNY 分比较，喂 USD 分必误判（全额退误判部分退→跳过权益回收→用户保留套餐）。改法二选一（实现时定，须带回归测试）：① PayPal 分支让 `_extract_refund_amount_fen` 返 `None`（按全额处理、触发回收，PayPal 部分退罕见且多为 owner 手动），partial 判定另置 USD 专用 gate 比 `退款 USD vs metadata.paypal_expected_usd_cents`；② 给 `_process_payment_event` 加按订单货币分流的 USD 比较分支，比 `metadata.paypal_expected_usd_cents` 而非 `amount_cny`。退款总额存 USD-keyed metadata 字段（不复用 `refund_amount_fen_total`）。
- 回归测试（必加）：PayPal 全额退 → 触发权益回收；部分退 → 不回收；REVERSED 拒付 → 触发回收。

### 7.4 结算路径 + 只结算一次 + Capture 模型（D3）

```
建单 → PAYER_ACTION_REQUIRED → 前端跳 payer-action URL → 买家批准 → 回跳 return_url
 ├─【顺路径】GET /api/billing/paypal/return → 服务端 capture → COMPLETED → _process_payment_event(paid) → 303 /settings/billing?status=paid
 ├─【真相】Webhook PAYMENT.CAPTURE.COMPLETED 验签+事实门 → _process_payment_event(paid)（幂等：(provider,event_id)唯一键 + 终态行锁）
 └─【兜底】买家批准后关页 → _refresh_order_from_provider/对账 sweeper 查到 APPROVED → 自动 capture（D3 已授权）
```

- `_process_payment_event`(:1102) **完全复用**，provider-agnostic、幂等、终态行锁不变。
- 新增 `GET /api/billing/paypal/return`（**B3：不复用 `fake_pay_browser` 的"凭 order_id 结算/`signature_valid=True` 硬编/无事实门"语义**）：回跳后**用订单存储的 `provider_order_id`（不是浏览器 `token`）** 调 `capture_order` → **对 capture 响应跑 `validate_paypal_webhook_payload`（USD 事实门）→ 仅事实门通过才传 `signature_valid=True` 给 `_process_payment_event`**；303 回 `/settings/billing`，永不抛 404/409 给浏览器。**不需要**校验调用者会话归属（webhook/return 是 provider 回调，三个现有 provider 均不查归属——复核明确这是错的控制）；防重放由终态守卫 + (provider,event_id) 幂等保证。
- Capture 动作只在 billing.py paypal 分支发生（`capture_order`，`PayPal-Request-Id=order_id` 幂等），**不塞进只读的 `query_order`/`map_status`**（S2）。跨入口（return/webhook/sweeper）去重靠现有订单行 `FOR UPDATE` 锁 + 终态守卫（与 Paddle query-vs-webhook 同款，event_id 不同无妨）。
- `_refresh_order_from_provider`(:280) paypal 分支：query order，若 `APPROVED` 则调 `capture_order`（user-poll / sweeper 触发，便捷+不丢单）。**点名兜底 sweeper = 现有 `billing_reconciliation`**（`main.py:474-478`，300s，重查 created/pending 单），它同时覆盖"已 capture 但验签 API 当时宕机未结算"的恢复（S7/S8）。
- 取消/弃单（S6）：`AVT_PAYPAL_CANCEL_URL` 回跳后把订单标 `cancelled`，前端 banner 加 `cancelled` 态；从不批准的弃单由 sweeper/过期清理（§14 M4）。

### 7.5 显示名 + 按地区路由的 checkout-config

- `_PROVIDER_DISPLAY_NAMES`(:395) 加 `"paypal": "PayPal"`。
- `get_checkout_config`(billing.py:408-488) 把 `recommended_provider` 的来源从**设备型号**（现 `surface_preference=["wechatpay","paddle"]`，:477）改成**地区**。

**地区信号**：在 `get_checkout_config` 里读 `request.headers.get("cf-ipcountry", "").upper()`（Starlette Headers 大小写不敏感；**不要**移植 traffic_analytics 的 `_first_header`——那是给 Caddy 访问日志 dict 用的，G-LOW-1）。`request` 可为 `None`（现签名 `request: Request = None`，default_provider 测试就传 None）→ 必须 None-guard，复用 :475 的 `if request is not None`。
> ⚠️ **Cf-Ipcountry 活路径待 P0/P1 验证（G-INFRA）**：`traffic_analytics.py` 读的是 **Caddy 访问日志行**里的 Cf-Ipcountry，**不是**活请求头；gateway 现无任何代码从活 `request.headers` 读它。生产拓扑（全量 CF 前置 + loopback gateway + Caddy 默认透传 header）使其**极可能**可用，但**必须 P0/P1 实测确认**（prod 临时打一行 `request.headers.get("cf-ipcountry")` 日志 / 经隧道 curl），不能当已证明。下面的 fail-open 保证即使该头永不到达也不挡支付。

**推荐 + 可见渠道（含两个已确认产品决策）：**

| 地区（`Cf-Ipcountry`）| 推荐/预选 | 选择器显示（**决策①：过滤跨区不可用**）|
|---|---|---|
| `CN`（国内）| **wechatpay** | wechatpay + paddle（**隐藏 paypal**，国内基本不可用）|
| 其它（海外）| **paypal** | paypal + paddle（**隐藏 wechatpay**，海外基本不可用）|
| **缺失/UNKNOWN**（决策②：直连/VPN/非 CF）| **paddle**（通用）| 全部 operational 渠道，**fail-open 不挡任何人** |

**两段式构造（G-HIGH-1/G-HIGH-2 修正——geo 过滤绝不能污染 `default_provider`，且 `recommended_provider` 必须在可见列表内）：**
1. **先建全量 `providers_payload`（不过滤）**，`default_provider` 仍按现 :462-465 "首个 operational" 从**全量**列表导出 → **历史语义不变**（保护不关心 geo 的既有调用方；G-HIGH-1：若在被过滤的列表上导出会让 default 静默漂移）。
2. **再按地区过滤出 `visible` 列表**（CN 略过 paypal、海外略过 wechatpay）——这是叠加在全局 `is_provider_operational` 之上的**可见性**层，**不改** operational 真值（核验确认这点正确）。
3. **`recommended_provider` 从 `visible`（已过滤）集计算**，fail-open 回退到 visible 里任一 operational。**硬不变量：`recommended_provider ∈ visible providers`（G-HIGH-2，load-bearing）**——否则前端 `providers.find(recommended)` 返 `undefined` → CTA 误标 fallback "测试支付"且 `operational ?? true` 仍可点 → 给一个 UI 从未展示的渠道下真单。
4. **`providers` 字段只 emit `visible` 列表**（前端 `operationalProviders` 据此渲染选择器）。
- **决策①过滤**：见上 step 2/4。前端"≥2 operational 显示选择器、只剩 1 个直接用"逻辑不变即生效——但**仅在不变量 §3 成立时**（单轨无选择器路径也靠它才安全，G-LOW-2）。
- **决策②兜底 + None-guard**：`request is None` **或** `Cf-Ipcountry` 空/`UNKNOWN`/`XX` → 走 fail-open：**不过滤**、`visible`=全部 operational、推荐 `paddle`（G-MEDIUM-1）。
- **不变量：geo 过滤绝不滤到 0 可见渠道**。若某区过滤后 `visible` 为空（如海外但 paypal+paddle 都未配置、只剩 wechat operational 却被隐藏）→ **回退 `visible`=全部 operational**（宁可显示一个跨区渠道也不让用户无法支付）。
- **paddle 始终是两区手动备选**：国内想用卡、海外没 PayPal 账户、geo 误判（IP 定位对旅行/VPN 用户可能错）时，Paddle 是通用安全网。
- **合规（核验为干净）**：以上只动"推荐 + 可见性"；`create_order` 仍用客户端显式 `body.provider`(:169)，provider 失败 → rollback + 502、**无切换**(:200-207)，无任何自动 provider fallback（红线保住）。
- **货币提示**：wechatpay/paddle=CNY、paypal=USD，金额按 provider 各自展示（§5/§8）。
- 前端：选择器主逻辑无需改；但 **hint map(:310-315) 必须加 paypal 分支 + USD notice（G-MEDIUM-2，非可选）**，经 message key（B4），顺手把现有 wechatpay/paddle 内联 CJK hint 一并迁 key、bootstrap `billing` 命名空间。可选硬化：`providerEntry` 为 `undefined` 时 `operational ?? false`（让幻影推荐**禁用**而非启用 CTA）+ 回退 selectedProvider 到首个 operational。
- **测试（必含，G-HIGH-3）**：① 新增 `Cf-Ipcountry`=CN→wechatpay / 海外→paypal / 缺失→paddle 三例，每例断言 `recommended_provider ∈ visible`；② **更新/删除现有 `test_wechatpay_is_recommended_on_mobile_and_desktop`(test_billing.py:838-860)**——它不带 Cf-Ipcountry，新规则下 recommended 由 wechatpay 翻成 paddle 会红；③ 若设备路由(`surface_preference`)被地区路由完全取代，退役其死代码；④ 保留 4-key shape-lock(:812-817) 与 request=None 既有 default_provider 测试绿。

## 8. 前端接入设计

- **B4：前端新串必须走 next-intl message key，禁内联 CJK**。`checkout-card.tsx` 在 `(app)/admin/**` 和 `workspace/[jobId]/edit/**` 之外，**不被 `uiloc:cjk-guard` 豁免**，加内联中文（如 `"PayPal 钱包/国际支付"`、`"≈ $X，PayPal 按美元结算"`）会触发 CI + pre-commit 阻断合并。改为 message key（如 `billing.checkout.providerHint.paypal`、`billing.checkout.paypalUsdNotice`），与 uiloc 同源；§12 加"跑 `npm run uiloc:cjk-guard`"任务。PayPal 是 `redirect` → 复用 `window.location.assign(result.checkout_url)`，无新中转页。
- USD 提示：选 PayPal 时显示"≈ $X，按美元结算"（**经 message key**），$X 由后端 `/api/plans` 的 `price_usd_cents` 回传，**汇率/价格逻辑不进前端**。
- **M1：admin USD 价 UI 是真前端工作（非"零基建"）**——`admin/pricing/page.tsx`（在 admin/** 豁免区，无 cjk 约束）的 `PlansEditor`/`PlansDisplay` 是手写 CNY 专用 markup，需加 USD 输入/展示行 + TS `PlanConfig` USD 类型 + `billing/types.ts` `Plan.price_usd`。
- `billing-status-banner.tsx`：复用现有 `status=processing/paid/closed` 轮询 + 加 `cancelled` 态（S6）；redirect 的 `status=` 参数仅显示提示，banner 必经 `GET /orders/{id}` 取服务端真相（M7）。
- `get-checkout-config.ts` / `create-order.ts` 类型基本通用；`/api/plans` 消费侧加 `price_usd_cents`（S1/M1）。

## 9. 配置、密钥与部署

`.env.example` 新增（仿 `AVT_PADDLE_*`:243-272）：
```
AVT_PAYPAL_ENABLED=false
AVT_PAYPAL_ENV=sandbox            # sandbox | live
AVT_PAYPAL_CLIENT_ID=
AVT_PAYPAL_SECRET=
AVT_PAYPAL_WEBHOOK_ID=
AVT_PAYPAL_RETURN_URL=https://aitrans.video/api/billing/paypal/return
AVT_PAYPAL_CANCEL_URL=https://aitrans.video/settings/billing?provider=paypal&status=cancelled
# USD 价不在 env —— 由 admin 定价页管理（pricing_runtime.json 的 price_usd_cents），无汇率 env
# 前端：无 NEXT_PUBLIC_PAYPAL_* —— PayPal 无 browser-safe token，前端零凭证
```
- `startup_checks.validate_paypal_config`（缺配置 CRITICAL+降级 enabled=False，仿 `validate_mainland_voice_worker_config`），main.py lifespan(:186-199) 接线。
- 部署：gateway 是镜像非 bind mount，改动后 rebuild image；LIVE 凭证经 SFTP 注入 US `/opt/aivideotrans/config/.env`（不进命令行/git），`up -d --force-recreate gateway`；env 变更前先 psql 查 in-flight pipeline。

## 10. 外汇 / 结汇 / 税务

- 收 USD：到账 USD，结汇按个体工商户既有通道。MoR 责任由 PayPal 直连模式落到本项目（首版海外数字服务税务延后，需 owner 知情）。
- 与 Paddle（MoR 代缴税）的差异：PayPal 直连费率更低但税务自理——这是选 PayPal 的取舍。

## 11. 安全清单（全程严守）

- [ ] Secret 仅服务端 `.env`；前端零 PayPal 凭证 + 新增 `test_paypal_frontend_no_leakage.py` 守卫
- [ ] Webhook 先验签（在线 API）再处理；复用 64KiB body 门 + `(provider,event_id)` 幂等 + 终态行锁
- [ ] 事实门：`custom_id` 绑 order_id + `currency_code=="USD"` 硬门 + 实收 USD≈期望 USD + PayPal order id==`provider_order_id`
- [ ] OAuth token 仅服务端缓存；capture 用 `PayPal-Request-Id` 幂等
- [ ] **provider 间不自动 fallback**——PayPal 必须用户显式选择
- [ ] return 端点对浏览器永不 4xx/5xx，始终 303 带 status
- [ ] 退款门按实收 USD 比对（禁止 amount_cny fen）

## 12. 测试与回归守卫

- `tests/test_payment_provider_paypal.py`：OAuth 缓存、建单折算 USD、payer-action 链接选取、capture 幂等、验签（mock verify API）、事实门（custom_id/currency/金额容差/身份）、退款金额提取、APPROVED 兜底 capture。
- `tests/test_paypal_frontend_no_leakage.py`：前端禁现 client_id/secret 任何片段（仿 `test_paddle_frontend_no_leakage.py`）。
- `tests/test_billing.py` 扩展：paypal webhook → 结算/退款幂等、终态守卫、未验签不结算。

## 13. 分阶段交付

### P0 — 账号就绪 + sandbox（PayPal 后台，零代码风险）
- 申请 sandbox 凭证；后台建 Webhook 订阅（listener=`/api/billing/webhooks/paypal`）拿 sandbox + live `webhook_id`；订阅事件 `PAYMENT.CAPTURE.COMPLETED/.DENIED/.REFUNDED/.REVERSED` + `CHECKOUT.ORDER.APPROVED`。
- sandbox 手动跑通 建单→批准→capture→webhook 全链路（确认 CNY 不可建单的疑虑无关——USD 轨绕开）。
- 确认 LIVE 应用已启用接收支付能力 + 商业账户就绪。

### P1 — 后端地基（Sandbox 先行，默认关）
- `payment_provider_paypal.py` + `PayPalProvider` 注册 + 结算门 + return 端点 + startup 校验 + 测试。全程 `AVT_PAYPAL_ENABLED=false`、sandbox-first。

### P2 — 前端
- CheckoutCard provider 选项 + USD 提示 + banner 文案 + leakage 守卫。

### P3 — 生产灰度上线
- 合并 main → Via-154 部署 → 注入 LIVE 凭证 + `webhook_id` → **在 admin 定价页发布各套餐 USD 价（强制前置，否则建单失败）** → 翻 `AVT_PAYPAL_ENABLED=true` → 真金小额 e2e（owner 付，不能代付）。

## 14. 风险与回滚

- **回滚**：`.env` 设 `AVT_PAYPAL_ENABLED=false` + `up -d --force-recreate gateway`，其它收款轨不受影响。
- **USD 价未设即开 PayPal**：`get_price_usd` 返 None → `create_order` 抛 ValueError → 用户看到建单失败。上线门槛：启用 PayPal 前必须先在 admin 定价页发布各套餐 USD 价（P3 checklist 强制项）。
- **CNY/USD 独立漂移**：两价各设各的，改 CNY 不动 USD（设计如此）；项目主需自行保持两者商业一致性，无自动联动（这正是 option c 取代汇率折算换来的可控性）。
- **退款/部分退款门（B1，已解）**：必须按实收 USD 比订单快照 `paypal_expected_usd_cents`，**禁止**喂进 `amount_cny`(CNY 分) 比较 → 否则全额退误判部分退、跳过权益回收（详见 §7.3）。
- **价格漂移误拒（B2，已解）**：结算门比订单**建单时快照**的 USD，不比 capture 时实时 `get_price_usd`（详见 §5/§7.1）。
- **金额相等门陷阱**：禁止照搬微信 `total==amount_cny fen`（USD 永不等于 CNY fen → PayPal 永不结算）；必须仿 Paddle（身份+货币+期望 USD），且容差只上浮不下浮（防少付）。
- **对账歧义**：billing_invoices/admin 对账面是 CNY-fen，USD 实收 != amount_cny → 必须 metadata 双记（amount_cny 账本 + `paypal_charged_usd` 实收）并 surface 到 admin 面板（M5），否则读成可疑。

## 15. 待确认事项

1. P0 sandbox 实测确认：USD 建单→capture→webhook 全绿；（可选）CNY 建单是否被拒以彻底坐实 USD 选择。
2. ~~checkout-config 中 paypal 排序位置~~（已由 §7.5 geo 路由定：CN→微信、海外→PayPal、Paddle 两区备选）。待确认：生产环境 `Cf-Ipcountry` 确经 Caddy 转发到 gateway（traffic_analytics 已用，强信号但 P0 抓包确认）；非 CF 路径/dev 走 fail-open。
3. 前端 USD 提示文案与是否由后端回传 USD 概览（避免汇率下放前端）。
4. 税务/MoR 责任 owner 知情确认（PayPal 直连=本项目自任 MoR）。
5. 研究缺口：账号配置研究 agent 失败 → P0 据实操补全后台建 webhook 步骤。

## 16. 与并行方案的协调（2026-06-26）

仓库现高度并行（20+ worktree）。PayPal 与在飞工作**无架构性冲突**（关注点正交），但有 3 处文件级/约定协调点：

- **多语言（uiloc，分支 `uiloc/marketing-en-seo-b`，已暂停）**：i18n 基座（`messages/{zh,en}/*`、`src/i18n/*`、`useTranslations`）+ `uiloc:cjk-guard` **均已在 main**（UI-01/02 合并）；尚无 `billing` 命名空间，`checkout-card.tsx` 现仍内联 CJK（baseline grandfather），其 key 迁移属暂停中的 UI-06。**排序（项目主定，2026-06-26）：PayPal 先做完合并 → uiloc 再恢复**，这是更优顺序：PayPal 用 message key 落账单 UI（顺手 bootstrap `billing` 命名空间），uiloc 恢复时 UI-06 直接把 PayPal 已 key 化的串扫进英文翻译，**纯增量零返工**。暂停消除的是并行改 `checkout-card.tsx`/消息目录的冲突；cjk-guard 仍在 main 故 PayPal **必须**用 key（=B4，不因暂停而豁免）。uiloc 恢复者须知 `billing` 命名空间已由 PayPal 建好，在其上扩展而非重建。
- **代码质量方案**：ruff 是 report-only + 只阻断改动文件（非全仓 format 横扫），结构重构目标是 `process.py`/`job_intercept.py` 非 billing；方案明确 auth/billing/payment **增量迁移不做大爆炸替换**。对 PayPal 是护栏非冲突：新代码从一开始 ruff/mypy clean、logger 不 print、新文件 <800 行（`payment_provider_paypal.py` 控制在 ~400 行内；billing.py 是既存超标白名单，增量编辑允许）。
- **billing.py 并发（已核实，2026-06-26）**：`codex/sync-04-billing-ops-hardening-pr` 与 `codex/wechat-qr-mobile` **均已 squash 合并进 main**（前者产物 `billing_reconciliation.py` + R7 退款闭环 + admin unsettled + 2026-06-13 Codex 行锁/adjustment.updated 修复都在 main；后者是微信 QR 弹窗前端 CSS，居中改动已在 main 且不碰 billing.py，与 redirect 式 PayPal 零交集），对应 worktree 是 squash 后的陈旧残留，**不是并发冲突源**。**sync-04 在 main 反而利好**：§7.4 复用的 `billing_reconciliation` sweeper 正因它在 main 才成立。措施：PayPal 仍在独立 worktree+分支 `claude/paypal-integration` 从最新 main 切出；上线前 `git log` 查是否有**仍在活跃开发且改 billing.py** 的分支（而非假定上述两个），有则排序 rebase。

## 17. 对抗性审核发现与修订清单（2026-06-26）

4-lens（安全/财务/架构/完整性）26 发现 + 8 条 HIGH/CRITICAL 逐条回代码对抗性复核。**总评：架构稳、正确复用 Paddle 模板、保 no-fallback 红线、amount_cny 仍权威、sandbox-first；但须先修 B1–B4 再开 P1**（全为方案文字层、非重构）。

### Blocker（开 P1 前必修；已并入上文）
- **B1 CRITICAL（确认）退款单位错配**：PayPal 退款 USD 喂进 `is_known_partial_refund = refund_fen < order.amount_cny`(CNY 分) → 全额退误判部分退 → 跳过 `_recall_entitlements_for_refund` → 用户全退后保留套餐。修：退款按 USD 比 `metadata.paypal_expected_usd_cents`，不碰 amount_cny（§5/§7.3）。
- **B2 HIGH（确认）价格漂移误拒**：结算门 capture 时实时读 `get_price_usd`（可被 admin 改）→ 批准窗口内改价 → 实收≠期望 → 误拒 → 付了没升级。修：建单把应收 USD 快照进订单 metadata，门比快照（§5/§7.1）。**B1+B2 同一根治。**
- **B3 HIGH（CRITICAL→降级）return 路径欠规约**："仿 fake_pay_browser"会复制凭 order_id 结算/无事实门的不安全语义。修：return 镜像 webhook 分支——capture 用存储的 provider_order_id、跑 USD 事实门、仅通过才 `signature_valid=True`；归属校验是错控制不做（§7.4）。
- **B4 HIGH（确认）i18n 守卫红灯**：checkout-card.tsx 内联中文触发 `uiloc:cjk-guard` CI 阻断。修：走 message key（§8/§12）。

### Significant（P1 一并做；已并入上文）
- **S1 MEDIUM** USD 价数据链须贯通 `PlanConfig→PlanPrice/PlanDefinition→_get_runtime_plans→/api/plans`（"零新基建"纠正，§5）。
- **S2 MEDIUM** capture 只在 billing.py paypal 分支，不塞进只读 `query_order`；跨入口去重靠订单行锁+终态守卫（§7.4）。
- **S3 MEDIUM** `is_paypal_live_ready` 须含"USD 价已发布"，否则 PayPal 显示可用但建单 502（§7.1）。
- **S4/S5 MEDIUM** `PAYMENT.CAPTURE.REVERSED`(拒付)接入退款映射 + `_is_refund_resource_event` + custom_id 绑定 + 测试，否则拒付后用户保留套餐（§7.1/§7.3）。
- **S6 MEDIUM** 取消/弃单流程 + banner `cancelled` 态（§7.4/§8）。
- **S7 MEDIUM** 点名 `billing_reconciliation` sweeper(main.py:474-478) 为 APPROVED-capture + 丢 webhook 兜底（§7.4）。
- **S8 LOW（HIGH→降级）** 验签 API 宕机已 fail-closed + sweeper 恢复；补一行说明 + 测试（§7.4）。

### Minor（折入 P1/P2/P3，不阻塞开工）
- **M1** admin USD UI 是真前端工作（PlansEditor/Display + TS 类型，§8）。
- **M2** 废弃 `fx_usd_cny=7.0` 字段与新模型撞，移除/隐藏（§5）。
- **M3** `AVT_PAYPAL_WEBHOOK_ID` 硬启动门 + transmission_time 新鲜度窗口（防 sandbox-id 误用 live）。
- **M4** order `expires_at` 不参与结算（晚结算允许，正确）；PayPal 长批准下 30min 倒计时误导，§14 加一句、考虑 PayPal 轨不显倒计时。
- **M5** USD 实收要进 admin 对账面板（`_serialize_order` 现只回 amount_cny，metadata 不可见）。
- **M6** USD 价纳入 `detect_frozen_field_changes` 审计（鉴于 B2，§5）。
- **M7** redirect `status=` 仅显示提示，banner 必经 `GET /orders/{id}` 取服务端真相（§8）。
- **M8** §5.1 定价缓冲在 Pro 年 +1% 偏薄（平 ¥2 仅够 $0.30 固定费）；薄档加 ¥3–4 或把上线后费率复核列为硬 checklist。

### 复核驳回/降级（避免过度设计）
- "三结算路径双计点"→ 已被订单行 FOR UPDATE 锁+终态守卫处理（三 provider 同款），非缺陷。
- "验签 API 宕机丢结算"→ 驳回，sweeper 已恢复（残留 S8 一行）。
- "伪造 return 结算他人订单"→ 归属校验是错控制，仅留事实门接线（=B3）。

### geo 路由（§7.5）专项对抗性核验（2026-06-26，3-facet）
PASS-WITH-FIXES，已全部并入 §7.5。**合规红线核验为干净**（只动推荐/可见性，create_order 仍用 body.provider、失败 502 无切换）。修正项（§7.5 内联 G-标签）：G-HIGH-1 两段式构造保护 `default_provider`（不在被过滤列表上导出）；G-HIGH-2 硬不变量 `recommended_provider ∈ visible`（否则前端 CTA 误标"测试支付"仍可点、给隐藏渠道下真单）；G-HIGH-3 现有 `test_wechatpay_is_recommended_on_mobile_and_desktop` 会因新规则翻红须更新；G-MEDIUM-1 `request=None`/空头 fail-open 不过滤；G-MEDIUM-2 paypal hint+USD notice 必加（message key）；G-INFRA `Cf-Ipcountry` 活路径未证（traffic_analytics 读的是 Caddy 日志非活请求头）→ 降级为"P0/P1 实测"，fail-open 兜底；G-LOW-1 用 `request.headers.get` 不移植 `_first_header`。
