# [Completion Report] V3-2 Minor Revision

- **From:** Claude Code
- **To:** CodeX
- **Type:** completion-report
- **Task:** V3-2 minor revision
- **Date:** 2026-04-07
- **Status:** Complete

---

## 1. 前端预计扣点现在是否完全消费 Gateway estimate

**是。** 前端 `CostEstimatePanel` 已移除本地硬编码的 `CREDITS_RATE` 映射，改为通过 `fetch('/api/credits/estimate?minutes=X&service_mode=Y&quality_tier=standard')` 调用 Gateway 真相源。

### 修改前

```tsx
const CREDITS_RATE: Record<string, number> = { express: 10, studio: 15 }
const rate = CREDITS_RATE[serviceMode] ?? 10
const creditsEstimate = Math.round(minutes * rate)
```

### 修改后

```tsx
// useEffect 里并发请求 3 个时长的 estimate
const res = await fetch(`/api/credits/estimate?minutes=${m}&service_mode=${serviceMode}&quality_tier=standard`)
const data = await res.json()
// creditsMap[minutes] = data.estimated_credits
```

行为变化：
- 切换 `serviceMode` 时，自动重新请求 Gateway estimate
- API 不可用时，点数徽章不展示（graceful degradation），费用预估照常显示
- 不再有前端与 Gateway 参数 drift 的风险

---

## 2. 哪些 bucket 已接入 live grant

### Free bucket — 已接入

| 触发路径 | 实现 |
|----------|------|
| 注册完成 (`complete-registration`) | `auth_phone.py` → `ensure_free_bucket(db, user.id)` |
| 首次读取 credits (`GET /api/me/credits`) | `credits_read.py` → lazy `ensure_free_bucket(db, user.id)` |

- 金额：500 点（`GRANT_AMOUNTS["free"]`）
- 过期：永不过期
- 幂等性：`ensure_free_bucket` 先检查是否已存在同类型 bucket，存在则跳过

### Trial bucket — 已接入

| 触发路径 | 实现 |
|----------|------|
| 注册完成且 trial 资格通过 | `auth_phone.py` → `ensure_trial_bucket(db, user.id, trial_ends_at)` |
| 已有用户首次验证码登录且 trial 资格通过 | `auth_phone.py` → `ensure_trial_bucket(db, user.id, trial_ends_at)` |
| 首次读取 credits 且 `is_user_in_active_trial` | `credits_read.py` → lazy `ensure_trial_bucket(db, user.id, trial_ends_at)` |

- 金额：300 点（`GRANT_AMOUNTS["trial"]`）
- 过期：`user.trial_ends_at`（7 天后）
- 幂等性：同 free，先检查已有 trial bucket

### Subscription bucket — 已接入

| 触发路径 | 实现 |
|----------|------|
| 支付成功 webhook 结算 (`_process_payment_event`, status="paid") | `billing.py` → `ensure_subscription_bucket(db, user.id, plan_code, order_id, sub_id, period_end)` |

- 金额：Plus = 3500 点，Pro = 12000 点（`GRANT_AMOUNTS[plan_code]`）
- 过期：`subscription.current_period_end`
- 幂等性：依赖 webhook 幂等机制（`PaymentWebhookEvent` 去重）+ 每个新计费周期创建新 bucket

---

## 3. 哪些 bucket 仍未接入

| Bucket 类型 | 状态 | 原因 |
|-------------|------|------|
| `topup` | **未接入** | Top-up 购买流程尚未建设（V3 后续阶段） |
| `manual_adjustment` | **未接入** | 无 admin 端点调用 `shadow_grant`（admin 工具后续阶段） |

---

## 4. `/api/me/credits` 对真实用户是否已能返回非零 bucket

**是。** 三条路径确保真实用户能看到有意义的 credits：

1. **新注册用户**：注册完成时自动创建 free bucket (500) + trial bucket (300，若 trial 资格通过)
2. **已有用户首次访问**：`GET /api/me/credits` 会 lazy-ensure free bucket + trial bucket
3. **付费用户**：支付结算时自动创建 subscription bucket (3500/12000)

一个典型的新注册 trial 用户的 `/api/me/credits` 返回：

```json
{
  "total_available": 800,
  "buckets": [
    {"type": "free", "remaining": 500, "reserved": 0, "granted": 500, "expires_at": null},
    {"type": "trial", "remaining": 300, "reserved": 0, "granted": 300, "expires_at": "2026-04-14T..."}
  ],
  "in_trial": true,
  "trial_expires_at": "2026-04-14T..."
}
```

---

## 5. 哪些部分仍然保持 shadow / not-yet-truth

| 事项 | 状态 |
|------|------|
| credits 余额用于 job gating | **未切真** — V2 quota 仍是 free 用户门控 |
| credits 取代 plan_code / trial 真值 | **未切真** — entitlements 仍读 V2 |
| credits 取代 billing / subscription 真值 | **未切真** — billing.py 结算逻辑不变 |
| `quality_tier` 动态传入 estimate | **未动态化** — 前端固定 `standard` |
| `final_cn_chars` / `tts_billed_chars` 真实采集 | **未落地** — 仅 schema 预留 |
| Top-up purchase | **未建设** |
| V2 quota 退役 | **未做** |

---

## 6. 测试与验证结果

### Credits 全量测试（44/44 通过）

```
python -m pytest tests/test_credits_read.py tests/test_credits_service.py -v
============================= 44 passed in 2.19s ==============================
```

### V2 回归测试（148/148 通过）

```
python -m pytest tests/test_gateway_job_policy.py tests/test_gateway_quota.py \
  tests/test_billing.py tests/test_subscriptions.py tests/test_plan_catalog.py -v
============================= 148 passed in 1.68s =============================
```

### 前端

```
npm run lint   → 0 errors, 7 warnings（均为已有 + 1 个 useEffect deps cosmetic warning）
npm run build  → 成功
```

---

## 7. 修改的文件总结

| 文件 | 变更 |
|------|------|
| `gateway/credits_service.py` | 新增 `GRANT_AMOUNTS` 常量 + `ensure_free_bucket` / `ensure_trial_bucket` / `ensure_subscription_bucket` 三个幂等 grant helpers |
| `gateway/credits_read.py` | `GET /api/me/credits` 中 lazy-ensure free + trial bucket |
| `gateway/auth_phone.py` | 注册完成时 + 首次登录 trial 授予时调用 `ensure_free_bucket` / `ensure_trial_bucket` |
| `gateway/billing.py` | 支付结算时调用 `ensure_subscription_bucket` |
| `frontend-next/src/app/(app)/translations/new/page.tsx` | `CostEstimatePanel` 改用 `fetch('/api/credits/estimate')` 替代本地硬编码 |

### 未修改

- `plan_catalog.py` — 冻结商业事实未改
- `subscriptions.py` — 未切到 credits 真值
- `entitlements.py` — 未切到 credits 真值
- `quota.py` — V2 配额完整保留
- migration 009 — 未变更
