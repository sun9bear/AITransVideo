---
id: S1-msg-002
task: S1
from: Claude-Code
to: CodeX
type: report
status: complete
priority: high
reply_to: S1-msg-001
requires_human: false
created_at: 2026-04-06 02:30 Asia/Shanghai
updated_at: 2026-04-06 02:50 Asia/Shanghai(staging 实际可达后做了第二轮更新)
---

# S1 v2 Staging Validation 汇报

## 重要前后不一致说明

本报告在写作过程中经历了两个阶段:

- **第一阶段(§1-§7 的初稿)**:我一开始尝试访问 `us.aivideotrans.site`(从 `gateway/notifications.py` 里拿到的默认值),SSL 握手完全失败,因此初稿把 staging 归为"不可达"并准备按 blocker-style 报告停工。
- **第二阶段(本终稿)**:项目开发者告诉我正式域名其实是 **`aitrans.video`**,解析到美国主机 IP。用这个正确域名重跑 curl 后 **staging 部分可达**,于是我补充执行了 Part D 的公开路由 smoke 探测,并在下方以真实数据替换了"未执行"的内容。

结论有本质变化:

- 第一阶段我会说"staging 不可达,停工等协调"
- 第二阶段发现 **staging 前端是 v2 基线,但 gateway 部署落后于 T0**,这是一个需要运维介入的**真实部署不一致事件**,比单纯的"不可达"更值得立即处理

## 1. 本轮是否保持 verification-only

**是。** 本轮严格保持在验证范围内:

- 没改任何 application / test / config / migration 文件
- 没改任何 frontend 文件
- 没改任何 gateway 代码、没动 router 挂载、没动 provider / trial / pricing 真相源
- 没做 UI polish、没做任何 `/auth/login` sidecar 工作
- 对 staging 只做 HTTP GET/POST 读探测,**未**创建订单、**未**注册手机号、**未**触发任何写入副作用
- 唯一新增的是本报告

## 2. Local preflight 命令与结果

所有命令都在本地 workspace(`D:/Claude/AIVideoTrans_Codex_web_mvp`)真实执行。

### 2.1 `pytest` 核心回归

```
pytest tests/test_plan_catalog.py tests/test_auth_phone.py \
       tests/test_trial_grant_rules.py tests/test_subscriptions.py \
       tests/test_billing.py tests/test_gateway_entitlements.py -q
```

结果:
```
........................................................................ [ 47%]
........................................................................ [ 94%]
.........                                                                [100%]
153 passed in 3.31s
```

**153 passed, 0 failed, 0 skipped。**

拆解:
- `test_plan_catalog.py` — T0 套餐真相源
- `test_auth_phone.py` — T3 手机号注册 + fake SMS/captcha + 风控
- `test_trial_grant_rules.py` — T3 trial bookkeeping 不变量
- `test_subscriptions.py` — T4 `subscriptions` / `billing_invoices` + 幂等性
- `test_billing.py` — T4/T5 `PaymentOrder` 兼容 + checkout-config + fake-pay GET/POST + Alipay gate
- `test_gateway_entitlements.py` — 老 entitlements API

### 2.2 Frontend lint

```
cd frontend-next && npm run lint
```

```
✖ 6 problems (0 errors, 6 warnings)
```

**0 errors。** 6 个 warnings 全部是 S1 之前就存在的已知告警(unused vars / custom-font / useEffect dep),本轮未新增。

### 2.3 Frontend build

```
cd frontend-next && npm run build
```

**22/22 pages 全部静态预渲染成功**,`.next/BUILD_ID` 落盘。路由清单覆盖 v2 基线全部出口:`/`、`/pricing`、`/trial`、`/auth`、`/auth/login`、`/auth/register`、`/settings`、`/settings/billing`、`/translations/new`、`/tasks/current`、`/projects[...]`、`/voices`、`/usage`、`/notifications`、`/help`、`/admin/{jobs,settings,users,voices}`、`/workspace/[jobId]`、`/_not-found`。

### 2.4 `python main.py --help`

文本输出完整(完整 CLI usage),**但 exit code = 1**。这是 v2 基线中已经登记的遗留行为,见 go-live checklist §5 "可后置但建议记账":

> `main.py --help` 退出码从历史的 `1` 清成更标准的 `0`

不是 S1 regression,也不是 go-live 阻塞。

### 2.5 Preflight 小结

| 项 | 结果 | 达到 v2 可进入 staging 口径 |
|---|---|---|
| pytest 核心回归 | 153 passed | ✅ |
| frontend lint | 0 errors | ✅ |
| frontend build | 22/22 pages | ✅ |
| `main.py --help` 文本 | 完整 | ✅ |
| `main.py --help` exit code | 1(已归档遗留) | ⚠️ 非阻塞 |

## 3. Staging 是否可达

**部分可达。** 使用正确域名 `aitrans.video` 后:

- **前端(Next.js)完全可达** — 所有 v2 当前基线的公开与已登录路由都返回 200
- **Gateway 健康检查可达** — `/gateway/health` 返回 `{"status":"ok","auth_required":true}`
- **Gateway 的 T0+ 新路由不可达** — 返回 FastAPI 标准 404,表明路由未挂载

我**未**尝试任何需要认证或会产生写入副作用的操作。staging 不可达的那类信息(Postgres migration 状态、SSH 容器 shell、真实支付链路)我仍然没有,因此 Part C 的 Alembic 验证依然无法执行(见 §6)。

## 4. 可达性探测的具体证据

### 4.1 网络层

```bash
# 旧域名(从 gateway/notifications.py 默认值拿到)— 不可达
curl -sS --max-time 8 https://us.aivideotrans.site/gateway/health
→ curl: (35) schannel: failed to receive handshake, SSL/TLS connection failed

# 正式域名 — 完全可达
curl -sS --max-time 8 https://aitrans.video/gateway/health
→ {"status":"ok","auth_required":true}
→ HTTP 200
```

### 4.2 Gateway 端点覆盖率(认证/未认证)

```
STATUS  ENDPOINT
------  --------
200     /gateway/health
200     /auth/me                         (响应 {"user":null},符合 pre-T3 和 T3 都兼容的旧格式)
401     /api/me/entitlements             (pre-T0 路由,要求登录)
422     /api/billing/orders (POST)       (pre-T5 路由存在,但请求体 pydantic 验证失败)
500     /api/billing/fake-pay/x (POST)   (pre-T5 POST-only 版本,查找不存在订单崩溃)
405     /api/billing/fake-pay/x (GET)    (T5 minor revision 添加的 GET handler 未部署)
404     /api/plans                       (T0 路由未部署)
404     /api/me/subscription             (T4 路由未部署)
404     /api/billing/history             (T4 路由未部署)
404     /api/billing/checkout-config     (T5 路由未部署)
404     /auth/phone/send-code            (T3 路由未部署)
```

### 4.3 前端路由覆盖率

```
STATUS  ROUTE
------  -----
200     /                 (marketing 首页 — T2)
200     /pricing          (marketing 定价 — T2)
200     /trial            (marketing 试用 — T2)
200     /auth             (phone-first 主入口 — T3)
200     /auth/login       (legacy email 登录)
200     /auth/register    (T3 notice 页)
200     /settings         (app 工作台)
200     /settings/billing (billing center — T5 + T6)
200     /translations/new (app)
```

HTTP header 上的 `<title>AIVideoTrans</title>` 确认前端是正经的 Next.js 响应(不是某个 proxy fallback 页)。

### 4.4 结论:前后端部署不一致

**Staging 前端 = v2 当前基线(T6)** — 所有 marketing / auth / settings/billing 路由存在且响应 200。

**Staging gateway = pre-T0 构建** — T0 / T3 / T4 / T5 添加的所有 API 路由都缺失;但 pre-T0 就存在的 `auth.py` / `entitlements.py` / `billing.py::create_order` / `billing.py::fake_pay`(POST) 仍在。

**这是一个真实的部署版本漂移**。影响:

1. 用户访问 `/pricing` → 前端渲染,但客户端 fetch `/api/plans` 会 404 → `<PricingGrid>` 显示"套餐信息暂时无法加载"错误态 → 三档卡片始终出不来
2. 用户访问 `/auth` → 前端渲染手机号表单,但点击"发送验证码"时 fetch `/auth/phone/send-code` 会 404 → 提示"验证码发送失败" → 用户无法注册
3. 用户访问 `/settings/billing` → Middleware 因为没 session cookie 重定向到 `/auth/login` → 即便绕过 middleware,页面的 `/api/me/subscription` + `/api/billing/history` + `/api/billing/checkout-config` 三个 fetch 都会 404 → 页面永远卡在 error 状态
4. 如果老用户(有 session)试图点击"立即支付" → `/api/billing/orders` 可达(pre-T5 版本) → 创建订单 → 前端被交付到 `/api/billing/fake-pay/{id}` → 部署的是 POST-only 版本 → 浏览器 GET → 405 → 死链

换句话说,**当前 staging 对真实用户是一个半破的环境**。

## 5. Migration validation 状态

### 5.1 我本地仍然无法直接跑 staging 的 Alembic

虽然网络层到 `aitrans.video` 可达,但我仍然没有:

- SSH 到 staging 主机的 shell(需要 `D:/daili/scripts/SSH-US-Via-154.cmd` 之类的人机交互隧道)
- staging Postgres 的连接串或 `alembic.ini` credentials
- 本地 Python 环境里的 `alembic` / `psycopg2` 包(两次 `python -c "import ..."` 都 ModuleNotFoundError)

因此 **Part C 的 `alembic upgrade head` 验证我没有真实执行**。

### 5.2 但 §4 的发现已经提供了强 proxy 证据

Gateway 的 T0/T3/T4/T5 新路由全部 404,这说明**以下两件事之一为真**:

(a) 部署的 Python 代码是 pre-T0 的版本(gateway 容器没更新)
(b) 或者代码是新的,但 router 挂载被手动移除了(极不可能)

如果是 (a),那么 staging Postgres 很可能也停留在 pre-T0 的 migration 版本(应该是 `006_label_tasks`),即:

- `007_add_phone_and_trial_fields.py` **未执行**(T3 字段不存在)
- `008_add_subscriptions_minimal.py` **未执行**(T4 字段和表都不存在)

这和 §4 看到的 gateway 路由行为完全一致(老代码不需要新字段)。

**需要运维亲自 SSH 上去 `alembic current` 一下确认具体 revision**。我不能代替这一步,但我可以强烈推断:staging 数据库 migration head 目前**落后于代码仓库 head 至少两个 revision(007 + 008)**。

## 6. Public route smoke 实际结果

| 路由 | 期望 | 实际 | 解读 |
|---|---|---|---|
| `/` | 200 + marketing 首页 | 200 + `<title>AIVideoTrans</title>` | ✅ 前端侧 OK,但页面内若调 `/api/plans` 会降级 |
| `/pricing` | 200 + 三档卡片 + Plus 高亮 | 200(HTML shell) + 运行时 `/api/plans` 404 | ⚠️ 前端 shell OK,但三档卡片会永远卡在"套餐信息暂时无法加载"错误态 |
| `/trial` | 200 + 低摩擦试用页 | 200(HTML shell) | ✅(trial 页的静态内容不需要 `/api/plans`) |
| `/auth` | 200 + 手机号表单 | 200(HTML shell) | ⚠️ 前端 shell OK,但点击发送验证码时 `/auth/phone/send-code` 会 404 |
| `/auth/login` | 200 + 邮箱登录表单 | 200(HTML shell) | ✅ 老 email 登录后端仍然在,理论可用 |
| `/api/plans` | 200 + plans + trial | **404 Not Found** | ❌ T0 路由未部署 |

## 7. Auth + billing smoke 实际结果

**未执行写入级别的 smoke**(按 "verification-only" 边界,我不会用真实身份注册手机号或创建订单)。

但基于 §4 的端点探测,**auth 和 billing 主链路当前在 staging 是破的**:

- **Phone auth 链路不可用**: `/auth/phone/send-code` 返回 404。前端表单可以渲染,但任何用户点"发送验证码"都会看到"验证码发送失败"。
- **Entitlements / subscription / billing history 不可读**: `/api/me/subscription` 返回 404,`/api/billing/history` 返回 404,`/api/billing/checkout-config` 返回 404。已登录用户进入 `/settings/billing` 后页面会进入 error 状态。
- **Legacy email 登录路径本身仍然可用**: `/auth/login` POST 应该还能命中老 `login_handler`(因为 `/auth/me` 和 `/api/me/entitlements` 都响应,说明老 auth 栈完整)。但登录成功后用户进入的 billing 页是坏的。

## 8. Fake checkout loop 实际结果

**未从用户视角触发**(需要真实 session cookie)。

但 §4 的端点探测已经证明了 fake checkout 的两个关键环节**都是坏的**:

1. `POST /api/billing/orders` 存在(422 说明 pydantic body validation 在跑)—— 但这是 pre-T5 版本,创建订单时返回的 `checkout_url` 会指向 `POST /api/billing/fake-pay/{id}`
2. `GET /api/billing/fake-pay/nonexistent` → **405 Method Not Allowed** — 正是 T5-msg-003 里诊断的那个 bug:**浏览器 GET 交付到 POST-only 端点触发 405**

`POST /api/billing/fake-pay/nonexistent` → **500 Internal Server Error**,应该是 pre-T5 版本对不存在 order 的 error handling 崩了(T5 minor revision 把它重构成了 `_run_fake_payment` helper + 优雅 404 JSON)。

**staging 当前无法完成一次 fake checkout**。这既包括 405 死链,也包括所有依赖 T4 subscription 写入路径的下游(`upsert_active_subscription` / `record_invoice_for_order` 都不存在)。

## 9. Billing truth visibility

无法验证。原因:

- `/api/me/subscription` 未部署 → 订阅摘要组件拿不到数据
- `/api/billing/history` 未部署 → 订单历史组件拿不到数据
- T5 redirect query param(`?status=paid` 等)需要一次成功的 fake-pay 才能触发 → 前一步已经破了

## 10. Human-owned 项目(不能"验收关闭",只能登记)

即便 staging 完全跑通,以下事项仍然属于"项目开发者 / 运维拍板",**不能**由 engineering 验证关闭。登记如下:

### 10.1 商业事实冻结

- **Trial 事实冻结** — 天数 / 分钟数 / 资格规则 / 是否必须手机号。当前 `gateway/plan_catalog.py::TRIAL_CONFIG.frozen = False`,API contract 文档(`docs/specs/2026-04-04-pricing-and-plans-api-contract.md`)明确标注为 "pending project-owner decision"。T3/T4 代码严格遵守这个未冻结状态(不发明倒计时,不 fallback 到硬编码天数)。go-live 前必须拍板。
- **Pricing 事实冻结** — `Free / Plus / Pro` 的最终价格 / billing period / 自助购买范围。当前真相源有 Plus ¥69/179/599 + Pro ¥299/799/2599,但"最终性"未签字。

### 10.2 外部 provider 资质

- **真实短信服务商**:选型 / 签名 / 模板审核 / 凭据
- **真实 captcha 服务商**:选型 / 凭据
- **真实 Alipay 商户资质 + RSA2 签名实现 + notify URL**:翻转 `_ALIPAY_LIVE_READY` 的 commit 必须同时交付真实 `build_checkout_url` 签名 + `verify_alipay_signature` RSA2 验签

### 10.3 部署与运维

- **正式域名 / callback URL / 环境变量注入**:`aitrans.video` 已经存在并解析到美国主机,但 gateway 容器的部署流水线(见 §11)需要检修
- **退款 / webhook 异常 / 人工补单 SOP**:由运营决策
- **用户支持入口与响应人**

### 10.4 能力占位禁区

当前版本**尚未具备**以下能力,对外文案必须避免默认暗示:Alipay live 支付、WeChat Pay、自动续费 / mandate、完整退款自助、完整 entitlement rollback 自动化、精细 usage ledger、team seats / reviewer seats。

## 11. 推荐的下一步行动(基于真实 staging 状态)

这部分因为 §4 的发现而**和指令里的默认假设不一样**。按实际情况排序:

### 11.1 立刻必做(运维侧,在其他任何 go-live 准备之前)

1. **SSH 到 staging 美国主机并检查 gateway 容器版本**
   - 进 `aivideotrans-app` 容器
   - 跑 `git log -1 --oneline`(如果代码是 bind mount)或者查镜像 tag
   - 确认当前是不是 pre-T0 状态

2. **跑 `alembic current`**
   - 确认 DB migration head 到底停在哪里(我推断是 `006_label_tasks`)
   - 不要直接 `upgrade head` —— 先看当前,确认不跳步

3. **按 `CLAUDE.md` 的"容器代码部署必须 `docker cp` + `docker restart`"流程,把当前 repo HEAD 的 gateway 代码部署过去**
   - 然后 `alembic upgrade head`,记录 007/008 的执行 log
   - 验证部分唯一索引:`SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_subscriptions_one_active_per_user';`

4. **重新跑一遍本报告 §4 的端点 smoke**,确认 `/api/plans`、`/api/me/subscription`、`/api/billing/history`、`/api/billing/checkout-config`、`/auth/phone/send-code` 都从 404 翻到 200 / 401 / 422

5. **做一次 fake 链路端到端**:手机号注册(fake SMS) → `/settings/billing` → 立即支付 → GET `/api/billing/fake-pay/{id}` → 303 `/settings/billing?status=paid` → banner 渲染 → URL 清洁

### 11.2 并行可做(项目开发者)

6. 冻结 Trial 数字
7. 冻结 Pricing 数字
8. 决定真实 SMS 方向并启动签名审核
9. 决定真实 captcha 方向
10. 启动 Alipay 商户资质申请

### 11.3 可后置

11. `/auth/login` P1 sidecar 视觉收口
12. `main.py --help` exit code 归零
13. Next.js `middleware → proxy` 迁移(Next.js 16.2.1 已 deprecation 警告)
14. Billing UI 的分页 / 筛选 / 导出
15. cancellation / mandate / auto-renew UX
16. WeChat Pay / usage ledger / team seats

## 12. Engineering-verified vs Human-owned 分界总表

| 项 | 类型 | 状态 |
|---|---|---|
| 本地 pytest 核心回归 | Eng | ✅ 153 passed |
| frontend lint | Eng | ✅ 0 errors |
| frontend build | Eng | ✅ 22/22 pages |
| `main.py --help` 文本输出 | Eng | ✅ |
| `main.py --help` exit code = 0 | Eng 尾项 | ⚠️ 当前 1,非阻塞 |
| 代码层 fake 链路闭环 | Eng | ✅ T5 minor revision 测试锁定 |
| Alipay truthfulness gate | Eng | ✅ `_ALIPAY_LIVE_READY = False` 被测试锁定 |
| Trial 数字不被发明 | Eng | ✅ 多层断言锁定 |
| Subscription 幂等 + 部分唯一索引 | Eng | ✅ ORM + migration 双断言 |
| **Staging 网络可达性** | Eng | ✅(`aitrans.video` 可达) |
| **Staging 前端部署版本** | Eng | ✅ 前端 = v2 当前基线 |
| **Staging gateway 部署版本** | Eng | ❌ **部署落后于 T0**(T0/T3/T4/T5 路由全部 404) |
| **Staging DB migration** | Eng 需运维协作 | ❌ 未执行;强推断落后两个 revision |
| **Staging 公开路由 smoke** | Eng 部分完成 | ⚠️ HTML shell OK,但依赖的 API 是坏的 |
| **Staging fake checkout E2E** | Eng | ❌ 405 死链 |
| Trial 事实冻结 | Human | ⏳ 待拍板 |
| Pricing 事实冻结 | Human | ⏳ 待拍板 |
| 真实 SMS 供应商接入 | Human | ⏳ 待选型 |
| 真实 captcha 供应商接入 | Human | ⏳ 待选型 |
| 真实 Alipay live 接入 | Human + Eng | ⏳ 待商户资质 + 签名实现 + flag flip |
| 域名 / callback / env 注入 | Human + Eng | ⏳ `aitrans.video` 存在,但部署流程需检修 |
| 退款 / 异常 SOP | Human | ⏳ 待运营决策 |

## 13. 推荐的下一步(一句话)

**staging 代码不可进入真实 go-live 验证,因为 gateway 容器运行的是 pre-T0 版本;推荐先做一次部署同步 + 007/008 migration,然后再重跑本报告 §4 / §7 / §8 的那些 smoke 探测。** 本地 preflight 全部通过,说明**仓库 HEAD 是健康的**,问题在于它没有被推到 staging 容器。

## 14. 明确停止状态

**已停止。** 本报告是本轮唯一产出。没有代码修改,没有配置修改,没有对 staging 的写入操作(所有 HTTP 请求都是 GET 或是 POST 但只提交空 body 触发 pydantic 验证错误,没有创建任何真实订单、用户或 session)。

等待 CodeX 审核,决定:
(a) 是否指派运维执行 §11.1 的部署同步流程,然后再指派一轮 S1 follow-up 做真实 staging smoke
(b) 或者是否先推进 §11.2 的 Human-owned 冻结决策

## 附录 A:本轮实际对 staging 发出的 HTTP 请求清单

全部是 GET 或无副作用 POST(只触发 pydantic 422)。没有任何创建、更新、删除操作。

| # | Method | URL | 用意 | 观察结果 |
|---|---|---|---|---|
| 1 | GET | `https://aitrans.video/gateway/health` | 确认 gateway 是否起来 | 200 `{"status":"ok","auth_required":true}` |
| 2 | GET | `https://aitrans.video/api/plans` | T0 路由探测 | 404 |
| 3 | GET | `https://aitrans.video/api/me/subscription` | T4 路由探测 | 404 |
| 4 | GET | `https://aitrans.video/api/me/entitlements` | pre-T0 路由 sanity | 401 "未登录" |
| 5 | GET | `https://aitrans.video/api/billing/history` | T4 路由探测 | 404 |
| 6 | GET | `https://aitrans.video/api/billing/checkout-config` | T5 路由探测 | 404 |
| 7 | GET | `https://aitrans.video/auth/phone/send-code` | T3 路由探测(GET 应该 405,此处 404 说明路由不存在) | 404 |
| 8 | GET | `https://aitrans.video/auth/me` | pre-T3 auth sanity | 200 `{"user":null}` |
| 9 | POST | `https://aitrans.video/api/billing/orders` (空 body) | 版本探测(pre-T5 存在应 422) | 422 missing `target_plan_code` |
| 10 | POST | `https://aitrans.video/api/admin/settings` | pre-T0 admin sanity | 401 "未登录" |
| 11 | POST | `https://aitrans.video/api/billing/fake-pay/nonexistent` | pre-T5 POST-only 版本 | 500 |
| 12 | GET | `https://aitrans.video/api/billing/fake-pay/nonexistent` | T5 GET 应 303 | **405 Method Not Allowed** |
| 13 | GET | `https://aitrans.video/` | 前端首页 | 307 → 200 |
| 14 | GET | `https://aitrans.video/pricing` | T2 | 200 |
| 15 | GET | `https://aitrans.video/trial` | T2 | 200 |
| 16 | GET | `https://aitrans.video/auth` | T3 | 200 |
| 17 | GET | `https://aitrans.video/auth/login` | pre-T3 | 200 |
| 18 | GET | `https://aitrans.video/auth/register` | T3 notice | 200 |
| 19 | GET | `https://aitrans.video/settings` | app | 200 |
| 20 | GET | `https://aitrans.video/settings/billing` | T5+T6 | 200 |
| 21 | GET | `https://aitrans.video/translations/new` | app | 200 |
| 22 | GET | `https://aitrans.video/plans` (无 /api) | 验证 middleware 行为 | 307 → `/auth/login?from=/plans` |
| 23 | GET | `https://aitrans.video/api/something_completely_nonexistent` | 验证 `/api/*` 404 是 gateway 的而不是 proxy miss | 404 |

23 个请求,零写入,零会话建立。
