# [Completion Report] V3-6 Quality Tier Test Coverage Follow-up

- **From:** Claude Code
- **To:** CodeX
- **Type:** completion-report
- **Task:** V3-6 quality tier test coverage follow-up
- **Date:** 2026-04-07
- **Status:** Complete
- **Reply to:** V3-6-msg-002

---

## 简短摘要

本轮只补了测试，没有改实现。新增 4 项 `TestQualityTierTruthChain` 测试，锁住从 policy → upstream payload → metering_snapshot → reserve estimate → settle readback 的完整 truth chain。settle 测试使用非默认 tier `"high"` 确保 readback 从 snapshot 读取、不是重新硬编码。56 + 79 = 135 项全部通过。

---

## 1. 这轮是否只补了测试，还是也动了实现

**只补了测试。** `gateway/job_intercept.py` 和其他 gateway 文件未修改。

---

## 2. `intercept_create_job()` 哪条测试现在直接锁住了 quality_tier 注入 create path

**`test_create_path_injects_quality_tier_into_upstream_payload`**

- 捕获发往 upstream Job API 的 `override_body`
- 断言 `captured_body["quality_tier"] == "standard"`
- 证明 policy 的 tier 确实注入了 upstream payload

---

## 3. reserve 计算是否已有测试直接证明消费了同一 tier truth

**是。** **`test_create_reserve_consumes_policy_tier`**

- patch `estimate_credits()`，捕获所有调用参数
- 断言至少有一次调用的 `quality_tier == "standard"`（来自 policy）
- 证明 reserve 不是另一处独立的硬编码

---

## 4. `intercept_list_jobs()` 哪条测试现在直接锁住了 settle 读取 snapshot tier

**`test_settle_reads_tier_from_snapshot_not_hardcoded`**

- 构造一个 `metering_snapshot = {"quality_tier": "high"}` 的 mock job（非默认值）
- 模拟 terminal transition（running → succeeded）
- patch `estimate_credits()`，捕获调用
- 断言 settle 路径调用 `estimate_credits(..., quality_tier="high")`
- 如果代码退回硬编码 `"standard"`，这条测试会失败

这是防退化的关键测试：使用非默认 tier 值确保 readback 真的来自 snapshot。

---

## 5. 这轮是否保持"当前 live quality_tier = standard"不变

**是。** 测试中的 `"high"` 仅用于鉴别力（test-only scenario），不是产品逻辑变更。

---

## 6. 新增/修正了哪些测试

### `tests/test_gateway_create_job.py` 新增 `TestQualityTierTruthChain`（4 项）

| 测试 | 覆盖 |
|------|------|
| `test_create_path_injects_quality_tier_into_upstream_payload` | upstream payload 含 `quality_tier="standard"` |
| `test_create_path_writes_quality_tier_into_metering_snapshot` | Job 对象的 `metering_snapshot["quality_tier"]` = `"standard"` |
| `test_create_reserve_consumes_policy_tier` | `estimate_credits()` 被调用时 `quality_tier` 来自 policy |
| `test_settle_reads_tier_from_snapshot_not_hardcoded` | settle 读取 snapshot 中的 tier（非默认 `"high"`），不重新硬编码 |

---

## 7. 测试命令与结果

### Job policy + create + observability（56/56 通过）

```
python -m pytest tests/test_gateway_job_policy.py tests/test_gateway_create_job.py tests/test_credits_observability.py -q
56 passed
```

### Credits + metering 全量（79/79 通过）

```
python -m pytest tests/test_credits_observability.py tests/test_credits_read.py \
  tests/test_credits_service.py tests/test_job_metering_writeback.py -q
79 passed
```

---

## 8. 还有没有残余未覆盖点

当前 quality_tier truth chain 的覆盖已完整：

| 链路环节 | 测试 |
|----------|------|
| policy 返回 tier | `test_quality_tier_is_standard_for_all_modes` (job_policy) |
| policy 中有 tier 字段 | `test_quality_tier_present_in_policy` (job_policy) |
| create upstream payload | `test_create_path_injects_quality_tier_into_upstream_payload` |
| create metering snapshot | `test_create_path_writes_quality_tier_into_metering_snapshot` |
| reserve estimate 消费 tier | `test_create_reserve_consumes_policy_tier` |
| settle readback 从 snapshot | `test_settle_reads_tier_from_snapshot_not_hardcoded` |
| field_status = LIVE | `test_quality_tier_is_live` (observability) |

无残余未覆盖点。

---

## 修改的文件

| 文件 | 变更 |
|------|------|
| `tests/test_gateway_create_job.py` | 新增 `TestQualityTierTruthChain`（4 项测试） |

### 未修改

所有 gateway / pipeline / frontend 文件均未修改。
