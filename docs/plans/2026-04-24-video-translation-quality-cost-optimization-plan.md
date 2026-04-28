# 视频翻译流程降本增效优化方案 v1.2

日期：2026-04-24

更新：2026-04-25，根据 Claude Code 对 v1.1 的评审意见，清理早期结论与 v1.1 增补之间的口径冲突。

## 目标

本方案目标是在不改变项目核心架构不变量的前提下，优化视频翻译、配音、对齐、审校流程：

- 降低无效 LLM rewrite、TTS 重合成、人工返工成本。
- 提高翻译配音视频质量，尤其是文本长度与原音频时长匹配、说话人归属准确性。
- 建立离线 benchmark 和数据闭环，让 prompt、阈值、流程变更先在历史数据上验证，再进入生产。

必须保持的架构边界：

- TTS unit 仍是 `SemanticBlock`，不是字幕行。
- Alignment 仍是 DSP first，rewrite loop 是 fallback。
- 字幕 retiming 仍应是数学/确定性逻辑，不交给 LLM。
- 目标交付物仍是 Jianying draft，不把直出 MP4 作为主路径。
- 优先小型、可测、可替换的改动，避免大重构。

## 数据基础

本次已从 US 主机拉取并分析两类数据。

项目中间产物样本：

- 来源：`/opt/aivideotrans/data/projects`、`/opt/aivideotrans/data/jobs`、`/opt/aivideotrans/data/runtime_logs`
- 范围：只拉取 JSON、JSONL、SRT、日志等文本类中间产物，排除 mp4/wav 等大媒体文件。
- 本地样本包：`.codex_tmp/us_fetch/avt_quality_text_sample_20260424.tar.gz`
- 本地解包目录：`.codex_tmp/us_fetch/extracted`
- 文件数：1068
- 压缩包大小：约 11 MB
- 覆盖：84 个 `job_*` 目录，4 个用户/工作区根目录。

Gateway/Postgres 指标：

- 来源：US 主机 `aivideotrans-postgres`，只读导出 job 级 metering 和按 job 聚合的 credits ledger。
- 本地样本包：`.codex_tmp/us_fetch/avt_gateway_metrics_20260424.tar.gz`
- 本地解包目录：`.codex_tmp/us_fetch/gateway_metrics/avt_gateway_metrics_20260424`
- 导出文件：`job_metering.csv`、`provider_cost_summary.csv`、`rewrite_outliers.csv`、`missing_metering_fields.csv`、`metering_key_counts.csv`、`credits_ledger_job_summary.csv`
- 排除：`users`、手机号、邮箱、支付订单、发票、webhook payload、数据库原始 dump。

本地分析产物：

- `.codex_tmp/us_fetch/analysis/segment_trace.csv`：3818 行 segment 级长度/TTS/对齐/rewrite 数据。
- `.codex_tmp/us_fetch/analysis/speaker_diff_summary.csv`：72 条 S2 说话人修正记录。
- `.codex_tmp/us_fetch/analysis/job_metering_joined.csv`：job metering 与可用 segment 数据交集。
- `.codex_tmp/us_fetch/analysis/credits_ledger_joined.csv`：credits ledger 与 job metering 聚合结果。
- `.codex_tmp/us_fetch/analysis/comprehensive_summary.json`：综合统计摘要。

这些 `.codex_tmp` 数据仅用于本地分析，不应提交。

## 关键事实

### 数据覆盖与缺口

- 项目目录中发现 84 个 job 目录。
- 59 个 job 有可解析 `segments.json`，共 3818 个 segment。
- Gateway DB 有 64 个 job 记录，其中 56 个有 `metering_snapshot`。
- 项目文件与 Gateway DB 的 job_id 交集只有 9 个，说明文件系统历史 job 与 DB job 并非完全同步。后续 benchmark 需要同时支持“项目文件主索引”和“Gateway job 主索引”两种入口。

V3 metering 字段覆盖：

| 字段 | 覆盖 |
|---|---:|
| `final_cn_chars` | 56 jobs |
| `rewrite_triggered` | 56 jobs |
| `rewrite_count` | 56 jobs |
| `credits_estimated` | 49 jobs |
| `total_segments` / `needs_review_count` / `first_pass_error_pct_p90` | 34 jobs |
| `tts_billed_chars` | 11 jobs |
| `credits_actual` | 0 jobs |

结论：V3 成本观测链路还没有闭环。`credits_actual` 没写入 `metering_snapshot`，`tts_billed_chars` 只覆盖少量 CosyVoice/VolcEngine job，MiniMax job 普遍缺失该字段。当前若直接用 `/admin/credits-monitor` 判断“实际成本”，会系统性低估或缺失。

Credits ledger 补充情况：

- `credits_ledger` 有 39 个按 job 聚合记录。
- 10 个 job 有 capture 记录，capture credits 合计 4201。
- 因为 ledger 与 `metering_snapshot` 口径不完全一致，P0 需要先把实际扣费字段回填或统一查询，而不是直接拿现有 snapshot 做成本结论。

### 长度、TTS、对齐

Segment 样本：

- 3818 个 segment 中，745 个发生过 rewrite，segment 级 rewrite 率约 19.5%。
- 641 个 segment 标记 `needs_review`，约 16.8%。
- `first_pass_error_pct` 覆盖 3080 个 segment，绝对误差 p50 约 0.17%，p90 约 0.75%，p95 约 1.06%。
- final audio 相对 target 绝对误差 p50 约 0.39%，p90 约 5.31%，p95 约 12.5%。
- `alignment_method` 分布中，`force_dsp` / `force_dsp_user` 占比较高，说明很多段最终依赖强制 DSP 或用户强制 DSP，而不是自然落在 direct/rewrite_direct。

Gateway metering：

- 56 个带 snapshot 的 DB job 中，54 个 `rewrite_triggered=true` 或 `rewrite_count>0`，job 级 rewrite 率约 96.4%。
- `rewrite_count` p50 为 9，p90 为 39，最大 96。
- provider 聚合中，MiniMax HD 平均 rewrite_count 约 29.4，MiniMax Turbo 约 12.3，CosyVoice Express 约 7.9，VolcEngine Express 约 10.0。该对比受样本内容和服务模式影响，不能直接等价为 provider 优劣，但足以提示 provider/voice/内容类型需要分层评估。
- `final_cn_chars / actual_minutes` 平均约 280.5 字/分钟。CosyVoice/VolcEngine 的 `tts_billed_chars` 覆盖样本中，CosyVoice billed chars/min 明显高于 VolcEngine，符合其计费口径可能包含倍数或 raw 字符的预期。

Pre-TTS rewrite 矛盾样本：

- 从 job event 中解析到 92 条可匹配的 `[S4] Pre-TTS rewrite` 事件。
- 其中 38 条在后续 first pass TTS 中出现方向性矛盾且绝对偏差超过 5%：
  - overshoot 缩短后，后续 TTS 反而短超 5%。
  - undershoot 扩充后，后续 TTS 反而长超 5%。
- overshoot 样本的后续误差 p50 约 -3.8%，p90 约 11.4%；undershoot 样本 p50 约 7.4%，p90 约 37.7%。

结论：用户感知的“前面判太长要求缩短，后面又发现太短再扩写”不是假设，历史数据中已经出现。根因大概率不是单一 prompt，而是 G1/G2/G3 各自独立判断、估算器与真实 voice CPS 不一致、rewrite 目标区间缺少统一 duration budget。

### 说话人归属

S2 speaker diff：

- 43 个 job 有 `s2_review_speaker_diff.json`。
- 14 个 job 发生说话人修正。
- 共 72 条 speaker correction。
- 70 条发生在 `original_to_after_corrections`，2 条发生在 `after_corrections_to_after_sanity`，`after_sanity_to_final` 为 0。
- 9 条 correction 的段落时长小于等于 1.2 秒，覆盖典型短插话风险。
- correction 时长 p50 约 11.14 秒，p90 约 38.03 秒；但与全部 segment 时长分布归一化对比后，两条 CDF 很接近。因此“段长”本身不是足够强的 verifier 触发特征，短插话只是高风险子集之一。

结论：Claude Code 提到的“短插话保守规则”和“3+ speaker verifier”合理，但触发条件不能只看 `<2s`。更应按相邻 speaker 切换密度、3+ speaker、局部 flip、ASR/S2 冲突、overlap 疑似、低置信 correction 等特征分层。

## 现流程问题抽象

### 三个长度 gate 口径割裂

当前长度控制可以抽象为三层 gate：

| Gate | 位置 | 核心输入 | 失败动作 | 主要问题 |
|---|---|---|---|---|
| G1 翻译长度校验 | `translator.py` | 翻译文本字符数、target/min/max chars | 重新翻译一次 | 主要看字数，不直接看 voice CPS 和真实 TTS 时长 |
| G2 TTS speed 决策 | `speed_decision.py` / TTS 前 | 估算时长与目标时长 | 给 speed 或退回 1.0 | 超限时信号不够明确，后续不知道 speed 已经无法吸收 |
| G3 对齐后时长校验 | `alignment/aligner.py` / orchestration | TTS 实测时长与目标时长 | DSP、rewrite、兜底 DSP | rewrite 不回看 G1 的 min/max chars 和前序决策原因 |

问题不是 gate 太少，而是 gate 之间“不通气”。同一段在 G1/G2/G3 可能分别基于不同口径作出相反动作。

### 数据闭环缺失

`metering_snapshot` 已经有一批高价值字段，但写入覆盖不完整：

- `credits_actual` 完全缺失，不能直接做实际成本归因。
- `tts_billed_chars` 只覆盖 11/56 snapshot jobs，MiniMax 缺口明显。
- project files 与 Gateway DB 的 job overlap 低，离线分析不能假定两边天然一一对应。
- 事件日志中包含 rewrite 方向，但没有结构化字段保存“pre-rewrite direction / estimate_ms / target_ms / post-tts duration_ms”，后续分析需要从 message 反解析。

### Speaker 修正缺少风险闭环

S2 已保存了 pass artifacts 和 speaker diff，这是很好的基础。但缺少结构化风险指标：

- correction 是否来自短插话、3+ speaker、相邻 speaker 切换、overlap、低置信 ASR。
- correction 是否被人工改回。
- correction 是否导致 voice remapping、TTS 重合成、人工 review。

因此现在只能看到 S2 改了什么，看不到“哪些改动是有害的”。

## 优化主线

### 主线 A：贯通 duration budget，消除冗余 rewrite

不建议再加第四个长度 gate。应让 G1/G2/G3 共享同一份 `duration_budget` 和审计字段。

建议新增或落盘字段：

```json
{
  "duration_budget": {
    "target_ms": 12340,
    "min_chars": 80,
    "max_chars": 120,
    "target_chars": 100,
    "char_counter": "spoken_filtered",
    "cps_source": "voice_catalog|provider_default|fallback",
    "provider": "minimax",
    "voice_id": "xxx"
  },
  "speed_decision": {
    "estimated_ms": 14000,
    "requested_speed": 1.08,
    "final_speed": 1.08,
    "reason": "in_range|clamped|downgrade_to_rewrite|disabled"
  },
  "rewrite_decision": {
    "trigger": "pre_tts_overshoot|pre_tts_undershoot|post_tts_overshoot|post_tts_undershoot",
    "direction": "shrink|expand",
    "source_gate": "G1|G2|G3",
    "attempt": 1
  }
}
```

实施要点：

1. G1 翻译通过后，保存 `duration_budget`，包括目标 ms、target/min/max chars、字符计数口径、CPS 来源。
2. G2 speed 决策必须落盘。超出 provider 可接受 speed 范围时，不再只静默回 `speed=1.0`，而是明确标记 `downgrade_to_rewrite`。
3. G3 读取 G2 信号。若 G2 已经判断 speed 无法吸收，G3 直接进入 rewrite 分支，并继承 direction、target、min/max chars。
4. G3 调 rewrite_engine 时传入 G1 的 min/max chars，避免 shrink 压破底线或 expand 过度。
5. rewrite 后再次用同一字符口径复核，不用另一套估算规则。
6. 统一 spoken-char 口径。TTS 计费仍可保留 provider raw/billed 口径，但必须同时保存 `spoken_chars` 和 `provider_billed_chars`，不要混用。

预期收益：

- 降低同段“先缩短后扩写”或“先扩写后压缩”的矛盾动作。
- 降低 rewrite 次数，尤其是高 rewrite job。
- 让每段失败原因可审计，可用于离线阈值优化。

### 主线 B：Speaker 修正保守化，加局部 verifier

不建议全量多模型并跑 speaker 识别。音频 token 成本高，且问题集中在风险段和启发式后处理。

建议分层：

1. 短插话保守规则：
   - 对 `duration_ms <= 1200` 或 `<= 2000` 的独立 utterance，默认保持 ASR speaker。
   - 只有当 S2 给出高置信、上下文一致、相邻段不冲突时才允许 correction。
   - `_apply_corrections` 层做硬兜底，而不只靠 prompt。

2. 3+ speaker verifier：
   - 触发条件建议先从数据中调参，不固定拍脑袋。
   - 初始候选：`speaker_count >= 3` 且 `correction_applied=true` 且满足任一条件：短插话、相邻 10 秒内 speaker 快速切换、ASR/S2 speaker diff 多、S2 confidence 低。
   - verifier 输入只给局部音频片段和结构化上下文，不全量重跑 Pass 1。
   - 输出：`accept | reject | uncertain`。`reject` 默认回 ASR 原归属；`uncertain` 按服务模式和 job 价值分层处理：付费/高价值 studio 标记人工 review，免费 trial/express 低风险段回 ASR 原归属。

3. 长段 correction 风险：
   - 本次数据归一化后显示，correction 并不显著偏向短段或长段，不能只按时长触发 verifier。
   - 对 correction，应检查是否跨越多个语义话轮、是否出现多人同时说话、是否 S2 将 quote/插话误合并到主 speaker。

4. 多人同时说话：
   - 不建议优先在 S2 prompt 里硬解。
   - 更适合在 ASR/VAD/diarization 配置层和后处理层单独立项。
   - S2 Pass 1 增加 `overlap_suspected` / `overlap_confidence` / `overlap_reason` 被动标记，不直接改归属；下游 review、voice selection、benchmark 和 verifier 使用该风险信号。

### 主线 C：离线 benchmark 和 replay

这是 P0 必做项。没有 benchmark，prompt/阈值/流程调整只能上生产试错。

建议新增：

- `scripts/benchmark/build_quality_dataset.py`
  - 从历史 job artifacts 构建固定 benchmark manifest。
  - 支持按用户/工作区根、job_id、speaker_count、rewrite_count、needs_review_rate、provider、content length 分层抽样。

- `scripts/benchmark/replay_length_chain.py`
  - 不调用付费 API。
  - 基于历史 `segments.json`、event logs、metering_snapshot，重放 G1/G2/G3 的判断逻辑。
  - 输出不同阈值、CPS 来源、speed clamp、rewrite target ratio 下的 rewrite 触发率、矛盾率、needs_review 预测。

- `scripts/benchmark/replay_s2_speaker.py`
  - 第一阶段不重跑音频 LLM，只基于 `s2_review_speaker_diff.json`、`raw_assemblyai.json`、`transcript.json`、`review_state.json` 做风险规则回放。
  - 第二阶段只对高风险局部片段调用 verifier。

- `reports/benchmark/<date>/<variant>/`
  - 保存 baseline、variant、diff、top outliers、go/no-go 指标。

Benchmark 集建议：

- 第一版不追求 24 格全覆盖，先选 10-12 个 job。
- 必须覆盖：
  - 高 rewrite：`rewrite_count >= 30`
  - 低 rewrite/正常样本
  - 1 speaker / 2 speaker / 3+ speaker
  - 短视频 2-5 分钟 / 中长视频 15-30 分钟 / 长视频 60 分钟以上
  - MiniMax Turbo / MiniMax HD / CosyVoice / VolcEngine
  - 有 speaker diff / 无 speaker diff

### 主线 D：成本观测闭环

这是本次数据分析新增的优先项，应放在 P0，而不是等到 P1/P2。

必须修复：

1. `credits_actual` 写入 `Job.metering_snapshot`。
   - 可以在 capture 成功时同步写入 snapshot，或在 credits monitor 查询时 join ledger 计算。
   - 推荐两者都做：snapshot 便于 job 自包含审计，ledger 仍是财务审计来源。

2. `tts_billed_chars` 覆盖 MiniMax。
   - 当前 CosyVoice/VolcEngine 有部分覆盖，MiniMax 大量缺失。
   - 对每个 TTS segment 保存 `spoken_chars`、`raw_chars`、`provider_billed_chars`、`provider_multiplier`。

3. 结构化记录 Pre-TTS rewrite。
   - 当前只能从日志 message 反解析 `[S4] Pre-TTS rewrite (...)`。
   - 应进入 segment metrics：`pre_tts_rewrite_direction`、`pre_tts_estimate_ms`、`pre_tts_target_ms`、`post_tts_first_pass_ms`、`pre_tts_contradiction`。

4. 统一 job_id mapping。
   - project files 与 DB overlap 低，benchmark 工具必须支持从 `project_state.json`、`job_*.json`、DB `jobs.project_dir` 三处建立索引。

## 关于多模型/多候选

### 翻译侧

不建议生产默认全量多候选。原因：

- 现有数据已经显示很多 first pass duration error 很低，盲目多候选会增加复杂度。
- 重写高发更多来自 gate 口径割裂、CPS/voice/provider 差异、speed 决策和 rewrite target，不一定是翻译质量本身。

建议：

- P0 先在离线 replay 中标出“高风险段”：历史 rewrite、pre-TTS 矛盾、chars/sec 极端、provider outside speed。
- 只对高风险段测试多候选翻译和语义守门。
- Go 条件：在高风险段集合上 rewrite 触发率下降 >= 10%，且术语/数字保真不下降。

### Speaker 侧

不建议全量多模型并跑。建议做局部 verifier：

- 只在高风险 correction 上触发。
- verifier 成本应与人工修改和重 TTS 成本比较。
- 若 verifier 驳回率高且人工改回率下降，再考虑扩展触发范围。

### LLM 自估 TTS 时长

Claude Code 提出的“翻译输出 text + 字数 + 自估 TTS 时长”值得放入 P0 实验，但只作为辅助特征，不应成为硬 gate。

实验方式：

- prompt 让模型输出 `estimated_spoken_duration_ms` 和 `confidence`。
- 离线比较 LLM 自估、当前 `TTSDurationEstimator`、voice catalog CPS 与真实 first_pass_duration_ms 的误差。
- 若 LLM 自估在特定内容类型上明显优于 estimator，可作为 G1/G2 的风险信号。
- 不建议直接让 LLM 决定字幕 retiming 或最终时长。

## 分阶段计划

### P0：数据闭环和 benchmark 基座

目标：先让历史数据能稳定复现当前问题，并让成本数据可用。

任务：

- 补 `credits_actual` 查询/写入闭环。
- 补 MiniMax `tts_billed_chars`。
- 结构化保存 Pre-TTS rewrite 事件。
- 建 benchmark manifest 和 replay 脚本骨架。
- 固定 10-12 个基准 job。
- 输出 baseline report：rewrite rate、pre-TTS contradiction rate、speaker correction risk、tts billed chars coverage、credits capture coverage。
- 输出 provider/content 分层报告：`provider × content_type × duration_bucket × speaker_count × rewrite_rate`。
- 输出货币化成本报告：LLM 翻译/rewrite、TTS 首次合成、TTS rewrite 重合成、speaker verifier、人工 review 的 job 级成本估算和 `net_saving`。
- 输出 Phase 2 before/after CDF：`first_pass_error_pct`、`rewrite_count/job`、`force_dsp_rate`、`needs_review_rate`。

Go/No-Go：

- benchmark 能复现现有 segment/job 指标，误差 <= 5%。
- `credits_actual` 或 ledger-derived actual credits 覆盖率 >= 90%。
- `tts_billed_chars` 覆盖率 >= 90%。
- 成本报告必须能明确回答：新增多候选/verifier 成本是否低于可避免的 TTS 重合成和人工 review 成本。

### P1：Duration budget 贯通

目标：降低冗余 rewrite 和方向矛盾。

任务：

- G1 产出并落盘 `duration_budget`。
- G2 落盘 `speed_decision`，显式记录 `downgrade_to_rewrite`。
- G3 读取 G1/G2 元数据，rewrite 时传 min/max chars。
- rewrite 后统一 spoken-char 复核。
- 新增指标：`gate_downgrade_reason_dist`、`pre_tts_contradiction_rate`、`rewrite_direction_flip_rate`。

Go/No-Go：

- benchmark 中 rewrite_count 下降 >= 20%。
- pre-TTS direction contradiction 下降 >= 50%。
- final duration p90 不恶化。
- 人工 review 率不升高。

### P2：Speaker 保守修正和局部 verifier

目标：降低 speaker 误改，尤其是短插话和 3+ speaker 场景。

任务：

- Pass 1 prompt 加短插话保守规则。
- `_apply_corrections` 增加短插话/低置信 correction 过滤。
- 实现 3+ speaker 局部 verifier。
- 保存 `speaker_correction_risk`、`verifier_decision`、`verifier_reason`、`speaker_correction_reject_rate`。

Go/No-Go：

- speaker correction 人工改回率下降 >= 50%。
- verifier 触发率控制在 <= 10% correction 段。
- S2 整体耗时和成本可接受。

### P3：条件触发多候选与 LLM 自估时长

目标：只在 P0/P1/P2 后仍高风险的局部段上探索增量收益。

任务：

- 高风险段多候选翻译。
- LLM 自估 TTS 时长作为风险信号。
- information completeness / number-term preservation 守门。

Go/No-Go：

- 高风险段 rewrite 率下降 >= 10%。
- 术语、数字、人名保真不下降。
- 增量 LLM 成本低于节省的 TTS 重合成和人工修改成本。

## 建议的近期具体行动

1. 先做 P0 数据闭环，不先改 prompt。
2. 把本次 `.codex_tmp/us_fetch/analysis` 里的 CSV 作为第一版 benchmark 输入。
3. 从 `rewrite_outliers.csv` 中优先选高 rewrite job，例如 `rewrite_count=96/52/49/45/39/31` 的样本。
4. 从 `speaker_diff_summary.csv` 选出短插话和 3+ speaker 样本，建立 speaker benchmark。
5. 在 P0 baseline report 中明确统计：
   - rewrite_count/job
   - rewrite_count/segment
   - pre-TTS contradiction rate
   - force_dsp rate
   - needs_review rate
   - speaker correction rate
   - tts_billed_chars coverage
   - credits_actual coverage

## 对 Claude Code 方案的吸收与修正

吸收：

- “三 gate 口径割裂”判断成立，并被本次 pre-TTS contradiction 数据支持。
- “duration budget 贯通”优于新增第四个 gate。
- “不全量多模型 speaker，并改做高风险局部 verifier”方向合理。
- “P0 先做离线 replay/benchmark”是必要前提。
- “LLM 自估 TTS 时长”值得作为 P0 实验变量。

修正：

- `credits_actual` 当前没有进入 `metering_snapshot`，不能假设 V3 成本字段已完整可用。
- `tts_billed_chars` 覆盖不足，尤其 MiniMax 缺口会影响 TTS 成本判断。
- speaker correction 不只发生在短插话，长段 correction 也不少，因此 verifier 触发条件不能只用 `<2s + 3+ speaker`。
- project files 与 Gateway DB job overlap 低，benchmark 需要独立索引层。

## v1.1/v1.2 增补：评审意见处理和操作化任务

Claude Code 对本方案 v0 的 push back 大体合理，应吸收到 v1.1。尤其是以下几项会影响 P0/P1/P2 的设计方向。

### 1. Speaker correction 时长需要归一化

v0 中提到 speaker correction duration p50 约 11 秒，进而提醒“长段 correction 也存在”。这个提醒方向没错，但直接用 p50 解读容易误导。

补充归一化统计：

| 分布 | p10 | p25 | p50 | p75 | p90 |
|---|---:|---:|---:|---:|---:|
| 全部 segment duration | 1160ms | 2800ms | 11180ms | 28625ms | 44490ms |
| speaker correction duration | 1120ms | 3200ms | 11140ms | 20240ms | 38030ms |

阈值覆盖率：

| 阈值 | 全部 segment 占比 | correction 占比 |
|---|---:|---:|
| <= 1200ms | 10.92% | 12.50% |
| <= 2000ms | 19.75% | 15.28% |
| <= 5000ms | 33.55% | 27.78% |
| <= 10000ms | 47.98% | 44.44% |
| <= 20000ms | 64.77% | 73.61% |
| <= 40000ms | 85.23% | 90.28% |

结论：

- correction 时长分布和总体 segment 时长分布接近，`duration_ms` 不是足够强的单独判别特征。
- 短插话仍应作为高风险规则之一，但 verifier 触发不能只靠 `<2s`。
- P0 baseline report 必须输出 `correction duration CDF vs all segment duration CDF`，并新增更有区分度的特征：
  - 相邻 speaker 切换密度。
  - `speaker_count >= 3`。
  - correction 是否跨越相邻不同 speaker。
  - ASR/S2 是否在同一局部窗口反复 flip。
  - 是否疑似 overlap。

### 2. 多人同时说话不能只推给 ASR 层

v0 中“不建议在 S2 层硬解 overlap”仍然成立，但需要补一个更贴近用户痛点的折中方案。

P2 增加 `overlap_suspected` 被动标记：

- 在 S2 Pass 1 输出结构中增加：

```json
{
  "segment_id": "segment_012",
  "speaker_id": "speaker_b",
  "overlap_suspected": true,
  "overlap_confidence": "low|medium|high",
  "overlap_reason": "brief_backchannel|simultaneous_voice|music_or_noise|unclear"
}
```

- 不让 S2 直接改 overlap speaker 归属。
- 只把风险暴露给下游：
  - Review UI 高亮。
  - voice selection 阶段提示该段可能含多人声。
  - benchmark 分层统计。
  - verifier 触发特征之一。

这样同一次音频 LLM 调用中增加字段，增量成本接近 0，同时不违反“speaker 修正保守化”的原则。

### 3. Pre-TTS contradiction 要拆根因

v0 把 38/92 的方向矛盾归为 gate 口径割裂，这个判断过粗。P0 必须拆成至少两类：

| 类型 | 现象 | 主要修复方向 |
|---|---|---|
| A. 估算器/CPS 偏差 | rewrite 后字数方向正确，但真实 TTS 时长与估算相反 | voice/provider CPS 校准、估算器特征增强、provider/voice 路由 |
| B. LLM rewrite 未执行到位 | 要求 shrink/expand，但文本字数没有按方向变化或变化不足 | rewrite prompt、多候选、rewrite 结果 verifier、min/max chars 复核 |

当前历史数据缺少结构化 `pre_rewrite_chars` / `post_rewrite_chars`，所以只能发现 contradiction，不能可靠拆 A/B。

P0 必须新增结构化字段：

```json
{
  "pre_tts_rewrite": {
    "direction": "shrink|expand",
    "estimate_ms_before": 17333,
    "target_ms": 13520,
    "spoken_chars_before": 142,
    "spoken_chars_after": 113,
    "estimate_ms_after": 12880,
    "first_pass_duration_ms_after": 13098,
    "char_delta_pct": -20.4,
    "duration_error_pct_after": -3.1,
    "root_cause_bucket": "estimator_error|rewrite_noncompliance|mixed|unknown"
  }
}
```

P0 baseline report 需要输出：

- contradiction 总数和占比。
- A/B/mixed/unknown 分布。
- 按 provider、voice、content type、segment duration、chars/sec 分组的 contradiction rate。

### 4. Benchmark fixture 必须固化到 repo

`.codex_tmp` 只能作为本次分析缓存，不能作为长期 benchmark 来源。P0 需要产出可版本化 fixture。

建议目录：

```text
tests/fixtures/benchmark/video_translation_quality/
  manifest.json
  jobs/
    bench_001/
      job_meta.json
      segments.json
      metering_snapshot.json
      s2_review_speaker_diff.json
      event_extract.jsonl
    bench_002/
      ...
```

fixture 原则：

- 不保存用户账号、手机号、邮箱、订单、支付信息。
- 不保存完整原视频、完整音频。
- 长 transcript 可按 benchmark 需要裁剪，只保留触发问题的局部上下文。
- `source_ref`、title 等可识别来源字段默认脱敏或哈希。
- `job_id` 可保留原值用于追溯，也可以额外生成 `benchmark_id`。
- benchmark fixture 的新增或替换必须走 PR review，避免基准集悄悄变化。

P0 工具：

- `scripts/benchmark/build_quality_dataset.py`
  - 输入：生产导出的 project artifacts + gateway metrics。
  - 输出：repo 内脱敏 fixture。
  - 同时生成 `manifest.json`，记录抽样理由和风险标签。

- `scripts/benchmark/validate_quality_dataset.py`
  - 校验 fixture 字段完整性。
  - 校验不含用户身份和支付敏感字段。
  - 校验 benchmark 指标可复现。

### 5. 成本模型要回答“是否抵消”

用户的核心问题之一是：多模型、多候选、verifier 的新增成本，能否抵消 TTS rewrite 和人工干预成本。P0 baseline report 必须给货币化模型。

不要先把单价写死在分析脚本里。成本模型应从配置或参数读取：

```json
{
  "unit_costs": {
    "llm_translate_per_1k_tokens_cny": 0.0,
    "llm_rewrite_per_1k_tokens_cny": 0.0,
    "llm_audio_verifier_per_min_cny": 0.0,
    "tts_minimax_per_1k_billed_chars_cny": 0.0,
    "tts_cosyvoice_per_1k_billed_chars_cny": 0.0,
    "tts_volcengine_per_1k_billed_chars_cny": 0.0,
    "human_review_per_min_cny": 0.0
  }
}
```

P0 baseline report 至少输出：

| 项 | 当前量 | 单位成本 | job 级成本 | 说明 |
|---|---:|---:|---:|---|
| LLM 首次翻译 | batch/segment 数 | 配置输入 | 估算 | 可按 provider token 估算 |
| LLM rewrite | `rewrite_count` | 配置输入 | 估算 | 当前 p50=9，p90=39 |
| TTS 首次合成 | `tts_billed_chars` | 配置输入 | 估算 | 必须先补 MiniMax billed chars |
| TTS rewrite 重合成 | rewrite 相关 billed chars | 配置输入 | 估算 | 当前最大成本嫌疑项 |
| speaker verifier | 触发段数/音频分钟 | 配置输入 | 估算 | 只对高风险局部触发 |
| 人工 review | needs_review 段/分钟 | 配置输入 | 估算 | 可先用假设单价 |

Go/No-Go 应改成净收益模型：

```text
net_saving =
  avoided_tts_rewrite_cost
  + avoided_human_review_cost
  - added_llm_candidate_cost
  - added_verifier_cost
```

只有当 `net_saving > 0` 且质量指标不下降，才进入生产默认路径。

### 6. Provider 差异要进入主线 E

v0 对 provider 差异提醒得不够强。当前样本虽然有内容偏差，但 provider/model 差异已经大到不能忽略：

| provider/model | jobs | minutes | avg rewrite/job | rewrite/min | final CN chars/min | billed chars/min |
|---|---:|---:|---:|---:|---:|---:|
| MiniMax Turbo / studio | 37 | 367.65 | 12.30 | 1.238 | 278.38 | 缺失 |
| MiniMax HD / studio | 8 | 164.10 | 29.38 | 1.432 | 286.59 | 缺失 |
| CosyVoice Flash / express | 8 | 30.93 | 7.88 | 2.037 | 299.47 | 610.02 |
| VolcEngine Seed / express | 3 | 15.42 | 10.00 | 1.945 | 266.20 | 275.49 |

不能直接下结论说某 provider 更差，因为样本内容、服务模式、用户选择、是否强制 DSP 都混在一起。但 P0 必须把 provider 作为一级分层维度。

新增主线 E：内容感知 provider/voice 路由。

P0 先只做分析：

- `provider × content_type × duration_bucket × speaker_count × rewrite_rate`
- `provider × voice_strategy × force_dsp_rate`
- `provider × chars_per_min × first_pass_error`
- `provider × billed_chars_per_min × net_cost`

P1/P2 后再决定是否实施：

- 对技术密集、长句、快节奏、多 speaker 内容，给不同 provider/voice 加权。
- voice 自动匹配阶段增加内容类型 hint，而不是直接硬切 provider。
- 只有当 benchmark 显示某类内容在某 provider 上稳定高 rewrite/high cost，才进入默认路由。

### 7. P1 duration budget 增加 gate_history

`duration_budget` 不只保存最终状态，还应保存 append-only 决策流：

```json
{
  "gate_history": [
    {
      "gate": "G1_translation_length",
      "decision": "pass|retry|fail_open",
      "reason": "within_budget|too_long|too_short",
      "target_ms": 13520,
      "spoken_chars": 118,
      "min_chars": 95,
      "max_chars": 125,
      "created_at": "..."
    },
    {
      "gate": "G2_tts_speed",
      "decision": "speed|downgrade_to_rewrite|disabled",
      "reason": "outside_provider_clamp",
      "estimated_ms": 17333,
      "requested_speed": 1.28,
      "final_speed": 1.0,
      "created_at": "..."
    },
    {
      "gate": "G3_alignment",
      "decision": "direct|dsp|rewrite|force_dsp",
      "reason": "first_pass_too_long",
      "first_pass_duration_ms": 13098,
      "error_pct": -3.1,
      "created_at": "..."
    }
  ]
}
```

这比只保存几个字段更利于离线 replay，也能解释线上单段为什么被反复处理。

### 8. P2 verifier 降级策略

`uncertain` 不应无条件回 ASR 或无条件人工 review。建议按 job 价值和服务模式分层：

| 条件 | `uncertain` 处理 |
|---|---|
| 付费订阅 / 高价值 studio / 用户已进入人工审校 | 标记人工 review |
| 免费 trial / express / 低风险内容 | 回 ASR 原归属 |
| correction 会触发大批 TTS 重合成 | 标记人工 review |
| correction 只影响极短 backchannel 且置信低 | 回 ASR 原归属 |

同时记录：

- `verifier_trigger_reason`
- `verifier_decision`
- `verifier_fallback_policy`
- `review_required_by_verifier`

### 9. P0 baseline 增加 Phase 2 before/after

Phase 2 可能已经带来收益，但当前样本里仍有高 rewrite。P0 应做时间切片：

- Phase 2 前样本。
- Phase 2 后样本。
- 同 provider、同内容类型、同长度 bucket 下比较 CDF：
  - first_pass_error_pct
  - rewrite_count/job
  - force_dsp_rate
  - needs_review_rate

若 Phase 2 后收益衰减，可能说明：

- provider 后端 TTS 模型变化导致 CPS 失准。
- voice catalog 标定过期。
- 新内容类型超出了原标定样本。

这会把一部分优化从 prompt/rewrite 转向周期性 calibration。

## v1.2 P0 第一周任务清单

优先只做两件事，其他先不要展开：

1. 固化 benchmark fixture。
   - 从 `.codex_tmp/us_fetch/analysis` 选择 10-12 个 job。
   - 覆盖高 rewrite、低 rewrite、speaker diff、短插话、3+ speaker、不同 provider、不同视频长度。
   - 生成 `tests/fixtures/benchmark/video_translation_quality/manifest.json`。
   - 输出脱敏 fixture 和 validate 脚本。

2. 修成本观测闭环。
   - 给 `/admin/credits-monitor` 增加 `credits_actual_source`：
     - `snapshot`
     - `ledger_derived`
     - `missing`
   - `credits_actual` 缺失时用 ledger capture 聚合推导，并明确标 source。
   - 补 MiniMax `tts_billed_chars`。
   - 报告中显示每个字段 coverage，不再假装数据完整。

完成这两项后再进入 P1 duration budget 贯通。否则 P1/P2 的收益无法可靠量化。

## 风险

- 若 P0 不先补数据闭环，P1/P2 的收益很难量化，容易变成主观判断。
- 若过早引入多候选翻译，会把问题复杂化，且可能掩盖真正的 gate 口径问题。
- 若 speaker verifier 触发范围过宽，音频 LLM 成本会上升；触发范围过窄，则漏掉长段误归属。
- 若把 duration 控制过度交给 LLM，会偏离项目“deterministic retiming”的架构边界。

## 结论

当前最值得做的不是直接换 prompt 或全量多模型，而是：

1. 先补 P0 数据闭环和 benchmark。
2. 用真实历史数据验证三 gate 矛盾和 speaker 风险分层。
3. 再做 P1 duration budget 贯通。
4. 同步做 P2 speaker 保守修正和局部 verifier。
5. 最后才评估 P3 多候选翻译和 LLM 自估时长。

这一路径成本最低、回归面可控，也最符合项目现有架构：DSP first，rewrite fallback，SemanticBlock 为 TTS unit，Jianying draft 为主交付物。
## 2026-04-25 P0 执行记录

本轮已把“离线 benchmark 固化”从方案推进到可复跑工具和首版数据集：

- 新增 `scripts/benchmark/build_quality_dataset.py`：从 `.codex_tmp/us_fetch/analysis` 和已拉取的项目中间产物生成脱敏 fixture。
- 新增 `scripts/benchmark/validate_quality_dataset.py`：校验 fixture 不包含 source URL、payment、email、phone 等高风险字段，不包含音视频媒体文件。
- 新增 `scripts/benchmark/report_quality_baseline.py`：基于 fixture 生成 baseline JSON/Markdown 报告。
- 新增 `tests/fixtures/benchmark/video_translation_quality/`：首版固定 benchmark fixture，抽样 12 个 job，覆盖 pre-TTS contradiction、speaker correction、provider/model 差异和 low-rewrite control。
- 新增 `reports/benchmark/video_translation_quality/latest/`：首版 baseline 报告。

首版 fixture 覆盖摘要：

- 可用历史 job：84；入选 benchmark job：12。
- provider 覆盖：CosyVoice 2、MiniMax 5、VolcEngine 2、unknown 3。
- 覆盖 pre-TTS contradiction 的 job：4。
- 覆盖 speaker correction 的 job：6。
- 覆盖 low-rewrite control 的 job：5。

首版 baseline 摘要：

- segments：993。
- rewrite segment rate：21.35%。
- pre-TTS contradiction rate：70.11%。注意该比例来自有意偏向 contradiction 的 benchmark 抽样，不代表全量生产比例；全量历史样本口径仍以“92 条 pre-TTS rewrite 事件中 38 条 contradiction”为准。
- speaker corrections：58。
- speaker correction duration CDF 与全量 segment duration CDF 仍然接近，继续支持“段长不是 verifier 唯一触发条件”的判断。
- cost proxy：LLM rewrite 约 CNY 0.0885，TTS rewrite 约 CNY 5.9-20.65，人工 speaker fix proxy 约 CNY 174。即使按保守 TTS 单价，一个避免掉的 TTS rewrite 也大约可抵 66-233 次 rewrite 量级的 LLM 调用。

当前仍无法完成 Claude Code 提出的 pre-TTS contradiction A/B 根因拆分，因为现有历史结构化数据缺少 `pre_rewrite_chars` / `post_rewrite_chars`。baseline 已显式输出 `pre_tts_contradiction_root_cause_proxy = {"missing_pre_post_chars": 61}`，后续 P0.2 必须先把这两个字段和 `post_tts_first_pass_ms` 结构化写入，才能区分“估算器/voice CPS 偏差”和“LLM 没按 shrink/expand 方向执行”。

## 2026-04-25 P0.2 成本观测闭环执行记录

本轮已完成 admin credits monitor 的最小闭环改造：

- 后端 `gateway/credits_observability.py` 新增 `credits_actual_source` rollup。
- 来源优先级固定为 `snapshot > ledger_derived > missing`。
- `snapshot` 表示 `metering_snapshot.credits_actual` 已存在。
- `ledger_derived` 表示 snapshot 缺失，但可从 `credits_ledger` 的 `capture` 记录按 job 聚合 `sum(abs(credits_delta))` 推导。
- `missing` 表示 snapshot 和 capture ledger 都缺失，不能被当作 0 成本。
- `/api/admin/credits/summary` 返回全量 `credits_actual_source`。
- `/api/admin/credits/cost-metrics` 返回窗口内 `credits_actual_effective_sum`、`estimate_effective_delta_pct` 和 `credits_actual_source`。
- `FIELD_STATUS["metering_snapshot.credits_actual"]` 覆盖为 `LIVE_PARTIAL`，避免继续把该字段误读成完整写入。
- 前端 `frontend-next/src/app/(app)/admin/credits-monitor/page.tsx` 新增“实扣点数(有效)”卡片，展示 snapshot / ledger / missing 分布，并用 effective actual 计算预估/实扣偏差率。

验证：

- `pytest -q tests/test_credits_observability.py tests/test_quality_benchmark_tools.py`：36 passed。
- `python -m py_compile gateway/credits_observability.py scripts/benchmark/quality_dataset.py scripts/benchmark/build_quality_dataset.py scripts/benchmark/validate_quality_dataset.py scripts/benchmark/report_quality_baseline.py`：通过。
- `npm run lint --prefix frontend-next -- "src/app/(app)/admin/credits-monitor/page.tsx"`：通过。
- 全量 `npm run lint --prefix frontend-next` 仍有既有错误，集中在 `admin/s2-monitor`、`admin/settings`、`projects` 等非本轮改动文件；本轮改动文件单独 lint 通过。

P0 后续未完成项：

- MiniMax `tts_billed_chars` 需要在 Pipeline/TTS writeback 侧补齐结构化写入。
- Pre-TTS rewrite 需要新增 `pre_rewrite_chars`、`post_rewrite_chars`、`post_tts_first_pass_ms`、`pre_tts_contradiction` 等结构化字段，避免继续从日志反解。

## 2026-04-25 P0.3 收尾执行记录

本轮已完成 P0 方案中剩余两个观测字段闭环：

MiniMax `tts_billed_chars`：

- `TTSResult.billed_chars` 原先已经在 MiniMax 分支按 `cn_chars * 2` 写入。
- 本轮修复了 S3/cache hit 路径中未保存 `generate_all(segments_needing_tts)` 返回值的问题；该路径现在会把新生成段的 `TTSResult.billed_chars` 汇总进 `_report_job_metering()`。
- 缓存复用段不重复计入本次 TTS bill，避免把复用缓存误算成新增成本。

Pre-TTS rewrite 结构化审计：

- `DubbingSegment` 新增观测字段：
  - `pre_tts_rewrite_direction`
  - `pre_tts_estimate_ms`
  - `pre_tts_target_ms`
  - `pre_tts_pre_chars`
  - `pre_tts_post_chars`
  - `pre_tts_post_tts_first_pass_ms`
  - `pre_tts_contradiction`
- `_pre_rewrite_obvious_overshoot_segments_before_tts()` 在 rewrite 成功时写入方向、估算时长、目标时长和改写前后 spoken-char 数。
- `SegmentAligner._align_one()` 在首轮 TTS 实测后，把 `first_pass_duration_ms` 回填到 `pre_tts_post_tts_first_pass_ms`，并按方向判断 contradiction：
  - overshoot/shrink 后仍短超 5%；
  - undershoot/expand 后仍长超 5%。
- `_report_job_metering()` 新增：
  - `pre_tts_rewrite_count`
  - `pre_tts_contradiction_count`
  - `pre_tts_contradiction_rate`
  - `pre_tts_rewrite_events`
- Gateway `/job-api/jobs/{job_id}/metering` 已允许上述字段写入 `metering_snapshot`。
- `FIELD_STATUS["metering_snapshot.pre_tts_rewrite_events"]` 标为 `LIVE_PARTIAL`，因为只有 pre-TTS rewrite 实际触发的 job 才会有该字段。
- benchmark 构建脚本已兼容这些新字段；未来新 job 进入 fixture 后，可以直接做 contradiction A/B 根因拆分。

验证：

- `pytest -q tests/test_job_metering_writeback.py tests/test_process_pipeline.py::test_process_pipeline_pre_rewrites_obvious_overshoot_before_tts tests/test_aligner.py::test_aligner_marks_pre_tts_contradiction_after_first_pass tests/test_credits_observability.py tests/test_quality_benchmark_tools.py`：53 passed。
- `python -m py_compile src/pipeline/process.py src/services/gemini/translator.py src/services/alignment/aligner.py gateway/job_intercept.py gateway/credits_observability.py scripts/benchmark/quality_dataset.py`：通过。
- `python scripts/benchmark/validate_quality_dataset.py`：通过。
- `python scripts/benchmark/report_quality_baseline.py`：通过。

P0 当前状态：

- 离线 benchmark fixture 已固化。
- `credits_actual` 来源闭环已补。
- `tts_billed_chars` 写回路径已补齐缓存路径。
- pre-TTS rewrite contradiction 结构化数据已补。

下一步进入 P1 前，建议先跑 1-2 个新 job 验证 `metering_snapshot` 是否出现 `pre_tts_rewrite_events` 和 MiniMax `tts_billed_chars`，确认生产数据写入无误后再改 duration gate 行为。

## 2026-04-25 P0.4 生产新 job 验证与方案调整

P0.3 部署后两条新 job 已验证观测链路生效：

- `job_b8a76a5a8ab64c03a4478602c45b6032`（Naval，约 20 分钟）：
  - `total_segments=49`
  - `rewrite_count=31`
  - `pre_tts_rewrite_count=24`
  - `pre_tts_contradiction_count=2`
  - `needs_review_count=17`
  - `tts_billed_chars=11220`
  - `alignment_method` 以 `force_dsp/dsp/direct` 为主，短 utterance 和极短 target 段是 needs_review 主要来源之一。
- `job_58a6fb6a7eac46dc9e5d2811ded8bd38`（Munger，约 10 分钟）：
  - `total_segments=11`
  - `rewrite_count=1`
  - `pre_tts_rewrite_count=1`
  - `pre_tts_contradiction_count=1`
  - `needs_review_count=0`
  - `tts_billed_chars=5136`
  - `alignment_method=direct/dsp`，整体质量接近预期，但 segment 11 出现 pre-TTS shrink 后首轮 TTS 反而短于 target 的 contradiction。

结论：

- P0 观测字段已经能支持后续 P1/P2 判断：`pre_tts_rewrite_events`、`pre_tts_contradiction_count`、`tts_billed_chars` 已在生产 job 中出现。
- 三 gate 矛盾仍然成立，但新数据进一步暴露出一个 P0 级执行 bug：审校后 cache-hit 继续跑时，pre-TTS rewrite 分支没有使用已经持久化的 `audio/probe_calibration.json`，而是回退到默认 `4.5` 字/秒。
- Munger segment 11 的估算值 `30222ms` 可由 `136 spoken chars / 4.5` 精确复现；同 job 的 probe 校准为 speaker 级约 `4.86` 字/秒，说明这次 contradiction 至少部分来自“cache-hit 路径丢失校准值”，而不是单纯 prompt 问题。
- Naval job 说明 P2 仍需保留“短 utterance / force_dsp / needs_review”专项，但它应排在 cache-hit 校准修复之后，否则会混入估算器假阳性。

方案调整：

- P1 前新增 P0 hotfix：cache-hit / translation-review resume 路径的 pre-TTS rewrite 必须优先使用 `_probe_chars_per_second` 与 `_probe_chars_per_second_by_speaker`，这些值来自 catalog 或持久化 probe 校准；只有缺失时才回退到缓存 TTS 段校准或默认 `4.5`。
- 已在 `src/pipeline/process.py` 修复并部署到美国主机：
  - 远端备份：`/opt/aivideotrans/deploy_backups/pre_tts_cps_fix_20260425T105923Z`
  - 远端验证：`python -m py_compile /opt/aivideotrans/app/src/pipeline/process.py` 通过；app 容器重启后健康检查通过。
- 新增回归测试：`test_process_pipeline_uses_persisted_probe_cps_for_pre_tts_on_translation_cache_hit`，覆盖“已有翻译缓存、无 TTS 缓存、存在持久化 probe 校准”的真实故障形态。

下一步：

- 再跑 1 个同类 Studio job，确认 pre-TTS estimate 不再精确落在 `spoken_chars / 4.5`。
- 若新 job 的 contradiction 明显下降，进入 P1 duration budget/gate history；若仍高，则先做 contradiction A/B 拆分，区分估算器偏差和 LLM 改写方向失败。
- Naval 类短 utterance 问题作为 P2 前置分析项：统计 `target_duration_ms < 1500`、`alignment_method=force_dsp`、`needs_review=true` 的交集，再决定是否加入短段专用策略。

## 2026-04-25 P1 前置修复执行记录

本轮先做两个低风险前置修复，不进入完整 `DurationGovernor` 或 gate history 重构。

### 1. Pre-TTS rewrite 字数护栏

目的：避免 overshoot 段被 pre-TTS rewrite 一次性缩得过短，随后 TTS first pass 又变成 undershoot contradiction。

实现：

- `_pre_rewrite_obvious_overshoot_segments_before_tts()` 在接受 LLM rewrite 前，统一用 spoken-char 口径计算 `pre_chars/post_chars`。
- overshoot rewrite 必须满足：
  - `post_chars < pre_chars`
  - `post_chars >= target_duration_ms * cps`
  - shrink 幅度默认不超过 35%，只有当理论 target 本身需要更大压缩时，才允许放宽到“required shrink + 5%”，上限 60%。
- undershoot rewrite 对称约束：
  - `post_chars > pre_chars`
  - `post_chars <= target_duration_ms * cps`
  - expand 幅度默认不超过 35%，极端情况下上限 60%。
- 支持 `rewrite_for_duration_with_profile()` 的 rewriter 会收到更保守的 ratio window：
  - overshoot：`preferred_min_ratio=1.0`，`preferred_max_ratio=1.12`
  - undershoot：`preferred_min_ratio=0.88`，`preferred_max_ratio=1.0`
- 不满足护栏的 rewrite 会被拒绝，保留原文进入 TTS/speed/DSP 后续路径。

这不是针对单个芒格视频的过拟合：阈值来自通用的 duration budget 关系，输入变量是 `pre_chars`、`target_duration_ms` 和当前 speaker/provider CPS，不绑定具体视频、speaker 或内容类型。

### 2. 极短 utterance force-DSP 降噪

目的：减少“嗯、对、是的”这类极短口语段因为 target 太短而落到 `force_dsp + needs_review` 的噪声。

实现：

- `SegmentAligner` 保持原有 `alignment_method="force_dsp"`，不改变输出结构。
- 仅当同时满足以下条件时，把 `needs_review` 从 `True` 降为 `False`：
  - `target_duration_ms <= 1500`
  - first-pass TTS `actual_duration_ms <= 3500`
  - `actual_duration_ms / target_duration_ms <= 5.0`
  - `count_spoken_chars(cn_text) <= 18`
- 长文本、长 target、极端倍率偏差仍然保留 `needs_review=True`。

这个策略只降噪极短 backchannel，不处理多人重叠说话和 speaker 归属，避免把 P2 speaker verifier 的问题提前混入 alignment。

验证：

- `python -m py_compile src\pipeline\process.py src\services\alignment\aligner.py`
- `pytest -q tests/test_process_pipeline.py::test_process_pipeline_pre_rewrites_obvious_overshoot_before_tts tests/test_process_pipeline.py::test_process_pipeline_rejects_pre_tts_rewrite_below_char_floor tests/test_process_pipeline.py::test_process_pipeline_pre_tts_rewrite_when_speed_cant_handle tests/test_process_pipeline.py::test_pre_tts_rewrite_skip_disabled_when_speed_flag_off tests/test_process_pipeline.py::test_pre_tts_rewrite_skip_disabled_for_provider_without_speed tests/test_process_pipeline.py::test_pre_tts_rewrite_skip_job_provider_without_speed_still_rewrites`
- `pytest -q tests/test_aligner.py::test_aligner_short_force_dsp_backchannel_does_not_require_review tests/test_aligner.py::test_aligner_short_force_dsp_long_text_still_requires_review tests/test_aligner.py::test_aligner_uses_force_dsp_and_marks_review_when_diff_exceeds_threshold tests/test_aligner.py::test_aligner_marks_pre_tts_contradiction_after_first_pass`

部署：

- 已部署到美国主机。
- 远端备份：`/opt/aivideotrans/deploy_backups/p1_guardrails_20260425T120042Z`
- 远端验证：`python -m py_compile /opt/aivideotrans/app/src/pipeline/process.py /opt/aivideotrans/app/src/services/alignment/aligner.py` 通过；`scripts/linux_remote_workbench_preflight.py app-health` 通过。

下一步生产验证指标：

- `pre_tts_rewrite_count` 是否下降。
- `pre_tts_contradiction_count/rate` 是否下降，尤其是 overshoot shrink 后短超 target 的样本。
- `needs_review_count` 是否因极短 `force_dsp` 降噪下降。
- final duration p90 和人工试听质量不能恶化。

## 2026-04-25 P1 前置修复生产验证

部署后新 job：

- `job_48a6013c9782424aa424ae8803480b41`
- 标题：`Just a regular billionaire`
- provider/model：MiniMax `speech-2.8-hd`
- 源视频时长：约 7.67 分钟
- 总段数：62

关键结果：

- `rewrite_count=10`
- `pre_tts_rewrite_count=4`
- `pre_tts_contradiction_count=2`
- `needs_review_count=24`
- `tts_billed_chars=4800`
- alignment method：
  - `direct=15`
  - `dsp=7`
  - `force_dsp=36`
  - `rewrite_direct=3`
  - `rewrite_dsp=1`

修复效果判断：

- P1-a 生效：日志中有 10 个 pre-TTS rewrite 候选，其中 4 个通过护栏，6 个被拒绝：
  - rejected segment：37、41、45、47、49、53
  - 这些拒绝都属于 LLM 输出触碰了 shrink 下限或 target 字数下限，避免了继续接受过度缩写。
- P1-b 生效：`force_dsp=36`，但只有 24 个仍 `needs_review`，有 12 个短段 `force_dsp` 被降噪。
- 这次不再出现“几乎每段 pre-TTS rewrite”的情况；pre-TTS accepted rewrite 只有 4/62。

需要进一步区分：

- 当前 `pre_tts_contradiction_count=2` 的两个段（32、57）虽然方向上属于 overshoot shrink 后偏短，但都落在 `direct` 可接受范围内：
  - segment 32：target 12152ms，first pass 11238ms，差约 914ms。
  - segment 57：target 23414ms，first pass 21904ms，差约 1510ms。
- 因此后续指标应拆成：
  - `pre_tts_contradiction_count`
  - `harmful_pre_tts_contradiction_count`
- harmful 定义建议：方向矛盾且不满足 direct/dsp 接受条件，或最终触发 post-TTS rewrite / needs_review。

仍然存在的问题：

- 主要压力已从 pre-TTS rewrite 转移到 MiniMax first-pass TTS 偏长：
  - `force_dsp=36/62`
  - 其中 11 个 target >= 5000ms 的长段仍依赖 `force_dsp`。
- 很多段 first-pass / target ratio 超过 1.6，甚至超过 2.0，说明仅靠 pre-TTS rewrite 护栏不能解决 provider/voice CPS 估计偏差和翻译长度自然过长的问题。
- pre-TTS rewrite 被拒绝后，当前只是保留原文继续 TTS；这保证质量保守，但没有减少一次 LLM rewrite 调用的耗时和成本。

下一步建议：

1. 先调整观测指标，新增 harmful contradiction，避免把“方向矛盾但最终 direct 可接受”的段误判为质量失败。
2. 做 P1-c：pre-TTS rewrite prompt 显式带入 `target_chars_floor/target_chars_ceiling`，要求模型输出前自检 spoken-char 数，减少“调用后被护栏拒绝”的比例。
3. 做 force-DSP 分类报表：按 `target_duration_ms`、`first_pass_ratio`、`speaker_id/voice_id`、`tts_provider` 分桶，确认是某个 voice CPS 偏差，还是翻译阶段整体字数偏长。

## 2026-04-25 P1-c 执行记录

本轮按生产验证结论补两个小改动：

1. 新增 harmful contradiction 观测字段。
   - `DubbingSegment` 新增 `pre_tts_harmful_contradiction`。
   - `SegmentAligner` 在最终 alignment method / needs_review 决策后计算：
     - `pre_tts_contradiction=true`
     - 且最终不是 `direct/dsp`，或仍 `needs_review=true`
     - 才算 harmful。
   - `_report_job_metering()` 新增：
     - `harmful_pre_tts_contradiction_count`
     - `harmful_pre_tts_contradiction_rate`
     - 每条 `pre_tts_rewrite_events[]` 增加 `harmful_contradiction`。
   - Gateway `/job-api/jobs/{job_id}/metering` 已允许上述两个汇总字段写入 `metering_snapshot`。

2. Pre-TTS rewrite prompt 显式接收代码护栏字数范围。
   - `_pre_tts_rewrite_char_bounds()` 统一计算本段可接受的 spoken-char `[lower, upper]`。
   - 这个范围同时用于：
     - 传给 `GeminiRewriter.rewrite_for_duration_with_profile()`
     - rewrite 后的代码侧 guardrail 复核。
   - `GeminiRewriter` 在 prompt 末尾追加硬约束说明：
     - 自检 spoken-char，只统计中文、英文、数字；
     - 最终文本必须落在传入的 `[lower, upper]`；
     - 不输出字数、解释或引号。

验证：

- `python -m py_compile src\pipeline\process.py src\services\alignment\aligner.py src\services\gemini\rewriter.py src\services\gemini\translator.py gateway\job_intercept.py gateway\credits_observability.py scripts\benchmark\quality_dataset.py`
- `pytest -q tests/test_rewriter.py::test_rewriter_profile_uses_explicit_char_bounds tests/test_process_pipeline.py::test_process_pipeline_passes_pre_tts_guardrail_bounds_to_profile_rewriter tests/test_process_pipeline.py::test_process_pipeline_pre_rewrites_obvious_overshoot_before_tts tests/test_process_pipeline.py::test_process_pipeline_rejects_pre_tts_rewrite_below_char_floor`
- `pytest -q tests/test_aligner.py::test_aligner_marks_pre_tts_contradiction_after_first_pass tests/test_aligner.py::test_aligner_marks_direct_pre_tts_contradiction_as_not_harmful tests/test_job_metering_writeback.py::TestReportJobMeteringCallback::test_pre_tts_rewrite_events_are_reported tests/test_job_metering_writeback.py::TestUpdateJobMetering::test_merges_fields_into_snapshot tests/test_quality_benchmark_tools.py`

部署：

- 已部署到美国主机。
- 远端备份：`/opt/aivideotrans/deploy_backups/p1c_harmful_prompt_20260425T125638Z`
- 远端验证：
  - app/gateway 容器均为 `healthy`。
  - `python scripts/linux_remote_workbench_preflight.py app-health` 通过。
  - Gateway `http://127.0.0.1:8880/gateway/health` 返回 200。
  - 远端代码 markers 已确认：
    - `harmful_pre_tts_contradiction_count`
    - `target_lower_chars=target_lower_chars`
    - `字数硬约束`

下一轮生产验证重点：

- `pre_tts_rewrite rejected ... outside guardrails` 日志数量是否下降。
- `pre_tts_contradiction_count` 与 `harmful_pre_tts_contradiction_count` 的差距是否拉开。
- harmful contradiction 应优先作为 P1 Go/No-Go 指标；普通 contradiction 只作为诊断指标。

## 2026-04-25 P1-c 生产验证与 P1-d 修正

部署后新 job：

- `job_ef0a73e543e64b428dbeb3b3f5741b4f`
- 标题：`Disappearance of UFO expert Gen. Neil McCasland 'alarming': Coulthart | Jesse Weber Live`
- provider/model：MiniMax `speech-2.8-hd`
- 源视频时长：约 8.05 分钟
- 总段数：13

关键结果：

- `rewrite_count=6`
- `pre_tts_rewrite_count=4`
- `pre_tts_contradiction_count=2`
- `harmful_pre_tts_contradiction_count=1`
- `needs_review_count=0`
- alignment method：
  - `direct=7`
  - `dsp=4`
  - `force_dsp=1`
  - `rewrite_direct=1`

验证结论：

- P1-c 生效：事件日志中没有 `outside guardrails` / rejected pre-TTS rewrite，说明显式 `[lower, upper]` prompt 已经减少“调用后被代码侧拒绝”的浪费。
- speaker 侧本 job 没有暴露误归属：`s2_review_speaker_diff.json` 中 `speaker_diffs` 为空。
- 普通 contradiction 与 harmful contradiction 已经拉开：
  - segment 13：54 字缩到 47 字，first pass 比目标短约 10.6%，但最终 `direct`，不算 harmful。
  - segment 12：99 字缩到 54 字，目标 11939ms，first pass 9752ms，最终触发 `rewrite_direct`，算 harmful。

P1-d 判断：

- segment 12 不是“模型没按字数约束输出”的问题；54 字落在当时 guardrail 允许范围内。
- 根因更接近“短目标段 + 大幅缩短时，TTS 实际语速比 CPS 估算更快”，导致 pre-TTS shrink 后反向变成 undershoot。
- 因此不应全局收紧 shrink cap，避免伤到 segment 6/8 这类已经对齐很好的普通 overshoot 段。

P1-d 实施：

- 只对 overshoot 且满足以下条件的段落进入高风险分支：
  - `target_duration_ms <= 12000`
  - 根据 CPS 估算需要缩短比例 `required_shrink >= 45%`
- 高风险分支把最大允许缩短比例收紧到 `40%`，并给上限保留 5% slack。
- 类似本次 segment 12 的输入：
  - 原 99 spoken chars
  - target 11939ms
  - CPS 约 4.088
  - 旧范围约 `49~55`
  - 新范围变为 `60~63`
- 这样会拒绝 54 字这种过短结果，但不影响普通 20 秒段的 `90~101` 范围。

验证：

- `python -m py_compile src\pipeline\process.py`
- `pytest -q tests/test_process_pipeline.py::test_process_pipeline_passes_pre_tts_guardrail_bounds_to_profile_rewriter tests/test_process_pipeline.py::test_process_pipeline_tightens_short_high_shrink_pre_tts_bounds tests/test_process_pipeline.py::test_process_pipeline_rejects_short_high_shrink_rewrite_below_risk_floor tests/test_process_pipeline.py::test_process_pipeline_pre_rewrites_obvious_overshoot_before_tts tests/test_process_pipeline.py::test_process_pipeline_rejects_pre_tts_rewrite_below_char_floor`
- `pytest -q tests/test_process_pipeline.py -k "pre_tts or Pre_TTS or guardrail"`

部署：

- 已部署到美国主机。
- 远端备份：`/opt/aivideotrans/deploy_backups/p1d_high_shrink_20260425T133041Z`
- 远端验证：
  - `docker exec aivideotrans-app python -m py_compile /opt/aivideotrans/app/src/pipeline/process.py` 通过。
  - `docker exec aivideotrans-app python scripts/linux_remote_workbench_preflight.py app-health` 通过。
  - Gateway `http://127.0.0.1:8880/gateway/health` 返回 200。
  - 远端 markers 已确认：
    - `PRE_TTS_REWRITE_HIGH_SHRINK_RISK_TARGET_MS = 12_000`
    - `PRE_TTS_REWRITE_HIGH_SHRINK_RISK_MAX_CHANGE_RATIO = 0.40`

下一轮生产验证重点：

- 是否还出现类似 `pre_chars≈99 -> post_chars≈54` 的高缩短 harmful contradiction。
- 高风险段被拒绝后，是否导致原文过长、`force_dsp` 或 post-TTS rewrite 反弹；如果反弹，需要把高风险分支从“保守字数下限”升级为“高风险二次目标区间重写”，而不是继续调全局阈值。
- 普通 20s+ overshoot 段的 pre-TTS rewrite 成功率不能下降。

## 2026-04-25 P1-d 生产验证：短段成为主要剩余瓶颈

部署后新 job：

- `job_b98bdc65841f4254bdc2a410eb6e0939`
- 标题：`摩根大通CEO杰米戴蒙谈AI与宏观经济`
- provider/model：MiniMax `speech-2.8-hd`，实际分段中 speaker_a 使用 CosyVoice `longanlang_v3`，speaker_b 使用 MiniMax 克隆音色。
- 源视频时长：约 19.13 分钟
- 总段数：86

关键结果：

- `rewrite_count=48`
- `pre_tts_rewrite_count=32`
- `pre_tts_contradiction_count=6`
- `harmful_pre_tts_contradiction_count=0`
- `needs_review_count=25`
- alignment method：
  - `direct=18`
  - `dsp=17`
  - `force_dsp=44`
  - `rewrite_dsp=6`
  - `rewrite_direct=1`

P1-d 验证结论：

- P1-d 目标成立：没有再出现 harmful pre-TTS contradiction。
- `pre_tts_rewrite rejected ... outside guardrails` 仍为 0，说明 P1-c 的显式字数范围 prompt 继续有效。
- 32 个 pre-TTS rewrite 中，6 个普通 contradiction 最终都不是 harmful；这类段落可保留为诊断指标，不应作为质量失败指标。
- speaker 侧本 job 未暴露误归属：`s2_review_speaker_diff.json` 中 speaker diff 为空。

新的主要瓶颈：

- `needs_review=25/86` 主要来自短段 force-DSP，而不是 pre-TTS contradiction。
- 当前 pre-TTS rewrite 对 `target_duration_ms < 8000` 的段落直接跳过，导致 2~8 秒短句没有进入文字压缩，TTS 首次音频偏长后只能走 `force_dsp`。
- needs_review 按目标时长分桶：
  - `<1s`：17 段，3 段 needs_review，0 段 pre-TTS。
  - `1~2s`：7 段，1 段 needs_review，0 段 pre-TTS。
  - `2~5s`：15 段，14 段 needs_review，0 段 pre-TTS。
  - `5~8s`：7 段，3 段 needs_review，0 段 pre-TTS。
  - `8~12s`：7 段，1 段 needs_review，4 段 pre-TTS。
  - `12~20s`：10 段，2 段 needs_review，6 段 pre-TTS。
  - `20s+`：23 段，1 段 needs_review，22 段 pre-TTS。
- provider / speaker 维度：
  - CosyVoice speaker_a `force_dsp=24`，needs_review=11，rewrite_sum=0。
  - MiniMax speaker_b `force_dsp=20`，needs_review=14，rewrite_sum=9。

下一步建议 P1-e：

- 不再继续调 P1-d 全局 shrink 参数。
- 增加“短段 overshoot 专用处理”：
  - `target 2~8s` 且估算/目标比超过较高阈值时，允许进入短段 pre-TTS rewrite。
  - 使用更保守的下限和更小的改写窗口，避免把短句压成信息残缺。
  - `<1s` 微插话不建议走 rewrite；应另行考虑合并到相邻 SemanticBlock、跳过配音、或在 review 中降噪。
- 同时增加短段统计字段：
  - `short_segment_count`
  - `short_segment_needs_review_count`
  - `short_segment_force_dsp_count`
  - `micro_segment_count`

## 2026-04-25 P1-e 执行记录

本轮按 P1-d 生产验证结果补“短段 overshoot 专用处理”。

实现：

- `2s <= target_duration_ms < 8s` 的短段不再一律跳过 pre-TTS rewrite。
- 短段只处理明显 overshoot：
  - `estimated_duration_ms / target_duration_ms - 1 >= 45%`
  - 只做 shrink，不做 undershoot expand。
- `<2s` 段仍跳过 pre-TTS rewrite，避免把“对 / 是的 / 嗯”这类微插话交给 LLM 改坏。
- 短段复用现有 char guardrail 和 P1-d 高缩短风险分支：
  - 代码侧计算 `[target_lower_chars, target_upper_chars]`
  - prompt 显式要求落在该范围
  - rewrite 后再次用同一范围复核
- 新增 metering 字段：
  - `micro_segment_count`
  - `short_segment_count`
  - `short_segment_needs_review_count`
  - `short_segment_force_dsp_count`
- Gateway `/job-api/jobs/{job_id}/metering` allowlist 已允许这些字段写入 `metering_snapshot`。
- credits monitor field status 增加 `metering_snapshot.short_segment_needs_review_count`，避免短段观测字段变成隐形数据。

验证：

- `python -m py_compile src\pipeline\process.py gateway\job_intercept.py gateway\credits_observability.py scripts\benchmark\quality_dataset.py`
- `pytest -q tests/test_process_pipeline.py::test_process_pipeline_pre_rewrites_short_obvious_overshoot_before_tts tests/test_process_pipeline.py::test_process_pipeline_skips_short_low_overshoot_before_tts tests/test_process_pipeline.py::test_process_pipeline_skips_micro_segment_pre_tts_rewrite tests/test_process_pipeline.py::test_process_pipeline_pre_rewrites_obvious_overshoot_before_tts tests/test_process_pipeline.py::test_process_pipeline_rejects_short_high_shrink_rewrite_below_risk_floor`
- `pytest -q tests/test_job_metering_writeback.py::TestUpdateJobMetering::test_merges_fields_into_snapshot tests/test_job_metering_writeback.py::TestReportJobMeteringCallback::test_pre_tts_rewrite_events_are_reported tests/test_job_metering_writeback.py::TestReportJobMeteringCallback::test_real_dubbing_segment_path`
- `pytest -q tests/test_process_pipeline.py -k "pre_tts or Pre_TTS or guardrail"`
- `pytest -q tests/test_quality_benchmark_tools.py tests/test_job_metering_writeback.py`

下一轮生产验证重点：

- `short_segment_needs_review_count / short_segment_count` 是否下降。
- `2~5s` 和 `5~8s` 段是否开始出现 pre-TTS rewrite，且没有新增 harmful contradiction。
- `pre_tts_rewrite_count` 会合理上升；Go/No-Go 不看 rewrite 数单项，而看 short needs_review 与 harmful contradiction 是否改善。
- `<2s` 微插话的 `needs_review` 若仍高，下一步应走“微段合并 / review 降噪”，不是继续放开 LLM rewrite。

部署：

- 已部署到美国主机。
- 远端备份：`/opt/aivideotrans/deploy_backups/p1e_short_segments_20260425T152511Z`
- 远端验证：
  - app `process.py`、benchmark `quality_dataset.py` py_compile 通过。
  - gateway `job_intercept.py`、`credits_observability.py` py_compile 通过。
  - gateway 镜像已重建，app / gateway 容器均为 healthy。
  - `docker exec aivideotrans-app python scripts/linux_remote_workbench_preflight.py app-health` 通过。
  - Gateway `http://127.0.0.1:8880/gateway/health` 返回 200。
  - 远端 markers 已确认：
    - `PRE_TTS_REWRITE_SHORT_OVERSHOOT_RATIO = 0.45`
    - `short_segment_needs_review_count`

## 2026-04-26 P1-e 生产验证：两条新视频

验证 job A：

- `job_ddba907f1cb146c5b8f9914efe4a7ab2`
- 标题：`Netskope CEO谈AI网络威胁与中国模型`
- 总段数：18
- `rewrite_count=18`
- `pre_tts_rewrite_count=14`
- `pre_tts_contradiction_count=6`
- `harmful_pre_tts_contradiction_count=1`
- `needs_review_count=0`
- `micro_segment_count=1`
- `short_segment_count=2`
- `short_segment_needs_review_count=0`
- `short_segment_force_dsp_count=0`

验证 job B：

- `job_6d1c4f0e1a4646199fef8232af416c89`
- 标题：`巴菲特回顾2008年金融危机`
- 总段数：63
- `rewrite_count=40`
- `pre_tts_rewrite_count=29`
- `pre_tts_contradiction_count=8`
- `harmful_pre_tts_contradiction_count=1`
- `needs_review_count=5`
- `micro_segment_count=10`
- `short_segment_count=15`
- `short_segment_needs_review_count=4`
- `short_segment_force_dsp_count=4`

与 P1-e 前的参考 job 对比：

- P1-e 前参考 job `job_b98bdc...`：
  - `needs_review=25/86`
  - `2~5s`：15 段，14 段 needs_review，0 段 pre-TTS
  - `5~8s`：7 段，3 段 needs_review，0 段 pre-TTS
- P1-e 后 job B：
  - `needs_review=5/63`
  - `2~5s`：9 段，3 段 needs_review，6 段 pre-TTS
  - `5~8s`：6 段，1 段 needs_review，3 段 pre-TTS
- P1-e 后 job A：
  - `needs_review=0/18`
  - `2~5s`：1 段，0 段 needs_review，1 段 pre-TTS
  - `5~8s`：1 段，0 段 needs_review，1 段 pre-TTS

结论：

- P1-e 方向成立：短段不再完全错过 pre-TTS rewrite，短段 needs_review 明显下降。
- `pre_tts rejected` 没有大规模反弹：
  - job A：0 次 rejected。
  - job B：1 次 rejected，segment 60，Gemini 超时后 fallback 到 OpenAI，输出 13 chars 低于 guardrail，被正确拒绝。
- harmful contradiction 仍存在但稀少：
  - job A：segment 2，长段 shrink 后 first pass 偏短，后续 `rewrite_direct` 修正。
  - job B：segment 1，约 10.5s 段 shrink 后 first pass 偏短，后续 `rewrite_direct` 修正。
  - 这两例不是 P1-e 放开 2~8s 短段造成的系统性质量回退。
- Speaker 侧：
  - job A speaker diff 为空。
  - job B 有 2 条短相邻行 speaker correction：`—to bail out?` 与 `There was huge.`，属于短插话/接话边界，不是大面积误归属。

剩余问题：

- job B 的 5 个 needs_review 中，4 个是 `2~8s` 短段，1 个是 `8~12s` 近短段。
- 其中部分没有触发 pre-TTS，是因为估算器按 spoken chars 和 probe CPS 判断没有超过阈值，但真实 MiniMax TTS first pass 偏长：
  - segment 17：target 2537ms，first pass 3750ms，未 pre-TTS。
  - segment 43：target 6186ms，first pass 10275ms，未 pre-TTS。
  - segment 7：target 9568ms，first pass 14176ms，未 pre-TTS。
- segment 60 触发过 pre-TTS，但 fallback 输出过短被 guardrail 拒绝，保守保留原文后仍进入 force-DSP。

下一步建议：

- P1-e 可以判定为阶段性有效，不需要回滚。
- 若继续优化，建议 P1-f 只做“小幅 estimator margin”：
  - `2~8s` 短段 overshoot 阈值从 45% 下调到 30%。
  - `8~12s` 近短段新增 30% overshoot 触发阈值。
  - 保持 `<2s` 微插话不走 LLM rewrite。
  - 保持现有 char guardrail；fallback 输出低于下限仍拒绝。
- Go/No-Go 指标：
  - `short_segment_needs_review_count / short_segment_count` 继续下降。
  - `harmful_pre_tts_contradiction_count` 不得明显上升。
  - `pre_tts rejected` 不得大规模反弹。

## 2026-04-26 P1-f 执行记录：短段估算安全边际与近短段纳入

本轮按 P1-e 生产验证结果推进小幅修正，不改变主流程：

- `<2s` 微插话继续跳过 pre-TTS rewrite，避免把极短接话交给 LLM 改坏。
- `2s <= target_duration_ms < 8s` 短段的 pre-TTS overshoot 触发阈值从 `45%` 下调到 `30%`。
- `8s <= target_duration_ms < 12s` 近短段新增 `30%` overshoot 触发阈值。
- 仅在短段 / 近短段的“是否触发 rewrite”判断里加入 `1.15` estimator margin，用于补偿 MiniMax 等 provider 在短段上真实 TTS 偏长的问题。
- speed-skip 判断同样使用该 decision estimate，避免边界短段被“理论可调速”掩盖。
- 字数 guardrail、CPS 估算和 `pre_tts_estimate_ms` 仍使用原始 estimate，不用放大后的 decision estimate，避免把安全边际误写进审计和字数预算。
- 现有 lower/upper char guardrail 保持不变；fallback 输出低于下限仍拒绝。

该修正不是针对单个视频的特例，而是覆盖 P1-e 后暴露的共同模式：`2~12s` 短/近短段在 spoken chars + probe CPS 下估算未达旧阈值，但真实 TTS first pass 偏长并进入 `force_dsp` / `needs_review`。

本地验证：

- `python -m py_compile src\pipeline\process.py`
- `pytest -q tests/test_process_pipeline.py::test_process_pipeline_pre_rewrites_short_obvious_overshoot_before_tts tests/test_process_pipeline.py::test_process_pipeline_skips_short_low_overshoot_before_tts tests/test_process_pipeline.py::test_process_pipeline_short_estimate_margin_catches_borderline_overshoot tests/test_process_pipeline.py::test_process_pipeline_near_short_estimate_margin_catches_borderline_overshoot tests/test_process_pipeline.py::test_process_pipeline_skips_micro_segment_pre_tts_rewrite`
- `pytest -q tests/test_process_pipeline.py -k "pre_tts or Pre_TTS or guardrail"`
- `pytest -q tests/test_job_metering_writeback.py::TestReportJobMeteringCallback::test_pre_tts_rewrite_events_are_reported tests/test_quality_benchmark_tools.py`

部署：

- 已部署到美国主机。
- 远端备份：`/opt/aivideotrans/deploy_backups/p1f_short_margin_20260425T232951Z`
- 远端验证：
  - `docker exec aivideotrans-app python -m py_compile /opt/aivideotrans/app/src/pipeline/process.py` 通过。
  - `docker exec aivideotrans-app python scripts/linux_remote_workbench_preflight.py app-health` 通过。
  - Gateway `http://127.0.0.1:8880/gateway/health` 返回 200。
  - 远端 markers 已确认：
    - `PRE_TTS_REWRITE_SHORT_OVERSHOOT_RATIO = 0.30`
    - `PRE_TTS_REWRITE_NEAR_SHORT_OVERSHOOT_RATIO = 0.30`
    - `PRE_TTS_REWRITE_SHORT_DECISION_ESTIMATE_MARGIN = 1.15`

下一轮生产验证重点：

- `2~8s` 与 `8~12s` 段的 `needs_review` 是否继续下降。
- `harmful_pre_tts_contradiction_count` 是否保持低位，不能因为 margin 放大而引入新的系统性反向改写。
- `pre_tts rejected` 是否仍是低频个案，不能大规模反弹。
- `<2s` 微插话若仍高频 `needs_review`，下一步应转向“微段合并 / review 降噪”，而不是放开 LLM rewrite。

## 2026-04-26 P1-f 生产验证：4 条新视频

验证范围：P1-f 部署后完成的最近 4 个 succeeded job，其中最后一条为同源视频重跑。

| job | 内容 | provider/model | 段数 | pre-TTS | contradiction | harmful | needs_review | force_dsp | 2~12s needs 未进 pre-TTS |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `job_d3ca1fb8bf0b42efb906be716ac6995c` | 查理·芒格最后访谈：最好的人生建议 | MiniMax `speech-2.8-hd` | 53 | 24 | 7 | 2 | 8 | 24 | 0 |
| `job_74860805483b4596877b8706195a5e30` | Anthropic 新 AI 威胁银行业，美联储如何应对 | MiniMax `speech-2.8-turbo` | 26 | 10 | 7 | 1 | 0 | 0 | 0 |
| `job_a621ae11e0d14f19be28aa568477c69c` | Anthropic 新模型 Mythos 引发网络安全担忧 | MiniMax `speech-2.8-turbo` | 44 | 29 | 20 | 2 | 1 | 1 | 0 |
| `job_187fcd0a44164734898466a4bcc26ea9` | 盘点十大背弃特朗普的选民群体 | MiniMax `speech-2.8-turbo` | 18 | 15 | 3 | 0 | 0 | 0 | 0 |

汇总：

- 4 条共 141 段，`needs_review=9`，整体约 6.4%。
- 3 条视频 `needs_review` 为 0 或 1；主要残留集中在芒格访谈。
- 关键验证项成立：4 条视频里 `2~12s` 短/近短段 `needs_review` 但未进入 pre-TTS 的数量为 0。
- 未看到 `outside guardrails` / `pre_tts rejected` 大规模反弹。
- speaker diff：
  - 芒格访谈有 3 条 speaker correction，集中在连续问答段。
  - Mythos 视频有 3 条 speaker correction，属于多 speaker 新闻剪辑场景。
  - 另外两条 speaker diff 为空。

同源视频重跑对比（YouTube `mM5CYeWyjaI`）：

| 指标 | P1 优化前旧 job `job_2f3f666...` | P1-f 后新 job `job_d3ca1...` |
| --- | ---: | ---: |
| 总段数 | 53 | 53 |
| `needs_review` | 28 | 8 |
| `force_dsp` | 28 | 24 |
| `rewrite_count` | 18 | 37 |
| `pre_tts_rewrite_count` | 12 | 24 |
| `pre_tts_contradiction_count` | 6 | 7 |
| `harmful_pre_tts_contradiction_count` | 0 / 未结构化 | 2 |
| `2~5s` needs_review | 6 / 7 | 3 / 7 |
| `2~5s` pre-TTS | 0 / 7 | 7 / 7 |
| `5~8s` needs_review | 4 / 5 | 1 / 5 |
| `5~8s` pre-TTS | 0 / 5 | 5 / 5 |
| `<1s` needs_review | 13 / 13 | 0 / 13 |
| `1~2s` needs_review | 5 / 6 | 3 / 6 |

判断：

- P1-e/P1-f 对短段问题有效：原来 `2~8s` 段几乎完全错过 pre-TTS，现在同源重跑中 `2~8s` 全部进入 pre-TTS，needs_review 从 `10/12` 降到 `4/12`。
- 同源视频总 `needs_review` 从 `28/53` 降到 `8/53`，说明这几轮修补确实改善了人工介入压力。
- 成本侧代价明确：同源视频 `pre_tts_rewrite_count` 从 12 增到 24，`rewrite_count` 从 18 增到 37。当前是在用更多 LLM rewrite 换更少的人工 review / force-DSP 风险。
- 新风险是 `harmful_pre_tts_contradiction` 仍存在，且主要不在 `2~8s` 短段，而在 `8~20s` 近短/中段 overshoot shrink 后真实 TTS 偏短：
  - 芒格 job：segment 1、32。
  - Anthropic 银行业 job：segment 3。
  - Mythos job：segment 35、40，其中 segment 40 最终仍 `needs_review`。

下一步建议：

- 不再继续降低短段触发阈值；P1-f 的目标已经达到，继续调阈值会把 rewrite 成本和 harmful contradiction 推高。
- 下一步转向 P1-g：针对 `8~20s` overshoot shrink 增加“反向过短保护”：
  - 对 `8~20s` 段设置更保守的 shrink floor / 最大缩短比例。
  - 当 pre-TTS shrink 后 first-pass 明显 undershoot 时，优先走结构化二次修正或回退策略，而不是继续让 G3 从错误方向上重写。
  - 保持 `2~8s` 短段当前策略不变，避免回退已验证收益。

## 2026-04-26 P1-g 执行记录：8~20s 反向过短保护

本轮目标不是继续扩大短段 rewrite，而是收口 P1-f 后暴露的新风险：`8~20s` 近短/中段在 overshoot shrink 后，真实 TTS first pass 有时反向偏短，形成 `harmful_pre_tts_contradiction`。

实现：

- 只作用于 `8_000ms <= target_duration_ms < 20_000ms` 的 overshoot 段。
- 仅当按 CPS 估算需要缩短比例 `required_shrink >= 20%` 时启用，避免影响轻微 overshoot。
- 不覆盖 P1-d 的短高缩短保护；`target <= 12s` 且 `required_shrink >= 45%` 的旧逻辑继续保持原来的 `(lower, upper)` 行为。
- 对 P1-g 风险段：
  - 最大缩短比例收紧到 `25%`。
  - 改写后最低字数提高到 `target_chars * 1.10`。
  - 改写后最高字数提高到 `target_chars * 1.18`。
- `20s+` 长段不受影响，已有 20s guardrail 测试保持 `90~101` 的旧窗口。
- `2~8s` 短段 P1-f 策略不变，继续保留 estimator margin 与 30% 触发阈值。

这相当于承认当前 provider 在 8~20s shrink 后存在“真实语速快于 CPS 估算”的系统性风险，因此 pre-TTS 阶段宁可少压一点，把剩余误差交给 speed / DSP，而不是把文本压到理论 target_chars 附近后触发 post-TTS 反向 rewrite。

本地验证：

- `python -m py_compile src\pipeline\process.py`
- `pytest -q tests/test_process_pipeline.py::test_process_pipeline_passes_pre_tts_guardrail_bounds_to_profile_rewriter tests/test_process_pipeline.py::test_process_pipeline_tightens_short_high_shrink_pre_tts_bounds tests/test_process_pipeline.py::test_process_pipeline_rejects_short_high_shrink_rewrite_below_risk_floor tests/test_process_pipeline.py::test_process_pipeline_tightens_mid_undershoot_risk_pre_tts_bounds tests/test_process_pipeline.py::test_process_pipeline_rejects_mid_undershoot_risk_rewrite_below_floor tests/test_process_pipeline.py::test_process_pipeline_short_estimate_margin_catches_borderline_overshoot tests/test_process_pipeline.py::test_process_pipeline_near_short_estimate_margin_catches_borderline_overshoot tests/test_process_pipeline.py::test_process_pipeline_skips_micro_segment_pre_tts_rewrite`
- `pytest -q tests/test_process_pipeline.py -k "pre_tts or Pre_TTS or guardrail"`

部署：

- 已部署到美国主机。
- 远端备份：`/opt/aivideotrans/deploy_backups/p1g_mid_undershoot_20260426T022708Z`
- 远端验证：
  - `docker exec aivideotrans-app python -m py_compile /opt/aivideotrans/app/src/pipeline/process.py` 通过。
  - `docker exec aivideotrans-app python scripts/linux_remote_workbench_preflight.py app-health` 通过。
  - Gateway `http://127.0.0.1:8880/gateway/health` 返回 200。
  - 远端 markers 已确认：
    - `PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_MIN_TARGET_MS = 8_000`
    - `PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_MAX_CHANGE_RATIO = 0.25`
    - `PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_MIN_TARGET_MULTIPLIER = 1.10`
    - `PRE_TTS_REWRITE_SHORT_OVERSHOOT_RATIO = 0.30`

下一轮生产验证重点：

- `harmful_pre_tts_contradiction_count` 是否下降，尤其是 `8~20s` 桶。
- `needs_review_count` 是否保持 P1-f 后的低位，不能因为少压文本导致 force-DSP 反弹。
- `pre_tts_rewrite_count` 不应继续明显上升；本轮只是收紧输出窗口，不扩大触发面。
- 若 `8~20s` harmful 仍高，下一步再考虑 post-TTS first-pass undershoot 的结构化回退，而不是继续调 pre-TTS 触发阈值。

## 2026-04-26 P1-g 生产验证：芒格重跑与 2 条新视频

验证范围：P1-g 部署后完成的 3 个 succeeded job。

| job | 内容 | provider/model | 段数 | rewrite_count | pre-TTS | contradiction | harmful | needs_review | force_dsp | 2~12s needs 未进 pre-TTS |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `job_abb3fedc905c403db67d784c480c170e` | 查理·芒格的终极人生建议 | MiniMax `speech-2.8-hd` | 53 | 38 | 24 | 5 | 1 | 5 | 22 | 0 |
| `job_877146b2c9bc4e67816c2cc2e472413f` | 黄仁勋：AI 将彻底改变计算方式 | MiniMax `speech-2.8-hd` | 16 | 13 | 0 | 0 | 0 | 2 | 2 | 0 |
| `job_67009391e2d1425eb52728aca4995130` | 比亚迪为何让欧洲车企感到恐惧 | MiniMax `speech-2.8-turbo` | 65 | 31 | 25 | 8 | 0 | 5 | 13 | 0 |

同源芒格视频三轮对比：

| 指标 | 优化前旧 job `job_2f3f666...` | P1-f job `job_d3ca1...` | P1-g job `job_abb3...` |
| --- | ---: | ---: | ---: |
| 总段数 | 53 | 53 | 53 |
| `needs_review` | 28 | 8 | 5 |
| `force_dsp` | 28 | 24 | 22 |
| `rewrite_count` | 18 | 37 | 38 |
| `pre_tts_rewrite_count` | 12 | 24 | 24 |
| `pre_tts_contradiction_count` | 6 | 7 | 5 |
| `harmful_pre_tts_contradiction_count` | 0 / 未结构化 | 2 | 1 |
| `2~5s` needs_review | 6 / 7 | 3 / 7 | 3 / 6 |
| `5~8s` needs_review | 4 / 5 | 1 / 5 | 0 / 5 |
| `8~12s` harmful | 0 / 8 | 1 / 8 | 0 / 8 |
| `12~20s` harmful | 0 / 5 | 1 / 5 | 0 / 5 |

判断：

- P1-g 的直接目标成立：芒格同源重跑里，P1-f 暴露的 `8~20s` harmful 从 2 个降到 0。
- 芒格总 `needs_review` 继续下降：`28 -> 8 -> 5`。
- 芒格 `pre_tts_rewrite_count` 没有继续上升，仍为 24；说明 P1-g 只是收紧输出窗口，没有扩大触发面。
- 芒格剩余 1 个 harmful 出现在 `20s+` 段，不在 P1-g 保护范围内；这提示下一步如果继续做，应处理长段 shrink 后 undershoot，而不是再调短段。
- 比亚迪新 job 的结果健康：65 段中 `harmful=0`，`needs_review=5`，且 `2~12s` 漏处理为 0。
- 黄仁勋 job 的 2 个 `needs_review` 都在 `20s+` 长段，且没有 pre-TTS 事件；这是长段后处理/重写策略问题，不是 P1-f/P1-g 短段策略问题。

当前结论：

- P1-e/P1-f/P1-g 对“短段漏处理”和“8~20s shrink 后反向过短”均有阶段性效果。
- 继续沿同一方向调短段阈值的边际收益已经变低，且会增加 rewrite 成本。
- 下一步应进入 P1-h 或 P1 收口评估：
  - 若继续优化长度链路，优先处理 `20s+` 长段 overshoot shrink 后 undershoot / force-DSP，而不是再放宽短段触发。
  - 若 `20s+` 问题不高频，则可以把 P1 判定为阶段性完成，进入 P2 speaker/overlap 标记。

## 2026-04-26 P1-h 执行记录：20s+ 长段 shrink 保护

本轮按 P1-g 生产验证暴露的剩余问题推进：`8~20s` harmful 已下降，但芒格重跑仍有 1 个 `20s+` 长段在 overshoot shrink 后 first pass 反向偏短；黄仁勋任务的 `needs_review` 也集中在 `20s+` 长段。

实现：

- 只作用于 `target_duration_ms > 20_000ms` 的 overshoot 段。
- 仅当按 CPS 估算需要缩短比例 `required_shrink >= 20%` 时启用。
- 不扩大 pre-TTS rewrite 触发面，只收紧触发后的字数窗口。
- 对 P1-h 风险段：
  - 最大缩短比例收紧到 `20%`。
  - 改写后最低字数提高到 `target_chars * 1.15`。
  - 改写后最高字数提高到 `target_chars * 1.28`。
- 保持 20.0s 边界段旧行为不变；已有 `target_duration_ms=20_000` guardrail 测试仍保持 `90~101` 窗口。
- `2~8s`、`8~12s`、`8~20s` 已验证策略不变。

典型样本映射：

- 芒格 P1-g job 的 segment 18：
  - `target_ms=28183`
  - `estimate_ms=37648`
  - `pre_chars=127`
  - P1-g 接受 `post_chars=103`，first pass 偏短约 `-16.7%`
  - P1-h 新窗口为 `110~122`，会拒绝过度缩短到 103 这类结果

本地验证：

- `python -m py_compile src\pipeline\process.py`
- `pytest -q tests/test_process_pipeline.py::test_process_pipeline_passes_pre_tts_guardrail_bounds_to_profile_rewriter tests/test_process_pipeline.py::test_process_pipeline_tightens_short_high_shrink_pre_tts_bounds tests/test_process_pipeline.py::test_process_pipeline_rejects_short_high_shrink_rewrite_below_risk_floor tests/test_process_pipeline.py::test_process_pipeline_tightens_mid_undershoot_risk_pre_tts_bounds tests/test_process_pipeline.py::test_process_pipeline_rejects_mid_undershoot_risk_rewrite_below_floor tests/test_process_pipeline.py::test_process_pipeline_tightens_long_undershoot_risk_pre_tts_bounds tests/test_process_pipeline.py::test_process_pipeline_rejects_long_undershoot_risk_rewrite_below_floor tests/test_process_pipeline.py::test_process_pipeline_short_estimate_margin_catches_borderline_overshoot tests/test_process_pipeline.py::test_process_pipeline_near_short_estimate_margin_catches_borderline_overshoot tests/test_process_pipeline.py::test_process_pipeline_skips_micro_segment_pre_tts_rewrite`
- `pytest -q tests/test_process_pipeline.py -k "pre_tts or Pre_TTS or guardrail"`

部署：

- 已部署到美国主机。
- 远端备份：`/opt/aivideotrans/deploy_backups/p1h_long_undershoot_20260426T044531Z`
- 远端验证：
  - `docker exec aivideotrans-app python -m py_compile /opt/aivideotrans/app/src/pipeline/process.py` 通过。
  - `docker exec aivideotrans-app python scripts/linux_remote_workbench_preflight.py app-health` 通过。
  - Gateway `http://127.0.0.1:8880/gateway/health` 返回 200。
  - 远端 markers 已确认：
    - `PRE_TTS_REWRITE_LONG_UNDERSHOOT_RISK_MIN_TARGET_MS = 20_000`
    - `PRE_TTS_REWRITE_LONG_UNDERSHOOT_RISK_MAX_CHANGE_RATIO = 0.20`
    - `PRE_TTS_REWRITE_LONG_UNDERSHOOT_RISK_MIN_TARGET_MULTIPLIER = 1.15`
    - `PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_MAX_CHANGE_RATIO = 0.25`

下一轮生产验证重点：

- `20s+` harmful 是否下降。
- `20s+` `needs_review` / `force_dsp` 是否不反弹。
- `pre_tts_rewrite_count` 是否保持稳定；P1-h 不应让 rewrite 触发数增加。
- 如果长段仍反复进入 `force_dsp`，下一步不再继续收紧 pre-TTS，而应评估 G3 post-TTS first-pass 的结构化回退策略。

## 2026-04-26 P1-h 生产验证：2 条同源重跑

验证范围：P1-h 部署后完成的 2 个同源重跑 job。

| job | 内容 | provider/model | 段数 | rewrite_count | pre-TTS | contradiction | harmful | needs_review | force_dsp | 2~12s needs 未进 pre-TTS |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `job_925ad5833c764d2ab386281b58a6c18f` | 比亚迪崛起：为何让欧洲车企感到恐惧 | MiniMax `speech-2.8-hd` | 63 | 31 | 25 | 5 | 0 | 2 | 11 | 1 |
| `job_ce814b8cdc2242d7b72764b2f0b72dd4` | 查理·芒格最后访谈：关于投资与人生的建议 | MiniMax `speech-2.8-hd` | 53 | 39 | 26 | 4 | 0 | 7 | 24 | 0 |

比亚迪同源对比：

| 指标 | P1-g `job_670093...` | P1-h `job_925ad...` |
| --- | ---: | ---: |
| 总段数 | 65 | 63 |
| `needs_review` | 5 | 2 |
| `force_dsp` | 13 | 11 |
| `pre_tts_rewrite_count` | 25 | 25 |
| `pre_tts_contradiction_count` | 8 | 5 |
| `harmful_pre_tts_contradiction_count` | 0 | 0 |
| `20s+` harmful | 0 | 0 |
| `20s+` needs_review | 0 | 0 |
| `20s+` force_dsp | 0 | 0 |

芒格同源多轮对比：

| 指标 | 优化前 `job_2f3f666...` | P1-f `job_d3ca1...` | P1-g `job_abb3...` | P1-h `job_ce814...` |
| --- | ---: | ---: | ---: | ---: |
| 总段数 | 53 | 53 | 53 | 53 |
| `needs_review` | 28 | 8 | 5 | 7 |
| `force_dsp` | 28 | 24 | 22 | 24 |
| `rewrite_count` | 18 | 37 | 38 | 39 |
| `pre_tts_rewrite_count` | 12 | 24 | 24 | 26 |
| `pre_tts_contradiction_count` | 6 | 7 | 5 | 4 |
| `harmful_pre_tts_contradiction_count` | 0 / 未结构化 | 2 | 1 | 0 |
| `20s+` harmful | 0 | 0 | 1 | 0 |
| `20s+` needs_review | 0 | 1 | 0 | 0 |
| `20s+` force_dsp | 0 | 1 | 0 | 0 |

判断：

- P1-h 直接目标成立：两个重跑 job 的 `20s+ harmful` 都为 0，芒格 P1-g 中的长段 harmful 被消除。
- 比亚迪整体继续改善：`needs_review 5 -> 2`，`force_dsp 13 -> 11`，`contradiction 8 -> 5`，且 pre-TTS 数保持 25。
- 芒格的 harmful 清零，但 `needs_review 5 -> 7`、`force_dsp 22 -> 24`，残留主要集中在 `1~8s` 短段 force-DSP：
  - `1~2s`：2 个 needs_review。
  - `2~5s`：4 个 needs_review。
  - `5~8s`：1 个 needs_review。
  - `20s+`：0 个 needs_review / 0 个 force-DSP。
- 比亚迪出现 1 个 `2~12s needs_review` 未进 pre-TTS 的残留段，target 约 2.6s，first pass 偏长约 26%；这是短段阈值/估算边界问题，不是 P1-h 长段保护问题。

当前阶段结论：

- P1-h 可以判定为有效，不建议回滚。
- P1 长度链路的主要质量风险已经从 harmful contradiction 转移到“短段/微段 force-DSP 残留”。
- 不建议继续用更激进的 pre-TTS rewrite 阈值处理短段；下一步应评估 deterministic 的短段处理策略，例如：
  - `<2s` / `2~5s` 短句合并到相邻 SemanticBlock。
  - 短段 first-pass 后的 review 降噪规则。
  - 对短段 force-DSP 做更精细的 severity 分级，而不是一律提高人工 review 权重。

## 2026-04-26 P1-i 执行记录：deterministic 短段策略

本轮针对 P1-h 后残留的 `1~8s` 短段 force-DSP 问题，不继续扩大 pre-TTS rewrite 阈值，避免用更多 LLM 改写成本换不稳定收益。

实现范围：

- `force_dsp` 增加结构化 severity：`low` / `medium` / `high`。
- 仅对非常短、低信息量的 backchannel 自动 review 降噪：
  - `target_duration_ms <= 2_000`
  - spoken chars `<= 18`
  - first-pass duration `<= 4_500ms`
  - first-pass / target ratio `<= 4.0`
- `2~5s` 且文本较短的 `force_dsp` 标记为 `medium`，仍保留 `needs_review=True`，先观察数据，不直接静默。
- 增加同说话人短段合并候选标注：
  - 只标注 `target_duration_ms <= 2_000` 且 spoken chars `<= 18` 的短段。
  - 只允许相邻同说话人、gap `<=650ms`、合并后目标时长 `<=10s` 的候选。
  - 不同说话人相邻时明确标记 `short_merge_blocked_reason=cross_speaker_adjacent`，不合并，避免把短插话吞进相邻长段。
  - 当前只写审计元数据，不改变 TTS 分块和音频拼接；实际合并需等待生产样本验证后再单独推进。
- Gateway metering 新增字段：
  - `force_dsp_severity_distribution`
  - `force_dsp_review_suppressed_count`
  - `short_merge_candidate_count`
  - `short_merge_blocked_cross_speaker_count`

本地验证：

- `python -m py_compile src\services\gemini\translator.py src\services\alignment\aligner.py src\pipeline\process.py gateway\job_intercept.py`
- `pytest -q tests\test_aligner.py::test_aligner_short_force_dsp_backchannel_does_not_require_review tests\test_aligner.py::test_aligner_short_force_dsp_long_text_still_requires_review tests\test_aligner.py::test_aligner_two_second_force_dsp_backchannel_is_review_denoised tests\test_aligner.py::test_aligner_two_to_five_second_force_dsp_keeps_review_with_medium_severity tests\test_process_pipeline.py::test_process_pipeline_marks_only_same_speaker_short_merge_candidates tests\test_job_metering_writeback.py::TestUpdateJobMetering::test_merges_fields_into_snapshot`

已知限制：

- 本机完整 `tests/test_aligner.py` 依赖 `ffmpeg/ffprobe`，当前 Windows 环境 PATH 缺失，完整套件会因环境失败；相关新增/受影响用例已单独通过。

下一轮生产验证重点：

- `<2s` `force_dsp` 的 `needs_review` 是否下降，且 `force_dsp_review_suppressed_count` 不应覆盖内容完整句。
- `2~5s` `medium` 桶是否集中承载剩余短段 review；如果确认为低风险，再考虑进一步降噪。
- `short_merge_candidate_count` 与 `short_merge_blocked_cross_speaker_count` 的比例，用于判断是否值得进入“真实合并同说话人短段”的下一步。

## 2026-04-26 P2-a 执行记录：单人演讲观众互动防碎片化

背景：

- 生产样本 `job_ca7c4396fe104d4c8ad23585af03c368` 是以单人演讲为主、夹杂台下观众短回答的课堂视频。
- 原始 ASR `original` 快照中 122 行全部为 `speaker_a`，但 S2 Pass 1 将短互动拆成 `speaker_b/c/d/e/g/h` 等多个一次性未知说话人，音色选择阶段显示 7 个 speaker。
- 用户听音频后确认：这些不是 7 个正式嘉宾，而是台下观众简短、低音量互动；其中部分长段是主讲人内容里夹杂一句观众回应，被 Pass 1 整段改成了观众。

实现范围：

- 在 S2 Pass 1 correction 之后、Pass 2 之前新增 `_apply_single_speaker_audience_fragmentation_guard`。
- 触发条件收窄为：
  - 原始 ASR 只有 1 个 speaker。
  - Pass 1 额外产生的 speaker 都是低支撑碎片：每个 extra speaker 行数 `<=2`，总时长 `<=20s`。
  - 所有 extra speaker 总时长占整段比例 `<=15%`。
  - extra speaker 的 profile 呈现 audience / unknown / 观众 / 未知 等特征；如果 profile 不像观众/未知，则必须同时满足 3 个以上 extra speaker 且总占比 `<=5%`，才按碎片化处理。
- 处理策略：
  - 短互动片段：`duration <=8s` 且 rough word count `<=24`，统一归到 `speaker_audience`，profile 名称为“现场观众”。
  - 超过短互动阈值的片段：回退到原始主讲 speaker，避免“长段主讲内容被一句台下回答带偏”。
  - 真实多人 ASR 不触发；原始 ASR 已经有 2+ speaker 时保持原结果。
- Pass 1 prompt 增加约束：
  - 单人演讲中，不要为每次短台下回应创建独立未知说话人。
  - 不要因为长段里夹杂一句台下回应，就把整段主讲内容改成观众。
- `s2_pass1_result.json` 新增 `audience_fragmentation_guard_applied`，现有 `after_corrections_to_after_sanity` diff 可看到 guard 的具体改动。

本地验证：

- `python -m py_compile src\services\transcript_reviewer.py`
- `pytest -q tests\test_transcript_reviewer.py::test_single_speaker_audience_guard_collapses_short_one_off_speakers tests\test_transcript_reviewer.py::test_single_speaker_audience_guard_ignores_real_multi_speaker_asr tests\test_transcript_reviewer.py::test_single_speaker_audience_guard_keeps_substantial_detected_speakers`
- `pytest -q tests\test_transcript_reviewer.py -k "audience or sanity or correction or pass1_prompt or pass1_drops"`

部署：

- 已部署到美国主机，仅替换 `src/services/transcript_reviewer.py`。
- 远端备份：`/opt/aivideotrans/deploy_backups/p2a_audience_fragmentation_20260426T105529Z`
- 远端验证：
  - `docker exec aivideotrans-app python -m py_compile /opt/aivideotrans/app/src/services/transcript_reviewer.py` 通过。
  - app 容器重启后为 `healthy`。
  - `docker exec aivideotrans-app python scripts/linux_remote_workbench_preflight.py app-health` 通过。
  - 远端 marker 已确认：
    - `_apply_single_speaker_audience_fragmentation_guard`
    - `audience_fragmentation_guard_applied`

下一轮生产验证重点：

- 单人演讲 + 台下短互动视频，音色选择阶段 speaker 数应从多个一次性未知 speaker 收敛到“主讲人 + 现场观众”。
- `audience_fragmentation_guard_applied` 应只在原始 ASR 单 speaker 且 Pass 1 碎片化明显时非 0。
- 真实访谈、多人播客、panel discussion 不应触发该 guard。
- 如果仍存在“同一个观众被多次短句识别但 profile 不含 audience/unknown”的样本，再考虑 P2-b 的局部 verifier；不建议回到全量多模型并跑 speaker 识别。

## 2026-04-26 P2-a2 执行记录：单观众 speaker 长段回退，撤销文本 cue

生产复测：

- 同源重跑 `job_b5cbf83e075d48628a24678d5c455205` 的音色选择已从 7 个 speaker 收敛到 2 个 speaker：
  - `speaker_a`：主讲人，120 段，约 2443.9s。
  - `speaker_b`：观众，4 段，约 34.7s。
- 但该 job 的 `audience_fragmentation_guard_applied=0`，原因是 Pass 1 已直接输出“主讲人 + 观众”两个 speaker，而不是多个碎片化 unknown speaker。
- 进一步解析发现 4 个观众段中有 18.4s 与 11.2s 长段，文本包含：
  - `for those of you who couldn't hear`
  - `what he said`
  - `Do you see how easy that was`
  这类文本是主讲人对台下回答的转述/点评，不应整段配观众音色。
- 但按这些具体文本 cue 写规则属于局部补丁，容易过拟合并增加误判风险。因此 P2-a2 最终不保留 phrase cue。

最终修正：

- 保留 P2-a 的总前提：仅在原始 ASR 只有 1 个 speaker 且 extra speaker 总占比 `<=15%` 时介入。
- 如果 extra speaker profile 明确像 audience / listener / unknown / 观众 / 听众 / 未知，则不再要求其行数 `<=2`、总时长 `<=20s`；这覆盖“已经收敛成单个观众 speaker，但仍含长混合段”的情形。
- 对 audience-like extra speaker 的每一行逐段判断：
  - `duration <=8s` 且 rough word count `<=24`，保留为 `speaker_audience`。
  - 否则回退为原始主讲 speaker。
- 规则只使用结构信号：原始 ASR speaker 数、extra speaker profile、extra speaker 时长占比、单段时长、粗略词数；不使用具体文本短语判断说话人。

本地验证：

- `python -m py_compile src\services\transcript_reviewer.py`
- `pytest -q tests\test_transcript_reviewer.py::test_single_speaker_audience_guard_collapses_short_one_off_speakers tests\test_transcript_reviewer.py::test_single_speaker_audience_guard_ignores_real_multi_speaker_asr tests\test_transcript_reviewer.py::test_single_speaker_audience_guard_keeps_substantial_detected_speakers tests\test_transcript_reviewer.py::test_single_speaker_audience_guard_keeps_named_short_guest_fragments tests\test_transcript_reviewer.py::test_single_speaker_audience_guard_reverts_long_audience_like_segments`
- `pytest -q tests\test_transcript_reviewer.py -k "audience or sanity or correction or pass1_prompt or pass1_drops"`

部署：

- 已部署到美国主机，仅替换 `src/services/transcript_reviewer.py`。
- 远端备份：`/opt/aivideotrans/deploy_backups/p2a_audience_fragmentation_20260426T132056Z`
- 远端验证：
  - app 容器重启后为 `healthy`。
  - `docker exec aivideotrans-app python scripts/linux_remote_workbench_preflight.py app-health` 通过。
  - 远端 marker 已确认：`audience_fragmentation_guard_applied`
  - 远端已确认不再包含 `_looks_like_lecturer_paraphrase_or_response` phrase-cue 逻辑。

后续 P2-b 方向：

- 不继续堆文本 cue。
- 对仍不确定的单 speaker 演讲 + 观众互动场景，增加局部音频 verifier：
  - 只截取候选段附近音频；当前复用 S2 既有 clip 提取链路，padding 为 ±10s。
  - 输入原始 ASR speaker、Pass 1 speaker、候选段时长和上下文，不输入翻译文本。
  - 输出 `main_speaker` / `audience` / `uncertain`。
  - `uncertain` 默认回退主讲人或进入人工 review，取决于套餐和任务价值。
- 同时记录 correction provenance：区分 whole-line `correct_speaker` 和 `split speaker_after`，避免把长原始段整段改成观众。

## 2026-04-26 P2-b 执行记录：低支撑 speaker 局部音频 verifier

设计判断：

- 不把 P2-a 的确定性结构规则直接放宽到所有多 speaker 视频。原因：
  - 多人访谈、panel、播客中，短 speaker 不一定是错误，也可能是真实短插话或嘉宾。
  - 对所有多 speaker 视频做确定性回退，会把“音频证据”重新降级成“结构猜测”，误伤风险高。
- P2-b 改为“低支撑 speaker correction verifier”：候选发现可覆盖多 speaker 视频，但只有局部音频 verifier 明确判定 `main_speaker` 时才改；`assigned_speaker` 和 `uncertain` 都保持 Pass 1 结果。

触发条件：

- 候选 speaker 不是全片 dominant speaker。
- 候选 speaker 行数 `<=3`，单段 `<=30s`，总时长 `<=30s`。
- 候选 speaker profile 为 audience / listener / unknown / 观众 / 听众 / 未知，或总时长占比 `<=5%`。
- 单个候选段时长 `<=20s`。
- 候选段能找到 main speaker 参照：
  - 原始 ASR 同 index 行的 speaker 与当前不同。
  - 或前后相邻的非低支撑 speaker 相同。
  - 或全片 dominant speaker 可作为参照。
- 每个 job 最多校验 6 个候选段，控制成本。

处理策略：

- 使用已有 S2 review 音频链路提取候选段局部音频 clip，复用 Pass 1 的音频模型。
- verifier 输出：
  - `main_speaker`：候选段声音属于原始/周边/main speaker，且 confidence 非 low，则回退到 main speaker。
  - `assigned_speaker`：保持当前低支撑 speaker。
  - `uncertain`：保持当前结果，不做自动回退。
- 结果写入 `s2_pass1_result.json`：
  - `speaker_verifier_applied`
  - `speaker_verifier.candidates`
  - `speaker_verifier.decisions`
  - `speaker_verifier.skipped_reason`
- 同时单独写 `s2_speaker_verifier_result.json`，保存候选、模型输出和解析结果，便于后续复盘。

本地验证：

- `python -m py_compile src\services\transcript_reviewer.py`
- `pytest -q tests\test_transcript_reviewer.py::test_low_support_speaker_verifier_collects_multi_speaker_candidate tests\test_transcript_reviewer.py::test_low_support_speaker_verifier_applies_main_speaker_decision tests\test_transcript_reviewer.py::test_low_support_speaker_verifier_keeps_uncertain_decision`
- `pytest -q tests\test_transcript_reviewer.py -k "audience or verifier or sanity or correction or pass1_prompt or pass1_drops"`

部署：

- 已部署到美国主机，仅替换 `src/services/transcript_reviewer.py`。
- 远端备份：`/opt/aivideotrans/deploy_backups/p2a_audience_fragmentation_20260426T155321Z`
- 远端验证：
  - app 容器重启后为 `healthy`。
  - `docker exec aivideotrans-app python scripts/linux_remote_workbench_preflight.py app-health` 通过。
  - 远端 marker 已确认：
    - `_apply_low_support_speaker_verifier`
    - `speaker_verifier_applied`

下一轮生产验证重点：

- `speaker_verifier.candidates` 数量是否保持低位，避免额外成本扩大。
- `speaker_verifier.decisions` 中 `main_speaker / assigned_speaker / uncertain` 的分布。
- 多 speaker 视频中是否出现低支撑 speaker 被错误回退；若有，优先收紧 verifier 触发条件，而不是扩大 P2-a 确定性规则。
- 单人演讲 + 观众互动场景中，`speaker_audience` 是否继续只保留真实短互动。

下一轮生产验证重点：

- 同源重跑后，观众 speaker 的总时长应明显低于 34.7s。
- 18.4s 与 11.2s 的主讲人转述段应回退到主讲人。
- 如果音色选择仍显示 2 个 speaker，这是合理的；目标不是删除真实观众互动，而是避免长混合段被整段归为观众。

## 2026-04-27 P1-j 执行记录：同说话人短段真实合并

背景：

- P1-i 已经证明短段候选可以用结构信号稳定标注，但当时只写审计字段，没有改变 TTS 分块。
- 最近几轮生产样本显示，P1 的主要残留已经不是 pre-TTS harmful contradiction，而是 `<2s` / `2~5s` 短段带来的 `force_dsp` 和 `needs_review` 噪声。
- 继续降低短段 pre-TTS rewrite 阈值会增加 LLM rewrite 成本，并可能重新引入过度缩写；因此本轮改为 deterministic 结构处理。

实现范围：

- 在 TTS 生成前执行真实合并，合并后的 `DubbingSegment` 仍是 TTS unit，保持 “TTS unit is SemanticBlock” 的架构边界。
- 合并条件只使用通用结构信号，不使用视频标题、人物、固定台词或内容关键词：
  - 被吸收段 `target_duration_ms <= 2_000`。
  - 被吸收段 spoken chars `<=18`。
  - 只允许相邻同说话人。
  - 相邻 gap `<=650ms`。
  - 合并后时间跨度 `<=10s`。
- 不同说话人短段仍明确阻断，不合并：
  - 保留 `short_merge_blocked_reason=cross_speaker_adjacent`。
  - 避免把台下观众、嘉宾短插话吞进主讲人长段。
- 合并审计字段：
  - `short_merge_applied`
  - `short_merge_absorbed_segment_ids`
  - `short_merge_applied_count`
  - `short_merge_absorbed_count`
- 合并后的 block 会把 `original_srt_indices` 扩展为原始段 id 集合，便于后续追溯。
- 对合并后的 segment 清理旧 TTS cache，避免因保留原 `segment_id` 而误用合并前的旧音频。

设计取舍：

- 不做跨 speaker 合并，即使另一个 speaker 很短，也只记录阻断原因。
- 不按文本内容判断是否“主讲人转述”或“观众回答”，避免回到 P2-a2 中已否定的 phrase-cue 过拟合路径。
- 不引入新的 LLM 调用；这是 P1 收口阶段的低成本确定性优化。

本地验证：

- `python -m py_compile src\pipeline\process.py src\services\gemini\translator.py gateway\job_intercept.py`
- `pytest -q tests\test_process_pipeline.py -k "same_speaker_short_segment or cross_speaker_short_segment or absorbed_ids"`
- `pytest -q tests\test_job_metering_writeback.py`

下一轮生产验证重点：

- `short_merge_applied_count` / `short_merge_absorbed_count` 是否非零，且只出现在同 speaker 相邻短段。
- `<2s` 和 `2~5s` 的 `force_dsp` / `needs_review` 是否下降。
- `short_merge_blocked_cross_speaker_count` 是否继续保护真实短插话，不应为了降低 review 而跨 speaker 合并。
- 若短段 review 仍高，下一步应做短段 severity 的 UI/报表分级，而不是继续扩大合并条件。

## 2026-04-27 P2 收敛基线：speaker attribution report

背景：

- 最近几轮生产验证已经证明，针对单个视频追加 phrase-cue 或人物专属规则不是最优路径。
- 同类失败并不只来自“单 speaker 演讲 + 观众互动”，也可能来自主持人、嘉宾、背景音乐、采访短问答、低占比真实 speaker、ASR/S2 旧产物缺少结构 metadata 等混合场景。
- 因此 P2 需要先固定可复跑的 speaker attribution 报告，再基于重复视频组和高风险 job 调 verifier 阈值。

新增工具：

- `scripts/benchmark/speaker_attribution_report.py`
  - 扫描项目中已有 `translation/segments.json`。
  - 输出 job 级 speaker profile、primary share、role distribution、force-DSP、needs-review、rewrite、pre-TTS rewrite、short-merge、verifier summary。
  - 自动识别同源重复视频组，并给出 `best_by_cost` 与 `best_by_speaker`。
  - 对旧 job 若缺少 `speaker_role` / `speaker_duration_share`，回退到 segment start/end 计算 speaker duration share，避免把旧样本误判为 primary=0。

报告产物：

- `reports/benchmark/speaker_attribution_p2_convergence_20260427.md`
- `reports/benchmark/speaker_attribution_p2_convergence_20260427.json`

本轮远端扫描：

- US projects root：`/mnt/HC_Volume_105524101/aivideotrans/projects`
- 最近 30 个 job。
- 高风险 job：19 个。
- 同源重复视频组：5 组。

关键结论：

- 不继续添加单视频文本 cue。
- deterministic profiling 适合做观察、UI hint 和候选收敛，但不应自动合并所有低占比 speaker。
- P2-b 仍应走局部 verifier：只有高风险低支撑候选允许触发音频 verifier；`uncertain` 和 `assigned_speaker` 保持现有归属。
- 旧 job 中大量 `speaker_role_distribution=unknown`，后续评估 P2 效果时必须区分“老产物缺 metadata”和“新产物结构规则无效”。

本地验证：

- `python -m py_compile scripts\benchmark\speaker_attribution_report.py`
- `pytest -q tests\test_speaker_attribution_report.py`

下一步：

1. 把 5 组同源重复视频和 19 个高风险 job 作为 P2 第一批收敛集，不再只凭单个新视频下结论。
2. 补充 verifier 报告字段的线上观测：candidate count、decision distribution、applied count、skipped_reason。
3. 对重复视频组比较 `best_by_cost` 与 `best_by_speaker` 是否一致；不一致的组优先人工抽样，判断是 speaker 归属问题还是短段/DSP 问题。
4. 调 P2-b 阈值时只改通用结构条件，例如低支撑 speaker 总时长、行数、dominant share、相邻 speaker 参照，不使用标题、人物名或固定台词。

## 2026-04-27 P2-b audit batch 与强模型局部评判

执行内容：

- 新增 `scripts/benchmark/speaker_attribution_audit_batch.py`。
  - 输入：`speaker_attribution_p2_convergence_20260427.json`。
  - 样本：19 个高风险 job + 5 组同源重复视频，合计 21 个目标 job。
  - 输出：115 个候选段，覆盖 21 个 job。
  - 每个候选段保存结构化上下文：ASR speaker、S2/final speaker、primary speaker、speaker profile、相邻上下文、触发原因。
  - 在容器内用 ffmpeg 裁剪 `±8s` 局部音频 clip，115 个 clip 全部成功，总音频时长约 2238.5 秒。
- 新增 `scripts/benchmark/speaker_attribution_model_judge.py`。
  - 使用 `gemini_pro`（`gemini-3.1-pro-preview`）对局部 clip 做强模型评判。
  - 输出 `decision` 与 `recommended_action`，用于调 P2-b 通用阈值。
  - 支持 `--start` / `--limit` / `--sleep-seconds` 分段执行，避免 429 后丢失结果。

产物：

- `reports/benchmark/speaker_attribution_audit_batch_20260427.json`
- `reports/benchmark/speaker_attribution_audit_batch_20260427.md`
- `reports/benchmark/speaker_attribution_audit_batch_20260427_model_inputs.jsonl`
- `reports/benchmark/speaker_attribution_audit_judged_partial_20260427.json`
- `reports/benchmark/speaker_attribution_audit_judged_partial_20260427.md`
- `reports/benchmark/speaker_attribution_audit_judged_full_20260427.json`
- `reports/benchmark/speaker_attribution_audit_judged_full_20260427.md`

强模型评判进度：

- `gemini_pro` smoke：8/8 成功。
- 全量一次性跑 115 个候选时触发 429 `RESOURCE_EXHAUSTED`，因此改为小批量分段执行。
- 分批完成全量评判后，去重后已评判 115/115 个候选，覆盖 21 个 job。

当前强模型汇总：

| 指标 | 数值 |
|---|---:|
| audit candidates | 115 |
| judged unique candidates | 115 |
| coverage | 100.0% |
| jobs covered | 21 |
| non-keep decisions | 8 / 115（7.0%） |

Decision 分布：

| decision | count |
|---|---:|
| `s2_speaker` | 60 |
| `distinct_speaker` | 36 |
| `asr_speaker` | 12 |
| `music_or_non_speech` | 5 |
| `main_speaker` | 2 |

Action 分布：

| recommended_action | count |
|---|---:|
| `keep` | 107 |
| `mark_non_speech` | 5 |
| `reassign_to_main` | 2 |
| `mark_review` | 1 |

全量结论：

- 低占比 speaker 不能 deterministic 自动合并。115 个候选中 107 个建议 `keep`，说明很多低占比 speaker 是真实主持人、采访者、嘉宾或观众互动。
- `music_or_non_speech -> mark_non_speech` 共 5 个，均为背景歌曲、人群齐声欢呼/喝彩等非正常个人发言。这类内容不应进入音色克隆，也不应作为普通 speaker 给用户强制选音色。
- `main_speaker -> reassign_to_main` 只有 2 个，说明主讲人 continuation 误分存在，但比例低，必须继续依赖局部音频 verifier 的高置信证据，不适合扩大成结构规则。
- 1 个 `mark_review` 是“确实为 distinct speaker，但 profile 归属/命名疑似不对”，适合进入人工抽查或后续 verifier 报告，不适合自动合并。
- 继续按单视频文本 cue 修补没有意义；下一步应把 P2-b 分成两个通用方向：
  1. `non_speech/music/crowd` 被动标记与 voice-selection 降噪。
  2. 低支撑 speaker 局部 verifier，只在高置信时回退 main/ASR，其他情况保持或标 review。

本轮生产化执行：

1. S2 Pass 1 与 Pass 3 prompt 新增 `is_non_speech` / `non_speech_reason` 字段；普通观众提问或短回答不标非对白。
2. P2-b low-support speaker verifier 新增 `non_speech` 决策；只有当某个低支撑 speaker 的全部候选行都被中高置信判为非对白时，才把该 speaker profile 标为 `is_non_speech=true`。
3. `ProcessPipeline._build_speaker_structure_profiles()` 接收 S2/P3 speaker profile，统一落成 `speaker_role=non_speech`、`speaker_role_label=背景音/非对白`、`speaker_review_hint`。
4. voice selection payload 对 `non_speech` speaker 禁用克隆（`can_clone=false`），泛化 speaker 名称显示为“背景音/非对白”，前端展示角色 badge 与提示。
5. metering snapshot 增加 `speaker_non_speech_count`，便于后续比较 non-speech 降噪是否减少误克隆、误选音色和 force-DSP。

下一步：

1. 继续观察新跑视频中 `speaker_non_speech_count`、`speaker_verifier.non_speech_marked` 与 `speaker_verifier.applied`。
2. 若 `music_or_non_speech` 仍以“单段混在正常 speaker 内”出现，下一轮再设计 segment-level non-speech 标记；本轮不直接丢弃或静音任何 SemanticBlock，避免误删真实对白。
3. P2-c 再处理 `mark_review` 类 profile 命名/归属问题，仍以报告驱动，不引入标题、人物名、固定台词 cue。

## 2026-04-27 P2-c 初步收口：verifier 评判汇总可复跑化

背景：

- P2-b 已完成 21 个目标 job、115 个候选片段的局部音频强模型评判。
- 之前的全量汇总依赖分批报告人工拼接，不利于后续重复跑样本、调整阈值或比较不同 verifier prompt。
- P2-c 的第一步不是继续改生产规则，而是把 judge 输出汇总成可复跑的 Go/No-Go 报告。

新增工具：

- `scripts/benchmark/speaker_attribution_judgement_summary.py`
  - 输入：`speaker_attribution_audit_batch_*.json` + 一个或多个 `speaker_attribution_audit_judged_*.json`。
  - 对重复 candidate judgement 去重，默认保留第一次出现的评判，记录 `duplicate_decisions_ignored`。
  - 输出完整覆盖率、decision/action 分布、job 覆盖、reason→action 透视、non-keep 明细。
  - 输出明确 Go/No-Go：
    - `broad_low_support_auto_merge`
    - `verifier_gated_main_reassignment`
    - `non_speech_profile_marking`
    - `phrase_or_title_specific_rules`

本轮复跑命令：

```powershell
python scripts\benchmark\speaker_attribution_judgement_summary.py `
  --audit-batch reports\benchmark\speaker_attribution_audit_batch_20260427.json `
  --judgement reports\benchmark\speaker_attribution_audit_judged_smoke_20260427.json `
  --judgement-glob "reports\benchmark\speaker_attribution_audit_judged_part[0-9]*_20260427.json" `
  --output-dir reports\benchmark `
  --output-stem speaker_attribution_p2_verifier_eval_20260427 `
  --force
```

新增产物：

- `reports/benchmark/speaker_attribution_p2_verifier_eval_20260427.json`
- `reports/benchmark/speaker_attribution_p2_verifier_eval_20260427.md`

自动汇总结果：

| 指标 | 值 |
|---|---:|
| audit candidates | 115 |
| judged unique candidates | 115 |
| coverage | 100.0% |
| jobs covered | 21 |
| duplicate decisions ignored | 12 |
| judge errors | 0 |
| non-keep | 8 / 7.0% |

Decision 分布：

| decision | count |
|---|---:|
| `s2_speaker` | 60 |
| `distinct_speaker` | 36 |
| `asr_speaker` | 12 |
| `music_or_non_speech` | 5 |
| `main_speaker` | 2 |

Action 分布：

| recommended_action | count |
|---|---:|
| `keep` | 107 |
| `mark_non_speech` | 5 |
| `reassign_to_main` | 2 |
| `mark_review` | 1 |

Go/No-Go：

| 项目 | 决策 | 原因 |
|---|---|---|
| Broad low-support auto-merge | NO-GO | 93.0% 候选建议 keep，只有 1.7% 是 main_speaker；不能把低占比 speaker 结构性自动合并。 |
| Verifier-gated main reassignment | CAUTIOUS GO | 只有 2 个高置信 main-speaker 重分配，说明问题存在但比例低，必须继续由局部音频 verifier 触发。 |
| Non-speech profile marking | GO | 5 个 `music_or_non_speech` 都是背景歌曲、人群齐声欢呼等非正常个人发言，适合做 speaker-level non-speech 降噪。 |
| Phrase/title/person-specific rules | NO-GO | 失败类型横跨主持人、嘉宾、观众、音乐和采访短问答，不应继续追加特定台词或特定视频补丁。 |

执行结论：

- P2-b 的“报告驱动调参”已经可以复跑，后续调阈值必须先跑该汇总，不再凭单个视频感受改规则。
- P2-c 当前不应扩大 deterministic speaker merge；只保留 verifier-gated 的 `main_speaker` 重分配。
- `non_speech` speaker-level 处理可以作为 P2 的第一个生产化收口方向：禁用克隆、显示“背景音/非对白”、写入 metering。
- 下一个可做项是 segment-level `non_speech/keep_original` 候选评估，但只有当新跑视频继续出现“非对白单段混在正常 speaker 内”时再做，避免误删真实对白。

本地验证：

- `pytest -q tests\test_speaker_attribution_judgement_summary.py tests\test_speaker_attribution_audit_batch.py tests\test_speaker_attribution_model_judge.py tests\test_speaker_attribution_report.py`

---

## 2026-04-28 补丁：P1-m Underflow DSP 限速并补静音

触发原因：

- 最近任务中发现“首轮 TTS 音频远短于目标槽位，但文本本身信息量很低”的段落被强制 DSP 慢放到极端比例，听感明显变差。
- 典型样本包括 `还剩十秒。` 从约 1.6s 拉到 14.7s，以及旧 Muniba 任务中 `耶！` 被拉到 17s+。

实现策略：

- 主对齐路径的默认 DSP policy 将 underflow 慢放下限收紧为 `atempo_min=0.67`，即最多约 `1.5x` 时长延展。
- 超出部分不再继续慢放，而是由 `fit_audio_to_slot` 在尾部补静音，仍保证最终音频长度等于目标槽位。
- 新增对齐方法 `capped_dsp_underflow`，用于区分“强制 DSP 兜底”与“限速后补静音”。
- 新增 segment 审计字段：
  - `dsp_speed_ratio_used`
  - `dsp_silence_padded_ms`
  - `dsp_truncated_ms`
  - `dsp_initial_duration_ms`
  - `dsp_trimmed_duration_ms`
  - `dsp_stretched_duration_ms`
- metering 新增：
  - `capped_dsp_underflow_count`
  - `dsp_silence_pad_segment_count`
  - `dsp_silence_padded_total_ms`
  - `dsp_silence_padded_max_ms`

部署与验证：

- 已部署到美国主机。
- 远端备份：`/opt/aivideotrans/deploy_backups/p1m_underflow_dsp_20260428T004930Z`
- 远端验证：
  - `py_compile` 通过：`aligner.py` / `translator.py` / `process.py` / `editor_package_models.py`
  - 容器内最小音频验证通过：1s tone 对齐到 5s 槽位，`speed_ratio_used=0.67`，`silence_padded_ms=3520`，最终 5000ms。
  - app 容器重启后 `healthy`，`scripts/linux_remote_workbench_preflight.py app-health` 通过。
- 随后修复一个审计字段清零问题：无 DSP 的 `direct/rewrite_direct` 段不再继承上一段的 `dsp_silence_padded_ms` 等 fit audit。该问题只影响统计字段，不影响实际音频输出。远端 `py_compile`、app 重启和健康检查通过。

---

## 2026-04-28 P4-lite：低信息 underflow 段自动保留原音

触发原因：

- P1-m 已经避免把短 TTS 极端慢放到 3x、5x 甚至更高比例，但对健身计时、口令、过场提示这类低信息段，`capped_dsp_underflow + 尾部静音` 仍不是最佳听感。
- 这些段的共同点不是某个视频标题或固定台词，而是：目标槽位较长、首轮 TTS 很短、源文本词数少、译文 spoken chars 少、内容多为计时/切换/开始/结束/口头填充。
- 继续针对单个视频或具体台词打补丁会过拟合；更通用的做法是只在对齐后已经证明“合成配音不适合填满槽位”的低信息段，改走原音保留。

实现策略：

- 新增 `auto_keep_original_reason` / `auto_keep_original_source` segment 字段。
- 在 S5 对齐完成后、写 snapshot 前执行低信息 underflow 路由：
  - `alignment_method == capped_dsp_underflow`
  - `target_duration_ms >= 4000`
  - `target_duration_ms / first_pass_duration_ms >= 2.5`
  - 源文本词数 `<= 8`
  - 中文 spoken chars `<= 18`
  - 源文本命中低信息 cue 结构，例如 `seconds`、`next`、`rest`、`exercise`、`ready`、`go`、`ok`、`yeah` 等通用口令/填充词
- 命中后把该段设为 `dubbing_mode=keep_original`，物化原音片段，并清理该段的 force-DSP / DSP 审计字段，避免统计仍显示它被强制 DSP。
- 空文本兜底保留原音现在也写入 `auto_keep_original_reason=empty_text`，方便和低信息 cue 路由区分。
- job metering 新增：
  - `auto_keep_original_count`
  - `auto_keep_original_reason_distribution`

边界：

- 该规则不根据视频标题、人物名或固定台词判断，不是单视频补丁。
- 内容型短句不会触发。验证样例中 `OK may be the most recognizable word in the world.` 虽然 underflow，但因信息量高，不会自动保留原音。
- 当前规则位于对齐后，因此主要先改善最终听感；它暂时不减少前置 TTS 成本。若后续多条生产样本证明命中准确，可以再把同一判定前移到 TTS 前，进一步降本。

部署与验证：

- 已部署到美国主机。
- 远端备份：`/opt/aivideotrans/deploy_backups/p4_low_info_route_20260428T012947Z`
- 本地验证：
  - `python -m py_compile src\pipeline\process.py src\services\gemini\translator.py tests\test_process_pipeline.py`
  - `pytest -q tests/test_process_pipeline.py -k "low_information_underflow or auto_keeps_low_information or contentful_underflow"`
- 远端验证：
  - `py_compile` 通过：`process.py` / `translator.py`
  - 容器内断言通过：低信息 cue 返回 `low_information_cue_underflow`，内容型 underflow 返回空原因。
  - app 容器重启后 `healthy`，`scripts/linux_remote_workbench_preflight.py app-health` 通过。

下一步观察：

- 新跑健身、计时、口令类视频时，关注 `auto_keep_original_reason_distribution.low_information_cue_underflow`。
- 对比 `capped_dsp_underflow_count`、`force_dsp_count`、`needs_review_count` 与主观听感，确认低信息段是否从“慢放+长静音”转为更自然的原音。
- 若出现真实内容段被错误保留原音，优先收紧结构条件；不追加具体文本黑名单。

---

## 2026-04-28 P1-n：短内容型问答段口播压缩

触发原因：

- CNBC 巴菲特长访谈同源重跑显示，P1 已显著降低长段和中长段的 `force_dsp`，但 `2~8s` 的真实内容短段仍是主要残留瓶颈。
- 最近 30 个成功任务只读分析显示：
  - `2~8s` 短段：542 个
  - 其中 TTS 明显超长/高风险短段：310 个
  - 排除低信息 cue / 非对白后，真实内容型候选：248 个
  - 候选占高风险短段：80.0%
  - 已被现有 pre-TTS rewrite 接受处理的候选只有 7 个
- 这些段落不是倒计时口令，也不适合保留原音或跨 speaker 合并。典型形态是采访短问句、短回答、技术访谈短判断句。

分析产物：

- `reports/benchmark/short_content_compaction_analysis_20260428.md`
- `.codex_tmp/short_content_compaction_analysis.json`

实现策略：

- 在现有 pre-TTS rewrite 内增加专门分支 `s5_short_content_compact`。
- 触发条件：
  - `2000ms <= target_duration_ms < 8000ms`
  - 仅处理 overshoot
  - decision estimate 超目标至少 30%
  - 源文本至少 3 个词
  - 排除低信息 cue、非对白、背景声、空文本、`keep_original`
  - 当前中文 spoken chars 明显高于短口播窗口
- 字数窗口按短口播 CPS 估算：
  - lower = `max(6, round(target_seconds * 2.6))`
  - upper = `max(lower + 2, round(target_seconds * 4.0))`
- Prompt 明确为“口播压缩”，不同于普通 duration rewrite：
  - 问句优先压成短中文问法。
  - 多个连续问题可合并为一个核心问题。
  - 去掉填充词、寒暄、重复主语、弱连接词。
  - 必须保留数字、否定、关键专名、公司/产品名、时间和方向性判断。
  - 不补背景、不改立场。
- 代码侧硬校验：
  - 输出不能为空。
  - 必须比原文短。
  - 必须落在 lower/upper spoken-char 窗口。
  - 当前中文中已有的数字或全大写 token 必须保留；否则拒绝，避免把 `80/20`、`GEICO`、`TSMC` 等关键信息压丢。
- 若 compact 输出不合格，不再继续用普通 rewrite 兜底，避免双倍 LLM 成本和不可控语义损失；保留原逻辑进入 TTS / review。

新增审计字段：

- segment:
  - `short_content_compact_attempted`
  - `short_content_compact_accepted`
  - `short_content_compact_rejected_reason`
  - `short_content_compact_class`
  - `short_content_compact_lower_chars`
  - `short_content_compact_upper_chars`
  - `short_content_compact_pre_chars`
  - `short_content_compact_post_chars`
- metering:
  - `short_content_compact_attempted_count`
  - `short_content_compact_accepted_count`
  - `short_content_compact_rejected_count`
  - `short_content_compact_rejected_reason_distribution`
  - `short_content_compact_class_distribution`

部署与验证：

- 已部署到美国主机。
- 远端备份：`/opt/aivideotrans/deploy_backups/p1n_short_content_compact_20260428T131340Z`
- 本地验证：
  - `python -m py_compile src\pipeline\process.py src\services\gemini\rewriter.py src\services\gemini\translator.py src\services\llm\router.py tests\test_process_pipeline.py tests\test_rewriter.py`
  - `pytest -q tests/test_rewriter.py tests/test_process_pipeline.py -k "short_content_compact or pre_tts_rewrite or pre_rewrite or short_merge or low_information_underflow"`
- 远端验证：
  - 容器内 `py_compile` 通过。
  - `Would you still say buy stocks right now?` 类短问句被判为 `question`，字数窗口 `(8, 13)`，候选判定为 true。
  - app 容器重启后 `healthy`，`scripts/linux_remote_workbench_preflight.py app-health` 通过。

下一步观察：

- 优先用 CNBC 巴菲特长访谈、Jensen Huang 技术访谈、Anthropic 产品团队访谈这类同源或同型视频验证。
- Go 条件：
  - `2~8s force_dsp` 下降至少 25%。
  - `short_content_compact_rejected_reason_distribution` 不被 `missing_required_token` 或 `below_floor` 主导。
  - 人工抽听确认短问句没有明显语义缺失。
- 若 compact 过于激进，先收紧触发条件或提高 lower/upper，不追加视频标题、人物名、固定台词规则。
