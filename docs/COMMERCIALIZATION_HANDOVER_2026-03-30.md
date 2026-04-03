# 商业化开发交接文档

> **阅读边界说明（2026-04-03 追加）：**
> 本文档冻结于 2026-03-30，是 Phase 0-5 的阶段性交接记录。
> 以下内容**仍然有效**：核心架构边界（§5）、锁定术语（§4）、各 Phase 进展（§6）、业务依据指向（§3）。
> 以下内容**仅作为历史背景**：§8 中的测试数字（当时 15 个失败，当前为 10 个）、§11 中"下一步是 Phase 5"（Phase 5 已放行）。
> 当前入口请阅读 [`docs/QUICKSTART.md`](QUICKSTART.md)。

原始冻结时间：2026-03-30

## 1. 文档用途

这份文档用于把当前“商业化前置能力”这条开发线交接给后续的 Claude 或其他协作者。

它重点回答 4 个问题：

- 现在到底做到哪一步了
- 哪些方案已经定稿并放行
- 哪些文件和模块是当前主战场
- 后续应该如何指导 Claude 继续开发，才能快而不乱

建议和以下文档一起阅读：

- `D:\Claude\AIVideoTrans_Codex_web_mvp\AGENTS.md`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\docs\specs\2026-03-29-commercialization-foundation-design.md`

## 2. 当前结论

截至目前，商业化链路已经放行到 Phase 5。

已放行：

- Phase 0+1：基线收口、权益只读、前端接线
- Phase 2：任务创建闭环
- Phase 3：Free 配额轻量闭环
- Phase 4：Admin 用户管理与审计
- Phase 5：支付订单与 Webhook 幂等基础设施

未放行：

- Phase 6：真实支付渠道接入

Phase 5 最后一轮收口补丁：将 `_process_payment_event` 中硬编码的 `signature_valid=True` 改为显式必传参数，由入口层（`receive_webhook` / `fake_pay`）负责传入。当前进入"可冻结、待 Phase 6 决策"状态。

验收说明：`docs/acceptance/PHASE_5_BILLING_FOUNDATION_ACCEPTANCE_NOTE.md`

当前还需要特别注意的事实：

- 仓库工作区很脏，存在大量无关修改和未跟踪文件
- 不要随意清理或回滚这些无关改动
- 商业化这条线是“按 Phase 验收”，不是“全仓库完全绿色”才继续
- `tests/test_process_pipeline.py` 仍有 15 个既有失败，这些目前不作为商业化 Phase 放行阻塞项

## 3. 唯一业务依据

后续涉及会员、配额、任务快照、Admin、支付时，唯一业务依据是：

- `D:\Claude\AIVideoTrans_Codex_web_mvp\docs\specs\2026-03-29-commercialization-foundation-design.md`

工程约束仍然必须遵守：

- `D:\Claude\AIVideoTrans_Codex_web_mvp\AGENTS.md`

如果旧文档和当前商业化方案冲突：

- 会员、配额、Admin、支付、任务快照，以商业化 spec 为准
- 像 `CURRENT_PROJECT_STATUS.md` 这类文档，更多看作历史背景，不再代表当前商业化主线

## 4. 锁定术语

下面 4 个术语已经锁定，后续不要再改名：

- `plan_code`
- `role`
- `service_mode`
- `quota_state`

含义如下：

- `plan_code`：用户当前套餐，`free | plus | pro`
- `role`：系统权限，`user | admin`
- `service_mode`：单个任务的运行方案，`express | studio`
- `quota_state`：轻量配额状态机，`none | reserved | committed | released`

## 5. 已定稿的核心架构边界

这些边界已经反复审查过，后续开发默认不能漂移：

- Gateway 是唯一商业规则入口
- Pipeline 只消费任务快照，不再动态推断当前套餐/支付状态
- 支付只修改用户当前权益，不修改已创建任务快照
- Admin 可以修改用户角色、套餐、额度，但不能反向重写历史任务快照

一句话概括就是：

用户买的是权益，任务跑的是快照，支付改的是当前权益，运行中的任务按创建时快照继续跑。

## 6. 各 Phase 进展

### Phase 0+1

状态：已放行

主要交付：

- Alembic 商业化基础迁移
- `role` 化管理员鉴权
- `/auth/me`
- `/api/me/entitlements`
- 前端 `service_mode` 真正接线
- 清理早期 `service_mode="pro"` 这类语义漂移

关键文件：

- `D:\Claude\AIVideoTrans_Codex_web_mvp\gateway\auth.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\gateway\entitlements.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\gateway\models.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\gateway\alembic\versions\002_add_commercialization_fields.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\frontend-next\src\app\translations\new\page.tsx`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\frontend-next\src\lib\api\entitlements.ts`

### Phase 2

状态：已放行

主要交付：

- Gateway 创建任务校验
- 结构化错误码
- 时长预估/轻量探测
- 任务快照全链打通
- source metadata 内部回写
- 本地上传 `local_file -> local_video` 规范化

关键文件：

- `D:\Claude\AIVideoTrans_Codex_web_mvp\gateway\job_intercept.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\src\services\jobs\api.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\src\services\jobs\service.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\src\services\jobs\models.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\src\pipeline\process.py`

### Phase 3

状态：已放行

主要交付：

- Free 用户轻量配额状态机
- 创建时 quota check + reserve
- 取消/删除/失败后的 release
- 成功后的 commit
- 预扣失败时的上游补偿链路
- 真实运行中任务删除时，阻止 monitor/finalize 再把 job 写回

关键文件：

- `D:\Claude\AIVideoTrans_Codex_web_mvp\gateway\quota.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\gateway\job_intercept.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\gateway\admin_settings.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\src\services\jobs\api.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\src\services\jobs\service.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\src\services\jobs\store.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\src\services\jobs\process_runner.py`

验收说明：

- Phase 3 的运行时补偿链路以 Linux 生产环境验收为准
- Windows 本地如果在这条路径上有运行时差异，可视为开发环境差异，不直接阻塞生产结论

### Phase 4

状态：已放行

主要交付：

- Admin 用户列表
- Admin 修改用户权益
- 审计日志表与审计查询
- Admin 用户管理前端页面
- 最后一个 admin 不可降级保护
- quota 边界校验

关键文件：

- `D:\Claude\AIVideoTrans_Codex_web_mvp\gateway\admin_settings.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\gateway\models.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\gateway\alembic\versions\003_add_admin_audit_log.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\frontend-next\src\app\admin\users\page.tsx`

### Phase 5

状态：已放行

主要交付：

- `payment_orders` 和 `payment_webhook_events` 表落地
- 订单创建 / 查询 / fake pay / webhook 接收
- Webhook 幂等处理（`provider_event_id` 唯一键去重）
- 支付成功 → `users.plan_code` 升级 → 审计日志
- `signature_valid` 由入口层显式传入（不再内部硬编码）

关键文件：

- `D:\Claude\AIVideoTrans_Codex_web_mvp\gateway\billing.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\gateway\models.py`（PaymentOrder, PaymentWebhookEvent）
- `D:\Claude\AIVideoTrans_Codex_web_mvp\gateway\alembic\versions\004_add_payment_tables.py`
- `D:\Claude\AIVideoTrans_Codex_web_mvp\tests\test_billing.py`

当前 `signature_valid` 状态：

- `fake_pay()` 传 `True`（fake provider 无需验签）
- `receive_webhook()` 传 `False`（Phase 5 无真实验签）
- Phase 6 必须在 webhook 入口先验签，再传入真实结果

验收说明：`docs/acceptance/PHASE_5_BILLING_FOUNDATION_ACCEPTANCE_NOTE.md`

## 7. 当前技术方案落点

### Gateway 层

商业化相关主模块：

- `auth.py`：身份信息接口
- `entitlements.py`：权益只读接口
- `job_intercept.py`：任务创建/查询/删除拦截、快照注入、结构化错误、source metadata 回写
- `quota.py`：Free 配额状态机
- `admin_settings.py`：Admin 设置、Admin 任务操作、Admin 用户管理、审计日志
- `billing.py`：支付订单、Webhook 幂等、fake pay（Phase 5 已放行）

### Job Service 层

- `src/services/jobs/api.py`
- `src/services/jobs/service.py`
- `src/services/jobs/store.py`
- `src/services/jobs/process_runner.py`

这里是 Phase 2 和 Phase 3 的关键落点，尤其是：

- 快照是否真正落盘
- 补偿删除是否真的停进程
- monitor/finalize 是否会把删除后的 job 写回

### 前端层

当前商业化相关页面和接口：

- `frontend-next/src/app/translations/new/page.tsx`
- `frontend-next/src/app/admin/users/page.tsx`
- `frontend-next/src/lib/api/jobs.ts`
- `frontend-next/src/lib/api/entitlements.ts`
- `frontend-next/src/types/jobs.ts`

## 8. 当前验证状态

商业化这条线的放行逻辑一直是：

- 先看 phase 目标是否达成
- 再看直接相关测试是否覆盖真实行为
- 不拿全仓库完全绿色作为唯一标准

当前已知状态：

- 已放行阶段的定向测试是通过的
- `tests/test_process_pipeline.py` 仍有 15 个既有失败
- 这些失败目前没有作为 Phase 0-4 的放行阻塞

Phase 3 的运行时补偿链路，最终采用了 Linux-only 验收。

这意味着：

- 如果后续是生产运行问题，看 Linux
- 如果只是 Windows 本地和 Linux 行为不一致，要先判断它是否真影响生产

## 9. 当前非阻塞遗留项

这些问题值得后续继续做，但不阻塞已经放行的 Phase：

- Admin 审计日志还没有分页
- Admin 前端保存时没有二次确认
- 一些旧文档与当前商业化主线不一致
- 某些运行时测试如果只服务 Linux 生产验收，后续应该显式标记 Linux-only，避免 Windows 本地误导

## 10. 对 Claude 的指导原则

后续指导 Claude，建议继续采用“高自主、低约束”的方式，但保留阶段性审查。

推荐模式：

1. 一次只给 Claude 一个 Phase
2. 让 Claude 以 spec 为唯一业务依据
3. 不要求 Claude 先写很长计划，默认直接实现
4. 每个 Phase 做完必须停下来汇报
5. 由审查方决定是否放行，再进入下一阶段

### 给 Claude 的固定前提

每次都应该明确告诉 Claude：

- 商业化 spec 是唯一业务依据
- 不要更改锁定术语
- Gateway 仍然是唯一商业规则入口
- 不要回滚无关已有改动
- 如果某条链路只在 Linux 生产环境有意义，可以按 Linux 验收

### Claude 什么时候应该停下来问

只有以下情况才值得打断：

- 需要修改 spec 的核心边界
- 需要高风险、不可逆的数据迁移
- 需要引入新的基础设施或外部依赖
- 当前代码状态下无法安全推进

其他普通工程决策，默认都让 Claude 自主完成。

## 11. 后续开发建议

下一步的正式目标是 Phase 5。

建议推进方式：

- 从现有 Phase 5 草稿文件继续
- 但不要假设 `gateway/billing.py` 现在就是最终方案
- 先审查数据模型、状态流、Webhook 幂等，再决定是否沿用当前 draft
- 继续优先使用 fake provider / simulate pay flow
- 在真实支付接入前，把“订单创建 -> 回调处理 -> 用户权益生效”这条闭环打稳

## 12. Phase 5 的最低通过线

后续审查 Phase 5 时，至少要满足：

- `payment_orders` 和 `payment_webhook_events` 落库
- Webhook 重复到达不会重复升级套餐
- 支付成功后只修改 `users.plan_code`
- 历史 job snapshot 不受影响
- fake payment 路径可重复、可测试、可审计

## 13. 推荐给 Claude 的提示词结构

建议未来继续用这种结构给 Claude 发任务：

“当前目标是 Phase X。以商业化 spec 为唯一业务依据。高自主推进，不改锁定术语，不回滚无关改动。完成后停下来汇报：做了什么、改了哪些文件、跑了哪些测试、还有哪些风险、是否满足放行条件。” 

## 14. Reviewer 检查清单

每次审查 Claude 的输出，建议按这个顺序看：

1. 有没有超出当前 Phase 边界
2. 锁定术语有没有漂移
3. Gateway 是否仍是商业规则入口
4. 测试是不是在测真实行为，而不是测试里复制逻辑
5. 如果涉及运行时链路，验收环境是不是对的
6. 这次到底是“已放行”，还是只是“工作区有草稿”

## 15. 当前底线结论

当前商业化基线可以认为是：

- Phase 0+1 已放行
- Phase 2 已放行
- Phase 3 已放行
- Phase 4 已放行
- Phase 5 已放行

当前仓库处于可冻结状态，等待 Phase 6 决策。

## 16. Next suggested phase

下一阶段应为 Phase 6：真实支付渠道接入。

前置条件：保留当前 Phase 5 边界不变。Phase 6 的核心增量是在 `receive_webhook` 入口实现真实签名验证，以及前端升级页面。
