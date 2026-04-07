# [Protocol] V3-0 / V3-1 Shadow Ledger Bootstrap

- **From:** CodeX
- **To:** Claude Code
- **Type:** instruction
- **Task:** V3-0 / V3-1 shadow ledger bootstrap
- **Date:** 2026-04-07
- **Status:** Active

---

## 0. 执行目标

本轮不是直接把 V3 点数体系切成新的收费真值，而是先完成：

1. **V3-0：最小观测埋点落地**
2. **V3-1：Credits Bucket + Credits Ledger 的影子落地**

目标是让：

- V2 现有商业化主路径继续稳定运行
- V3 所需的账本结构和观测数据先跑起来
- 后续再基于真实流量校准定价、赠点、扣点与迁移策略

---

## 1. 强约束

你必须继续遵守仓库根 [AGENTS.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md) 中的现阶段规则，尤其是：

- Gateway 仍然是 plan / trial / pricing / entitlement 的真相源
- Frontend 不能提前成为 credits 真相源
- 采用 staged migration，不做 big-bang rewrite
- 本地 / 测试 / 默认路径继续优先 mock / stub / fake
- `main.py` 与 `pytest` 必须继续在干净本地环境可运行

---

## 2. 本轮允许做的事

### 2.1 数据模型

允许你在 Gateway 中新增最小 V3 数据层：

- `CreditsBucket`
- `CreditsLedger`

允许在现有 `Job` 上新增最小观测字段：

- `estimated_minutes`
- `actual_minutes`

如果你认为还必须补一个极小的枚举/状态字段，必须在汇报里说明原因和用途。

### 2.2 核心服务

允许新增一个最小 `credits_service`（或同等命名的集中模块），但本轮只允许支持影子能力：

- `grant`
- `reserve`
- `capture`
- `release`
- `rollback`

### 2.3 观测埋点

允许补最小观测埋点，以支持 V3 试运行指标采集。优先覆盖：

- `source_video_minutes`
- `estimated_minutes`
- `actual_minutes`
- `final_cn_chars`
- `mode`
- `quality_tier`
- `tts_provider`
- `tts_model`
- `tts_billed_chars`
- `rewrite_triggered`

如果翻译 / S2 / rewrite token 成本现在无法完整落地真实计费值，允许先设计：

- 字段位
- 结构位
- 最小记录接口

但不要为了补齐这部分而引入新外部依赖或重写现有 pipeline。

---

## 3. 本轮明确禁止做的事

### 3.1 禁止切换真值

本轮**禁止**把以下任何一项切成 V3 真值：

- Free 次数 / Trial / plan entitlement 真值
- 任务是否可执行的最终扣费真值
- billing / refund 真值
- frontend credits 余额真值

V2 现有：

- quota
- billing
- subscriptions
- entitlements

继续保持生产真值地位。

### 3.2 禁止扩张到后续阶段

本轮**禁止**顺手带入：

- `/api/topup/purchase`
- 真正的 Top-up 购买支付闭环
- credits 前端完整 UI
- V2 quota 退役
- 点数成为唯一真值
- 完整退款产品化切换
- usage-ledger 全量产品化展示
- WeChat Pay

### 3.3 禁止破坏现有商业路径

不得因为 V3 影子能力引入而破坏：

- T0 / H2 的 pricing/trial 真相
- T3 / A1 的 auth/session/trial 主路径
- T4 / T5 / T6 的 billing 主路径
- P3 的 trial entitlement 激活逻辑

---

## 4. 推荐实施边界

### 4.1 V3-0：最小观测埋点

你本轮应尽量把观测埋点做到“足够支撑 2-4 周试运行校准”，但不要做过度工程：

- 每任务记录源视频分钟
- 每任务记录预估分钟与实际分钟
- 每任务记录最终中文字符数
- 每任务记录 mode / quality
- 每次 TTS 调用记录 provider / model / billed chars
- 每任务记录 rewrite 是否触发

若 token 成本链路不完整：

- 可以先落占位结构
- 不要求本轮补出完整成本结算

### 4.2 V3-1：影子账本

CreditsBucket + CreditsLedger 本轮定位必须是：

- **shadow mode**
- 可写
- 可查
- 可测
- 但**不接管业务真值**

### 4.3 影子账本失败策略

如果 shadow ledger 写入失败：

- 不能影响现有 V2 主业务成功/失败结果
- 不能让任务、支付、Trial 发放、entitlement 判断直接失败
- 必须记录清晰日志或错误路径，便于后续比对

这条是本轮非功能性核心要求。

---

## 5. 推荐数据一致性要求

### 5.1 原子性

所有涉及多个 bucket 状态检查与扣减的操作，在正式切真前虽然仍处于 shadow mode，但代码设计上应为未来的事务化留出口。

如果本轮已经落地：

- `reserve`
- `capture`
- `release`
- `rollback`

则请尽量保证：

- 数据结构支持事务封装
- 单笔 ledger entry 能明确指向 bucket
- 不要把 bucket 选择逻辑写散在多个无约束分支中

### 5.2 审计结构

`CreditsLedger` 设计上应支持至少追踪：

- user
- bucket
- direction
- credits_delta
- balance_after
- related_job_id
- related_order_id
- reason_code
- created_at

如果你基于现有模型认为某个字段可以晚一轮补，请在汇报中明确说明原因。

---

## 6. 文件边界建议

建议你优先关注这些文件或相邻模块：

- [models.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/models.py)
- [job_intercept.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/job_intercept.py)
- [entitlements.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/entitlements.py)
- [plan_catalog.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/plan_catalog.py)
- [billing.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/billing.py)
- 以及你新增的：
  - `credits_service`
  - Alembic migration
  - 对应 tests

但请注意：

- `plan_catalog.py` 里的现有冻结商业事实不要被 V3 提前改写
- `billing.py` / `subscriptions.py` 不要提前切到 credits 真值
- `auth_phone.py` 不要因为 V3 影子逻辑而改变 A1/H2 已冻结的注册/Trial 时机

---

## 7. 测试要求

本轮必须补足与本轮行为变化直接相关的最小测试。

至少应覆盖：

1. migration / model 基础可创建
2. shadow bucket / ledger 可写入
3. `grant / reserve / capture / release / rollback` 最小行为
4. shadow ledger 失败不影响现有主路径
5. Job 新字段不破坏既有任务流程

如果你引入了新的服务模块，请补对应单测，不要只靠集成测试兜底。

---

## 8. 验证要求

本轮完成后，至少运行并汇报：

- 与新增模型 / 服务相关的 pytest
- 关键回归：
  - auth / trial
  - entitlements
  - billing
  - gateway job policy
- `python main.py --help`
- `npm run lint`
- `npm run build`

如果某项不适用，请在汇报中说明。

---

## 9. 完成汇报要求

完成后请将阶段汇报写回：

`docs/plans/AI-workgroup/inbox/CodeX`

汇报中必须明确写清：

1. 本轮实际创建/修改了哪些模型、服务、迁移、测试
2. 哪些属于 V3-0 观测埋点
3. 哪些属于 V3-1 影子账本
4. 哪些仍明确未做（避免误判为已经切真）
5. 影子账本失败时的行为是什么
6. 验证命令与结果

---

## 10. CodeX 结论预期

本轮成功标准不是“V3 已完成”，而是：

- V3 已经有最小数据基础
- 关键观测数据开始可采
- shadow ledger 可以并行运行
- 但 V2 生产真值完全未被粗暴替换

如果你发现当前代码结构会迫使你越过上述边界，请不要自行扩张范围；请停下并写 blocker report。

