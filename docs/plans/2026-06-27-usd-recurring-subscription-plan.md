# USD 自动续费订阅 / CNY 一次性 — 方案（2026-06-27）

> 目标：**美元（信用卡）用户走自动续费订阅，人民币（微信/支付宝）用户保持一次性付费手动续**。
> 证据基线：4-agent 并行研究（现有架构代码精读 + Paddle 订阅 + PayPal 订阅 + 合规）+ 主模型综合。
> 关联：[[project_paypal_integration_status]]、[[project_wechatpay_integration]]、[[project_paddle_p1_status]]、`docs/plans/2026-06-26-paypal-integration-plan.md`。

---

## 0. 一句话结论

- **CNY「不订阅」= 现状,零改动**：当前**所有 4 条轨都是一次性付费**（微信 Native QR / Paddle `POST /transactions` / PayPal Orders v2 `intent=CAPTURE` / 支付宝页面跳转），`subscriptions` 表只是「一次性付费授权窗口」的投影,没有任何自动续费机制。
- **USD「走订阅」= 新增专项**：要加**真·自动续费**（provider 周期扣款 + 续费 webhook + 取消 + 到期 + 合规）。
- **续费轨推荐 Paddle（MoR）而非 PayPal**：Paddle 作为 Merchant of Record **替我们扛掉**销售税/VAT 申报、催款 dunning、click-to-cancel 合规、EU 14 天撤回权、拒付争议。PayPal Subscriptions API 会把**全部合规与税务**压回我们身上（PayPal 是 PSP 不是 MoR）。

---

## 1. 现状（研究 agent 代码精读,file:line 实证）

| 事实 | 位置 |
|---|---|
| 建单只算 CNY、一次性 | `billing.py:157` `get_price(plan,period)` 出 CNY 分；USD 仅 metadata（`billing.py:212-220` 存 `paypal_expected_usd_cents`） |
| Provider 抽象无 recurring 面 | `payment_providers.py:42-73` Protocol 只有 create_checkout/verify/parse/map/query；`CheckoutResult` 无 plan_id/subscription_id/mandate |
| 4 轨全一次性 | PayPal `intent=CAPTURE`(`payment_provider_paypal.py:265`)、Paddle `create_transaction`、微信 Native QR、支付宝页面跳转 |
| `Subscription` 模型刻意不含续费字段 | `models.py:461-529` docstring 明写 mandates/续费 **OUT OF SCOPE**；无 `provider_subscription_id`/`auto_renew`/`cancel_at_period_end`/`next_billing_at`/`currency` |
| 结算漏斗 provider 无关 + 幂等 | `billing.py:1470` `_process_payment_event`：`(provider,event_id)` 去重 + 订单行锁 + 终态守卫；paid → 写 invoice → `upsert_active_subscription` → 投影 `user.plan_code` → `ensure_subscription_bucket` |
| `upsert_active_subscription` 续费盲（原地覆盖） | `subscriptions.py:144-195` 复用同一 `Subscription.id`、`current_period_end = paid_at + 固定 30/90/365 天`，纯信息性、**没人读它去过期** |
| credits bucket 已按订单/周期发 | `credits_service.py:1582` 每个 paid order 一个 bucket；alembic 044 `uq_credits_bucket_subscription_order` 保证「一订单一 bucket」→ **续费 = 新订单 = 新 bucket,天然安全** |
| **没有到期 sweeper** | 唯一 sweeper `billing_reconciliation.py` 只管未结算订单（created/pending），从不碰 subscriptions；`current_period_end` 只写不读 |
| 权益门读 `user.plan_code` 不读周期窗 | `entitlements.py:227`；付费用户**无限期保留**直到退款把 plan_code 打回 free |

> **关键含义**：现在「订阅」是假的（一次性授权 + 永不过期）。要做真订阅,缺的不只是支付集成,还缺**到期/续费的状态机**。

---

## 2. 设计原则 / 硬不变量

1. **续费 = 每期一个新 `PaymentOrder`,走原有结算漏斗**（不是原地改那唯一 active 行）。这样下游零改动地写新 `BillingInvoice` + 新 per-period credits bucket（alembic 044 已保证唯一性）。
2. **CNY 侧完全不动**——保持一次性、手动续。理由见 §6（代扣资质门 + 合规更轻）。
3. **遵守 CLAUDE.md 付费 API 硬约束**：续费扣款失败**绝不**自动改走另一条轨重扣;`past_due` 必须显式、用户可见。各 provider 由用户显式选择,**渠道间不自动 fallback**。
4. **USD 锚定账本**：USD 续费发票必须记 USD（延用 PayPal B1/B2 的 USD 快照纪律,`billing.py:212`）;`Subscription`/`BillingInvoice` 需加 `currency`,**不得**把 USD 当 CNY 记。
5. **provider 是续费真相源,本地是投影**：续费日历由 provider 管（Paddle `next_billed_at` / PayPal billing anchor）,本地 `current_period_end` 不再用固定天数算,改存 provider 给的真实周期;**webhook 是真相 + GET 查询兜底**（PayPal ~8% `CANCELLED` 事件会丢,必须 reconcile）。

---

## 3. 续费轨选型（核心决策）

| 维度 | **Paddle 订阅（MoR）— 推荐** | PayPal Subscriptions（PSP） |
|---|---|---|
| 谁是 Merchant of Record | **Paddle** | **我们** |
| 销售税/VAT 计算+申报 | Paddle 全包（100+ 法区） | **我们自负** |
| 催款 dunning / 失败重试 | Paddle 自动（7 次/30 天,或 Retain 算法重试 + 召回邮件） | **我们自建** |
| click-to-cancel / ARL / ROSCA 合规 | Paddle 托管取消邮件+链接 | **我们自建全套** |
| EU 14 天撤回 + 撤回按钮 | Paddle 吸收 | **我们自建** |
| 拒付/欺诈争议 | Paddle Dispute Defense | **我们处理** |
| 接入工作量 | **加法**：复用现有 `POST /transactions`,换成 recurring price + 反转一个 drift 断言 + 加 `subscription.*` webhook 族 | 全新 Subscriptions v1 层 + 全套合规 + 税务 |
| 与现有代码契合 | 高（已有 Paddle 签名验/价格门/customer 解析,且价格门**故意按 price_id 不按总额**,正是为 MoR 加税设计） | 中（复用 OAuth/验签,但要新建订阅层） |
| 取舍 | **省掉巨大的合规+税务建设** | 保持 PayPal 当主 USD 轨,但**自背全部合规** |

**推荐：USD 自动续费走 Paddle 订阅模式。** PayPal Orders v2（一次性）**保留**作为「PayPal 钱包一次性付」选项;微信保持一次性 CNY。

> ⚠️ **路由含义**：我之前做的 geo 路由是「海外推荐 PayPal」。若续费走 Paddle,则**「订阅」意图的结账要路由到 Paddle**,PayPal 降级为一次性选项。这是要项目主拍板的产品点（见 §10 Q1）。
> ⚠️ **Paddle 合规反面教材**：2025 年 FTC 对 Paddle $5M 和解,正是因自动续费**披露不当**——即便用 MoR,**结账页仍必须清晰展示续费条款 + 易取消**,否则照样踩同一条红线。

---

## 4. 数据模型 + migration

新建 alembic `045_subscription_recurring_fields`,给 `Subscription` 加：

| 列 | 用途 |
|---|---|
| `provider_subscription_id`（unique, nullable） | provider 侧订阅/合约句柄（Paddle `sub_...` / PayPal `I-...`）。CNY 一次性行此列为 NULL |
| `auto_renew`（bool, default false） | true=USD 自动续费;false=CNY/一次性 |
| `cancel_at_period_end`（bool） | 用户已约取消、期末停 |
| `next_billing_at`（timestamptz, nullable） | provider 给的下次扣费日 |
| `currency`（varchar(8), default 'CNY'） | 区分 USD/CNY,杜绝币种歧义 |
| `provider_customer_id`（nullable） | Paddle `ctm_...` 等 |
| 扩 `status` 实际写入值 | 真正用上 `past_due`/`paused`/`cancelled`/`expired`（现仅写 active） |

并加：
- **续费发票 per-period key**：`record_invoice_for_order`（`subscriptions.py:67-141`）现按 `payment_order_id` 1:1;续费每期一个新 order 即天然满足,无需新键（延用「每期新 PaymentOrder」原则）。
- **合规同意快照**（ARL 要求留存 ≥3 年）：新表 `subscription_consent`（user_id、provider_subscription_id、disclosed_terms 快照、consented_at、ip/ua）——**结账时落库,不可事后补**。

> migration 是**金融 schema**,按 credits 044 的纪律：模型 `__table_args__` 同步 + 契约测试 + 生产 `alembic upgrade` 走维护窗口。

---

## 5. 计费/续费流程

### 5.1 开通（USD 订阅,Paddle）
1. 结账「订阅」意图 → 映射 plan/period 到 **recurring price_id**（`billing_cycle` 非空,与意图 interval/frequency 一致）。
2. **反转 `check_price_drift`**：现有 `_price_problems`（`payment_provider_paddle.py:555-570`）把 recurring 价当 drift **拒绝**;USD 订阅轨要求 `billing_cycle` 非空 + `currency==USD`。
3. `transaction.completed` → Paddle **自动创建 Subscription**（`sub_...`）+ 发 `subscription.created`。回写 `provider_subscription_id` + `auto_renew=true`。

### 5.2 续费（每期）
- Paddle 每期发 `transaction.completed`（这期的扣款）+ `subscription.updated`。
- **`_process_payment_event` 收到续费 charge → 为该期 mint 一个新 `PaymentOrder`（带 `provider_subscription_id`）→ 走原漏斗** → 新 `BillingInvoice` + alembic-044 自动 mint 新 per-period credits bucket。**下游零改动。**
- ⚠️ 风险：续费 charge 若**没有新 order** 就进漏斗,会被 `(provider,event_id)` 去重/终态守卫**误当签约单的重复而静默丢弃**（无新发票、无新 bucket）。**每期必须先有自己的 order 行。**

### 5.3 取消 / past_due / 到期
- **取消**：用户点「取消自动续费」→ 调 Paddle `POST /subscriptions/{id}/cancel`（`effective_from=next_billing_period`,期末停、期内仍有权益）;`subscription.canceled` 到达后落终态。Paddle 托管门户 `management_urls` 亦可。
- **past_due**（续费失败）：Paddle 自动 dunning（7 次/30 天或 Retain）。本地收 `subscription.past_due` → 置 `past_due`,**按产品决策**保留或限制权益（§10 Q3）。**绝不自动改走别的轨重扣。**
- **到期/lapse**（缺失件,必须新建）：今天**没有任何东西**在 `current_period_end` 过后收回权益。两选一：① 纯 webhook 驱动（`subscription.canceled/expired` → flip status + 降级 plan_code）;② 加一个 **到期/对账 sweeper**（兜 provider 漏发的续费/取消 webhook,把过期且非 active 的订阅 flip 成 expired）。**推荐 ①+② 并用**（webhook 为主、sweeper 兜底,呼应 PayPal ~8% CANCELLED 丢失）。
- **权益门改造**：把付费门从「开放式 `user.plan_code` 投影」改成**周期/auto_renew 感知**——否则取消/失败续费后用户仍永久保留权益（现状 bug 会被订阅放大）。

### 5.4 退款（呼应 PayPal B1 教训）
- 单期退款 ≠ 取消订阅。`adjustment.created`（单笔退款）**不得**误当订阅取消去全量回收权益（会过度收权,正是 PayPal B1 那类误判）。退款回收只针对该期 bucket;订阅存续与否由 `subscription.*` 决定。

---

## 6. 合规（USD 侧必建,CNY 侧免）

**为什么 CNY 保持一次性 = 正确**（研究实证）：
- 微信「委托代扣/自动续费」只对**企业/机构、且在指定行业白名单**（在线会员:视频/音频/阅读/游戏、公用事业、交通）开放,要标准费率类目 + 认证客服电话 + **每个扣费模板单独审批** + 用户签约授权;支付宝周期扣款同理。**资质门极高。**
- 一次性付费**绕开**整个代扣资质门,且因**没有周期扣款**,negative-option 的披露/提醒/取消义务大多不附着——每次都是离散的、用户主动授权的支付。

**USD 侧必须建（PayPal 路径下我们是 merchant;Paddle 路径下 Paddle 吸收大部分）**：
1. **结账前**「清晰显著」自动续费披露 + **独立的 affirmative consent 勾选**（与接受 ToS 分开）,并**落同意快照存 ≥3 年**（加州 ARL）。
2. **易取消**：账单页「取消自动续费」按钮,在线取消 ≥ 注册同样容易（ROSCA「online signup → online cancel」），直达、无挽留阻碍。
3. **续费提醒邮件**（ARL：所有套餐含月付都要年度提醒;涨价提前通知）+ 开通确认邮件。
4. **状态对账**：靠 `GET subscription` 兜底,不只信 webhook（PayPal ~8% CANCELLED 丢失 → 否则会继续扣已取消用户 = ROSCA「10 个工作日内停扣」违规 + 退款/拒付磁铁）。
5. **EU**：14 天撤回权 + 即将的撤回按钮 → **强烈建议 EU 客户走 Paddle（MoR 吸收）**,而非 PayPal 自背。

> 法规现状（2026-06 核实）：FTC「click-to-cancel」规则 2025-07-08 被第八巡回**撤销**,但 ROSCA + 加州 ARL + FTC §5 仍是**常在底线**,且 FTC 2026-03 已发 ANPRM 重新立法。**按被撤销规则的实质建设以求前瞻**。

---

## 7. 前端

- **结账（仅 USD 订阅意图）**：续费披露块 + 独立同意勾选;CNY/一次性结账**跳过**（无周期扣款）。
- **账单页**：USD 订阅用户显示**下次扣费日 + 「取消自动续费」按钮**;CNY 用户显示**「手动续费」CTA**（一次性）。
- **定价页**：USD 写「按月自动续费,随时可取消」;CNY 写「一次性,X 天有效,到期手动续」。
- 走现有 i18n（`billing` namespace 已存在,见协调注记);**不得**内联 CJK（cjk-guard）。

---

## 8. 分阶段

- **P0 决策**：续费轨（Paddle vs PayPal）+ 路由（订阅意图路由到谁）+ past_due 权益策略 + EU 路由 → 项目主拍板（§10）。
- **P1 模型 + 状态机**：alembic 045（Subscription 续费列 + consent 表）+ 模型同步 + 契约测试;`upsert_active_subscription` auto_renew 感知;权益门改周期感知;**默认全 inert**（无开关不启用）。
- **P2 Paddle 订阅接入**：recurring price 配置 + 反转 drift 门 + `subscription.*` webhook 族 + 「续费=新 order」分支 + 取消端点 + GET 兜底对账。
- **P3 到期/对账 sweeper** + past_due 处理 + 退款单期化。
- **P4 合规件**：披露+同意快照、续费提醒邮件、取消 UI、（EU 路由）。
- **P5 前端** + 灰度开关 + 真金小额 e2e（开通→续费→取消→退款全链路）。

---

## 9. 风险 / 红线

1. **没有到期 sweeper**——引入真周期边界后,权益门必须从开放式 plan_code 改成周期感知,否则取消/失败续费后用户仍永久有权益。
2. **续费 charge 缺新 order 会被静默丢弃**（去重 + 终态守卫）——每期必须先建 order。
3. **币种歧义**——`Subscription` 无 currency、`BillingInvoice` 默认 CNY;USD 续费发票必须记 USD（B1/B2 纪律）。
4. **CLAUDE.md 付费 API 硬约束**——失败续费不得跨轨自动重扣;past_due 显式可见。
5. **Paddle FTC 和解教训**——MoR 不免「清晰披露 + 易取消」的 UI 责任。
6. **dropped CANCELLED webhook**——必须 GET 对账,否则继续扣已取消用户。
7. **CNY「手动续」不得悄悄变成免密快捷续**（保存卡一键续而无每次显式授权）——那会重新触发代扣资质 + negative-option 义务。每笔 CNY 必须用户显式发起。
8. **upsert 复用单行**——续费不得原地 clobber 签约行 provenance;续费建模为新 order。

---

## 10. 待项目主拍板的开放问题

- **Q1 续费轨**：Paddle（MoR,省合规,推荐）还是 PayPal Subscriptions（保持 PayPal 主轨,但自背全合规+税务）?「订阅」意图路由到谁,PayPal/微信是否保留为一次性选项?
- **Q2 CNY 永远一次性?** 还是将来也要 WeChat/Alipay 代扣（影响 `auto_renew` 字段是放共享 `Subscription` 还是 USD-only 兄弟表）。
- **Q3 past_due 权益**：dunning 窗口内（最多 30 天）保留权益（Paddle 推荐,利营收）还是立即限制（防滥用）?
- **Q4 EU 客户**：走 Paddle（吸收 14 天撤回 + EU 披露）还是 PayPal（自背）?
- **Q5 试用**：USD 订阅是否带 provider 托管试用?如何与现有内部 trial（`User.trial_*`,subscriptions 刻意分离）协调。
- **Q6 升降级**：in-place `PATCH /subscriptions`（按比例 proration）还是 cancel+resubscribe?现 `create_order` 只建模单调升级。
- **Q7 USD 续费价格源**：复用 `plan_catalog.*_usd_cents` 映射到 provider 的 recurring price,还是单独 recurring 价目表?

---

## 11. 研究来源（4-agent,~497k tokens）

- 架构（代码精读 file:line）：`billing.py` / `subscriptions.py` / `payment_providers.py` / `payment_provider_{paypal,paddle}.py` / `models.py` / `credits_service.py` / `entitlements.py` / `billing_reconciliation.py`。
- Paddle 订阅：developer.paddle.com（subscription-creation / provision-access-webhooks / cancel / dunning）。
- 合规：FTC ANPRM 2026-03、加州 ARL（2025-07-01 生效）、ROSCA、EU CRD/撤回按钮、微信支付代扣自助申请指引、Paddle/PayPal MoR-vs-PSP。
- ⚠️ `paypal_subscriptions` 专项 agent 输出退化为占位（未产出实质）;PayPal Subscriptions 事实由 compliance agent 覆盖（BILLING.SUBSCRIPTION.* webhook、suspend/cancel、~8% CANCELLED 丢失、PayPal=PSP 我们自背合规）。**若 Q1 选 PayPal,实施前需补一轮 PayPal Subscriptions API 专项核实。**
