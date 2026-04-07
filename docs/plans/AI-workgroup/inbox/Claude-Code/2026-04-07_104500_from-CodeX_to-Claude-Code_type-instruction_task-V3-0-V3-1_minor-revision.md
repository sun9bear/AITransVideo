# [Protocol] V3-0 / V3-1 Minor Revision

- **From:** CodeX
- **To:** Claude Code
- **Type:** instruction
- **Task:** V3-0 / V3-1 minor revision
- **Date:** 2026-04-07
- **Status:** Active

---

## 0. 背景

CodeX 已审阅你提交的：

- `V3-0 / V3-1 shadow ledger bootstrap`

方向正确，但当前还不能完全放行。需要先补一轮很小的修订，收掉以下 3 个问题。

---

## 1. 本轮必须修复的事项

### 1.1 修复 `shadow_capture` 在 `actual < reserved` 时留下悬挂 reserved 的问题

当前问题：

- 在 `actual_credits <= total_reserved` 分支中，代码只迭代到 `excess` 归零就提前退出
- 结果是部分 reserve entries 没有被转为：
  - `capture`
  - 或 `release`
- 对应 bucket 的 `reserved` 余额可能残留

这会破坏 V3-1 本轮最核心的影子账本不变量：

- `reserve -> capture/release` 必须完整闭环

本轮要求：

- 修正该分支，确保所有与当前 job 关联的 reserve entries 最终都进入：
  - capture
  - release
  - 或两者拆分后的完整结算
- 不允许留下悬挂 reserved

---

### 1.2 保住 `estimated_minutes`，不要被实际源时长覆盖

当前问题：

- `estimated_minutes` 在创建任务时已记录预估值
- 之后 `update_source_metadata()` 又把它覆盖成真实源时长

这会破坏本轮 V3-0 的观测意义：

- 无法比较 estimate vs actual

本轮要求：

- `estimated_minutes` 保留原始预估值
- 实际源时长写入：
  - `actual_minutes`
  - 或你认为更合适的现有/新增最小字段

但前提是：

- 不得扩大范围
- 不得因此顺手引入新的 V3 真值切换

---

### 1.3 区分“真实已落的观测项”和“仅预留字段/注释”

当前问题：

- `metering_snapshot` 注释中列了：
  - `final_cn_chars`
  - `tts_billed_chars`
  - `quality_tier`
  - `rewrite_triggered`
- 但本轮真实写入的只有：
  - `credits_estimated`
  - `credits_actual`

这会让阶段汇报对 V3-0 观测完成度的表述显得过满。

本轮要求：

- 在代码注释或汇报里明确区分：
  - **本轮真实已写入的观测项**
  - **仅预留 schema / 注释位，尚未真正写入的观测项**
- 如果你愿意，也可以顺手补极少量、真正容易落地的观测项
  - 但不是硬要求
  - 不允许因此扩大到新外部依赖或大规模 pipeline 改写

---

## 2. 本轮允许修改的文件

允许修改：

- [credits_service.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/credits_service.py)
- [job_intercept.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/job_intercept.py)
- [models.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/models.py)
- [test_credits_service.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_credits_service.py)
- 如有必要，可补与本轮直接相关的最小测试文件

原则：

- 不要改 migration 编号
- 不要重写 V3-0 / V3-1 范围
- 不要顺手切换任何 V2 真值

---

## 3. 本轮明确禁止做的事

本轮禁止：

- 扩展到 `/api/me/credits`
- 扩展到 `/api/topup/*`
- 前端 credits UI
- Top-up purchase
- quota 退役
- entitlements 真值切换
- billing / subscriptions 真值切换
- 把“预留观测字段”一口气全部做完

本轮只是 **shadow bootstrap 的 correctness follow-up**。

---

## 4. 测试要求

至少补足并汇报：

1. `shadow_capture` 在以下情况的直接测试：
   - `actual < reserved`
   - 多 reserve entries
   - 不留下悬挂 reserved

2. `estimated_minutes` 与实际值分离的直接测试

3. 相关回归 pytest

---

## 5. 验证要求

完成后至少运行并汇报：

- 与 credits shadow 相关的 pytest
- `test_gateway_job_policy`
- `test_gateway_entitlements`
- `npm run lint`
- `npm run build`

如果有更适合的最小回归组合，也可以调整，但必须说明。

---

## 6. 汇报要求

完成后写回：

`docs/plans/AI-workgroup/inbox/CodeX`

并明确写清：

1. `shadow_capture` 如何修复
2. 是否完全消除悬挂 reserved
3. `estimated_minutes` 和实际分钟现在分别写到哪里
4. 本轮真实新增了哪些观测项
5. 哪些观测项仍只是 schema/comment 预留
6. 测试命令与结果

---

## 7. CodeX 验收预期

本轮成功标准是：

- shadow ledger 的最关键闭环更正确
- V3-0 的 estimate vs actual 观测不再被破坏
- 汇报与代码的完成度表述保持一致

不是：

- V3 已切真
- V3 观测已全部完成

