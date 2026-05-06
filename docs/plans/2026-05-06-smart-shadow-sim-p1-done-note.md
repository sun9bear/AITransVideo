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

## 5. P1→P2 待办（不阻 P1 Done，但是 P2 入口前必须闭环）

### 5.1 数据累积侧

- [ ] **post-Phase-D metered jobs ≥ 10**：production 持续跑 → 重跑 P0 collector → 重跑 P1 simulator+aggregator，看 §11 verdict 是否仍 PASS
- [ ] **post-Phase-D metered jobs ≥ 20**：才能开始考虑 P2 决策窗口

### 5.2 关键 gap 闭环（建议远端只读模式，不拉本地）

#### Gap A：`retry_estimation_vs_actual` INCONCLUSIVE

- **现状**：4 jobs 有 metering actual，但 simulator 本地没 project_dir → 无 segments → 无法估算
- **不要这样做** ❌：把 5 个 project_dirs SSH 拉回本地（会带原始音频/字幕/用户内容，不符合最小数据原则）
- **正确路径** ✅：
  1. 在生产服务器上只读运行 simulator：`python smart_shadow_sim_simulator.py --facts <prod_facts> --projects-root /opt/aivideotrans/data/projects --out-dir /tmp/smart_shadow_sim/<run_id>/`
  2. 仅拉回脱敏后的 sidecar：`smart_shadow_decisions.jsonl` / `smart_shadow_report.json` / `aggregate_report.{json,md}`
  3. 不拉 project_dir 任何原始内容
- 闭环后 `retry_estimation_vs_actual.jobs_with_metering` 才会 > 0，p50/p90 估算精度才能给数

#### Gap B：`translation_review_auto_approval` 100% unevaluable

- **现状**：collector 当前不发射 `uncertain_speaker_duration_share`（spec §3.1 已 TODO 标）→ simulator 全 unevaluable
- **修复**：collector 增 1 个聚合字段：每段 `speaker_id == "unknown" OR confidence < 0.5` 的 duration 总和 / total duration
- **影响**：translation_review_diff 4 象限才能填进数据；当前阶段全 5/5 unevaluable

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
