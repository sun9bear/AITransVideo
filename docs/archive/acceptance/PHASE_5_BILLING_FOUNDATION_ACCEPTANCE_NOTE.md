# Phase 5 验收说明：支付订单与 Webhook 幂等基础设施

> 状态：**已放行**
> 放行日期：2026-03-30
> 适用范围：AIVideoTrans Web MVP / Gateway / billing.py

## 1. 背景与目标

Phase 5 的目标是搭建支付前置基础设施，让"订单创建 → 回调处理 → 用户权益生效"这条链路在不接入真实支付网关的前提下闭合。

本阶段不做真实 Stripe / Alipay / WeChat Pay 集成。使用 fake provider 模拟支付，验证订单状态流和 webhook 幂等逻辑。

## 2. 本轮范围

**包含：**
- `payment_orders` 表落地（ORM + Alembic 004）
- `payment_webhook_events` 表落地（ORM + 同一迁移）
- 订单创建 API（`POST /api/billing/orders`）
- 订单查询 API（`GET /api/billing/orders/{order_id}`）
- Webhook 接收 API（`POST /api/billing/webhooks/{provider}`）
- Fake pay 模拟支付（`POST /api/billing/fake-pay/{order_id}`）
- Webhook 幂等处理核心（`_process_payment_event`）
- 支付成功 → `users.plan_code` 升级 → 审计日志写入

**不包含：**
- 真实支付网关验签（Stripe signature / Alipay sign）
- 前端升级页面
- 退款 / 降级 / 过期订单清理
- `usage_ledger`
- 按分钟计费
- Phase 6 的任何内容

## 3. 已完成能力

### 3.1 数据模型

`payment_orders` 关键约束：
- `provider_order_id`: UNIQUE nullable（第三方订单号去重）
- `status`: server_default="created"
- `user_id`: FK → users.id, NOT NULL
- 索引：`idx_payment_orders_user_id`, `idx_payment_orders_status`

`payment_webhook_events` 关键约束：
- `provider_event_id`: UNIQUE NOT NULL（幂等核心约束，DB 层硬保证）
- `signature_valid`: BOOLEAN NOT NULL
- `processed`: BOOLEAN NOT NULL, default false

### 3.2 订单创建

- 校验 `target_plan_code`（只接受 plus / pro）
- 校验 `billing_period`（monthly / quarterly / annual）
- 校验 `provider`（stripe / alipay / wechatpay / fake）
- 拒绝降级或同级购买
- 价格表 6 档（PLAN_PRICES_CNY）

### 3.3 Webhook 幂等处理

`_process_payment_event` 是唯一的支付事件处理入口，`fake_pay` 和 `receive_webhook` 都调用它。

幂等保证：
- 以 `provider_event_id` 做 DB 唯一键去重
- 同一 event_id 重复到达 → 跳过，不重复升级
- 订单已在终态（paid / refunded / cancelled）→ 跳过
- 失败支付 → 只更新订单状态，不升级 plan_code

### 3.4 权益生效

支付成功时：
- 更新 `users.plan_code` 为 `target_plan_code`
- 写 `admin_audit_log`（action="payment_upgrade"）
- **不修改任何已创建任务的快照**

### 3.5 `signature_valid` 接口收口

`_process_payment_event` 的 `signature_valid` 是必传参数（无默认值），由入口层显式传入：

| 入口 | `signature_valid` 值 | 语义 |
|------|---------------------|------|
| `fake_pay()` | `True` | fake provider 不存在真实签名，标记为已验证 |
| `receive_webhook()` | `False` | Phase 5 无真实验签能力，标记为未验证 |

Phase 6 必须在 `receive_webhook` 入口先验签，再将真实验签结果传入 `_process_payment_event`。

## 4. 明确未完成项

| 项 | 说明 |
|----|------|
| 真实支付网关验签 | Phase 6 |
| 前端升级页面 | Phase 6 |
| 退款处理 | Phase 6+ |
| 订单过期清理 | 建议后续加定时任务 |
| 降级流程 | Phase 6+ |
| 按分钟精细计费 | 需要 `usage_ledger`，不在当前范围 |

## 5. 边界声明

- 支付系统只修改 `users.plan_code`，不修改已创建任务快照
- Gateway 仍是唯一商业规则入口
- `plan_code`、`role`、`service_mode`、`quota_state` 术语锁定不变
- 本阶段无真实支付网关联通，fake provider 是唯一可运行的支付路径
- `signature_valid=True` 仅出现在 `fake_pay` 路径；`receive_webhook` 路径在 Phase 5 中传 `False`

## 6. 关键实现文件

| 文件 | 职责 |
|------|------|
| `gateway/billing.py` | 订单创建、查询、fake pay、webhook 接收、`_process_payment_event` |
| `gateway/models.py` | `PaymentOrder` + `PaymentWebhookEvent` ORM |
| `gateway/alembic/versions/004_add_payment_tables.py` | 数据库迁移 |
| `gateway/main.py` | 注册 `billing_router` |

## 7. 测试与验证结果

测试文件：`tests/test_billing.py`，共 23 个测试。

| 类别 | 测试数 | 覆盖 |
|------|--------|------|
| 常量/价格表 | 4 | 计划、周期、渠道、价格完整性 |
| ORM 模型 | 4 | 字段存在性、unique 约束 |
| `create_order` 真实函数 | 4 | 成功、无效 plan、降级拒绝、未登录 |
| `_process_payment_event` 真实函数 | 6 | 升级成功、重复 webhook 幂等、已付跳过、未知订单、失败不升级、不触碰 job |
| `signature_valid` 参数验证 | 5 | 无默认值、True 记录、False 记录、webhook 传 False、fake_pay 传 True |

最终验证结果：**23/23 通过**。

## 8. 放行结论

Phase 5 已放行。支付前置基础设施的订单状态流、webhook 幂等、权益升级闭环均已成立并通过测试验证。当前仓库状态适合冻结在 Phase 5，等待 Phase 6 决策。
