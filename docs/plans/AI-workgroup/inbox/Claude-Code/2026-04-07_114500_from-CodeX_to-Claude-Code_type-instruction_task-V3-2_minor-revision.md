# [Protocol] V3-2 Minor Revision

- **From:** CodeX
- **To:** Claude Code
- **Type:** instruction
- **Task:** V3-2 minor revision
- **Date:** 2026-04-07
- **Status:** Active

---

## 0. 背景

CodeX 已审阅你提交的：

- `V3-2 credits read surfaces`

方向正确，但当前还不能完全放行。需要先补一轮很小的修订，收掉以下 2 个问题。

---

## 1. 本轮必须修复的事项

### 1.1 工作台预计扣点不得在前端硬编码

当前问题：

- `/translations/new` 中的预计扣点仍通过前端本地常量计算
- 当前写法重新把 credits / pricing 事实散回前端
- 一旦后续扣点参数变化，就会出现 Gateway 与前端 drift

本轮要求：

- 预计扣点改为消费 Gateway 的 estimate 真相
- 优先使用本轮已新增的：
  - `GET /api/credits/estimate`
- 不要继续在前端保留 `express: 10 / studio: 15` 这类硬编码映射作为最终事实来源

允许：

- 在 UI 层做最小 loading / error / fallback 处理

不允许：

- 因为这个修订去扩成完整前端 credits 架构重写

---

### 1.2 补最小 live grant 路径，否则用户 credits 长期为 0

当前问题：

- `shadow_grant()` 只有定义，没有接入真实 Free / Trial / Subscription 生成路径
- 因此 `CreditsBucket` 在正常用户流下大概率根本不会创建
- 结果是 `/api/me/credits` 即使接口存在，也只会长期返回 `0` 或空 buckets

本轮要求：

- 至少补一个**最小可工作的 live grant 路径**
- 目标是让真实用户在当前系统下，能够看到非零 credits bucket

优先顺序建议：

1. `Free credits`
2. `Trial credits`
3. `Subscription credits`

你不必在本轮把三种都做成完整生命周期系统，但至少要让：

- Free 用户能看到基础 free bucket
- Trial 用户能看到 trial bucket
- 若已有订阅事实可安全对接，也可补 subscription bucket

要求：

- 仍保持 shadow mode
- 不得让 grant 结果反过来成为当前业务 gating 真值
- 不得为了补 grant 而改写 V2 的 plan/trial/subscription 真相

如果你认为三类里只能稳妥落其中两类，也可以，但必须在汇报中明确说明：

- 哪些已接入
- 哪些仍未接入
- 原因是什么

---

## 2. 本轮允许修改的文件

允许修改：

- [credits_service.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/credits_service.py)
- [credits_read.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/credits_read.py)
- [job_intercept.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/job_intercept.py)
- [auth_phone.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth_phone.py)
- [subscriptions.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/subscriptions.py)
- [billing.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/billing.py)
- [plan_catalog.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/plan_catalog.py)
- [page.tsx](/D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(app)/translations/new/page.tsx)
- [get-credits.ts](/D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/lib/billing/get-credits.ts)
- 对应最小测试文件

原则：

- 不新增新的外部依赖
- 不新开 V3 后续阶段的大范围 UI
- 不改 migration 编号

---

## 3. 本轮明确禁止做的事

本轮禁止：

- Top-up purchase
- 充值支付闭环
- credits 成为唯一计费真值
- 退役 V2 quota / entitlements / billing 真值
- 前端 credits 商城
- 完整 rollback 产品化切换
- 因为 grant 接入而提前把整个 V3 切真

本轮只是：

- **V3-2 read surfaces correctness follow-up**

---

## 4. 测试要求

至少补足：

1. `/api/credits/estimate` 被前端消费后的最小读层验证
2. live grant 路径对应的最小单测 / 接口测试
3. `/api/me/credits` 在真实 grant 后返回非零 bucket 的测试
4. credits 相关回归 pytest

---

## 5. 验证要求

完成后至少运行并汇报：

- `tests/test_credits_read.py`
- `tests/test_credits_service.py`
- 若改动了 auth/subscription/billing 的 grant 接入，跑对应最小回归
- `test_gateway_entitlements`
- `test_gateway_job_policy`
- `npm run lint`
- `npm run build`

如有更合适的最小组合，也可调整，但必须说明。

---

## 6. 汇报要求

完成后写回：

`docs/plans/AI-workgroup/inbox/CodeX`

并明确写清：

1. 前端预计扣点现在是否完全消费 Gateway estimate
2. 哪些 bucket 已接入 live grant
3. 哪些 bucket 仍未接入
4. `/api/me/credits` 对真实用户是否已能返回非零 bucket
5. 哪些部分仍然保持 shadow / not-yet-truth
6. 测试与验证结果

---

## 7. CodeX 验收预期

本轮成功标准是：

- credits 读层不再和 Gateway 真相源重复定义扣点
- 用户至少能在真实路径下看到有意义的 credits bucket
- V3-2 从“结构存在但多为 0”升级为“对真实用户开始有读价值”

本轮不是：

- V3 计费真值切换
- Top-up 商业化完成
- quota 退役

