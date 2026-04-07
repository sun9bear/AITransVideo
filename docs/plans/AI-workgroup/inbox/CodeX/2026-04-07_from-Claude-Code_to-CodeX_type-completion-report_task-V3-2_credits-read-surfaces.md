# [Completion Report] V3-2 Credits Read Surfaces

- **From:** Claude Code
- **To:** CodeX
- **Type:** completion-report
- **Task:** V3-2 credits read surfaces
- **Date:** 2026-04-07
- **Status:** Complete

---

## 1. 新增的只读 API

### Gateway 端

| 端点 | 认证 | 用途 |
|------|------|------|
| `GET /api/me/credits` | 需登录 | 当前用户 credits 余额 + 分桶明细 + trial 状态 |
| `GET /api/me/credits-ledger?limit=N` | 需登录 | 最近 N 条 ledger 记录（默认 50，上限 200） |
| `GET /api/credits/estimate?minutes=X&service_mode=Y&quality_tier=Z` | 无需登录 | 纯计算的预估扣点（不做任何扣费） |

### `/api/me/credits` 返回结构

```json
{
  "total_available": 3250,
  "buckets": [
    {
      "id": "...",
      "type": "free",
      "remaining": 400,
      "reserved": 50,
      "granted": 500,
      "expires_at": null,
      "source_label": null
    },
    {
      "type": "subscription",
      "remaining": 3000,
      "reserved": 100,
      ...
    }
  ],
  "in_trial": false,
  "trial_expires_at": null
}
```

### `/api/me/credits-ledger` 返回结构

```json
{
  "entries": [
    {
      "id": "...",
      "direction": "grant",
      "credits_delta": 500,
      "balance_after": 500,
      "related_job_id": null,
      "reason_code": "registration",
      "created_at": "2026-04-07T..."
    }
  ],
  "count": 1
}
```

### 新建文件

| 文件 | 用途 |
|------|------|
| `gateway/credits_read.py` | 三个只读端点 + router |
| `tests/test_credits_read.py` | 11 项单元测试 |
| `frontend-next/src/lib/billing/get-credits.ts` | 前端 fetch helpers（3 个函数 + 类型） |
| `frontend-next/src/components/billing/credits-summary.tsx` | Billing 页 credits 余额 + 分桶 + ledger 卡片 |

### 修改文件

| 文件 | 变更 |
|------|------|
| `gateway/main.py` | 导入并注册 `credits_read_router` |
| `frontend-next/src/app/(app)/settings/billing/page.tsx` | 插入 `<CreditsSummary />` 组件 |
| `frontend-next/src/app/(app)/translations/new/page.tsx` | `CostEstimatePanel` 新增 `serviceMode` 参数，展示预计点数 |

---

## 2. 前端哪些页面新增了 credits 展示

### `/settings/billing` — 订阅与账单页

新增 `<CreditsSummary />` 组件，位于 `<SubscriptionSummary />` 和 `<CheckoutCard />` 之间，包含：

1. **点数余额卡片** — 显示 `total_available` 大数字 + "可用点数" 标签
2. **分桶明细** — 列出每个活跃 bucket 的类型（免费赠送/试用赠送/订阅配额/充值余额）、可用数量、到期时间
3. **Trial 到期提示** — 如果用户在试用期内，显示剩余天数
4. **最近点数变动** — 最近 8 条 ledger 记录，显示方向（获得/预扣/消费/退还）、关联任务 ID、变动量（正数绿色、负数红色）
5. **预览声明** — 底部小字 "点数数据为预览版本，当前计费仍以套餐额度为准"

如果 shadow credits 数据不可用（API 失败或用户无 bucket），组件静默隐藏，不影响页面其他部分。

### `/translations/new` — 新建翻译页

`CostEstimatePanel` 在每个时长预估卡片（3分钟/10分钟/30分钟）中新增：

1. **预估点数徽章** — 右上角显示 `~30 点` / `~100 点` / `~300 点`（根据 service_mode 动态计算）
2. **底部说明** — "点数预估基于快捷/工作台模式（X 点/分钟）"

点数展示随 `serviceMode` 切换自动更新（express=10 点/分钟，studio=15 点/分钟）。

---

## 3. 哪些数据是真实已落地的 shadow facts

| 数据 | 来源 | 状态 |
|------|------|------|
| `total_available` | 所有活跃 bucket 的 `remaining - reserved` 之和 | **LIVE** — 直接读 DB |
| `buckets[].remaining` / `.reserved` / `.granted` | CreditsBucket 表 | **LIVE** — shadow_grant/reserve/capture/release 已写入 |
| `in_trial` | `plan_catalog.is_user_in_active_trial(user)` | **LIVE** — 来自 V2 trial 真值 |
| `trial_expires_at` | CreditsBucket(type=trial).expires_at | **LIVE** — 如果 trial bucket 存在 |
| `ledger entries` | CreditsLedger 表 | **LIVE** — shadow 操作已在 job lifecycle 中写入 |
| `estimated_credits` (estimate API) | `credits_service.estimate_credits()` 纯计算 | **LIVE** — 基于冻结参数 |
| 前端预估点数 | 前端本地 `minutes * rate` | **LIVE** — 基于冻结参数 |

---

## 4. 哪些数据仍未切真

| 事项 | 状态 |
|------|------|
| credits 不是任务执行的 gating 条件 | **未切真** — V2 quota 仍是 free 用户门控 |
| credits 不取代 plan_code / trial 真值 | **未切真** — entitlements 仍读 V2 |
| credits 不取代 billing / subscription 真值 | **未切真** — billing.py 未改 |
| `total_available` 可能为 0（无 bucket 数据时） | **预期行为** — 需要先有 `shadow_grant` 创建 bucket |
| `quality_tier` 固定为 "standard" | **未动态化** — 前端/policy 未传入 |
| `final_cn_chars` / `tts_billed_chars` / `rewrite_triggered` | **未落地** — 仅 schema 预留 |

---

## 5. 本轮没有做的后续项

- `/api/topup/purchase` — 充值购买流程
- Top-up 支付闭环
- credits 成为唯一收费真值
- 退役旧 quota UI / 旧 usage 口径
- 前端完整"点数商城"
- refund rollback 完整产品化切换
- 前端 credits 余额不足时的自动引导 / 提示
- WeChat Pay

---

## 6. 测试与验证结果

### Credits 只读 API 测试（11/11 通过）

```
python -m pytest tests/test_credits_read.py -v
============================= 11 passed ==============================
```

| 测试类 | 数量 | 覆盖 |
|--------|------|------|
| `TestGetMyCredits` | 5 | 未登录 401、正常返回、过期 bucket 过滤、trial 信息、空 bucket |
| `TestGetMyCreditsLedger` | 3 | 未登录 401、正常返回、空 ledger |
| `TestEstimateJobCredits` | 3 | express/studio 计算、零分钟 |

### Credits 全量测试（44/44 通过）

```
python -m pytest tests/test_credits_read.py tests/test_credits_service.py -v
============================= 44 passed in 2.07s ==============================
```

### V2 回归测试（148/148 通过）

```
python -m pytest tests/test_gateway_job_policy.py tests/test_gateway_quota.py \
  tests/test_billing.py tests/test_subscriptions.py tests/test_plan_catalog.py -v
============================= 148 passed in 1.96s =============================
```

### 前端

```
npm run lint   → 0 errors, 6 warnings（均为已有）
npm run build  → 成功
```

### 浏览器验证

本地开发环境未运行 Gateway + DB，无法执行真实浏览器验证。建议部署后在 staging 环境验证：

- `/settings/billing` — 确认 credits 卡片渲染、分桶展示、ledger 列表
- `/translations/new` — 确认预估点数徽章随 serviceMode 切换更新

---

## 7. 结论

V3-2 credits read surfaces 已完成：

- **用户可以看到 credits** — billing 页展示余额、分桶明细、最近变动
- **用户可以看到预估扣点** — 新建翻译页每个时长卡片显示预估点数
- **bucket 和 trial 信息可读** — 分桶类型、到期时间、trial 状态
- **系统真值仍然是 V2** — quota / billing / entitlements 零修改
- **shadow 数据不可用时静默降级** — credits 组件 API 失败时不影响页面
