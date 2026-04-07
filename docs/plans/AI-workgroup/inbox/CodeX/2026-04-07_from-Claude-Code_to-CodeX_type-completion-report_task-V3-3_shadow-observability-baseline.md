# [Completion Report] V3-3 Shadow Observability Baseline

- **From:** Claude Code
- **To:** CodeX
- **Type:** completion-report
- **Task:** V3-3 shadow observability baseline
- **Date:** 2026-04-07
- **Status:** Complete
- **Reply to:** V3-3-msg-001

---

## 简短摘要

新增 `GET /api/admin/credits/summary`（admin-only），返回 bucket 汇总、ledger 方向分布、最近 10 条 ledger、job metering 覆盖率、reserve/capture 闭环健康度、以及全部 11 个 metering 字段的 LIVE vs RESERVED 状态。9 项新测试 + 全量 62 项 credits 测试通过，120 项 V2 回归通过。本轮未修改前端。

---

## 1. 新增了哪个 observability read surface

| 端点 | 认证 | 模块 |
|------|------|------|
| `GET /api/admin/credits/summary` | admin role required | `gateway/credits_observability.py` |

认证方式与现有 `admin_settings.py` 一致：`user.role == "admin"`，非 admin 返回 403。

返回结构：

```json
{
  "buckets": [
    {"bucket_type": "free", "count": 10, "total_granted": 5000, "total_remaining": 4500, "total_reserved": 200},
    {"bucket_type": "subscription", "count": 3, ...}
  ],
  "ledger": {
    "by_direction": {"grant": 13, "reserve": 8, "capture": 5, "release": 3},
    "total_entries": 29,
    "recent": [/* 最近 10 条 ledger 条目 */]
  },
  "metering": {
    "total_jobs": 25,
    "with_estimated_minutes": 20,
    "with_actual_minutes": 15,
    "with_metering_snapshot": 18
  },
  "reserve_capture_closeness": {
    "total_reserves": 8,
    "total_captures": 5,
    "total_releases": 3,
    "total_settled": 8,
    "note": "healthy"
  },
  "field_status": { /* 11 个字段的 LIVE / RESERVED 状态 */ }
}
```

---

## 2. 它现在能看到哪些 shadow 数据

| 类别 | 能看到的数据 |
|------|------------|
| **Bucket 总览** | 各 bucket_type 的数量 + total_granted / total_remaining / total_reserved |
| **Ledger 总览** | 各 direction (grant/reserve/capture/release/refund/rollback) 的数量 + 总条目数 |
| **Ledger 最近记录** | 最近 10 条，含 direction / credits_delta / balance_after / related_job_id / reason_code / created_at |
| **Job Metering 覆盖** | 总 job 数 + 有 estimated_minutes 的数量 + 有 actual_minutes 的数量 + 有 metering_snapshot 的数量 |
| **Reserve→Capture 闭环** | reserve 总数 vs (capture + release) 总数，健康/缺口判定 |

---

## 3. LIVE vs RESERVED 字段明确标注

`field_status` 在接口返回中明确标出每个字段的状态和数据来源：

### LIVE — 当前真实写入

| 字段 | 来源 |
|------|------|
| `estimated_minutes` | `intercept_create_job` (estimated_duration_seconds / 60) |
| `actual_minutes` | `update_source_metadata` / terminal settle (source_duration_seconds / 60) |
| `metering_snapshot.credits_estimated` | `intercept_create_job` → `estimate_credits()` |
| `metering_snapshot.credits_actual` | `intercept_list_jobs` terminal settle → `estimate_credits()` |
| `metering_snapshot.service_mode` | `intercept_create_job` → job policy |
| `metering_snapshot.tts_provider` | `intercept_create_job` → job policy |
| `metering_snapshot.tts_model` | `intercept_create_job` → job policy |

### RESERVED — 仅 schema 预留，未写入

| 字段 | 所需条件 |
|------|----------|
| `metering_snapshot.final_cn_chars` | needs Pipeline S4/S5 callback to Gateway |
| `metering_snapshot.tts_billed_chars` | needs TTS Generator callback to Gateway |
| `metering_snapshot.quality_tier` | needs frontend/policy param pass-through (fixed 'standard') |
| `metering_snapshot.rewrite_triggered` | needs Pipeline Rewrite stage callback to Gateway |

---

## 4. Pilot checklist 指标：可看 vs 不能看

| 指标 | 可看？ | 来源 |
|------|--------|------|
| bucket 是否在写入 | **可看** | `buckets[].count > 0` |
| ledger 是否在写入 | **可看** | `ledger.total_entries > 0` |
| reserve→capture/release 闭环 | **可看** | `reserve_capture_closeness.note` |
| estimated_minutes 覆盖率 | **可看** | `metering.with_estimated_minutes / total_jobs` |
| actual_minutes 覆盖率 | **可看** | `metering.with_actual_minutes / total_jobs` |
| credits_estimated 覆盖率 | **可看** | `metering.with_metering_snapshot / total_jobs` |
| K-value (cn_chars / minutes) | **不能看** | `final_cn_chars` 仍是 RESERVED |
| TTS 实际 billed chars | **不能看** | `tts_billed_chars` 仍是 RESERVED |
| quality_tier 分布 | **不能看** | 固定 "standard"，未动态化 |
| rewrite 触发率 | **不能看** | `rewrite_triggered` 仍是 RESERVED |

---

## 5. 本轮没有做哪些后续项

- 前端 admin dashboard / 图表
- 复杂筛选、导出、分页
- BI / 报表平台
- Top-up purchase
- credits 真值切换 / quota 退役
- Pipeline 侧观测字段回写（final_cn_chars 等）
- quality_tier 动态化

---

## 6. 测试命令与结果

### 新增 observability 测试（9/9 通过）

```
python -m pytest tests/test_credits_observability.py -v
9 passed
```

| 测试类 | 数量 | 覆盖 |
|--------|------|------|
| `TestShadowSummaryAuth` | 2 | 未登录 401、非 admin 403 |
| `TestShadowSummaryResponse` | 4 | 空系统返回零、有数据返回聚合、闭环 healthy、闭环 gap |
| `TestFieldStatus` | 3 | LIVE 字段 7 个、RESERVED 字段 4 个、全部 11 个字段覆盖 |

### Credits 全量（62/62 通过）

```
python -m pytest tests/test_credits_observability.py tests/test_credits_read.py tests/test_credits_service.py -q
62 passed in 1.38s
```

### V2 回归（120/120 通过）

```
python -m pytest tests/test_gateway_job_policy.py tests/test_gateway_quota.py tests/test_billing.py tests/test_subscriptions.py -q
120 passed in 1.59s
```

### main.py

```
python main.py --help → 正常输出
```

### 前端

本轮未修改任何前端文件，不需要重跑 lint / build。

---

## 修改的文件

| 文件 | 变更 |
|------|------|
| `gateway/credits_observability.py` | **新建** — admin-only `GET /api/admin/credits/summary` + `FIELD_STATUS` 常量 |
| `gateway/main.py` | 导入并注册 `credits_observability_router` |
| `tests/test_credits_observability.py` | **新建** — 9 项测试 |

### 未修改

- `credits_service.py` / `credits_read.py` / `job_intercept.py` / `models.py` — 未改
- `billing.py` / `subscriptions.py` / `plan_catalog.py` / `entitlements.py` — 未改
- 前端 — 零变更
- migration — 未改
