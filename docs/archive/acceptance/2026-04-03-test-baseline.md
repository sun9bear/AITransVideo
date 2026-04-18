# 测试基线 — 2026-04-03

> 本文档记录截至 2026-04-03 在当前工作区 HEAD 上实际执行的测试结果。
> 后续每个 Task 的验收门槛：**不新增失败；已有失败数量只允许下降，不允许上升。**

## 环境

- Python: 3.12.13 (uv managed, cpython-3.12-windows-x86_64)
- OS: Windows 10 IoT Enterprise LTSC 2021
- Branch: `codex/review-guidelines`
- 工作区状态: dirty（含未提交的功能代码变更）

---

## 基线组 1: process pipeline 回归

**命令:**

```bash
pytest tests/test_process_pipeline.py --tb=no -q
```

**结果:** 10 failed, 63 passed, 1 warning — 耗时 29.01s

### 失败用例清单（10 个）

| # | 测试名 | 观察到的症状分类 | 实际断言错误 |
|---|--------|----------------|-------------|
| 1 | `test_process_pipeline_reused_project_requires_fresh_translation_review_even_if_old_review_was_approved` | A: active_stage 不匹配 | `assert 'translation_config_review' == 'translation_review'` |
| 2 | `test_process_pipeline_wait_for_review_writes_state_files_to_final_project_dir` | A: active_stage 不匹配 | `assert 'translation_config_review' == 'speaker_review'` |
| 3 | `test_process_pipeline_uses_inferred_single_speaker_name_for_voice_registry_lookup` | B: speaker name 不匹配 | `assert {'speaker_a': 'Speaker A'} == {'speaker_a': 'Dan Koe'}` |
| 4 | `test_process_pipeline_single_speaker_default_placeholder_skips_generic_registry_lookup` | C: voice lookup 行为不匹配 | `AssertionError: lookup_voice_ids should be skipped for default single-speaker placeholder names` |
| 5 | `test_process_pipeline_auto_clones_voice_a_when_missing_in_single_speaker_mode` | B: speaker name 不匹配 | `assert {'speaker_a': 'Speaker A'} == {'speaker_a': 'Dan Koe'}` |
| 6 | `test_process_pipeline_auto_clones_voice_b_when_registry_misses` | B: speaker name 不匹配 | `assert 'Speaker B' == 'Guest'` |
| 7 | `test_process_pipeline_auto_clones_both_voices_when_both_are_missing` | C: auto-clone 行为不匹配 | `AssertionError: Unexpected speaker_name: Speaker A` |
| 8 | `test_process_pipeline_wait_for_review_pauses_for_voice_review_when_sample_is_too_short` | A: active_stage 不匹配 | `assert 'translation_config_review' == 'voice_review'` |
| 9 | `test_process_pipeline_runs_review_step_for_two_speaker_mode` | D: speaker label 分配不匹配 | `assert ['speaker_a', ..., 'speaker_a'] == ['speaker_a', ..., 'speaker_b']` |
| 10 | `test_process_pipeline_skips_review_when_requested` | E: skip_review 行为不匹配 | `assert 1 == 0`（review_called 应为 0 但实际为 1） |

### 失败症状分类

以下分类基于实际观察到的断言错误，不做根因推断。

**A. review stage / active_stage 断言不匹配（3 个：#1, #2, #8）**

测试期望的 stage 名称（`translation_review` / `speaker_review` / `voice_review`）与代码实际输出的 stage 名称（`translation_config_review`）不一致。三个用例均断言在不同的期望 stage 名上，但实际值都是 `translation_config_review`。

**B. speaker name 不匹配（3 个：#3, #5, #6）**

测试期望 speaker_name 为推断后的人名（如 `"Dan Koe"`、`"Guest"`），但实际返回的是占位符（`"Speaker A"`、`"Speaker B"`）。

**C. voice lookup / auto-clone 下游行为不匹配（2 个：#4, #7）**

- #4：测试期望对 default placeholder name 跳过 voice lookup，但 pipeline 仍执行了 lookup 并失败。
- #7：测试期望 auto-clone 流程使用推断后的人名，但收到了占位符名称 `"Speaker A"`。

**D. speaker label 分配不匹配（1 个：#9）**

双说话人模式下，测试期望段落的 speaker label 按 `[speaker_a, speaker_b, speaker_a]` 分配，但实际分配为 `[speaker_a, speaker_b, speaker_a]` 中有一位被统一成了 `speaker_a`。

**E. skip_review 行为不匹配（1 个：#10）**

测试期望 `skip_review=True` 时 review 不被调用（`review_called == 0`），但实际 review 被调用了 1 次。

### 附注

所有 10 个失败用例的日志中均出现 `WARNING: GEMINI_API_KEY not set, skipping unified review`。这是一个观察到的共性现象，可能与部分失败相关，但本轮 Task 0 不据此断定它是全部失败的唯一根因。各类症状（stage 命名、speaker name 传播、voice lookup 逻辑、skip_review 控制流）可能有各自独立的触发条件。

### 警告（1 个）

- `pydub.utils`: `audioop` 模块在 Python 3.13 中将被移除的 DeprecationWarning

---

## 基线组 2: TTS routing + canonical shape + output dispatch

**命令:**

```bash
pytest tests/test_tts_routing_invariants.py tests/test_project_builder.py tests/test_project_shape_helpers.py tests/test_output_dispatcher.py --tb=no -q
```

**结果:** 28 passed, 2 warnings — 耗时 2.73s

### 通过用例分布

| 测试文件 | passed |
|----------|--------|
| `test_tts_routing_invariants.py` | 11 |
| `test_project_builder.py` | 6 |
| `test_project_shape_helpers.py` | 3 |
| `test_output_dispatcher.py` | 8 |

### 警告（2 个）

- `pydub.utils`: `audioop` DeprecationWarning
- `src.services.control_panel`: `cgi` 模块 DeprecationWarning

---

## 与计划预估的比对

| 指标 | 计划预估 | 实际结果 | 一致性 |
|------|---------|---------|--------|
| `test_process_pipeline.py` failed | 10 | 10 | 一致 |
| `test_process_pipeline.py` passed | 63 | 63 | 一致 |
| 基线组 2 全部通过 | 是 | 是 | 一致 |
| 基线组 2 通过数量 | 未精确预估 | 28 | N/A |

---

## 后续验收规则

1. 每个后续 Task 完成后，重跑上述两组命令。
2. **基线组 1**: failed 数量 ≤ 10，passed 数量 ≥ 63。不允许新增失败。
3. **基线组 2**: 保持全部通过。不允许出现新的失败。
4. 本次未引入任何 `@pytest.mark.xfail`。如需标记已知失败，须单独 commit 并经维护者确认。
