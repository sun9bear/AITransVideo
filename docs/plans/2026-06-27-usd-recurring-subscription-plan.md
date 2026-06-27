# USD 自动续费订阅 / CNY 一次性 + 自由续费顺延 — 方案 v2（2026-06-27）

> 目标：**美元（信用卡）用户走自动续费订阅；人民币（微信/支付宝）用户保持「无自动扣款」但可自由续费、时长顺延**。
> 证据基线：4-agent 并行研究（架构代码精读 + Paddle/PayPal 订阅 + 合规，~497k tokens）+ 主模型综合 + **CodeX 审核（6 条全采纳）** + **项目主补充「自由续费/顺延」**。
> **v2 变更**：① 改正「CNY 零改动」误述（一次性模型其实要改进）；② 新增 §4 自由续费+顺延（项目主提）；③ 合规提前为上线门（CodeX#2）；④ MoR 表述收紧（CodeX#3）；⑤ open-subscription 唯一约束（CodeX#4）；⑥ transaction/subscription 两条流拆分（CodeX#5）；⑦ 币种字段收紧（CodeX#6）；⑧ 新增历史用户 shadow 审计（CodeX#1）；⑨ 阶段重排。
> 关联：[[project_paypal_integration_status]]、[[project_wechatpay_integration]]、[[project_paddle_p1_status]]、`docs/plans/2026-06-26-paypal-integration-plan.md`。

---

## 0. 一句话结论

- **CNY 侧 ≠ 零改动**：CNY 保持「**无自动周期扣款**」（不碰代扣资质），但一次性模型本身要改进——**放开自由续费 + 时长顺延 + 权益按周期感知**。（原 v1「零改动」是误述，CodeX#1 + 项目主自由续费两点共同纠正。）
- **USD 侧 = 真自动续费专项**：provider 周期扣款 + 续费 webhook + 取消 + past_due/到期状态机 + 合规。
- **续费轨推荐 Paddle（MoR）**：Paddle **降低**税务/VAT、退款、争议、部分运营负担；但**自动续费披露、账户页取消体验、中英文案仍是我们自己的产品责任**（Paddle 2025 因披露不当被 FTC $5M 和解为证）。PayPal Orders v2 一次性保留作钱包一次性选项。

---

## 1. 现状（研究 agent + CodeX 代码精读,file:line 实证）

| 事实 | 位置 |
|---|---|
| 4 轨全一次性 | PayPal `intent=CAPTURE`(`payment_provider_paypal.py:265`)、Paddle `create_transaction`、微信 Native QR、支付宝跳转 |
| **未到期/同档不能续费(后端硬挡)** | `billing.py:150-151` `plan_rank={free:0,plus:1,pro:2}; if rank[target] <= rank[current]: 拒绝` → **Plus 只能买 Pro;Pro 什么都不能买** |
| **续费即重置、不顺延** | `subscriptions.py:192-193` `current_period_start=paid_at; current_period_end=_period_end(paid_at)=paid_at+30/90/365天` → 丢掉剩余时长 |
| `Subscription` 刻意无续费字段 | `models.py:461-529`,无 `provider_subscription_id`/`auto_renew`/`cancel_at_period_end`/`next_billing_at` |
| 单活跃唯一约束**只覆盖 active** | `models.py:483` `uq_subscriptions_one_active_per_user` WHERE `status='active'` |
| 结算漏斗 provider 无关 + 幂等,**且强依赖 order_id** | `billing.py:1470` `_process_payment_event`;归一事件要 `order_id`(`payment_providers.py:12`) |
| credits bucket 已按订单/周期发,alembic 044 保证一订单一 bucket | `credits_service.py:1582`;`models.py:619` `uq_credits_bucket_subscription_order` |
| **无到期 sweeper** | `billing_reconciliation.py` 只管未结算订单,从不碰 subscriptions |
| 权益门读 `user.plan_code` 不读周期窗 | `entitlements.py:227` → 付费用户**无限期保留**直到退款 |
| 退款是唯一降级路径 | `billing.py:1721` `_recall_entitlements_for_refund` |
| 币种：`BillingInvoice` **已有** `currency`,但 `PaymentOrder` 金额字段叫 `amount_cny` | `models.py:570` / `models.py:402` |

---

## 2. 设计原则 / 硬不变量

1. **续费 = 每期一个新 `PaymentOrder` 走原结算漏斗**（不是原地改唯一 active 行）→ 下游零改动写新 invoice + 新 per-period bucket（alembic 044 已保证）。
2. **两条事件流分离（CodeX#5）**：
   - **`transaction.*`（钱）** → 创建/结算每期 `PaymentOrder`，进 `_process_payment_event`（保留 order_id 强依赖 + `(provider,event_id)` 幂等）。
   - **`subscription.*`（合同）** → **只**更新本地 provider-subscription 状态（active/past_due/paused/canceled），用**独立幂等表/事件类型**,**不**伪造 order。
3. **CNY 无自动扣款,但支持自由续费 + 顺延**（§4）。**遵守 CLAUDE.md 付费 API 硬约束**：失败续费不得跨轨自动重扣;`past_due` 显式可见;渠道由用户显式选,不自动 fallback。
4. **USD 锚定账本**：USD 续费记 USD,不塞进 `amount_cny`（§5 币种模型）。
5. **provider 是续费真相源,本地是投影**：续费日历由 provider 管(`next_billed_at`);**webhook 为主 + `GET subscription` 兜底对账**(PayPal ~8% `CANCELLED` 会丢)。
6. **`user.plan_code` 从最终真源降级为投影**（迟早要做,CodeX 赞成）——但**先 shadow 审计历史用户再切**（§7）。

---

## 3. 续费轨选型（核心决策,MoR 表述按 CodeX#3 收紧）

| 维度 | **Paddle 订阅（MoR）— 推荐** | PayPal Subscriptions（PSP） |
|---|---|---|
| 谁是 Merchant of Record | **Paddle** | **我们** |
| 销售税/VAT 计算+申报 | Paddle 全包 | **我们自负** |
| 退款/争议/拒付清算 | Paddle 是被诉方、Dispute Defense | **我们处理** |
| 催款 dunning / 失败重试 | Paddle 自动(7次/30天或 Retain) | **我们自建** |
| **自动续费披露 / 取消 UX / 中英文案** | **仍是我们自己的责任**(Paddle 仅托管取消邮件链接) | **我们自建** |
| EU 14 天撤回 + 撤回按钮 | EU 客户走 Paddle 可由其吸收大部分 | **我们自建** |
| 接入工作量 | **加法**:复用 `POST /transactions`+换 recurring price+反转一个 drift 断言+加 `subscription.*` webhook | 全新 Subscriptions v1 层+全套合规+税务 |

**推荐 Paddle**;PayPal Orders v2 一次性保留作选项。⚠️ **路由含义**:我之前 geo 路由「海外推 PayPal」需调整——「订阅」意图路由到 Paddle,PayPal 降为一次性选项（待 Q1）。⚠️ MoR ≠ 免责披露:即便用 Paddle,结账披露+独立同意+易取消 UI **必须自建**。

---

## 4. 自由续费 + 时长顺延（项目主提,可早做的快赢）

**问题**:现在「未到期/同档不能续费,且续了也重置丢时长」（§1 两条 file:line）——不合理、阻断营收。

**改造**（仅动现有一次性模型,**不需要 provider 订阅机制**,故可在自动续费大项目**之前先上**）：
1. **放开同档续费**:`billing.py:150-151` 由 `rank[target] <= rank[current]` 拒绝 → 改成**只拒严格降级** `rank[target] < rank[current]`;`==`（同档续费）放行。
2. **时长顺延而非重置**:`upsert_active_subscription` 同档续费时 `current_period_end = max(now, 现 current_period_end) + 周期天数`（叠在剩余时长上）,`started_at`/首个 `current_period_start` 不动。
3. **积分顺延**:续费照常新建 order → 新 credits bucket（alembic 044 安全）,`expires_at` = 顺延后的新 end → 时长+用量一起顺延。

**要拍板的设计点（§12 Q8）**：
- **同档**（Plus+Plus）：干净,先做这个。
- **跨档**（Plus 买 Pro 升级 / Pro 想买 Plus 降级）：复杂——升级是立即转 Pro 还是剩余 Plus 时长按 Pro 顺延?降级是否排队到 Pro 结束?**建议第一版只放开同档,跨档另议**（保留现有「仅升级」语义,只是不再重置而是顺延）。
- **积分 bucket 到期**:各 bucket 各算到期（先到先用）还是都到最终 end?默认各算各的（更符合 per-period 语义）。

---

## 5. 数据模型 + migration（含 CodeX#4 约束、CodeX#6 币种）

新建 alembic `045_subscription_recurring_fields`：

**Subscription 加列**：`provider_subscription_id`(unique,nullable)、`auto_renew`(bool,default false)、`cancel_at_period_end`(bool)、`next_billing_at`、`provider_customer_id`、`currency`(varchar8)；并真正写入 `past_due/paused/cancelled/expired`。

**约束重做（CodeX#4）**：`uq_subscriptions_one_active_per_user` 只覆盖 `status='active'`,引入 past_due/paused 后会让一个 past_due 用户再开一个 active 打架。改成 **「每用户最多一个 OPEN 订阅」**:partial unique index WHERE `status IN ('active','past_due','paused')`（非终态集）。

**币种模型收紧（CodeX#6）**：`BillingInvoice` 已有 `currency`;真问题是 `PaymentOrder.amount_cny` 字段名 + MoR 的金额拆分。USD recurring 上线前**明确分字段**：① 展示金额（用户看到的 USD/CNY）② provider **gross / tax / net**（Paddle 在 list price 上加税）③ 内部权益定价（plan→credits 的锚）④ CNY 估值（若需统一报表）。**不得把 USD 硬塞进 `amount_cny`**（延用 PayPal B1/B2「USD 快照、永不喂 amount_cny」纪律）。

**合规同意快照**（ARL 留存 ≥3 年）：新表 `subscription_consent`（user_id、provider_subscription_id、disclosed_terms 快照、consented_at、ip/ua）——**结账时落库,不可事后补**（CodeX#2,故属上线门）。

> migration 是金融 schema,按 credits 044 纪律：模型 `__table_args__` 同步 + 契约测试 + 生产 `alembic upgrade` 走维护窗口。

---

## 6. 计费/续费流程

### 6.1 开通（USD 订阅,Paddle）
映射 plan/period 到 **recurring price_id**（`billing_cycle` 非空）→ **反转** `_price_problems`（`payment_provider_paddle.py:555-570`,现把 recurring 当 drift 拒绝）→ `transaction.completed` → Paddle 自动建 Subscription（`sub_…`）+ `subscription.created` → 回写 `provider_subscription_id` + `auto_renew=true`。

### 6.2 续费（每期,两条流——CodeX#5）
- **`transaction.completed`（每期扣款）** → `_process_payment_event` **为该期 mint 新 `PaymentOrder`（带 `provider_subscription_id`）** → 原漏斗写新 invoice + alembic-044 新 bucket。**下游零改动。**
- **`subscription.updated`（合同状态）** → 只更新本地 Subscription 状态/`next_billing_at`,**不**建 order。
- ⚠️ 续费 charge 若没有自己的新 order 就进漏斗,会被去重/终态守卫**误当签约单重复而静默丢弃**——每期必须先有 order 行。

### 6.3 取消 / past_due / 到期
- **取消**:用户点「取消自动续费」→ Paddle `POST /subscriptions/{id}/cancel`（`effective_from=next_billing_period`,期末停、期内仍有权益）;`subscription.canceled` 落终态。
- **past_due**:Paddle 自动 dunning;本地收 `subscription.past_due` 置 `past_due`,**按 §12 Q3 决策**保留/限制权益,**绝不跨轨自动重扣**。
- **到期 lapse（缺失件,必须新建）**:① webhook 驱动（`subscription.canceled/expired` → flip + 降级 plan_code）+ ② **对账 sweeper 兜底**（兜 provider 漏发,把过期且非 active 翻 expired,对账 PayPal ~8% 丢失）。
- **退款单期化（呼应 PayPal B1）**:`adjustment.created`（单笔退款）**不得**误当订阅取消去全量回收权益（过度收权风险）。退款只回收该期 bucket;订阅存续由 `subscription.*` 定。

---

## 7. 历史用户 / 权益周期化（CodeX#1,新增）

把权益门从开放式 `user.plan_code`（`entitlements.py:227`,付费用户永不过期）改成**周期/auto_renew 感知**——但一旦读 `current_period_end`,**历史一次性付费用户可能突然过期**。故先做 **shadow 审计阶段**：
1. **影子统计**:不改判定,先跑一遍统计「若按 `current_period_end` 过期会被降级的用户数 + 名单 + 末单时间」。
2. **据数据定策略**（项目主拍 §12 Q9）：grandfather 老用户永久保留 / 按最后订单周期过期 / 给统一宽限期。
3. **再切换**判定逻辑,默认配开关、可回滚。

---

## 8. 合规（USD 侧,**上线门——CodeX#2**）

**为什么 CNY 保持无自动扣款 = 正确**：微信「委托代扣/自动续费」只对企业+行业白名单开放、每模板单独审批+用户签约,资质门极高;无周期扣款则 negative-option 披露/提醒/取消义务大多不附着。

**USD 侧上线前必须就位（P1/P2 门,不是 P4/P5）**：
1. 结账前**清晰显著自动续费披露** + **独立 affirmative consent 勾选**（与 ToS 分开）+ **同意快照存 ≥3 年**（加州 ARL）。
2. **易取消**:账单页「取消自动续费」按钮,在线取消 ≥ 注册同样容易（ROSCA）。
3. **续费提醒邮件**（ARL：所有套餐含月付都要年度提醒;涨价提前通知）+ **开通确认邮件**。
4. **`GET subscription` 兜底对账**（PayPal ~8% CANCELLED 丢失 → 否则继续扣已取消用户 = ROSCA「10 工作日内停扣」违规）。
5. **EU**:14 天撤回 + 撤回按钮 → **建议 EU 走 Paddle**（MoR 吸收大部分,但披露 UI 仍我们的）。

法规现状（2026-06 核实）：FTC click-to-cancel 2025-07-08 被第八巡回撤销,但 ROSCA+加州 ARL+FTC§5 仍底线,FTC 2026-03 已发 ANPRM 重新立法 → **按被撤销规则的实质前瞻建设**。

---

## 9. 前端

- **结账（仅 USD 订阅意图）**:续费披露块 + 独立同意勾选;CNY/一次性结账跳过。
- **账单页**:USD 订阅显示**下次扣费日 + 「取消自动续费」**;CNY 显示**「续费/续期」CTA（任何时候可点,时长顺延）**。
- **定价页**:USD「按月自动续费,随时可取消」;CNY「一次性,到期手动续,可提前续、时长顺延」。
- 走现有 i18n（`billing` namespace 已存在）;不得内联 CJK（cjk-guard）。

---

## 10. 分阶段（按 CodeX 改版 + P0.5 快赢）

- **P0 决策**:续费轨 Paddle/PayPal + 路由 + past_due 权益 + 历史用户策略 + 同档自由续费/顺延语义（§12）。
- **P0.5 快赢（可先单独上）**:放开同档自由续费 + 时长顺延 + 积分顺延（§4,纯改一次性模型、无 provider 订阅、无合规增量）→ 立刻给用户价值。
- **P1 schema + 状态机 + 合规最小闭环 + shadow 权益审计**:alembic 045（续费列 + open-subscription 约束 + consent 表）+ 模型同步 + 契约测试;`upsert_active_subscription` auto_renew 感知;**结账披露+独立同意+同意快照+取消入口+开通确认邮件最小闭环**（CodeX#2 上线门）;shadow 权益审计（§7）;**默认全 inert**。
- **P2 Paddle recurring 隐藏开关接入**:recurring price + 反转 drift 门 + **transaction/subscription 两条流**（CodeX#5）+ 取消端点 + `GET` 兜底对账。
- **P3 subscription 对账 sweeper + past_due 处理 + 退款单期化**。
- **P4 前端 + 灰度开关 + 小额真金 E2E**（开通→续费→取消→退款全链路）。

---

## 11. 风险 / 红线

1. **历史用户突然过期**（CodeX#1）——权益周期化前必须 shadow 审计 + grandfather/宽限策略,否则一次性老用户被误降级。
2. **续费 charge 缺新 order 会被静默丢弃**（去重+终态守卫）——每期先建 order;`subscription.*` 不伪造 order（CodeX#5）。
3. **合规漏建 = 直接法律暴露**（CodeX#2）——PayPal 路径我们是 merchant,披露/同意/取消/提醒缺一是 FTC§5/州 AG/ARL 私诉风险;同意快照必须结账时落,事后补不了。
4. **MoR 不免责**（CodeX#3）——Paddle 担税务/争议,但披露+取消 UX+文案仍我们的（Paddle 自己就因披露被罚）。
5. **状态约束打架**（CodeX#4）——past_due/paused 必须纳入「open 订阅」唯一约束,否则同用户多活跃合同。
6. **币种混淆**（CodeX#6）——USD 续费分字段记 gross/tax/net,不塞 amount_cny。
7. **CLAUDE.md 付费 API 硬约束**——失败续费不跨轨自动重扣;past_due 显式。
8. **dropped CANCELLED webhook**——必须 `GET` 对账。
9. **CNY「手动续」不得悄变免密快捷续**——每笔 CNY 须用户显式发起,否则重触代扣资质 + negative-option 义务。

---

## 12. 待项目主拍板的开放问题

- **Q1 续费轨**:Paddle（推荐,省合规）还是 PayPal Subscriptions（保持 PayPal 主轨但自背全合规+税务）?「订阅」意图路由到谁?
- **Q2 CNY 永远无自动扣款?**（影响 `auto_renew`/`provider_subscription_id` 放共享 `Subscription` 还是 USD-only 兄弟表）
- **Q3 past_due 权益**:dunning 窗口（最多 30 天）保留权益（利营收）还是立即限制（防滥用）?
- **Q4 EU 客户**:走 Paddle（吸收 14 天撤回+EU 披露）还是 PayPal（自背）?
- **Q5 试用**:USD 订阅是否带 provider 托管试用?与现有内部 trial（`User.trial_*`）如何协调?
- **Q6 升降级**:in-place `PATCH /subscriptions`（proration）还是 cancel+resubscribe?
- **Q7 USD 续费价格源**:复用 `plan_catalog.*_usd_cents` 映射 provider recurring price,还是单独 recurring 价目表?
- **Q8 自由续费范围**:第一版只放开同档顺延（推荐）还是含跨档?积分 bucket 到期各算/统一?
- **Q9 历史用户**（权益周期化）:grandfather / 按末单周期过期 / 统一宽限期?

---

## 13. 研究来源 + 审核记录

- 架构（代码精读 file:line）：`billing.py`/`subscriptions.py`/`payment_providers.py`/`payment_provider_{paypal,paddle}.py`/`models.py`/`credits_service.py`/`entitlements.py`/`billing_reconciliation.py`。
- Paddle 订阅：developer.paddle.com（subscription-creation / provision-access-webhooks / cancel / dunning）。
- 合规：FTC ANPRM 2026-03、加州 ARL（2025-07-01 生效）、ROSCA、EU CRD/撤回按钮、微信代扣自助申请指引、Paddle/PayPal MoR-vs-PSP。
- **CodeX 审核（2026-06-27,6 条全采纳）**：① CNY 非零改动+历史用户审计 ② 合规提前为上线门 ③ MoR 表述收紧 ④ open-subscription 唯一约束 ⑤ transaction/subscription 两条流 ⑥ 币种字段收紧。
- **项目主补充**：自由续费 + 时长顺延（§4,P0.5 快赢）。
- ⚠️ `paypal_subscriptions` 研究 agent 输出退化为占位;PayPal Subscriptions 事实由 compliance agent 覆盖（BILLING.SUBSCRIPTION.* webhook、suspend/cancel、~8% CANCELLED 丢失、PayPal=PSP 自背合规）。**若 Q1 选 PayPal,实施前补一轮 PayPal Subscriptions API 专项核实。**
