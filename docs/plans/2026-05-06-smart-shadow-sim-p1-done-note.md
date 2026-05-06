# Smart Shadow Sim — P1 Done Note

- 创建日期：2026-05-06
- 状态：**P1 工具链完成，不代表 P2 可进**
- 适用范围：智能版 §15 P1 阶段实施收尾记录
- 关联：
  - Spec：[`2026-05-06-smart-shadow-sim-design.md`](2026-05-06-smart-shadow-sim-design.md)
  - 实施 plan：[`2026-05-06-smart-shadow-sim-plan.md`](2026-05-06-smart-shadow-sim-plan.md)
  - P0 结果：[`2026-05-06-smart-shadow-eval-p0-results.md`](2026-05-06-smart-shadow-eval-p0-results.md)
  - 智能版方案：[`2026-05-04-smart-auto-pipeline-plan.md`](2026-05-04-smart-auto-pipeline-plan.md) §15 P1

---

## 1. Verdict（Bottom Line First）

**P1 工具链 DONE** ✅
**不代表 P2 可启动** ⛔

P1 交付的是离线 shadow simulator + cross-job aggregator 一对工具：
- 真实生产 facts 上能跑出可解释的 stage / segment-level diff
- 三套 hardening guard（AST stdlib-only / PII / paths-in-sync）全绿
- 0 调付费 API、0 写 production `audit/`、0 改 `src/` `gateway/` `services/`

但本次 smoke 数据量（5-job real smoke + 12-job legacy extract）远未达 P0 §7.1 定义的 **post-Phase-D metered ≥10** 入 P0 重跑、**≥20** 进 P2 评估的门槛。**进 P2 决策需要更多 production 真实数据积累，而不是更多代码**。

---

## 2. P1 Done 的边界（明确两件事）

### 2.1 P1 工具链完成 = ✅

- [x] simulator 能针对 1 个 fact 产 per-job sidecars（decisions.jsonl + report.json）
- [x] aggregator 能 glob 多个 sidecar 并产 cross-job aggregate（JSON + markdown）
- [x] 6 个 stage 决策 + per-segment "interesting" 决策都能跑
- [x] 5-bucket diff_kind（match / smart_more_aggressive / smart_less_aggressive / orthogonal / no_studio_signal）正确分类
- [x] unknown voice ID 单独 bucket（`studio_unknown_voices`），不被混入 `preset` 桶（重要修复，见 §3.2）
- [x] 真实生产 facts smoke 通过：5-job 集合在 prod_full 上能跑出符合预期的 aggregate
- [x] 三套 hardening guard 全绿（AST + PII + 跨脚本一致性）
- [x] 离线、stdlib-only、不依赖任何付费 API、不写 production `audit/`

### 2.2 P1 完成 ≠ P2 可启动 = ⛔

**绝对不要**因为本 note "P1 Done" 字样就开始：
- 创建 `service_mode=smart` job
- 给前端加智能版入口
- 启用 `voice_clone_credits` 计费 100/min
- 把 simulator 挂成 production lifecycle hook
- 在生产路径里跑 simulator 触发任何真实 clone / TTS

P2 启动条件（plan §15 / P0 §7.1，不变）：
- post-Phase-D metered jobs **≥ 20**
- aggregator `retry_estimation_vs_actual` 在 ≥10 jobs 上稳定 PASS（p90 估算误差 ≤ 50%，目前 INCONCLUSIVE）
- production 写入真实 `pricing_runtime.json`
- cost p90/p99 在 INCONCLUSIVE 转 PASS

P1 工具就位 + 数据继续累积 + 关键 gap 闭环（§5.2）三步齐了才能开 P2 决策窗口。

---

## 3. Gate 进度回顾

| Gate | 时间 | 内容 | 结果 |
|---|---|---|---|
| Gate 1 | Phase A 完 | skeleton + 3 guards + empty smoke | ✅ Approve |
| Gate 2 (v1) | Phase B 完 | 1-job demo on stale fact | ❌ Reject — vt_ 误判 preset |
| **Gate 2 (v2)** | 195911c 修复 + b1b1f8c 修复 | 1-job demo on refreshed fact + unknown_speakers schema | ✅ Approve |
| Gate 3 (v1) | Phase C 完 | 12-job old extract smoke | ❌ Reject — 数据源错（无 metering / 无 whisper） |
| **Gate 3 (v2)** | prod_full subset + retry semantics 三分 | 5-job smoke 含 post-Phase-D / metering / audit / pre-Phase-D | ✅ Approve |

---

## 4. Gate 3 v2 关键证据（5-job real smoke）

### 4.1 数据源 + 选样

- **facts**：`D:/Claude/temp/smart_shadow_eval/prod_full/facts.jsonl`（38 jobs, P0 时段生产 SSH 跑出）
- **selected**：5 jobs 覆盖 6 个 Codex 验收维度
- **artifacts**：`D:/Claude/temp/smart_shadow_sim/c7_smoke_v2/`
  - `facts.jsonl`（5 selected facts）
  - `aggregate_report.json` + `aggregate_report.md`
  - `<job_id>/smart_shadow_decisions.jsonl` + `smart_shadow_report.json` × 5

| label | job_id (short) | main | metering | whisper | audit | voice cls | 用途 |
|---|---|---|---|---|---|---|---|
| j1 | `2593995420f5...` | 1 | ✅ | small | no | `[c]` | post-Phase-D 简洁 |
| j14 | `8295482dcde7...` | 2 | ✅ | small | no | `[ccu]` | post-Phase-D + 1 unknown |
| j19 | `90df4a8a5506...` | 2 | ✅ | small | **✅** | `[cccuuuuuu]` | post-Phase-D + audit + 多 unknown |
| j29 | `c6cb720d2d68...` | **3** | ✅ | none | no | `[ccc]` | main=3, metering 无 whisper |
| j22 | `b4e64512be54...` | 1 | fallback | none | no | `[u]` | pre-Phase-D 降级 |

> Voice classification: `c=cloned`(vt_/moss_audio_/UUID), `p=preset`(preset_*), `u=unknown`

### 4.2 5-job stage diff 表

| job | elig | voice | clone | trans | tts | sub |
|---|---|---|---|---|---|---|
| j1  | M | **M** | **M** | – | – | **M** |
| j14 | M | O¹ | – | – | – | **M** |
| j19 | M | O¹ | – | – | – | **M** |
| j22 | M | – | – | – | – | – |
| j29 | M | **M** | **M** | – | – | – |

> Legend: M=match · O=orthogonal · –=no_studio_signal · `>`=smart_more_aggressive · `<`=smart_less_aggressive

¹ orthogonal 解释：smart 只识别 main_speaker_count ≤ main_threshold(0.10)，但 studio segments 实际包含更多 speaker。`len(smart) ≠ len(studio)` → 维度不可比 = 诚实差异。

### 4.3 Aggregate 摘要

| 字段 | 值 |
|---|---|
| jobs_simulated | 5 |
| smart_eligibility_breakdown | `pass: 5` |
| voice_selection_diff.smart_studio_match | 2 |
| voice_selection_diff.smart_more_clones | 0 |
| voice_selection_diff.smart_fewer_clones | 0 |
| voice_selection_diff.studio_unknown_voices | **3**（不入 preset 桶 ✓） |
| subtitle_drift_observations.jobs_with_drift_data | **3** |
| subtitle_drift_observations.jobs_with_drift_count_zero | **3** |
| subtitle_sync_policy match_rate | **3/5 (60%)** |
| retry_estimation_vs_actual.jobs_with_metering_actual | 4 |
| retry_estimation_vs_actual.jobs_with_smart_estimate | 0（本地无 project_dir） |
| retry_estimation_vs_actual.jobs_with_metering | 0（INCONCLUSIVE） |
| p2_readiness_signals.post_phase_metered_jobs | **3** |
| p2_readiness_signals.ready_for_p2_rerun | **NO**（3 < 10 阈值） |

### 4.4 P0 §3 数据回看是否被影响

P0 results note §3 数据**仍然可信**（参 P0 results note §11.4 已加更正）：
- §3.1 main speaker 分布（速记：38/38 全过 ≤3 gate）— 不依赖 `_classify_voice_id`
- §3.2 克隆样本可用率（来自 `clone_sample_stats` 容量）— 不依赖 `_classify_voice_id`
- §3.3 阈值矩阵 — 同上
- §3 中的 "Studio 实际克隆 vs preset 占比" 不曾出现，所以 vt_ → preset 误判没污染任何 P0 §3 结论

---

## 4-bis. 远端只读 P0+P1 闭环（2026-05-07，post-publish 增补）

### 4-bis.1 运行参数

- **触发原因**：closure §5.2 Gap A（retry_estimation INCONCLUSIVE）+ Gap B（uncertain_speaker_duration_share 缺失）
- **运行方式**：`D:/Claude/temp/smart_shadow_sim/c7_remote_run/run_remote.py` 一次性 paramiko orchestration
  - 上传 3 个最新 script 到 `/tmp/smart_shadow_p1_run_<run_id>/scripts/`
  - 远端跑 P0 collector + P1 simulator + P1 aggregator（**read-only**，仅用 `/opt/aivideotrans/data/{projects,jobs}`）
  - tarball + sftp 拉回**仅 sidecar JSON**（不拉 project_dir 任何原始内容）
  - 远端 `rm -rf /tmp/<run_id>/` 清理
- **远端 run_id**：`2026-05-06T22-42Z-AIVideoTrans-unknown`
- **本地 artifacts**：`D:/Claude/temp/smart_shadow_sim/c7_remote_run/results/{p0/, p1/}/`
- **数据规模**：38 succeeded jobs（同 P0 全量）

### 4-bis.2 全量 38-job aggregate 关键指标

#### Stage decision 5-bucket 分布

| stage | match | more_aggr | less_aggr | orthogonal | no_studio_signal | match_rate |
|---|---|---|---|---|---|---|
| `eligibility_gate` | **38** | 0 | 0 | 0 | 0 | 38/38 (100%) |
| `voice_sample_selection` | 14 | 0 | 0 | 9 | 15 | 14/38 (37%) |
| `clone_policy` | 14 | 0 | **1** | 0 | 23 | 14/38 (37%) |
| `translation_review_auto_approval` | 1 | **1** | 0 | 0 | 36 | 1/38 (3%) |
| `tts_duration_repair_policy` | 0 | **15** | 0 | 0 | 23 | 0/38 (0%) |
| `subtitle_sync_policy` | 4 | 0 | 0 | 0 | 34 | 4/38 (11%) |

#### 关键聚合

| 字段 | 全量 38 真实数 |
|---|---|
| smart_eligibility | `pass: 38`（≤3 gate 100% 通过） |
| voice_selection_diff.smart_studio_match | **14** |
| voice_selection_diff.smart_more_clones | 0 |
| voice_selection_diff.smart_fewer_clones | 0 |
| voice_selection_diff.studio_unknown_voices | **23**（不入 preset 桶） |
| translation_review_diff.smart_auto_approved_studio_unmodified | **1**（true positive） |
| translation_review_diff.smart_rejected_studio_unmodified | **1**（**smart over-cautious 实例**） |
| translation_review_diff.smart_unevaluable | 36（studio 侧 audit 数据缺，待累积） |
| subtitle_drift_observations.jobs_with_drift_data | **6** |
| subtitle_drift_observations.jobs_with_drift_count_zero | 6 |
| subtitle_drift_observations.jobs_with_drift_count_gt_zero | 0 |
| retry_estimation_vs_actual.jobs_with_metering_actual | **15** |
| retry_estimation_vs_actual.jobs_with_smart_estimate | **38** |
| retry_estimation_vs_actual.jobs_with_metering | **15**（双侧齐全） |
| p2_readiness_signals.post_phase_metered_jobs | **4**（仍 < 10 阈值） |
| p2_readiness_signals.ready_for_p2_rerun | **NO** |

### 4-bis.3 ⚠️ Spec §3.5 retry estimation v1 公式 FAIL

**这是 P1 闭环最重要的发现**——比"P1 工具链 done"重要得多。

#### 数字

| 指标 | 实测 | spec §3.5 期望 |
|---|---|---|
| `smart_estimated_retts_count_p50` | 90 | — |
| `actual_retts_count_p50` | 21 | — |
| `smart_estimated_retts_count_p90` | 237 | — |
| `actual_retts_count_p90` | 83 | — |
| **`estimation_error_p50`** | **385.7%** | ≤ 50% |
| **`estimation_error_p90`** | **666.7%** | ≤ 50% |

p50 偏差超期望上限 **7.7×**，p90 超 **13.3×**。**v1 公式不能用作 P2 cost 推断基础**。

#### 15 个 metered job 的偏差分布（按误差排序）

| job | smart_est | actual | err% |
|---|---|---|---|
| `c6cb720d2d68...` | 30 | 0 | **3000%** |
| `2593995420f5...` | 111 | 7 | 1486% |
| `e4c11402df8d...` | 23 | 3 | 667% |
| `6c60a2b4239f...` | 94 | 13 | 623% |
| `8295482dcde7...` | 151 | 23 | 557% |
| `717fc15dbc56...` | 25 | 4 | 525% |
| `cd545d7b1325...` | 60 | 10 | 500% |
| `bd8d2bcfb3a4...` | 558 | 111 | 403% |
| `90df4a8a5506...` | 102 | 21 | 386% |
| `34e55cab79c4...` | 82 | 26 | 215% |
| `cf8f0b9bd408...` | 90 | 29 | 210% |
| `bf416598a7bf...` | 237 | 83 | 186% |
| `f31f63df2e7c...` | 36 | 14 | 157% |
| `99a2cbb7fc77...` | 91 | 42 | 117% |
| `144e49c9c2b3...` | 80 | 39 | 105% |

15/15 jobs **smart over-predict**，无一例外。最坑的 case `c6cb720d`：smart 说要 30 次 retry，Studio 实际跑了 0 次。

#### 根因分析（看 segment 级别）

跨 38 jobs 的 "interesting" segment decisions 分布：
- **177** segments 仅触发 `expected_rewrite`（true positive 概率高 — Studio 也走了 rewrite 路径）
- **1534** segments 仅触发 `expected_retts` **（length overflow only，无 rewrite）**——smart 把"cn_text 字符数 > 240×duration_min×1.05"等同于"必发生 retts"
- **797** segments 同时触发两者 → 在 `_estimate_retry` 里被**双计**（`expected_retts += 1` 在 if rewrite 里 + 1 次，在 if length 里又 + 1 次）

三个具体问题：

1. **双计 bug**：`scripts/smart_shadow_sim_simulator.py::_estimate_retry` 对同时满足两条件的 segment 累加 2 次到 `expected_retts`。spec §3.5 没明确禁止，但实证 797 segments 落进这个区间，**显著放大估算**。
2. **length-only 触发器太乐观**：1534 segments 只是 cn_text 字符数偏长，但首次 TTS 在 duration tolerance 内合成成功的占绝大多数。length 是 retts 的**必要非充分条件**——TTS 速度 / 暂停剥离 / 角色变速等多个因素决定首发是否过线。
3. **`k_cn_chars_per_src_min=240` 是粗略 default**：未按 voice_id / TTS provider / 语速校准。

#### v2 公式建议（spec §3.5 升级路径）

- 修复双计：`expected_retts += 1`分支二选一，不重复计入
- length 触发器降级：要么不计入 retts、只标记 segment "可能 retts"，要么乘以经验系数（实测每 length-overflow segment 实际 retts 率）
- per-voice / per-provider k 校准：从 metered jobs 反推 `actual_retts_per_segment / cn_chars_per_segment` 分桶

阻 P2 程度：**强**。任何基于 §3.5 公式的 P2 cost 推断（spec §11 / smart-auto-pipeline-plan §15）都会偏 4-7×，商业可行性结论会全部偏。

### 4-bis.4 Gap B 闭环效果

- **before**：`stages_unevaluable_rate.translation_review_auto_approval = 100%`（全 missing_signals）
- **after**：`stages_unevaluable_rate = 0%`（38/38 都给出 smart decision）
- **diff_kind 分布**：
  - `match`: 1（smart auto_approve + studio 没改 = 真正例）
  - `smart_more_aggressive`: 1（smart 说要 manual review，但 Studio 用户没改 = smart 多管闲事，**首个 false positive 实例**）
  - `no_studio_signal`: 36（studio 侧无 audit/user_edit_events.jsonl，=> studio_actual=unknown）

值得展开看的两个 case（待 P0 results note 收集 job 级证据）：
- 1 真正例：smart auto_approve 路径在哪一类 job 上对了
- 1 假正例：smart 误判的 manual review 是哪个 case，触发条件是什么

### 4-bis.5 voice_selection 实证

- 14/38 (37%) 真实 match — 所有 main speaker 都被 vt_ / moss_audio_ / UUID 形态克隆音色覆盖
- 23/38 jobs 含 unknown studio voices（**60%**）：production 大量 voice_id 不匹配 vt_/moss_audio_/preset_/UUID 模式。这是 P0 results note §11.2 提及的"修复前会被误判 preset"的真实人群——**60% 的 production job 受影响**
- 1 个 `clone_policy.smart_less_aggressive` 实例：smart 说不要 clone（main speaker 样本 < 8s soft）但 Studio 实际克隆了。这是 smart 阈值过保守的实证候选

### 4-bis.6 subtitle_sync_policy 实证

- post-Phase-D detection 准确：4/38 jobs 有 whisper.alignment_model="small"，全部 match
- 6 jobs 有 drift_data（含 alignment_model None 但 drift 字段非 None 的混合 case），全部 drift_count=0 — Phase D Whisper alignment 实际产物质量好

### 4-bis.7 P2 readiness 现状

| 指标 | 实测 | 阈值 |
|---|---|---|
| post_phase_metered_jobs | **4** | ≥ 10 (P0 重跑) / ≥ 20 (P2 启动) |
| ready_for_p2_rerun | NO | — |

P0 results note §7.1 的 ≥10 metered 阈值现在仍然不到。继续等数据累积。

### 4-bis.8 工件归档

- 远端 run_id：`2026-05-06T22-42Z-AIVideoTrans-unknown`
- Tarball：`D:/Claude/temp/smart_shadow_sim/c7_remote_run/results/sidecars.tar.gz`（67 KB）
- 解压后：
  - `p0/facts.jsonl`（38 facts，新含 `uncertain_speaker_duration_share` 字段）
  - `p1/aggregate_report.{json,md}`
  - `p1/job_<id>/smart_shadow_decisions.jsonl` × 38
  - `p1/job_<id>/smart_shadow_report.json` × 38
- 远端 `/tmp/<run_id>/` 已清理
- 本次只拉 sidecar JSON，**0 字节** project_dir 原始内容（音频/字幕/用户文本）触本地

---

## 5. P1→P2 待办（不阻 P1 Done，但是 P2 入口前必须闭环）

### 5.1 数据累积侧

- [ ] **post-Phase-D metered jobs ≥ 10**：production 持续跑 → 重跑 P0 collector → 重跑 P1 simulator+aggregator，看 §11 verdict 是否仍 PASS
- [ ] **post-Phase-D metered jobs ≥ 20**：才能开始考虑 P2 决策窗口

### 5.2 关键 gap 闭环

#### Gap A：~~`retry_estimation_vs_actual` INCONCLUSIVE~~ → **CLOSED, but reveals P2 blocker**

- **2026-05-07 闭环**（远端 P0+P1 read-only run，§4-bis.1）
- **结果**：jobs_with_metering=15，**spec §3.5 v1 公式 FAIL**（p50=385.7%，p90=666.7%，远超 ≤50% 期望）
- **新待办**：spec §3.5 retry estimation **v2 公式**（详见 §4-bis.3 三条根因 + 三条建议）
  - [ ] 修双计 bug：rewrite + length 同 segment 不重复加
  - [ ] length-only 触发器降级或乘经验系数
  - [ ] per-voice / per-provider `k_cn_chars_per_src_min` 校准（用 metered jobs 反推）
- **阻 P2 程度**：**强**——任何基于 §3.5 v1 公式的 P2 cost 推断（spec §11 / smart-auto-pipeline-plan §15 商业可行性）会偏 4-7×

#### Gap B：~~`translation_review_auto_approval` 100% unevaluable~~ → **CLOSED on tooling side**

- **2026-05-06 闭环**（commit `e6e5c36`）：collector 增 `speaker_stats.uncertain_speaker_duration_share` 字段，从 `transcript/s2_review_audit.json` 的 `audit_events[].source == "correction"` 派生
- **结果**：
  - smart 侧 unevaluable rate: 100% → 0%（38/38 给出真实决策）
  - studio 侧仍多数 unknown（36/38）：因为 production 大多数 job 没 `audit/user_edit_events.jsonl`
- **剩余 gap**：studio 侧 audit 数据累积——这是 production 经历更多 post-edit 流量的事，**非工具问题**
- **意外发现**：1 个 smart_more_aggressive 实例（smart 说 manual review，但 Studio 用户没改 cn_text）。值得抽检看 smart 触发条件是否过于保守

#### Gap C（new from §4-bis.5）：production voice_id 60% unknown 分类

- 38 jobs 里 23 jobs (**60%**) 的 studio voice_ids 不匹配 `vt_/moss_audio_/preset_/UUID` 已知模式
- P0 results note §11.2 已指出 195911c 修复前会被误判为 preset → P1 假阳性 smart_more_aggressive 也会假高
- **后续**：抽样这 23 jobs 的 voice_ids 形态，扩展 `_classify_voice_id` 模式集（或 surface 出来归类，避免 unknown 桶过大造成 voice_selection_diff 多数 INCONCLUSIVE）

### 5.3 production 准备

- [ ] `pricing_runtime.json` 写入真实单价 snapshot（替代 code defaults，见 P0 §2.3 caveat）
- [ ] P0 §8 4 个 known diagnostic gaps 跟踪进展（`workflow_alignment_cache` 0 命中 / `orphaned_project_dir_count = 11` / pricing snapshot / audit 命中率）

---

## 6. P1 commits 清单（共 20 commits）

| # | sha | type | desc |
|---|---|---|---|
| 1 | `30a4db7` | feat | simulator skeleton |
| 2 | `123668e` | feat | aggregator skeleton |
| 3 | `f311cf8` | feat | simulator facts loader + per-job sidecar scaffolding |
| 4 | `6f34d45` | feat | aggregator dir scan + empty aggregate writer |
| 5 | `e4afc6d` | test | AST import guards |
| 6 | `1d4fe29` | test | PII injection guard |
| 7 | `ca4ad55` | docs | P1 spec + plan |
| 8 | `95bdc17` | feat | simulator inline editor/segments.json reader |
| 9 | `880ca4e` | feat | simulator stage decisions — eligibility/voice/clone |
| 10 | `58874c8` | feat | translation_review_auto_approval |
| 11 | `30c7faa` | feat | tts_duration_repair_policy retry v1 |
| 12 | `1a5e8aa` | feat | subtitle_sync_policy + pre-Phase-D fallback |
| 13 | `81bbb7a` | feat | studio_actual extraction (6 stages) |
| 14 | `f10fb1a` | feat | diff_kind classification + match field |
| 15 | `c5a0d94` | feat | per-segment decisions |
| 16 | `490e98c` | feat | per-job report.json schema |
| 17 | `195911c` | fix  | vt_ prefix is cloned voice |
| 18 | `b1b1f8c` | fix  | unknown_speakers in actual_clone_stats; clone_policy unknown handling |
| 19 | `4a55eb0` | feat | aggregator C2-C5 (5-bucket / unevaluable / voice / trans / drift / retry / p2 / markdown) |
| 20 | `516a80f` | docs | P0 results note §11 fix impact |
| 21 | `e8f0e6c` | feat | aggregator splits jobs_with_metering into actual / smart_estimate / both |

实际 21 个；包含 1 个 spec/plan docs commit + 1 个 P0 docs 修正 commit + 19 个 code/test commits。

测试统计：**94 passed, 1 skipped**
- P0 collector + analyzer + guards: 45
- P1 simulator + guards: 38
- P1 aggregator: 14
- 共享 baseline 已扣（重叠 -3）

---

## 7. 工件归档

- C7 v2 真实 smoke：`D:/Claude/temp/smart_shadow_sim/c7_smoke_v2/`
- C7 v1 legacy graceful smoke：`D:/Claude/temp/smart_shadow_sim/c7_smoke/`
- Gate 2 v3 单 job demo：`D:/Claude/temp/smart_shadow_sim/gate2_demo_v3/`
- 修复后本地 12-job collector dump：`D:/Claude/temp/smart_shadow_eval/prod_full_refreshed_v2/`
- 原始 P0 全量 38 jobs：`D:/Claude/temp/smart_shadow_eval/prod_full/`

---

## 8. 决策摘要

```
P1 DONE — 工具链交付完成，不代表 P2 可启动。

✅ simulator + aggregator 双脚本完成，stdlib-only，0 付费 API
✅ 三套 hardening guard 全绿（AST + PII + 跨脚本一致性）
✅ 6 stage + per-segment "interesting" 决策正确分类
✅ 5-bucket diff_kind 正确产出
✅ unknown voice ID 单独 bucket（不入 preset）
✅ Gate 3 v2 prod_full 5-job smoke 通过
   - subtitle_sync_policy 3/5 真实 match
   - voice_selection_diff 不混 unknown 入 preset
   - retry_estimation_vs_actual 三字段诚实分账（actual=4 / smart=0 / 交集=0 INCONCLUSIVE）
✅ P0 results note §11 已记录 vt_ + unknown_speakers 修复对历史数据的影响
✅ 21 commits, 94 passed 1 skipped

⛔ 不要因 P1 Done 启动 P2：
   - post-Phase-D metered 当前仅 3 个 < 10 阈值
   - retry_estimation 仍 INCONCLUSIVE，未在真实 metered 集合上验证过 estimation 精度
   - production pricing snapshot 缺
   - translation_review 100% unevaluable（collector 字段缺失）

⏭ 下一步：
   1. production 持续跑积累 metered jobs
   2. 远端只读 simulator 闭环 retry_estimation（不拉 project_dir）
   3. collector 补 uncertain_speaker_duration_share 字段
   4. metered jobs 累积到 ≥10 后重跑 P0+P1 重新评估
```
