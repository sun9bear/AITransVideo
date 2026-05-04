# 用户修改行为审计数据与质量优化方案

- 创建日期：2026-05-04
- 适用范围：翻译审校、音色选择、视频修改 / post-edit、二次合成
- 关联方案：
  - `docs/plans/2026-04-18-studio-post-edit-plan.md`
  - `docs/plans/2026-04-24-video-translation-quality-cost-optimization-plan.md`
  - `docs/graphs/GITNEXUS_REVIEW_GRAPH.md`
  - `docs/graphs/GITNEXUS_EDITING_POST_EDIT_GRAPH.md`
- 状态：方案草案，建议作为 P2 说话人识别优化的观测闭环补充项

## 1. 背景与核心判断

当前系统已经有较完整的中间产物、事件日志、S2 speaker diff、TTS rewrite / DSP 数据，以及 post-edit 工作区。但用户在审校和修改视频时产生的“人工修正行为”还没有被完整结构化记录。

这些用户修改行为非常有价值。它们不是普通点击日志，而是用户对模型输出的真实纠错，天然接近弱标注数据：

- 用户把某段从 `speaker_b` 改成 `speaker_a`，说明说话人归属可能错误。
- 用户拆分一个段落，并给拆分后两段不同说话人，说明原始 ASR / S2 / SemanticBlock 把多人轮流说话合并错了。
- 用户把某段设为 `keep_original`，说明该段可能是观众互动、背景声、掌声、音乐、非对白，或不值得翻译配音。
- 用户改文本后重新 TTS，说明翻译质量、长度控制、口语化或时间匹配存在问题。
- 用户反复换音色或克隆音色，说明自动音色推荐质量不足。

因此，本方案目标不是单纯“留操作日志”，而是把用户修改转化为可统计、可回放、可训练 prompt / 规则 / verifier 的质量数据闭环。

## 2. 当前已有记录能力

### 2.1 已经有较明确历史的部分

音色选择阶段已经有局部历史：

- `speaker_reassignment_history`：记录用户在音色选择阶段把某个原音片段从一个 speaker 改到另一个 speaker。
- `dubbing_mode_history`：记录用户把某段设置为配音、保留原音等模式。

这些记录写在 `review_state.json` 的 `voice_selection_review.payload` 中，适合短期继续使用。

### 2.2 只有最终状态、缺少操作轨迹的部分

翻译审校阶段：

- 可以改 `segmentSpeakers`
- 可以 split segment
- approve 时会把结果写回项目数据
- 但缺少 append-only 历史，难还原“用户先改了什么、后来是否撤销”

视频修改 / post-edit 阶段：

- `editor/editing/segments.json` 保存当前编辑段落状态
- `editor/editing/segment_status.json` 保存 dirty / accepted 等状态
- `editor/editing/voice_map.json` 保存当前音色覆盖
- `editor/editing/tts_segments_draft/*.wav` 保存草稿 TTS
- commit 后 `editor/editing/` 会被清理，因此很多过程数据会消失

结论：现有数据足够支撑部分复盘，但不足以系统分析用户修改行为，也不足以长期积累弱标注样本。

## 3. 目标

1. 完整记录用户在 review / post-edit 中的关键确认动作。
2. 区分“临时试错”和“最终有效修改”。
3. 将用户修改与原始模型输出、ASR/S2 结果、音频时间范围、最终 TTS/DSP 结果关联。
4. 形成可离线统计的数据集，用于优化：
   - S2 说话人识别
   - 短段 / 插话 / 观众互动处理
   - overlap suspected 标记
   - SemanticBlock 切分
   - 翻译长度控制
   - 自动音色推荐
   - keep original 策略

## 4. 事件记录原则

### 4.1 Append-only

用户行为必须追加记录，不覆盖历史。最终状态文件仍然保留，但分析以审计事件流为准。

### 4.2 分 proposal 与 effective

用户在 UI 上打开拆分面板、拖动滑块、切换下拉框，属于临时操作，不一定需要记录。

只有服务端接受并写入状态的动作记录为 `proposed` 或 `confirmed`。

真正进入质量分析的数据，建议以 `effective=true` 为准：

- speaker change 被保存并进入 approve / commit
- split 后新段落完成 re-TTS 且被 accept
- split 后用户 commit 成功
- dubbing mode 修改进入最终 pipeline

这样可以避免把用户试错误判为模型错误。

### 4.3 原始值与新值必须双写

每条事件都要记录：

- 变更前值
- 变更后值
- 关联 segment 的原始时间范围
- 原始 speaker / 当前 speaker
- source_text / cn_text 的摘要或 hash
- 事件发生阶段

只记录“改成了 speaker_a”不够，必须知道“从谁改过来”和该段原始上下文。

### 4.4 可脱敏

分析不一定需要完整文本，尤其长期汇总时可以只保留：

- text hash
- 字符数
- duration
- speaker ids
- diff type
- 片段前后 1-2 个邻居的结构特征

需要人工抽查时，再回到项目产物查看文本和音频。

### 4.5 effective marker 策略

`user_edit_events.jsonl` 必须保持 append-only。不要为了把历史事件从 `effective=false` 改成 `effective=true` 而回写旧行。

建议做法：

- 用户修改、拆分、重合成、discard 等动作先写普通事件，默认 `effective=false`
- `translation_review_approved`、`voice_selection_approved`、`post_edit_draft_tts_accepted`、`post_edit_committed` 这类确认动作追加一条 `effective_marker` 事件
- `effective_marker.context.event_ids` 记录被确认生效的事件 id 列表；如果事件量太大，可记录 `session_id + stage + before_event_id / after_event_id` 范围
- 离线 parser 在读取事件流时，根据 marker 计算最终 effective 状态
- cancel 不回退已经 accepted / committed 的事件；但当前 session 中未被 marker 覆盖的修改应保留为 `confirmed_but_not_effective` 或 `discarded` 反例

这样既保留 append-only 语义，也能区分临时试错、用户确认、最终进入输出结果三层状态。

## 5. 建议新增数据文件

### 5.1 项目内审计文件

每个项目新增：

```text
project_dir/
  audit/
    user_edit_events.jsonl
    user_edit_summary.json
```

`user_edit_events.jsonl` 是 append-only 事件流。每行一条 JSON。

`user_edit_summary.json` 是可重建的汇总快照，用于管理后台和 benchmark 快速读取。它不是事实源。

### 5.2 为什么不只写 job events

当前 `JobEvent` 更适合展示“关键进展”，例如 `editing.entered`、`editing.commit_started`。用户逐段修改会非常多，如果全部塞进现有 job events：

- 工作区日志会变噪
- 管理员排障视图会被污染
- 数据 schema 难扩展

建议：

- `job_events` 继续记录生命周期摘要
- `audit/user_edit_events.jsonl` 记录细粒度质量数据
- 必要时 job event 的 payload 只挂汇总，例如 `speaker_change_count=7`

### 5.3 与 `JobEvent` / `UsageMeter` 的边界

这里新增 `user_edit_events.jsonl` 是有意引入第三类事实源，不是绕开既有审计体系：

- `JobEvent` 是任务生命周期和排障视图的事实源，面向用户进度、管理员排障、关键失败定位
- `UsageMeter` 是费用、扣点、API 调用消耗的事实源，面向账务一致性
- `user_edit_events.jsonl` 是用户行为和质量反馈的事实源，面向离线质量分析、speaker 纠错、流程优化

三者不能互相吞并。把高频逐段修改写进 `JobEvent` 会污染进度和排障视图；把用户行为写进 `UsageMeter` 会让费用真源承担非账务语义。合理做法是各自保持单一职责，再用 correlation key 做关联分析。

### 5.4 费用审计关联

当一次用户编辑触发付费调用时，行为事件和费用事件必须可关联：

- `user_edit_events.jsonl` 事件保留 `usage_event_ids: []`
- 如果当前调用链能拿到 `UsageMeter` 事件 id，就在行为事件中写入
- 如果 P0 暂时拿不到，可以先写空数组，P1 dataset builder 再通过 `job_id + segment_id + created_at window + provider` 尝试回填
- 后续也可以在 `UsageMeter` 侧补 `triggered_by_audit_event_id`，但费用侧字段只能作为反向索引，不能替代行为事件

这样才能回答“用户改一次 speaker / 重合成一次 TTS 实际增加了多少成本”，也能评估 P2 verifier、多候选翻译、keep_original 策略是否足以抵消重合成和人工修正成本。

### 5.5 写入边界

P0 实现时，审计 append 只应发生在 Job API / JobService 所在进程内，例如 `src/services/jobs/*` 的 mutation handler 或 service method 完成后。Gateway 只做鉴权、透传、状态同步，不应为了写用户编辑审计而 `import services.jobs.events` 或依赖会拉入音频库的 jobs 模块。

如果未来某个 mutation 被迁移成 Gateway 原生端点，应使用 `gateway/storage/event_log.py` 同类的 stdlib-only writer 单独落盘，不能让 Gateway 反向依赖 Job API 的音频处理栈。

## 6. 统一事件结构

基础 schema：

```json
{
  "event_id": "uuid",
  "schema_version": 1,
  "job_id": "job_xxx",
  "root_job_id": "job_root",
  "project_id": "project_dir_name",
  "created_at": "2026-05-04T12:00:00+08:00",
  "stage": "translation_review | voice_selection_review | post_edit",
  "event_type": "segment_speaker_changed",
  "actor": {
    "type": "user",
    "user_id_hash": "optional"
  },
  "segment": {
    "segment_id": "segment_003",
    "source_index": 3,
    "start_ms": 12345,
    "end_ms": 15678,
    "duration_ms": 3333
  },
  "before": {},
  "after": {},
  "context": {},
  "effective": false,
  "effective_reason": null,
  "usage_event_ids": []
}
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `event_id` | 幂等和排重 |
| `schema_version` | 事件结构版本，第一天就写入，离线解析必须 forward-compatible |
| `job_id` | 当前任务 |
| `root_job_id` | 同源视频多次重跑 / copy_as_new 归并分析 |
| `stage` | 发生在哪个用户流程 |
| `event_type` | 事件类型 |
| `actor` | 操作者信息；如记录用户 id，只写不可逆 hash |
| `segment` | 统一段落定位 |
| `before / after` | 修改前后 |
| `context` | 用于分析的上下文特征 |
| `effective` | 是否已经进入最终有效样本 |
| `effective_reason` | 例如 `committed`, `approved`, `tts_accepted` |
| `usage_event_ids` | 本行为触发的费用事件 id 列表，可为空 |

`actor.user_id_hash` 的建议生成方式是 `sha256(user_id + per-deployment salt)`。salt 从环境变量读取，不写入审计文件。这样可以做跨项目去重和用户级统计，但不能从审计文件反推真实用户 id。

## 7. 需要记录的事件

### 7.1 翻译审校阶段

#### `translation_segment_speaker_changed`

用户在翻译审校中更改某段 speaker。

```json
{
  "event_type": "translation_segment_speaker_changed",
  "stage": "translation_review",
  "segment": {"segment_id": "12", "start_ms": 10000, "end_ms": 18000},
  "before": {"speaker_id": "speaker_b"},
  "after": {"speaker_id": "speaker_a"},
  "context": {
    "source_text_chars": 86,
    "cn_text_chars": 34,
    "asr_speaker_id": "speaker_b",
    "s2_speaker_id": "speaker_b",
    "neighbor_speakers": ["speaker_a", "speaker_a"]
  },
  "effective": false
}
```

#### `translation_segment_split_confirmed`

用户确认拆分一个翻译审校段。

记录：

- 原段 id / 时间范围
- source/cn 拆分位置
- 两个子段 speaker
- 两个子段文本长度
- 原段 speaker
- 是否子段 speaker 不同

关键分析价值：

- 如果很多拆分都发生在同一类长段，说明 ASR utterance 或 SemanticBlock 切分有问题。
- 如果 split 后 A/B speaker 不同，说明原流程漏掉了短插话或 speaker turn。

#### `translation_review_approved`

approve 时追加汇总事件：

```json
{
  "event_type": "translation_review_approved",
  "stage": "translation_review",
  "context": {
    "speaker_change_count": 5,
    "split_count": 2,
    "text_edit_count": 14,
    "changed_segment_ratio": 0.21
  },
  "effective": true,
  "effective_reason": "approved"
}
```

approve 时按 §4.5 追加 `effective_marker`，关联本阶段被确认生效的修改事件。

### 7.2 音色选择阶段

已有事件可以标准化为：

#### `voice_selection_speaker_reassigned`

对应当前 `speaker_reassignment_history`。

增强字段：

- from speaker / to speaker
- 片段 duration
- 该片段音量 / RMS / speech probability（如果可得）
- 该段在 speaker 中的占比
- 是否短段
- 是否疑似观众互动

#### `voice_selection_dubbing_mode_changed`

对应当前 `dubbing_mode_history`。

模式建议：

- `dub`：翻译配音
- `keep_original`：保留原音
- `mute_or_background`：背景声 / 音乐 / 掌声，不参与配音

分析价值：

- 高频 keep_original 的片段，是训练“非对白 / 观众互动 / 背景声”规则的关键样本。
- 如果某类短段总被用户保留原音，后续可在 S2 / voice selection 自动标风险。

#### `voice_selection_approved`

记录本阶段最终摘要：

- speaker 数量
- 用户重分配次数
- keep_original 段数 / 时长
- 背景声段数 / 时长
- 每个 speaker 的段数 / 时长 / 占比

### 7.3 视频修改 / post-edit 阶段

#### `editing_session_started`

进入修改视频阶段时写一条 baseline marker。它不是用户修改，不应当被当作“纠错样本”，但它是后续所有 before / after 对比的锚点。

记录：

- baseline segment count / speaker count
- 每个 speaker 的段数、总时长、占比
- 当前 `editor/tts_segments` 的 manifest-level 单一 hash 摘要
- 是否发生 legacy lazy backfill，例如 `tts/*_aligned.wav -> editor/tts_segments/`

音频指纹不要默认写每段明细列表，避免一个 100+ 段 job 的 baseline marker 过大。推荐对 `{segment_id, audio_path, duration_ms, size, mtime 或 content_hash}` 的有序 manifest 计算一个单一 hash；需要深查时再回项目目录读取原始文件。

legacy 任务进入 editing 时的 lazy backfill 是系统补齐行为，不写成 user edit；只通过这条 marker 标明基线状态。

#### `post_edit_text_changed`

用户修改 `source_text` 或 `cn_text`。

记录：

- 字符数前后变化
- 是否超过 / 低于 duration budget
- 是否随后 re-TTS
- re-TTS 后是否 accept

用于优化：

- 翻译质量
- 长度控制
- 哪类段落需要更强约束或人工提示

#### `post_edit_segment_speaker_changed`

用户在 post-edit 里改 speaker。

这是 P2 说话人优化最核心的数据之一。

必须记录：

- from / to speaker
- 原始 ASR speaker
- S2 修正后的 speaker
- voice selection 阶段是否已改过
- 当前段前后邻居 speaker
- segment duration
- source_text / cn_text 长度
- 是否随后 re-TTS 并 accept

#### `post_edit_segment_split_confirmed`

用户确认拆分。

记录：

- 原段 id
- 新 segment ids
- split source/cn index
- split 后时间边界
- split 后 speaker_a / speaker_b
- split 后是否分别 re-TTS
- split 后是否 commit

如果“拆分后两个子段 speaker 不同”，这是强 speaker 误合并信号。

#### `post_edit_tts_regenerated`

用户对某段或批量 dirty 段重新合成。

记录：

- 触发原因：`text_dirty | voice_dirty | split | manual_retry`
- provider / voice_id / model
- target_duration_ms
- draft_audio_duration_ms
- speed / DSP 预估
- 成功 / 失败

#### `post_edit_draft_tts_accepted`

用户接受草稿 TTS。

这条事件让前面的 text / voice / split 修改成为强有效样本。

#### `post_edit_draft_tts_discarded`

用户丢弃草稿 TTS。

这条事件是重要反例：同一段反复重合成但一直 discard，说明现有 voice / speed / rewrite 策略或交互流程可能没有解决用户问题。它用于区分 `weak`、`confirmed`、`effective`，避免只看 accepted 样本造成幸存者偏差。

记录：

- draft id / segment id
- 丢弃前的 text / voice / speed / duration / DSP 信息
- 是否随后再次 re-TTS

#### `post_edit_committed`

commit 时记录汇总：

```json
{
  "event_type": "post_edit_committed",
  "stage": "post_edit",
  "context": {
    "strategy": "overwrite",
    "text_edit_count": 8,
    "speaker_change_count": 3,
    "split_count": 2,
    "voice_override_count": 4,
    "tts_regenerated_count": 11,
    "draft_accepted_count": 11
  },
  "effective": true,
  "effective_reason": "committed"
}
```

#### `post_edit_cancelled`

用户退出修改流程且未 commit。

这不是最终有效修改，但它是流程可用性信号。若用户在某个 job 中做了大量 text / speaker / split / TTS regenerate 后 cancel，需要在离线分析里保留为“confirmed but not effective”反例。

记录：

- session duration
- edit counts by type
- draft accepted / discarded count
- dirty segment count
- cancel reason（如果前端能提供）

commit 成功后的 effective 归并按 §4.5 追加 `effective_marker`，不要回写旧事件。

## 8. 如何把事件变成可分析样本

### 8.1 同源任务归并

分析时必须按 `root_job_id` / source video hash 归并。

同一个视频用户可能多次重跑，优化前后版本也不同。需要把样本分成：

- baseline run
- after P1
- after P2
- after manual edit
- copy_as_new

否则会把同源重复视频当成独立样本，误判效果。

### 8.2 事件有效性分层

建议分三层：

| 层级 | 定义 | 用途 |
| --- | --- | --- |
| weak | 用户改过，但未 commit / approve | UI 摩擦、试错行为分析 |
| confirmed | 服务端写入，进入 review_state / editing buffer | 流程体验分析 |
| effective | approve / TTS accepted / commit 成功 | 质量优化、模型评估 |

P2 speaker 优化只使用 `effective` 或高置信 `confirmed`。

### 8.3 数据集输出

新增离线构建脚本：

```text
scripts/benchmark/build_user_edit_audit_dataset.py
```

输出：

```text
reports/benchmark/<date>/user_edit_audit/
  segment_speaker_corrections.jsonl
  segment_splits.jsonl
  keep_original_segments.jsonl
  text_rewrite_segments.jsonl
  job_summary.csv
```

`segment_speaker_corrections.jsonl` 每行是一条 speaker 纠错样本。

`segment_splits.jsonl` 每行是一条用户拆分样本。

`keep_original_segments.jsonl` 每行是一条用户认为不应配音或应保留原音的样本。

## 9. 如何优化说话人识别

### 9.1 建立 speaker correction benchmark

用用户修改事件构造基准集：

正样本：

- 用户把 segment 从 A 改到 B，且最终 approve / commit
- 用户拆分 segment 后两个子段 speaker 不同，且最终 commit
- 用户在音色选择原音核对中重分配 speaker

负样本：

- 用户查看但没有修改的 speaker 高风险段
- 用户改了又改回原 speaker 的段
- 多次重跑同源视频中稳定未改的段

注意：负样本不能简单取“所有未改段”，因为用户可能没检查到。

### 9.2 归一化分析 correction duration

不能只看“被改段 p50 时长”。必须对比：

- 所有 utterance duration CDF
- 被用户改 speaker 的 duration CDF
- 被 split 的原段 duration CDF

如果两条 CDF 接近，说明错误不依赖段长。

如果 correction 在短段显著集中，说明短插话规则有效。

如果 correction 在长段 / split 集中，说明 ASR utterance 合并、SemanticBlock 切分或 overlap 处理更重要。

### 9.3 提取高价值特征

每条 speaker correction 样本应提取：

- `duration_ms`
- `source_text_chars`
- `cn_text_chars`
- `start_ms / end_ms`
- `asr_speaker_id`
- `s2_speaker_id`
- `user_final_speaker_id`
- `neighbor_prev_speaker_id`
- `neighbor_next_speaker_id`
- `speaker_switch_density_10s`
- `speaker_segment_count`
- `speaker_duration_share`
- `is_low_share_speaker`
- `is_short_segment`
- `is_long_segment`
- `was_split`
- `split_child_speakers_different`
- `dubbing_mode`
- `rms / volume`（可后补）
- `overlap_suspected`（P2 后补）

这些特征可用于规则、风险评分、verifier 触发条件。

### 9.4 形成 speaker 风险评分

基于用户纠错样本，给每段计算 `speaker_risk_score`：

```text
risk = 
  low_share_speaker_weight
  + isolated_segment_weight
  + neighbor_same_speaker_conflict_weight
  + short_interjection_weight
  + long_segment_split_likelihood_weight
  + overlap_suspected_weight
  + prior_user_correction_pattern_weight
```

用途：

- 音色选择阶段高亮“建议核对原音”
- P2 verifier 只对高风险段触发，控制成本
- S2 prompt 增加针对性规则，而不是全量多模型并跑

### 9.5 优化 S2 prompt

把用户纠错统计转成 prompt 规则：

如果数据显示短段高错：

- 增加短插话保守规则
- 相邻主讲人包围的低音量短段，不要轻易新建 speaker

如果数据显示长段 split 高错：

- 增加“长段中途 speaker turn / audience interaction”检查
- 要求输出 `split_or_resegment_suspected`

如果数据显示低占比 speaker 常被合并回主讲人：

- 增加“低占比 speaker 可能是背景声 / 观众互动 / 非目标对白”的分类
- 不直接删 speaker，而是标风险，交给 voice selection 审核

### 9.6 P2 verifier 触发条件从数据中学习

不要固定只用 `<2s`。应从用户事件中回放不同触发策略：

- duration threshold
- speaker duration share threshold
- neighbor pattern
- source text pattern
- split likelihood
- overlap suspected

目标：

- 用最少 verifier 调用覆盖最多用户真实纠错
- 初始参考线：在历史有效 speaker corrections 中，候选触发规则覆盖率约 70%，触发段占总段数约 15%
- 正式 Go 条件必须等 P0/P1 跑出一周 baseline 后再定，按“相对 baseline 提升多少百分点、额外 verifier 成本是否低于节省的 TTS 重合成和人工修正成本”判断，不能先固定数字再反推结论

## 10. 如何优化翻译与配音流程

### 10.1 翻译长度控制

从 `post_edit_text_changed + tts_regenerated + draft_tts_accepted` 分析：

- 用户是否经常缩短某类段落
- 用户是否经常扩写某类短段
- 改后字符数与原 duration 的关系
- 改后是否减少 force-DSP

可反推：

- G1 字数范围是否过宽 / 过窄
- pre-TTS rewrite 是否过度
- 某 provider / voice 的 CPS 画像是否不准
- 哪些内容类型需要不同 target chars 策略

### 10.2 SemanticBlock 切分

从 `segment_split_confirmed` 分析：

- split 主要发生在哪类时长段
- split 位置是否接近标点、停顿、speaker turn
- split 后是否两个 speaker 不同
- split 后 TTS / DSP 是否明显改善

可反推：

- 是否需要在 ASR 后增加 deterministic resegmentation
- 是否需要在 S2 输出 split suggestion
- 是否需要在 SemanticBlock 构造时避免合并跨 speaker turn

### 10.3 keep original / 非对白策略

从 `dubbing_mode_changed` 分析：

- 哪些短段用户常设为 `keep_original`
- 哪些 speaker 常被用户标成背景声 / 非对白
- 哪些文本或音频特征对应“观众互动”

可优化：

- voice selection 自动风险提示
- 默认不克隆低占比背景 speaker
- 后续 TTS 跳过 keep_original
- 最终合成保留原音，避免无意义 TTS 和 DSP

### 10.4 音色推荐

从 `voice_override_changed` 分析：

- 用户从自动推荐换到什么音色
- 哪些 speaker 类型自动匹配失败率高
- 克隆音色是否减少后续修改

可优化：

- 自动匹配排序
- speaker label prompt
- cloned voice speed profile
- provider / model 默认路由

## 11. 数据汇总指标

### 11.1 Job 级指标

| 指标 | 含义 |
| --- | --- |
| `user_edit_rate` | 有任何人工修改的 job 占比 |
| `speaker_correction_rate` | 每 job speaker 改动数 / segment 数 |
| `split_rate` | 每 job split 数 / segment 数 |
| `keep_original_rate` | keep_original 段数 / segment 数 |
| `text_edit_rate` | 文本修改段数 / segment 数 |
| `re_tts_accept_rate` | re-TTS 后被接受比例 |
| `commit_success_rate` | post-edit commit 成功率 |

### 11.2 Segment 级指标

| 指标 | 含义 |
| --- | --- |
| `speaker_changed_effective` | 用户最终是否改 speaker |
| `split_effective` | 用户最终是否拆分 |
| `keep_original_effective` | 用户最终是否保留原音 |
| `text_changed_effective` | 用户最终是否改文本 |
| `tts_regenerated_effective` | 用户是否重新合成并接受 |

### 11.3 P2 speaker 指标

| 指标 | 含义 |
| --- | --- |
| `speaker_false_assignment_proxy_rate` | 用户有效 speaker correction 率 |
| `low_share_speaker_correction_rate` | 低占比 speaker 被改率 |
| `split_cross_speaker_rate` | split 后两段 speaker 不同的比例 |
| `audience_keep_original_rate` | 观众/背景 speaker 被 keep_original 的比例 |
| `risk_rule_coverage` | 风险规则覆盖有效纠错的比例 |
| `risk_rule_trigger_rate` | 风险规则触发段占总段比例 |

## 12. 实施方案

### P0：只补审计框架，不改变业务行为

交付：

- 新增 `src/services/jobs/user_edit_audit.py`
- 新增 `project_dir/audit/user_edit_events.jsonl`
- 提供 `append_user_edit_event(...)`
- 提供 `append_effective_marker(...)`，不回写旧事件
- 在 `JobService` 或对应 service 层提供 `audit_observer` callback，业务 mutation 完成后统一 observe，不在每个 endpoint 中散点直写
- observer 的异常隔离放在 service 层：service 调用 `self._audit_observer.observe(event)` 时统一 `try/except`，observer 实现本身保持简单，便于未来替换 JSONL / Postgres / Kafka writer
- 审计校准：检查所有会由用户 mutation 触发的付费 API 调用点，例如 TTS regenerate、voice clone、LLM rewrite，确保 `record_tts` / `record_llm` 或等价 UsageMeter 记录显式携带 `segment_id`；缺失时在 P0 补齐，否则 P1 的 `usage_event_ids` 回填会大面积失败
- 单元测试覆盖 append / load / effective marker / fake observer 调用序列

接入点：

- translation review speaker change / split / approve
- voice selection speaker reassignment / dubbing mode / approve
- editing session started baseline marker
- post-edit text patch / speaker patch / split / voice override / TTS regenerate / draft accept / draft discard / cancel / commit

原则：

- 只写审计文件
- 不改变当前流程
- 写失败不阻断用户主路径
- 写失败除 warning log 外，还应写一条去重后的 `JobEvent(level=WARN, payload={"audit_write_failed": true, "event_type": "..."})`，同一 job 同类失败一小时内最多写一次，避免 docker log 丢失后完全不可观测

### P1：生成离线分析数据集

交付：

- `scripts/benchmark/build_user_edit_audit_dataset.py`
- `reports/benchmark/<date>/user_edit_audit/job_summary.csv`
- `segment_speaker_corrections.jsonl`
- `segment_splits.jsonl`
- `keep_original_segments.jsonl`

输出首版报告：

- 最近 N 个 job 用户修改分布
- speaker correction CDF vs 全体 segment CDF
- split 原段时长分布
- keep_original 特征分布
- 同源视频多次重跑对比

### P2：说话人识别优化闭环

交付：

- speaker risk scoring 离线回放
- P2 verifier 触发条件候选评估
- S2 prompt 修订建议
- voice selection 高风险提示规则更新

Go 条件：

- 先用 P0/P1 baseline 校准覆盖率、触发率和成本，不把 70% / 15% 当硬阈值
- 风险规则覆盖率相对 baseline 有明确提升，且触发段占比和 verifier 成本保持在预算内
- 同源回归视频 speaker correction 次数下降
- 高风险提示不能显著增加用户无效操作或 cancel 率

### P3：线上监控与产品化

交付：

- admin quality dashboard 增加用户修改指标
- 每周生成 user-edit quality report
- 将高风险样本加入固定 benchmark fixture

## 13. 风险与边界

1. 用户修改不等于绝对真值。需要用 effective、重复视频一致性、音频抽查提高置信度。
2. 未修改段不等于模型正确。用户可能没检查到。
3. 不应直接用用户文本训练模型，除非后续明确隐私与授权策略。
4. 初期不要引入复杂数据库表，先用项目内 JSONL，跑通价值后再考虑入 Postgres。
5. 审计写入必须非阻塞，不能影响用户审校和二次合成。
6. `copy_as_new` 不复制 `audit/` 目录到新 job，避免把父任务用户行为误当成子任务新行为；离线分析通过 `root_job_id` / `copy_of_job_id` 归并父子任务事件流。
7. 当前 `speaker_reassignment_history` / `dubbing_mode_history` 可以短期保留给 UI 使用，但应视为过渡字段。P3 收口时改成从 `user_edit_events.jsonl` 重建的只读视图，避免长期维护两套行为真源。
8. `UsageMeter` 仍然是费用事实源，`user_edit_events` 只保存行为到费用事件的关联 id，不能用行为事件直接替代扣点和账务记录。

## 14. 推荐下一步

优先做 P0，原因是当前系统已经有足够多的用户实际修改场景，但数据正在流失。

最小可落地范围：

1. 新增 `user_edit_events.jsonl`。
2. 接入首批 11 个事件：
   - `editing_session_started`
   - `voice_selection_speaker_reassigned`
   - `voice_selection_dubbing_mode_changed`
   - `post_edit_text_changed`
   - `post_edit_segment_speaker_changed`
   - `post_edit_segment_split_confirmed`
   - `post_edit_tts_regenerated`
   - `post_edit_draft_tts_accepted`
   - `post_edit_draft_tts_discarded`
   - `post_edit_cancelled`
   - `post_edit_committed`
3. P0 先用 `audit_observer` 接入，测试里注入 fake observer 验证事件序列。
4. 写一个离线脚本输出 speaker correction 表，并把 `usage_event_ids` 留好关联位。

这一步不改变模型、不增加 API 成本，但能把后续 P2 的“怎么优化说话人识别”从猜测变成数据驱动。
