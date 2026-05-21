# CSRF Phase 1D：Job 创建/删除、rename 与认证写入口覆盖

## 目标

继续复用 `require_same_origin_state_change`，覆盖下一批高频状态变更入口：

- Job 创建 / 删除。
- Job rename。
- 登录、注册、登出、改密、绑定邮箱。
- 手机 / 邮箱验证码认证流。

仍不引入 CSRF token，也不改 `SameSite=Lax`。

## 本次覆盖

`gateway/main.py` 直接注册的写路由：

- `POST /auth/register`
- `POST /auth/login`
- `POST /auth/logout`
- `POST /api/account/change-password`
- `POST /api/account/bind-email`
- `PATCH /gateway/jobs/{job_id}`
- `POST /job-api/jobs`
- `DELETE /job-api/jobs/{job_id}`

Router-level 覆盖：

- `auth_phone.router`
- `auth_email.router`

## 明确不覆盖

- 支付 webhook / fake-pay / provider callback。
- Internal API。
- `GET /auth/me` 等只读入口。
- `/job-api/jobs/{job_id}/{subpath:path}` catch-all 的 `POST` 子资源；它包含更宽的 Job API 代理面，留到下一批按 subpath 行为拆分评估。
- `/gateway/upload-video`；它是上传专用入口，留到文件上传路径一起评估。

## 验收

- Wiring 测试检查本批 direct `main.py` 路由和 auth router 写接口都挂 `require_same_origin_state_change`。
- 既有 job route coverage 不变。
- Job create / quota / rename 直接 handler 测试继续通过。
- Auth phone/email/rate-limit 相关测试继续通过。
