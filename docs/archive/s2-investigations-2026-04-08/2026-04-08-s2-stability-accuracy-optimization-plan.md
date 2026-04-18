# S2 审校阶段稳定性 / 准确率优化方案
> 日期：2026-04-08
> 状态：提案
> 目标：在**不大幅增加成本支出**、**不打断现有后续流程**的前提下，提升 S2 审校阶段的稳定性、准确率与可复盘性。

---

## 1. 背景

当前 S2 审校阶段是一次统一的大模型调用，输出：

- `speakers`
- `glossary`
- `corrections`

随后代码会继续做 deterministic 后处理，并把结果灌入后续链路：

- `corrections` 最终改写 transcript 真值
- `glossary` 进入 S3 翻译 prompt
- `speakers` 进入 speaker profile / voice matching / voice selection payload

这条链路已经能工作，但目前有 3 个现实问题：

1. **出错时不够可追踪**  
   当前有 debug artifact，但还缺一等结构化结果产物与稳定审计产物。

2. **单次调用任务过载**  
   当前 S2 在一次 prompt 里同时做 speaker 识别、transcript 修正、术语提取、风格/音色画像等多件事，容易互相干扰。

3. **多说话人信息传播不完整**  
   `speaker_c+` 的姓名在下游链路里仍可能静默丢失。

### 1.1 当前已落地的基础证据链

这份方案**不是从零开始设计证据链**。当前代码中，S2 已经有一层基础 debug artifact 能力：

- `transcript/s2_review_raw_response.json`
- `transcript/s2_review_speaker_diff.json`

对应实现入口在：

- `/D:/Claude/AIVideoTrans_Codex_web_mvp/src/services/transcript_reviewer.py`
- `/D:/Claude/AIVideoTrans_Codex_web_mvp/src/pipeline/process.py`

这两份文件的作用是：

- 保存 S2 大模型原始返回
- 保存 `original -> after_corrections -> after_sanity -> final` 的 speaker diff

因此，本方案里的 Phase 1 是**在现有 debug artifact 基础上继续扩展**，而不是推翻重做。  
新增的：

- `s2_review_result.json`
- `s2_review_audit.json`
- `s2_runs/<run_id>/...`

都应被视为对当前证据链的补强与结构化升级。

---

## 2. 已确认事实

这份方案只建立在**当前已验证的代码事实**上：

### 2.1 当前真正影响后续结果的主链

- `ReviewResult.lines`  
  是后续 transcript 真值的直接来源，会影响翻译分组、voice selection speaker 片段统计、media understanding payload。

- `ReviewResult.glossary`  
  是当前直接进入 S3 翻译 prompt 的结构化结果。

- `ReviewResult.speakers`  
  当前主要影响：
  - speaker profile 注入到 segments
  - voice matching / auto-match
  - voice selection payload 的 speaker profile 展示

### 2.2 `style / role` 不是当前下游主真值

`role / style` 当前最明确、最直接的消费者仍是 S2 内部的 `_resolve_interview_roles()`。  
它们**不是**当前 S3 翻译 prompt 的直接输入。

因此，后续优化中不应继续把 `role / style` 视作当前最核心的 downstream truth。

### 2.3 已修复的 2-speaker 残留问题不再应作为当前主设计前提

此前“3+ speakers 被误当成 2-speaker interview 处理”的问题，已经在当前代码中通过“先看 transcript 实际 speaker 数量”修正。  
因此，这份方案不再围绕那个旧 bug 做架构设计，而是聚焦**当前仍然存在的稳定性 / 准确率问题**。

### 2.4 当前已有 debug artifact，后续方案必须兼容并复用

当前已存在的：

- `s2_review_raw_response.json`
- `s2_review_speaker_diff.json`

不是临时排障副产物，而是当前 S2 证据链的已落地基础设施。  
因此后续任何开发和评审都应理解为：

- 这些文件继续保留
- `s2_review_result.json` 与 `s2_review_audit.json` 是在其之上的新增层
- `s2_runs/<run_id>/...` 是把“latest snapshot”升级为“可比较的 run history”

---

## 3. 优化目标

本方案追求的不是“把 S2 一次性重写成全新架构”，而是用最小扰动实现：

1. **更稳定**
   - 出错时能明确知道是模型原始输出错、post-pass 错，还是日志表象错
   - 多说话人信息不会在下游静默丢失

2. **更准确**
   - 降低 speaker correction 被其他任务干扰的概率
   - 对高风险 speaker flip 增加小成本二次确认

3. **成本可控**
   - 不先引入全量双调用 / 三调用
   - 不把所有任务都升级成更贵的全局复核

---

## 4. 总体策略

优化顺序分为 3 个阶段：

### Phase 1：先补证据链与中间产物

目标：让 S2 变成**可审计、可复盘**的节点。

内容：

- 新增 `transcript/s2_review_result.json`
- 新增 `transcript/s2_review_audit.json`
- 保留并继续使用：
  - `transcript/s2_review_raw_response.json`
  - `transcript/s2_review_speaker_diff.json`
- 完整保留 `speaker_c+` 的姓名信息，并传入后续链路

收益：

- 不直接增加模型成本
- 为后续所有提准度动作提供稳定证据链

### Phase 2：不拆调用，先瘦身当前 S2 prompt

目标：在**保持单次 multimodal 调用**的前提下，让模型把注意力优先放到最关键的任务上。

内容：

- 保留一次调用，不新增音频 token 主成本
- 重写 prompt 优先级：
  - 必做：`correct_speaker`、`fix_text`、`merge/split`、`glossary`、`name/gender/age_group/voice_description`
  - 降权：`role`、`style`
- 增加明确规则：
  - 不确定时不要改 speaker
  - 不要仅凭身份猜测重分配 speaker
  - merge / split 只做保守修改

收益：

- 减少任务互相争 attention
- 不需要引入新阶段、新 UI、新协议
- 成本基本不变，甚至 prompt 变短后可能略降

### Phase 3：只给高风险 speaker 修正加“小 verifier”

目标：只对最贵的错误类型增加小成本保护，而不是全量第二次审校。

内容：

- 不做全量第二次 S2
- 只在高风险场景触发一个轻量 verifier
- verifier 只检查：某次 `correct_speaker` 是否成立

建议触发条件：

- 出现 `correct_speaker`
- batched review
- 实际 speaker 数较多
- sanity check 发生 flip

建议 verifier 输入：

- 候选 correction
- 该 line 的局部上下文
- 当前 `speakers` 结构化结果

建议 verifier 输出：

- `accept`
- `reject`
- `uncertain`

收益：

- 增量成本小
- 直接拦住最影响体验和后续成本的 speaker 误改

---

## 5. 为什么当前不直接拆成 S2-A / S2-B / S2-C

完整拆分并不是错误方向，但当前不建议优先做，原因有 4 个：

1. **会改动阶段时序**  
   需要重新定义 speaker review 与 transcript correction 的先后关系。

2. **会改动缓存恢复与 review state 逻辑**  
   当前 S2 结果已经和后续 payload / segment profile 链路耦合，直接拆会动到较多节点。

3. **会扩大验证面**  
   不是只验证准确率提升，还要验证流程兼容、阶段切换、恢复逻辑、UI 行为。

4. **当前证据链还不够完整**  
   在 `s2_review_result.json + audit` 还没落地之前，直接大拆会让“到底是哪一步变好/变坏”更难判断。

因此，完整 S2-A / S2-B / S2-C 拆分应视为**后续候选方案**，而不是当前第一优先级。

---

## 6. 各阶段详细设计

### 6.1 Phase 1：结构化结果与稳定审计

#### 新增产物 1：`transcript/s2_review_result.json`

作用：

- 保存 S2 结构化结果真值
- 让 `speakers / glossary / raw_corrections` 不再只是短暂内存值

建议包含：

- `version`
- `review_model`
- `has_audio`
- `speakers`
- `speaker_names`
- `glossary`
- `raw_corrections`
- `corrections_applied`
- `line_counts`
- `artifacts`

#### 新增产物 2：`transcript/s2_review_audit.json`

作用：

- 替代当前按位置 `zip(old_lines, new_lines)` 的脆弱日志
- 用稳定 `line_uid` 记录 correction / sanity / split / merge 的应用过程

建议事件类型：

- `correct_speaker`
- `fix_text`
- `merge`
- `split`
- `sanity_check_flip`
- `reindex`

#### 新增能力：每次 S2 运行单独留档

如果后续要评估“prompt 改了之后是否真的更准、更稳”，只保留 latest snapshot 不够，必须保留 **run history**。

建议目录结构：

- `transcript/s2_runs/<run_id>/raw_response.json`
- `transcript/s2_runs/<run_id>/speaker_diff.json`
- `transcript/s2_runs/<run_id>/review_result.json`
- `transcript/s2_runs/<run_id>/audit.json`
- `transcript/s2_runs/<run_id>/metrics.json`
- `transcript/s2_runs/<run_id>/meta.json`

同时保留当前 latest 便捷入口：

- `transcript/s2_review_raw_response.json`
- `transcript/s2_review_speaker_diff.json`
- `transcript/s2_review_result.json`
- `transcript/s2_review_audit.json`

其中 latest 文件可以视为“最近一次运行的复制或软链接等价物”，而 `s2_runs/<run_id>/...` 才是长期评估基线。

#### `meta.json` 建议字段

- `run_id`
- `timestamp`
- `review_model`
- `prompt_version`
- `code_version`
- `has_audio`
- `speaker_count`
- `line_count`
- `input_fingerprint`

#### `metrics.json` 建议字段

- `raw_corrections_count`
- `applied_corrections_count`
- `correct_speaker_count`
- `merge_count`
- `split_count`
- `fix_text_count`
- `sanity_flip_count`
- `final_line_count`
- `speaker_name_count`

目标不是一开始就做复杂 BI，而是先把后续比较所需的最小指标稳定留档。

> 当前建议：这部分**不作为第一轮推荐执行内容**。  
> 推荐先完成 latest 结构化产物与轻量 audit；`s2_runs/<run_id>/...`、`meta.json`、`metrics.json` 作为后续增强项，在真正进入 benchmark 对比阶段时再补。

#### `speaker_c+` 姓名全链保留

最小实现方式：

- 在 `process.py` 中维护完整 `review_speaker_names: dict[str, str]`
- 不再只覆盖 `speaker_a` / `speaker_b`
- 至少应用到：
  - `voice_selection_review` payload
  - `translation/segments.json` 的 `display_name`
  - 其他依赖 segment display name 的 review 展示链路

#### 预计成本影响

- 模型成本：接近 0
- 工程成本：低到中
- 风险：低

---

### 6.2 Phase 2：当前单次 S2 prompt 的瘦身与聚焦

#### 核心原则

- 保留单次 multimodal 调用
- 不增加一轮完整音频审校
- 提升主任务优先级，降低次任务干扰

#### Prompt 优化方向

把任务分成两层：

**核心任务（强约束）**

- 识别说话人身份基础字段：
  - `name`
  - `gender`
  - `age_group`
  - `voice_description`
- 输出 `glossary`
- 输出 `corrections`
  - `correct_speaker`
  - `fix_text`
  - `merge`
  - `split`

**次级任务（弱约束 / 可空）**

- `role`
- `style`

#### 需要新增的 prompt 约束

- 如果 speaker correction 没有足够依据，则不要输出该 correction
- 不要仅凭人物身份猜测 speaker 重分配
- 如果无法确认，则保持原 speaker
- `merge` / `split` 只做非常保守的修改

#### 为什么这一步性价比高

- 不需要改下游协议
- 不需要改 review 阶段顺序
- 不需要显著增加 token 成本
- 直接减少“任务互相拖累”的概率

#### 预计成本影响

- 模型成本：基本不变
- 工程成本：低
- 风险：低到中

---

### 6.3 Phase 3：高风险 speaker correction verifier

#### 目标

只用很小的额外成本，保护最容易产生高代价回滚的问题：speaker 误改。

#### 不做什么

- 不全量二次审校
- 不再次上传整段音频
- 不重做全文修正

#### 建议触发条件

- 当前 batch 中存在 `correct_speaker`
- batched review 模式
- 实际 speaker 数较多
- `sanity_check_flip` 数量大于 0

#### 建议输入

- 当前 line 与前后少量上下文
- 候选 correction
- 当前 `speakers` 结构化结果
- 可选：局部文本窗口，不重复上传整段音频

#### 建议输出

- `accept`
- `reject`
- `uncertain`

#### 策略

- `accept`：允许应用 correction
- `reject`：丢弃该 correction
- `uncertain`：默认保守，维持原 speaker，不做 flip

#### 预计成本影响

- 模型成本：小幅增加，但只发生在高风险 case
- 工程成本：中
- 风险：中

---

## 7. 推荐实施顺序

### 当前推荐执行版本

当前最推荐的不是完整执行全部三阶段细节，而是先按下面这条**收敛后的止血顺序**推进：

1. 修 `speaker_c+` 姓名全链保留
2. 确认并默认启用当前 S2 debug artifact 输出
3. 新增 `s2_review_result.json`
4. 新增**轻量版** `s2_review_audit.json`
5. 给当前单次 S2 prompt 增加“**不确定时不要改 speaker**”约束

这一版的特点是：

- 不拆阶段
- 不改 review state 时序
- 不引入 run-history 平台化
- 不立即做 verifier
- 先用最小工程改动提升定位能力和保守性

后续只有在这版跑出足够证据后，才继续推进更重的：

- 更完整的 prompt slimming
- 条件式 verifier
- `s2_runs/<run_id>/...` 历史留档

### 为什么推荐先这样做

相较于完整拆分或一次性铺开所有增强，这条顺序更适合当前阶段：

- 能最快止血真实 bug 与信息丢失问题
- 能把“模型原始输出 / corrections / sanity / final”之间的关系看清楚
- 能以极低成本让 S2 speaker correction 更保守
- 不会把验证面一下子扩大到阶段切换、缓存恢复、UI 协议

因此，下面的 `P0 / P1 / P2` 应理解为**完整路线图**，而“当前推荐执行版本”是它的第一轮收敛落地形式。

### P0：先做证据链与姓名保留

范围：

- `s2_review_result.json`
- `s2_review_audit.json`
- `speaker_c+` names 全链保留

理由：

- 这是后续所有提准度动作的基础
- 几乎不增加模型成本

### P1：再做 prompt slimming

范围：

- 保留单调用
- 收紧主任务
- 降权次任务

理由：

- 这是当前最值得做、且成本最低的提准度动作

### P2：最后做条件式 verifier

范围：

- 只拦高风险 speaker flip

理由：

- 成本可控
- 对最贵错误最有效

---

## 8. 验收标准

### 8.1 Phase 1 验收

S2 结束后，`transcript/` 下稳定存在：

- `s2_review_raw_response.json`
- `s2_review_speaker_diff.json`
- `s2_review_result.json`
- `s2_review_audit.json`

并且每次运行都有独立历史目录：

- `transcript/s2_runs/<run_id>/raw_response.json`
- `transcript/s2_runs/<run_id>/speaker_diff.json`
- `transcript/s2_runs/<run_id>/review_result.json`
- `transcript/s2_runs/<run_id>/audit.json`
- `transcript/s2_runs/<run_id>/metrics.json`
- `transcript/s2_runs/<run_id>/meta.json`

并且：

- 可以明确看出模型原始 correction
- 可以明确看出 post-pass 又改了什么
- `speaker_c+` 的名字在 payload / segments 中不再静默丢失
- 可以按 `run_id` 比较不同 prompt / code 版本下的 S2 输出变化

### 8.1.1 效果评估准备度验收

为后续评估“修改后的效果是否显著”做准备，至少需要满足：

- `meta.json` 中有 `prompt_version`
- `meta.json` 中有 `code_version`
- `metrics.json` 中有最小 correction / sanity / speaker 统计
- 同一批 benchmark job 可以跨多个 `run_id` 重复比较

### 8.2 Phase 2 验收

在不增加额外大模型调用次数的前提下：

- `correct_speaker` 的总输出更保守
- 无依据的 speaker flip 明显减少
- glossary 与 speaker profile 的可用性不显著下降

### 8.3 Phase 3 验收

在高风险 case 上：

- speaker 误改率下降
- verifier 的额外成本可接受
- 不显著拖慢整体处理时长

---

## 9. 成本与收益判断

| 阶段 | 准确率收益 | 稳定性收益 | 模型成本增幅 | 工程风险 |
|------|------------|------------|--------------|----------|
| Phase 1 | 间接 | 极高 | 近乎 0 | 低 |
| Phase 2 | 高 | 中高 | 近乎 0 | 低到中 |
| Phase 3 | 中到高 | 高 | 小幅、条件触发 | 中 |

结论：

- **最先该做的是 Phase 1 + Phase 2**
- **最不该先做的是完整大拆分**

---

## 9.1 如何评估“是否显著变好”

本方案不要求第一天就建立复杂统计平台。  
当前推荐执行版本只要求先把 latest 结构化结果和轻量 audit 做好；如果后续确认要做 benchmark 对比，再补 `s2_runs/<run_id>/...`、`meta.json`、`metrics.json`。

建议最小评估口径：

1. 固定一组 benchmark 视频  
   覆盖：
   - 1-speaker
   - 2-speaker
   - 3+ speakers
   - interview / panel / news / commentary 等不同类型

2. 固定比较维度
   - `correct_speaker_count`
   - `sanity_flip_count`
   - 用户后续手动改 speaker 次数
   - `speaker_c+` 姓名保留完整率
   - reviewer 主观判定的 speaker 误改率

3. 固定比较对象
   - prompt slimming 前 vs 后
   - verifier 前 vs 后
   - 同一 job 的不同 `run_id`

这样后续我们讨论“有没有显著改善”时，不再依赖个别案例印象，而是可以基于 run history 和固定 benchmark 做对比。

---

## 10. 结论

如果目标是“让 S2 审校阶段的成效更稳定、更准确，但又不能大幅度提高成本支出”，最合理的路线不是直接把 S2 重构成多阶段大系统，而是：

1. 先把 S2 变成**可观测、可审计、N-speaker 信息不丢**的节点
2. 再让当前单次 multimodal 调用**更聚焦主任务**
3. 最后只给高风险 speaker correction 增加**小成本 verifier**

这条路线的特点是：

- 准确率提升有抓手
- 成本上升可控
- 对现有主流程冲击最小
- 每一步都能独立验证和回退

因此，本方案建议作为当前 S2 优化的正式主线方案。
