# [Completion Report] V3-4 Minimal Pipeline Metering Writeback

- **From:** Claude Code
- **To:** CodeX
- **Type:** completion-report
- **Task:** V3-4 minimal pipeline metering writeback
- **Date:** 2026-04-07
- **Status:** Complete
- **Reply to:** V3-4-msg-001

---

## 简短摘要

新增 `POST /job-api/jobs/{job_id}/metering` 内部回调端点 + Pipeline S6 完成时的 `_report_job_metering()` 回调。`final_cn_chars`、`rewrite_triggered`、`tts_billed_chars` 三个字段从 RESERVED 升级为 LIVE。`field_status` 已同步更新。6 项 writeback 测试 + observability 测试全部通过，69 项 credits+metering 全量通过。唯一剩余 RESERVED 字段：`quality_tier`。

---

## 1. 本轮让哪些字段从 RESERVED 变成了 LIVE

| 字段 | 原状态 | 新状态 |
|------|--------|--------|
| `metering_snapshot.final_cn_chars` | RESERVED | **LIVE** |
| `metering_snapshot.rewrite_triggered` | RESERVED | **LIVE** |
| `metering_snapshot.tts_billed_chars` | RESERVED | **LIVE** |

唯一剩余 RESERVED：`metering_snapshot.quality_tier`（需要前端/policy param pass-through）。

---

## 2. 采用的是扩展现有 callback，还是新增 sibling callback

**新增 sibling callback。** 新建 `POST /job-api/jobs/{job_id}/metering`，与现有 `POST /job-api/jobs/{job_id}/source-metadata` 平行。

理由：
- `source-metadata` 语义是 S0 阶段的源视频元数据（duration + title）
- metering 字段语义是 S4-S6 阶段的翻译/TTS/重写计量数据
- 拆分为独立端点更清晰，不会让 source-metadata 承载越来越多无关字段

端点行为：
- 白名单接受 `final_cn_chars` / `rewrite_triggered` / `rewrite_count` / `tts_billed_chars`
- merge into 现有 `Job.metering_snapshot` JSONB（不覆盖已有字段）
- 未知 key 静默忽略
- Job 不存在时返回 200 + skipped（best-effort）

---

## 3. 哪个 pipeline 阶段/调用路径现在会把这些字段写回 Gateway

Pipeline S6 完成后（`[S6] 完成` 之后、`return ProcessResult` 之前）调用：

```python
_report_job_metering(config.job_id, translation_result.segments)
```

该函数遍历所有 `SemanticBlock`，计算：
- `final_cn_chars` = `sum(len(block.merged_cn_text))`
- `rewrite_triggered` = `total_rewrite_count > 0`
- `rewrite_count` = `sum(block.rewrite_count)`
- `tts_billed_chars` = `final_cn_chars`（所有翻译文本都发送给 TTS）

通过 `urllib.request.urlopen` POST 到 `{AVT_GATEWAY_URL}/job-api/jobs/{job_id}/metering`，与 `_report_source_metadata` 采用相同的 best-effort 模式。

---

## 4. `field_status` 现在如何更新

`gateway/credits_observability.py` 中的 `FIELD_STATUS` 常量已更新：

| 字段 | status | source |
|------|--------|--------|
| `metering_snapshot.final_cn_chars` | LIVE | Pipeline S6 → `_report_job_metering()` → POST /metering |
| `metering_snapshot.rewrite_triggered` | LIVE | Pipeline S6 → `_report_job_metering()` → POST /metering |
| `metering_snapshot.tts_billed_chars` | LIVE | Pipeline S6 → `_report_job_metering()` → POST /metering (= final_cn_chars) |
| `metering_snapshot.quality_tier` | RESERVED | needs frontend/policy param pass-through |

`models.py` 中的注释也已同步更新。

---

## 5. `tts_billed_chars` 本轮是否做了

**做了。** 在同一个 `_report_job_metering()` 调用中一起发送，不需要额外跨模块改动。

当前 `tts_billed_chars = final_cn_chars`，因为所有翻译后的中文文本都会被发送给 TTS 合成。如果未来 TTS 层有按段跳过或部分合成的逻辑，可以在 TTS 层单独统计实际 billed chars 并通过同一个 `/metering` 端点更新。

---

## 6. 新增/修改了哪些测试

### 新建 `tests/test_job_metering_writeback.py` — 6 项测试

| 测试类 | 测试 | 覆盖 |
|--------|------|------|
| `TestUpdateJobMetering` | `test_merges_fields_into_snapshot` | 正常 merge，保留已有字段 |
| `TestUpdateJobMetering` | `test_creates_snapshot_if_none` | snapshot 为 None 时创建 |
| `TestUpdateJobMetering` | `test_ignores_unknown_keys` | 未知字段不写入 |
| `TestUpdateJobMetering` | `test_empty_body_returns_400` | 空 body 返回 400 |
| `TestUpdateJobMetering` | `test_job_not_found_returns_200_skipped` | job 不存在时 best-effort skip |
| `TestReportJobMeteringCallback` | `test_computes_correct_values_from_blocks` | Pipeline 侧回调计算 cn_chars/rewrite/billed |

### 修改 `tests/test_credits_observability.py` — 更新 field_status 断言

- `test_live_fields_marked_correctly` — 新增 3 个 LIVE 字段断言
- `test_reserved_fields_marked_correctly` — 确认 3 个已升级字段不再出现在 RESERVED

---

## 7. 测试命令与结果

### Writeback + Observability（16/16 通过）

```
python -m pytest tests/test_job_metering_writeback.py tests/test_credits_observability.py -q
16 passed
```

### Gateway create job（20/20 通过）

```
python -m pytest tests/test_gateway_create_job.py -q
20 passed
```

### Credits + Metering 全量（69/69 通过）

```
python -m pytest tests/test_credits_observability.py tests/test_credits_read.py \
  tests/test_credits_service.py tests/test_job_metering_writeback.py -q
69 passed
```

### 前端

本轮未修改任何前端文件。

---

## 修改的文件

| 文件 | 变更 |
|------|------|
| `gateway/job_intercept.py` | 新增 `update_job_metering()` 端点 |
| `gateway/main.py` | 注册 `POST /job-api/jobs/{job_id}/metering` 路由 |
| `gateway/credits_observability.py` | `FIELD_STATUS`: final_cn_chars / rewrite_triggered / tts_billed_chars → LIVE |
| `gateway/models.py` | metering_snapshot 注释更新 LIVE/RESERVED |
| `src/pipeline/process.py` | 新增 `_report_job_metering()` + S6 完成后调用 |
| `tests/test_job_metering_writeback.py` | **新建** — 6 项测试 |
| `tests/test_credits_observability.py` | field_status 断言更新 |

### 未修改

- `credits_service.py` / `credits_read.py` — 未改
- `billing.py` / `subscriptions.py` / `entitlements.py` — 未改
- migration — 未改
- 前端 — 零变更
