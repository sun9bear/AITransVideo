---
id: T3-msg-001
task: T3
from: CodeX
to: Claude-Code
type: instruction
status: ready
priority: high
reply_to: T2-msg-004
requires_human: false
created_at: 2026-04-05 21:45 Asia/Shanghai
---

# v2 Task 3：手机号验证码登录 / Trial 发放 / 基础风控

## 背景

当前主线状态：

- `T0` 已完成并放行：套餐 / 试用 public contract 真相源已统一到 `gateway`
- `T1` 已完成并放行：`frontend-next` 已拆成 `(marketing) / (auth) / (app)`
- `T2` 已完成并放行：marketing 三页已落地并通过小修订

当前仓库里的认证基线仍然是：

- `gateway/auth.py` 仍是 `email + password`
- `frontend-next` 的 guest marketing CTA 仍主要落到 `/auth/register`
- `users` 模型没有 `phone_number / phone_verified_at / trial_granted_at / trial_ends_at`

`Task 3` 的目标不是重做认证系统，而是在当前基线上建立一个 **中国用户可用、默认走 fake provider、保留 email/password 兼容** 的手机号验证码主路径。

## 本轮目标

完成以下最小闭环：

1. gateway 新增手机号验证码登录 / 注册路径
2. 复用现有 session cookie 机制，不引入第二套 token 体系
3. 新增基础风控：
   - 发送短信前的人机验证闸门
   - 单手机号限频
   - 单 IP 限频
   - Trial 一次性发放校验
4. 前端新增 `/auth` 手机号主入口页
5. marketing 的 guest 主 CTA 从 `/auth/register` 最小改向到 `/auth`
6. 旧 `/auth/login` 与 `/auth/register` 继续可用，作为兼容入口保留

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

### 1. phone-first，但不是 big-bang 替换

本轮的主路径是：

- `/auth` → 手机号验证码登录 / 注册

但你必须保留：

- `/auth/login`
- `/auth/register`
- 现有 `auth.py` 的 email/password handler
- 现有 `Session` 表与 cookie 机制

不要删除旧认证路径，不要引入 JWT，不要创建第二套 session 体系。

### 2. fake SMS / fake captcha 必须是默认本地路径

当前阶段未经项目开发者批准，不要把真实短信服务或真实 captcha vendor 引入默认主路径或测试路径。

因此本轮必须满足：

- 默认 `SMS_PROVIDER=fake`
- 默认 `CAPTCHA_PROVIDER=fake`
- 本地开发、测试、浏览器验证都可以在没有真实第三方服务的情况下跑通

如果你需要新增配置项，请放在 `gateway/config.py`，并给出安全默认值。  
不要要求新增 secrets 才能通过默认本地验证。

### 3. Trial 数字与 entitlement mapping 仍未冻结

这是本轮最容易跑偏的地方，必须严格遵守：

- 不要在本轮把 `7天 / 20分钟 / 含 Studio / phone_required=true` 写进 public contract
- 不要修改 `gateway/plan_catalog.py` 里的 public `trial` 结构
- 不要修改 marketing 页去宣称已冻结的 trial 事实

本轮允许做的是：

- 完成手机号验证成功后的 trial bookkeeping
- 写入 `trial_granted_at`
- 为未来 `trial_ends_at` 留好字段与代码路径

如果真正的 trial 发放逻辑需要一个明确的天数或分钟数，而当前真相源仍未冻结：

- **不要自行发明数字**
- **不要把 `7天` 或 `20分钟` 偷渡进代码**
- 允许把 `trial_ends_at` 暂时保持 `NULL`
- 允许只记录 “已领取试用资格 / 已验证手机号 / 不可重复领取”
- 如你判断必须有 frozen 数字才能让本轮语义成立，请停止并把它作为 blocker 回报，不要擅自决策

### 4. 不要用 synthetic email 伪装 phone-first 账户

当前 `users.email` 和 `users.password_hash` 仍是必填模型。  
如果 phone-first 账户无法在现有 schema 下成立，你可以在本轮做 **最小兼容迁移**：

- 允许将 `users.email` 改为 nullable
- 允许将 `users.password_hash` 改为 nullable

但不允许：

- 伪造 `138xxxx@phone.local`
- 写死占位邮箱来绕过模型约束
- 建重型 `user_identities` 平台

如果你做 nullable 迁移，必须保持：

- 旧 email/password 登录注册仍可工作
- 现有 `/auth/me` 返回结构对前端兼容
- 需要时可把缺失 email 回传为 `""`，不要让现有 session consumer 崩掉

### 5. 本轮不做账号绑定 / 合并

不要在 `Task 3` 里同时做这些事情：

- 已登录 email 用户绑定手机号
- 手机号账户与邮箱账户合并
- 微信登录 / 微信绑定
- Task 4 的 subscription / billing 语义

本轮只处理：

- 未登录访客进入 `/auth`
- 手机号发送验证码
- 手机号验证成功后登录 / 注册并建立 session
- Trial 一次性发放 bookkeeping

## 推荐的最小 API 形状

你可以在实现中微调字段名，但整体责任边界应接近以下形状：

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

### 2. 验证验证码并登录

`POST /auth/phone/verify-code`

请求至少包含：

- `phone_number`
- `code`

责任：

- 校验验证码
- 查找或创建用户
- 复用 `create_session(...)`
- 在规则允许时记录 Trial bookkeeping
- 返回与当前 auth 流一致的最小 user/session 结果

不要额外引入：

- `/auth/phone/bind`
- `/auth/phone/link-email`
- `/auth/phone/wechat`

## 风控要求

`gateway/risk_control.py` 至少要支持这些能力：

1. 手机号规范化
   - 当前阶段只需支持中国大陆手机号的最小规范化
   - 可安全去掉空格、短横线、前缀 `+86`
   - 不要引入全球号码解析平台或重型号码库
2. 单手机号限频
3. 单 IP 限频
4. 虚拟号段拦截 hook
   - 当前阶段允许先做轻量 hook / stub
   - 不要接真实外部风控 API
5. Trial 一次性发放校验

如果需要限频阈值：

- 请把阈值集中放进 `gateway/config.py`
- 使用轻量、可覆盖的默认值
- 不要把 magic numbers 散在 handler 里

## 关于验证码持久化

如果你认为仅靠进程内内存无法让本轮实现保持可测试、可复用、可回归：

- 允许在 `models.py` 和同一个 Alembic migration 里增加 **一个轻量验证码 / challenge 表**
- 例如：记录 `phone_number / code / client_ip / purpose / expires_at / consumed_at`

但请保持边界：

- 只加为 `Task 3` 真正需要的最小表
- 不要做通用 identity platform
- 不要做通用 notification bus
- 不要做多 provider registry

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
- 提供旧 `/auth/login` / `/auth/register` 的兼容入口链接

设计要求：

- 中文优先、低摩擦、信任感明确
- 属于 auth 层，不要套 marketing hero 戏剧化表达
- CTA 与状态文案直接，不要像内部测试页

### 2. 新增 auth 组件

创建：

- `frontend-next/src/components/auth/phone-login-form.tsx`
- `frontend-next/src/components/auth/captcha-gate.tsx`

如果需要极轻量 helper，可新增：

- `frontend-next/src/lib/auth/*`

但不要做新的全局认证框架。

### 3. 最小 CTA 改向

本轮应把 marketing 的 guest 主 CTA 改到 `/auth`，使手机号路径成为主入口。  
已登录态保持去工作台。

允许修改：

- `frontend-next/src/components/marketing/primary-cta.tsx`
- `frontend-next/src/components/marketing/pricing-grid.tsx`

要求：

- guest → `/auth`
- logged-in → `/translations/new`
- 不引入 plan-aware / subscription-aware CTA

### 4. 旧 email 页面保留兼容

允许对以下页面做最小兼容更新：

- `frontend-next/src/app/(auth)/auth/login/page.tsx`
- `frontend-next/src/app/(auth)/auth/register/page.tsx`

仅限：

- 增加指向 `/auth` 的入口
- 轻量文案说明“手机号验证码是当前主路径”
- 不做整页重设计

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
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/marketing/primary-cta.tsx`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/marketing/pricing-grid.tsx`

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
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/middleware.ts`
- 任意 payment / subscription / billing migration 文件

说明：

- `middleware.ts` 目前已允许 `/auth/*` 公开访问，本轮默认不要碰
- 如果你发现为了 `/auth` 公开访问必须改 `middleware.ts`，请先回报 blocker；按当前代码看通常不需要
- 不要把 `Task 3` 扩成 `Task 4`

## 推荐实施顺序

1. 先做 schema / migration 决策
   - `phone_number`
   - `phone_verified_at`
   - `trial_granted_at`
   - `trial_ends_at`
   - 如确有必要，再做 `email/password_hash` nullable 兼容
   - 如确有必要，再加一个轻量 challenge 表
2. 再做 `sms_provider.py` 与 `risk_control.py`
3. 再做 `auth_phone.py`
4. 再在 `main.py` 挂载 phone auth 路由
5. 再做 `/auth` 页面与两个 auth 组件
6. 最后做 marketing guest CTA 最小改向

## 必须覆盖的测试点

### Backend

`tests/test_auth_phone.py` 至少覆盖：

1. 正常手机号发送验证码（fake provider）
2. 未通过 captcha 不发短信
3. 手机号限频命中
4. IP 限频命中
5. 验证码正确时成功登录 / 注册并建立 session
6. 验证码错误时拒绝

`tests/test_trial_grant_rules.py` 至少覆盖：

1. 首次验证手机号时记录 `trial_granted_at`
2. 同一手机号不会重复领取 Trial
3. 重复验证同一手机号时返回同一用户，而不是新建第二个账户
4. 在 `trial` 未冻结的情况下，不会偷偷写入硬编码天数 / 分钟数

如果你引入轻量 challenge 表，也要覆盖：

5. 已消费验证码不能重复使用
6. 过期验证码无效

### Frontend

至少确保：

1. `/auth` 页面能构建并正常渲染
2. guest marketing CTA 改向 `/auth`
3. `/auth/login` 与 `/auth/register` 仍可访问

## 验证要求

至少运行：

1. `pytest tests/test_auth_phone.py tests/test_trial_grant_rules.py -q`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp`
2. `npm run lint`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next`
3. `npm run build`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next`

如果你新增了 Alembic migration，还必须验证：

4. `alembic upgrade head`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway`

并做最小浏览器核验：

1. `/auth` 返回 200，手机号表单可见
2. fake captcha 通过后可以发送验证码
3. fake provider 路径下可以完成验证码验证并建立 session
4. guest homepage / pricing CTA 指向 `/auth`
5. `/auth/login` 与 `/auth/register` 仍返回 200
6. 控制台 0 errors

如果你无法在浏览器中完整跑通 fake 验证码 / fake 短信链路，请明确说明阻塞点，不要假装验证通过。

## 完成后必须写回汇报

请写一份新的 `report` 到：

- `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/`

建议文件名：

- `2026-04-05_22xxxx_from-Claude-Code_to-CodeX_type-report_task-T3_stage-complete.md`

请至少包含：

1. 本轮执行范围
2. schema / migration 决策
3. 是否对 `users.email` / `password_hash` 做了 nullable 兼容
4. 是否引入了 challenge 表；如果有，引入原因是什么
5. phone auth API 形状
6. fake SMS / fake captcha 如何工作
7. Trial bookkeeping 如何处理未冻结事实
8. frontend `/auth` 与 CTA 改向结果
9. 实际修改文件
10. `pytest`
11. `npm run lint`
12. `npm run build`
13. `alembic upgrade head`（如有 migration）
14. 浏览器核验结果
15. 风险与 blocker
16. 是否已停止

## 结论

本轮的成功标准是：

- 建立 phone-first auth 主入口
- 保留 email/password 兼容
- fake provider 下可本地跑通
- Trial 只做最小 bookkeeping，不擅自冻结数字与 entitlement mapping

修完后停止，等待 CodeX 审核。
