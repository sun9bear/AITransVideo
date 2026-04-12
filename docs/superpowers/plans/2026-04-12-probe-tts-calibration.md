# 探针 TTS 校准 + 文本字段统一 + 音色前置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Context

**问题：** 翻译阶段用固定 4.5 字/秒估算目标字数，但实际 TTS 语速因引擎/音色/克隆差异可达 3.5~5.5 字/秒。导致下游 rewrite 触发率 ~25%、force_dsp ~33-57%，浪费 LLM + TTS 成本。

**根因：** 校准太晚（全量 TTS 后才做），音色选择在翻译之后（翻译时不知道最终引擎/音色），`cn_text/tts_cn_text/literal_cn_text` 三层文本字段是过渡产物（TTS 和字幕已不需要区分）。

**方案：** 四步改造——
1. 统一文本字段为 `cn_text`（清理历史债务）
2. 音色选择移到翻译前（Pipeline 重排序）
3. 探针翻译 + TTS 校准 chars/sec → 主批翻译用校准值（核心优化）
4. 翻译提示词优化（增强）

**新 Pipeline 顺序（Studio）：**
```
S2 审校 → Pass 3 音色画像 → 音色选择（暂停）
  → S4-probe：探针翻译 + TTS → 校准 chars/sec
  → S3 主批翻译（用校准值）→ 翻译审核（暂停）
  → S4 TTS 全量 → S5 对齐 → S6 合成
```

**新 Pipeline 顺序（Express）：**
```
S2 审校
  → S4-probe：探针翻译 + TTS → 校准 chars/sec
  → S3 主批翻译（用校准值）
  → S4 TTS 全量 → S5 对齐 → S6 合成
```

---

## Phase 1: 统一 cn_text 文本字段

### 背景
`SubtitleLine` 有 `cn_text` / `literal_cn_text` / `tts_cn_text` 三个字段，`SemanticBlock` 有对应的 `merged_*` 版本，`DubbingSegment` 有 `cn_text` / `tts_cn_text`。`get_preferred_cn_text_for_tts()` 和 `get_preferred_cn_text_for_caption()` 返回相同结果，说明区分已无意义。

### 改动范围
全部合并为 `cn_text`（`SubtitleLine`/`DubbingSegment`）和 `merged_cn_text`（`SemanticBlock`），删除 `tts_cn_text`、`literal_cn_text` 及所有过渡方法。

**影响文件（16 个）：**

| 文件 | 改动 |
|------|------|
| `src/core/models.py` | 删 `literal_cn_text`/`tts_cn_text`/`merged_literal_cn_text`/`merged_tts_cn_text` 字段 + 所有过渡方法 |
| `src/services/gemini/translator.py` | DubbingSegment 删 `tts_cn_text` 字段，构造时不再传 |
| `src/services/gemini/rewriter.py` | 参数 `tts_cn_text` 重命名为 `cn_text` |
| `src/services/tts/tts_generator.py:722` | `segment.tts_cn_text or segment.cn_text` → `segment.cn_text` |
| `src/services/alignment/aligner.py` | `segment.tts_cn_text` → `segment.cn_text`（读写都改） |
| `src/pipeline/process.py` | ~17 处 `tts_cn_text` → `cn_text`，删 fallback 链 |
| `src/services/web_ui/translation_review.py` | 删 `tts_cn_text` 字段处理 |
| `src/services/web_ui/speaker_review.py` | 删 `tts_cn_text` 初始化和覆盖 |
| `src/services/web_ui/segment_loader.py` | 删 `tts_cn_text` 加载 |
| `src/services/voice_asset.py` | `merged_tts_cn_text=` → `merged_cn_text=` |
| `src/modules/workflow/project_workflow.py` | `target_line.tts_cn_text =` → `target_line.cn_text =` |
| `src/modules/workflow/alignment_stage_runner.py` | 删 `merged_tts_cn_text` 导出/恢复 |
| `src/modules/alignment/alignment_orchestrator.py` | `block.merged_tts_cn_text =` → `block.merged_cn_text =` |
| `src/modules/chunking/semantic_block_builder.py` | 删 `merged_tts_cn_text` 构建逻辑 |
| `frontend-next/src/types/api.ts` | 删 `tts_cn_text` 字段 |
| `frontend-next/src/lib/api/reviews.ts` | 删 `ttsCnText` 发送/映射 |

### 注意事项（Codex 审查补充）
- Phase 1 是一次正式的数据模型迁移，不是"顺手清理"。涉及 16 文件 ~100 处引用。
- `translation_review.py` 中 split 操作分别处理 `cn_text` / `tts_cn_text`（L44, L239），需确保 split 后只操作 `cn_text`。
- `caption_retiming.py` 的 caption fallback 依赖 `final_cn_lines` 和 caption getter，需确认删除 getter 后 caption 仍正常。
- 需检查 `tests/test_text_layers.py` 等已有测试，同步更新测试用例中的旧字段引用。

### 执行步骤

- [ ] 1.1 修改 `src/core/models.py`：SubtitleLine 删 `literal_cn_text`/`tts_cn_text` 字段、`__post_init__` fallback 链、`has_tts_cn_layer()`/`has_literal_cn_layer()`/`get_literal_cn_text()`/`get_preferred_cn_text_for_tts()`/`get_preferred_cn_text_for_caption()` 方法。SemanticBlock 同理删 `merged_literal_cn_text`/`merged_tts_cn_text` 和对应方法。删 `summarize_subtitle_text_layers`/`summarize_block_text_layers` 中的 tts/literal 统计。
- [ ] 1.2 修改 `src/services/gemini/translator.py`：DubbingSegment 删 `tts_cn_text` 字段，构造时删 `tts_cn_text=` 参数。
- [ ] 1.3 修改 `src/services/gemini/rewriter.py`：`rewrite_for_duration(tts_cn_text=...)` 参数名改为 `cn_text`，内部变量同步改名。
- [ ] 1.4 修改 `src/services/tts/tts_generator.py:722`：`segment.tts_cn_text or segment.cn_text` 简化为 `segment.cn_text`。
- [ ] 1.5 修改 `src/services/alignment/aligner.py`：所有 `segment.tts_cn_text` → `segment.cn_text`（~5 处读写）。
- [ ] 1.6 修改 `src/pipeline/process.py`：全文替换 `tts_cn_text` → `cn_text`（~17 处），删 fallback 逻辑（如 `segment.tts_cn_text = segment.tts_cn_text or segment.cn_text`），删 `literal_cn_text` 引用。
- [ ] 1.7 修改 web_ui 三个文件：`translation_review.py`（删 tts_cn_text 处理）、`speaker_review.py`（删初始化）、`segment_loader.py`（删加载）。
- [ ] 1.8 修改 modules 四个文件：`voice_asset.py`、`project_workflow.py`、`alignment_stage_runner.py`、`alignment_orchestrator.py`、`semantic_block_builder.py`——所有 `tts_cn_text`/`literal_cn_text`/`merged_tts_cn_text`/`merged_literal_cn_text` → `cn_text`/`merged_cn_text`。
- [ ] 1.9 修改前端：`types/api.ts` 删 `tts_cn_text` 字段，`reviews.ts` 删 `ttsCnText` 发送和映射。
- [ ] 1.10 修改 `src/modules/output/caption_retiming.py`（如有 getter 引用）：确认 caption fallback 改为直接用 `cn_text` / `merged_cn_text`。
- [ ] 1.11 修改 `tests/test_text_layers.py`：更新测试用例，删除 `literal_cn_text`/`tts_cn_text` 相关断言和构造参数。
- [ ] 1.12 全局搜索确认无遗漏：`grep -r "tts_cn_text\|literal_cn_text\|merged_tts_cn_text\|merged_literal_cn_text\|get_preferred_cn_text\|has_tts_cn_layer\|has_literal_cn_layer\|get_literal_cn_text" src/ frontend-next/ tests/`
- [ ] 1.13 Commit

---

## Phase 2: 音色选择移到翻译前

### 背景
当前流程：S3 翻译 → 翻译审核 → Pass 3 音色画像 → 音色选择 → S4 TTS。
翻译时不知道最终引擎/音色，只能用 4.5 猜。

### 可行性验证
- `_build_voice_selection_review_payload` 依赖 `translation_result` 仅作为 fallback 获取 speaker_profiles（L1900），当 `speaker_styles` 非空时不使用。Pass 3 已提供 speaker_styles，因此 translation_result 可选。
- 前端 `VoiceSelectionPanel.tsx` 只显示 `source_text`（原文），不显示译文。
- Pass 3 只需 `transcript_result.lines` + 音频，不需翻译结果。

### 改动范围

| 文件 | 改动 |
|------|------|
| `src/pipeline/process.py` | 移动 voice_selection_review 块到 S3 翻译之前；`_build_voice_selection_review_payload` 的 `translation_result` 改为可选 |
| 前端 | 审核步骤顺序调整（音色选择在翻译审核之前显示） |

### 注意事项（Codex 审查补充）
- `process.py:951` 有一条"统一审核时把音色选择延迟到 translation_review"的 defer 逻辑，移动音色选择后必须删除此 defer 路径。
- `translation_review` payload 仍写入 `voice_id_a`/`voice_id_b`（L1085），移动后需从 translation_review payload 中移除 voice_id 相关字段（音色选择已有独立暂停点）。
- pause/resume 语义需调整——音色选择不再和翻译审核合并，各有独立的暂停/恢复流程。

### 执行步骤

- [ ] 2.1 修改 `_build_voice_selection_review_payload` 签名：`translation_result: TranslationResult | None = None`，fallback 逻辑加 `if translation_result:` 守卫。
- [ ] 2.2 删除 `process.py:951` 附近的"统一审核时延迟音色选择到 translation_review"的 defer 逻辑。
- [ ] 2.3 从 `translation_review` payload 构建中移除 `voice_id_a`/`voice_id_b` 字段（L1085-1086），音色信息已由独立的 voice_selection_review 阶段管理。
- [ ] 2.4 在 `process.py` 的 `run()` 中，将 voice_selection_review 块（L1170-1290）移到 S3 翻译（L1014）之前。确保 Pass 3 在音色选择之前运行（当前已是）。
- [ ] 2.5 音色选择恢复后（`approved_voice_selection`），将 `_speaker_voices` 和 `_speaker_providers` 传入后续的探针和翻译流程。
- [ ] 2.6 Commit

---

## Phase 3: 探针翻译 + TTS 校准

### 背景
音色确认后、主批翻译前，用少量代表段做探针翻译（无字数约束）→ TTS → 校准 chars/sec → 主批翻译用校准值。

### 设计要点
- **探针翻译不给 min_chars/max_chars**：避免 4.5 假设污染探针。只给 `target_duration_seconds`，让 LLM 凭语感翻译。
- **探针 TTS 输出到 `tts/_probe/`**：避免与主批 segment_id 碰撞。
- **校准值传入主批翻译**：通过 `_build_groups` 的新参数 `chars_per_second` / `chars_per_second_by_speaker`。
- **探针段选取**：每说话人 2-3 段，3-8 秒，避开首尾。最少 3 段，最多 10 段。
- **探针提示词与主批风格一致**：强调口语化、保留语气词，确保校准值可迁移。

### 改动范围

| 文件 | 改动 |
|------|------|
| `src/pipeline/process.py` | 新增 `_select_probe_segments()`、`_run_probe_tts_calibration()`；在音色选择后、S3 翻译前调用 |
| `src/services/gemini/translator.py` | 新增 `PROBE_TRANSLATION_PROMPT_TEMPLATE`、`_build_probe_groups()`、`translate_probe()` 方法；`_build_groups` 和 `_estimate_dynamic_target_chars` 支持 `chars_per_second` 参数；`translate()` 透传 `chars_per_second` |
| `src/services/tts/duration_estimator.py` | 不改（现有 `calibrate()` 已满足） |
| `tests/test_probe_tts_calibration.py` | 新增探针选取、校准逻辑的测试 |

### 执行步骤

- [ ] 3.1 在 `translator.py` 新增 `PROBE_TRANSLATION_PROMPT_TEMPLATE`（不含 min_chars/max_chars，强调口语化、保留语气词）。
- [ ] 3.2 在 `translator.py` 新增 `_build_probe_groups(lines)` 函数（只含 segment_id, speaker_id, target_duration_seconds, source_text）。
- [ ] 3.3 在 `GeminiTranslator` 新增 `translate_probe()` 方法，使用探针提示词和精简 groups。
- [ ] 3.4 修改 `_estimate_dynamic_target_chars` 支持 `chars_per_second` 参数（替代硬编码 4.5）。
- [ ] 3.5 修改 `_build_groups` 支持 `chars_per_second` 和 `chars_per_second_by_speaker` 参数，按说话人选择对应语速。
- [ ] 3.6 修改 `translate()` 方法签名，新增 `chars_per_second` / `chars_per_second_by_speaker` 可选参数，传入 `_build_groups`。
- [ ] 3.7 在 `process.py` 新增 `_select_probe_segments()` 静态方法。
- [ ] 3.8 在 `process.py` 新增 `_run_probe_tts_calibration()` 方法：选取探针段 → 调用 `translate_probe` → TTS 到 `tts/_probe/` → `_calibrate_tts_duration` → 返回校准值。
- [ ] 3.9 在 `run()` 中，音色选择恢复后、S3 翻译前，调用 `_run_probe_tts_calibration`，将校准值传入 `translator.translate(..., chars_per_second=..., chars_per_second_by_speaker=...)`。
- [ ] 3.10 新增 `tests/test_probe_tts_calibration.py`：探针选取逻辑、校准计算、calibrated _build_groups 的测试。
- [ ] 3.11 Commit

---

## Phase 4: 翻译提示词优化

### 改动范围

| 文件 | 改动 |
|------|------|
| `src/services/gemini/translator.py` | 提示词模板 + 重试阈值 + 重试提示词 + LLM JSON 字段精简 |

### 执行步骤

- [ ] 4.1 修改 `DEFAULT_TRANSLATION_PROMPT_TEMPLATE` L98：`仅供参考，不是硬性约束` → `中文字数的目标范围，直接影响配音时长匹配度，请将译文字数控制在此范围内`。
- [ ] 4.2 精简发给 LLM 的 JSON 字段：`_build_prompt` 中过滤掉 `density_factor`、`reference_words_per_second`、`source_word_count`、`source_words_per_second`、`dynamic_target_chars`、`target_chars`、`target_duration_ms` 等内部字段。注意 `_build_translation_fingerprint` 仍用完整 groups 不影响缓存。
- [ ] 4.3 统一字数范围与重试阈值：删除 `DEFAULT_TRANSLATION_LENGTH_UNDERSHOOT_FACTOR` / `OVERSHOOT_FACTOR` 这套独立重试阈值。重试判断直接用 `min_chars` / `max_chars`——超出范围即重翻，不再有额外的宽松倍数。
- [ ] 4.4 字数范围可配置化：`_estimate_target_char_range` 从 `admin_settings.json` 读取 `translation_char_range_min_factor`（默认 0.85）和 `translation_char_range_max_factor`（默认 1.15），管理员可根据实际 rewrite 率动态调整。
- [ ] 4.5 改进重试提示词：从英文一句话改为中文具体指导（含偏长精简、偏短补充的操作建议）。
- [ ] 4.6 Commit

---

## 验证方案

1. **Phase 1 验证**：`grep -r "tts_cn_text\|literal_cn_text" src/ frontend-next/` 确认无遗漏。跑 `python -m pytest tests/` 确认不破坏现有测试。
2. **Phase 2 验证**：在 Studio 模式下创建任务，确认音色选择出现在翻译审核之前。
3. **Phase 3 验证**：查看 pipeline 日志中 `[S4-probe]` 输出，确认校准值合理（2.5~6.0 字/秒范围内）。对比探针校准前后的 `rewrite_count` 和 `force_dsp` 比率。
4. **Phase 4 验证**：对比翻译提示词改动前后的字数偏差分布。

## 风险

| 风险 | 缓解 |
|------|------|
| Phase 1 遗漏引用导致运行时 AttributeError | 全局 grep 确认 + 跑测试 |
| Phase 2 音色前置后前端步骤顺序混乱 | 前端改动与后端同步 |
| Phase 3 探针翻译/TTS 失败 | 全程 try-except，失败回退 4.5 |
| Phase 4 收紧重试阈值增加翻译成本 | 每次重试 ~$0.01，预计净成本下降 |

## 预期效果

| 指标 | 改动前 | 改动后 |
|------|--------|--------|
| 翻译阶段 chars/sec 估计 | 固定 4.5 | 探针校准（精准） |
| DSP 拉伸触发率 | ~35% | ~25% |
| LLM rewrite 触发率 | ~25% | ~8-10% |
| 每任务额外成本 | — | ~$0.02（探针翻译 + TTS） |
| 每任务节省成本 | — | ~$0.1-0.3（减少 rewrite + 重 TTS） |
