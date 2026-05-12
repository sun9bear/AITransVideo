# 翻译质量与成本优化方案

> **版本**: v1.0
> **日期**: 2026-04-30
> **状态**: 设计完成，待执行
> **父文档**: `docs/plans/hermes/2026-04-11-hermes-platform-design.md`

---

## 一、现状评估

### 1.1 已做得好的部分

| 维度 | 当前方案 | 评价 |
|------|----------|------|
| 翻译长度控制 | Plan C 公式（EN word count × 1.8）+ 双层 char range 目标 + 一次重试 | 设计合理，但 `_ENGLISH_TO_CHINESE_CHAR_RATIO = 1.8` 是静态常量，未按说话人/语速动态校准 |
| 对齐策略 | DSP-first（15% threshold），重写 fallback，lexicographic 评分函数 | 架构正确，但阈值未做 A/B 调优 |
| 说话人归因 | ASR 声纹分离 + S2 三 pass LLM review + 低支持度校验 | 全链路最强模块，但 Pass 1 用 Gemini Pro（最贵模型）是成本大头 |
| 模型选路 | `llm_registry` 按 prompt_key 分配 cheap/deepseek → 严格/gemini_pro | 方向正确，但未按 segment 特征（短句/长句/对话/独白）差异化选路 |
| 成本计量 | `UsageMeter` sidecar + 按 bucket 分桶 + `credits_observability` | 观测面已成型，但缺少闭环反馈——记了账但没用来调优 |

### 1.2 关键缺口

1. **翻译长度公式无反馈校准**：`source_word_count × 1.8` 是固定经验值，不随语速/语种/内容密度自动修正。如果这个系数偏大，会导致 TTS 音频普遍过长，触发更多重写→重合成。
2. **重写/重合成比例缺少全局监控**：`DubbingSegment` 有 50+ audit 字段，但没有聚合流水线把这些字段转化为 actionable insight。
3. **说话人归因缺少精度衡量**：Pass 1 做了大量纠正工作，但"纠正了是否正确"缺乏评估——只靠低支持度说话人校验器做了一小部分。
4. **成本无实时反馈驱动选路**：`llm_registry` 的 fallback 链和 cost_rank 是正确的，但没把"这个 user 的这段内容是短对话，用 deepseek 就够了"这类实时信号注入选路决策。

---

## 二、优化路线图

### Phase A：翻译长度公式自适应校准（ROI 最高，优先做）

**目标**：让翻译目标字数更精确，从源头减少 TTS 后重写/重合成的触发率。

**方案**：

```
当前：target_chars = source_word_count × 1.8（全局固定）
优化：target_chars = source_word_count × calibrated_ratio(speaker_id, content_type)
```

**具体步骤**：

1. **收集每个 segment 的 post-TTS 实际时长 vs 目标时长**（已有 `DubbingSegment.first_pass_error_pct` 等字段）。
2. **按说话人聚合**：同一 `speaker_id` 的所有 segment 计算 `median(actual_chars_sec / target_chars_sec)`，得到该说话人的校准系数 `calibrated_ratio = 1.8 × median_error_ratio`。
3. **按内容类型聚合**：区分"独白长段"（source_word_count > 30, 单句）和"对话短段"（source_word_count < 15, 对话），各自校准。
4. **写入 voice_speed_catalog**：在 Gateway `/api/internal/voice-catalog` 中增加 `per_speaker_translation_ratio` 字段，与 `chars_per_second` 同类管理。
5. **透传到 `_build_groups`**：`GeminiTranslator` 在构建翻译 prompt 时使用校准后的 `target_chars`。

**预期效果**：将 first-pass 超幅比例（需要重写/重合成的比例）从当前基线降低 30-50%。

**成本**：不增加 LLM 调用，仅增加一次简单的统计聚合。

**改动文件**：

| 文件 | 改动 |
|------|------|
| `src/services/tts/voice_speed_catalog.py` | 增加 `translation_ratio` 字段和读取逻辑 |
| `src/services/gemini/translator.py` | `_build_groups` 读取并使用校准系数 |
| 新增 `src/services/translation_length_calibrator.py` | 统计聚合逻辑：收集历史 segment 数据，计算校准系数 |
| `gateway/voice_catalog_api.py` | 增加 `per_speaker_translation_ratio` 返回字段 |

---

### Phase B：Segment 差异化模型选路（降成本，次优先）

**目标**：在不降低翻译质量的前提下，把昂贵的 Gemini Pro 调用量降到最低。

**当前路径**：

```
translate 任务 → deepseek（低成本默认）
rewrite_strict → gemini_pro（高质量强制）
```

**优化路径**：

```
translate 任务 → 按 segment 特征分流：
  ├─ 短对话/回馈词（source_word_count < 10）      → deepseek (cost_rank=3)
  ├─ 中长独白（10 ≤ source_word_count < 30）       → deepseek (cost_rank=3)
  ├─ 长段技术内容（source_word_count ≥ 30, 术语多） → deepseek_v4_pro (cost_rank=4)
  └─ 内容合规/高风险段（检测到有害/敏感内容）      → gemini_pro (cost_rank=5)
```

**实现方式**：

1. 在 `llm_registry` 的 prompt-model 映射中增加 `content_density` 维度。
2. `GeminiTranslator._translate_batch_with_length_retry()` 在构建 prompt 前评估 segment 密度和风险等级，选择对应的 model。
3. 添加 `segment_model_selection` audit 字段到 `DubbingSegment` 用于监控。

**预期成本节省**：假设 deepseek 价格是 gemini_pro 的 1/5，当前 `rewrite_strict` 用 gemini_pro 的比例为 X%，那么将 deepseek_v4_pro 用于中等复杂度内容可省约 60-70% 的"非强制高质量" Gemini 调用费用。

**改动文件**：

| 文件 | 改动 |
|------|------|
| `src/services/llm_registry.py` | 增加 density-segmented 默认模型映射 |
| `src/services/gemini/translator.py` | `_get_prompt_model` 增加 density 维度；`_translate_batch_with_length_retry` 增加 density 评估 |
| `src/services/gemini/translator.py` | `DubbingSegment` 增加 `translation_model` audit 字段 |
| `src/services/transcript_reviewer.py` | `ReviewResult` 透传 density 相关 signal |

---

### Phase C：说话人归因精度闭环（中等 ROI，需先有数据支撑）

**目标**：量化 S2 Pass 1 的说话人纠正准确率，驱动模型选择和 prompt 优化。

**方案**：

1. **收集 Pass 1 的 corrections 作为"纠正集"**。
2. **低支持度说话人校验器**（`_apply_low_support_speaker_verifier`）已存在——将其从"安全网"升级为"评估器"，输出 `verifier_agrees` / `verifier_disagrees` 标注。
3. **人工审校结果**（Web UI translation review 中的 speaker reassignments）作为 ground truth。
4. **计算 `correction_precision`**：Pass 1 做的 correct_speaker 操作中，有多少比例与人工审校结果一致。
5. **低于阈值时自动调整**：如果某类 segment（如 A-B-A 交替对话）的 correction_precision < 60%，自动在该场景 turn off Pass 1 纠错或切换到更贵的模型。

**成本影响**：减少不必要的高成本 Pass 1 调用（Gemini with audio），或降低 Pass 1 模型等级。

**改动文件**：

| 文件 | 改动 |
|------|------|
| `src/services/transcript_reviewer.py` | Pass 1 corrections 增加 verifier 交叉验证标注 |
| 新增 `src/services/speaker_correction_evaluator.py` | correction_precision 计算和阈值决策 |
| `src/services/llm_registry.py` | speaker review 场景的适应性 fallback 规则 |

---

### Phase D：Hermes Ops Control Plane Phase 1（长期建设，当前不宜启动）

**说明**：Hermes 设计文档（`docs/plans/hermes/`）已完整，以下是从中裁剪的最小可行版本，专门服务于上述优化方案的闭环反馈需求。**当前不建议启动实施**，原因见第五节。

**Hermes Phase 1 最小闭环**：

```
collector（收集 UsageMeter + DubbingSegment audit 字段）
  → detector（阈值规则引擎）
  → analyst（生成结构化报告）
  → publisher（写入 hermes_insights 表，Telegram/web 推送）
```

**Phase 1 最小可行异常类型**：

| 异常类型 | 检测规则 | 对应优化 |
|---------|---------|---------|
| `rewrite_rate_spike` | 某 speaker 的 post-TTS rewrite 比例超过历史 baseline + 2σ | 触发 Phase A 校准系数重新计算 |
| `translation_length_drift` | 某 speaker 的 `actual_chars / target_chars` 中位数偏离 1.0 超过 15% | 触发 Phase A 校准系数重新计算 |
| `cost_model_deviation` | LLM 成本超过 estimate_credits 预测值 2× | 触发 Phase B 选路规则审查 |
| `first_pass_alignment_degradation` | 全局对齐成功率低于 70% | 触发全链路参数调优 |

**Segment 效率评分函数**（非 LLM 规则引擎）：

```python
def evaluate_segment_efficiency(seg: DubbingSegment) -> dict:
    """计算单段效率分数，越高越好"""
    score = 100.0

    # 惩罚1: 需要重写（每需重写扣15分）
    if seg.pre_tts_rewrite_attempted:
        score -= 15
    if seg.alignment_method in ("rewrite", "force_dsp"):
        score -= 10

    # 惩罚2: 长度偏差（每偏离目标5%扣5分）
    error_pct = abs(seg.first_pass_error_pct or 0)
    score -= (error_pct / 0.05) * 5

    # 惩罚3: 高成本模型（gemini_pro扣10分）
    if "gemini_pro" in (seg.translation_model or ""):
        score -= 10

    # 奖励: 保留原说话人特征
    if seg.catalog_hit and seg.match_confidence > 0.8:
        score += 5

    return {"score": max(0, score), "segment_id": seg.segment_id}
```

**安全边界**（与 Hermes 设计文档一致）：
- Hermes 只读数据、只写报告，不修改任何配置
- Backend 是安全边界，Hermes 受控 API 读数据，不直连 DB
- 报告作为人工决策参考，确认后再手动调参
- 与 AGENTS.md 安全边界一致：不在无人审批情况下自动修改生产配置

---

### Phase E：前端使用仪表盘（长期，可延后）

当 Hermes 积累足够数据后，在 `(app)/usage/page.tsx`（当前是占位页）中展示：
- 个人翻译质量趋势（rewrite rate, alignment rate）
- 成本分布（LLM 调用次数/模型/费用）
- 与整体用户的百分位对比

纯前端消费，不涉及后端逻辑改动。

---

## 三、Segment 效率评分方法论

### 3.1 核心指标

| 指标 | 定义 | 来源 | 目标 |
|------|------|------|------|
| `rewrite_rate` | 需要预 TTS 重写的 segment 比例 | `DubbingSegment.pre_tts_rewrite_attempted` | < 15% |
| `alignment_rate` | 直接对齐（非 rewrite/force_dsp）的比例 | `DubbingSegment.alignment_method` | > 70% |
| `first_pass_error` | `abs(actual - target) / target` | `DubbingSegment.first_pass_error_pct` | < 10% 均值 |
| `cost_per_minute` | 每个源视频分钟的总 LLM + TTS 费用 | `UsageMeter.summarize()` | 按 plan tier 设定 baseline |
| `speaker_correction_precision` | Pass 1 纠正与人工审校一致的比率 | `speaker_correction_evaluator.py` | > 85% |

### 3.2 评分权重

```python
SEGMENT_EFFICIENCY_WEIGHTS = {
    "alignment_quality": 0.35,    # direct > dsp > rewrite > force_dsp
    "length_accuracy":  0.30,    # first_pass_error_pct 越小越好
    "model_cost":       0.20,    # cost_rank 越低越好
    "voice_match":      0.15,    # catalog_hit + match_confidence
}
```

单个 segment 效率分 = Σ(weight × normalized_dimension_score)，范围 0–100。

---

## 四、执行优先级

| 优先级 | Phase | 改动量 | 成本节省 | 质量提升 | 风险 |
|--------|-------|--------|---------|---------|------|
| P0 | A: 翻译长度自适应校准 | 小（~200行） | 中（减少重写/重合成 ≈ 减少 TTS 费用） | 高（更准确的字数目标 → 更好的对齐质量） | 低 |
| P1 | B: Segment 差异化选路 | 中（~300行） | 高（减少 Gemini Pro 调用量） | 中性（大多数 segment 质量不降） | 低 |
| P2 | C: 说话人归因闭环 | 中（~400行） | 小（减少低效 Pass 1 调用） | 中（更准确说话人 → 更好的配音质量） | 中（需要人工审校数据作为 ground truth） |
| P3 | D: Hermes Phase 1 | 大（~1500行 + DB migration + 前端页面） | 小（间接，通过更好的监控发现浪费） | 小（间接，通过更好的参数调优） | 高（独立平台维护成本） |
| P4 | E: 前端仪表盘 | 中（~500行前端组件） | 无 | 无（UX 提升） | 低 |

**建议本 sprint 执行 P0 + P1**，投入产出比最高：

| 指标 | P0 效果 | P1 效果 |
|------|---------|---------|
| 目标 | 更精准的翻译字数 → 更少重写 | 更便宜的模型 → 更低 LLM 成本 |
| 衡量 | rewrite_rate 下降 30-50% | Gemini Pro 调用量下降 60% |
| 风险 | 需积累足够样本后才生效 | deepseek 质量可能不足的边界 |

---

## 五、关于 Hermes 的执行建议

**为什么不建议现在启动 Hermes 全部实现：**

1. **当前项目主线仍在活跃收敛中**（`process.py` → `project_workflow.py` 迁移未完成），再加一个独立的观测平台会分散工程资源。
2. **Phase A-C 是 rule-first 的聚合逻辑**，不需要 LLM agent。先跑通数据反馈环路，再考虑引入 LLM 做智能分析。
3. **闭环反馈的"闭环"不在于 Hermes 是否运行**，而在于翻译参数是否真的被数据驱动更新。当前最大的价值是：把 `DubbingSegment` 中已有的 50+ audit 字段从"观测用"升级为"校准用"。

**Hermes 的渐进式引入路径：**

```
Step 1: 手工跑 P0 + P1（当前 sprint）
  → 验证数据反馈链路有效
  → 积累 baseline 数据（rewrite_rate, alignment_rate, cost_per_minute）

Step 2: 手工跑 P2（下个 sprint）
  → 积累 speaker correction precision 数据
  → 验证 ground truth（人工审校）质量

Step 3: 当手工分析的人力成本超过自动化开发成本时
  → 启动 Hermes Phase 1：自动化 collector → detector → analyst → publisher
  → 复用 P0-P2 的评估函数和数据格式
```

**Hermes 设计与本方案的衔接点：**
- `hermes_runs` 表 → 存储 Phase A 的校准参数快照和 Phase C 的 precision 趋势
- `hermes_insights` 表 → 存储 Phase D 异常检测结果
- Hermes 的 `collector` 角色 → 直接读取 `UsageMeter.events` 和 `DubbingSegment` audit 字段

---

## 六、风险评估

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 翻译长度校准系数在小样本下方差大 | 校准不稳定，反而增加重写率 | 最少 5 个 segment 才计算；低于阈值回退到全局 1.8 |
| deepseek 在长段技术内容上翻译质量不足 | 用户审校退修率上升 | 保留 gemini_pro 作为 rewrite_strict 的默认；初版只在 translate 任务分流 |
| 说话人校正 precision 计算缺乏 ground truth | precision 数值不可信 | P2 先输出 metric 不做自动调整；人工确认后再接入选路 |
| Hermes 运维成本拖慢主线 | 核心功能交付延期 | Phase D 标记"长期"，不分配当前 sprint 资源 |
| deepseek_v4_pro 的 thinking mode 干扰 | 响应格式变化导致解析失败 | `deepseek_provider.py` 已处理 v4 thinking mode 禁用；增加 JSON 格式校验 |

---

## 七、附录：当前代码引用索引

### 7.1 翻译长度控制相关

| 文件 | 关键函数/常量 | 行号 |
|------|-------------|------|
| `src/services/gemini/translator.py` | `_ENGLISH_TO_CHINESE_CHAR_RATIO = 1.8` | ~1 |
| `src/services/gemini/translator.py` | `_build_groups()` | ~2010 |
| `src/services/gemini/translator.py` | `_estimate_dynamic_target_chars()` | ~1800 |
| `src/services/gemini/translator.py` | `_batch_needs_length_retry()` | ~1600 |
| `src/services/tts/duration_estimator.py` | `count_spoken_chars()`, `TTSDurationEstimator` | 全文 |

### 7.2 模型选路相关

| 文件 | 关键函数/常量 | 行号 |
|------|-------------|------|
| `src/services/llm_registry.py` | `MODEL_REGISTRY`, `_DEFAULTS`, `get_prompt_model()` | 全文 |
| `src/services/gemini/translator.py` | `_call_task_with_fallback()` | ~1400 |
| `src/services/llm/providers/deepseek_provider.py` | `OpenAICompatDeepSeekProvider` | 全文 |

### 7.3 对齐与重写相关

| 文件 | 关键函数/常量 | 行号 |
|------|-------------|------|
| `src/services/alignment/aligner.py` | `SegmentAligner._attempt_rewrite_loop()` | ~385 |
| `src/services/alignment/aligner.py` | `_evaluate_alignment()` | ~200 |
| `src/services/gemini/rewriter.py` | `rewrite_for_duration_with_profile()` | ~50 |
| `src/utils/audio_fit.py` | `AudioFit.fit_to_target()` | 全文 |

### 7.4 说话人归因相关

| 文件 | 关键函数/常量 | 行号 |
|------|-------------|------|
| `src/services/transcript_reviewer.py` | `review_transcript()`, `_orchestrate_three_pass()` | ~700 |
| `src/services/transcript_reviewer.py` | `_PASS1_PROMPT` | ~1123 |
| `src/services/transcript_reviewer.py` | `_apply_low_support_speaker_verifier()` | ~3148 |
| `src/services/assemblyai/transcriber.py` | `_apply_3layer_split()` | ~346 |

### 7.5 成本计量相关

| 文件 | 关键函数/常量 | 行号 |
|------|-------------|------|
| `src/services/usage_meter.py` | `UsageMeter.record_llm()`, `record_tts()`, `summarize()` | 全文 |
| `gateway/credits_service.py` | `estimate_credits()`, `shadow_capture()` | 全文 |
| `gateway/credits_observability.py` | cost-metrics, provider-breakdown | 全文 |
