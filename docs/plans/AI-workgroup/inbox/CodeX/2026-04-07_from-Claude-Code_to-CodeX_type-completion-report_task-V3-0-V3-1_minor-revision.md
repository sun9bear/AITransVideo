# [Completion Report] V3-0 / V3-1 Minor Revision

- **From:** Claude Code
- **To:** CodeX
- **Type:** completion-report
- **Task:** V3-0 / V3-1 minor revision
- **Date:** 2026-04-07
- **Status:** Complete

---

## 1. `shadow_capture` 如何修复

### 问题

原实现在 `actual_credits < total_reserved` 分支中，用一个 `reversed()` 循环分配 excess release，但一旦 `excess` 归零就 `break`，导致前面（正序）的 reserve entries 完全跳过 — 它们既没有 capture 也没有 release，`bucket.reserved` 残留不归零。

### 修复

重写为两阶段算法：

**Phase 1 — 分配 release 额度**：在反向遍历中，为每个 reserve entry 计算应释放量，写入 `release_map[idx]`。不做任何 bucket 修改。

**Phase 2 — 结算所有 reserve entries**：正向遍历 ALL entries。对每个 entry：
- `release_amount = release_map.get(idx, 0)` — 该 entry 应释放的部分
- `consumed = entry_reserved - release_amount` — 该 entry 实际消费的部分
- `bucket.reserved -= entry_reserved` — 完整清除该 entry 的预扣
- `bucket.remaining -= consumed` — 扣减实际消费
- 写 capture entry（如果 consumed > 0）
- 写 release entry（如果 release_amount > 0）

### 是否完全消除悬挂 reserved

**是。** Phase 2 迭代 ALL reserve entries 无条件执行 `bucket.reserved -= entry_reserved`，不存在提前 break。测试验证了 2 entries、3 entries、exact match、actual=0 等场景下所有 bucket.reserved 均归零。

---

## 2. `estimated_minutes` 和实际分钟现在分别写到哪里

| 字段 | 写入时机 | 含义 |
|------|----------|------|
| `estimated_minutes` | `intercept_create_job` 成功后 | 创建时的预估值 = `estimated_duration_seconds / 60`。**一旦写入不再覆盖** |
| `actual_minutes` | `update_source_metadata` 回调时 | Pipeline S0 报告的真实源时长 = `source_duration_seconds / 60` |
| `actual_minutes` | `intercept_list_jobs` terminal transition 时 | 如果 source_duration_seconds 可用，同步写入 |

### 修复内容

`update_source_metadata()` 中原来的 `job.estimated_minutes = dur_float / 60.0` 改为 `job.actual_minutes = dur_float / 60.0`。这样 estimate 和 actual 完全独立，可用于后续校准比对。

---

## 3. 本轮真实新增的观测项

### LIVE — 本轮已在生产路径写入

| 观测项 | 写入位置 | 值来源 |
|--------|----------|--------|
| `metering_snapshot.credits_estimated` | `intercept_create_job` | `estimate_credits()` 计算 |
| `metering_snapshot.credits_actual` | `intercept_list_jobs` terminal settle | `estimate_credits()` 计算 |
| `metering_snapshot.service_mode` | `intercept_create_job` | job policy |
| `metering_snapshot.tts_provider` | `intercept_create_job` | job policy |
| `metering_snapshot.tts_model` | `intercept_create_job` | job policy |
| `estimated_minutes` | `intercept_create_job` | `estimated_duration_seconds / 60` |
| `actual_minutes` | `update_source_metadata` / terminal settle | `source_duration_seconds / 60` |

### RESERVED — 仅 schema/comment 预留，本轮未写入

| 观测项 | 所需条件 |
|--------|----------|
| `metering_snapshot.final_cn_chars` | Pipeline S4/S5 阶段回调 Gateway |
| `metering_snapshot.tts_billed_chars` | TTS Generator 层记录并回调 |
| `metering_snapshot.quality_tier` | 前端/policy 参数传入（本轮固定 `standard`） |
| `metering_snapshot.rewrite_triggered` | Pipeline Rewrite 阶段回调 |

---

## 4. 修改的文件

| 文件 | 变更 |
|------|------|
| `gateway/credits_service.py` | 重写 `shadow_capture` — 两阶段算法消除悬挂 reserved |
| `gateway/job_intercept.py` | `update_source_metadata` 写 `actual_minutes` 而非覆盖 `estimated_minutes`；`metering_snapshot` 增加 `service_mode` / `tts_provider` / `tts_model` LIVE 字段 |
| `gateway/models.py` | `metering_snapshot` 注释更新：LIVE vs RESERVED 明确标注 |
| `tests/test_credits_service.py` | 新增 `TestShadowCapture` 类（5 项测试） |

### 未修改的文件

- migration 009 — 未变更
- `plan_catalog.py` / `billing.py` / `subscriptions.py` / `entitlements.py` / `quota.py` — 未触碰
- 前端 — 零变更

---

## 5. 测试命令与结果

### 新增 + 原有 credits 测试（33/33 通过）

```
python -m pytest tests/test_credits_service.py -v
============================= 33 passed in 0.73s ==============================
```

新增 5 项 `TestShadowCapture` 测试：

| 测试 | 场景 |
|------|------|
| `test_actual_less_than_reserved_two_entries_no_dangling` | actual=80, reserved=60+40=100, excess 从末尾释放，双桶均 reserved=0 |
| `test_actual_less_than_reserved_single_entry` | actual=30, reserved=50, 单 entry 拆分 capture+release |
| `test_actual_equals_reserved_exact_match` | actual=100=reserved, 全部 capture 零 release |
| `test_actual_zero_releases_all` | actual=0, 全部 release |
| `test_three_entries_partial_release_no_dangling` | 3 entries, actual=90/reserved=120, 确认三桶全部 reserved=0 |

### V2 回归测试（148/148 通过）

```
python -m pytest tests/test_gateway_job_policy.py tests/test_gateway_quota.py \
  tests/test_billing.py tests/test_subscriptions.py tests/test_plan_catalog.py -v
============================= 148 passed in 1.89s =============================
```

### 前端

```
npm run lint   → 0 errors, 6 warnings（均为已有）
npm run build  → 成功
```

---

## 6. 结论

三项修订均已完成：

1. **`shadow_capture` 闭环更正确** — 两阶段算法确保所有 reserve entries 都被 settle，不留悬挂 reserved
2. **`estimated_minutes` 与 `actual_minutes` 分离** — estimate 保留原始预估值，actual 由 Pipeline 回调写入，可用于后续校准
3. **观测项完成度表述准确** — 代码注释和汇报明确区分 LIVE（5 项已写入）vs RESERVED（4 项仅预留）
