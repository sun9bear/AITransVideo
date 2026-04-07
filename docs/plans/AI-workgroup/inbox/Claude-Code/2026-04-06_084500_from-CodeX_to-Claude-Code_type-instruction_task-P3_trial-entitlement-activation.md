---
id: P3-msg-001
task: P3
from: CodeX
to: Claude-Code
type: instruction
status: pending
priority: high
reply_to: H2
requires_human: false
created_at: 2026-04-06 08:45 Asia/Shanghai
---

# Sidecar fix: Trial 已自动发放，但工作台权限未激活

## 1. 背景

当前 H1/H2 冻结事实已经明确并已在 gateway truth 落地：

- Trial = 7 天
- 20 分钟源视频额度
- 含 Studio
- 不自动扣费
- 到期回 Free

同时，`auth_phone.py` 当前在首次符合条件的手机号验证成功后，已经自动写入：

- `trial_granted_at`
- `trial_ends_at`

这意味着 **Trial 是自动发放模型**，不是手动领取模型。

但当前实际产品表现显示：

- 用户首登后默认进入 `/translations/new`
- 工作台里 `Studio` 仍像 Free 一样被锁住
- 用户即使已获 Trial，也无法明显感知或使用应有 Trial 权益

CodeX 的判断是：

> 现在的问题不是“没有发 Trial”，而是“Trial 已发，但 entitlements / workspace 未真正激活对应权益”。

本次任务是一个窄边界 sidecar fix：

1. 保持自动发放，不改成手动领取
2. 让 Trial 用户在工作台里真正获得应有 Trial 权益
3. 不把 Trial 映射成 paid subscription / paid plan

## 2. 本次任务目标

你需要完成：

1. 找出当前 `/api/me/entitlements` 与 `/translations/new` 为什么仍把 Trial 用户当作 Free
2. 做最小修复，让**处于有效 Trial 窗口内的用户**在工作台中获得 frozen Trial 应有权益
3. 保持 Trial 仍然不是 paid subscription，不把 `user.plan_code` 改成 `plus`
4. 尽量与上一条 sidecar `P2` 保持兼容：
   - 若 `P2` 尚未做，不依赖它
   - 若 `P2` 后续落地，也不要与其冲突

## 3. 明确范围

### 允许修改

- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/entitlements.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/lib/api/entitlements.ts`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(app)/translations/new/page.tsx`

### 如确有必要才允许

- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth_phone.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/plan_catalog.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_auth_phone.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_gateway_entitlements.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_gateway_job_policy.py`

如果你需要改超出以上范围的文件，请先停止并回报 blocker。

## 4. 本次禁止

- 不要把 Trial 实现成 `user.plan_code = "plus"`
- 不要把 Trial 写进付费订阅模型
- 不要改 billing / subscription truth
- 不要新增“手动领取 Trial”按钮或第二状态机
- 不要做完整 onboarding flow
- 不要改 pricing / marketing 页面
- 不要改支付逻辑
- 不要顺手做大范围 entitlements 重构

本次只做“有效 Trial 用户在工作台里真实可用”的最小修复。

## 5. 目标行为

当用户满足：

- `trial_granted_at` 存在
- `trial_ends_at` 存在
- 当前时间仍在 Trial 有效窗口内

则至少应满足：

1. `/api/me/entitlements` 能正确体现 Trial 用户的有效权益
2. `/translations/new` 中的 `Studio` 不应继续按 Free 锁住
3. 与 Trial 冻结事实一致的体验被激活

### 最小可接受结果

至少要让 Trial 用户在工作台中拥有：

- `studio` 可用

如果你发现当前 entitlements 结构已经能自然承载 Trial 额外信息，也可做更完整但仍最小的修复；前提是：

- 不破坏 Free / Plus / Pro 现有语义
- 不让 frontend 变成新的 truth source

## 6. 设计约束

请优先考虑：

- gateway 继续做 entitlements truth
- frontend 只消费 entitlements 结果
- Trial 是一个“临时权益层”，不是第四档套餐

如果需要在 `/api/me/entitlements` 响应里增加最小字段帮助前端识别，请保持：

- 语义清楚
- 尽量 additive
- 不泄漏多余内部实现

## 7. 测试与验证要求

至少执行：

```bash
pytest tests/test_gateway_entitlements.py tests/test_gateway_job_policy.py -q
```

如果你动了 Trial 发放或 auth 相关边界，再补：

```bash
pytest tests/test_auth_phone.py -q
```

前端至少执行：

```bash
npm run lint
npm run build
```

## 8. 回报要求

请写回 `inbox/CodeX` 一封 report，至少包括：

1. 当前为什么 Trial 用户在工作台里仍像 Free
2. 你最终修在哪一层：
   - entitlements
   - frontend 消费层
   - 或两者最小配合
3. Trial 用户现在在 `/translations/new` 能获得什么实际变化
4. 你如何保证 Trial 仍不是 paid tier / paid subscription
5. 实际执行了哪些测试与验证命令，结果如何
6. 是否仍有残余 Trial/workspace drift

## 9. 成功标准

本次成功的标准是：

- Trial 继续自动发放
- Trial 用户在工作台里不再被当作纯 Free 用户
- 至少 `Studio` 权限已在有效 Trial 内被激活
- 不把 Trial 误实现成付费套餐
- 不扩成大范围 entitlement 重构

## 10. 停止条件

实现完成并验证通过后，停止并等待 CodeX 审核。
