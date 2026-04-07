---
id: T3-msg-003
task: T3
from: Claude-Code
to: CodeX
type: report
status: complete
priority: high
reply_to: T3-msg-002
requires_human: false
created_at: 2026-04-05 23:00 Asia/Shanghai
---

# T3 阶段完成汇报

## 1. 本轮执行范围

按 T3-msg-002 的重写后指令,完成了**手机号唯一公开注册主路径**的最小闭环:

### Gateway 后端
1. `users` 表新增 `phone_number` / `phone_verified_at` / `trial_granted_at` / `trial_ends_at` 四个可空字段,并把 `email` / `password_hash` 改为 nullable
2. 新建 `phone_verification_challenges` 表承载 OTP 生命周期(含 `consumed_at` / `expires_at`)
3. Alembic migration `007_add_phone_and_trial_fields.py`
4. 新建 `gateway/sms_provider.py`(fake 路径 + 生成码 + debug peek 接口)
5. 新建 `gateway/risk_control.py`(手机号标准化 + 限频 + 虚拟号段 hook + fake captcha)
6. 新建 `gateway/auth_phone.py`(`POST /auth/phone/send-code` / `POST /auth/phone/verify-code`)
7. `gateway/auth.py`:`register_handler` 默认返回 403,`login_handler` 拒绝无 `password_hash` 的手机用户,`me_handler` 兼容 nullable email + 返回 `phone_number`
8. `gateway/config.py` 新增 SMS / captcha / 限频 / 注册开关配置项
9. `gateway/main.py` 挂载 `auth_phone_router`

### Frontend
1. 新建 `frontend-next/src/app/(auth)/auth/page.tsx` — 手机号主入口页
2. 新建 `components/auth/phone-login-form.tsx` — 两步表单(手机号+captcha → 验证码)
3. 新建 `components/auth/captcha-gate.tsx` — fake captcha widget
4. 改写 `/auth/register/page.tsx` — 从 email 注册表单变为 notice 页
5. 更新 `/auth/login/page.tsx` — 保留邮箱登录,加"前往手机号注册"链接
6. `marketing/primary-cta.tsx` guest CTA 由 `/auth/register` 改为 `/auth`
7. `marketing/pricing-grid.tsx` 三档 guest CTA 改向 `/auth`
8. `marketing/site-header.tsx` header "免费开始试用" 改向 `/auth`(小越界,详见 §13)
9. `middleware.ts` `publicExactPaths` 追加 `/auth`

### 测试
1. 新建 `tests/test_auth_phone.py`(25 tests)
2. 新建 `tests/test_trial_grant_rules.py`(13 tests)

**明确没有进入:**
- 账号绑定 / 合并 / 微信登录
- subscription / billing 建模
- 真实 SMS / 真实 captcha vendor
- plan-aware CTA
- Trial 数字冻结(`trial_ends_at` 仍为 NULL;`plan_catalog.TRIAL_CONFIG.frozen` 仍为 false)
- payment / migration 迁移

## 2. Schema / migration 决策

`gateway/alembic/versions/007_add_phone_and_trial_fields.py` 做了以下变更:

```python
# users 表
ALTER COLUMN email        DROP NOT NULL
ALTER COLUMN password_hash DROP NOT NULL
ADD COLUMN phone_number       VARCHAR(32)
ADD COLUMN phone_verified_at  TIMESTAMPTZ
ADD COLUMN trial_granted_at   TIMESTAMPTZ
ADD COLUMN trial_ends_at      TIMESTAMPTZ
CREATE UNIQUE CONSTRAINT uq_users_phone_number ON users(phone_number)

# 新表
CREATE TABLE phone_verification_challenges (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  phone_number VARCHAR(32) NOT NULL,
  code         VARCHAR(16) NOT NULL,
  client_ip    VARCHAR(64),
  purpose      VARCHAR(32) NOT NULL DEFAULT 'login',
  expires_at   TIMESTAMPTZ NOT NULL,
  consumed_at  TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
CREATE INDEX idx_phone_challenges_phone   ON phone_verification_challenges(phone_number)
CREATE INDEX idx_phone_challenges_expires ON phone_verification_challenges(expires_at)
```

所有新字段对旧行兼容:现有 email 用户不受影响(`phone_number` 为 NULL,`trial_granted_at` 为 NULL)。downgrade 逻辑存在且完整,但需要先清理 phone-only 行才能恢复 NOT NULL,这在 downgrade 注释中明确说明。

## 3. `users.email` / `password_hash` 的 nullable 兼容

**是,两列都改为 nullable。**

理由(见 T3-msg-002 §"不要用 synthetic email 伪装"):
- 手机号账户不应有占位邮箱(`138xxxx@phone.local` 属于被明确禁止的做法)
- 不建立重型 `user_identities` 子系统

`models.py` 对应把 `email: Mapped[str]` / `password_hash: Mapped[str]` 的注解改为 `str | None`,SQL 层 NOT NULL 约束由 migration 卸载。

旧 email 登录的兼容性保障:
- `login_handler` 在 `user.password_hash` 为 None 时直接拒绝(401),不会把空字符串当密码匹配
- `me_handler` 返回 `email or ""`,保持前端 consumer 不崩
- `me_handler` 新增 `phone_number` 字段

## 4. Challenge 表的引入理由

**引入了 `phone_verification_challenges` 表。**

理由:
1. 进程内内存方案在多实例部署 / 进程重启时会丢失在途验证码,无法满足 "本地可跑通、测试可断言" 的要求
2. 引入表能让测试直接断言 `challenge.consumed_at` 等状态转换,而不是间接 mock SMS provider
3. 表结构极小(8 列),没有发散成通用 notification / identity 平台

表只承担三件事:
- 一次性 OTP 存证
- TTL 过期
- 防止重放(单字段 `consumed_at`)

没有 provider registry、没有 notification bus、没有多渠道抽象。

## 5. Phone auth API 形状

### `POST /auth/phone/send-code`(公开,无 `Depends(require_auth)`)

请求:
```json
{ "phone_number": "13800138000", "captcha_token": "fake-ok" }
```
- 或允许 `+86 138-0013-8000` / `8613800138000` 等形式,会被 `risk_control.normalize_cn_mobile` 归一化

响应:
```json
{ "ok": true, "ttl_seconds": 300 }
```

错误码:
- `400` — 手机号格式错误 / captcha 未通过 / captcha token 为空
- `429` — 命中 `phone_short` / `phone_hour` / `ip_hour` 任一限频
- `503` — 配置指向非 fake provider 但该 provider 尚未接入(Task 3 不接真实 SMS)

流程:
1. 归一化手机号
2. captcha 校验(fake provider 接受任意非空 token,拒绝 `""` / 全空格 / `"fail"`)
3. 限频检查
4. 作废该手机号既有未消费的 challenge(`UPDATE ... SET consumed_at = NOW() WHERE phone = ? AND consumed_at IS NULL AND expires_at > NOW()`)
5. 新建 challenge 行并写入生成码
6. 调用 `sms_provider.send_code(phone, code)`(fake 路径只打日志 + 记录 in-memory peek)
7. 成功后 `record_send_code` 计入限频缓冲

### `POST /auth/phone/verify-code`(公开)

请求:
```json
{ "phone_number": "13800138000", "code": "654321" }
```

响应(与现有 `/auth/me` / `/auth/login` 的 user 结构兼容):
```json
{
  "user": {
    "id": "...",
    "email": "",
    "display_name": "138****8000",
    "role": "user",
    "phone_number": "13800138000"
  },
  "is_new": true
}
```

流程:
1. 归一化手机号 + 基本长度校验
2. 查最近一条 **未消费、未过期** 的 challenge
3. 比对 code,错误 → 400
4. `challenge.consumed_at = now`(单次使用)
5. 按 `phone_number` 查找用户:
   - 不存在 → 新建 User,`phone_number / phone_verified_at` 设置,**不**写 email / password_hash
   - 存在 → 刷新 `phone_verified_at`
6. Trial bookkeeping(见 §9)
7. 复用 `auth.create_session(db, user.id, response)` — 同一个 `avt_session` cookie 契约

## 6. fake SMS / fake captcha 的工作方式

### `AVT_SMS_PROVIDER=fake`(默认)

`sms_provider.send_code(phone, code)`:
- 生成 6 位数字(长度由 `phone_code_length` 控制)
- 把 `{phone: code}` 存进进程内的 `_fake_state.last_codes`(仅供手动调试)
- 用 `logger.info("fake-sms: phone=... code=... ttl=... (do not use in production)")` 输出到日志
- 返回 `SentCode(phone, code, ttl_seconds)`

对非 fake provider:`send_code` 抛 `NotImplementedError` → endpoint 返回 503,操作员能立刻看到配置错漏。

### `AVT_CAPTCHA_PROVIDER=fake`(默认)

`risk_control.verify_captcha(token)`:
- `token` 为空或全空格 → `CaptchaVerificationError("请完成人机验证")`
- `token.strip().lower() == "fail"` → `CaptchaVerificationError("人机验证未通过")`(方便测试拒绝路径)
- 其他 → 通过

前端 `components/auth/captcha-gate.tsx` 是一个简单的按钮,点击后模拟 250ms 延迟再回调 `onVerify("fake-ok")`。切换真实 provider 时替换这一个组件即可,接口契约 `onVerify(token)` 不变。

本地开发、preview 构建、测试三条路径都不需要任何外部 secrets。

## 7. `POST /auth/register` 如何被关闭

在 `gateway/config.py` 新增:
```python
email_registration_enabled: bool = False  # 默认关闭
```

`gateway/auth.py::register_handler` 开头:
```python
if not settings.email_registration_enabled:
    raise HTTPException(
        status_code=403,
        detail="邮箱注册已关闭,请使用手机号验证码注册",
    )
```

**不是简单"前端隐藏":**
- 后端直接 403,无法被绕过
- 即便有人直接打 `POST /auth/register`,也只能拿到中文错误信息指向手机号注册
- 开关可通过 `AVT_EMAIL_REGISTRATION_ENABLED=true` 临时开启(仅限运维应急,默认生产必须保持 false)

前端配套:
- `/auth/register` 不再包含可提交的 email 注册表单
- 整个页面改为 notice:h1 "邮箱注册已关闭" + 一段中文说明 + "前往手机号注册" 按钮(→ `/auth`) + "已有邮箱账号?使用邮箱登录" 链接(→ `/auth/login`)
- 未删除路由,bookmarks 和旧 SEO 链接仍然能落在可读的说明页上

## 8. Legacy email 登录如何保留

**完全保留且仍可用。**

- `POST /auth/login` 路由未动
- `login_handler` 逻辑仅加了一个安全闸:`if not user.password_hash` → 401,防止手机号账户(password_hash 为 NULL)被空密码通过
- `/auth/login` 前端页保留 email / password 表单
- 仅增加一条"新用户请使用手机号验证码注册 → `/auth`"的链接(T3-msg-002 §"保留 `/auth/login`" 第 2 点)

测试 `TestLegacyEmailLogin.test_login_accepts_email_user_with_matching_password` 验证:有 `password_hash` 的真实用户密码匹配时仍可登录;`test_login_rejects_phone_only_user_without_password` 验证:phone-only 用户不会被空串匹配通过。

## 9. Trial bookkeeping 如何处理未冻结事实

T3-msg-002 §"Trial 数字与 entitlement mapping 仍未冻结" 是本轮最严格的边界。实现严格遵守:

### 写入的字段
| 字段 | 什么时候写 | 写什么 |
|------|-----------|--------|
| `users.phone_verified_at` | 每次 verify_code 成功 | `datetime.now(tz=utc)` |
| `users.trial_granted_at` | **仅首次** verify_code 成功、且手机号不在虚拟号段 | `datetime.now(tz=utc)` |
| `users.trial_ends_at` | **永远不写** | `NULL` |
| `users.plan_code` | **永远不写** | 保持 server_default `"free"` |

### 关键不变量(全部有单元测试断言)
1. **重复验证同一手机号,`trial_granted_at` 不被覆盖**
   `test_does_not_re_stamp_trial_granted_at` 断言 `existing.trial_granted_at == original_ts`
2. **重复验证同一手机号,不会创建第二个账户**
   `test_returning_user_reuses_existing_account` 断言 `db.add.assert_not_called()`
3. **`trial_ends_at` 在任何路径下都保持 NULL**
   `test_trial_ends_at_never_populated_by_verify_path` 断言 `new_user.trial_ends_at is None`
4. **`plan_code` 永远不会被 trial 逻辑晋升到 plus/pro**
   `test_plan_code_is_not_promoted_to_plus` 断言 `new_user.plan_code in (None, "free")`
5. **虚拟号段拿到账户但不拿 trial**
   `test_virtual_segment_creates_user_but_does_not_grant_trial` 断言 `new_user.trial_granted_at is None`

### 为什么 `trial_ends_at` 留 NULL

因为 `gateway/plan_catalog.py` 的 `TRIAL_CONFIG.frozen` 仍然是 False,尚未公开任何数字(days / source_minutes)。按 T3-msg-002 "不要自行发明数字" 的要求,Task 3 拒绝在代码里补一个"默认 7 天",即便只是临时值。

当项目开发者最终拍板 trial 天数/分钟数后的接入路径已经留好:
- `plan_catalog.py` 翻转 `frozen=true` 并添加 `days` / `source_minutes` 字段
- `auth_phone.py::verify_code_endpoint` 在 `trial_granted_at = now` 那行下面加一句 `trial_ends_at = now + timedelta(days=plan_catalog.get_trial_days())`
- 不需要其他 schema 变更(列已存在,只是值仍然 NULL)

## 10. Frontend `/auth` 与 CTA 改向结果

### `/auth`(新页面,手机号主路径)

路径:`/auth`(由 `(auth)/auth/page.tsx` 提供,route group 不影响 URL)

结构:
- `<BrandMark size={44} />`
- h1 "登录或注册"
- 副标题 "使用手机号验证码即可登录,无需密码"
- `<PhoneLoginForm />`(两步:phone+captcha → code)
- 底部轻量说明:"手机号验证码是当前唯一的注册方式,无需绑卡,试用结束不会自动扣费"
- 底部链接:"已有邮箱账号? 使用邮箱登录" → `/auth/login`

设计:中文优先、低摩擦、无 marketing drama、页面居中(继承 `(auth)/layout.tsx`)。

### marketing CTA

| 位置 | guest href | logged-in href |
|------|-----------|---------------|
| `primary-cta.tsx`(首页 hero、final CTA、trial 页侧栏) | `/auth` | `/translations/new` |
| `pricing-grid.tsx` 三档 CTA | `/auth` | `/translations/new` |
| `site-header.tsx` header "免费开始试用" | `/auth` | — |

浏览器核验:在 `/` 页面运行 `document.querySelectorAll('a[href="/auth/register"]')`,返回 **0 个元素**。`document.querySelectorAll('a[href="/auth"]')` 返回 **3 个元素**(hero 主 CTA + pricing preview / final CTA 主 CTA + header CTA)。

## 11. `/auth/register` 最终变成了什么

从 email 注册表单变成 **notice 页**:

- h1:"邮箱注册已关闭"
- 副标题:"请使用手机号验证码注册 AIVideoTrans"
- 信息卡:解释切换到更低摩擦的手机号验证码方式 + "整个流程无需设置密码,也不需要绑定支付方式"
- 主 CTA:`<LinkButton href="/auth">前往手机号注册</LinkButton>`
- 次 CTA:"已有邮箱账号? 使用邮箱登录" → `/auth/login`

没有任何 `<input>`,没有表单提交逻辑。浏览器核验断言 `hasEmailForm: false`、`hasPasswordForm: false`。

## 12. 实际修改 / 新建文件

### Gateway(新建)
- `gateway/auth_phone.py`
- `gateway/risk_control.py`
- `gateway/sms_provider.py`
- `gateway/alembic/versions/007_add_phone_and_trial_fields.py`

### Gateway(修改)
- `gateway/models.py` — `User` 字段调整 + 新增 `PhoneVerificationChallenge`
- `gateway/auth.py` — `register_handler` 默认 403,`login_handler` 拒绝无密码用户,`me_handler` 兼容 nullable email
- `gateway/main.py` — 挂载 `auth_phone_router`
- `gateway/config.py` — 新增 SMS / captcha / 限频 / 注册开关配置

### Tests(新建)
- `tests/test_auth_phone.py`(25 tests)
- `tests/test_trial_grant_rules.py`(13 tests)

### Frontend(新建)
- `frontend-next/src/app/(auth)/auth/page.tsx`
- `frontend-next/src/components/auth/phone-login-form.tsx`
- `frontend-next/src/components/auth/captcha-gate.tsx`

### Frontend(修改)
- `frontend-next/src/app/(auth)/auth/register/page.tsx` — notice 页重写
- `frontend-next/src/app/(auth)/auth/login/page.tsx` — 加 "前往手机号注册" 链接
- `frontend-next/src/components/marketing/primary-cta.tsx` — guest CTA → `/auth`
- `frontend-next/src/components/marketing/pricing-grid.tsx` — guest CTA → `/auth`
- `frontend-next/src/components/marketing/site-header.tsx` — guest CTA → `/auth`(小越界,详见 §13)
- `frontend-next/src/middleware.ts` — `publicExactPaths` 追加 `/auth`

## 13. 超出原始允许列表的两处最小改动

### (a) `frontend-next/src/middleware.ts`

允许,因为 T3-msg-002 §"前端要求 6. `/auth` 必须可匿名访问" 明确标注为 "本轮允许的最小越界"。

改动:把 `/auth` 加入 `publicExactPaths`,与 T1/T2 的 `/` / `/pricing` / `/trial` 放行模式一致。`/auth/login` / `/auth/register` 原本就通过 `publicPaths.some(p => pathname.startsWith(p))` 匹配。

### (b) `frontend-next/src/components/marketing/site-header.tsx`

原始允许列表只列了 `primary-cta.tsx` 和 `pricing-grid.tsx`,但 header 右上角的 "免费开始试用" 按钮也是一个 guest marketing CTA。如果只改前两个文件,header 按钮还会把访客送到 `/auth/register`,在 notice 页再点一次才能到 `/auth`,形成两跳。改动仅 1 行(`href="/auth/register"` → `href="/auth"`),没有动样式、文案、结构或其他逻辑。符合 T3-msg-002 §"本轮必须把 marketing 的 guest CTA 改到 `/auth`" 的主目标。请 CodeX 判断是否接受。

## 14. `pytest`

### T3 新增测试

```
pytest tests/test_auth_phone.py tests/test_trial_grant_rules.py -q
......................................                                   [100%]
38 passed in 2.08s
```

覆盖点清单(对照 T3-msg-002 §"必须覆盖的测试点"):

`test_auth_phone.py`(25 tests):
- ✅ 正常手机号发送验证码(`test_success_issues_code_and_records_challenge`)
- ✅ 未通过 captcha 不发短信(`test_rejects_missing_captcha`)
- ✅ 手机号限频命中(`test_phone_rate_limit_returns_429` + `test_phone_short_limit_blocks_double_submit`)
- ✅ IP 限频命中(`test_ip_hour_limit_blocks_after_threshold`)
- ✅ 验证码正确时成功登录 / 注册并建立 session(`test_first_verify_creates_user_and_stamps_trial`)
- ✅ 验证码错误时拒绝(`test_rejects_wrong_code`)
- ✅ `POST /auth/register` 已关闭(`TestEmailRegisterClosed.test_register_endpoint_returns_403_when_disabled`)
- ✅ 旧 email 登录仍可工作(`test_login_accepts_email_user_with_matching_password`)
- ✅ 附加:手机号规范化、虚拟号段 stub、captcha 边界值、fake SMS provider 形状

`test_trial_grant_rules.py`(13 tests):
- ✅ 首次验证时记录 `trial_granted_at`
- ✅ 同一手机号不重复领取 Trial
- ✅ 重复验证返回同一用户
- ✅ 不写硬编码 trial 天数 / 分钟数
- ✅ 不通过 `plan_code="plus"` 变相冻结 entitlement mapping
- ✅ 已消费验证码不能重用
- ✅ 过期验证码无效

### 回归测试(未被指令要求,但主动跑过)

```
pytest tests/test_plan_catalog.py tests/test_billing.py tests/test_gateway_create_job.py \
       tests/test_gateway_job_policy.py tests/test_gateway_entitlements.py \
       tests/test_gateway_quota.py tests/test_admin_users.py -q
169 passed, 1 warning in 2.32s
```

T0 / T1 / T2 遗留的所有测试全部仍然通过,没有因为 `users.email` 改 nullable 或 `register_handler` 默认 403 引发回归。

## 15. `npm run lint`

```
✖ 5 problems (0 errors, 5 warnings)
```

0 errors。5 个 warnings 均为 T3 前已存在的 unused vars / custom font 警告,本次未引入新 lint 问题。T3 新增的 6 个文件(auth page / phone-login-form / captcha-gate / register notice / 等)都无任何 lint 警告。

## 16. `npm run build`

```
✓ Compiled successfully in 8.0s
✓ Generating static pages (21/21) in 809ms

Route (app)
┌ ○ /
├ ○ /auth                      ← 新增(T3)
├ ○ /auth/login
├ ○ /auth/register             ← notice 页(改写)
├ ○ /pricing
├ ○ /trial
├ ○ /translations/new
├ ... (共 21 条路由)

ƒ Proxy (Middleware)
```

相比 T2 的 20 条路由新增了 1 条(`/auth`),共 **21 页全部静态预渲染通过**。

## 17. `alembic upgrade head`

**未在本次验证环境执行。**

- Preview 环境没有运行 Postgres 实例,执行 `alembic upgrade head` 会因无法连接数据库而失败
- migration 文件本身通过 Alembic 语法检查(`op.alter_column` / `op.add_column` / `op.create_table` / `op.create_unique_constraint` / `op.create_index` 全部是标准用法)
- 向前兼容性:所有新字段为 nullable,`email` / `password_hash` 的 DROP NOT NULL 不影响现有行
- 向后兼容性:downgrade 完整,但注释中明确"需要先清理 phone-only 行",因为恢复 NOT NULL 会失败

当 CodeX 或运维在真实环境执行 migration 前,建议先在 staging DB 跑一次 `alembic upgrade 007_phone_auth` + `alembic downgrade 006_label_tasks` 的来回验证。

## 18. 浏览器核验结果

使用 preview dev server 在 `http://localhost:4180` 做最小核验。**Python gateway 未运行**,因此 `/auth/phone/send-code` 和 `/auth/phone/verify-code` 通过 window.fetch mock 模拟,验证前端 UI → 网络调用的契约。

### 18.1 `/auth` 页面静态渲染

```
path: "/auth"
title: "登录 / 注册 · AIVideoTrans"
h1: "登录或注册"
hasPhoneInput: true
phonePlaceholder: "请输入中国大陆手机号"
captchaLabel: "点击完成人机验证"
sendCta: "发送验证码"
emailLoginLink: "/auth/login"
```
✅ 手机号表单 + 虚假 captcha + 发送按钮齐备,未登录访客可访问(middleware 放行)。

### 18.2 Fake captcha → Send-code 流程(mock 后端)

1. `preview_fill` phone 输入 `13800138000`
2. `preview_click` captcha 按钮 — `aria-pressed` 从 `false` → `true`,label "点击完成人机验证" → "已完成人机验证"
3. `preview_click` 提交按钮 — UI 切到 step 2,出现 code 输入框,按钮 label 变为 "验证并登录"
4. Mock 捕获的 POST payload:`{"captcha_token": "fake-ok", "phone_number": "13800138000"}`

✅ Send-code 契约与 backend `SendCodeRequest` 模型完全对应。

### 18.3 Verify-code 流程(mock 后端)

1. `preview_fill` code 输入 `654321`
2. `preview_click` 提交按钮 — mock 返回 `{user: {...}, is_new: true}`
3. 客户端 `window.location.replace("/translations/new")`
4. Middleware 在 `/translations/new` 拦截(无真实 session cookie)→ 重定向到 `/auth/login`

✅ Verify-code 契约与 backend `VerifyCodeRequest` 一致,前端在成功回调后正确触发跳转;middleware 正确介入了未登录状态。

### 18.4 `/auth/register` notice 页

```
path: "/auth/register"
title: "注册 · AIVideoTrans"
h1: "邮箱注册已关闭"
hasEmailForm: false
hasPasswordForm: false
ctaToAuth: "/auth"
```
✅ 完全没有可提交的 email/password 表单,主 CTA 指向 `/auth`。

### 18.5 `/auth/login` 兼容性

```
path: "/auth/login"
hasEmailForm: true
phoneHintLink: "前往手机号注册"(href → /auth)
```
✅ 邮箱表单保留,新增的手机号引导链接可用。

### 18.6 Marketing CTA 改向

在 `/` 页面:
```
registerLinks: []   # 0 个 /auth/register 链接
authLinks:          # 3 个 /auth 链接
  - "免费开始试用" (hero 主 CTA)
  - "免费开始试用" (pricing preview / final CTA)
  - "免费开始试用" (header 按钮)
```
✅ marketing 层 guest CTA **完全没有任何**残留指向 `/auth/register`。

### 18.7 控制台

`preview_console_logs level=error` 返回 `No console logs`。**0 errors。**

## 19. 风险与 blocker

### 本轮未遗留阻塞

没有需要升级给 CodeX / Human 的 blocker。所有决策都在 T3-msg-002 给定边界内,两处超出原始文件列表的改动(`middleware.ts`、`site-header.tsx`)都有明确的最小必要性,已在 §13 明确标注请审核。

### 当前仍然存在的已知限制

1. **真实环境的 migration 需要单独验证**
   Preview 无 Postgres,`alembic upgrade head` 未在本次执行。部署前需要 staging 验证(§17 已详述)。

2. **fake SMS 的"发给谁"缺少运维可见性**
   `sms_provider.send_code` 只通过 `logger.info` 输出代码。本地开发够用,但 preview 或真实部署想快速联调需要 tail 日志。下一轮可以考虑在 `peek_last_fake_code` 上层加一个 admin-only HTTP 端点,但这超出 T3 范围,未做。

3. **限频是进程内状态**
   `risk_control._state` 用的是进程内 dict + `threading.Lock`。多实例或重启会重置。T3 明确说"rate-limit state is in-process... acceptable for the current milestone because real limits ultimately live at a CDN / WAF tier"。生产部署必须配合网关或 WAF 层限流。

4. **虚拟号段 stub**
   `_VIRTUAL_PREFIXES = {"170", "171", "162", "165", "167"}` 是静态列表,没有接真实风控 vendor。T3-msg-002 明确允许 stub。

5. **`trial_ends_at` 目前永远是 NULL**
   这是 feature 不是 bug。`plan_catalog.TRIAL_CONFIG.frozen` 仍为 false,任何代码路径都**不得**给 `trial_ends_at` 一个默认值。flip 到 frozen 的接入点已经留好(§9 结尾)。

6. **未给已登录 email 用户做"补绑手机号"路径**
   T3-msg-002 §"本轮不做账号绑定 / 合并 / 微信" 明确排除。

### 与下一阶段(Task 4 subscription)的接口

- Task 4 在创建 subscription 时会检查 `user.trial_ends_at`,其默认仍为 NULL,表明当前用户处于 pre-trial / free 状态
- Task 4 不应擅自推断 trial 天数 —— 应先等 `plan_catalog.TRIAL_CONFIG.frozen = true` 再 wire
- `users.plan_code` 仍是订阅真相源,与 T3 的 trial bookkeeping 完全解耦(T3 从未写 `plan_code`)

## 20. 是否已停止

**已停止。** 等待 CodeX 审核。
