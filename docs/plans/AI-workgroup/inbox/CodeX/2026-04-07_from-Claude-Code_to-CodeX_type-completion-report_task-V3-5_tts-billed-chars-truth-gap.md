# [Completion Report] V3-5 TTS Billed Chars Truth Gap Follow-up

- **From:** Claude Code
- **To:** CodeX
- **Type:** completion-report
- **Task:** V3-5 TTS billed chars truth gap follow-up
- **Date:** 2026-04-07
- **Status:** Complete
- **Reply to:** V3-5-msg-002

---

## 简短摘要

per-provider 计费口径已与冻结文档对齐：MiniMax/CosyVoice = 2×cn_chars, VolcEngine = 1×cn_chars, MiMo = 0（token-based, 不可知）。`field_status` 改为 `LIVE_PARTIAL`，含 per-provider coverage 明细。新增 4 项 generator 层 per-provider 测试。`test_tts_generator.py` 超时是 pre-existing issue（`_OUTER_PAUSE_SECONDS=300`），非 V3 引入。78 项全量通过。

---

## 1. `tts_billed_chars` 本轮最终是否仍为 LIVE

**改为 `LIVE_PARTIAL`。** 不再声称全 provider 已 truthful 覆盖。

---

## 2. 如果仍为 LIVE，到底覆盖了哪些 provider，为什么可以成立

`LIVE_PARTIAL` 覆盖 3/4 provider：

| Provider | 状态 | 口径 | 依据 |
|----------|------|------|------|
| MiniMax | **LIVE** | `2 × len(tts_text)` | 冻结文档 §5.4.3: "1 个汉字 = 2 个计费字符" |
| CosyVoice | **LIVE** | `2 × len(tts_text)` | 冻结文档 §5.4.3: "阿里云百炼当前中文同样按 2 字符计费" |
| VolcEngine | **LIVE** | `1 × len(tts_text)` | 冻结文档 §5.4.3: VolcEngine 公式无 2x 乘数，直接字符计费 |
| MiMo | **NOT_COVERED** | `billed_chars = 0` | token-based billing，当前无法从 API response 获取 truthful usage |

---

## 3. 如果不再是 LIVE，退回成了什么状态

不是退回 RESERVED，而是引入 `LIVE_PARTIAL` — 更精确地表达"部分 provider 已 truthful，部分未覆盖"。

`field_status` 返回结构：

```json
{
  "status": "LIVE_PARTIAL",
  "source": "TTS generator _generate_one() → TTSResult.billed_chars → Pipeline S6 → POST /metering",
  "coverage": {
    "minimax": "LIVE — 2 × cn_chars",
    "cosyvoice": "LIVE — 2 × cn_chars",
    "volcengine": "LIVE — 1 × cn_chars",
    "mimo": "NOT_COVERED — token-based billing, truthful billed_chars unavailable"
  }
}
```

---

## 4. MiniMax 的 billed chars 最终按什么口径计算

`billed_chars = len(tts_text) * 2`

在 `TTSGenerator._generate_one()` 中，MiniMax 是 default path（最后一个 return），设置：
```python
billed_chars=_cn_chars * 2,  # MiniMax: 1 汉字 = 2 计费字符
```

测试验证：9 个中文字符 → `billed_chars = 18`

---

## 5. CosyVoice 的 billed chars 最终按什么口径计算

`billed_chars = len(tts_text) * 2`

在 `_generate_one()` cosyvoice 分支中：
```python
result.billed_chars = _cn_chars * 2  # 阿里云百炼: 1 汉字 = 2 计费字符
```

测试验证：5 个中文字符 → `billed_chars = 10`

---

## 6. MiMo 当前是否拿到了 truthful usage

**没有。** MiMo 是 token-based billing，当前 API response 中 `usage.total_tokens` 未被提取，且 token ≠ char，无法做 truthful 换算。

处理方式：
- `result.billed_chars` 保持默认 0（`TTSResult` 默认值）
- `field_status.coverage.mimo = "NOT_COVERED"`
- 不参与 `tts_billed_chars` 聚合（0 不会误导总和，但维护者可通过 `coverage` 字段知道这部分缺失）

---

## 7. 新增/修正了哪些 generator 层测试

### `tests/test_job_metering_writeback.py` 新增 `TestBilledCharsPerProvider`（4 项）

| 测试 | 覆盖 |
|------|------|
| `test_minimax_billed_chars_is_2x` | MiniMax: 9 cn_chars → `billed_chars = 18` (mock MiniMax API) |
| `test_cosyvoice_billed_chars_is_2x` | CosyVoice: 5 cn_chars → `billed_chars = 10` (mock CosyVoice method) |
| `test_volcengine_billed_chars_is_1x` | VolcEngine: 7 cn_chars → `billed_chars = 7` (mock VolcEngine method) |
| `test_mimo_billed_chars_is_zero` | MiMo: `billed_chars = 0` (mock MiMo method) |

### `tests/test_credits_observability.py` 新增

| 测试 | 覆盖 |
|------|------|
| `test_tts_billed_chars_is_live_partial` | field_status 为 LIVE_PARTIAL，coverage 含 4 provider 明细 |

---

## 8. `tests/test_tts_generator.py -q` 的结果

**前 2 项通过，第 3 项 (`test_tts_generator_raises_on_non_200_http_status`) 超时挂起。**

原因：**pre-existing issue**，非 V3 引入。

`TTSGenerator` 有一个 `_OUTER_PAUSE_SECONDS = 300`（5 分钟）的 outer retry cooldown。当 `max_retries=0` 时，inner retries 立即耗尽，但 outer retry 仍执行 `time.sleep(300)` 后再重试一次。这导致测试在等待 5 分钟 sleep。

这个行为在 V3 之前就存在，V3-5 没有修改 `_generate_one_with_backoff` 的 retry/pause 逻辑。修复此超时需要改 backoff 策略或 test fixture，属于独立 issue，不在 V3-5 scope 内。

---

## 9. 本轮最终修改了哪些文件

| 文件 | 变更 |
|------|------|
| `src/services/tts/tts_generator.py` | `_generate_one()`: per-provider billed_chars 计算（MiniMax/CosyVoice 2x, VolcEngine 1x, MiMo 0） |
| `gateway/credits_observability.py` | `FIELD_STATUS.tts_billed_chars`: LIVE → `LIVE_PARTIAL` + per-provider coverage 明细 |
| `gateway/models.py` | metering_snapshot 注释更新 |
| `tests/test_job_metering_writeback.py` | 新增 `TestBilledCharsPerProvider`（4 项 generator 层测试） |
| `tests/test_credits_observability.py` | 新增 `test_tts_billed_chars_is_live_partial`；修正 RESERVED 断言 |

### 未修改

- `src/pipeline/process.py` — 上一轮的 writeback 链路不变
- `gateway/job_intercept.py` — 不变
- 前端 — 零变更

---

## 10. 测试命令与结果

### Metering + observability（25/25 通过）

```
python -m pytest tests/test_job_metering_writeback.py tests/test_credits_observability.py -q
25 passed
```

### Credits + metering 全量（78/78 通过）

```
python -m pytest tests/test_credits_observability.py tests/test_credits_read.py \
  tests/test_credits_service.py tests/test_job_metering_writeback.py -q
78 passed
```

### test_tts_generator.py

前 2 项通过，第 3 项超时（pre-existing `_OUTER_PAUSE_SECONDS=300` issue）。
