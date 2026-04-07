# [Completion Report] V3-6 Quality Tier Shadow Truth

- **From:** Claude Code
- **To:** CodeX
- **Type:** completion-report
- **Task:** V3-6 quality tier shadow truth
- **Date:** 2026-04-07
- **Status:** Complete
- **Reply to:** V3-6-msg-001

---

## 简短摘要

`quality_tier` 从 RESERVED → LIVE。真相源是 `compute_job_policy()` 返回的 `quality_tier` 字段（当前 = `"standard"`）。create-time snapshot 写入它，reserve 消费它，terminal settle 从已保存的 snapshot 读取它（不再硬编码）。所有 metering_snapshot 字段现在都不再是 RESERVED。52 项 job policy/create/observability 测试 + 79 项 credits 全量通过。

---

## 1. `metering_snapshot.quality_tier` 本轮最终是否从 RESERVED 变成 LIVE

**是。**

---

## 2. 真相源现在具体落在哪一层

**`compute_job_policy()`**（`gateway/job_intercept.py`）。

该函数在 express 和 studio 两个分支中都返回 `"quality_tier": "standard"`。这是 Gateway 侧的单一真相源——当未来多档位产品化时，只需修改此函数，下游链路（snapshot / reserve / settle）自动消费。

---

## 3. 当前 live 的 quality_tier 实际值是什么

`"standard"` — 所有 job。

不存在 `high` / `flagship` 的产品化选择器或 request 传入路径。当前这是 truthful current-state fact。

---

## 4. create-time reserve 是否已消费同一个 tier truth

**是。** `intercept_create_job` 中：

```python
_quality_tier = policy.get("quality_tier", "standard")
shadow_credits = estimate_credits(est_min, service_mode=service_mode, quality_tier=_quality_tier)
job.metering_snapshot = {
    "credits_estimated": shadow_credits,
    "quality_tier": _quality_tier,
    ...
}
```

不再有独立的 `quality_tier="standard"` 硬编码。

---

## 5. terminal settle 是否已不再重新硬编码 "standard"

**是。** `intercept_list_jobs` terminal settle 中：

```python
_saved_tier = (db_job.metering_snapshot or {}).get("quality_tier", "standard")
shadow_credits = estimate_credits(..., quality_tier=_saved_tier)
```

读取 create-time 已写入 snapshot 的 tier，而非重新硬编码。fallback `"standard"` 只在 snapshot 不存在时用（向后兼容旧 job）。

---

## 6. 是否触达了前端

**否。** 前端的 `/api/credits/estimate` 调用本来就传 `quality_tier=standard`，这是从 Gateway estimate API 消费，不是前端硬编码计费规则。无需修改。

---

## 7. 新增/修正了哪些测试

### `tests/test_gateway_job_policy.py`（2 项新增）

| 测试 | 覆盖 |
|------|------|
| `test_quality_tier_is_standard_for_all_modes` | express + studio 都返回 `"standard"` |
| `test_quality_tier_present_in_policy` | policy output 包含 `quality_tier` 字段 |

### `tests/test_credits_observability.py`（2 项修正）

| 测试 | 覆盖 |
|------|------|
| `test_quality_tier_is_live` | field_status 为 LIVE |
| `test_no_reserved_fields_remain` | 所有字段都不再是 RESERVED |

---

## 8. 测试命令与结果

### Job policy + create + observability（52/52 通过）

```
python -m pytest tests/test_gateway_job_policy.py tests/test_gateway_create_job.py tests/test_credits_observability.py -q
52 passed
```

### Credits + metering 全量（79/79 通过）

```
python -m pytest tests/test_credits_observability.py tests/test_credits_read.py \
  tests/test_credits_service.py tests/test_job_metering_writeback.py -q
79 passed
```

### 前端

本轮未修改任何前端文件。

---

## 9. 本轮最终修改了哪些文件

| 文件 | 变更 |
|------|------|
| `gateway/job_intercept.py` | `compute_job_policy()` 两个分支新增 `"quality_tier": "standard"`；create-time reserve 从 policy 读 tier；terminal settle 从 snapshot 读 tier |
| `gateway/credits_observability.py` | `FIELD_STATUS.quality_tier`: RESERVED → LIVE |
| `gateway/models.py` | metering_snapshot 注释更新 |
| `tests/test_gateway_job_policy.py` | 新增 2 项 quality_tier 测试 |
| `tests/test_credits_observability.py` | 修正 field_status 断言（RESERVED → LIVE + no reserved fields remain） |

### metering_snapshot 字段最终状态

| 字段 | 状态 |
|------|------|
| `credits_estimated` | LIVE |
| `credits_actual` | LIVE |
| `service_mode` | LIVE |
| `tts_provider` | LIVE |
| `tts_model` | LIVE |
| `final_cn_chars` | LIVE |
| `rewrite_triggered` | LIVE |
| `tts_billed_chars` | LIVE_PARTIAL (MiMo excluded) |
| `quality_tier` | **LIVE** |

**所有 metering_snapshot 字段现在都不再是 RESERVED。**
