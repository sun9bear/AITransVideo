# State-Changing Endpoint CSRF Inventory（2026-05-21）

## 目的

这是 `docs/plans/2026-05-21-samesite-csrf-decision-plan.md` 的 Phase 0 盘点结果。目标是先把 Gateway 请求面分清楚，再决定是保留 `SameSite=Lax` 并补 Origin/Referer 校验，还是进一步切到 `SameSite=Strict`。

本文件不要求立即改代码；它给 Phase 1 的 CSRF guard 设计提供边界。

## 当前全局事实

- Gateway CORS 当前 `allow_credentials=True`，origin 来自 `settings.cors_origins`。
- 登录 session cookie 当前为 `SameSite=Lax`。
- 当前没有统一的 Origin / Referer / CSRF token guard。
- 认证态 state-changing API 主要依赖 session cookie + `require_auth` / `_require_admin` / ownership check。
- 内部 API 使用 `X-Internal-Key`，其中 `voice_catalog_api` 与 `user_voice_api` 还带 loopback 校验。
- 支付 webhook 使用 provider signature / provider event idempotency，不应该套 session CSRF guard。

## 防护分类

| 分类 | 是否纳入通用 CSRF guard | 说明 |
| --- | --- | --- |
| Session-auth user write API | 是 | 依赖浏览器 cookie，典型 CSRF 风险面 |
| Admin write API | 是，优先级高 | 高权限状态变更，必须同源校验 |
| Anonymous support cookie API | 单独策略 | 不是登录 session，但匿名 cookie 也会被浏览器自动携带 |
| Public auth / OTP API | 不按 session CSRF 处理，但需保留 captcha/rate-limit | 无登录 cookie 依赖，主要是滥用/刷接口风险 |
| Payment webhook | 否 | 用 provider signature / event idempotency |
| Gateway internal API | 否 | 用 `X-Internal-Key` + loopback / Caddy block |
| OAuth callback GET | 否，使用 OAuth state | 外部跳转入口，通用 same-origin guard 会误杀 |
| Dev/test fake provider endpoint | 不应生产暴露 | Phase 1I 已加生产默认禁用 gate |

## GET 副作用例外

| Endpoint | 状态变更 | 当前防护 | 风险判断 | 建议 |
| --- | --- | --- | --- | --- |
| `GET /api/billing/fake-pay/{order_id}` | settle fake payment order | `AVT_ENV=production` 默认禁用；需显式 `AVT_ENABLE_FAKE_PAYMENT=true` | 合理例外。它服务 local/test 浏览器回跳，但生产默认不可结算 | 不纳入通用 CSRF guard；保持 fake provider 生产 gate |
| `GET /api/admin/pan/callback` | 兑换 OAuth code，写 `pan_credentials` | one-shot OAuth `state` token | 合理例外。它必须接受 Baidu 外部跳转，不能套同源 Origin guard | 保持 OAuth state；测试 state 过期/重放/缺失 |

除上述两类外，本轮没有发现明显的“普通业务 GET 直接改用户状态”入口。后续 Phase 1 可继续用测试守卫防止新增副作用 GET。

## Public Auth / OTP

| Endpoint | State change | Current guard | CSRF guard 建议 |
| --- | --- | --- | --- |
| `POST /auth/register` | 创建 email registration challenge | captcha / risk control / email flow | 不纳入 session CSRF；保留 anti-abuse |
| `POST /auth/login` | 创建 session cookie | password + rate limit | 可选 same-origin；重点是 login CSRF 体验/风险评估 |
| `POST /auth/logout` | 删除 session | session cookie | **纳入 same-origin guard** |
| `POST /auth/phone/send-code` | 创建 phone challenge / attempts | captcha + rate limit | 不纳入 session CSRF；保留 anti-abuse |
| `POST /auth/phone/verify-code` | 消耗/更新 challenge，可能登录 | code + attempts | 不纳入通用 session CSRF；保留 attempts guard |
| `POST /auth/phone/complete-registration` | 创建 user + trial credits + session | registration token + password | 可选 same-origin；若加 guard，必须验证手机注册 UX |
| `POST /auth/phone/reset-password` | 重置密码 | phone code / attempts | 可选 same-origin；不能破坏重置流程 |
| `POST /auth/email/verify-registration-code` | 消耗/更新 email challenge | code + attempts | 不纳入通用 session CSRF |
| `POST /auth/email/complete-registration` | 创建 user + trial credits + session | registration token + password | 可选 same-origin；需验证邮箱注册路径 |
| `POST /auth/email/send-reset-code` | 创建 reset challenge | captcha + rate limit | 不纳入 session CSRF；保留 anti-abuse |
| `POST /auth/email/reset-password` | 重置密码 | email code / attempts | 可选 same-origin；需验证邮件重置路径 |

## Account / Session User APIs

| Endpoint | State change | Current guard | CSRF guard 建议 |
| --- | --- | --- | --- |
| `POST /api/account/change-password` | 修改当前用户密码 | session via `get_current_user` | **纳入** |
| `POST /api/account/bind-email` | 绑定邮箱 | session via `get_current_user` | **纳入** |
| `POST /gateway/upload-video` | 上传源视频 / 创建临时资源 | `require_auth` | **纳入** |
| `PATCH /gateway/jobs/{job_id}` | 重命名 job | `require_auth` + ownership | **纳入** |
| `POST /job-api/jobs` | 创建 job / reserve quota / upstream proxy | `require_auth` + policy/credits | **纳入** |
| `DELETE /job-api/jobs/{job_id}` | 删除 job / release quota | `require_auth` + ownership | **纳入** |
| `POST /job-api/jobs/{job_id}/voice-clone` | 克隆/复用音色，可能计量 | `require_auth` + ownership | **纳入** |
| `POST /job-api/jobs/{job_id}/voice-match` | 查询复用匹配，可能偏 read-like | `require_auth` + ownership | 可纳入，低风险 |
| `POST /job-api/jobs/{job_id}/voice-candidates` | 查询候选，可能偏 read-like | `require_auth` + ownership | 可纳入，低风险 |
| `POST /job-api/jobs/{job_id}/{subpath}` | continue / review approve / editing / generation 等 | `require_auth` + ownership | **纳入**，但需排除内部 callback |
| `/job-api/{path}` PUT / DELETE / PATCH | 透传 Job API 非 jobs 路径 | `require_auth` | **纳入** |

## Billing / Payment

| Endpoint | State change | Current guard | CSRF guard 建议 |
| --- | --- | --- | --- |
| `POST /api/billing/orders` | 创建 payment order | `get_current_user` required in handler | **纳入** |
| `POST /api/billing/fake-pay/{order_id}` | fake provider settle | `AVT_ENV=production` 默认禁用；需显式 `AVT_ENABLE_FAKE_PAYMENT=true` | dev/test only；不纳入 session CSRF |
| `GET /api/billing/fake-pay/{order_id}` | fake provider settle + redirect | `AVT_ENV=production` 默认禁用；需显式 `AVT_ENABLE_FAKE_PAYMENT=true` | **GET 副作用例外**；由 fake provider 生产 gate 约束 |
| `POST /api/billing/webhooks/{provider_name}` | 处理支付 webhook / settle order | provider signature + idempotency | **不纳入**；保持 provider signature |

## User Voice / Voice Selection

| Endpoint | State change | Current guard | CSRF guard 建议 |
| --- | --- | --- | --- |
| `POST /gateway/user-voices` | 新增个人音色记录 | `require_auth` | **纳入** |
| `PATCH /gateway/user-voices/{voice_id}` | 修改 label | `require_auth` | **纳入** |
| `DELETE /gateway/user-voices/{voice_id}` | 删除个人音色 | `require_auth` | **纳入** |
| `POST /gateway/user-voices/probe` | 试听/探测 voice id，可能触发 provider call | `require_auth` | **纳入** |
| `POST /gateway/user-voices/{voice_id}/calibrate-speed` | 触发 calibration / 写 speed profile | `require_auth` | **纳入** |
| `POST /job-api/jobs/{job_id}/voice-clone` | voice clone / reuse | `require_auth` + ownership | **纳入** |
| `POST /job-api/jobs/{job_id}/voice-match` | match 查询 | `require_auth` + ownership | 可纳入，低风险 |
| `POST /job-api/jobs/{job_id}/voice-candidates` | candidates 查询 | `require_auth` + ownership | 可纳入，低风险 |

## Background Tasks / Materials

| Endpoint | State change | Current guard | CSRF guard 建议 |
| --- | --- | --- | --- |
| `POST /api/jobs/{job_id}/tasks` | 创建 background task | `require_auth` | **纳入** |
| `POST /api/jobs/{job_id}/materials-pack` | 创建 materials_pack task | `require_auth` | **纳入** |

## Notifications

| Endpoint | State change | Current guard | CSRF guard 建议 |
| --- | --- | --- | --- |
| `POST /api/notifications/read` | 标记已读 | `require_auth` | **纳入** |
| `POST /api/notifications/archive` | 归档通知 | `require_auth` | **纳入** |
| `POST /api/notifications/popups/{popup_id}/dismiss` | dismiss popup | `require_auth` | **纳入** |
| `POST /internal/notifications/dispatch` | 内部派发通知 | `X-Internal-Key` | 不纳入；建议未来路径统一到 `/api/internal/*` 或保留明确 internal guard |

## Support

| Endpoint | State change | Current guard | CSRF guard 建议 |
| --- | --- | --- | --- |
| `POST /api/support/conversations` | 创建客服会话 | session user 或 anonymous support cookie | 单独策略；对匿名入口做 Origin/Referer 或 rate-limit 加强 |
| `POST /api/support/conversations/{id}/messages` | 发送消息 | session user 或 matching anonymous cookie | 单独策略；匿名 cookie 也会自动携带，建议纳入 same-origin guard 或更严格 rate-limit |
| `POST /api/support/conversations/{id}/handoff` | 创建 handoff request | session user 或 matching anonymous cookie | 单独策略；建议纳入 same-origin guard |

## Admin APIs

Admin 写接口全部建议纳入 same-origin guard。现有 `_require_admin` 只验证登录与角色，不验证请求来源。

| Area | Endpoints | State change | Current guard |
| --- | --- | --- | --- |
| Admin settings | `POST /api/admin/settings`, `POST /api/admin/model-toggle`, review prompt writes/deletes | settings / model / prompt policy | `_require_admin` |
| Admin jobs | `POST /api/admin/jobs/{job_id}/cancel`, `POST /api/admin/jobs/{job_id}/delete` | job cancel/delete | `_require_admin` |
| Admin users | `PATCH /api/admin/users/{user_id}/entitlements` | entitlement override | `_require_admin` |
| Admin disk | `POST /api/admin/disk/cleanup-*`, `POST /api/admin/disk/resize-filesystem` | filesystem cleanup / resize | `_require_admin` |
| Admin support | settings, handoff close, heartbeat/presence, WeChat QR, conversations, announcements | support config/content/admin actions | `_require_admin` |
| Admin voices | create/import/verify/label/patch/delete voice catalog | catalog DB / provider verification | `_require_admin` |
| Admin pricing | `POST /api/admin/pricing/draft`, `POST /api/admin/pricing/publish` | pricing config | `_require_admin` |
| Admin pan | connect, backup, restore, disconnect credentials, delete backup | OAuth state / backup lifecycle / credentials | `_require_admin` plus Pan-specific guards |
| Admin monitor | `POST /api/admin/jobs/{job_id}/analyze-logs` | LLM/log analysis task | `_require_admin` |

## Internal APIs

These should stay outside browser CSRF guard and rely on internal auth:

| Endpoint | Current guard |
| --- | --- |
| `POST /job-api/jobs/{job_id}/source-metadata` | `Depends(_require_internal_access)` from voice catalog internal helper |
| `POST /job-api/jobs/{job_id}/metering` | `Depends(_require_internal_access)` from voice catalog internal helper |
| `GET /api/internal/voice-catalog` | `X-Internal-Key` + loopback |
| `/api/internal/user-voices/*` | `X-Internal-Key` + loopback |
| `POST /internal/notifications/dispatch` | `X-Internal-Key` |

Note: notification internal path uses `/internal/notifications/dispatch`, not `/api/internal/*`. It is still header-gated, but it does not benefit from the Caddy `/api/internal/*` block naming convention.

## Phase 1 推荐接入顺序

1. Build small helper: `require_same_origin_state_change(request)`.
2. Add it first to admin write routers and session-auth account/job write routes.
3. Add explicit allowlist/exemption for:
   - payment webhooks,
   - internal endpoints,
   - Pan OAuth callback,
   - public OTP/auth endpoints pending UX validation.
4. Add tests:
   - same-origin POST passes,
   - cross-origin POST rejects,
   - payment webhook still reaches provider signature path,
   - Pan callback still validates `state`,
   - fake-pay GET is either dev/test-only or explicitly documented as an exception.

## Immediate Findings

1. **No broad state-changing GET pattern found**, but two explicit exceptions must be tracked: fake-pay browser endpoint and Pan OAuth callback.
2. **Admin write APIs are the highest ROI CSRF guard target** because they combine cookie auth with high-impact state changes.
3. **Job and post-edit write APIs should be second** because ownership checks stop cross-user access but do not stop same-user CSRF actions such as job deletion, continuation, or review approve.
4. **Support anonymous cookie needs a separate decision** because it is not login session CSRF, but browser auto-cookie behavior still applies.
5. **Webhook/internal APIs should not use browser CSRF guard**; they already have provider/internal authentication and would break if same-origin-only checks were applied.
