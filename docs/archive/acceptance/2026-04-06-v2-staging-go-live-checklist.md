# v2 Staging / Go-Live 准备清单

> 状态：草案
> 适用范围：AIVideoTrans Web MVP v2（Task 0 - Task 6 已放行后的上线准备）
> 最后更新：2026-04-06

## 1. 当前基线结论

当前 v2 主线代码闭环已经完成：

- Task 0：gateway 套餐 / pricing / trial 真相源统一
- Task 1：`marketing / auth / app` 三层前端结构完成
- Task 2：`/`、`/pricing`、`/trial` marketing 三页完成
- Task 3：手机号公开注册主路径 + fake SMS / fake captcha + 基础风控完成
- Task 4：最小订阅真相源与 billing history API 完成
- Task 5：首次支付闭环基线完成，fake provider 本地闭环可用，Alipay 已被正确收窄为未 live
- Task 6：`/settings/billing` 基础 Billing UI 完成

这意味着：

- 可以进入 staging 验证
- 但还不能默认视为“真实公开收费已可上线”

## 2. Staging 前必须完成

以下项目完成后，才能认为“当前代码适合进入 staging 环境验证”。

### 2.1 数据库与迁移

- [ ] 在 staging 的真实 PostgreSQL 上执行 `alembic upgrade head`
- [ ] 记录 `007_add_phone_and_trial_fields.py` 与 `008_add_subscriptions_minimal.py` 的执行结果
- [ ] 验证 `subscriptions` 表存在，并包含“每用户最多一条 active 订阅”的数据库级唯一约束
- [ ] 验证 `billing_invoices`、`phone_verification_challenges`、`users.phone_number`、`users.trial_granted_at` 等字段均已落地
- [ ] 如 staging 允许，额外跑一次 `alembic downgrade -1` / `upgrade head` 演练；如不允许 downgrade，至少做 schema snapshot 留档

### 2.2 服务启动与基础 smoke

- [ ] `python main.py --help` 能在 staging 镜像里正常输出 usage
- [ ] gateway、Next.js、Postgres 容器或进程都能启动并通过健康检查
- [ ] `/api/plans` 可匿名访问
- [ ] `/auth`、`/auth/login`、`/pricing`、`/trial` 可公开访问
- [ ] 已登录用户可访问 `/settings/billing`

### 2.3 主链路 smoke

- [ ] guest 从 `/` 点击 CTA 能进入 `/auth`
- [ ] 手机号登录主路径在 staging 可走通
- [ ] fake captcha / fake SMS 在 staging 默认环境仍可用
- [ ] Trial bookkeeping 正常：首个手机号可发放 trial，重复手机号不重复发放
- [ ] `/settings/billing` 能正常显示订阅摘要、checkout card、订单历史空态或历史态
- [ ] fake checkout 能从 `/settings/billing` 走到 `/api/billing/fake-pay/{order_id}` 再 303 返回 `/settings/billing?status=paid`
- [ ] 返回后的 status banner 能显示并自动清理 URL query

### 2.4 回归验证

- [ ] 运行 gateway 核心测试集
- [ ] 运行 `frontend-next` 的 `npm run lint`
- [ ] 运行 `frontend-next` 的 `npm run build`
- [ ] 保留测试结果记录，至少包含通过数与失败数

推荐命令：

```bash
pytest tests/test_plan_catalog.py tests/test_auth_phone.py tests/test_trial_grant_rules.py tests/test_subscriptions.py tests/test_billing.py tests/test_gateway_entitlements.py -q
```

```bash
cd frontend-next
npm run lint
npm run build
```

## 3. Staging 验收必须明确记录

以下内容不是“尽量记”，而是建议形成文字记录，避免 go-live 时靠记忆判断。

- [ ] staging 使用的 gateway / next / app commit sha
- [ ] staging 使用的数据库 migration revision
- [ ] `/api/plans` 实际返回内容截图或 JSON 样例
- [ ] `/settings/billing` 页面截图
- [ ] fake pay 完成后的 `/settings/billing?status=paid` 截图
- [ ] 手机号注册 / 登录路径截图
- [ ] 已知限制清单

## 4. Go-Live 前必须补齐

以下项目未完成前，不应把当前版本视为“真实公开收费上线可用”。

### 4.1 商业事实冻结

- [ ] 项目开发者拍板 Trial 事实
  - 天数
  - 分钟数
  - 资格规则
  - 是否必须手机号
- [ ] 项目开发者拍板正式 pricing 事实
  - `Free / Plus / Pro` 的价格
  - billing period
  - 是否允许自助购买
- [ ] marketing / auth / billing 页面文案与 gateway 真相源逐项对齐

### 4.2 真实短信与人机验证

当前代码仍是 fake-first。若要真实对外开放手机号注册，至少要补齐：

- [ ] 选定一个真实短信服务商
- [ ] 完成短信签名与模板审核
- [ ] 接入真实验证码发送配置
- [ ] 决定并接入真实 captcha 或等价风控前置
- [ ] 明确 send-code 与 verify-code 的线上限频阈值
- [ ] 明确虚拟号段策略是否正式启用

### 4.3 真实支付通道

当前 Alipay 仍不是 live-ready，只是被正确地标记为不可用。真实 go-live 前必须完成：

- [ ] 完成 Alipay signed checkout URL 生成
- [ ] 完成 Alipay webhook RSA2 验签
- [ ] 在同一个提交中翻转 `_ALIPAY_LIVE_READY`
- [ ] 用 staging 的真实 callback URL 跑通一次完整支付回路
- [ ] 确认 `/api/billing/checkout-config` 在真实环境只暴露真正可用的 provider

### 4.4 支付后运营兜底

当前系统已有最小 billing truth，但仍有后置能力未做完。真实收费前至少要有人工兜底方案：

- [ ] 形成退款人工处理 SOP
- [ ] 形成 webhook 异常人工补单 SOP
- [ ] 明确“退款后 entitlement 是否立即回滚”当前版本的人工处理原则
- [ ] 明确用户支持入口与响应人

备注：

- 当前版本 billing history 已支持 `refunded` 状态记录
- 但完整的 entitlement rollback UX 仍不是当前版本能力
- 因此 go-live 前必须先有运营层面的补救与说明机制

### 4.5 域名、回调、环境变量

- [ ] 确认正式站点域名
- [ ] 确认 gateway / next / app 的正式 base URL
- [ ] 确认 Alipay notify/callback URL
- [ ] 确认短信与 captcha 的正式环境变量注入方式
- [ ] 确认生产日志、告警、敏感配置管理方式

## 5. 可后置但建议记账

这些不是 go-live 阻塞项，但建议明确列为后续任务，不要混淆成“已完成”。

- [ ] `/auth/login` 视觉小收口，与新 auth 基线对齐
- [ ] `main.py --help` 退出码从历史的 `1` 清成更标准的 `0`
- [ ] Next.js `middleware -> proxy` 迁移
- [ ] Billing UI 的分页、筛选、导出
- [ ] cancellation / mandate / auto-renew UX
- [ ] WeChat Pay
- [ ] usage ledger
- [ ] team seats / reviewer seats

## 6. 当前不应误判为已具备的能力

以下能力当前**尚未具备**，不要在对外表述、页面文案或运营说明里默认暗示：

- 真正可用的 Alipay live 支付
- WeChat Pay
- 自动续费
- 完整退款自助流程
- 完整 entitlement rollback 自动化
- 精细 usage ledger 计费
- 团队协作席位与 reviewer seats

## 7. 推荐上线判定口径

### 7.1 可以进入 staging 的口径

满足以下条件即可：

- Task 0 - Task 6 代码已放行
- staging migration 已跑通
- fake auth / fake pay / billing UI 主链路已在 staging 验证
- 核心回归测试与前端构建通过

### 7.2 可以进入真实 go-live 的口径

必须同时满足：

- staging 验证完成并留档
- Trial / pricing / provider 事实已冻结
- 真实短信与 captcha 已接通
- 真实 Alipay 已 live-ready 并完成至少一次真实端到端验证
- 支付异常与退款的人工 SOP 已准备

## 8. 当前建议的下一步顺序

1. 先在 staging 跑 `alembic upgrade head`
2. 完成 fake auth + fake pay + `/settings/billing` 的 staging 验证留档
3. 由项目开发者拍板 Trial / pricing / payment channel 事实
4. 再决定是否进入真实短信与真实 Alipay 接入
5. `/auth/login` 视觉 sidecar 可并行，但不阻塞以上主路径
