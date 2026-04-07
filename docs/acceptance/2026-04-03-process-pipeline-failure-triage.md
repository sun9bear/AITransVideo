# test_process_pipeline.py 失败测试去向梳理 — 2026-04-03

> 本文档将当前 10 个失败测试从"accepted baseline"转为"有明确去向的队列"。
> 基于本轮实际运行结果和代码阅读，不修改任何代码或测试。

## 1. 当前基线

**命令：** `pytest tests/test_process_pipeline.py --tb=no -q`
**结果：** 10 failed, 63 passed, 1 warning (30.28s)
**与 Task 0 基线一致：** 是（同样的 10 个测试名、同样的通过/失败数）

## 2. 失败症状总述

通过阅读 `tests/test_process_pipeline.py` 的测试代码和 `src/pipeline/process.py` 的生产代码，观察到以下两组现象：

### 已确认的 drift：review stage / gate 命名漂移（影响 A 类）

**代码证据：** `process.py` L871-916 使用 `TRANSLATION_CONFIG_REVIEW_STAGE`（"translation_config_review"）作为翻译前的配置审核门。`speaker_review` 和 `voice_review` 的暂停逻辑在统一审核流程中已变化（L779-782 日志："说话人审核已合并到统一审核，自动跳过"）。

**观察到的影响：** 测试期望 pipeline 在 `speaker_review` / `translation_review` / `voice_review` 阶段暂停，但 pipeline 实际暂停在 `translation_config_review`。

### 未定根因的症状簇：speaker name / label 传播不一致（影响 B 类）

**观察到的症状：** 测试期望 speaker_name 为推断后的人名（如 "Dan Koe"、"Guest"），但实际返回占位符（"Speaker A"、"Speaker B"）。所有 10 个失败用例的日志中均出现 `WARNING: GEMINI_API_KEY not set, skipping unified review`，这可能相关，但本轮 triage 不能据此断定它是 B 类全部失败的唯一根因。

**已观察到的事实：**
- legacy fallback 确实执行了 `infer_speaker_identities`（日志可见 `[S2-legacy] Speaker A -> Dan Koe`）
- 但推断后的名称未出现在后续断言点（voice registry lookup 的 `speaker_names` dict、auto-clone 的 `clone_speaker_name`）
- 传播链路中断裂的精确位置未在本轮定位
- #9 的 speaker label 分配不一致（`speaker_b` 变成 `speaker_a`）可能与推断传播相关，也可能是独立的 label correction 逻辑变化

## 3. 去向分流

### 类别 A：过时断言 — 适合下一小轮直接修（3 个）

这 3 个测试的失败原因是审核流程重构后，测试断言中的 stage 名称未同步更新。pipeline 行为本身是正确的（在新的 `translation_config_review` gate 暂停），只是测试期望的 stage 名过时了。

| # | 测试名 | 断言错误 | 判断依据 | 建议去向 | 需维护者决定 |
|---|--------|---------|---------|---------|------------|
| 1 | `..._reused_project_requires_fresh_translation_review_...` | `'translation_config_review' != 'translation_review'` | 测试 L2236 断言 `paused_review_stage == TRANSLATION_REVIEW_STAGE`；pipeline 实际暂停在 `TRANSLATION_CONFIG_REVIEW_STAGE`（L874）。stage 名已变，测试未更新 | **A：修测试断言** | 否 |
| 2 | `..._wait_for_review_writes_state_files_to_final_project_dir` | `'translation_config_review' != 'speaker_review'` | 测试 L2350 断言 `active_stage == SPEAKER_REVIEW_STAGE`；但 speaker_review 已被合并到统一审核并自动跳过（L779-782），pipeline 直接到 `translation_config_review` | **A：修测试断言** | 否 |
| 8 | `..._wait_for_review_pauses_for_voice_review_when_sample_is_too_short` | `'translation_config_review' != 'voice_review'` | 测试 L3450 断言 `active_stage == "voice_review"`；但 voice_review 在统一审核流程中的暂停逻辑已变化，pipeline 先到 `translation_config_review` | **A：修测试断言** | 否 |

### 类别 B：speaker name / label 传播不一致 — 需先定位断裂点再确认期望语义（6 个）

这 6 个测试观察到 speaker name 或 speaker label 的实际值与测试期望不一致。日志显示 legacy fallback 路径确实执行了推断（如 `[S2-legacy] Speaker A -> Dan Koe`），但推断结果未出现在后续断言点。传播链路中的精确断裂位置未在本轮定位。修复前需要先定位断裂点，再确认 mocked 测试环境下的传播期望。

| # | 测试名 | 断言错误 | 判断依据 | 建议去向 | 需维护者决定 |
|---|--------|---------|---------|---------|------------|
| 3 | `..._uses_inferred_single_speaker_name_for_voice_registry_lookup` | `{'speaker_a': 'Speaker A'} != {'speaker_a': 'Dan Koe'}` | 测试 L2757 期望 speaker_name 被推断为 "Dan Koe"。代码路径：unified reviewer 返回 None → legacy fallback 执行 `infer_speaker_identities` → 确实推断出 "Dan Koe"（日志可见 `Speaker A -> Dan Koe`），但推断结果未传播到 voice registry lookup 的 `speaker_names` dict。**传播断裂点需要定位** | **B：确认传播语义** | 是 |
| 4 | `..._single_speaker_default_placeholder_skips_generic_registry_lookup` | `lookup_voice_ids should be skipped for default single-speaker placeholder names` | 测试 L2870 期望对 placeholder "Speaker A" 跳过 lookup 走 auto-clone。但 pipeline 仍执行了 lookup。**需确认 placeholder 检测逻辑是否在重构中被绕过** | **B：确认 placeholder 检测** | 是 |
| 5 | `..._auto_clones_voice_a_when_missing_in_single_speaker_mode` | `{'speaker_a': 'Speaker A'} != {'speaker_a': 'Dan Koe'}` | 与 #3 同类：speaker name 推断结果未传播到 auto-clone 路径 | **B：同 #3** | 是 |
| 6 | `..._auto_clones_voice_b_when_registry_misses` | `'Speaker B' != 'Guest'` | 双说话人模式下 speaker_b 推断为 "Guest" 但实际为 "Speaker B"。与 #3 同类传播问题，但影响的是 speaker_b | **B：同 #3** | 是 |
| 7 | `..._auto_clones_both_voices_when_both_are_missing` | `Unexpected speaker_name: Speaker A` | auto-clone 逻辑收到的 speaker_name 是占位符而非推断名，导致 clone 注册时的名称不符预期 | **B：同 #3** | 是 |
| 9 | `..._runs_review_step_for_two_speaker_mode` | `['speaker_a', ..., 'speaker_a'] != ['speaker_a', ..., 'speaker_b']` | 双说话人 transcript 中 speaker label 分配不符预期。测试 L3729 期望 `["speaker_a", "speaker_a", "speaker_b"]` 但实际最后一个 segment 变成了 `speaker_a`。**可能是 label correction 逻辑变化** | **B：确认 label correction** | 是 |

### 类别 C：当前不适合处理 — 继续保留为 accepted baseline（1 个）

| # | 测试名 | 断言错误 | 判断依据 | 建议去向 | 需维护者决定 |
|---|--------|---------|---------|---------|------------|
| 10 | `..._skips_review_when_requested` | `assert 1 == 0`（review_called 应为 0 但实为 1） | 测试 L3752 期望 `skip_review=True` 时 `review_called == 0`。但 `review_called` 的计数器挂在 `review_speaker_labels()` mock 上（L1259）。在当前统一审核流程下，即使 `skip_review=True`，`review_speaker_labels` 仍可能被 legacy fallback 调用。**这涉及 skip_review 的语义边界问题：skip_review 是否也应跳过 legacy fallback 中的 label correction？需要产品决定** | **C：保留，需产品决定** | 是 |

## 4. 结论：是否值得开一个 1-2 天的小修 Sprint

**建议：是，但范围限定为类别 A（3 个）。**

### 适合集中修复的（类别 A，3 个）

- #1、#2、#8：审核 stage 名称漂移
- 预估工作量：0.5 天
- 修复方式：更新测试断言中的 stage 名（从 `translation_review` / `speaker_review` / `voice_review` 改为 `translation_config_review`），并验证 pipeline 在新 gate 上的暂停行为仍然正确
- 风险：低。只改测试断言，不改生产代码

### 不适合现在动的（类别 B，6 个）

- #3、#4、#5、#6、#7、#9：speaker name / label 传播不一致症状簇
- 原因：传播链路中的精确断裂位置未在本轮定位。修复前需先定位断裂点，可能需要改动 `process.py` 中审核流程的传播逻辑，影响面待评估
- 建议：先做类别 A 的修复，观察是否有连带效果，再决定是否进入类别 B

### 继续保留为 baseline 的（类别 C，1 个）

- #10：`skip_review` 语义边界问题
- 原因：需要产品维护者先确认 `skip_review=True` 是否应该同时跳过 legacy fallback 中的 speaker label correction
- 建议：记录为"待产品决定"，不在技术 Sprint 中处理

### 总结

| 类别 | 数量 | 建议 |
|------|------|------|
| A：修测试断言 | 3 | 下一小轮直接修（0.5 天） |
| B：确认传播语义 | 6 | 等类别 A 修完后再评估 |
| C：保留为 baseline | 1 | 需产品决定 |

**如果开小修 Sprint，建议在 rerank readiness review 之前做类别 A（3 个）。** 类别 A 修完后，基线从 10 failed 降到 7 failed，且剩余的 7 个全部属于同一症状簇（speaker name/label 传播不一致 + skip_review 语义边界），更容易统一评估。
