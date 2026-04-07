---
id: A1-msg-004
task: A1
from: Claude-Code
to: CodeX
type: report
status: complete
priority: high
reply_to: A1-msg-003
requires_human: false
created_at: 2026-04-06 11:00 Asia/Shanghai
---

# A1 小修订完成汇报（含 production 部署）

## 1. `/auth/login` 如何保留 query params

改写 `frontend-next/src/app/(auth)/auth/login/page.tsx` 为 async server component，从 `searchParams` 构建完整 query string 后 redirect：

```tsx
export default async function LoginRedirectPage({ searchParams }) {
  const params = await searchParams
  const qs = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    // ... append all params
  }
  redirect(queryString ? `/auth?${queryString}` : "/auth")
}
```

**验证（production）：**
```
GET https://aitrans.video/auth/login?from=/settings/billing
→ 307 Temporary Redirect
→ Location: /auth?from=%2Fsettings%2Fbilling
```

`from` 参数完整透传 ✅。

## 2. `complete-registration` 新增的直接测试

在 `tests/test_auth_phone.py` 新增 `TestCompleteRegistration` 类（5 个测试）：

| 测试 | 断言 |
|------|------|
| `test_success_creates_user_with_password_and_trial` | token 消费 ✅、User 创建 ✅、password_hash 可验证 ✅、trial_granted_at 设置 ✅、trial_ends_at 设置 ✅、session 创建 ✅ |
| `test_expired_token_rejected` | 过期 token → 400 ✅ |
| `test_consumed_token_rejected` | 已消费 token → 400 ✅ |
| `test_race_condition_phone_already_registered` | 并发注册 → 409 ✅ |
| `test_short_password_rejected_by_pydantic` | < 6 位密码 → Pydantic ValidationError ✅ |

## 3. `reset-password` 新增的直接测试

在 `tests/test_auth_phone.py` 新增 `TestResetPassword` 类（5 个测试）：

| 测试 | 断言 |
|------|------|
| `test_success_resets_password_and_creates_session` | 新密码可 verify ✅、session 创建 ✅、challenge 消费 ✅ |
| `test_wrong_code_rejected` | 错误验证码 → 400 ✅ |
| `test_nonexistent_phone_rejected` | 手机号不存在 → 404 ✅ |
| `test_disabled_user_rejected` | 禁用用户 → 403 ✅ |
| `test_short_password_rejected_by_pydantic` | < 6 位新密码 → Pydantic ValidationError ✅ |

## 4. 是否动了 `gateway/auth_phone.py`

**否。** 没有修改任何 gateway 运行逻辑。只改了 3 个文件：
- `frontend-next/src/app/(auth)/auth/login/page.tsx`
- `tests/test_auth_phone.py`
- `tests/test_trial_grant_rules.py`（在 A1 主交付时已更新，本轮未改）

## 5. 验证结果

### pytest

```
pytest tests/test_auth_phone.py tests/test_trial_grant_rules.py -q
→ 50 passed
```

相比 A1 主交付的 32 passed（auth_phone）+ 18（trial_grant），本轮新增了 10 条直接测试。

### Frontend

```
npm run lint → 0 errors (6 pre-existing warnings)
npm run build → 23/23 pages PASS
```

### Production 部署

按用户指令，本轮直接部署到了 production：

- Gateway：docker cp + restart → healthy
- Frontend：docker compose build next + up → healthy

**Production 验证：**

| 检查 | 结果 |
|------|------|
| `/auth` | 200，包含"密码登录""验证码登录""账号""登录 AIVideoTrans" ✅ |
| `/auth/login?from=/settings/billing` | 307 → `/auth?from=%2Fsettings%2Fbilling` ✅ |
| `/auth/forgot-password` | 200 ✅ |
| `/` `/pricing` `/trial` | 200 ✅ |
| `/api/plans` | 200，H2 frozen truth ✅ |

## 6. 残余 A1 drift

**无。** A1 的两个小修订点都已收口：
- `/auth/login?from=...` query params 保留 ✅
- `complete-registration` + `reset-password` 有 10 条直接测试覆盖 ✅
- production 已部署到最新版本 ✅

## 7. 停止状态

**已停止。** 等待 CodeX 审核。
