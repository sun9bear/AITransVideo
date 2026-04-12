# 方案三：V3 Shadow Credits 校准面板

> 日期：2026-04-10
> 状态：已实施（2026-04-12 commit 76b0f44）

---

## 1. 目标

新建管理员后台 `/admin/credits-monitor` 页面，将 V3 pilot 期间手工查库的 shadow credits 校准工作自动化，回答三个核心问题：

1. **数据健康吗** — shadow bucket/ledger/metering 是否在正常写入、闭环
2. **定价锚点合理吗** — K 值、rewrite 率、预估/实扣偏差、job 默认 provider 分布
3. **哪里在吞毛利** — 偏差最大的 job、rewrite 最多的 job、未闭环的 job

当前这些数据要么靠 `curl /api/admin/credits/summary` 肉眼看 JSON，要么靠 runbook 里的 4 条 SQL 手工查库。本方案把它们变成一个可视化页面。

### 1.1 监控范围边界

> **本页仅覆盖 Gateway 内部 shadow metering / credits 数据。**
>
> 以下成本维度**不在本页覆盖范围**内，需从外部系统手工采集（见 runbook §5.3）：
> - 外部 TTS 账单（MiniMax / 阿里云百炼 / 火山引擎控制台）
> - LLM token 消耗（Gemini / DeepSeek 控制台）
> - 云资源 / 服务器成本
>
> 本页数据适合用于：校准冻结参数（K 值、rewrite 率）、验证 shadow 闭环、发现异常 job。
> **不适合**直接作为完整单位成本核算依据。

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

**公共 helper**（新增）：

```python
# 时间窗口：Python 侧计算，避免 SQL 注入
def _parse_window(window: str = "7") -> tuple[int, datetime]:
    """Parse window query param → (days, cutoff datetime). Range: 1-90."""
    days = max(1, min(90, int(window)))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return days, cutoff

# JSONB 安全取值：防止空字符串 cast 报错
def _safe_jsonb_float(col, key):
    """NULLIF(col->>'key', '')::float — 空值/空字符串返回 NULL 而非报错"""
    return cast(func.nullif(col.op("->>")(key), ""), Float)

def _safe_jsonb_int(col, key):
    return cast(func.nullif(col.op("->>")(key), ""), Integer)

# 未闭环 job 查询（summary / cost-metrics / outliers 共用）
async def _get_unsettled_job_ids(db: AsyncSession) -> set[str]:
    """reserve 有记录但 capture/release 无记录的 job_id 集合"""
    reserve_result = await db.execute(
        select(func.distinct(CreditsLedger.related_job_id)).where(
            CreditsLedger.direction == "reserve",
            CreditsLedger.related_job_id.isnot(None),
        )
    )
    reserve_ids = {r[0] for r in reserve_result.all()}
    settle_result = await db.execute(
        select(func.distinct(CreditsLedger.related_job_id)).where(
            CreditsLedger.direction.in_(["capture", "release"]),
            CreditsLedger.related_job_id.isnot(None),
            CreditsLedger.reason_code != "capture_additional",
        )
    )
    settle_ids = {r[0] for r in settle_result.all()}
    return reserve_ids - settle_ids
```

> **同步重构**：`/summary` 端点的 set-diff 逻辑改为调用 `_get_unsettled_job_ids()`。

### 3.2 `GET /api/admin/credits/cost-metrics?window=7`

**用途**：总览卡片 + 成本校准区的核心指标

**参数**：`window`（整数天数，默认 7，范围 1-90）

**SQL 逻辑**（runbook 里的 4 条 SQL 合并为一个端点）：

```python
days, cutoff = _parse_window(window)
window_filter = Job.created_at > cutoff

# 基础统计
jobs_total = count(*)
credits_est_sum = sum(_safe_jsonb_int(metering_snapshot, 'credits_estimated'))
credits_act_sum = sum(_safe_jsonb_int(metering_snapshot, 'credits_actual'))
delta_pct = (est_sum - act_sum) / act_sum * 100

# K 值（final_cn_chars / actual_minutes）
k_avg, k_p50, k_p75, k_p90 = percentile_cont 聚合
# 只取 actual_minutes > 0 AND final_cn_chars IS NOT NULL 的 job
# 使用 _safe_jsonb_float() 取 final_cn_chars

# Rewrite 率
rewrite_rate = count(rewrite_triggered=true) / count(metering_snapshot IS NOT NULL)
rewrite_count_avg = avg(_safe_jsonb_int(metering_snapshot, 'rewrite_count'))

# 模式分布（使用 Job.service_mode 顶级列，有索引）
service_mode_dist = group by Job.service_mode → {express: N, studio: M}

# TTS 覆盖率
tts_coverage = count(tts_billed_chars IS NOT NULL) / count(metering_snapshot IS NOT NULL)

# 未闭环 job 数（复用公共 helper）
unsettled = await _get_unsettled_job_ids(db)
jobs_unsettled = len(unsettled)
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

### 3.3 `GET /api/admin/credits/provider-breakdown?window=7`

**用途**：成本校准区的 **job 默认** provider 分布

> **真值边界**：此处 `tts_provider` / `tts_model` 是 job 创建时的默认策略快照，
> 不是实际执行时每个 speaker 的 provider。Studio 模式下用户可在 voice selection
> 阶段为不同 speaker 选择不同引擎（三引擎混用），此时 job 级 provider 不等于
> 实际执行 provider 组合。
>
> V1 先按 job 默认 provider 聚合，页面标注"按 job 默认引擎"。
> 第二步若需精确到 per-speaker 执行 provider，需先在 Pipeline 层补充
> 实际执行 provider 聚合写回 `metering_snapshot`。

**SQL 逻辑**：
```python
days, cutoff = _parse_window(window)

# 使用 Job.tts_provider / Job.tts_model 顶级列（有索引，比 JSONB 取值快）
# 注意：这是 job 创建时的默认 provider，非实际执行 provider
# tts_billed_chars 仍从 metering_snapshot 取（无顶级列）
SELECT
  tts_provider AS provider,
  tts_model AS model,
  COUNT(*) AS job_count,
  SUM(actual_minutes) AS total_minutes,
  SUM(NULLIF(metering_snapshot->>'tts_billed_chars', '')::int) AS total_billed_chars,
  AVG(NULLIF(metering_snapshot->>'tts_billed_chars', '')::float / NULLIF(actual_minutes, 0)) AS avg_billed_per_min,
  AVG(NULLIF(metering_snapshot->>'credits_actual', '')::float / NULLIF(actual_minutes, 0)) AS avg_credits_per_min
FROM jobs
WHERE created_at > cutoff
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
      "avg_billed_per_min": 2832,
      "avg_credits_per_min": 14.5
    },
    {
      "provider": "cosyvoice",
      "model": "cosyvoice-v1",
      "job_count": 12,
      "total_minutes": 38.5,
      "total_billed_chars": 95000,
      "avg_billed_per_min": 2468,
      "avg_credits_per_min": 12.1
    }
  ]
}
```

### 3.4 `GET /api/admin/credits/outliers?window=7`

**用途**：异常作业区

**SQL 逻辑**：4 个子查询，JSONB 取值统一用 `_safe_jsonb_*` helper

```python
days, cutoff = _parse_window(window)

# 1. estimate/actual 偏差最大 Top 10
# 使用 _safe_jsonb_int 取 credits_estimated / credits_actual
ORDER BY ABS(credits_estimated - credits_actual) DESC LIMIT 10

# 2. rewrite_count 最高 Top 10
ORDER BY _safe_jsonb_int(metering_snapshot, 'rewrite_count') DESC NULLS LAST LIMIT 10

# 3. reserve 未闭环 jobs（复用公共 helper）
unsettled = await _get_unsettled_job_ids(db)

# 4. metering 缺字段 jobs（有 metering_snapshot 但缺关键字段）
WHERE metering_snapshot IS NOT NULL
  AND (metering_snapshot->>'final_cn_chars' IS NULL
    OR metering_snapshot->>'credits_actual' IS NULL)
```

**返回**（`video_title` → `title`，与 `Job.title` 对齐）：
```json
{
  "window_days": 7,
  "estimate_actual_outliers": [
    {
      "job_id": "job_abc...",
      "title": "...",
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
      "title": "...",
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

### 4.1 新建 `frontend-next/src/app/(app)/admin/credits-monitor/page.tsx`

**页面布局**：

```
┌──────────────────────────────────────────────────────────┐
│ Shadow Credits 校准              时间窗口 [7d ▼] [30d]    │
│ ┌──────────────────────────────────────────────────────┐ │
│ │ ℹ️ 仅覆盖内部 shadow metering / credits 数据，       │ │
│ │    不含外部 TTS 账单、LLM token、云资源成本。         │ │
│ └──────────────────────────────────────────────────────┘ │
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
│ Provider 分布（按 job 默认引擎）                            │
│ ┌──────────┬──────┬───────┬──────────┬──────────┬────────┐│
│ │ Provider │ Jobs │ 分钟数 │ 计费字符 │ 字符/分钟│点数/分钟││
│ │ minimax  │  8   │ 45.2  │ 128,000 │  2,832  │ 14.5  ││
│ │cosyvoice │ 12   │ 38.5  │  95,000 │  2,468  │ 12.1  ││
│ └──────────┴──────┴───────┴──────────┴──────────┴────────┘│
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
- `useEffect` 加载 4 个 API（summary + cost-metrics + provider-breakdown + outliers），并行 `Promise.allSettled`
  - 各区域独立 loading / error 状态，某个 API 失败不阻塞其他区域渲染
- 时间窗口 state：`window`（`"7"` | `"30"`），切换时重新加载 3 个带 window 参数的 API
- 总览卡片用 2×4 grid，每个卡片显示标签+数值+辅助说明
- 成本校准区和账本健康区用 section 分隔
- 表格用简单 `<table>` + 现有 Tailwind 样式
- 异常作业的 job_id 截取前 8 位显示，hover 显示完整
- K 值、rewrite 率旁标注冻结假设值，便于对比
- 第一版纯数字表格，不引入 recharts

### 4.2 导航入口

在 `frontend-next/src/components/app-shell.tsx` 的 admin nav group 中已有 `/admin/pricing`（定价管理），将 `/admin/credits-monitor`（点数校准）加在其后。图标使用 `TrendingUp`（lucide-react，需新增 import），与已使用的 `Activity`（审校监控）区分。

---

## 5. 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `gateway/credits_observability.py` | 修改 | 新增 3 个端点：cost-metrics、provider-breakdown、outliers |
| `frontend-next/src/app/(app)/admin/credits-monitor/page.tsx` | **新建** | Shadow Credits 校准页 |
| `frontend-next/src/components/app-shell.tsx` | 修改 | 加 `/admin/credits-monitor` 导航入口 + import `TrendingUp` |

**不改动**：`credits_service.py`、`models.py`、`admin_settings.py`

**同步小修**：
- `credits_observability.py` 的 `FIELD_STATUS` 补充 `rewrite_count` 条目（当前缺失，实际已 LIVE）
- `/summary` 端点的 reserve-capture set-diff 改为调用 `_get_unsettled_job_ids()` helper

---

## 6. 第二步（2-4 周数据后）

| 增强 | 说明 |
|------|------|
| per-speaker 执行 provider 写回 | Pipeline TTS 层记录每个 speaker 实际使用的 provider/model → 聚合写回 `metering_snapshot.tts_execution_providers`，provider-breakdown 改为按实际执行统计 |
| recharts 图表 | K 值趋势折线、每日任务量柱状、Provider 成本对比柱状 |
| 更多筛选器 | service_mode / provider / quality_tier |
| Bucket 消耗率曲线 | 判断 Plus/Pro 点数是否够用 |
| 每日 estimated vs actual 折线 | 观察偏差趋势是否在收敛 |

---

## 7. 验证

1. 访问 `/admin/credits-monitor` → 8 个卡片有数据
2. 页面顶部 scope banner 可见
3. 切换时间窗口 7d → 30d → 数字变化
4. 成本校准区：K 值、Provider 表格（标注"按 job 默认引擎"）、Rewrite 率有数据
5. 账本健康区：与 `curl /api/admin/credits/summary` 返回一致
6. 异常作业区：表格有内容（如果有偏差 job）
7. 非 admin 用户 → 403
8. 无 job 数据时 → 友好空状态提示
9. 某个 API 故意返回错误 → 对应区域显示 error，其他区域正常渲染

---

## 8. Review 修订记录（2026-04-10）

| # | 问题 | 修订 |
|---|------|------|
| 1 | `text(f"INTERVAL '{days} days'")` SQL 注入风险 | 改为 Python `timedelta` 计算 cutoff，新增 `_parse_window()` helper |
| 2 | 返回字段 `video_title` 不存在（Job 表列名是 `title`） | 统一用 `title` |
| 3 | 未闭环 set-diff 逻辑在 summary/cost-metrics/outliers 重复 3 次 | 抽取 `_get_unsettled_job_ids()` 公共 helper |
| 4 | JSONB `::int` / `::float` 遇空字符串会报错 | 新增 `_safe_jsonb_float()` / `_safe_jsonb_int()` 统一 `NULLIF` |
| 5 | provider-breakdown 用 JSONB 取 tts_provider/tts_model | 改用 `Job.tts_provider` / `Job.tts_model` 顶级列（有索引） |
| 6 | window 参数无校验 | 只接受整数天数 1-90，默认 7 |
| 7 | `FIELD_STATUS` 缺 `rewrite_count` | 本次同步补充 |
| 8 | `Promise.all` 一个失败全部不显示 | 改为 `Promise.allSettled`，各区域独立 loading/error |
| 9 | 导航图标与审校监控的 `Activity` 冲突 | 改用 `TrendingUp` |
| 10 | provider-breakdown 缺每分钟点数消耗 | 新增 `avg_credits_per_min` 列，直接对比 DEBIT_RATES |
| 11 | provider-breakdown 用 job 默认 provider 但页面暗示是实际执行成本（Codex P1） | V1 明确标注"按 job 默认引擎"；§3.3 加真值边界说明；第二步规划 per-speaker 执行 provider 写回 |
| 12 | 页面标题"成本监控"超出实际可观测范围（Codex P2） | 标题改为"Shadow Credits 校准"；页面顶部加 scope banner 说明不含外部 TTS/LLM/云成本；§1 新增监控范围边界 |
| 13 | 路由 `/admin/metrics` 太泛，未来易冲突 | 改为 `/admin/credits-monitor` |
| 14 | 未闭环指标不受时间窗口过滤，7 天视图混入历史数据（Codex 实施审核 P1） | `_get_unsettled_job_ids()` 新增可选 `cutoff` 参数；cost-metrics/outliers 传 cutoff；summary 保持全量 |
| 15 | 新增 3 个端点无测试覆盖（Codex 实施审核 P2） | 新增 20 个测试：_parse_window 6 + cost-metrics 6 + provider-breakdown 4 + outliers 4，32 passed |
| 16 | 前端 Summary 类型定义与后端返回结构完全不匹配（Opus 审核） | 重写全部 Summary 相关类型和渲染逻辑，对齐后端 /summary 实际返回结构 |
