# Smart Shadow Eval — P0 Results Note

- 创建日期：2026-05-06
- 状态：**conditional PASS for P1 shadow，NOT PASS for P2 production launch**
- 适用范围：智能版 §15 P0 阶段实测结果记录
- 关联：
  - 评估工具 spec：[`2026-05-06-smart-shadow-evaluator-design.md`](2026-05-06-smart-shadow-evaluator-design.md)
  - 评估工具 plan：[`2026-05-06-smart-shadow-evaluator-plan.md`](2026-05-06-smart-shadow-evaluator-plan.md)
  - 智能版方案：[`2026-05-04-smart-auto-pipeline-plan.md`](2026-05-04-smart-auto-pipeline-plan.md) §15 P0

---

## 1. Verdict（Bottom Line First）

**P0 PASS for P1 shadow** ✅
**P0 NOT PASS for P2 production smart launch** ❌

- P1 是真实 Studio 任务完成后的"如果是智能版会怎么决策"模拟，**不调用付费 API、不影响交付、不对用户收费**，所以 §1.1B 字段 n=3 的 early signal 不应阻塞它
- P2 是真实创建 `service_mode=smart` job + 真实扣 100 credits/min + 真实自动 clone / re-TTS —— 当前证据不足以支持

---

## 2. Run Metadata

### 2.1 Full run (§1.1A 校准)
- **`run_id`**：`2026-05-06T11-14Z-AIVideoTrans-unknown`
- **样本量**：38 succeeded jobs（all `--since 2026-01-01`）
- **errors**：0；**is_complete_run**：true
- **`skipped_for_status_filter`**：1（一个非-succeeded 的 job 被过滤）
- **`orphaned_project_dir_count`**：11（生产数据 hygiene 信号）
- **artifact**：[`D:/Claude/temp/smart_shadow_eval/prod_full/report_all/report.md`](../../../temp/smart_shadow_eval/prod_full/report_all/report.md)

### 2.2 Post-Phase-D smoke run (§1.1B early signal)
- **`run_id`**：`2026-05-06T11-03Z-AIVideoTrans-unknown`
- **样本量**：**3 jobs**（`--since 2026-05-05 --limit 3`）
- **errors**：0；**is_complete_run**：true
- **`skipped_for_date_filter`**：17
- **artifact**：[`D:/Claude/temp/smart_shadow_eval/prod_smoke/report_post_phase/report.md`](../../../temp/smart_shadow_eval/prod_smoke/report_post_phase/report.md)

### 2.3 Pricing 来源 caveat
**Production 没有 `pricing_runtime.json` 文件**（SSH 验证：`/opt/aivideotrans/config/` + container 内 `find` 都未命中）。两份报告的成本估算都基于 `gateway/pricing_schema.py::build_default_pricing_payload()` 默认值：
- `point_cost_rmb=0.015`、`point_price_rmb=0.03`、`k_cn_chars_per_src_min=250`
- `*_cost_rmb_per_src_min`: translate 0.03 / s2_review 0.02 / rewrite 0.02 / server 0.03
- `voice_clone_cost_credits=500`

任何基于 §8/§9 RMB 数字的商业判断都要先问"production 有没有写过自定义单价"。

---

## 3. §1.1A 数据（基于 38 jobs，足够下结论）

### 3.1 Speaker 数分布

| Threshold | Main ≤ 3 | Main ≤ 2 | Main ≤ 1 |
|---|---|---|---|
| 0.05 | **38/38 (100%)** | 34/38 (89%) | 15/38 (39%) |
| 0.10 | **38/38 (100%)** | 36/38 (95%) | 16/38 (42%) |
| 0.15 | 38/38 (100%) | 36/38 (95%) | 18/38 (47%) |
| 0.20 | 38/38 (100%) | 37/38 (97%) | 25/38 (66%) |

**结论**：Smart `main_speaker_count ≤ 3` gate **零拒绝率**。历史数据 100% 通过。

### 3.2 克隆样本可用率（按 main_count 分桶）

| main_count | jobs | all-eligible (≥5s) | all-eligible (≥8s) |
|---|---|---|---|
| 1 | 16 | 100% | 100% |
| 2 | 20 | 100% | 100% |
| 3 | 2 | 100% | 100% |

**结论**：所有 main speaker 都有充足克隆样本（≥5s 和 ≥8s 各 100%）。

### 3.3 §10 阈值校准矩阵

| min_sample_seconds | 0.05 | 0.10 | 0.15 | 0.20 | 备注 |
|---|---|---|---|---|---|
| ≥5s | 100% | 100% | 100% | 100% | 全部 eligible |
| ≥8s | 100% | 100% | 100% | 100% | 全部 eligible |
| ≥10s | 100% | 100% | 100% | 100% | 全部 eligible |
| ≥15s | 92% | 92% | 92% | 95% | 5-8% degraded |

**结论**：
- ≥15s 作为硬要求 = **5-8% jobs 进入 degraded** —— 没必要这么严
- ≥10s 作为硬要求 = 0% degraded —— 可以考虑
- ≥8s 是稳妥默认（100% eligible 跨所有 main_threshold）

---

## 4. §1.1B 数据（基于 3 jobs，仅 early signal）

⚠️ **n=3 不能做产品决策**。下面数字方向都很好但样本太小。

### 4.1 字幕一致性（§6）
- **`text_audio_drift_count = 0`：3/3 (100%)** ✅
- 没有 high-drift case
- 强信号：post-Phase-A/B 的 `tts_input_cn_text` 链路在生产上工作正常

### 4.2 Whisper 覆盖（§7）
- `alignment_model = small`：3 jobs
- `whisper_aligned / total cue`：**p50=98.62%, p90=99.14%**
- `whisper_sidecar_count`：p50=74, p90=128
- 强信号：deliverable-time Whisper 在生产上 >98% 覆盖

### 4.3 Retry / Cost / Margin（§5/§8/§9）
- **§5 retry**（all 3 metering）：rewrite_count p50=70, retts_count p50=21, retts_audio/src ratio p50=25%
- **§8 margin (RMB)**：p50=66.64, p90=121.64, p99=121.64（**3/3 正毛利**）
- **§11 verdict**：**PASS** —— 但只是 n=3 PASS，不能据此进 P2

### 4.4 全量 §11 verdict（含 35 老 job）
- margin RMB: p50=50.06, p90=193.52, p99=319.79
- **verdict: INCONCLUSIVE (metering data < 50%)** ✅ 正确反映：35/38 任务无 metering 不能给可靠结论

---

## 5. P1 执行边界

进 P1 Shadow 智能决策时**严格遵守**：

| 边界 | 状态 |
|---|---|
| 创建 `service_mode=smart` job | ❌ 禁止 |
| 前端展示智能版入口 | ❌ 禁止 |
| 扣 100 credits/min | ❌ 禁止 |
| 调用真实 clone / TTS / verifier 付费 API | ❌ 禁止 |
| 触发 Whisper 重跑 | ⚠️ 仅当 Studio 交付路径本来会跑时 |
| 生成 `smart_shadow_decisions` / `smart_shadow_report` sidecar | ✅ 允许 |
| 对比 Studio 用户实际修改 / user_edit_events / usage_meter / 最终交付结果 | ✅ 允许 |

---

## 6. P1 初始阈值建议（基于本次 P0 数据）

| 参数 | 建议值 | 来源 |
|---|---|---|
| `main_speaker_threshold` | **0.10** | §3.1 显示 0.10 在 main ≤ 2 区分度最佳（95%） |
| Smart gate | `main_speaker_count ≤ 3` | §3.1 全部 38 jobs 都通过 |
| Clone sample 单段最低秒数 | **≥ 8s 软标准（≥10s 优先）** | §3.3 矩阵显示 ≥8s 全 100% eligible |
| Clone sample 数量门槛 | **每 main speaker 至少 3 段 ≥8s，合计 ≥ 20s** | 沿用 §7.2 spec 默认 |
| `≥15s` 硬要求 | **不采纳** | §3.3 显示会带 5-8% degraded，没必要 |

---

## 7. 进 P2 之前必须满足的条件

### 7.1 数据累积门槛
- **post-Phase-D metered jobs ≥ 10**：重跑 P0 report，确认 §11 verdict 仍 PASS
- **post-Phase-D metered jobs ≥ 20**（覆盖更多内容类型）：才能考虑 P2 实施

### 7.2 P2 实施前必须再看的指标
- **cost p90/p99**：在 INCONCLUSIVE 转 PASS / FAIL 后才能定 100 cred/min 的可行性
- **`text_audio_drift_count` 分布**：drift > 0 的 job 占比
- **Whisper fallback 原因**：proportional_fallback_cue 的根本原因分布
- **Retry ratio**：retts_audio/src 是否仍 ≤ 30%（spec §9 上限 1.5x = 50%）

### 7.3 Production 准备
- 写入 `pricing_runtime.json` 真实快照（替代 code defaults）
- 解决 known diagnostic gaps（见 §8）

---

## 8. Known Diagnostic Gaps（不阻塞 P1）

| Gap | 影响 | 后续处理 |
|---|---|---|
| `workflow_alignment_cache` 0 命中 — production project_state 没有 `audio_alignment` / `subtitle_alignment` / `alignment` 任一 stage | 不影响 §7 Whisper（已隔离） | spec §12 #1 关闭：production 真实 stage 名跟方案推测不匹配，§7b 留作 P3 verifier-driven repair 时再回头看 |
| `orphaned_project_dir_count = 11` | 数据 hygiene 信号 | 运维侧排查（11 个 project_dir 无 JobRecord，可能是 crashed jobs / cancelled / 异常清理） |
| `pricing_runtime.json` production 不存在 | 成本估算用 code defaults | P2 前由 ops 写入真实单价 snapshot |
| `audit/user_edit_events.jsonl` 1/3 jobs 命中 | 仅 1 个 post-Phase-D job 走过 post-edit，audit 数据样本极小 | P1 累积过程中自然增加 |

---

## 9. 决策摘要

```
P0 conditional PASS for P1.
- §1.1A 全 strong evidence (38/38).
- §1.1B early signal good but n=3.
- Cost INCONCLUSIVE on full 38, PASS on n=3 (3/3 positive margin, p50=66.64 RMB).
- Pricing snapshot absent, used code defaults.
- 4 known diagnostic gaps documented, none block P1.
- Recommend P1 with main_threshold=0.10, gate ≤3, sample ≥8s soft / ≥10s preferred.
- Re-evaluate P0 when post-Phase-D metered jobs ≥10.
- P2 launch decision waits for ≥20 metered jobs + cost p90/p99 stable + pricing snapshot in place.
```

---

## 10. 工件归档

- 全量报告：`D:/Claude/temp/smart_shadow_eval/prod_full/report_all/`
- Post-Phase-D 报告：`D:/Claude/temp/smart_shadow_eval/prod_smoke/report_post_phase/`
- Facts dumps：`D:/Claude/temp/smart_shadow_eval/prod_full/facts.jsonl` (38 facts) 和 `prod_smoke/facts.jsonl` (3 facts)
- Run summaries：同目录 `summary.json`

---

## 11. 后置修正：vt_ 前缀分类 + unknown_speakers schema（2026-05-06，P1 Gate 2 期间发现）

P1 simulator 实施期间 Codex 第二意见审查 **`smart_shadow_eval_collector._classify_voice_id`** 发现两轮 bug，对本 note §3 / §4 部分数字做事后修正：

### 11.1 第一轮（commit `195911c`）：vt_ 前缀被误分为 preset

**触发**：production 用户克隆音色 ID 走 `vt_<speaker>_<timestamp>` 格式（参 `src/pipeline/process.py::_validate_cloned_voices`），collector 当时只识别 `moss_audio_*` + UUID-like 两种克隆形态，把 `vt_*` fallback 默认归为 `preset`。

**对本 note 的影响**：§3.2 / §3.3 表格依赖 `clone_sample_stats`（容量统计）而非 `actual_clone_stats`（实际选择），所以**§3 数据不变**。但若有后续基于本 38-job dump 重算"Studio 实际克隆 vs preset 占比"的分析，需注意：

- 旧 `actual_clone_stats.cloned_speakers / preset_speakers` 把 `vt_*` 计入 preset 桶 → 当年若做"Studio 主动克隆比例"统计会偏低。
- 修复后默认应假设：production main speakers 走 vt_ 克隆是主流路径。

### 11.2 第二轮（commit `b1b1f8c`）：unknown_speakers 桶缺失 + clone_policy false-positive

**触发**：195911c 把 `_classify_voice_id` 改成 3 类（`cloned/preset/unknown`）但 `_compute_actual_clone_stats` 还是只 count 2 桶；P1 simulator `clone_policy._classify_diff` 也把 unknown 静默丢掉，造成 `smart_set={0} > actual_set={}` 假阳性 `smart_more_aggressive`。

**修复内容**：
- collector schema 增加 `unknown_speakers` count + `classifications_by_speaker` 平行数组。invariant: `cloned + preset + unknown == len(voice_ids_by_speaker)`。
- simulator `_extract_studio_actual` 为 clone_policy 增 `unknown_speaker_indices`；`_classify_diff` 在该列表非空时直接降级 `no_studio_signal`（与 voice_sample_selection 保持一致）。

**对 P1 真实 smoke 数据的影响**（C7 5-job smoke）：12 jobs 重新走 collector 后发现 **5/12 jobs 至少有 1 个 unknown 分类的 voice_id**（占 41%）。这些 voice_id 不匹配 `vt_*` / `moss_audio_*` / `preset_*` / UUID 任一形态，典型为内部测试音色 / 早期 schema 残留。修复前这些 job 会被误判为 `preset_speakers` 升高 → P1 simulator voice_sample_selection 假阳性 `smart_more_aggressive`。

### 11.3 重跑验证

| 数据集 | facts | 修复前 | 修复后 |
|---|---|---|---|
| 本 note §3 38-job 全量 | 不可重跑（生产 SSH 当时数据，已变） | — | 待生产 collector 重跑后回填 |
| 本地 12-job extract | `prod_full_refreshed_v2/facts.jsonl` | n/a | 已重跑，§3 类型分布维持 100%（不变） |
| C7 smoke 5-job | `c7_smoke/facts.jsonl` | 旧 schema 会出 ≥2 假阳性 more_aggressive | 0 假阳性（见 P1 aggregate） |

### 11.4 §3.x 数据是否仍然可信

- **§3.1 main speaker 分布**：完全不依赖 `_classify_voice_id`（来自 `speaker_stats`），**仍然可信**。
- **§3.2 克隆样本可用率**：来自 `clone_sample_stats`（容量），不依赖分类，**仍然可信**。
- **§3.3 阈值矩阵**：同 §3.2，**仍然可信**。
- **若以本 note 38-job 数据反推"Studio 实际克隆 vs preset 比例"**：旧数据被偏置，需要重跑生产 collector 后再下结论。本次未做（生产 SSH 没排队）。

### 11.5 行动项

- [x] **2026-05-07 远端只读 collector 重跑**（38 jobs）已完成，回填三桶真实分布：
  - **23/38 jobs (60%)** 至少含 1 个 unknown 分类的 voice_id
  - 14/38 (37%) 全 cloned（vt_ / moss_audio_ / UUID）
  - 0/38 显式 preset_*（即 production 实际不用 `preset_*` 命名约定）
  - 余 1 job 是混合
  - 详见 P1 Done note §4-bis.5
- [x] P1 simulator + aggregator 已对齐新 schema（commits `195911c` `b1b1f8c` `4a55eb0` `e8f0e6c`）。
- [x] 5 个新回归测试钉住 invariant 与 unknown 分类不再退化。

---

## 12. 2026-05-07 增补：spec §3.5 retry estimation v1 公式 FAIL

> **状态摘要**（2026-05-07）：v1 FAIL → v2 LANDED（commit `21e1653`）；详见下方 §13。本节保留 v1 失败的初始证据。

P1 Done note §5.2 Gap A 的远端只读闭环（2026-05-07）拿到了真实估算精度数字。**结论**：

| 指标 | 实测 (n=15 metered jobs) | spec §3.5 期望 |
|---|---|---|
| `estimation_error_p50` | **385.7%** | ≤ 50% |
| `estimation_error_p90` | **666.7%** | ≤ 50% |

**v1 公式 FAIL** —— 15/15 metered jobs 全部 smart over-predict actual retts，最坏 case 30 vs 0（3000% 误差）。

### 12.1 对本 note §11 verdict 的影响（v1 时刻立场，已在 §13 中重新表态）

P0 §11 verdict（2026-05-06）当时基于 38 jobs 给的是 **conditional PASS for P1 shadow，NOT PASS for P2 production launch**——这个判断仍然成立。**v2 LANDED 之后**（详见 §13）已不再要求 "retry estimation 公式 ≤50% 命中" 作为 P2 硬阻塞；P2 cost 推断改走 **metered actual + safety margin** 路径，不依赖 simulator 估算精度。

### 12.2 对本 note §6 / §7 主张的影响

- §6 推荐的"P1 初始阈值"不依赖 retry estimation，**仍然有效**
- §7.2 提及"retts_audio/src 是否仍 ≤ 30%"是 Studio 实测数据（来自 metering），不依赖 smart 公式预测，**仍然有效**
- §11 决策摘要的"Recommend P1 with main_threshold=0.10..." 是 P1 操作建议，与 P2 cost 推断解耦，**仍然有效**
- ⚠️ 任何基于"smart 估算成本"做的 P2 商业可行性推断（如果存在）现在**不再可信**——v1 公式 over-predict 4-7× 会让 smart 看起来比实际贵得多。v2 把这层偏差从 4-7× 降到 ~1.2×，但仍偏保守，请直接走 metered actual

### 12.3 行动项

- [x] **spec §3.5 公式 v2**（P1 Done note §4-bis.3 → 实施 commit `21e1653`）
- [x] v2 实施后用同一份 prod facts.jsonl 重跑 simulator：`estimation_error_p50=75.0%, p90=119.8%`（5× 改善但未达 ≤50% 原目标）
- [x] **决议变更**：原 ≤50% 不再作为 P2 入口条件；详见 §13

---

## 13. 2026-05-07 增补：spec §3.5 retry estimation v2 LANDED — verdict 与 P2 入口条件调整

### 13.1 v2 实施摘要

- **commit**：`21e1653` — `feat: §3.5 retry estimation v2 LANDED + soft-signal demarcation`
- **修复**：删 length-only stage-level 贡献（保留为 per-segment soft signal）+ per-seg max（修双计）
- **测试**：`tests/test_smart_shadow_sim_retry_v2.py` 9 个新测试 + 既有 simulator/aggregator 旧测试更新到 v2 数字。`tests/test_smart_shadow_*.py` 总数 99→108 passed, 1 skipped
- **Soft-signal demarcation**：per-segment `expected_retts: True` 是诊断/人工审用信号，**P2-alpha 严禁**用它当 re-TTS action trigger（注释加在 simulator 两处 docstring）

### 13.2 v2 实测数字（同一份 15 metered c7 jobs）

| 指标 | v1 | **v2** | 原 spec §3.5 期望 |
|---|---|---|---|
| `estimation_error_p50` | 385.7% | **75.0%** | ≤ 50% |
| `estimation_error_p90` | 666.7% | **119.8%** | ≤ 50% |
| smart 偏向 | over-predict 15/15 | over-predict 14/15 + exact 1/15 | — |
| 改善倍数 | — | **5.1×** (p50) / **5.6×** (p90) | — |

per-job 表见 P1 Done note §4-bis.3。

### 13.3 Verdict（2026-05-07 决议）

**v2 公式 LANDED, 5× improvement, but FAIL original ≤50% target; ACCEPTED as conservative planning signal**。

理由（决策记录）：
- v2 偏保守（over-predict 14/15、0 under-predict 超 parity）→ 不会让真实成本暴击预算 cap
- 单线性系数（如 `rewrite_total × 0.40`）能把 p90 推到 ~60% 但：(1) n=15 上经验拟合过拟合；(2) 仍未达 50%；(3) scaling 引入 under-predict 风险，对成本闸更危险
- 真正达标需要 per-voice / per-provider `k_cn_chars_per_src_min` 校准，受 metered 样本量阻（v3 backlog）

### 13.4 P2 入口条件调整（取代 §12.1 临时表述）

**移除**：
- ~~"P2 入口前必须 spec §3.5 retry estimation **v2 公式 + 在新 metered jobs 上重测 ≤50%**"~~

**保留**（仍要满足）：
- post-Phase-D metered jobs ≥ 20
- cost p90/p99 stable
- production `pricing_runtime.json` snapshot 写入

**P2 cost 推断路径变更**：
- ❌ **不再**用 simulator `expected_retts_count` 做精确成本估算
- ✅ 改用 **metered actual + safety margin**（即根据 production 已观测的 retts/rewrite 真实分布定保守上限，simulator 估算只作 sanity check）
- ✅ Smart shadow simulator 的 `expected_retts_count` 仅作**保守 planning signal**：`expected_retts_count > N` 触发 "可能贵" 警示，但不直接换算金额扣点

### 13.5 v3 backlog（不阻 P2-alpha 启动）

- [ ] **per-voice / per-provider `k_cn_chars_per_src_min` 校准**
  - 触发条件：`post_phase_metered_jobs ≥ 30 且 per-voice metered ≥ 10`
  - 目标：把 p90 从 v2 的 119.8% 推到 ≤ 50%（spec §3.5 原目标）
  - 当前阻塞：n=15 jobs 不够分桶；per-voice 分桶后每桶 < 10 样本

---

## 14. 2026-05-07 增补：Gap C closed — voice_id 60% unknown 分类率修复

> 关联：P1 Done note §5.2 Gap C / §4-bis.5。

### 14.1 问题与定位

P1 Done note §4-bis.5 揭示 38 jobs 里 **23/38 (60.5%)** 至少含 1 个 unknown 分类的 voice_id，触发 P1 simulator `voice_selection_diff` / `clone_policy` 多数 INCONCLUSIVE，无法对这些 production 数据做有意义的 shadow 比较。

根因：`scripts/smart_shadow_eval_collector.py::_classify_voice_id` v1 只识别三种克隆模式（`vt_*` / `moss_audio_*` / UUID）+ 显式 `preset_*`，不覆盖 production 实际使用的多 provider preset 命名约定。

### 14.2 数据驱动的 sub-pattern 提取（n=38，2026-05-07）

从 2026-05-06T22-42Z 远端 P0 全量 facts.jsonl 中抽取 41 个 unknown voice_id 实例，按 ≥3 实例阈值聚类（task 硬约束："至少 3 个同模式实例才纳入,不要主观猜"）：

| Pattern | 描述 | Instances | Unique IDs | Catalog provenance | 决策 |
|---|---|---:|---:|---|---|
| **A** `Chinese (Mandarin)_*` | MiniMax 自然描述名 | **10** | 5 | `src/services/tts/minimax_voice_catalog_604.json` 5/5 命中 | ✅ → preset |
| **B** `Chinese_*_vv1` / `Chinese_*_vv2` | MiniMax 系统名 | **24** | 7 | minimax_voice_catalog_604.json 7/7 命中 | ✅ → preset |
| **C** `*_v3` 后缀 | CosyVoice 3.0 | **3** | 3 | `cosyvoice_voice_catalog.py` 66/68 voices 该后缀 | ✅ → preset |
| **D** `*_bigtts` 后缀 | VolcEngine 豆包 | 1 | 1 | volcengine_voice_catalog.py 命名约定 | ❌ < 3 不纳入 |
| **E** `Wise_Woman` 字面 | 不在 MiniMax 604 catalog | 3 | 1 | 无 catalog 来源 | ❌ literal 不算 pattern + 无 catalog 证据 |

合计 41 instances；A+B+C 共 37 → preset；D+E 共 4 保留 unknown。

`Wise_Woman` 单字面在 production 出现 3 次但**不在** MiniMax 604 catalog（也非 vt_/moss_audio_/UUID/preset_/Chinese_/* 任一已知约定），可能是历史/废弃 preset ID。当前数据不足以建立"以下划线分隔的 PascalCase 英文词组 = preset"这种过宽规则；surface 为 unknown 让 catalog 维护方面后续确认。

### 14.3 实施

| 项 | 内容 |
|---|---|
| 改动文件 | `scripts/smart_shadow_eval_collector.py::_classify_voice_id`（添加 3 条 preset 规则）+ `tests/test_smart_shadow_eval_collector.py`（5 个新测试） |
| 测试 | 113 passed, 1 skipped（smart_shadow 全套）；`classify_voice_id` 子集从 4 涨到 9 个测试 |
| 守卫 | AST stdlib-only / PII / paths-in-sync 三组守卫全绿（无新增依赖、无 PII 字面量、ARTIFACT_PATHS 未改） |
| 顺序约束 | cloned 检查（`vt_*` / `moss_audio_*` / UUID）仍在 preset 检查之前。一个假想的 `vt_anything_v3` 仍归 cloned，不会被 `*_v3` 抢走 |

### 14.4 远端验证（同一 38-job 集合 reclassify）

- **run_id**：`gap-c-reclassify-20260507T0037Z`
- **artifacts**：`D:/Claude/temp/smart_shadow_sim/gap_c/gap-c-reclassify-20260507T0037Z/p0/{facts,inventory,summary}.json{,l}`
- **方式**：collector-only（task 约束："只跑 collector,不动 simulator/aggregator"），read-only，0 字节 project_dir 原始内容触本地

| 指标 | before (v1) | **after (v2)** |
|---|---|---|
| **unknown jobs / 38** | **23 (60.5%)** | **4 (10.5%)** |
| voice_id instances cloned | 41 | 41（不变 ✓） |
| voice_id instances preset | 0 | **37**（新增） |
| voice_id instances unknown | 41 | **4** |
| invariant `cloned + preset + unknown == voice_ids` | OK | **OK** ✓ |

剩余 4 instances unknown：3× `Wise_Woman` + 1× `zh_male_liufei_uranus_bigtts`，分布在 4 个不同 jobs（即 §14.2 表格 D+E 行的预期分布）。

### 14.5 对 P1 simulator / aggregator 的级联影响（待 P1 闭环重跑后确认）

**直接预期**（基于 reclassified facts.jsonl，未实跑 simulator）：
- `voice_selection_diff.studio_unknown_voices` 应从 23 → 4
- `voice_selection_diff.smart_studio_match` 应在 14 + (新 preset 桶里 smart 也匹配 preset 的 jobs) 之间增长
- `clone_policy._classify_diff` 不再因 unknown 大量降级 `no_studio_signal`，可以给出更多有效 diff_kind

但本次 task 硬约束是 **"只跑 collector,不动 simulator/aggregator"**，所以 simulator 端真实 diff 数字留给下一次 P1 闭环重跑（数据累积期内自然触发，不再单独排队）。

### 14.6 不顺手做的事项（task 硬约束守住）

- ❌ 没动 `src/` `gateway/` `frontend-next/`
- ❌ 没创 worktree / 新分支（直接 main 提交）
- ❌ 没碰付费 API（远端只 SSH read）
- ❌ 没顺手做 v3 retry estimation / P2-alpha / collector 重构
- ❌ 没扩展超出 ≥3 实例阈值的 pattern（D+E 保留 unknown，等数据再说）

### 14.7 Gap C verdict

**CLOSED.** unknown jobs 60.5% → 10.5%（5.7× 改善），低于 task 设定的 <20% 阈值。继续阻 P2 的是 P0 §7.1 的 metered jobs ≥ 20 数据累积门槛 + §13.4 cost p90/p99 stable + production pricing snapshot 写入，与 voice_id 分类完全解耦。

---

## 15. 2026-05-10 增补：post_phase_metered_jobs gate 定义修正 + §11 verdict 转 PASS

### 15.1 问题：旧 gate 把 Phase D 当 Smart 信号源

P0 §7.1 的 P2 入口阈值（≥10 重跑、≥20 启动）依赖 `aggregate_report.json::p2_readiness_signals.post_phase_metered_jobs`。**旧 gate 定义错了**：它要求
- `retts_actual.data_source == "metering"`（metering 存在）
- AND `sub_actual.alignment_model` truthy（Whisper 数据存在）

但 Smart 6 个 stage 决策（eligibility / voice / clone / translation / retry / subtitle_sync）实际依赖的是 **Phase A/B**（`subtitle_quality_report.json::text_audio_drift_count` 字段是否 set），不是 Phase D Whisper alignment。生产 `admin whisper_alignment_trigger="deliverable"` 是 by-design 的合理 trade-off（精准字幕只在用户下载剪映草稿/素材包时跑，节省 publish 5-15s 路径成本）——用户已确认**不改 production**，问题完全在 collector/aggregator gate 定义。

### 15.2 修复（commit `193c100`）

- 文件：`scripts/smart_shadow_sim_aggregator.py::_p2_readiness_signals`
- 旧 check：`if not sub_actual.get("alignment_model"): continue`
- 新 check：`if sub_actual.get("drift_count") is None: continue`（注意：`is None`，因为 `drift_count == 0` 表示 Phase A/B 跑过且 healthy，必须计入）
- `alignment_model` 字段在 fact sheet + simulator/aggregator sidecar **保留**作为 informational signal，不再参与 gate
- 新增单测 `test_c4_p2_readiness_drift_only_counts_as_post_phase`：验证 alignment_model=None 但 drift_count 非 None 的 job 必须计为 post-phase
- 全测试：`smart_shadow` 套件 113 → 114 passed, 1 skipped（0 回归）
- 守卫 AST stdlib-only / PII / paths-in-sync 三组全 GREEN

### 15.3 远端实测（2026-05-10T04:43Z，n=39 production jobs）

run_id `gate-fix-20260510T0443Z`，用 `D:/Claude/temp/smart_shadow_sim/c7_remote_run/run_remote_gate_fix.py` 一次性 paramiko orchestration（read-only，只跑 collector + simulator + aggregator + analyzer，远端 `/tmp/<run_id>` 跑完即清，**0 字节 project_dir 原始内容触本地**）。

| Gate 定义 | 满足 jobs | P0 重跑阈值 ≥10 |
|---|---:|---|
| 旧（metering AND alignment_model not None） | **9/39** | ❌ 还差 1 |
| **新（metering AND drift_count is not None）** | **12/39** | **✅ 已达成** |

数据底层（从 facts.jsonl 直读）：
- 21/39 jobs 有 metering（usage_events.jsonl）
- 12/39 jobs 有 `text_audio_drift_count` 设值（subtitle_quality_report.json，Phase A/B 已部署）
- 9/39 jobs 有 `whisper.alignment_model` 设值（Phase D 已跑 = deliverable 阶段已触发）
- **3 个新增 jobs**（drift 设值但 alignment_model 是 None）：正是被旧 gate 误漏的 Phase A/B-only jobs

### 15.4 §11 verdict：INCONCLUSIVE → PASS（同次远端 run）

P0 §4.4 当时 n=38 jobs 上 `verdict: INCONCLUSIVE (metering data < 50%)`（35/38 无 metering）。本次 n=39 上跑 `smart_shadow_eval_analyzer.py`，用 gateway/pricing_schema.py code defaults（production 仍无 `pricing_runtime.json`）：

| 指标 | 实测（n=39） |
|---|---|
| jobs with metering data | **21/39 (54%)** |
| metering 50% 门槛 | ≥ 50% → 不 INCONCLUSIVE |
| margin (RMB) p50 | **55.37** |
| margin (RMB) p90 | **282.89** |
| margin (RMB) p99 | **350.59** |
| **§11 Risk Verdict** | **PASS**（三档都正毛利，21/39 metering ≥ 50%） |

> 注意：**§11 PASS 是数据自然累积穿过 50% metering 阈值导致**（21/39=54% 已 > 50%），不是本次 gate fix 的直接产物。Gate fix 只直接影响 aggregator `post_phase_metered_jobs` 计数。两件事在同一次远端 run 里同时落定，但因果独立。

### 15.5 对 §13.4 P2 入口条件的影响

§13.4 列出 3 条 P2 入口前必须满足条件：
- ~~post-Phase-D metered jobs ≥ 20~~ → 改为 **post-Phase-A/B（drift_count is not None）metered jobs ≥ 20**；当前 **12/20**（60% 进度）
- ~~cost p90/p99 stable~~ → **本次 PASS，但仅为单次 snapshot；P2 启动前应在累积到 ≥ 20 metered jobs 时再次复测**
- production `pricing_runtime.json` snapshot 写入 → **未改变，仍待 ops 写入**

### 15.6 不顺手做的事项（task 硬约束守住）

- ❌ 不动 production `admin_settings.json::whisper_alignment_trigger`（deliverable 是 by-design）
- ❌ 不重构 simulator 6 stage decision logic（`_decide_subtitle_sync` 仍按 alignment_model 给 smart 决策，与 gate 解耦）
- ❌ 不做 v3 retry estimation per-voice k 校准
- ❌ 不删 `alignment_model` 字段（保留作为 informational signal）
- ❌ 不修复 production whisper trigger
- ❌ 不动 `src/` `gateway/` `frontend-next/`
- ❌ 远端只读，不写入任何 production 状态

### 15.7 仍阻 P2 launch 的剩余条件

1. **post_phase_metered_jobs ≥ 20**（P2 启动门槛，当前 12/20）
2. **production `pricing_runtime.json` snapshot 写入**（替代 code defaults）
3. P0 §8 4 个 known diagnostic gaps 跟踪（`workflow_alignment_cache` 0 命中 / `orphaned_project_dir_count` / pricing snapshot / audit 命中率）

P0 §7.1 的 ≥10 重跑门槛已达成（12 ≥ 10），`§11 verdict` 也已从 INCONCLUSIVE 转 PASS。从 P0 视角看，P2 评估窗口已开，但 P2 实施仍受 ≥20 metered + production pricing 阻塞。

### 15.8 工件归档

- 远端 run_id：`gate-fix-20260510T0443Z`
- Tarball：`D:/Claude/temp/smart_shadow_sim/c7_remote_run/results_gate_fix/sidecars.tar.gz`（86 KB）
- 解压后：
  - `p0/facts.jsonl`（39 facts）
  - `p1/aggregate_report.{json,md}` — 含 `post_phase_metered_jobs: 12`, `ready_for_p2_rerun: true`
  - `p1/job_<id>/smart_shadow_decisions.jsonl` × 39
  - `p1/job_<id>/smart_shadow_report.json` × 39
  - `analyzer_out/report.{md,_summary.json}` — 含 §11 Risk Verdict: PASS
