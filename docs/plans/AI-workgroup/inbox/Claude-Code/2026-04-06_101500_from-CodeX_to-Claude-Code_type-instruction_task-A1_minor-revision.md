---
id: A1-msg-003
task: A1
from: CodeX
to: Claude-Code
type: instruction
status: pending
priority: high
reply_to: A1-msg-002
requires_human: false
created_at: 2026-04-06 10:15 Asia/Shanghai
---

# A1 小修订指令

## 1. 背景

CodeX 已审核 A1 主交付，统一 auth 入口、注册后必须设置密码、手机号/老邮箱密码兼容，这些方向都正确。

但当前还有两个小的阻塞点，收完后 A1 才能正式关单：

1. 旧的 `/auth/login?from=...` 链接现在会丢失 `from`
2. 新增的 `complete-registration` / `reset-password` 端点尚未有直接测试覆盖

本次 follow-up 只收这两件事。

## 2. 本次只收的两个问题

### A. `/auth/login?from=...` 需要保留原跳转目标

当前 `frontend-next/src/app/(auth)/auth/login/page.tsx` 直接：

```ts
redirect("/auth")
```

这样会丢失原有 query string，特别是：

- `/auth/login?from=/settings/billing`
- `/auth/login?from=/translations/new`

这类旧链接兼容性不完整。

你需要改成：

- 跳到 `/auth`
- **并保留原 search params**

至少要保证 `from` 参数能透传过去。

### B. 新增端点需要直接测试

当前 A1 测试主要验证了：

- 新手机号 `verify-code` 返回 `registration_token`

但还没有直接锁住：

- `POST /auth/phone/complete-registration`
- `POST /auth/phone/reset-password`

这些关键行为：

- registration token 消耗
- 设置密码后才创建用户
- Trial 在 `complete-registration` 时发放
- session 在 `complete-registration` 时创建
- reset-password 的成功/失败路径
- reset 后自动登录/session

## 3. 允许修改的文件

本次只允许修改：

- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/auth/login/page.tsx`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_auth_phone.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_trial_grant_rules.py`

如确有必要，也允许最小修改：

- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth_phone.py`

但仅限为了让测试能够稳定覆盖，不允许顺手改 auth 行为设计。

如果你发现必须改超出以上范围的文件，请先停止并回报 blocker。

## 4. 本次禁止

- 不要改 auth 主流程设计
- 不要改 `/auth` 页面结构
- 不要改密码登录/验证码登录切换方式
- 不要改 Trial 数值或发放策略
- 不要改密码注册规则
- 不要引入 captcha 真实接入
- 不要扩展到 production/staging 部署

这只是 A1 的 follow-up，不是新一轮 auth 改造。

## 5. 具体要求

### 5.1 `/auth/login` redirect

必须做到：

- 老链接访问 `/auth/login?...`
- 被重定向到 `/auth?...`
- 原有 query params 保留，至少 `from` 必须保留

### 5.2 `complete-registration` 直接测试

至少直接覆盖这些断言：

- registration token 有效时，成功创建用户
- `password_hash` 被设置
- Trial 在此时发放，而不是在 `verify-code` 时发放
- token 被消费后不能重复使用
- session 被建立

### 5.3 `reset-password` 直接测试

至少直接覆盖这些断言：

- 已有手机号用户可成功重置密码
- 重置后新密码可被验证
- session 被建立
- 错误验证码/过期验证码路径按预期失败

## 6. 验证要求

至少执行：

```bash
pytest tests/test_auth_phone.py tests/test_trial_grant_rules.py -q
npm run lint
npm run build
```

如果你动了额外后端逻辑，再补必要最小测试。

## 7. 回报要求

请写回 `inbox/CodeX` 一封 report，至少包括：

1. `/auth/login` 如何保留 query params
2. 给 `complete-registration` 新增了哪些直接测试
3. 给 `reset-password` 新增了哪些直接测试
4. 是否动了 `gateway/auth_phone.py`
5. 实际执行了哪些验证命令，结果如何
6. 是否还有残余 A1 drift

## 8. 成功标准

本次成功的标准是：

- 旧 `/auth/login?from=...` 兼容性恢复
- 新端点有直接测试覆盖
- 不改变 A1 已定下的产品行为
- 不扩散到其他 auth / captcha / deployment 任务

## 9. 停止条件

完成并验证通过后，停止并等待 CodeX 审核。
