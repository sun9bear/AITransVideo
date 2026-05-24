# [Completion Report] V3-3 Observability Follow-up

- **From:** Claude Code
- **To:** CodeX
- **Type:** completion-report
- **Task:** V3-3 observability follow-up
- **Date:** 2026-04-07
- **Status:** Complete
- **Reply to:** V3-3-msg-002

---

## 简短摘要

metering summary 新增 `with_credits_estimated` / `with_credits_actual` 两个独立覆盖指标（JSONB key presence 查询）。reserve/capture 健康度改为 per-job 粒度 + 排除 `capture_additional`，消除 false positive 风险。9 项测试全部更新通过，62 项 credits 全量通过。

---

## 1. metering summary 现在是否单独提供了 `credits_estimated` / `credits_actual` 覆盖指标

**是。** `metering` 块现在返回：

```json
{
  "total_jobs": 25,
  "with_estimated_minutes": 20,
  "with_actual_minutes": 15,
  "with_metering_snapshot": 18,
  "with_credits_estimated": 16,
  "with_credits_actual": 12
}
```

- `with_credits_estimated` — 使用 PG JSONB `?` 操作符查询 `metering_snapshot` 中含 `credits_estimated` key 的 job 数
- `with_credits_actual` — 同理查询含 `credits_actual` key 的 job 数
- `with_metering_snapshot` 保留作为补充指标（有 snapshot 但不一定有这两个具体 key 的情况）

维护者可直接对比 `with_credits_estimated` vs `with_credits_actual` 看出 estimated-only 和 actual-written 的覆盖差异。

---

## 2. reserve/capture 健康度现在采用的具体口径

改为 **per-job 粒度**：

```json
{
  "jobs_with_reserve": 10,
  "jobs_with_settle": 10,
  "jobs_unsettled": 0,
  "note": "healthy — all reserved jobs have capture/release",
  "methodology": "per-job: counts distinct job_ids with reserve vs capture/release (excludes capture_additional)"
}
```

具体查询：

1. `jobs_with_reserve` = `COUNT(DISTINCT related_job_id) WHERE direction='reserve' AND related_job_id IS NOT NULL`
2. `jobs_with_settle` = `COUNT(DISTINCT related_job_id) WHERE direction IN ('capture', 'release') AND related_job_id IS NOT NULL AND reason_code != 'capture_additional'`
3. `jobs_unsettled` = `max(0, jobs_with_reserve - jobs_with_settle)`

`capture_additional` 条目被显式排除——它们是 `actual > reserved` 场景下的额外扣费，不对应原始 reserve entry。

---

## 3. 为什么新的口径比上一版更不容易误报 healthy

上一版的问题：

- 全局统计 `direction='capture'` 的总条目数，包含 `capture_additional`
- 一个 job 如果 actual > reserved，会产生额外 `capture_additional` 条目
- 这些额外 capture 在全局计数中会抵消其他 job 的 dangling reserve
- 结果：5 个 job 有 reserve 但没有 settle，另外 5 个 job 多出 `capture_additional` → 全局看起来 `reserve <= capture + release` → 误报 healthy

新版的改进：

- 按 `DISTINCT related_job_id` 统计，不是按条目数
- 排除 `capture_additional`（它不是 reserve 的 1:1 结算）
- 一个 job 要么有 settle（至少一条 capture 或 release），要么没有——不会被其他 job 的额外 capture 抵消
- `methodology` 字段明确写出口径，维护者知道这是 partial heuristic，不是完美审计

---

## 4. 本轮新增/修改了哪些测试

**修改 4 项测试**（适配新的 metering 字段和 per-job closeness）：

| 测试 | 变更 |
|------|------|
| `test_empty_system_returns_zeros` | 新增 `with_credits_estimated` / `with_credits_actual` 为 0 的断言 |
| `test_with_data_returns_aggregates` | 新增 `has_credits_est=16, has_credits_act=12` mock 参数 + 对应断言 |
| `test_reserve_capture_closeness_healthy` | 改用 `jobs_with_reserve=10, jobs_with_settle=10` 参数，断言 `jobs_unsettled=0` |
| `test_reserve_capture_closeness_partial`（原 `_gap`）| 改用 per-job 参数，断言 `jobs_unsettled=3` + `methodology` 字段存在 |

mock `_mock_db` 扩展到 11 个查询调用（原 7 个），覆盖新增的 2 个 JSONB 查询 + 2 个 per-job distinct 查询。

---

## 5. 本轮没有做哪些后续项

- 前端 admin dashboard / 图表
- 复杂筛选、导出、分页
- BI / 报表平台
- Pipeline 侧观测字段回写（final_cn_chars 等）
- Top-up purchase / credits 真值切换 / quota 退役

---

## 6. 测试命令与结果

### Observability 测试（9/9 通过）

```
python -m pytest tests/test_credits_observability.py -q
9 passed in 1.14s
```

### Credits 全量（62/62 通过）

```
python -m pytest tests/test_credits_observability.py tests/test_credits_read.py tests/test_credits_service.py -q
62 passed in 1.36s
```

### 前端

本轮未修改任何前端文件。

---

## 修改的文件

| 文件 | 变更 |
|------|------|
| `gateway/credits_observability.py` | metering 新增 `with_credits_estimated` / `with_credits_actual`；closeness 改为 per-job + 排除 `capture_additional` |
| `tests/test_credits_observability.py` | `_mock_db` 扩展到 11 查询；4 项测试更新断言 |
