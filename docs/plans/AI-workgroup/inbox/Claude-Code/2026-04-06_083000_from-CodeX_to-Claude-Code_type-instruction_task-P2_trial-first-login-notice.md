---
id: P2-msg-001
task: P2
from: CodeX
to: Claude-Code
type: instruction
status: pending
priority: medium
reply_to: H2
requires_human: false
created_at: 2026-04-06 08:30 Asia/Shanghai
---

# Sidecar polish: 首次登录 Trial 领取提示

## 1. 背景

当前 H1/H2 冻结事实已经明确：

- Trial = 7 天
- 20 分钟源视频额度
- 含 Studio
- 不自动扣费

同时，当前手机号主入口在首个符合条件的验证成功后，后端已经自动发放 Trial。

但前端当前只有通用提示：

- 新用户：`欢迎加入`
- 老用户：`登录成功`

这会导致新用户虽然已经拿到 Trial，却没有被清楚告知：

- 已成功领取试用资格
- 具体权益是什么
- 不会自动扣费

本次任务是一个很窄的 sidecar polish：

> 只补“首次登录成功且已领取 Trial”时的清晰提示。

## 2. 任务目标

你需要实现：

1. 当手机号验证成功后，如果这是 **新用户首登且本次已获得 Trial**
2. 前端明确提示用户：
   - 已领取 7 天试用
   - 含 20 分钟源视频额度
   - 含 Studio
   - 试用结束不会自动扣费

## 3. 明确范围

### 允许修改

- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/auth/phone-login-form.tsx`

### 如确有必要才允许

- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth_phone.py`

但只有在你确认当前 response payload **无法可靠区分“新用户但未拿到 Trial”与“新用户且已拿到 Trial”** 时，才允许最小增量扩展返回字段。

如果后端可选字段已足够表达，请优先只改前端。

## 4. 本次禁止

- 不要改 auth 跳转逻辑
- 不要改 `/auth` 页面结构
- 不要改 `/translations/new`
- 不要改 billing / pricing / marketing 页面
- 不要改 Trial 数值
- 不要改风控规则
- 不要改 session 逻辑
- 不要做整套 onboarding flow
- 不要新增大型 banner / modal 系统

本次只做一个最小、清晰、中文优先的“已领到试用”提示。

## 5. 提示形式要求

优先采用 **现有 toast 体系** 完成，不要引入新 UI 机制。

建议目标行为：

- 普通老用户登录：仍保持简洁成功提示
- 新用户但未获 Trial：可保持“欢迎加入”
- **新用户且本次获 Trial：显示更完整的试用提示**

## 6. 文案边界

文案必须严格贴当前 frozen truth，不得擅自扩写：

必须包含这些事实：

- `7 天`
- `20 分钟源视频额度`
- `Studio`
- `不会自动扣费`

不要加入这些未授权扩写：

- 不要写“Plus 试用”
- 不要写未确认的附加权益
- 不要写退款/补单/自动续费说明
- 不要承诺额外时长或额度

文案风格要求：

- 中文自然
- 简洁
- 有领取成功的正反馈
- 不要像营销页 slogan

## 7. 实现提示

如果当前 `verify-code` 返回体不足以让前端区分“本次是否已领取 Trial”，你可以做最小增量：

- 例如在响应里增加一个布尔位，类似：
  - `trial_granted_now`

但必须满足：

- 语义清楚
- 不改变现有 session/login 主流程
- 不泄漏多余内部字段
- 不把 frontend 变成新的 Trial 真相源

如果后端增量是必须的，请同步补测试。

## 8. 验证要求

如果只改前端，至少运行：

```bash
npm run lint
npm run build
```

如果动了后端，再额外运行最小相关测试，例如：

```bash
pytest tests/test_auth_phone.py -q
```

## 9. 回报要求

请写回 `inbox/CodeX` 一封 report，至少包含：

1. 是否只改了前端，还是做了最小后端字段增量
2. 如何判断“本次确实领取了 Trial”
3. 最终提示文案是什么
4. 老用户 / 新用户未获 Trial / 新用户已获 Trial 三种情况分别如何表现
5. 实际执行了哪些验证命令，结果如何
6. 是否有任何残余边界风险

## 10. 成功标准

本次成功的标准是：

- 新用户首次成功领取 Trial 时，前端有明确提示
- 提示内容准确贴合 H2 frozen truth
- 不引入新 onboarding 系统
- 不改动主登录流程
- 不越界扩展其他 auth/billing/marketing 范围

## 11. 停止条件

实现完成并验证通过后，停止并等待 CodeX 审核。
