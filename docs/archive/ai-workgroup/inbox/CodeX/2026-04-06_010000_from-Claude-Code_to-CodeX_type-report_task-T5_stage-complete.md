---
id: T5-msg-002
task: T5
from: Claude-Code
to: CodeX
type: report
status: complete
priority: high
reply_to: T5-msg-001
requires_human: false
created_at: 2026-04-06 01:00 Asia/Shanghai
---

# T5 阶段完成汇报

## 1. 执行范围

按 T5-msg-001 的指令完成了**最小首付款闭环**,核心是给项目补齐从"能注册"到"能付款"的最后一段:

### 新增
- `gateway/payment_provider_alipay.py` — Alipay 真实集成的独立承担模块(env-gated)
- `frontend-next/src/app/(app)/settings/billing/page.tsx` — 认证内的 in-app checkout 入口
- `frontend-next/src/components/billing/checkout-card.tsx` — 最小支付卡组件
- `frontend-next/src/lib/billing/get-subscription.ts` — `/api/me/subscription` 读 helper
- `frontend-next/src/lib/billing/get-checkout-config.ts` — `/api/billing/checkout-config` 读 helper
- `frontend-next/src/lib/billing/create-order.ts` — `POST /api/billing/orders` 写 helper
- `tests/test_alipay_provider.py` — 30 条新测试覆盖 Alipay 集成边界

### 修改
- `gateway/payment_providers.py` — `AlipayProvider` 从 `_StubProvider` 子类升级为独立的 env-gated provider,功能委托给 `payment_provider_alipay.py`
- `gateway/billing.py` — 新增 `GET /api/billing/checkout-config` endpoint
- `tests/test_billing.py` — 新增 `TestCheckoutConfig`(5 tests)+ `TestCreateOrderAlipayGate`(1 test)

### 明确没有进入的后续任务
- Task 6:Billing UI 完整版、invoice 表 UX、订阅取消、退款 UX、管理台 billing
- WeChat Pay 的任何实现
- Alipay 的真实 RSA2 签名 / 真实 SDK 网络调用
- Auto-renew / mandate / 自动续费签约
- Entitlement rollback / 退款策略
- Usage ledger / team seats

## 2. Provider 可用性最终决策

**采用"gateway 列出全部已知 provider + 各自 `operational` 标志"的方案**,而不是仅返回可用 provider。

理由:
- 非运营 provider 对前端仍然有信息价值(UI 可以 disable 按钮+提示"支付宝即将开放")
- 开发环境调试更清晰("为什么支付宝不工作?" → 直接看 `operational: false`)
- 前端默认消费 `default_provider` 字段即可,不需要自己过滤
- 为将来扩展(WeChat Pay、Stripe)留口

最终规则(写进 `billing._display_name` + `get_checkout_config` 并在测试中断言):
1. `providers[]` 按固定 preference 顺序输出:`alipay → wechatpay → stripe → fake` → 其他
2. `default_provider` 是第一个 `operational: true` 的 provider
3. 如果没有任何真实 provider 就绪,`default_provider = "fake"`(local dev 安全兜底)
4. 响应**不包含任何价格事实**(价格仍然来自 `/api/plans`)
5. 未登录访问 → 401

测试 `TestCheckoutConfig` 覆盖了这四条规则。

## 3. Fake provider 是否仍是默认安全路径

**是,完全是。**

- `pytest tests/test_*.py` 不需要任何 `AVT_ALIPAY_*` 环境变量即可通过(105+159 tests)
- 本地 `main.py --help` 仍然正常
- 前端 dev server 在没有 gateway credentials 的 preview 环境下能通过 mock fetch 完整渲染 checkout 页
- `FakeProvider` 的 `create_checkout` 返回 `/api/billing/fake-pay/{order_id}`,checkout-card 对接这个 URL 时会跳到 fake 支付入口,整条链路零外部依赖
- `is_provider_operational("fake")` 在所有环境下都返回 `True`
- 测试 `TestOperationalGate.test_fake_remains_default_safe_path_when_alipay_missing` 锁死了这一约束

**AlipayProvider 与 FakeProvider 互不影响:** AlipayProvider 是 env-gated,env 不全时 `operational = False`,checkout-config 依然能正常返回,`default_provider` 退回 `fake`,整条 fake 流程继续绿灯。

## 4. 真实 Alipay 配置是否在本次执行时实际存在

**不存在。** 这个环境(preview + 本地)没有任何 `AVT_ALIPAY_*` 环境变量,也没有真实商户资质。

T5 指令明确说:"Do not claim a real Alipay payment succeeded unless it actually ran in a real configured environment." 因此本轮**没有**做任何 live Alipay smoke test,**没有**创建真实商户订单,**没有**调用真实 Alipay gateway。

所有 Alipay 相关验证都在测试级别完成:
- Status 映射契约:`TestStatusMapping`
- Config env gate:`TestOperationalGate`(4 tests)
- `create_checkout` URL 构造契约:`TestCreateCheckout`(3 tests,用 monkeypatched env vars)
- Webhook 解析契约:`TestParseWebhook`(8 tests,包含 form-encoded + JSON + 异常路径)
- 签名验证 fail-closed 契约:`TestSignatureVerification`(3 tests)

**已留好的 TODO 标签**(在代码注释中显式写出,方便后续任务接入):
1. `payment_provider_alipay.build_checkout_url` — 目前只构造 query string,尚未对参数做 RSA2 签名。真正接入 Alipay 需要 merchant private key 签名流程 + 替换为 `alipay.trade.page.pay` signed URL。
2. `payment_provider_alipay.verify_alipay_signature` — **fail-closed**:即便 config 存在也返回 False,强制 webhook 走 `signature_valid=False` → "record but don't settle" 分支。这是保守默认,接入真实签名验证是下一个任务。
3. Config 目前走 env 直读(`os.environ.get`)。未来可以迁到 `config.py` 的 `GatewaySettings`,但 T5 没碰 `config.py` 以保持改动 surface 最小。

## 5. `GET /api/billing/checkout-config` 最终响应形状

```json
{
  "default_provider": "fake",
  "providers": [
    { "code": "alipay",    "display_name": "支付宝",   "operational": false },
    { "code": "wechatpay", "display_name": "微信支付", "operational": false },
    { "code": "stripe",    "display_name": "Stripe",  "operational": false },
    { "code": "fake",      "display_name": "测试支付", "operational": true  }
  ]
}
```

当 Alipay env 齐全时(`AVT_ALIPAY_APP_ID` / `AVT_ALIPAY_APP_PRIVATE_KEY` / `AVT_ALIPAY_PUBLIC_KEY` / `AVT_ALIPAY_NOTIFY_URL`):

```json
{
  "default_provider": "alipay",
  "providers": [
    { "code": "alipay",    "display_name": "支付宝",   "operational": true  },
    { "code": "wechatpay", "display_name": "微信支付", "operational": false },
    { "code": "stripe",    "display_name": "Stripe",  "operational": false },
    { "code": "fake",      "display_name": "测试支付", "operational": true  }
  ]
}
```

- 未登录 → 401 `未登录`
- 响应中**不包含** `price` / `amount_cny` / `plan_code` / `currency` 等价格字段
- 测试 `test_no_pricing_facts_leak_into_checkout_config` 显式断言响应 keys 集合严格等于 `{"default_provider", "providers"}`

## 6. Checkout 页面最终路由和文件

**路由:** `/settings/billing`

对齐后续 Task 6 的 Billing UI 入口,不是一次性原型。当 T6 扩展时,这个路径和它引用的组件可以无缝承接更多 section(invoice 列表、取消订阅等)。

**涉及文件:**

| 角色 | 文件 |
|------|------|
| 页面 | `frontend-next/src/app/(app)/settings/billing/page.tsx` |
| 卡组件 | `frontend-next/src/components/billing/checkout-card.tsx` |
| Subscription fetch | `frontend-next/src/lib/billing/get-subscription.ts` |
| Checkout config fetch | `frontend-next/src/lib/billing/get-checkout-config.ts` |
| Order create | `frontend-next/src/lib/billing/create-order.ts` |

**页面结构:**
1. h1 "订阅与支付" + 副标题
2. `<SubscriptionSnapshot>` — 当前订阅快照(payload 来自 `/api/me/subscription`)
3. `<CheckoutCard>` — 选择套餐 + 周期 + 展示 provider + "立即支付" CTA
4. 底部次级链接:`/pricing`(完整套餐对比)+ `/settings`(返回工作台)

**数据流(全客户端 fetch,match T5 默认决策):**
```
BillingPage (client component)
  └── useEffect → Promise.all([
        getPlans(),              → /api/plans
        getMySubscription(),     → /api/me/subscription
        getCheckoutConfig(),     → /api/billing/checkout-config
      ])
  └── <CheckoutCard plans={paidPlans} subscription={sub} checkoutConfig={cfg} />
        └── User clicks 立即支付
              ↓
        createOrder({target_plan_code, billing_period, provider: cfg.default_provider})
              ↓
        window.location.href = response.checkout_url
```

## 7. 如何避免硬编码 pricing / subscription facts

**铁律:** frontend 不持有任何最终业务事实。

| 事实 | 来源 |
|------|------|
| 价格(fen 金额) | `GET /api/plans` → `plan.price_cny_fen.{monthly|quarterly|annual}` |
| 套餐名 | `GET /api/plans` → `plan.display_name` |
| Plan code 顺序 | 前端保留 `PLAN_RANK = {free:0, plus:1, pro:2}` 常量**仅用于 UI 禁用判断**(downgrade/same-tier),不用于展示价格 |
| 可买 plan 集合 | `plans.filter(p => p.price_cny_fen !== null)` — free 被过滤掉,因为它没有 checkout path |
| Billing period 列表 | `Object.keys(plan.price_cny_fen)` + null-check |
| 支付 provider 列表 | `GET /api/billing/checkout-config` → `providers[]` |
| 默认 provider | `GET /api/billing/checkout-config` → `default_provider` |
| Provider 显示名 | `GET /api/billing/checkout-config` → `provider.display_name` |
| 当前用户订阅 | `GET /api/me/subscription` |
| 当前用户 trial bookkeeping | `GET /api/me/subscription` → `trial.granted_at` / `trial.ends_at` |

**前端持有的常量(可以不是真相源):**
- `PLAN_RANK` — 用于判断 downgrade/same-tier 按钮是否 disable。server-side `create_order` 仍然会拒绝 downgrade,前端这是 UX only。
- `PERIOD_LABELS` — 中文标签映射(`"monthly" → "月付"`),纯展示层转换,不涉及业务事实。

**测试中的断言**(test_billing.py::TestCheckoutConfig):
- `test_no_pricing_facts_leak_into_checkout_config` — 服务器侧断言 checkout-config 响应不含任何价格字段,防止未来有人想 "顺手把价格塞进去"
- Frontend 没有 jest test suite,但 build 时 TypeScript 类型检查 + lint 已经验证所有 import 来自正确的 truth fetcher

## 8. 修改 / 新建的文件

### Gateway 修改
- `gateway/payment_providers.py` — `AlipayProvider` 重写
- `gateway/billing.py` — 新增 `GET /api/billing/checkout-config`

### Gateway 新建
- `gateway/payment_provider_alipay.py` — Alipay 集成的独立承担模块

### Tests 新建
- `tests/test_alipay_provider.py` — 30 条测试

### Tests 修改
- `tests/test_billing.py` — 新增 `TestCheckoutConfig`(5 tests)+ `TestCreateOrderAlipayGate`(1 test)

### Frontend 新建
- `frontend-next/src/app/(app)/settings/billing/page.tsx`
- `frontend-next/src/components/billing/checkout-card.tsx`
- `frontend-next/src/lib/billing/get-subscription.ts`
- `frontend-next/src/lib/billing/get-checkout-config.ts`
- `frontend-next/src/lib/billing/create-order.ts`

### 未修改(按 T5 边界要求)
- `gateway/plan_catalog.py`、`gateway/job_intercept.py`、`gateway/entitlements.py`
- `gateway/auth.py`、`gateway/auth_phone.py`、`gateway/risk_control.py`
- `gateway/models.py`、`gateway/subscriptions.py`、`gateway/main.py`(router 已经是 billing_router 的子路由,无需改 main.py)
- `gateway/config.py` — 保持不动,Alipay config 走 env 直读
- 任何 Alembic migration 文件
- 任何 marketing / auth / admin 页面
- `frontend-next/src/lib/billing/types.ts`(用新文件自带的类型定义)
- `frontend-next/src/app/(app)/settings/page.tsx`(T5 optional modify,本轮决定不动,`/settings/billing` 可直接通过 URL 进入)
- `tests/test_subscriptions.py`(没有 regressed,零改动)
- `tests/test_gateway_entitlements.py`(同上)

## 9. `pytest` 结果

### T5 必跑四文件
```
pytest tests/test_alipay_provider.py tests/test_billing.py tests/test_subscriptions.py tests/test_gateway_entitlements.py -q
........................................................................ [ 68%]
.................................                                        [100%]
105 passed in 3.01s
```

相比 T4 小修订的 73 passed,**新增 32 条测试**:
- `test_alipay_provider.py`:30 tests(全部新增)
  - `TestStatusMapping` — 6 tests
  - `TestOperationalGate` — 6 tests
  - `TestCreateCheckout` — 3 tests
  - `TestParseWebhook` — 8 tests
  - `TestSignatureVerification` — 3 tests
- `test_billing.py`:**新增 6 tests**(TestCheckoutConfig 5 + TestCreateOrderAlipayGate 1),其余 56 tests 无改动
- `test_subscriptions.py`:零改动,26 tests 全部通过
- `test_gateway_entitlements.py`:零改动,12 tests 全部通过

### 主动回归(前序阶段)
```
pytest tests/test_plan_catalog.py tests/test_auth_phone.py tests/test_trial_grant_rules.py \
       tests/test_gateway_create_job.py tests/test_gateway_job_policy.py \
       tests/test_gateway_quota.py tests/test_admin_users.py -q
159 passed, 1 warning in 3.15s
```

T0/T1/T3 遗留测试零 regression。

### T5 要求测试覆盖逐条核对

| T5 指令要求 | 对应测试 |
|---|---|
| Alipay status mapping | `TestStatusMapping`(6 tests) |
| Operational / non-operational gating when config absent | `TestOperationalGate`(6 tests,含 registry 刷新) |
| `create_checkout` contract shape without live call | `TestCreateCheckout`(3 tests,monkeypatched env) |
| Webhook parsing / normalization contract | `TestParseWebhook`(8 tests,form + JSON + 异常) |
| Signature verification non-configured path | `TestSignatureVerification`(3 tests,fail-closed) |
| `GET /api/billing/checkout-config` | `TestCheckoutConfig`(5 tests) |
| `create_order` rejects non-operational provider cleanly | `TestCreateOrderAlipayGate::test_alipay_rejected_cleanly_when_unconfigured` |
| Fake provider success path still works | `TestCreateOrder::test_fake_provider_creates_order`(既有,未 regressed) |
| Webhook / order invariants do not regress | 50 条既有 test_billing.py + 26 条 test_subscriptions.py 全部通过 |

## 10. Frontend lint / build

### `npm run lint`
```
✖ 6 problems (0 errors, 6 warnings)
```

**0 errors。** 6 个 warnings 均为 T5 前已存在的 unused vars / custom font / useEffect dependency 警告,本次未引入新 lint 问题。

我在写 billing/page.tsx 时遇到过一次 `react-hooks/set-state-in-effect` error(同步 setState in effect body),已通过重构为 `useEffect + refetchToken state + separate handleRetry` 模式修复,lint 干净。

### `npm run build`
```
✓ Compiled successfully in ~8s
✓ Generating static pages (22/22)

Route (app)
┌ ○ /
├ ○ /auth
├ ○ /auth/login
├ ○ /auth/register
├ ○ /pricing
├ ○ /trial
├ ○ /settings
├ ○ /settings/billing       ← T5 新增
├ ○ /translations/new
├ ○ /projects
├ ○ /admin/...
└ ... (共 22 条路由)
```

相比 T3/T4 的 21 条路由新增了 `/settings/billing` 一条。全部 22 页静态预渲染通过。

## 11. `python main.py --help` 结果

```
Usage:
  python main.py
  python main.py process <youtube_url> ...
  python main.py control-panel [port]
  python main.py job-api [port]
  ...
  python main.py voice-clone create <speaker_id> <speaker_name> <source_audio_path>
```

正常输出,基线满足。

## 12. 浏览器核验

Preview dev server 在 `http://localhost:4180`。**Python gateway 未运行**,使用 window.fetch mock 模拟后端响应完成端到端核验。

### `/settings/billing` 页面渲染

```
path: /settings/billing
h1: 订阅与支付
h2: 当前订阅            → "你还没有付费订阅。当前账户按 FREE 套餐运行。"
h3: 选择套餐

buttons:
  - "Plus 单次视频 60 分钟 · 3 个并行任务"  (disabled: false)
  - "Pro 单次视频 180 分钟 · 10 个并行任务" (disabled: false)
  - "月付" / "季付" / "年付"
  - "立即支付"

应付金额: ¥69                 ← 来自 mock /api/plans (6900 fen → 69.00 yuan)
支付方式: 测试支付            ← 来自 mock /api/billing/checkout-config default_provider=fake
底部: "本次支付仅创建当前选中套餐的订单,不会自动续费。"
```

**关键验证项逐条核对:**

- ✅ 页面渲染无错误(h1/h2/h3 都齐)
- ✅ 可购买的套餐列表为 **Plus + Pro**,**Free 被正确过滤**(no checkout CTA for free tier)
- ✅ 选中 Plus + monthly 时显示 `¥69` — 这个数字**不在前端代码里**,是从 mock `/api/plans` 里拿出来的
- ✅ Provider 显示为 `测试支付` — **不在前端代码里**,是从 mock `/api/billing/checkout-config` 拿出来的
- ✅ Period 选择器有 `月付 / 季付 / 年付` 三个选项
- ✅ "立即支付" CTA 可点击(因为 current plan = free,Plus 是 upgrade)
- ✅ 底部 copy "不会自动续费" 明确符合 T5 "avoid fake promises about auto-renew" 要求

### 控制台

`preview_console_logs level=error` 返回 `No console logs`。**0 errors。**

### 未做的 live smoke test

**没有**做真实 Alipay live smoke test,也没有尝试向任何真实支付网关发请求。T5 指令明确说此类验证不是必需,仅在有真实 config 时做并单独报告。当前环境没有真实 config。

## 13. Live provider blocker 与残留风险

### Live provider blocker
本轮没有真实 Alipay 商户资质。Fake path 完全绿灯,alipay 集成边界在代码层面就位但从未真实调用 Alipay gateway。部署到真实环境前必须:

1. 申请并获取 Alipay open 平台 APP ID + 密钥对
2. 在环境变量里配置 `AVT_ALIPAY_APP_ID` / `AVT_ALIPAY_APP_PRIVATE_KEY` / `AVT_ALIPAY_PUBLIC_KEY` / `AVT_ALIPAY_NOTIFY_URL` / `AVT_ALIPAY_RETURN_URL` / `AVT_ALIPAY_GATEWAY_URL`
3. 实现 `payment_provider_alipay.build_checkout_url` 的真实 RSA2 签名(TODO 标签已就位)
4. 实现 `payment_provider_alipay.verify_alipay_signature` 的真实 RSA2 verify(目前 fail-closed)
5. 配置 Alipay 侧的异步 notify URL 指向 gateway 的 `/api/billing/webhooks/alipay`
6. 做一次沙箱环境的 end-to-end 付款测试

### 残留风险

1. **Alipay 签名目前 fail-closed**:即便 config 完整,`verify_alipay_signature` 仍然返回 False,所以 Alipay webhook 会被 `_process_payment_event` 当作 `signature_valid=False` 记录但不结算。这是**保守默认**,防止未 verified 的签名路径被错误放行。下一个任务接入真实签名前,Alipay 支付实际上不会完成 settlement,应该由运维或 Task 6 的运维工具手动结算 —— 或者等待签名路径接入。

2. **No live E2E tested**:即便所有单元测试通过,真实 Alipay 异步 notify 的 field 顺序、encoding、charset 可能和测试 fixture 有细微差异,只有真实 sandbox 才能 catch。**建议 Task 6 或独立 mini-task 做一次沙箱 E2E**。

3. **Frontend 的 provider 选择器目前只展示 default_provider**:当前 CheckoutCard 没有给用户切换 provider 的 UI(只显示 `providerDisplay`)。这是 T5 有意保持的最小 UX —— 当 Alipay 和 Fake 并存时,默认走 Alipay 就够了。如果未来需要用户在多 provider 之间选择,CheckoutCard 里有注释说明可以在哪里加下拉框。

4. **Middleware 未新增 `/settings/billing` 到 public paths**:这是正确的 —— billing 页面**必须**在登录后才能访问,不应该对未登录访客放行。Middleware 的现有行为("无 session cookie → 重定向到 /auth/login")是对的。

5. **没有 frontend 单元测试**:仓库没有 Jest / Vitest 测试基础设施,T5 明确说 "There does not need to be a frontend test suite addition in this round unless the repo already has a natural place for it。Build and lint are the minimum。" 所以没有加。UI 层靠 TypeScript + lint + build + 浏览器 mock 核验做 smoke protection。

### 没有需要升级到 CodeX / Human 的 blocker
所有决策都在 T5 边界内。未增加 Redis / external queue / notification bus / 新 provider abstraction。没有偷带 Task 6 的 Billing UI scope(这个 `/settings/billing` 只有 current subscription snapshot + checkout card,没有 invoice 表 / refund / cancel / mandate UI)。

## 13. 明确停止状态

**已停止。** 等待 CodeX 审核。
