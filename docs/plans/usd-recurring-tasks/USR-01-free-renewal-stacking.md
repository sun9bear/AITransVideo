# USR-01：同档自由续费 + 时长顺延 + 积分 FIFO（P0.5 快赢）

> 母方案 §4 + Q8。**纯改现有一次性付费模型,不碰 provider 订阅、无合规增量、无 schema migration。** 可脱离整个续费大项目先单独上。
> 范围内：**同档续费**（`target_plan_code == 当前 plan`）。范围外：跨档升级/降级（保留现语义,Q6 另开专项）、provider 订阅、合规。

---

## Step 0 — 确认现状（开工前 re-read,防漂移）

| 改动点 | 现状 file:line | 现行为 |
|---|---|---|
| A 建单闸 | `gateway/billing.py:149-155` | `plan_rank={free:0,plus:1,pro:2}; if rank[target] <= rank[current]: 400` → 同档/降级都挡 |
| B 顺延 | `gateway/subscriptions.py:188-194` | 已有 active 行：`current_period_start=paid_at; current_period_end=_period_end(paid_at,period)` → **重置、丢剩余时长** |
| C 积分消耗序 | `gateway/credits_service.py:213-221` `_pick_buckets_by_priority` | `sorted(key=type_rank)`(稳定排序,同 type 内沿用 `_load_buckets` 的 `order_by(created_at)` line 197) → **非 earliest-expiring** |
| D 前端闸 | `frontend-next/src/components/billing/checkout-card.tsx`（`isDowngradeOrSame = selectedRank <= currentRank && currentRank>0` → `canCheckout` false） | 同档/降级不可结账 |

---

## 改动

### A. 后端建单闸：放开同档（`billing.py:151`）
```python
# 现：if plan_rank.get(target,0) <= plan_rank.get(current,0): 400
# 改：只拒严格降级
if plan_rank.get(body.target_plan_code, 0) < plan_rank.get(current_plan, 0):
    raise HTTPException(400, detail=f"不可降级：当前套餐({current_plan})高于目标({body.target_plan_code})；同档可续费、升级可升级")
```
- `==`（同档续费）+ `>`（升级）放行；`<`（严格降级）仍 400。

### B. 后端顺延：同档不重置（`subscriptions.py` `upsert_active_subscription` existing 分支 188-194）
```python
# existing 分支内：
if order.target_plan_code == existing.plan_code:
    # 同档续费 → 在剩余时长上顺延（不重置 start）
    anchor = max(paid_at, existing.current_period_end or paid_at)
    existing.current_period_end = _period_end(anchor, order.billing_period)
    # current_period_start 不动（保留首期起点）
else:
    # 跨档（升级）→ 保留现有重置语义（Q6：跨档另议）
    existing.current_period_start = paid_at
    existing.current_period_end = _period_end(paid_at, order.billing_period)
existing.plan_code = order.target_plan_code
existing.billing_period = order.billing_period
existing.provider = order.provider
existing.updated_at = paid_at
```
- 同档：`end = max(now, 旧end) + 周期天数`（顺延）；跨档：现状不变。

### C. 积分 earliest-expiring-first FIFO（`credits_service.py` `_pick_buckets_by_priority:221`）
```python
_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)
# 现：return sorted(buckets, key=lambda b: type_rank.get(b.bucket_type, 99))
# 改：次级按 expires_at 升序、无到期(None)排最后
return sorted(buckets, key=lambda b: (type_rank.get(b.bucket_type, 99), b.expires_at or _FAR_FUTURE))
```
- **效果**：保留 type 优先级；同 type 内**先消耗最早到期的 bucket**。对 subscription 续费=Q8（旧 bucket 不延寿、先到期先用）；对其它类型=合理改进（先用快过期的、减浪费）。
- ⚠️ **这是全类型消耗序的行为变更**：开工时**审计现有消耗测试**是否断言 `created_at` 序,按需改成 `expires_at` 序或确认无依赖。

### D. 前端：放开同档结账 + 文案（`checkout-card.tsx`）
- `isDowngradeOrSame` → `isStrictDowngrade = selectedRank < currentRank && currentRank > 0`;`canCheckout` 用它。
- 同档时 CTA/提示用 message key（如 `billing.checkout.renewCta`="续费/续期",**zh-CN 源 + en**）显示「续费、时长顺延」,**不内联 CJK**。

---

## 测试

- `tests/test_billing.py`：同档 `create_order` 200 不再 400；严格降级 400；升级 200。
- `tests/test_subscriptions.py`：同档续费 `current_period_end == _period_end(max(now,旧end),period)` 且 `current_period_start` 不变；跨档升级仍 reset。
- `tests/test_credits_read.py`（或新）：同 type 多 bucket 不同 `expires_at` → 消耗顺序 earliest-first；`expires_at=None` 排最后。
- 前端：同档时 `canCheckout=true`、CTA 显示「续费」；`tsc`/`eslint`/`cjk-guard` 绿。

## 验收命令
```bash
python -m pytest tests/test_billing.py tests/test_subscriptions.py tests/test_credits_read.py tests/test_credits_service.py -q
cd frontend-next && npx tsc --noEmit --incremental --tsBuildInfoFile node_modules/.cache/tsc-usr01.tsbuildinfo \
  && node scripts/cjk-guard.mjs && npx eslint src/components/billing/checkout-card.tsx
```
+ backend-full-suite **set-diff**（对照 baseline 无新失败,本机 ~335 预存失败按 [[feedback_test_database_stub_convention]]）。

## 回滚 / DoD

- **回滚**：`git revert`（4 处改动 + 测试,纯逻辑无 schema,安全）。
- **DoD**：A-D 四改 + 新增/更新测试绿 + set-diff 无新失败 + 前端 gate 绿 + 多 lens 对抗评审(重点:同档边界、FIFO 全类型影响、跨档未误改) + @codex + CI 全绿 → squash-merge。
- **红线核对**：未碰付费 API auto-fallback;CNY 续费仍用户显式发起;旧 bucket 不延寿（FIFO 各算各的）。
