---
id: T3-msg-002
task: T3
from: CodeX
to: Claude-Code
type: instruction
status: ready
priority: high
reply_to: T3-msg-001
requires_human: false
created_at: 2026-04-05 22:15 Asia/Shanghai
---

# v2 Task 3：手机号唯一公开注册主路径 / Trial bookkeeping / 基础风控

## 本文件地位

这是一份 **重写后的 Task 3 正式实施协议**。  
它**覆盖并取代**以下旧版 T3 指令中的“保留 email 注册兼容入口”表述：

- `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/Claude-Code/2026-04-05_214500_from-CodeX_to-Claude-Code_type-instruction_task-T3_phone-auth-trial-risk.md`

执行时请以**本文件**为准，不要回退到旧版 T3 边界。

## 背景

当前主线状态：

- `T0` 已完成并放行：gateway 真相源已统一
- `T1` 已完成并放行：`(marketing) / (auth) / (app)` 三层布局已拆分
- `T2` 已完成并放行：marketing 三页已上线到当前阶段可接受首版

当前认证与转化现状：

- `gateway/auth.py` 仍以 `email + password` 为基线
- marketing 的 guest CTA 仍主要通过旧 auth 流承接
- `users` 仍没有手机号与 trial bookkeeping 字段

项目开发者已明确新决策：

- **去掉公开 email 注册**
- **防止通过 email 注册薅 Trial**
- **手机号验证码是唯一公开注册主路径**

因此，`Task 3` 的核心目标是：

- 建立 **phone-first 且 phone-only 的公开注册路径**
- 保留旧 email **登录** 兼容，但不再保留旧 email **注册** 主路径
- 在 fake provider 下可本地跑通
- Trial 只做最小 bookkeeping，不擅自冻结 trial 数字或 entitlement mapping

## 本轮目标

完成以下最小闭环：

1. gateway 新增手机号验证码发送 / 校验 / 登录注册路径
2. 复用现有 session cookie 机制，不引入第二套 token
3. 新增基础风控：
   - 发送短信前的人机验证闸门
   - 单手机号限频
   - 单 IP 限频
   - Trial 一次性发放校验
4. 前端新增 `/auth` 手机号主入口页
5. marketing 的 guest CTA 改向 `/auth`
6. **关闭公开 email 注册**
7. 保留旧 email **登录** 兼容

## 你必须先阅读的文件

1. `D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md`
2. `D:/Claude/AIVideoTrans_Codex_web_mvp/DESIGN.md`
3. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md`
4. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/specs/2026-04-04-pricing-and-plans-api-contract.md`
5. `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth.py`
6. `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/models.py`
7. `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/main.py`
8. `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/config.py`
9. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/marketing/primary-cta.tsx`
10. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/marketing/pricing-grid.tsx`
11. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/auth/login/page.tsx`
12. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/auth/register/page.tsx`
13. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/middleware.ts`

## 关键执行决策

### 1. 手机号是唯一公开注册主路径

本轮主路径：

- `/auth` → 手机号验证码登录 / 注册

必须保留：

- `/auth/login` → legacy email 登录兼容入口

不再保留公开注册能力：

- `/auth/register` 不再作为可创建新账户的公开注册入口
- `POST /auth/register` 不应继续创建新用户

这次不是简单“前端不露出 email 注册”，而是要把 **公开 email 注册链路本身关掉**，防止被绕过。

### 2. 关闭 email 注册，但不要删光 email 能力

本轮必须做到：

1. 保留 `POST /auth/login`
2. 保留旧 email 用户登录能力
3. 关闭 `POST /auth/register` 的新用户创建能力
4. 前端 `/auth/register` 不再呈现可提交的 email 注册表单

推荐行为：

- `POST /auth/register` 返回 `403` 或 `410`
- 中文错误信息应清晰指向：`邮箱注册已关闭，请使用手机号验证码注册`
- `/auth/register` 页面可做成：
  - 轻量 notice page，提供去 `/auth` 的 CTA
  - 或安全 redirect 到 `/auth`

但不要：

- 保留一个还能提交成功的 email 注册表单
- 只隐藏 UI 却让后端继续开放注册

### 3. fake SMS / fake captcha 必须是默认本地路径

当前阶段未经项目开发者批准，不要把真实短信服务或真实 captcha vendor 引入默认主路径或测试路径。

因此本轮必须满足：

- 默认 `SMS_PROVIDER=fake`
- 默认 `CAPTCHA_PROVIDER=fake`
- 本地开发、测试、浏览器验证都可以在没有真实第三方服务的情况下跑通

如需新增配置项，请放在 `gateway/config.py` 并提供安全默认值。  
不要要求新增 secrets 才能通过默认本地验证。

### 4. Trial 数字与 entitlement mapping 仍未冻结

这是本轮最容易跑偏的地方，必须严格遵守：

- 不要把 `7天 / 20分钟 / 含 Studio / phone_required=true` 写进 public contract
- 不要修改 `gateway/plan_catalog.py` 里的 public `trial` 结构
- 不要修改 marketing 文案去宣称已冻结的 trial 事实
- **不要把 `user.plan_code = "plus"` 当作试用替代实现**

本轮允许做的是：

- 完成手机号验证成功后的 trial bookkeeping
- 写入 `trial_granted_at`
- 为未来 `trial_ends_at` 留好字段与代码路径
- 阻止同一手机号重复领取 trial

如果真正的 trial 发放逻辑需要明确的天数或 entitlement mapping，而当前真相源仍未冻结：

- **不要自行发明数字**
- **不要把 trial 直接映射到 plus/pro**
- 允许把 `trial_ends_at` 暂时保持 `NULL`
- 允许只记录 “已领取试用资格 / 已验证手机号 / 不可重复领取”
- 若你判断必须有 frozen 商业事实才能让本轮继续，请停止并上报 blocker

### 5. 不要用 synthetic email 伪装 phone-only 账户

当前 `users.email` 与 `users.password_hash` 仍是必填模型。  
如果 phone-only 账户在现有 schema 下无法成立，你可以做 **最小兼容迁移**：

- 允许将 `users.email` 改为 nullable
- 允许将 `users.password_hash` 改为 nullable

但不允许：

- 伪造 `138xxxx@phone.local`
- 写死占位邮箱
- 建重型 `user_identities` 平台

如果你做 nullable 迁移，必须保持：

- 旧 email 登录仍可工作
- 现有 `/auth/me` 返回结构对前端兼容
- 缺失 email 时可返回 `""`，不要让现有 session consumer 崩掉

### 6. 本轮不做账号绑定 / 合并 / 微信

不要在 `Task 3` 里同时做这些事情：

- 已登录 email 用户绑定手机号
- 手机号账户与邮箱账户合并
- 微信登录 / 微信绑定
- subscription / billing 建模

本轮只处理：

- 未登录访客进入 `/auth`
- 发送验证码
- 校验验证码
- 建立 session
- Trial bookkeeping
- 关闭公开 email 注册

## 推荐的最小 API 形状

### 1. 发送验证码

`POST /auth/phone/send-code`

请求至少包含：

- `phone_number`
- `captcha_token`

责任：

- 规范化手机号
- 校验 fake/real captcha provider
- 执行手机号 / IP 限频
- 通过 `sms_provider` 发送验证码

### 2. 验证验证码并登录 / 注册

`POST /auth/phone/verify-code`

请求至少包含：

- `phone_number`
- `code`

责任：

- 校验验证码
- 查找或创建用户
- 复用 `create_session(...)`
- 在规则允许时记录 trial bookkeeping
- 返回与当前 auth 流兼容的最小 user/session 结果

不要额外引入：

- `/auth/phone/bind`
- `/auth/phone/link-email`
- `/auth/phone/wechat`

## 风控要求

`gateway/risk_control.py` 至少要支持这些能力：

1. 中国大陆手机号的最小规范化
   - 可安全去掉空格、短横线、前缀 `+86`
   - 不要引入全球号码平台或重型库
2. 单手机号限频
3. 单 IP 限频
4. 虚拟号段拦截 hook
   - 当前阶段允许先做 stub / hook
   - 不要接真实外部风控 API
5. Trial 一次性发放校验

限频阈值请集中放进 `gateway/config.py`，不要把 magic numbers 散在 handler 里。

## 关于验证码持久化

如果你认为进程内内存不足以让本轮实现保持可测试、可回归：

- 允许在 `models.py` 和同一个 Alembic migration 中增加 **一个轻量 challenge / code 表**
- 例如记录：
  - `phone_number`
  - `code`
  - `client_ip`
  - `purpose`
  - `expires_at`
  - `consumed_at`

但请保持边界：

- 只加最小表
- 不做通用 identity platform
- 不做 notification bus
- 不做多 provider registry

## 前端要求

### 1. 新增 `/auth` 主入口页

创建：

- `frontend-next/src/app/(auth)/auth/page.tsx`

页面职责：

- 手机号输入
- fake captcha gate
- 发送验证码
- 输入验证码
- 完成验证并跳转
- 提供去 `/auth/login` 的兼容入口
- 明确告诉用户这是当前唯一公开注册方式

设计要求：

- 中文优先、低摩擦、信任感明确
- 属于 auth 层，不要套 marketing hero 戏剧化表达
- 文案不要像内部测试页

### 2. `/auth/register` 不再是注册表单

允许修改：

- `frontend-next/src/app/(auth)/auth/register/page.tsx`

要求：

- 不再显示可提交的新注册表单
- 改成 redirect 或轻量 notice
- 提供：
  - 去 `/auth` 的 CTA
  - 去 `/auth/login` 的兼容入口

### 3. 保留 `/auth/login`

允许修改：

- `frontend-next/src/app/(auth)/auth/login/page.tsx`

但只限：

- 增加一处去 `/auth` 的入口
- 轻量说明“新用户请使用手机号验证码注册”
- 不做整页重设计

### 4. 新增 auth 组件

创建：

- `frontend-next/src/components/auth/phone-login-form.tsx`
- `frontend-next/src/components/auth/captcha-gate.tsx`

如确有必要可新增：

- `frontend-next/src/lib/auth/*`

但不要做新的全局认证框架。

### 5. marketing CTA 改向

本轮必须把 marketing 的 guest CTA 改到 `/auth`：

- guest → `/auth`
- logged-in → `/translations/new`

允许修改：

- `frontend-next/src/components/marketing/primary-cta.tsx`
- `frontend-next/src/components/marketing/pricing-grid.tsx`

不要引入 plan-aware / subscription-aware CTA。

### 6. `/auth` 必须可匿名访问

当前 `middleware.ts` 对 `/auth` 这个**精确路径**并不会自动放行。  
因此本轮通常需要最小修改：

- 让 `/auth` 对未登录访客公开可访问

这类修改是本轮允许的最小越界，不算跑偏。

## 本轮允许修改 / 新建的文件

### Gateway

- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/models.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/main.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/config.py`
- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth_phone.py`
- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/risk_control.py`
- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/sms_provider.py`
- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/alembic/versions/007_add_phone_and_trial_fields.py`

### Tests

- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_auth_phone.py`
- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_trial_grant_rules.py`

### Frontend

- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/auth/page.tsx`
- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/auth/phone-login-form.tsx`
- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/auth/captcha-gate.tsx`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/auth/login/page.tsx`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/auth/register/page.tsx`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/marketing/primary-cta.tsx`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/marketing/pricing-grid.tsx`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/middleware.ts`

### 如确有必要可额外新增，但应保持很轻

- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/lib/auth/*`

## 不要修改

- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/plan_catalog.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/billing.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/job_intercept.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/entitlements.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(marketing)/*`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/globals.css`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/providers/session-provider.tsx`
- 任意 payment / subscription / billing migration 文件

不要把 `Task 3` 扩成 `Task 4`。

## 推荐实施顺序

1. 先做 schema / migration 决策
   - `phone_number`
   - `phone_verified_at`
   - `trial_granted_at`
   - `trial_ends_at`
   - 如确有必要，再做 `email/password_hash` nullable
   - 如确有必要，再加 challenge 表
2. 再做 `sms_provider.py` 与 `risk_control.py`
3. 再做 `auth_phone.py`
4. 再在 `main.py` 挂载 phone auth 路由
5. 再关闭 `POST /auth/register`
6. 再做 `/auth` 页面与 auth 组件
7. 再把 `/auth/register` 改成 redirect / notice
8. 最后做 marketing CTA 与 `middleware.ts` 的最小收口

## 必须覆盖的测试点

### Backend

`tests/test_auth_phone.py` 至少覆盖：

1. 正常手机号发送验证码（fake provider）
2. 未通过 captcha 不发短信
3. 手机号限频命中
4. IP 限频命中
5. 验证码正确时成功登录 / 注册并建立 session
6. 验证码错误时拒绝
7. `POST /auth/register` 已关闭，不能再创建新用户
8. 旧 email 登录仍可工作

`tests/test_trial_grant_rules.py` 至少覆盖：

1. 首次验证手机号时记录 `trial_granted_at`
2. 同一手机号不会重复领取 Trial
3. 重复验证同一手机号时返回同一用户，而不是新建第二个账户
4. 不会偷偷写入硬编码 trial 天数 / 分钟数
5. 不会通过 `plan_code="plus"` 变相冻结 trial entitlement mapping

如引入 challenge 表，也要覆盖：

6. 已消费验证码不能重复使用
7. 过期验证码无效

### Frontend

至少确保：

1. `/auth` 页面能构建并正常渲染
2. guest marketing CTA 改向 `/auth`
3. `/auth/login` 仍可访问
4. `/auth/register` 不再是可提交注册表单
5. `/auth` 对未登录访客公开可访问

## 验证要求

至少运行：

1. `pytest tests/test_auth_phone.py tests/test_trial_grant_rules.py -q`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp`
2. `npm run lint`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next`
3. `npm run build`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next`

如新增 Alembic migration，还必须验证：

4. `alembic upgrade head`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway`

并做最小浏览器核验：

1. `/auth` 返回 200，手机号表单可见
2. fake captcha 通过后可以发送验证码
3. fake provider 路径下可以完成验证码验证并建立 session
4. guest homepage / pricing CTA 指向 `/auth`
5. `/auth/login` 返回 200
6. `/auth/register` 不再出现可提交 email 注册表单
7. 控制台 0 errors

如果你无法在浏览器中完整跑通 fake captcha / fake SMS 链路，请明确说明阻塞点，不要假装验证通过。

## 完成后必须写回汇报

请写一份新的 `report` 到：

- `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/`

建议文件名：

- `2026-04-05_22xxxx_from-Claude-Code_to-CodeX_type-report_task-T3_stage-complete.md`

请至少包含：

1. 本轮执行范围
2. schema / migration 决策
3. 是否对 `users.email` / `password_hash` 做了 nullable 兼容
4. 是否引入 challenge 表；如果有，引入原因是什么
5. phone auth API 形状
6. fake SMS / fake captcha 如何工作
7. `POST /auth/register` 如何被关闭
8. legacy email 登录如何保留
9. Trial bookkeeping 如何处理未冻结事实
10. frontend `/auth` 与 CTA 改向结果
11. `/auth/register` 最终变成了什么
12. 实际修改文件
13. `pytest`
14. `npm run lint`
15. `npm run build`
16. `alembic upgrade head`（如有 migration）
17. 浏览器核验结果
18. 风险与 blocker
19. 是否已停止

## 结论

本轮的成功标准是：

- 建立 phone-only 的公开注册主路径
- 关闭公开 email 注册
- 保留 legacy email 登录兼容
- fake provider 下本地可跑通
- Trial 只做最小 bookkeeping，不擅自冻结商业事实

修完后停止，等待 CodeX 审核。
