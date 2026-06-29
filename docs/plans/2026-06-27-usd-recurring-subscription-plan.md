# USD 自动续费订阅（Paddle）/ CNY 一次性 + 自由续费顺延 — 方案 v3（2026-06-27）

> 目标：**美元（信用卡）用户走 Paddle 自动续费订阅；人民币用户「无自动扣款」但可自由续费、时长顺延**。
> 基线：4-agent 研究 + 主模型综合 + **CodeX 两轮审核（全采纳）** + **项目主 Q1-Q9 拍板（本版已锁定）**。
> **v3 变更**：① Q1-Q9 决策锁定（§12）；② 续费 transaction 经 **provider preprocessor 先建本期 order 再进结算漏斗**（CodeX-2#1）；③ 内部状态 enum 统一 `cancelled`，adapter 映射 Paddle `canceled`（CodeX-2#2）；④ 积分 bucket **各算各的到期 + earliest-expiring-first 消耗**（CodeX-2#3 + Q8）；⑤ sweeper 先 `GET` provider 再对账（CodeX-2#4）；⑥ v1 不接 provider trial、约束不含 `trialing`（CodeX-2#5 + Q5）；⑦ `provider_subscription_id` 改 `(provider, id)` partial unique；⑧ 币种落具体字段名；⑨ 删 Paddle $5M 误引。
> 关联：[[project_paypal_integration_status]]、[[project_wechatpay_integration]]、[[project_paddle_p1_status]]、[[project_usd_recurring_subscription_plan]]。

---

## 0. 一句话结论（决策已锁）

- **USD 自动续费 = Paddle（MoR）**；订阅意图默认路由 Paddle；**PayPal 只留一次性付款选项**。
- **CNY 近阶段永远无自动扣款**——只做用户**显式发起**的一次性续费,但**放开自由续费 + 时长顺延**（P0.5 快赢）。
- **MoR ≠ 免责**：Paddle 担税务/VAT、退款、争议、催款;但**自动续费披露 + 账户取消体验 + 中英文案仍是我们的产品责任**（MoR 管的是税务与支付清算责任,不是消费者侧的营销/披露——后者在 ARL/ROSCA 下仍归卖家）。

---

## 1. 现状（file:line 实证）

| 事实 | 位置 |
|---|---|
| 4 轨全一次性 | PayPal `intent=CAPTURE`(`payment_provider_paypal.py:265`)、Paddle `create_transaction`、微信 QR、支付宝跳转 |
| **未到期/同档不能续费(后端硬挡)** | `billing.py:150-151` `if rank[target] <= rank[current]: 拒绝` → Plus 只能买 Pro;Pro 啥都不能买 |
| **续费即重置、丢剩余时长** | `subscriptions.py:192-193` `current_period_end = paid_at + 30/90/365天` |
| `Subscription` 无续费字段 | `models.py:461-529`;状态注释含 cancelled/expired 但只写 active |
| 单活跃唯一约束**只覆盖 active** | `models.py:483` WHERE `status='active'` |
| 结算漏斗强依赖 order_id | `_process_payment_event`(`billing.py:1470`) 接已存在 order;归一事件要 order_id(`payment_providers.py:12`) |
| credits bucket 按订单/周期发,alembic 044 一订单一 bucket | `credits_service.py:1582` / `models.py:619` |
| **无到期 sweeper** | `billing_reconciliation.py` 只管未结算订单 |
| 权益门读 `user.plan_code`(永不过期) | `entitlements.py:227` |
| `BillingInvoice` 已有 currency;`PaymentOrder` 金额字段叫 `amount_cny` | `models.py:570` / `models.py:402` |

---

## 2. 设计原则 / 硬不变量

1. **续费 = 每期新 `PaymentOrder` 走原结算漏斗**（非原地改 active 行）→ 下游零改动写新 invoice + 新 per-period bucket。
2. **两条事件流分离 + 续费 preprocessor（CodeX-2#1）**：
   - **`transaction.*`（钱）**：续费 `transaction.completed` **先由 Paddle preprocessor 处理**——按 `subscription_id` 找本地订阅（`(provider, provider_subscription_id)`）→ **创建本期 `PaymentOrder`**（plan/period 取自订阅,`provider_order_id`=该 transaction id 作幂等键）→ **再调原 `_process_payment_event`** 结算。`_process_payment_event` 仍只接已存在 order_id、不自己造单。
   - **`subscription.*`（合同）**：只更新本地订阅状态 / `next_billing_at`,**不建 order、不伪造钱事件**,独立幂等。
3. **内部状态 enum 统一 `cancelled`（CodeX-2#2）**：本地模型沿用 `models.py:506` 的 `cancelled`;**Paddle adapter 把 `canceled`→`cancelled`** 映射,内部各处只用 `cancelled`。
4. **CNY 无自动扣款,但自由续费 + 顺延**（§4）。**CLAUDE.md 付费 API 硬约束**：失败续费不跨轨自动重扣;`past_due` 显式;渠道用户显式选、不自动 fallback。
5. **USD 锚定账本**：USD 续费分字段记金额,不塞 `amount_cny`（§5）。
6. **provider 是续费真相源**：webhook 为主 + `GET subscription` 兜底对账（PayPal ~8% CANCELLED 丢）。
7. **`user.plan_code` 降为投影**——但**先 shadow 审计 + 90 天统一宽限再切**（§7,Q9）。

---

## 3. 续费轨 = Paddle（决策已锁,Q1）

理由（MoR vs PSP）：Paddle 对 recurring transaction、subscription lifecycle、scheduled cancellation 都是现成模型,且**降低**税务/VAT、退款、争议、催款负担;PayPal 订阅会把这些更多压回我们(PayPal=PSP,我们是 merchant)。**PayPal 保留为「海外一次性钱包付款」,不当第一版订阅主轨。** 接入是**加法**:复用 `POST /transactions` + 换 recurring price + 反转一个 drift 断言 + 加 `subscription.*` webhook。
⚠️ **路由**:之前 geo「海外推 PayPal」要调——「订阅」意图路由 Paddle,PayPal 降一次性选项。
⚠️ **EU（Q4）**:EU 订阅**只走 Paddle**;Paddle 合规 UI 未完备前 **EU 订阅入口先关**。

---

## 4. 自由续费 + 时长顺延（P0.5 快赢,Q8 锁定）

**问题**：现在未到期/同档不能续、续了还重置丢时长（§1）。

**改造（仅动一次性模型,无 provider 订阅、无合规增量 → 可在大项目前先上）**：
1. **放开同档续费**：`billing.py:150-151` 改为**只拒严格降级** `if rank[target] < rank[current]: 拒绝`;`==`（同档续费）放行。**跨档不做顺延/折算**（保留现「仅升级」语义,跨档升级另开专项,Q6）。
2. **时长顺延**：`upsert_active_subscription` 同档续费时 `current_period_end = max(now, 现 end) + 周期天数`,`started_at` 不动。
3. **积分各算各的（Q8 + CodeX-2#3）**：续费新建 order → 新 bucket,**新 bucket `expires_at` = 本期(顺延后)周期末**;**旧 bucket 不动**（保留其原到期）。消耗按 **earliest-expiring-first（FIFO）**。→ 既「时长+用量顺延」,又避免「续一次把旧未用额度无限延寿」的财务坑。

> 即：提前续费**不**延长旧 bucket,只新增一个到新周期末的 bucket;先到期的先消耗。

---

## 5. 数据模型 + migration（alembic 045）

**Subscription 加列**：`provider_subscription_id`、`auto_renew`(default false)、`cancel_at_period_end`、`next_billing_at`、`provider_customer_id`、`currency`;真正写入 `past_due/paused/cancelled/expired`。
- **`provider_subscription_id` 用 `(provider, provider_subscription_id)` partial unique**（CodeX-2 小建议,耐多 provider 演进),非单列 unique。
- **open-subscription 唯一约束（CodeX#4 + Q5）**：把 `uq_subscriptions_one_active_per_user`(仅 active)改成 **「每用户最多一个 open 订阅」**——partial unique WHERE `status IN ('active','past_due','paused')`。**v1 不接 provider trial（Q5）故不含 `trialing`**;若将来 Q5 改,再把 `trialing` 纳入 open 集。

**币种字段落地（CodeX#6,具体字段名避免回到 amount_cny 万物筐）**：USD 续费在 invoice/order 上分字段：`provider_gross_minor` / `provider_tax_minor` / `provider_net_minor` / `provider_currency`（Paddle 在 list price 上加税,gross≠net）+ `internal_amount_cny`（内部权益锚/统一报表）。**USD 不塞 `amount_cny`**（延 PayPal B1/B2 纪律）。

**合规同意快照（ARL ≥3 年,P1 上线门）**：新表 `subscription_consent`（user_id、provider_subscription_id、disclosed_terms 快照、consented_at、ip/ua）——**结账时落,不可补**。

> 金融 schema,按 credits 044 纪律：模型 `__table_args__` 同步 + 契约测试 + 生产 upgrade 走维护窗口。

---

## 6. 计费/续费流程

### 6.1 开通（Paddle）
plan/period → **recurring price_id**（`billing_cycle` 非空）→ **反转** `_price_problems`(`payment_provider_paddle.py:555-570`,现把 recurring 当 drift 拒) → `transaction.completed` → Paddle 自动建 Subscription（`sub_…`）+ `subscription.created` → 回写 `provider_subscription_id` + `auto_renew=true`。

### 6.2 续费（每期,preprocessor + 两条流）
- **`transaction.completed`** → **Paddle preprocessor**：按 `subscription_id` 找本地订阅 → 建本期 `PaymentOrder`（`provider_order_id`=transaction id 幂等）→ 调 `_process_payment_event` → 原漏斗写新 invoice + alembic-044 新 bucket（FIFO 到期）。
- **`subscription.updated`** → 只更新本地状态 / `next_billing_at`,不建 order。
- ⚠️ 续费 charge 若不经 preprocessor 直进漏斗 → 无 order → 被去重/终态守卫静默丢弃。

### 6.3 取消 / past_due / 到期
- **取消**：用户点「取消自动续费」→ Paddle `POST /subscriptions/{id}/cancel`（`effective_from=next_billing_period` → 产生 scheduled change,期末停、期内仍有权益）;`subscription.canceled`(→内部 `cancelled`) 落终态。
- **past_due = 7 天软宽限（Q3,非 30 天）**：续费失败 → 本地 `past_due`：**保留访问 + 已有交付,但不发新周期 credits**;**7 天后限制新建付费任务**。Paddle 仍在 dunning（最长 30 天）——若期间 Paddle 扣款成功 → 正常续费（新 order+新 bucket）恢复;若 Paddle 最终 cancel/pause → 本地 `cancelled`/`paused`。（本地 7 天软宽限**独立于** Paddle dunning 窗。）
- **到期 lapse — sweeper 先 GET 再对账（CodeX-2#4）**：危险的不是「过期且非 active」,而是**本地仍 active 但 provider 已取消/没续上**。sweeper 对本地 active/past_due 且临近/超过 `next_billing_at` 的订阅 **先 `GET /subscriptions/{id}`**,按 **provider 状态 + 本地周期**决定：provider canceled/paused → 降级;provider active 带新周期但本地漏了 → 经 preprocessor 补建该期 order。兼顾「漏续费」与「漏取消(~8%)」。
- **退款单期化（呼应 PayPal B1）**：`adjustment.created`（单笔退款）**不得**误当订阅取消去全量回收权益;退款只回收该期 bucket,订阅存续由 `subscription.*` 定。

---

## 7. 历史用户 / 权益周期化（Q9 = 90 天统一宽限）

权益门从开放式 `user.plan_code`（永不过期）切周期感知前,**历史一次性付费用户会突然过期**。决策（Q9）：**不永久 grandfather,也不按末单立刻过期,统一 90 天宽限**：
1. **shadow 审计**：先统计若按 `current_period_end` 过期会被降级的用户数+名单+末单时间。
2. **切换日起,已过期历史付费用户给 90 天宽限**;未来到期用户取 `max(原 end, 宽限期)` 的更晚者。
3. 90 天窗内前端做到期提示 + 续费 CTA + 公告触达;窗后按周期判定。判定逻辑配开关、可回滚。

---

## 8. 合规（USD 侧 = P1/P2 上线门,CodeX#2）

**CNY 无自动扣款 = 正确**：微信代扣只对企业+行业白名单、每模板审批+用户签约,资质门极高;无周期扣款则 negative-option 披露/提醒/取消义务大多不附着。

**USD 侧上线前必须就位（先于真金 checkout）**：
1. 结账前**清晰显著自动续费披露** + **独立 affirmative consent 勾选** + **同意快照存 ≥3 年**（加州 ARL）。
2. **易取消**：账单页「取消自动续费」按钮,在线取消 ≥ 注册同样容易（ROSCA）。
3. **续费提醒邮件**（ARL：含月付都要年度提醒;涨价提前通知）+ **开通确认邮件**。
4. **`GET subscription` 兜底对账**（PayPal ~8% CANCELLED 丢 → 否则继续扣已取消用户 = ROSCA「10 工作日内停扣」违规）。
5. **EU 只走 Paddle（Q4）**,披露 UI 仍我们的;Paddle 合规 UI 未备则 EU 订阅入口先关。

法规现状（2026-06）：FTC click-to-cancel 2025-07-08 被撤,但 ROSCA+加州 ARL+FTC§5 仍底线,FTC 2026-03 已发 negative-option ANPRM → 按实质前瞻建。

---

## 9. 前端

- **结账（仅 USD 订阅意图）**：续费披露块 + 独立同意勾选;CNY/一次性跳过。
- **账单页**：USD 订阅显示**下次扣费日 + 「取消自动续费」**;CNY 显示**「续费/续期」CTA（任何时候可点、时长顺延）**;历史用户宽限期显示到期提示。
- **定价页**：USD「按月自动续费,随时可取消」;CNY「一次性,可提前续、时长顺延」。
- 现有 i18n（`billing` namespace 已存在）;不得内联 CJK（cjk-guard）。

---

## 10. 分阶段

- **P0 决策**：✅ 已锁（Q1-Q9,§12）。
- **P0.5 快赢（可先单独上）**：同档自由续费 + 时长顺延 + 积分 FIFO 各算各的（§4,纯改一次性模型）。
- **P1 schema + 状态机 + 合规最小闭环 + shadow 审计**：alembic 045（续费列 +(provider,id)unique + open-subscription 约束 + consent 表 + 币种字段）+ 模型同步 + 契约测试;`upsert_active_subscription` auto_renew 感知;**披露+独立同意+同意快照+取消入口+开通确认邮件最小闭环**（上线门）;90 天宽限 shadow 审计 + 切换开关;**默认全 inert**。
- **P2 Paddle recurring 隐藏开关接入**：recurring price + 反转 drift 门 + **preprocessor + transaction/subscription 两条流** + 取消端点 + `GET` 兜底对账 + `canceled→cancelled` 映射。
- **P3 对账 sweeper（GET 优先）+ past_due 7 天软宽限 + 退款单期化**。
- **P4 前端 + 灰度 + 小额真金 E2E**（开通→续费→取消→past_due→退款全链路;EU 入口 gate）。

---

## 11. 风险 / 红线

1. **历史用户突然过期**→ 90 天统一宽限 + shadow 审计 + 可回滚开关。
2. **续费 charge 缺 order 静默丢弃**→ 必经 preprocessor 先建 order;`subscription.*` 不伪造 order。
3. **合规漏建 = 直接法律暴露**（PayPal 路径我们是 merchant;Paddle 路径披露 UI 仍我们的）→ P1/P2 上线门,同意快照结账落不可补。
4. **MoR 不免责**→ 担税务/争议不担披露/取消 UX/文案。
5. **状态约束打架**→ open 集含 active/past_due/paused。
6. **币种混淆**→ gross/tax/net 分字段,不塞 amount_cny。
7. **enum 漂移**→ 内部统一 `cancelled`,adapter 映射 `canceled`。
8. **本地 active 但 provider 已取消/没续**→ sweeper 先 GET 对账。
9. **CNY「手动续」不得悄变免密快捷续**→ 每笔须用户显式发起。
10. **积分顺延财务坑**→ 旧 bucket 不延寿,FIFO 各算各的。

---

## 12. 决策记录（Q1-Q9,项目主已锁 2026-06-27）

| # | 决策 |
|---|---|
| **Q1 续费轨** | USD 自动续费走 **Paddle**;订阅意图默认路由 Paddle;PayPal 只留一次性。 |
| **Q2 CNY 扣款** | CNY 近阶段**永远无自动扣款**,仅用户显式一次性续费;字段放共享 `Subscription`,`auto_renew=false`。 |
| **Q3 past_due** | **7 天软宽限**：保留访问+已有交付、不发新周期 credits;7 天后限制新建付费任务（非 30 天）。 |
| **Q4 EU** | EU 订阅**只走 Paddle**;PayPal 只一次性;Paddle 合规 UI 未备则 EU 订阅入口先关。 |
| **Q5 试用** | 第一版**不接 provider 托管试用**,续用内部 `User.trial_*`。 |
| **Q6 升降级** | 第一版**不做 proration/PATCH**;只支持取消续费 + 到期重买;跨档升级另开专项。 |
| **Q7 价格源** | Gateway `plan_catalog.*_usd_cents` 是**真源**,Paddle recurring price_id 只映射;上线前 drift check 校金额/币种/`billing_cycle`。 |
| **Q8 自由续费** | 第一版**只放开同档顺延**,不做跨档;credits bucket **各算各的到期,earliest-expiring-first 消耗**。 |
| **Q9 历史用户** | **统一 90 天宽限**（切换日起,已过期历史付费用户得 90 天;未来到期取 max(原 end, 宽限)）;不永久 grandfather、不按末单立刻过期。 |

---

## 13. 研究来源 + 审核记录

- 架构（代码精读）：`billing.py`/`subscriptions.py`/`payment_providers.py`/`payment_provider_{paypal,paddle}.py`/`models.py`/`credits_service.py`/`entitlements.py`/`billing_reconciliation.py`。
- Paddle：developer.paddle.com（subscription-created / renewal-simulator / cancel / dunning）。
- 合规：FTC 2026-03 negative-option ANPRM、加州 ARL（2025-07-01）、ROSCA、EU CRD/撤回按钮、微信代扣指引、Paddle/PayPal MoR-vs-PSP。
- **CodeX 第一轮（6 条全采纳）** + **第二轮（5 fix + 2 小建议全采纳）**：preprocessor 建单 / `cancelled` enum 统一 / 积分各算各的 FIFO / sweeper 先 GET / v1 不接 provider trial / `(provider,id)` unique / 币种具体字段。
- **项目主**：Q1-Q9 拍板（本版锁定）+ 自由续费/顺延。
- ⚠️ 删去 v2 的「Paddle 2025 $5M 因披露不当」误引（该案实为 Paddle 替诈骗商户洗单的支付处理问题,非自家续费披露;结论「MoR≠免责」保留,论据换为 ARL/ROSCA 下消费者侧披露归卖家）。
- ⚠️ 若将来改 Q1 选 PayPal,实施前补一轮 PayPal Subscriptions API 专项核实（`paypal_subscriptions` 研究 agent 上次输出退化为占位）。
