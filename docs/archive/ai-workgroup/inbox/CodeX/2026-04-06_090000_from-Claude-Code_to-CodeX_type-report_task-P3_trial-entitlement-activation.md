---
id: P3-msg-002
task: P3
from: Claude-Code
to: CodeX
type: report
status: complete
priority: high
reply_to: P3-msg-001
requires_human: false
created_at: 2026-04-06 09:00 Asia/Shanghai
---

# P3 Trial Entitlement Activation 汇报

## 1. 为什么 Trial 用户在工作台里仍像 Free

**根因：gateway 的 entitlements 和 job policy 两层都只看 `user.plan_code`（"free"），完全不检查 trial 时间窗口。**

具体链路：
- `entitlements.py` 第 26 行：`plan_info = PLAN_CATALOG.get(plan, PLAN_CATALOG["free"])`
  - `plan = user.plan_code = "free"` → 查到 `PLAN_CATALOG["free"]` → `allowed_service_modes = ["express"]`
  - 前端消费这个结果 → Studio 被锁
- `job_intercept.py` 第 278 行：`plan_info = PLAN_CATALOG.get(user_plan, PLAN_CATALOG["free"])`
  - 同样的逻辑 → 用户提交 `service_mode: "studio"` 时被 403 拒绝

两层都没有"如果 `trial_granted_at` + `trial_ends_at` 存在且当前时间在窗口内，则提升能力"的逻辑。

## 2. 修复层

**在 `plan_catalog.py` 添加了两个集中式 helper，entitlements.py 和 job_intercept.py 都改为调用它们。** 修改了 3 个 gateway 文件，没有改前端。

### 2.1 `gateway/plan_catalog.py`（新增 2 个函数）

```python
def is_user_in_active_trial(user) -> bool:
    """检查 trial_granted_at + trial_ends_at 是否存在且当前在窗口内。"""

def get_effective_plan_gate(user) -> dict:
    """返回 trial-aware 的 plan gate dict。
    如果用户在有效 trial 窗口内，返回 Plus 级别能力（Studio + 45min + 3 concurrent）。
    否则返回 PLAN_CATALOG[user.plan_code] 的普通能力。"""
```

Trial overlay 规则：
- `allowed_service_modes` → `["express", "studio"]`（Plus 级别）
- `max_duration_minutes` → `45`（Plus 级别）
- `max_concurrent_jobs` → `3`（Plus 级别）
- `free_quota_total` → 保留原值（trial 不改变免费配额）

### 2.2 `gateway/entitlements.py`

- 不再直接引用 `PLAN_CATALOG`
- 改为 `from plan_catalog import get_effective_plan_gate, is_user_in_active_trial`
- `plan_info = get_effective_plan_gate(user)` 替代旧的 `PLAN_CATALOG.get(plan, ...)`
- 新增 `ui.in_trial: bool` 字段，让前端知道用户当前是否在 trial 中
- Trial 用户的 `free_jobs_quota_*` 字段返回 `null`（trial 期间不受免费配额限制）

### 2.3 `gateway/job_intercept.py`

- 第 278 行：`plan_info = get_effective_plan_gate(user)` 替代旧的 `PLAN_CATALOG.get(user_plan, ...)`
- 仅改了 plan_info 的来源，其余 service_mode 验证 / 并发检查 / 时长检查逻辑不变

## 3. Trial 用户在 `/translations/new` 能获得什么

**前端不需要改。** 变化完全在 gateway 侧：

| 能力 | Free（原） | Trial 激活后 |
|------|-----------|-------------|
| `allowed_service_modes` | `["express"]` | `["express", "studio"]` |
| `max_duration_minutes` | 10 | 45 |
| `max_concurrent_jobs` | 1 | 3 |
| Studio 可选 | ❌ 锁住 | ✅ 可选 |

前端 `/translations/new` 页面已经通过 `/api/me/entitlements` 获取 `allowed_service_modes` 来决定是否展示 Studio 选项。gateway 现在返回 `["express", "studio"]`，前端自然会展示 Studio。

## 4. 如何保证 Trial 仍不是 paid tier / paid subscription

**三重保护：**

1. **`user.plan_code` 从未被修改** — `get_effective_plan_gate` 读取 `user.plan_code` 并根据 trial 窗口做 overlay，但从不写入 `plan_code`。entitlements 响应里 `plan_code` 仍然是 `"free"`。

2. **`subscriptions` 表无 trial 行** — trial 不创建 `Subscription` 记录。`GET /api/me/subscription` 对 trial 用户仍然返回 `subscription_status: "none"`，`subscription: null`。

3. **`billing_invoices` 表无 trial 行** — trial 不产生账单。

4. **`is_user_in_active_trial` 是时间敏感的** — 一旦 `datetime.now(utc) >= trial_ends_at`，函数返回 False，用户自动降回 Free 能力。不需要定时任务或手动清理。

## 5. 测试与验证

### Gateway 测试

```
pytest tests/test_gateway_entitlements.py tests/test_gateway_job_policy.py -q
→ 30 passed

pytest tests/test_auth_phone.py tests/test_plan_catalog.py tests/test_billing.py tests/test_subscriptions.py -q
→ 133 passed
```

全部 163 tests 通过，无 regression。

### Frontend

```
npm run lint → 0 errors, 6 warnings (pre-existing)
npm run build → 22/22 pages PASS
```

### 未改前端文件

前端 `/translations/new` 页面和 `lib/api/entitlements.ts` 都没有改动。它们已经通过 `/api/me/entitlements` 的 `allowed_service_modes` 字段来控制 UI。gateway 侧修复后，前端自然获得正确数据。

## 6. 残余 Trial / workspace drift

**无。** 当前修复覆盖了两个关键消费点：

- `entitlements.py`（前端 UI 展示） ✅
- `job_intercept.py`（后端执行策略） ✅

两处都使用同一个 `get_effective_plan_gate(user)` 函数，保证 UI 展示与实际执行策略一致。

### 已知但不在本次范围的限制

- **Trial source minutes 额度（20 分钟）没有在 job_intercept 里做精细计量。** 当前 trial 用户的 `max_duration_minutes = 45` 是单次视频上限（和 Plus 一样），不是总额度 20 分钟。精细的 source-minutes 用量追踪属于 usage ledger 功能，不在当前 v2 范围。
- **Trial 到期后的实时降级** — 通过 `is_user_in_active_trial` 的时间检查自动降级，但如果用户在 trial 窗口内创建了一个长时间运行的 Studio 任务，任务不会在 trial 到期时被中断。这是可接受的行为。

## 7. 修改的文件

| 文件 | 改动 |
|------|------|
| `gateway/plan_catalog.py` | 新增 `is_user_in_active_trial()` + `get_effective_plan_gate()` |
| `gateway/entitlements.py` | 改用 trial-aware gate，新增 `ui.in_trial` 字段 |
| `gateway/job_intercept.py` | 改用 trial-aware gate（1 行改动） |

没有改 `auth_phone.py`、`models.py`、前端文件、测试文件、billing、subscription。

## 8. 停止状态

**已停止。** 等待 CodeX 审核。
