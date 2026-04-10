# 方案三：V3 成本监控

> 日期：2026-04-10
> 状态：方案（待审批）

---

## 1. 目标

新建管理员后台 `/admin/metrics` 页面，将 V3 pilot 期间手工查库的成本校准工作自动化，回答三个核心问题：

1. **数据健康吗** — shadow bucket/ledger/metering 是否在正常写入、闭环
2. **定价合理吗** — K 值、rewrite 率、预估/实扣偏差、provider 成本结构
3. **哪里在吞毛利** — 偏差最大的 job、rewrite 最多的 job、未闭环的 job

当前这些数据要么靠 `curl /api/admin/credits/summary` 肉眼看 JSON，要么靠 runbook 里的 4 条 SQL 手工查库。本方案把它们变成一个可视化页面。

---

## 2. 现有基础设施

| 组件 | 文件 | 说明 |
|------|------|------|
| Credits summary API | `gateway/credits_observability.py` | `GET /api/admin/credits/summary`，返回 bucket/ledger/metering/闭环/field_status |
| Job model V3 字段 | `gateway/models.py:165-188` | `estimated_minutes`, `actual_minutes`, `metering_snapshot` (JSONB) |
| Credits service | `gateway/credits_service.py` | shadow grant/reserve/capture/release/rollback，DEBIT_RATES，GRANT_AMOUNTS |
| Pilot runbook SQL | `docs/plans/2026-04-07-v3-pilot-observability-runbook.md §5.2` | K-value、service_mode 占比、rewrite 率、时长分布 |
| Admin 权限模式 | `gateway/credits_observability.py:96-101` | `_require_admin(user)` 校验 |

### metering_snapshot JSONB 字段清单

| 字段 | 状态 | 来源 |
|------|------|------|
| `credits_estimated` | LIVE | Gateway create-time / source-metadata 回调 |
| `credits_actual` | LIVE | terminal settle |
| `service_mode` | LIVE | job policy |
| `quality_tier` | LIVE | job policy（当前全部 standard） |
| `tts_provider` | LIVE | job policy |
| `tts_model` | LIVE | job policy |
| `final_cn_chars` | LIVE | Pipeline S6 完成时回写 |
| `rewrite_triggered` | LIVE | Pipeline S6 完成时回写 |
| `rewrite_count` | LIVE | Pipeline S6 完成时回写 |
| `tts_billed_chars` | LIVE_PARTIAL | TTS 层（MiMo 除外） |

---

## 3. 后端改动

### 3.1 新增端点位置

在 `gateway/credits_observability.py` 中新增 3 个端点，复用已有的 `router`（prefix `/api/admin/credits`）、`_require_admin()`、DB session。

### 3.2 `GET /api/admin/credits/cost-metrics?window=7d`

**用途**：总览卡片 + 成本校准区的核心指标

**SQL 逻辑**（runbook 里的 4 条 SQL 合并为一个端点）：

```python
# 时间窗口过滤
window_filter = Job.created_at > func.now() - text(f"INTERVAL '{window_days} days'")

# 基础统计
jobs_total = count(*)
credits_est_sum = sum(metering_snapshot->>'credits_estimated')
credits_act_sum = sum(metering_snapshot->>'credits_actual')
delta_pct = (est_sum - act_sum) / act_sum * 100

# K 值（final_cn_chars / actual_minutes）
k_avg, k_p50, k_p75, k_p90 = percentile_cont 聚合
# 只取 actual_minutes > 0 AND final_cn_chars IS NOT NULL 的 job

# Rewrite 率
rewrite_rate = count(rewrite_triggered=true) / count(metering_snapshot IS NOT NULL)
rewrite_count_avg = avg(rewrite_count)

# 模式分布
service_mode_dist = group by service_mode → {express: N, studio: M}

# TTS 覆盖率
tts_coverage = count(tts_billed_chars IS NOT NULL) / count(metering_snapshot IS NOT NULL)

# 未闭环 job 数（复用 summary 的 set-diff 逻辑）
jobs_unsettled = len(reserve_ids - settle_ids)
```

**返回**：
```json
{
  "window_days": 7,
  "jobs_total": 20,
  "credits_estimated_sum": 3200,
  "credits_actual_sum": 2850,
  "estimate_actual_delta_pct": 12.3,
  "k_actual": { "avg": 281, "p50": 275, "p75": 310, "p90": 350 },
  "rewrite_rate_pct": 86.0,
  "rewrite_count_avg": 2.3,
  "service_mode_dist": { "express": 10, "studio": 10 },
  "tts_billed_chars_coverage_pct": 85.0,
  "jobs_unsettled": 1
}
```

### 3.3 `GET /api/admin/credits/provider-breakdown?window=7d`

**用途**：成本校准区的 provider 成本结构

**SQL 逻辑**：
```python
# group by tts_provider, tts_model
# 每组：job 数、actual_minutes 总和、tts_billed_chars 总和、avg billed/min
SELECT
  metering_snapshot->>'tts_provider' AS provider,
  metering_snapshot->>'tts_model' AS model,
  COUNT(*) AS job_count,
  SUM(actual_minutes) AS total_minutes,
  SUM((metering_snapshot->>'tts_billed_chars')::int) AS total_billed_chars,
  AVG((metering_snapshot->>'tts_billed_chars')::float / NULLIF(actual_minutes, 0)) AS avg_billed_per_min
FROM jobs
WHERE created_at > NOW() - INTERVAL '7 days'
  AND metering_snapshot IS NOT NULL
GROUP BY 1, 2
ORDER BY job_count DESC;
```

**返回**：
```json
{
  "window_days": 7,
  "providers": [
    {
      "provider": "minimax",
      "model": "speech-2.8-hd",
      "job_count": 8,
      "total_minutes": 45.2,
      "total_billed_chars": 128000,
      "avg_billed_per_min": 2832
    },
    {
      "provider": "cosyvoice",
      "model": "cosyvoice-v1",
      "job_count": 12,
      "total_minutes": 38.5,
      "total_billed_chars": 95000,
      "avg_billed_per_min": 2468
    }
  ]
}
```

### 3.4 `GET /api/admin/credits/outliers?window=7d`

**用途**：异常作业区

**SQL 逻辑**：3 个子查询

```python
# 1. estimate/actual 偏差最大 Top 10
ORDER BY ABS(credits_estimated - credits_actual) DESC LIMIT 10

# 2. rewrite_count 最高 Top 10
ORDER BY (metering_snapshot->>'rewrite_count')::int DESC LIMIT 10

# 3. reserve 未闭环 jobs（复用 summary 逻辑）
reserve_ids - settle_ids

# 4. metering 缺字段 jobs（有 metering_snapshot 但缺关键字段）
WHERE metering_snapshot IS NOT NULL
  AND (metering_snapshot->>'final_cn_chars' IS NULL
    OR metering_snapshot->>'credits_actual' IS NULL)
```

**返回**：
```json
{
  "window_days": 7,
  "estimate_actual_outliers": [
    {
      "job_id": "job_abc...",
      "video_title": "...",
      "service_mode": "studio",
      "credits_estimated": 90,
      "credits_actual": 45,
      "delta": 45,
      "actual_minutes": 3.0
    }
  ],
  "rewrite_top": [
    {
      "job_id": "job_def...",
      "video_title": "...",
      "rewrite_count": 12,
      "actual_minutes": 5.2
    }
  ],
  "unsettled_jobs": ["job_xxx..."],
  "missing_fields_jobs": [
    {
      "job_id": "job_yyy...",
      "missing": ["final_cn_chars", "credits_actual"]
    }
  ]
}
```

---

## 4. 前端改动

### 4.1 新建 `frontend-next/src/app/(app)/admin/metrics/page.tsx`

**页面布局**：

```
┌──────────────────────────────────────────────────────────┐
│ V3 成本监控                     时间窗口 [7d ▼] [30d]    │
├──────────────────────────────────────────────────────────┤
│ 总览卡片（2×4 网格）                                      │
│ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐        │
│ │ 任务数   │ │预估点数  │ │实扣点数  │ │偏差率   │        │
│ │   20    │ │  3,200  │ │  2,850  │ │ +12.3% │        │
│ └─────────┘ └─────────┘ └─────────┘ └─────────┘        │
│ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐        │
│ │Rewrite% │ │K_actual │ │TTS覆盖率│ │未闭环   │        │
│ │  86.0%  │ │   281   │ │  85.0%  │ │   1    │        │
│ └─────────┘ └─────────┘ └─────────┘ └─────────┘        │
├──────────────────────────────────────────────────────────┤
│ 成本校准                                                  │
│                                                          │
│ K 值分布                                                  │
│ ┌────────────────────────────────────────┐               │
│ │ avg=281  P50=275  P75=310  P90=350    │               │
│ │ 冻结假设: K=250 → 实际偏高 12%         │               │
│ └────────────────────────────────────────┘               │
│                                                          │
│ Provider 成本结构                                         │
│ ┌──────────┬──────┬───────┬──────────┬──────────┐       │
│ │ Provider │ Jobs │ 分钟数 │ 计费字符 │ 字符/分钟│       │
│ │ minimax  │  8   │ 45.2  │ 128,000 │  2,832  │       │
│ │cosyvoice │ 12   │ 38.5  │  95,000 │  2,468  │       │
│ └──────────┴──────┴───────┴──────────┴──────────┘       │
│                                                          │
│ 模式分布                                                  │
│ Express: 10 (50%)  |  Studio: 10 (50%)                   │
│                                                          │
│ Rewrite 压力                                              │
│ 触发率: 86.0%（冻结假设 30%）  平均次数: 2.3              │
├──────────────────────────────────────────────────────────┤
│ 账本健康（复用 /api/admin/credits/summary）                │
│                                                          │
│ Bucket 汇总                                               │
│ ┌──────────┬──────┬───────┬───────┬───────┐             │
│ │ 类型     │ 数量 │ 已授  │ 余额  │ 预扣  │             │
│ │ free     │  6   │ 3,000│ 2,100│    0 │             │
│ │ trial    │  2   │   600│   150│   30 │             │
│ └──────────┴──────┴───────┴───────┴───────┘             │
│                                                          │
│ Ledger 分布: grant=12 reserve=18 capture=15 release=3    │
│ 闭环状态: 1 个未闭环 job（可能正在运行中）                  │
│ Field Status: 8 LIVE / 1 LIVE_PARTIAL                    │
│                                                          │
│ 最近 Ledger 流水                                          │
│ ┌────────┬───────┬───────┬──────────┬────────────┐      │
│ │ 方向   │ 金额  │ 余额  │ 关联Job  │ 时间        │      │
│ │capture │  -45  │  255  │ abc...  │ 04-10 15:30│      │
│ └────────┴───────┴───────┴──────────┴────────────┘      │
├──────────────────────────────────────────────────────────┤
│ 异常作业                                                  │
│                                                          │
│ 预估/实扣偏差最大                                          │
│ ┌──────────┬──────┬──────┬──────┬──────┐                │
│ │ Job ID   │ 预估 │ 实扣 │ 偏差 │ 分钟 │                │
│ │ abc...   │  90  │  45  │ +45  │ 3.0  │                │
│ └──────────┴──────┴──────┴──────┴──────┘                │
│                                                          │
│ Rewrite 次数最多                                          │
│ ┌──────────┬──────────┬──────┐                           │
│ │ Job ID   │ rewrite数│ 分钟 │                           │
│ │ def...   │    12    │ 5.2  │                           │
│ └──────────┴──────────┴──────┘                           │
│                                                          │
│ 未闭环 Jobs: job_xxx...                                   │
│ 缺字段 Jobs: job_yyy... (缺 final_cn_chars, credits_actual)│
└──────────────────────────────────────────────────────────┘
```

**实现要点**：
- `useEffect` 加载 4 个 API（summary + cost-metrics + provider-breakdown + outliers），并行 `Promise.all`
- 时间窗口 state：`window`（`"7"` | `"30"`），切换时重新加载 3 个带 window 参数的 API
- 总览卡片用 2×4 grid，每个卡片显示标签+数值+辅助说明
- 成本校准区和账本健康区用 section 分隔
- 表格用简单 `<table>` + 现有 Tailwind 样式
- 异常作业的 job_id 截取前 8 位显示，hover 显示完整
- K 值、rewrite 率旁标注冻结假设值，便于对比
- 第一版纯数字表格，不引入 recharts

### 4.2 导航入口

在 `frontend-next/src/components/app-shell.tsx` 的 admin nav group 中已有 `/admin/pricing`（定价管理），将 `/admin/metrics`（成本监控）加在其后。

---

## 5. 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `gateway/credits_observability.py` | 修改 | 新增 3 个端点：cost-metrics、provider-breakdown、outliers |
| `frontend-next/src/app/(app)/admin/metrics/page.tsx` | **新建** | V3 成本监控页 |
| `frontend-next/src/components/app-shell.tsx` | 修改 | 加 `/admin/metrics` 导航入口 |

**不改动**：`credits_service.py`、`models.py`、`admin_settings.py`、已有 summary 端点

---

## 6. 第二步（2-4 周数据后）

| 增强 | 说明 |
|------|------|
| recharts 图表 | K 值趋势折线、每日任务量柱状、Provider 成本对比柱状 |
| 更多筛选器 | service_mode / provider / quality_tier |
| Bucket 消耗率曲线 | 判断 Plus/Pro 点数是否够用 |
| 每日 estimated vs actual 折线 | 观察偏差趋势是否在收敛 |

---

## 7. 验证

1. 访问 `/admin/metrics` → 8 个卡片有数据
2. 切换时间窗口 7d → 30d → 数字变化
3. 成本校准区：K 值、Provider 表格、Rewrite 率有数据
4. 账本健康区：与 `curl /api/admin/credits/summary` 返回一致
5. 异常作业区：表格有内容（如果有偏差 job）
6. 非 admin 用户 → 403
7. 无 job 数据时 → 友好空状态提示
