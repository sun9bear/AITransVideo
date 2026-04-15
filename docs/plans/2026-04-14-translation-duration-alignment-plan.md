# 翻译时长对齐系统化方案（方案 C+，v2.1 经 CodeX 两轮审核修订）

> **v2 变更**（2026-04-14）：基于 CodeX 第一轮审核反馈修订，关键修正：
> 1. 翻译目标不再用 `english_words × 1.8` 替代现有 `target_chars/min/max` 硬约束，降级为 `hint`
> 2. TTS speed 严格限幅，不再 `1/ratio` 动态算
> 3. 音色匹配语速维度改为"top-K 内重排"，不冲击现有全局权重
> 4. 明确 Phase 1/2 只覆盖 Studio 路径，Express 保留 probe fallback
> 5. Phase 3 多候选必须加"语义守门"
> 6. rewrite 率预期调整为更保守的数值
>
> **v2.1 变更**（2026-04-14）：基于 CodeX 第二轮审核反馈继续修订：
> 7. speed 决策统一用 `TTSDurationEstimator` 的 spoken-char 计数，避免标点带偏（口径一致性 P2）
> 8. 实施优先级强化"阶段性决策点"——Phase 1 后先验证收益，Phase 3 可延后
> 9. 翻译质量评估主看"术语/数字保留率 + 人工抽检"，BLEU 降级为辅助
> 10. Metrics 新增 `first_pass_duration_error_pct`，和最终误差区分，看清上游真实收益

## Context — 为什么做这件事

**核心目标**：让翻译后文本 TTS 出的音频时长，与原英文段落时长基本一致（误差小），同时不损失翻译质量，最大化自动化。

**当前痛点**：S5 rewrite 率 36-57%，原因是**多层误差累积**，没有任何一层主动消化：

| 误差层 | 当前情况 |
|-------|---------|
| 音色 chars/sec 校准 | probe 样本少（±10%） |
| LLM 字数遵循 | prompt 只给 min/max 黑盒（±15%） |
| **音色匹配不看语速** | **完全盲点**——可能给快节奏英文讲者挑到慢音色 |
| TTS 语速波动 | 无 per-segment speed 控制（±5%） |
| DSP atempo | 30%+ 时听感变得不自然 |

累积误差经常 >30%，DSP 硬拉听感崩坏。

**方案 C+ 的思路**：每一层只做自己擅长的、消化适量误差，不让任何一层背 30% 的锅。

```
音色匹配层：top-K 内选语速接近的音色（根源避免 ±10%+ 错配）
翻译层：保留现有时长边界（硬约束）+ 新增自然度 hint（软引导）
TTS 层：speed 参数限幅微调 [0.92, 1.08] 默认（抵消 ±8%）
DSP 层：atempo 收尾（±5%）
兜底层：rewrite + 警告用户换音色
```

---

## 关键架构决策（前置讨论 + CodeX 审核修订）

### 1. 翻译层：保留现有时长约束，新增自然度 hint（v2 重要修正）

**错误的原方案**（已废弃）：
> "翻译按英文词数 × 1.8 算字数，不再用音色速度强算"

**问题**：技术类、数字密集、专有名词密集的内容（例："一款搭载骁龙 8 Gen 4 处理器的手机"），固定系数 `1.8` 会让翻译漂移——要么过度精简丢信息，要么冗长超时。当前 `translator.py:1729` 附近的时长+密度+chars_per_sec 链路是成熟的，不能被简单系数替代。

**正确方案**（v2）：
```
保留（硬约束）:
  target_chars / min_chars / max_chars
    ← 基于 target_duration_ms × 音色 chars/sec × density_factor
    ← 现有成熟链路，是 TTS 能在目标时长内说完的真实边界

新增（软 hint）:
  target_chars_hint = english_words × 1.8
    ← 作为"自然中文长度参考值"
    ← prompt 里明确说明："这是内容密度正常时的自然长度"

LLM 同时看到两者，自己权衡:
  - 信息密度高时（技术/数字密集）：可以超过 hint，但不能超过 max_chars
  - 信息密度低时（口语化、感叹）：可以低于 hint，但不能低于 min_chars
  - 两者接近时：按 hint 写最自然
```

### 2. 音色匹配加语速维度：top-K 内重排（v2 重要修正）

**错误的原方案**（已废弃）：
> "加 W_SPEED = 0.10，从 texture/childlike/delivery 让出权重"

**问题**：直接改全量排序权重，可能把"人格/气质/年龄更匹配"的优质候选挤掉。语速匹配和音色适配不在同一个优先级层。

**正确方案**（v2，两阶段）：
```
Stage 1: 现有 8 维度全量打分 → 取 top-10 候选（权重不变）
Stage 2: 在 top-10 候选内，按 |cand_chars_per_sec - target_chars_per_sec| 二次排序
  - target_chars_per_sec = english_words_per_sec × 1.8
  - 语速差异 ≤5%：加分 0.10
  - 语速差异 5-15%：加分 0.05
  - 语速差异 15-30%：不加不减
  - 语速差异 >30%：减分 0.05（但不出局）
Stage 3: 取 Stage 2 重排后的 top-1
```

这样：主维度（人格/年龄/气质）保持稳定，语速只在"合格候选池"内择优。

### 3. Express 路径明确定界（v2 新增）

**现状**：`process.py:970` 明确写了 Express 模式下 `voice_id_a/b = None`，真实音色在 TTS 阶段自动匹配。

**本方案边界**：
- **Phase 1/2 收益只覆盖 Studio 已确认 voice_id 的路径**
- **Express 保留现有 probe fallback 不变**
- 未来如要让 Express 也受益，需要补"翻译前 auto-match 前移"逻辑，**不在本方案范围**

**理由**：贸然在 Express 翻译前做 auto-match，会破坏现有"翻译 → TTS 时再自动匹配"的架构，且对 Express 的快速体验有性能影响。留给后续专门方案处理。

### 4. 用户克隆音色：probe fallback + 可选手动标定
- **默认**：第一次使用时走现有 probe 校准路径（无缝）
- **可选**：音色库页面加"测试语速"按钮，用户显式触发标定（符合 CLAUDE.md "付费 API 不能自动调用"硬约束）

### 5. 手动选音色语速不匹配：选前显示 + 选后警告
- 音色选择面板每个卡片显示"4.3 字/秒（中速）"标签，并标注"原视频预估需要 X 字/秒"
- 选定后端验证：如果 |音色 chars/sec - 目标 chars/sec| > 30%，弹窗警告用户考虑换音色

### 6. 成本分析支持"多候选翻译"
- 翻译成本 ¥0.00022/段（DeepSeek Chat 批量 15 段）
- TTS 成本 ¥0.02-0.07/段
- **TTS / 翻译比例 = ~180×**
- 多候选让翻译成本 +¥0.00016/段，只要 rewrite 率降 0.4% 就回本

### 7. 分层衔接已成熟功能，复用不重复造轮子
- ✅ S2 Pass2 产出的**术语表**已经以"请严格遵循"硬约束注入翻译 prompt（`translator.py:480`），人名/专有名词一致性已解决，新方案不动它
- ✅ 当前翻译 prompt 已声明"用于中文 TTS 配音"和"目标配音时长与原英文一致"（`translator.py:109-141`）
- ✅ `GeminiRewriter` 已接收 `chars_per_second` 和 `chars_per_second_by_speaker`，方案只改上游传入值来源

---

## 三阶段实施路线

### Phase 1：基础设施（音色语速数据 + prompt 补上下文）
**工作量**：~1 周｜**预期 rewrite 率**：36-57% → **25-35%**（v2 保守调整）

**覆盖范围**：Studio 模式 voice_selection 已确认的 job（Express 保留现状）

1. **DB Migration 012** — `voice_catalog` 加 `chars_per_second`（Float）、`chars_per_second_by_model`（JSONB）、`speed_calibrated_at`（TZ）
2. **标定脚本** `gateway/scripts/calibrate_voice_speeds.py` + 标准文本文件 `standard_calibration_texts.py`
   - 三段标准文本：T1 101 字（科技评测）、T2 153 字（纪录片旁白）、T3 204 字（创业演讲），含不同情绪
   - 标定范围：MiniMax Turbo/HD × 81、CosyVoice flash × 65、VolcEngine 2.0 × 33，共 260 组，¥54.2 一次性
   - CLI：`--provider` / `--voice-id` / `--model` / `--dry-run` / `--force`
3. **voice_catalog 内部 API 返回 chars_per_second**（`gateway/voice_catalog_api.py:165`）
4. **pipeline 查表替代 probe**（仅 Studio 已确认 voice_id 路径；Express 保留 probe fallback）
5. **翻译 prompt 重构（v2 修正）**：
   - **保留现有字段**：`target_duration_seconds`、`target_chars`、`min_chars`、`max_chars`（硬约束不动）
   - **新增字段**：`english_words_per_second`（原说话人语速，给 LLM 判断内容密度）
   - **新增字段**：`catalog_chars_per_second`（音色预标定值，说明 min/max 的来源）
   - **新增字段**：`target_chars_hint`（= 英文词数 × 1.8，作为"自然度参考"，**不是硬约束**）
   - **加推导说明**：prompt 里明确告诉 LLM
     ```
     - target_chars_hint 是"内容密度正常时的自然中文字数参考"
     - min_chars ~ max_chars 是"TTS 能在目标时长内说完的字数边界"（硬约束）
     - 信息密度高时可超过 hint 逼近 max；口语化内容可低于 hint 逼近 min
     ```

### Phase 2：音色匹配加语速维度（top-K 内）+ TTS speed 限幅微调
**工作量**：~1 周｜**预期 rewrite 率**：25-35% → **15-20%**（v2 保守调整）

1. **`voice_reranker.py` 实现 top-K 内重排（v2 修正）**
   - 不改现有 8 维度权重
   - 新增 `rerank_by_speed(top_k_candidates, target_cps)` 函数
   - 语速差异打分规则：≤5% +0.10 / 5-15% +0.05 / 15-30% ±0 / >30% -0.05
   - 音色目录 chars_per_second 为 NULL 时跳过 Stage 2（平滑降级）
2. **`VoiceMatchRequest` 加 `target_chars_per_second` 字段**（`voice_match_types.py`）
3. **`process.py` 音色匹配调用点**（~1927-1942）传入 `english_words_per_second × 1.8` 作为目标 cps
4. **TTS 三引擎接入 speed 参数**：
   - **MiniMax**：`TTSConfig` 从全局 speed 改为 per-segment，payload `voice_setting.speed` 动态写入
   - **CosyVoice**：helper script 参数加 `rate`（0.5-2.0），`synthesize()` 签名加 speed 参数
   - **VolcEngine V3 单向流式**：**先写独立测试脚本验证** `audio_params.speech_rate`（-50~100）字段生效，通过后再接入主流程
5. **TTS 前 speed 决策逻辑（v2 严格限幅 + v2.1 统一字符计数口径）**：

   **字符计数口径**：必须用 `TTSDurationEstimator` 既有的 `_NON_SPOKEN_CHAR_PATTERN` 去掉标点/空格/格式符后再算，与 `duration_estimator.py:13` 和 `rewriter.py:77` 保持一致。不要用 raw `len(cn_text)`，否则 speed 决策会被标点带偏，和下游 rewrite/校准口径不一致。

   ```python
   # 复用现有 TTSDurationEstimator（内部已做 spoken-char 过滤）
   estimator = TTSDurationEstimator(chars_per_second=effective_cps)
   estimated_ms = estimator.estimate_duration_ms(cn_text)
   ratio = estimated_ms / target_ms
   
   # 默认模式（admin 可切到激进模式）
   SPEED_MIN, SPEED_MAX = 0.92, 1.08     # 默认：±8%
   # SPEED_MIN, SPEED_MAX = 0.85, 1.15   # 激进：±15%
   
   if abs(ratio - 1) <= 0.05:
       speed = 1.0  # DSP 收尾
   elif (1/SPEED_MAX) <= ratio <= (1/SPEED_MIN):  # 能被 speed 消化的范围
       speed = clamp(1 / ratio, SPEED_MIN, SPEED_MAX)
   else:
       # 超出 speed 能力 → 降级处理
       走 rewrite / 标记需人工审核 / 建议用户换音色
   ```

### Phase 3：多候选翻译 + 择优（v2 加语义守门）
**工作量**：~1 周｜**预期 rewrite 率**：15-20% → **8-12%**（v2 保守调整，不承诺 <5%）

> **定位**：锦上添花，不是止血第一刀。建议 Phase 1+2 观察效果后再决定是否做。

1. **翻译 prompt 改多候选输出**：每段产出 3 个候选（精炼/标准/丰满，字数约 0.85×/1.0×/1.15× 目标）
2. **JSON schema 扩展**：LLM 输出结构加 `information_completeness` 字段
   ```json
   {
     "id": "seg_1",
     "candidates": [
       {"text": "精炼版", "char_count": 20, "information_completeness": 9},
       {"text": "标准版", "char_count": 25, "information_completeness": 10},
       {"text": "丰满版", "char_count": 30, "information_completeness": 10}
     ]
   }
   ```
3. **语义守门（v2 新增）**：LLM prompt 硬约束：
   ```
   每个候选必须保留：
   - 人名/地名/专有名词（参照术语表）
   - 具体数字（百分比、时间、距离等）
   - 结论句 / 主旨
   
   information_completeness 评分：
   - 10：所有核心信息完整保留
   - 8-9：略去少量修饰但主旨完整
   - <8：有核心信息损失（比如删了数字或专有名词）
   ```
4. **择优算法**：
   ```
   # 满足语义守门的候选
   valid = [c for c in candidates if c.information_completeness >= 8]
   
   if not valid:
       # 所有候选都损失信息 → 标记此段人工审核
       mark_for_manual_review(segment)
       return best_by_duration(candidates)  # 退回按时长选
   
   # 在满足守门的候选中，选时长最接近目标的
   return argmin(|TTSDurationEstimator.estimate(c.text) - target_ms| for c in valid)
   ```
5. **批量大小调整**：从 15 段降到 8-10 段（output tokens × 3）
6. **兜底 rewrite**：择优后仍超 15% 才触发（极端情况）

### Phase 4：UX（用户可感知部分，和 Phase 2 并行）

1. **音色库页面加"测试语速"按钮**（可选克隆音色标定）
2. **音色选择面板显示语速标签**（"4.3 字/秒（中速）"+"原视频预估需要 Y 字/秒"）
3. **选音色后端验证**：差异 >30% 弹窗警告

---

## 关键文件清单（三阶段整合）

| 操作 | 文件 |
|------|------|
| 新建 | `gateway/alembic/versions/012_add_voice_speed_calibration.py` |
| 新建 | `gateway/scripts/calibrate_voice_speeds.py` |
| 新建 | `gateway/scripts/standard_calibration_texts.py` |
| 新建 | `scripts/test_volcengine_speech_rate.py`（Phase 2 前置验证） |
| 修改 | `gateway/voice_catalog_models.py`（加 3 列） |
| 修改 | `gateway/voice_catalog_api.py`（返回 CPS + admin 重标定端点） |
| 修改 | `src/services/tts/voice_reranker.py`（加 `rerank_by_speed()` top-K 重排函数） |
| 修改 | `src/services/tts/voice_match_types.py`（`VoiceMatchRequest` 加字段） |
| 修改 | `src/services/tts/voice_match_resolver.py`（传递字段 + 调用 top-K 重排） |
| 修改 | `src/services/tts/minimax_voice_selector.py` / `cosyvoice_voice_selector.py` / `volcengine_voice_selector.py` |
| 修改 | `src/services/tts/tts_generator.py`（MiniMax per-segment speed + 限幅决策） |
| 修改 | `src/services/tts/cosyvoice_provider.py` + helper（rate 参数） |
| 修改 | `src/services/tts/volcengine_tts_provider.py`（speech_rate，先实测） |
| 修改 | `src/pipeline/process.py`（查表 + speed 决策 + english_words_per_second 传递，仅 Studio 路径） |
| 修改 | `src/services/gemini/translator.py`（prompt 模板加 hint 字段 + Phase 3 多候选 JSON schema） |
| 修改 | `frontend-next/src/.../VoiceSelectionPanel.tsx`（语速标签 + 警告弹窗） |

---

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| VolcEngine V3 单向流式 `speech_rate` 字段未实测确认 | Phase 2 **第一步**写独立测试脚本验证；如不支持则 VolcEngine 跳过 TTS speed，只靠匹配层 + DSP |
| 翻译 prompt 加 hint 字段后 LLM 反而混乱 | prompt 里明确区分"硬约束"和"软参考"；灰度一小批 job 对比翻译质量（BLEU + 人工抽检）再全量 |
| TTS speed 限幅外频繁触发 rewrite | 监控 `speed_out_of_range_rate`；如果 >10% 说明限幅太严，admin 可切换激进模式 [0.85, 1.15] |
| 音色匹配 top-K 重排影响现有 job 选择结果 | 分步上线：先只对新 job 启用；灰度期间记录新旧匹配差异，有明显劣化时可一键关闭 |
| Phase 3 多候选质量守门失效（LLM 虚报 completeness=10） | Prompt 明确守门规则 + 抽检验证；下游加简单检查（数字/术语覆盖率） |
| Express 用户误以为也享受新优化 | 产品文档说明：Express 保留原 probe 流程，要精确时长建议用 Studio |
| 克隆音色没有目录值 | probe fallback 保底；可选"测试语速"按钮精化 |
| 标定费用（~¥54）是付费 API | 用户 dry-run 确认后手动触发；支持分 provider/分音色增量跑 |

---

## 验证方法

### 前置验证（Phase 2 开工前必做）

1. **VolcEngine speech_rate 字段实测**
   - 独立脚本 `scripts/test_volcengine_speech_rate.py`
   - 用同一段文本、同一音色，分别传 `speech_rate = -30 / 0 / 30`
   - 测量输出音频时长，验证字段是否真的生效
   - 结果写入方案文档附录，作为 Phase 2 Go/No-Go 依据

### 端到端对比测试

1. 选 5 个典型 job（不同内容类型：访谈/纪录片/演讲/教程/快节奏 vlog）
2. Phase 1 完成后：复跑，对比 rewrite 次数、**首轮时长误差**（DSP/rewrite 前）、**翻译质量指标**
3. Phase 2 完成后：同上对比 + speed 分布统计
4. Phase 3 完成后：同上，并统计"多候选中哪个被选中的分布" + completeness 评分分布

### 翻译质量评估口径（v2.1 CodeX 建议调整）

**主指标（重点看）**：
- **术语保留率**：S2 Pass2 产出的 glossary 中，翻译结果包含的比例（每段 100% 为达标）
- **数字保留率**：原文中数字/百分比/时间单位，翻译中原样或规范化保留的比例
- **人工抽检**：每批随机抽 10 段，按 1-5 分评估"准确性 + 自然度"

**辅助指标**（不作为主评判）：
- BLEU 分数（可选，内容类型差异大时易失真）

### Metrics 新增（`gateway/admin_job_monitor_api.py`）
- `rewrite_rate`：本 job rewrite 段数 / 总段数
- `first_pass_duration_error_pct`（v2.1 新增）：**首轮 TTS 时长误差**（DSP/rewrite 之前），用于识别上游翻译 + TTS speed 层的真实收益，不被下游补救淹没
- `final_duration_error_pct`：最终时长误差（经过所有补救后）
- `speed_param_distribution`：speed=1.0 / (0.92, 1.08) / 激进区间 / 超限的段数分布
- `voice_speed_mismatch_rate`：音色 chars/sec 和目标 chars/sec 偏差 >15% 的比例
- `term_preservation_rate`：术语表命中率（v2.1 新增）
- `number_preservation_rate`：数字保留率（v2.1 新增）
- `translation_quality_flag_rate`（Phase 3）：information_completeness < 8 被标记的段数比例

### 回归防护
- 保留 probe 校准路径作为任何新路径的 fallback（任一失败退回老流程）
- Admin 开关控制新功能启用：
  - `settings.voice_speed_calibration_enabled`
  - `settings.voice_speed_rerank_enabled`
  - `settings.tts_speed_adjustment_enabled`
  - `settings.tts_speed_mode`（`default` / `aggressive`）
  - `settings.multi_candidate_translation_enabled`

---

## 实施优先级

**强烈建议 Phase 1 → 2 → 3 顺序**，每阶段独立验证效果再决定是否进入下一阶段。

| 阶段 | 价值 | 建议 |
|------|------|------|
| Phase 1 | 最值钱的一刀 | 必做。预标定 + prompt hint，最可能直接把 rewrite 率砍下来 |
| Phase 2 | 次之 | 必做。语速感知匹配 + TTS speed 限幅微调，继续扩大收益 |
| Phase 3 | 锦上添花 | **Phase 1 完成后先看实测收益再定**。如果 Phase 1 的首轮时长误差和 rewrite 已明显下降，Phase 3 可延后甚至不做 |

**阶段性决策点（v2.1 CodeX 建议强化）**：
- **Phase 1 完成后**：跑一批典型 job 验证，看 `first_pass_duration_error_pct` 和 `rewrite_rate` 是否已达预期。若已明显好转，Phase 2 保留、Phase 3 延后观察。
- **Phase 2 完成后**：再次验证，如 rewrite 率已到 15-20% 区间，Phase 3 的多候选 + 语义守门可以不做（复杂度和收益不成比例时不必硬上）。

**不要一次性全做**：中间任一步走不通或效果不理想（比如 VolcEngine speech_rate 实测失败、翻译质量因 hint 下降），能及时止损、调整后续。

---

## 对 CodeX 审核反馈的修正汇总

### 第一轮（v2，6 条）

| # | CodeX 建议 | 本方案 v2 吸收方式 |
|---|-----------|------------------|
| 1 | english_words × 1.8 不能替代时长约束，只能做 hint | 架构决策 #1 + Phase 1 第 5 项重写，保留现有 target_chars，新增 target_chars_hint |
| 2 | speed 自动调节要严格限幅 | Phase 2 第 5 项重写，默认 [0.92, 1.08]，激进 [0.85, 1.15]，超出降级 rewrite |
| 3 | 语速维度不全局生效，先 top-K 内 rerank | 架构决策 #2 + Phase 2 第 1 项重写，两阶段重排 |
| 4 | Express 路径先确认翻译前是否知道最终音色 | 架构决策 #3 新增，明确 Phase 1/2 只覆盖 Studio 路径 |
| 5 | VolcEngine speech_rate 先单独实测 | Phase 2 第 4 项 + 新增 Phase 2 前置验证步骤 |
| 6 | 多候选翻译要加"语义守门" | Phase 3 第 3 项新增，information_completeness 评分 + argmin subject to >= 8 |

### 第二轮（v2.1，4 条）

| # | CodeX 建议 | 本方案 v2.1 吸收方式 |
|---|-----------|-------------------|
| 7 | speed 决策统一用 spoken-char 计数（P2） | Phase 2 第 5 项代码改为复用 `TTSDurationEstimator.estimate_duration_ms()`，与下游 rewriter/校准口径一致 |
| 8 | Phase 1 完成后先看真实收益，Phase 3 可延后 | 实施优先级新增"阶段性决策点"小节 |
| 9 | BLEU 降级为辅助指标，主看术语/数字保留率 + 人工抽检 + 首轮时长误差 | 验证方法新增"翻译质量评估口径"小节，主指标换为术语/数字保留率 |
| 10 | Monitor 加"DSP/rewrite 之前的首轮误差"指标 | Metrics 新增 `first_pass_duration_error_pct`，与 `final_duration_error_pct` 区分 |
