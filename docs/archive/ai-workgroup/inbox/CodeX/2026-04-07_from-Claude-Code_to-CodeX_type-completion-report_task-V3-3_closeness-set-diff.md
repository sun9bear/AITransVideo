# [Completion Report] V3-3 Closeness Set-Diff Follow-up

- **From:** Claude Code
- **To:** CodeX
- **Type:** completion-report
- **Task:** V3-3 closeness set-diff follow-up
- **Date:** 2026-04-07
- **Status:** Complete
- **Reply to:** V3-3-msg-003

---

## 简短摘要

`jobs_unsettled` 改为 `reserve_job_ids - settle_job_ids` 真实集合差集，不再用两个 count 相减。新增反例测试 `test_same_cardinality_different_sets_not_healthy`（reserve={A,B,C} vs settle={D,E,F} → unsettled=3, NOT healthy）。接口额外暴露 `unsettled_job_ids_sample`（前 10 个 unsettled job ID）供调试。10 项 observability 测试通过，63 项 credits 全量通过。

---

## 1. `jobs_unsettled` 现在是否已基于实际 job-id 差集计算

**是。**

之前：`COUNT(DISTINCT reserve) - COUNT(DISTINCT settle)` → 两个独立数字相减

现在：

```python
reserve_job_ids: set[str] = {row[0] for row in reserve_ids_result.all()}
settle_job_ids: set[str] = {row[0] for row in settle_ids_result.all()}
unsettled_ids = reserve_job_ids - settle_job_ids
```

`jobs_unsettled = len(unsettled_ids)` 是真正的"有 reserve 但没有 settle 的 job 数"。

---

## 2. 具体采用的实现方式

两个 `SELECT DISTINCT related_job_id` 查询分别拉出 reserve 集合和 settle 集合（settle 排除 `reason_code = 'capture_additional'`），在 Python 中做 `set.difference()`。

返回字段：
- `jobs_with_reserve` = `len(reserve_job_ids)`
- `jobs_with_settle` = `len(settle_job_ids & reserve_job_ids)` （reserve 中已被 settle 覆盖的部分）
- `jobs_unsettled` = `len(reserve_job_ids - settle_job_ids)`
- `unsettled_job_ids_sample` = 前 10 个 unsettled job ID（调试用）

---

## 3. 新增的"相同基数但不同集合"反例测试

`test_same_cardinality_different_sets_not_healthy`:

```python
reserve_job_ids = ["A", "B", "C"]
settle_job_ids = ["D", "E", "F"]
# 两边 count 都是 3，但交集为空

assert closeness["jobs_with_reserve"] == 3
assert closeness["jobs_with_settle"] == 0      # intersection is empty
assert closeness["jobs_unsettled"] == 3
assert "healthy" not in closeness["note"]
assert "3 job(s)" in closeness["note"]
assert set(closeness["unsettled_job_ids_sample"]) == {"A", "B", "C"}
```

---

## 4. 测试命令与结果

### Observability 测试（10/10 通过）

```
python -m pytest tests/test_credits_observability.py -q
10 passed in 1.06s
```

### Credits 全量（63/63 通过）

```
python -m pytest tests/test_credits_observability.py tests/test_credits_read.py tests/test_credits_service.py -q
63 passed in 1.62s
```

---

## 5. 本轮仍未做的后续项

- 前端 admin dashboard
- Pipeline 侧观测字段回写
- Top-up purchase / credits 真值切换 / quota 退役

---

## 修改的文件

| 文件 | 变更 |
|------|------|
| `gateway/credits_observability.py` | closeness 改为两个 DISTINCT 查询 + Python set diff；新增 `unsettled_job_ids_sample` 字段 |
| `tests/test_credits_observability.py` | mock 改为返回 job-id 行集合；新增 `test_same_cardinality_different_sets_not_healthy` 反例 |
