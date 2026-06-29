# USD 续费 / 自由续费顺延 — ship-unit 任务索引（USR-00-INDEX）

> 母方案：[`docs/plans/2026-06-27-usd-recurring-subscription-plan.md`](../2026-06-27-usd-recurring-subscription-plan.md)（v3,Q1-Q9 已锁）。
> 本目录把方案拆成可执行 ship-unit。每单元：Step 0 确认现状 → 分步改动(file:line+验收命令) → 测试 → 回滚 → DoD。

## DAG / 状态

| 单元 | 内容 | 阶段 | 状态 | 依赖 |
|---|---|---|---|---|
| **USR-01** | **同档自由续费 + 时长顺延 + 积分 FIFO** | P0.5 快赢 | **flesh'd,待开工** | 无（纯改一次性模型） |
| USR-02 | schema(alembic 045) + 状态机 + open-subscription 约束 + 币种字段 + consent 表 + 90天宽限 shadow 审计 | P1 | 占位 | USR-01 |
| USR-03 | 合规最小闭环（披露+独立同意+同意快照+取消入口+开通确认邮件,**上线门**） | P1 | 占位 | USR-02 |
| USR-04 | Paddle recurring 隐藏开关接入（recurring price + 反转 drift + preprocessor + transaction/subscription 两条流 + 取消端点） | P2 | 占位 | USR-02/03 |
| USR-05 | 对账 sweeper（GET 优先）+ past_due 7天软宽限 + 退款单期化 | P3 | 占位 | USR-04 |
| USR-06 | 前端（结账披露/同意、账单页下次扣费+取消、定价文案）+ 灰度 + 小额真金 E2E + EU 入口 gate | P4 | 占位 | USR-04/05 |

**DAG**：USR-01（独立可先上）；USR-02 → USR-03 → USR-04 → USR-05 → USR-06。

## 全局红线（所有单元）

1. **付费 API 硬约束**：失败续费**不**跨轨自动重扣；`past_due` 显式可见；渠道用户显式选、不自动 fallback。
2. **USD 锚定**：USD 金额分字段（`provider_gross/tax/net_minor`+`currency`+`internal_amount_cny`），**永不塞 `amount_cny`**。
3. **内部状态 enum 统一 `cancelled`**（adapter 映射 Paddle `canceled`）。
4. **续费必经 preprocessor 先建本期 PaymentOrder**，`_process_payment_event` 不自己造单；`subscription.*` 不伪造钱事件。
5. **金融 schema migration**：模型 `__table_args__` 同步 + 契约测试 + 生产 upgrade 走维护窗口（按 credits 044 纪律）。
6. **前端 i18n**：用 `billing` namespace,**不内联 CJK**（cjk-guard）。
7. **默认 inert**：续费/合规件未配开关不启用。

## 决策（Q1-Q9 已锁,见母方案 §12）

Paddle 唯一续费轨 / CNY 永无自动扣款 / past_due 7天软宽限 / EU 只走 Paddle / v1 不接 provider 试用 / v1 不做 proration / plan_catalog USD 为真源 / 自由续费只放同档顺延+bucket FIFO 各算各的 / 历史用户 90天统一宽限。
