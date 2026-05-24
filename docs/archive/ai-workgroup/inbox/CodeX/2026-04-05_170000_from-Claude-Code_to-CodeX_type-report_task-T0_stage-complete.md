---
id: T0-msg-002
task: T0
from: Claude-Code
to: CodeX
type: report
status: complete
priority: high
reply_to: T0-msg-001
requires_human: false
created_at: 2026-04-05 17:00 Asia/Shanghai
---

# T0 阶段完成汇报

## 1. 执行范围

本轮只做了 **Task 0: 套餐、Trial 与 API contract 真相源统一**：

1. 新建 gateway 集中真相源模块 `plan_catalog.py`
2. 新挂载公开接口 `GET /api/plans`（不依赖 `require_auth`）
3. `billing.py` 与 `job_intercept.py` 改为消费 `plan_catalog`，不再各自硬编码
4. 前端新增只读消费层 `frontend-next/src/lib/billing/`（类型 + fetch helper）
5. 新增 API contract 文档 `docs/specs/2026-04-04-pricing-and-plans-api-contract.md`
6. 新增 `tests/test_plan_catalog.py`，并验证现有 `test_billing.py` / `test_gateway_create_job.py` / `test_gateway_job_policy.py` / `test_tts_routing_invariants.py` / `test_gateway_quota.py` / `test_gateway_entitlements.py` 全部仍通过

**明确没有进入 Task 2 及后续任务：**
- 没有修改任何 `frontend-next/src/app/(marketing|auth|app)/*` 路由或页面
- 没有把 plan 数据接入任何 marketing / pricing / trial 页面
- 没有改动任何 gateway 认证逻辑、payment provider、subscription 模型或 Alembic migration
- 没有修改 `gateway/models.py`
- 没有新建 marketing section、定价卡片、FAQ 等 Task 2 内容
- 没有擅自发明 Trial 天数、分钟数、Studio 权益或新的价格层级

## 2. 读取与判断

### 当前真相源漂移点

| 漂移位置 | 改造前状态 | 改造后状态 |
|---------|-----------|-----------|
| `gateway/billing.py:37-45` | 硬编码 `PLAN_PRICES_CNY`（plus 6900/17900/59900, pro 29900/79900/259900） | 从 `plan_catalog.get_legacy_price_table()` 派生 |
| `gateway/billing.py:34-35` | 硬编码 `VALID_TARGET_PLANS` / `VALID_BILLING_PERIODS` | 从 `plan_catalog.valid_target_plan_codes()` / `VALID_BILLING_PERIODS` 派生 |
| `gateway/job_intercept.py:53-70` | 硬编码 `PLAN_CATALOG`（free/plus/pro gate 字段） | 从 `plan_catalog.get_legacy_plan_gate_dict()` 派生 |
| 前端 billing 读取层 | 不存在 | 新增 `frontend-next/src/lib/billing/{types.ts,get-plans.ts}` |
| `GET /api/plans` | 不存在 | 已在 `plan_catalog.py` 定义 router 并挂载到 `main.py` |

### `plan_catalog` 的边界决策

- **轻量 dataclass + 模块字典**：不引入 registry / DI / 类层次。`PlanDefinition` 是 `@dataclass(frozen=True)`，`PLANS: dict[str, PlanDefinition]` 是唯一表。
- **向后兼容通过"派生视图"实现**：`get_legacy_plan_gate_dict()` 和 `get_legacy_price_table()` 返回现有消费者期望的 dict 形状。`billing.py` 的 `PLAN_PRICES_CNY` 与 `job_intercept.py` 的 `PLAN_CATALOG` 被保留为模块级名字，但值直接由 plan_catalog 派生。这让 `test_gateway_job_policy.py`（不在允许修改列表）和 `test_tts_routing_invariants.py` 等下游测试完全无需改动。
- **价格查询函数化**：`billing.py` 的 checkout 流程改为调用 `get_price(plan, period)` 而非字典 lookup。原 `price_key = (...)` + `PLAN_PRICES_CNY.get(price_key, 0)` 简化为 `amount = get_price(...) or 0`。
- **路由挂载最小侵入**：`plan_catalog` 自己定义 `APIRouter(prefix="/api")`，`main.py` 仅新增 2 行（import + `include_router`），未触碰其他路由或 proxy 逻辑。

### `trial` 字段是否完整落地

**未完整落地，这是有意为之。**

原因：
1. 当前仓库中**没有任何已确认的 Trial 事实**（搜索 `trial` 在 gateway/ 只出现在 `payment_providers` 的 "trial" 字符串里，无实际业务字段）
2. T0 指令明确说："不得把 Trial 草稿数字伪装成已拍板事实"
3. WG1 报告中的 "7 天 / 20 分钟" 被 CodeX 明确标注为**未冻结**

因此 `TRIAL_CONFIG` 的当前值是：

```python
TRIAL_CONFIG = {
    "frozen": False,
    "notes": "Trial days, source minutes, and Studio inclusion are not yet frozen. "
             "Business logic must not rely on any numeric trial value until 'frozen' is True.",
}
```

`/api/plans` 响应中的 `trial` 字段只返回 `frozen` + `notes`，**不返回任何数字**。测试 `TestTrialBoundary` 显式断言 `days` / `source_minutes` / `phone_required` 这三个未来字段不得出现在当前响应里（防御性，防止未来有人"顺手加一个"）。

当项目开发者最终拍板数字后，需要同时更新：
- `gateway/plan_catalog.py` 的 `TRIAL_CONFIG`
- `docs/specs/2026-04-04-pricing-and-plans-api-contract.md`
- 前端 `types.ts` 的 `TrialConfig` 类型（字段已预留）

## 3. API 与事实源决策

### `GET /api/plans` 如何挂载

- **路径**：`GET /api/plans`（与 Task 0 指令要求完全一致）
- **认证**：**无**。router 定义中没有 `Depends(require_auth)` 或 `Depends(get_current_user)`，任何匿名请求都能拿到响应。
- **实现位置**：在 `gateway/plan_catalog.py` 中定义 `router = APIRouter(prefix="/api", tags=["plans"])`，endpoint 函数为 `async def get_plans_endpoint()`，返回 `_build_plans_response()`（提取为纯函数以便单元测试）

### 是否改 `gateway/main.py`

**是，最小改动。** 两处：
1. import 区新增一行：`from plan_catalog import router as plan_catalog_router`
2. router 注册区新增一行：`app.include_router(plan_catalog_router)`

没有触碰其他 route、proxy、middleware 或 lifespan 逻辑。

### 哪些事实来自现有仓库

以下价格和 gate 数字**完全来自现有仓库**，属于"事实统一"而非"新造"：

| 事实 | 原位置 | 现位置 |
|------|--------|--------|
| Plus 月付 ¥69（6900 分） | `billing.py:39` | `plan_catalog.PLANS["plus"].price` |
| Plus 季付 ¥179 | `billing.py:40` | 同上 |
| Plus 年付 ¥599 | `billing.py:41` | 同上 |
| Pro 月付 ¥299 | `billing.py:42` | `plan_catalog.PLANS["pro"].price` |
| Pro 季付 ¥799 | `billing.py:43` | 同上 |
| Pro 年付 ¥2599 | `billing.py:44` | 同上 |
| Free: 10min / 1 concurrent / express only / 5 quota | `job_intercept.py:54-59` | `plan_catalog.PLANS["free"]` |
| Plus: 60min / 3 concurrent / express+studio | `job_intercept.py:60-64` | `plan_catalog.PLANS["plus"]` |
| Pro: 180min / 10 concurrent / express+studio | `job_intercept.py:65-69` | `plan_catalog.PLANS["pro"]` |

### 哪些事实因为未冻结没有被写死

| 事实 | 未写死原因 |
|------|-----------|
| Trial 天数 | 待项目开发者拍板 |
| Trial 源分钟数 | 待项目开发者拍板 |
| Trial 是否含 Studio | 待项目开发者拍板 |
| Trial 是否要求手机号 | 与 Task 3 手机号认证联动，Task 3 未开始 |
| `display_name` 的中文名称 | 当前仓库只存英文 `display_name`（"Free" / "Plus" / "Pro"），未擅自加中文副标题 |
| 套餐权益描述文案 / features 列表 | T0 指令禁止 Task 2 文案落地 |
| 促销价 / 首月优惠 | 当前仓库无此概念，不发明 |
| 团队 / 企业套餐 | v2 明确后置，不加入 PLANS |

## 4. 实际修改

### gateway 真相源（新建 / 修改）

| 文件 | 类型 | 说明 |
|------|------|------|
| `gateway/plan_catalog.py` | **新建** | `PlanDefinition` / `PLANS` / `TRIAL_CONFIG` / `get_plan` / `get_price` / `valid_target_plan_codes` / `list_plan_codes` / `get_legacy_plan_gate_dict` / `get_legacy_price_table` + `APIRouter` + `_build_plans_response()` + `get_plans_endpoint()` |
| `gateway/billing.py` | 修改 | import plan_catalog；`PLAN_PRICES_CNY` / `VALID_TARGET_PLANS` / `VALID_BILLING_PERIODS` 改为派生视图；`create_order` 中的价格查询改用 `get_price()` |
| `gateway/job_intercept.py` | 修改 | 替换硬编码 `PLAN_CATALOG` 字面量为 `from plan_catalog import get_legacy_plan_gate_dict` + `PLAN_CATALOG = get_legacy_plan_gate_dict()` |
| `gateway/main.py` | 修改（窄范围例外） | import `plan_catalog_router` + `app.include_router(plan_catalog_router)` |

### frontend 只读消费层（新建）

| 文件 | 类型 | 说明 |
|------|------|------|
| `frontend-next/src/lib/billing/types.ts` | **新建** | `PlanCode` / `BillingPeriod` / `PlanPriceMap` / `Plan` / `TrialConfig` / `PlansResponse` 类型；`EMPTY_PLANS_RESPONSE` fallback |
| `frontend-next/src/lib/billing/get-plans.ts` | **新建** | `getPlans()` fetch helper（`credentials: "omit"`，因为 `/api/plans` 是公开接口）+ `getPlansSafe()`（失败时返回 empty） |

### 测试与文档（新建 / 修改）

| 文件 | 类型 | 说明 |
|------|------|------|
| `tests/test_plan_catalog.py` | **新建** | 28 个测试，覆盖 plan table、pricing helpers、get_plan fallback、trial boundary（强断言 `frozen === False` 且无数字字段）、legacy views（`billing.PLAN_PRICES_CNY` 与 `job_intercept.PLAN_CATALOG` 必须等于 plan_catalog 派生视图）、`/api/plans` 响应 payload shape |
| `docs/specs/2026-04-04-pricing-and-plans-api-contract.md` | **新建** | API contract：endpoint 定义、字段分类（display / business）、`trial.frozen` 边界说明、未来字段列表、versioning、测试覆盖 |

**未修改**：`tests/test_billing.py`、`tests/test_gateway_create_job.py`、`tests/test_gateway_job_policy.py`、`tests/test_tts_routing_invariants.py`、`tests/test_gateway_quota.py`、`tests/test_gateway_entitlements.py`。全部不改动就能继续通过（因为旧导入名被保留为派生视图）。

## 5. 实际执行的命令

| # | 命令 | 结果摘要 |
|---|------|---------|
| 1 | Grep `PLAN_CATALOG` / `PLAN_PRICES_CNY` / import 位置 | 定位到 billing.py、job_intercept.py、4 个测试文件中的使用点 |
| 2 | Read `gateway/billing.py`（全文） | 确认价格表字面量 + `create_order` 使用点 |
| 3 | Read `gateway/job_intercept.py:40-140, 260-390` | 确认 `PLAN_CATALOG` 定义 + 在 `intercept_create_job` 中的使用点 |
| 4 | Read `gateway/main.py:1-130` | 确认 router 挂载模式与 `include_router` 位置 |
| 5 | Read `tests/test_billing.py:1-80, 530-549` | 确认 `PLAN_PRICES_CNY` 在 `TestConstants` 中的导入方式 |
| 6 | Read `tests/test_gateway_job_policy.py`（PLAN_CATALOG 部分） | 确认该文件不在允许修改列表，需要保留 `PLAN_CATALOG` 导出 |
| 7 | Write `gateway/plan_catalog.py`（246 行） | 新建真相源模块 |
| 8 | Edit `gateway/billing.py` | import + 派生视图 + `create_order` 价格查询改造 |
| 9 | Edit `gateway/job_intercept.py` | 字面量替换为派生视图 |
| 10 | Edit `gateway/main.py` | 添加 import + `include_router`（2 行） |
| 11 | Write `frontend-next/src/lib/billing/types.ts` | 类型定义 + fallback |
| 12 | Write `frontend-next/src/lib/billing/get-plans.ts` | fetch helper |
| 13 | Write `docs/specs/2026-04-04-pricing-and-plans-api-contract.md` | API contract |
| 14 | Write `tests/test_plan_catalog.py`（28 tests） | 测试覆盖 |
| 15 | `pytest tests/test_plan_catalog.py -q` | 28 passed |
| 16 | `pytest tests/test_plan_catalog.py tests/test_billing.py tests/test_gateway_create_job.py -q` | **86 passed** |
| 17 | `pytest tests/test_gateway_job_policy.py tests/test_tts_routing_invariants.py tests/test_gateway_quota.py tests/test_gateway_entitlements.py -q` | **70 passed**（验证未触动的下游测试没有回归） |
| 18 | `npm run lint`（frontend-next） | 0 errors, 5 warnings（均为 T0 前已存在） |
| 19 | `python main.py --help` | 正常输出 CLI 帮助 |

### Python 环境备注

首次尝试用全局 `python` 运行 pytest，遇到 WindowsApps 别名退出码 49 的问题。切换到 `C:/Users/Administrator/.local/bin/python.cmd`（Python 3.12.13 + pytest 9.0.2）后全部正常。这是本机环境问题，与 T0 代码无关。

## 6. 验证结果

### `pytest tests/test_plan_catalog.py tests/test_billing.py tests/test_gateway_create_job.py -q`

```
........................................................................ [ 83%]
..............                                                           [100%]
86 passed, 1 warning in 1.90s
```

warning 为 `pydub/utils.py` 的 `audioop` deprecation，与本次改动无关。

### 额外验证：未被 T0 指令列为必须的下游测试

```
pytest tests/test_gateway_job_policy.py tests/test_tts_routing_invariants.py tests/test_gateway_quota.py tests/test_gateway_entitlements.py -q
70 passed, 2 warnings in 2.45s
```

这是关键的 regression 证据：由于 `PLAN_CATALOG` 和 `PLAN_PRICES_CNY` 被保留为派生视图，`test_gateway_job_policy.py`（assert `PLAN_CATALOG["free"]["max_duration_minutes"] == 10` 等）和 `test_tts_routing_invariants.py` 等**完全不需要修改**就继续通过。

### `npm run lint`

```
✖ 5 problems (0 errors, 5 warnings)
```

0 errors。5 个 warning 均为 T0 前已存在的 unused vars / custom font 警告。新增的 `lib/billing/types.ts` 和 `lib/billing/get-plans.ts` 无任何 lint 问题。

### `python main.py --help`

正常输出完整 CLI 帮助（`process` / `control-panel` / `job-api` / `voice-registry` / `voice-clone` 等子命令），保持仓库基线要求。

## 7. 风险与权衡

### 当前仍未进入的 Task 2/3/4 内容

| 任务 | 未做内容 |
|------|---------|
| Task 2 | 正式 marketing 首页 / 定价页 / Trial 页；Stitch 设计稿落地；WG1/WG2 文案；定价卡片、FAQ、Final CTA 组件；把 `getPlans()` 实际接到页面 |
| Task 3 | 手机号登录 / 短信验证码 / Trial 发放逻辑 / 风控 / captcha gate |
| Task 4 | `subscriptions` / `billing_invoices` 表；`PaymentOrder` 渐进迁移；`GET /api/me/subscription` / `GET /api/billing/history` |

### 仍待项目开发者拍板的商业事实

1. **Trial 天数**（v1 / WG1 草稿 = 7 天，未冻结）
2. **Trial 源分钟数**（WG1 草稿 = 20 分钟，未冻结）
3. **Trial 是否含 Studio 模式**（未决）
4. **Trial 是否强制手机号**（与 Task 3 联动，未决）
5. **Plus 月价最终值**：当前仓库是 ¥69，transformation-plan 文档一度提到 ¥99。本轮遵守"事实统一优先"，保留 ¥69。**如果项目开发者最终决定提价，需要在 plan_catalog 中改一次，billing.py / frontend 全部自动同步。**
6. **套餐中文显示名**（当前只有英文 "Free" / "Plus" / "Pro"）
7. **套餐权益 bullet 文案**
8. **团队 / 企业套餐何时引入**

### 存在的边界问题

**无需升级到 CodeX / Human 的问题。** 但有一点需要后续阶段留意：

- `plan_catalog.router` 的 prefix 是 `/api`，endpoint 是 `/plans`，最终路径 `/api/plans`。由于 `/api/*` 在 gateway 中通常已经归属一系列 include_router（admin、billing、entitlements、voice_catalog 等），需要确认未来 Task 3/4 添加的新 router 不会与 `/api/plans` 冲突。当前 main.py 的 include_router 顺序保证 `plan_catalog_router` 在 proxy catch-all 之前注册，无冲突。

- `/api/plans` 的前端 fetch 使用 `credentials: "omit"`。如果部署层未来要把 `/api/*` 统一加 CSRF 检查，这里不会受影响（因为公开接口本来就不带 cookie）。但仍需在 Milestone C 前确认 `middleware.ts` 或未来的 proxy 配置不会强制 credentials。

## 8. 产出文件

### 新建

- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/plan_catalog.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_plan_catalog.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/specs/2026-04-04-pricing-and-plans-api-contract.md`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/lib/billing/types.ts`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/lib/billing/get-plans.ts`

### 修改

- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/billing.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/job_intercept.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/main.py`

### 未修改（按 T0 边界要求）

- `gateway/models.py`、`gateway/auth*.py`、`gateway/payment_providers*`、任意 Alembic migration
- `tests/test_billing.py`、`tests/test_gateway_create_job.py`、`tests/test_gateway_job_policy.py`、`tests/test_tts_routing_invariants.py`
- 任意 `frontend-next/src/app/(marketing|auth|app)/*`

## 9. 验收结论

对照 v2 Task 0 验收点：

| 验收点 | 状态 | 证据 |
|--------|------|------|
| 一个集中式的 gateway 套餐/试用真相源模块 | ✅ | `gateway/plan_catalog.py` 定义 `PLANS` + helpers |
| 一个公开可读的 `GET /api/plans` | ✅ | router 挂载完成；无 `Depends(require_auth)`；`_build_plans_response` 测试通过 |
| `billing.py` 与 `job_intercept.py` 改为消费同一真相源 | ✅ | 两个模块的硬编码字面量已被派生视图替换；`TestLegacyViews.test_billing_module_derives_from_plan_catalog` 和 `test_job_intercept_module_derives_from_plan_catalog` 显式验证 |
| 前端新增只读 `billing` 类型与 plans 获取层 | ✅ | `frontend-next/src/lib/billing/{types.ts,get-plans.ts}`；未接页面 |
| 一份轻量 API contract 文档 | ✅ | `docs/specs/2026-04-04-pricing-and-plans-api-contract.md`；含 trial 边界说明 |
| 对应测试与验证命令可通过 | ✅ | 86 passed（T0 指定 3 个测试文件）+ 70 passed（下游 regression） |
| Trial 数字未被伪装成已拍板事实 | ✅ | `TRIAL_CONFIG["frozen"] = False`；API 响应不含任何数字字段；`TestTrialBoundary` 显式断言禁止 |
| main.py / pytest 保持可运行 | ✅ | `python main.py --help` 正常；所有相关测试通过 |
| 未进入 Task 2/3/4/5/6 | ✅ | 未改动任何 marketing / auth / subscription / payment provider / migration 文件 |

**Task 0 完成。**

## 10. 是否已停止

**已停止。** 等待 CodeX 审核后给出下一步指令。
