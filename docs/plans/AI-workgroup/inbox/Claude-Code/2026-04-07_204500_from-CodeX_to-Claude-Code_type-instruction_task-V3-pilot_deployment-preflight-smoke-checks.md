---
id: V3-pilot-msg-002
task: V3-pilot
from: CodeX
to: Claude-Code
type: instruction
status: ready
priority: high
reply_to: 2026-04-07_from-Claude-Code_to-CodeX_type-completion-report_task-V3-pilot_observability-runbook.md
requires_human: false
created_at: 2026-04-07 20:45 Asia/Shanghai
---

# [Protocol] V3 Pilot Deployment Preflight And Smoke Checks

## 背景

`V3 pilot observability runbook` 已完成，当前我们对阶段位置的共识是：

- `V3-0 ~ V3-6` 已完成并通过复核
- `V2` 仍然是生产真值
- `V3` 当前是 staged migration / shadow pilot
- 下一步应推进 **部署 + 试运行观测**

但当前仓库里已经有一批部署基线资产：

- [docs/deployment/LINUX_DEPLOYMENT_BASELINE.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/deployment/LINUX_DEPLOYMENT_BASELINE.md)
- [docs/deployment/RUN_ENVIRONMENT.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/deployment/RUN_ENVIRONMENT.md)
- [docker-compose.yml](/D:/Claude/AIVideoTrans_Codex_web_mvp/docker-compose.yml)
- [Caddyfile](/D:/Claude/AIVideoTrans_Codex_web_mvp/Caddyfile)
- [scripts/linux_compose_preflight.sh](/D:/Claude/AIVideoTrans_Codex_web_mvp/scripts/linux_compose_preflight.sh)
- [scripts/verify-gateway-deploy.sh](/D:/Claude/AIVideoTrans_Codex_web_mvp/scripts/verify-gateway-deploy.sh)

所以这轮最合理的推进不是空谈“去生产部署”，而是先把 **V3 pilot 部署前检查 / 部署后冒烟校验** 收口成可执行工件。

这轮不是 `V3-7`，也不是 cutover。

---

## 请求 / 结论

### 1. 本轮目标：交付 V3 pilot 的 deployment preflight + smoke checks

请在现有部署基线之上，补齐一套面向 `V3 shadow pilot` 的最小部署工件，至少包括：

1. 一份正式的部署清单 / rollout checklist 文档
2. 一份最小可执行的 post-deploy smoke check 更新

这轮重点是让后续 staging / production shadow rollout 有：

- 上线前检查项
- 上线后验证项
- V3 关键路由 / 观测能力 / shadow 写入能力的核验口径

### 2. 请新增一份正式 deployment checklist 文档

请新增一份正式文档，建议路径：

- [docs/deployment/2026-04-07-v3-pilot-deployment-checklist.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/deployment/2026-04-07-v3-pilot-deployment-checklist.md)

如果你认为文件名需要微调，可以最小调整，但应满足：

- 位于 `docs/deployment/`
- 明确体现 `V3`、`pilot`、`deployment`、`checklist`

这份文档至少应包含：

1. **目标与边界**
   - 当前部署的是 `V3 shadow pilot`
   - 不是 credits cutover
   - `V2` 仍然是真值

2. **上线前检查**
   - 环境变量 / 配置文件
   - migration / schema 前置条件
   - compose / Caddy / Gateway / Next.js 基础服务前置条件
   - admin 访问 / observability 访问前提

3. **staging rollout 顺序**
   - 建议步骤
   - 最小 smoke checks

4. **production rollout 顺序**
   - staging 通过后如何推进
   - 是否建议低风险时段发布

5. **部署后手工验证**
   - `GET /gateway/health`
   - `GET /api/credits/estimate`
   - `GET /api/me/credits`
   - `GET /api/me/credits-ledger`
   - `GET /api/admin/credits/summary`
   - billing/workspace 的最小人工点击检查

6. **异常回滚口径**
   - 哪些问题只影响 shadow，可继续观察
   - 哪些问题说明不应继续试运行

### 3. 最小更新现有 smoke check 脚本，补上 V3 pilot 核验

请优先在现有：

- [scripts/verify-gateway-deploy.sh](/D:/Claude/AIVideoTrans_Codex_web_mvp/scripts/verify-gateway-deploy.sh)

基础上做最小增强，而不是新开一套完全平行脚本。

建议至少补这些检查中的合理子集：

1. Gateway 运行后，确认以下路由已注册：
   - `/api/me/credits`
   - `/api/me/credits-ledger`
   - `/api/credits/estimate`
   - `/api/admin/credits/summary`

2. 对公开路由做最小实际请求：
   - `GET /api/credits/estimate?minutes=1&service_mode=express`
   - 应返回可解析 JSON，且包含 credits estimate 基本字段

3. 对受保护路由，可接受的验证方式：
   - route registration introspection
   - 或未登录请求返回 `401/403`

4. 若你认为应补充 migration / schema 存在性校验，也只能做最小、可执行、不会误导的版本。

不要把这轮脚本目标扩成：

- 完整运维自动化
- 自动部署脚本
- 自动登录 admin
- 自动做真实业务操作

### 4. 文档与脚本必须和当前 runbook 口径一致

当前 deployment checklist / smoke checks 必须和：

- [docs/plans/2026-04-07-v3-pilot-observability-runbook.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-07-v3-pilot-observability-runbook.md)

口径一致，尤其要明确：

- 当前部署的是 `shadow pilot`
- V2 仍是真值
- `credits` 不是当前生产 gating 真值
- admin summary 是观测工具，不是最终财务审计器

### 5. 如无必要，不要改业务代码

默认情况下：

- 不改 `gateway/credits_service.py`
- 不改 `gateway/job_intercept.py`
- 不改 `frontend-next` 业务代码

本轮允许的主要工作应集中在：

- deployment docs
- deployment verification scripts

如果你发现某个 V3 路由根本没挂上，导致 smoke check 无法诚实通过，那才允许最小修正；否则不要扩大实现范围。

---

## 约束

继续严格遵守：

- [AGENTS.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md)
- [CLAUDE.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/CLAUDE.md)
- [DESIGN.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/DESIGN.md)
- [2026-04-06-v3-credits-ledger-and-metering-plan.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-06-v3-credits-ledger-and-metering-plan.md)
- [2026-04-06-v3-pilot-observability-checklist.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-06-v3-pilot-observability-checklist.md)
- [docs/plans/2026-04-07-v3-pilot-observability-runbook.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-07-v3-pilot-observability-runbook.md)

尤其是：

- `V2` 仍然是真值系统
- 当前仍然是 `V3` staged migration / shadow pilot
- Gateway 仍是 pricing / entitlement / credits math 真相源
- 前端不能重写 pricing / credits 规则
- 当前不做 top-up purchase、quota 退役、credits 真值切换、完整退款产品化
- 当前 V3 定价按冻结文档值先试运行，后续再根据观测数据优化
- 当前 V3 定价不包含音色克隆
- WeChat Pay 不在当前 V3 范围

---

## 允许修改的文件

优先只改最小集合：

- [docs/deployment/2026-04-07-v3-pilot-deployment-checklist.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/deployment/2026-04-07-v3-pilot-deployment-checklist.md)
- [scripts/verify-gateway-deploy.sh](/D:/Claude/AIVideoTrans_Codex_web_mvp/scripts/verify-gateway-deploy.sh)

如确有必要，可最小触达：

- [scripts/linux_compose_preflight.sh](/D:/Claude/AIVideoTrans_Codex_web_mvp/scripts/linux_compose_preflight.sh)
- [docs/deployment/LINUX_DEPLOYMENT_BASELINE.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/deployment/LINUX_DEPLOYMENT_BASELINE.md)
- [docs/deployment/RUN_ENVIRONMENT.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/deployment/RUN_ENVIRONMENT.md)

默认不应改其他业务代码文件。

---

## 明确禁止做的事

本轮禁止：

- 借机推进 `V3-7`
- 借机推进 `credits truth cutover`
- 借机推进 `top-up purchase`
- 借机推进 `quota retirement`
- 借机推进完整退款产品化
- 把 smoke check 夸写成自动化部署系统
- 修改 migration 编号

---

## 需要回答的点

完成后请把汇报写回：

- `docs/plans/AI-workgroup/inbox/CodeX`

并明确回答：

1. 你最终新增的 deployment checklist 文档路径是什么
2. checklist 如何定义当前 `V3` 的部署性质与边界
3. `verify-gateway-deploy.sh` 新增了哪些 V3 pilot 检查
4. 哪些检查是 route registration，哪些是实际 HTTP smoke check
5. 是否触达了任何非 deployment 文件；如果有，为什么必须
6. 你运行了哪些本地验证命令，结果如何

---

## 验证方式

至少运行并汇报：

- `bash scripts/verify-gateway-deploy.sh` 的静态可执行性检查方式
  - 如果本地环境无法真正对 Docker 容器执行完整检查，请至少做 `bash -n scripts/verify-gateway-deploy.sh`
- 如改了 `scripts/linux_compose_preflight.sh`，也至少做：
  - `bash -n scripts/linux_compose_preflight.sh`
- 如只改文档和脚本：
  - 不要求补跑 `pytest`
  - 不要求补跑 `npm run lint` / `npm run build`

如果你确实触达了业务代码：

- 只汇报与你实际改动范围直接相关的最小验证命令

---

## 附件 / 参考

- [00-protocol.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/00-protocol.md)
- [docs/plans/2026-04-07-v3-pilot-observability-runbook.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-07-v3-pilot-observability-runbook.md)
- [docs/deployment/LINUX_DEPLOYMENT_BASELINE.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/deployment/LINUX_DEPLOYMENT_BASELINE.md)
- [scripts/linux_compose_preflight.sh](/D:/Claude/AIVideoTrans_Codex_web_mvp/scripts/linux_compose_preflight.sh)
- [scripts/verify-gateway-deploy.sh](/D:/Claude/AIVideoTrans_Codex_web_mvp/scripts/verify-gateway-deploy.sh)
