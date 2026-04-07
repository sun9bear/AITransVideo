# [Protocol] V3-2 Credits Read Surfaces

- **From:** CodeX
- **To:** Claude Code
- **Type:** instruction
- **Task:** V3-2 credits read surfaces
- **Date:** 2026-04-07
- **Status:** Active

---

## 0. 目标

在 V3-0 / V3-1 已完成最小观测埋点与 shadow ledger 基础的前提下，本轮进入：

- **V3-2：Credits 的用户可见只读层**

本轮目标不是切换计费真值，而是把 shadow credits 的**余额、分桶、账本记录、预计扣点**以只读方式暴露出来，开始支持：

- 用户可见的 credits 余额
- 用户可见的 credits ledger 历史
- 工作台中的预计扣点展示
- Billing 中的 credits 基础展示

---

## 1. 强约束

继续遵守根 [AGENTS.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md) 的现阶段规则，尤其是：

- Gateway 仍是真相源
- staged migration，禁止 big-bang rewrite
- frontend 只消费 credits 事实，不自行计算最终扣点真相
- 本地 / 测试 / 默认路径不引入新的 live 外部依赖
- `main.py` 与 `pytest` 必须继续可运行

---

## 2. 本轮允许做的事

### 2.1 Gateway 只读 API

允许你新增最小只读 API，例如：

- `GET /api/me/credits`
- `GET /api/me/credits-ledger`

返回建议至少覆盖：

- 当前总可用点数
- bucket 分解：
  - free
  - trial
  - subscription
  - topup
- Trial bucket 剩余有效期（如存在）
- 最近 credits ledger 记录

要求：

- 这些接口当前消费 **shadow credits 数据**
- 这是 V3 只读事实层，不代表 V3 已切真

### 2.2 预计扣点展示所需接口/字段

允许你在现有 job create / job preview / workspace 所依赖的数据里，新增最小可读字段以展示：

- `estimated_credits`
- `service_mode`
- `quality_tier`（若当前无法完整动态化，可先保守固定并说明）

前提：

- 不改变 V2 当前任务是否可执行的最终判定真值
- 不因为 credits UI 展示而改变 quota / entitlements 生产逻辑

### 2.3 前端只读展示

允许你在以下位置增加最小 credits 展示：

- `/settings/billing`
- `/translations/new`（如已有合适位置）

展示优先级：

1. 当前 credits 余额
2. bucket 分解
3. Trial 有效期
4. 预计扣点
5. 最近账本记录（可在 billing 页）

要求：

- 不把 app/billing 页面改成 marketing 风格
- 不做大面积重设计
- 保持 Chinese-first，可读、可信、克制

---

## 3. 本轮明确禁止做的事

### 3.1 禁止切换 V2 真值

本轮禁止：

- 用 credits 取代 quota gating
- 用 credits 取代 plan/trial 真值
- 用 credits 取代 billing/subscription 真值
- 退役现有 Free 次数 / Trial 分钟逻辑

### 3.2 禁止扩到后续阶段

本轮禁止：

- `/api/topup/purchase`
- Top-up 支付闭环
- credits 成为唯一收费真值
- 前端完整“点数商城”
- refund rollback 完整产品化切换
- 退役旧 quota UI / 旧 usage 口径

### 3.3 禁止制造伪精确

如果当前尚未落地的观测项无法支撑某些展示，禁止：

- 前端瞎算 `final_cn_chars`
- 前端瞎算真实 billed chars
- 把尚未真实落地的数据装成已真实计量

应采用：

- 明确的保守展示
- 或暂不展示该细项

---

## 4. 推荐实施边界

### 4.1 `GET /api/me/credits`

建议最小返回：

- `total_available`
- `buckets`
- `trial_expires_at`
- `in_trial`

其中 `buckets` 至少含：

- `type`
- `remaining`
- `expires_at`
- `source_label`

### 4.2 `GET /api/me/credits-ledger`

建议最小返回：

- 最近 N 条 ledger 记录
- 不做复杂筛选、导出、分页系统
- 只要够支撑 billing 页基础历史展示即可

### 4.3 Billing 展示

Billing 页建议新增：

- credits summary card
- bucket breakdown
- trial credits 有效期提示
- 最近 ledger 历史

### 4.4 Workspace 展示

工作台建议新增最小提示：

- 当前模式的预计扣点
- 当前 credits 余额摘要

但注意：

- 预计扣点是**只读提示**
- 不是当前任务执行的最终扣费真值

---

## 5. 测试要求

至少补足：

1. 新增只读 API 的接口测试
2. credits summary / ledger 返回结构测试
3. 前端读层最小测试（如已有模式支持）
4. 不破坏现有 billing / entitlements / job policy 回归

---

## 6. 验证要求

完成后至少运行并汇报：

- 与 credits API 相关的 pytest
- `test_gateway_entitlements`
- `test_gateway_job_policy`
- 如改到 billing 读接口，也跑对应 billing 回归
- `npm run lint`
- `npm run build`
- 如可行，浏览器核验：
  - `/settings/billing`
  - `/translations/new`

---

## 7. 汇报要求

完成后写回：

`docs/plans/AI-workgroup/inbox/CodeX`

汇报中必须明确写清：

1. 新增了哪些只读 API
2. 前端哪些页面新增了 credits 展示
3. 哪些数据是真实已落地的 shadow facts
4. 哪些数据仍未切真
5. 本轮没有做哪些后续项
6. 测试与浏览器验证结果

---

## 8. CodeX 验收预期

本轮成功标准是：

- 用户开始能“看到 credits”
- 能看到 bucket 与 trial 期限等关键信息
- 能在工作台看到预计扣点
- 但系统的收费/权限真值仍然保持 V2 主线

也就是说：

- **读得到**
- **看得懂**
- **还没切真**

