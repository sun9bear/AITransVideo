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
