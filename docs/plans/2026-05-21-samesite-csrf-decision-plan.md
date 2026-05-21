# SameSite / CSRF 决策与落地计划（2026-05-21）

## 目标

把当前 `samesite="lax"` 的遗留安全债变成可执行、可测试的决策，而不是直接做一行 `lax -> strict` 修改。

本计划只覆盖 Gateway 会话与状态变更请求的 CSRF 防护，不改变套餐、支付、Smart、Post-edit 等业务语义。

## 当前事实

- `gateway/auth.py` 的 session cookie 当前为 `samesite="lax"`。
- `gateway/support_api.py` 的匿名客服 cookie 当前为 `samesite="lax"`，它不是登录 session cookie，应单独评估。
- `tests/test_auth_phone.py` 目前断言 Lax，并把它命名为 mobile compatible 行为。
- 旧计划 `2026-04-17-migration-debt-fixes.md` 判断 Strict 无业务影响，但当时前提是“唯一活跃认证流是手机验证码，无邮件链接跳回场景”。
- 当前 `gateway/config.py` 默认 `email_registration_enabled=True`，认证入口已经包含邮箱注册与密码重置。
- 前端 API 默认相对路径，同源部署仍是主路径；但支付、营销入口、邮件通知、客服链接都可能形成外部导航入口。

## 决策原则

1. **不把 SameSite 当作唯一 CSRF 防线**：Strict 能降低风险，但不能替代状态变更请求的应用层校验。
2. **先保住登录、注册、重置、支付回跳体验**：中国用户侧支付/客服/短信/邮件路径不能因为安全加固出现不可解释的登录丢失。
3. **状态变更请求优先做 Origin/Referer 校验**：这是比全站 Strict 更可控的短期安全闭环。
4. **客服匿名 cookie 单独处理**：匿名客服会话不是登录 session，不应机械跟随 session cookie 策略。

## 推荐决策

短期采用：

- Session cookie 暂时保持 `SameSite=Lax`。
- 对认证态 state-changing API 增加 Origin / Referer 白名单校验。
- 审计并禁止有副作用的 GET。
- 更新 `tests/test_auth_phone.py` 的测试名，避免继续把 Lax 表述为最终安全策略。

中期再评估：

- 若外部跳转链路全部可验证，再将 session cookie 收紧为 `SameSite=Strict`。
- 对高风险状态变更请求引入 CSRF token，而不是全站一次性上 token。

## Phase 0：请求面盘点

输出一个表，按以下维度分类：

| 维度 | 说明 |
| --- | --- |
| Endpoint | 路径与方法 |
| Auth | 是否需要 session |
| State changing | 是否修改 DB / 文件 / 任务状态 / 支付状态 |
| External entry | 是否可能由支付、邮件、营销、客服入口触发 |
| CSRF guard | 当前防护：SameSite / Origin / Referer / token / none |

优先盘点：

- `auth.py`
- `auth_phone.py`
- `auth_email.py`
- `billing.py`
- `subscriptions.py`
- `payment_*`
- `support_api.py`
- `job_intercept.py` 中的 POST / DELETE / PATCH 类入口
- `voice_selection_api.py`
- `user_voice_api.py`
- `admin_*` 路由

验收：

- GET 状态变更端点为 0；若发现，改方法或加显式例外记录。
- 所有 session-auth state-changing API 都有后续 guard 策略。

## Phase 1：Origin / Referer 校验

新增一个小型 Gateway helper，例如：

```python
def require_same_origin_state_change(request: Request) -> None:
    ...
```

建议语义：

- 只对 state-changing method 生效：POST / PUT / PATCH / DELETE。
- 允许无 Cookie 的公开接口跳过，例如公开价格、公开 FAQ、验证码前置配置。
- 对认证态请求：
  - 优先检查 `Origin`；
  - 缺 `Origin` 时检查 `Referer`；
  - host 必须在 `SITE_URL`、CORS 白名单、当前 gateway public origin 的 allowlist 中；
  - 不通过返回 403，错误码稳定，例如 `csrf_origin_rejected`。

测试：

- same-origin POST 通过。
- missing Origin/Referer 的认证态 state-changing 请求按策略拒绝或明确允许。
- cross-origin POST 拒绝。
- public read-only GET 不受影响。
- 支付 webhook 不套 session CSRF guard，继续走 provider signature / webhook idempotency。

## Phase 2：Cookie 策略复评

在 Phase 1 落地并跑完关键路径后，再决定是否改 Strict。

必须手工或自动验收：

- 手机注册 / 登录
- 邮箱注册 / 邮箱验证码 / 密码重置
- 从营销页进入工作台
- 支付创建订单与支付后回跳
- 客服匿名会话与登录用户客服会话
- 通知邮件链接进入 workspace
- 管理员后台入口

如果改 Strict：

- 修改 `gateway/auth.py`。
- 修改 `tests/test_auth_phone.py` 断言和测试名。
- 明确 `support_api.py` 是否跟随；默认建议不跟随，单独评估匿名客服连续性。

## 不做范围

- 不引入完整前端 CSRF token 框架，除非 Phase 1 证明 Origin/Referer 不够。
- 不改变支付 webhook 的验签模型。
- 不把 Gateway 套餐、权益、价格事实迁到前端。
- 不改变 Smart consent / billing / trial 规则。

## 推荐第一步

先做 Phase 0 请求面盘点，生成 `docs/audits/2026-05-21-state-changing-endpoint-csrf-inventory.md`。盘点完成后，再决定 Phase 1 helper 的接入位置。
