# Smart analytics — voice auto-reuse 质量指标（Task #26 短设计）

- 创建日期：2026-05-24
- v2 更新：2026-05-24 晚（吸收 codex 第二轮 review + 自查发现 audit 字段丢失 bug）
- 状态：设计草稿，待 codex / 用户 review v2
- 关联：
  - `docs/plans/2026-05-24-smart-auto-pipeline-rebaseline.md` §6.3（阈值）
  - `docs/plans/2026-05-22-smart-analytics-v1.md`（v1 看板基线）

## 1. 目标

P5 已经上线了三档 voice auto-reuse 策略：

| 决策 | reason_code | 入口 |
|---|---|---|
| Strong same-source REUSED | `reused_user_voice` + `metrics.match_confidence=strong` | P2 baseline |
| Strong cross-source named REUSED | `reused_user_voice` + `metrics.match_confidence=strong_named` | Task strong_named (commit 598e137) |
| Possible-match auto-reused | `possible_user_voice_match_auto_reused` | Phase 5 (commit 4df2b92) |

**问题**：auto-reuse 选错音色用户会自己改回去，但我们目前看不到这件事 →
不知道当前阈值是否合理 → 不知道该收紧还是放松。

**本设计目标**：在 admin smart-analytics 看板加一个新 Tab，回答两个问题：

1. 各档 auto-reuse 命中后，用户改音色的比例是多少？
2. 哪些任务被 auto-reuse 选错了？（看具体案例评估校准方向）

## 2. 数据契约

### 2.1 命中事件 — 来自 `audit/smart_decisions.jsonl`

**事实校准（2026-05-24 v2 自查发现 audit 字段丢失 bug）**：

当前 `src/pipeline/process.py::_apply_smart_reused_voice_decision`
**对所有 REUSED 决策都硬编码** `reason_code="reused_user_voice"`，
忽略 `decision.reason_code`。同时 `evidence` 只写 `match_confidence`
/ `match_reason` / `matched_user_voice_id`，**丢失** Phase 5 的
`auto_reused_from_possible_match=True` 标志、`top_candidate_confidence`、
`possible_match_count` 等关键区分字段。

结果：生产 audit 里所有三档 auto-reuse 决策**长得一模一样**，分不出来。

| 命中类型 | 期望识别（修后） | 当前 audit 状态 |
|---|---|---|
| **strong same-source** | `reason_code=reused_user_voice` AND `evidence.match_confidence=strong` | ✅ 但 strong 可能写成 null（legacy bucket） |
| **strong_named** | `reason_code=reused_user_voice` AND `evidence.match_confidence=strong_named` | ⚠️ confidence 已传到 metrics，但 evidence 只读 `metrics.get("match_confidence")` —— 应 work |
| **possible auto-reused** | `reason_code=possible_user_voice_match_auto_reused` AND `evidence.auto_reused_from_possible_match=true` | ❌ **当前丢失** — 被错误归类为 `reused_user_voice` |

**前置工作（必须 Task #26 实施前完成）**：

修 `src/pipeline/process.py::_apply_smart_reused_voice_decision`：
1. `reason_code=decision.reason_code` 而不是硬编码 `"reused_user_voice"`
2. evidence 增加：
   ```python
   evidence={
       ...既有字段...,
       "auto_reused_from_possible_match": decision.metrics.get("auto_reused_from_possible_match", False),
       "top_candidate_confidence": decision.metrics.get("top_candidate_confidence"),
       "possible_match_count": decision.metrics.get("possible_match_count"),
   }
   ```
3. 回归测试：3 档 REUSED 决策 → 3 种 audit record 区分清晰

**读取规则（codex 第二轮 review 第 1 点）**：

| 字段路径 | 优先级 | 说明 |
|---|---|---|
| `record.evidence.match_confidence` | **首选** | 这是磁盘上 JSONL 的实际字段 |
| `record.metrics.match_confidence` | fallback | 只为兼容 legacy / 测试 fixture（实际不存在于生产 JSONL） |

**legacy / null confidence 命名（codex 第二轮 review 第 4 点）**：

历史决策可能 `evidence.match_confidence=null`（早期代码未传该字段）。
归入新桶 `strong_or_legacy`，**不**默认等同于 strong：

| Bucket key | 识别 |
|---|---|
| `strong` | `evidence.match_confidence == "strong"` |
| `strong_named` | `evidence.match_confidence == "strong_named"` |
| `strong_or_legacy_null` | `evidence.match_confidence in (None, "")` AND `reason_code=reused_user_voice` |
| `possible_auto` | `reason_code=possible_user_voice_match_auto_reused` AND `evidence.auto_reused_from_possible_match=true` |

每条命中记录都有 `speaker_id`（在 audit record 顶层）+
`cloned_voice_id`（在 `evidence.voice_id`）。

### 2.2 改音色事件 — 来自 `audit/user_edit_events.jsonl`

| event_type | payload 关键字段 | v1 主分子 | v1 辅助 |
|---|---|---|---|
| `post_edit_voice_override_changed` | `segment.segment_id` + `before/after.voice_id`（**无 speaker_id**） | ✅ | — |
| `voice_selection_speaker_reassigned` | `segment_id` + speaker change | ❌ codex 修订：不进主分子 | ✅ 单独"早期归属修正"指标 |
| `voice_selection_dubbing_mode_changed` | mode change | ❌ v1 排除 | 仅 case 表边注 |
| `post_edit_tts_regenerated` | TTS 重生成（音色不变）| ❌ | ❌ |
| `post_edit_segment_speaker_changed` | 改段落 speaker 归属 | ❌ | ❌ |

**codex 第二轮 review 第 3 点**：`voice_selection_speaker_reassigned`
更像"说话人归属修正"（S2 / voice_selection 阶段的 reassign），不是
用户对 auto-reuse 音色选择的否定。把它放进主分子会让
`strong_named_change_rate` / `possible_auto_change_rate` 被
speaker segmentation 问题污染。

**v1 主分子**：仅 `post_edit_voice_override_changed`。
**v1 辅助指标**：`speaker_reassigned_rate` 单独展示（不与三档命中
率混算）。

### 2.3 Segment_id → speaker_id 映射（codex 第二轮 review 第 2 点）

`post_edit_voice_override_changed` 事件的 payload 只有
`segment.segment_id`，没有 `speaker_id`。需要外部映射。

**映射来源（按优先级）**：

1. `{project_dir}/editor/baseline/segments.json` — post-edit 是
   editor/ 路径，baseline 保留进入 editing 时的 segment→speaker 关系
2. `{project_dir}/editor/editing/segments.json` — 如果 baseline 不存在
   或 segment_id 缺失，退到 editing 当前快照
3. `{project_dir}/transcript/segments.json` — 最早期 segment 表
   （可能没有 editor/ 目录的 legacy job）

**unmapped 策略**：

- 所有源都查不到 segment_id → 该事件归入 `unmapped_segment` 桶
  （不进任何 speaker 的分子）
- 在 admin UI 的 Tab 4 顶部显示一个 `unmapped_segment_count`
  metric，>5% 时变 ochre 色（提示数据契约可能漂移）
- 测试 fixture 必须覆盖：a) 映射成功 b) 仅 baseline 命中
  c) 全部 unmapped → unmapped_segment_count 正确累加

## 3. 指标定义

### 3.1 主指标（4+1，speaker 级）

| 指标 | 分子（仅 `post_edit_voice_override_changed`，去重 speaker） | 分母 |
|---|---|---|
| `strong_change_rate` | speakers where (bucket=strong) AND (该 speaker 有任一 segment 被 override) | 命中 bucket=strong 的 speaker 总数 |
| `strong_named_change_rate` | 同上但 bucket=strong_named | 命中 bucket=strong_named 的 speaker 总数 |
| `possible_auto_change_rate` | 同上但 bucket=possible_auto | 命中 bucket=possible_auto 的 speaker 总数 |
| `strong_or_legacy_null_change_rate` | 同上但 bucket=strong_or_legacy_null | 命中 bucket=strong_or_legacy_null 的 speaker 总数 |
| `auto_reuse_overall_change_rate` | 四桶分子之和 | 四桶分母之和 |

**关键说明（v2 修订）**：
- "speaker" 指 `(job_id, speaker_id)` 唯一对，不去重跨 job
- 分子**只**用 `post_edit_voice_override_changed`（per codex 修订）；
  `voice_selection_speaker_reassigned` 进 §3.4 辅助指标
- `post_edit_voice_override_changed` 通过 §2.3 映射拿到 speaker_id；
  unmapped 不进任何分子（也不进 unmapped 桶的分母，单独 `unmapped_segment_count`）
- 用户改了又改回原值 → 仍计 1 次"改"
- v1 不细分"改成 preset" vs "改成 clone" — 等 1-2 个月数据再做

### 3.4 辅助指标（单独展示，不进主率）

| 指标 | 公式 | 说明 |
|---|---|---|
| `speaker_reassigned_rate` | speakers with `voice_selection_speaker_reassigned` / 全部 smart 命中 speaker | S2/voice_selection 阶段的归属修正，独立于音色满意度 |
| `unmapped_segment_count` | `post_edit_voice_override_changed` 事件 segment_id 找不到对应 speaker 的总数 | 监控数据契约是否漂移 |

### 3.2 衍生指标（2 个，job 级）

- `auto_reuse_jobs_entering_edit_rate`: smart job 中含至少一个
  auto-reuse 命中 AND 之后 `entered_editing` 的比例
- `auto_reuse_jobs_with_voice_change_rate`: 同上但要求至少一个
  speaker 被改音色

### 3.3 触发阈值（写进文档供 admin 参考，不做自动报警）

来自 rebaseline §6.3：

- `strong_named_change_rate > 30%` → 收紧 strong_named（提高 score
  阈值 / 要求更强的来源一致性）
- `possible_auto_change_rate > 30%` → 关闭默认 auto-reuse
  （setting `smart_auto_reuse_on_possible_user_voice_match=false`），
  退回 Phase 4 pause/confirm

## 4. UI 位置

**选项对比**：

| 方案 | 优点 | 缺点 |
|---|---|---|
| A. 新增 Tab 4「自动复用质量」 | 独立视角清晰；不挤压现有 Tab | 多一个 Tab，移动设备 wrap |
| B. 加进 Tab 3「用户返工」 | 概念相关（都是反映用户行为）| 概念混淆 — 改音色 ≠ 返工率 |
| C. KPI 卡片加 4 张 | 顶部就能扫到 | 4 张已经满了，再加变 8 张挤 |

**推荐 A**。Tab 4 内容：

```
┌─────────────────────────────────────────────────────────┐
│ [Handoff 分布] [对齐质量] [用户返工] [自动复用质量]  ← 新 Tab │
│                                                           │
│ 总体改音色率：N%（M / K 个 speaker）                       │
│ ─────────────────────────────────────────                │
│ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐      │
│ │ Strong 同源  │ │ Strong_named │ │ Possible     │      │
│ │ 改音色率: N% │ │ 改音色率: N% │ │ 改音色率: N% │      │
│ │ 命中 K 次    │ │ 命中 K 次    │ │ 命中 K 次    │      │
│ │ [阈值: 30%]  │ │ [阈值: 30%]  │ │ [阈值: 30%]  │      │
│ └──────────────┘ └──────────────┘ └──────────────┘      │
│                                                           │
│ 改音色的 speaker 案例（最多 20 个，按时间倒序）             │
│ ─────────────────────────────────────────                │
│ task | speaker | 命中档 | 复用 voice → 改成 voice | 时间 │
│ ...                                                       │
└─────────────────────────────────────────────────────────┘
```

阈值用 cinnabar/ochre 颜色标注，超过即变色。

## 5. 后端实现范围（不含本设计）

新加在 `gateway/admin_smart_analytics_api.py`：

1. `_classify_voice_decision(record)` — strong / strong_named / possible_auto / other
2. `_collect_voice_changed_speakers_per_job(events_path)` — 返回 set[speaker_id]
3. `_aggregate_voice_reuse_quality(metrics)` — 跨任务聚合 4 主 + 2 衍生指标
4. `_build_summary_payload` 加 `voice_reuse_quality` 字段
5. 测试覆盖：3 档命中 × 改/未改 = 6 个核心 case + 阈值边界 2 个

新加在 `frontend-next/src/app/(app)/admin/smart-analytics/page.tsx`：

1. 新 Tab "自动复用质量"
2. 3 张档位卡 + 总体卡
3. case 表格（job_id / speaker_id / 命中档 / before-voice / after-voice / 时间）

## 6. 验收标准（v2 修订）

### 6.0 前置（开工前必须落地）

- [ ] **修 `_apply_smart_reused_voice_decision`**：reason_code 不
      硬编码、evidence 增加 `auto_reused_from_possible_match` /
      `top_candidate_confidence` / `possible_match_count` 字段
      （§2.1 前置工作）。
- [ ] 回归测试：3 档 REUSED 决策 emit → 3 种 audit record 可区分。

### 6.1 实现完成时必须满足

- [ ] 指标按 §3.1 + §3.4 公式计算，与手算样本一致（**至少**用 2-3
      个真实 job 做 ground truth）
- [ ] 阈值在 UI 上可见，颜色随实际值变（< 30% 默认色，≥ 30% ochre，
      ≥ 50% cinnabar）
- [ ] 当前 smart-analytics 现有 3 Tab 行为不变（回归测试）
- [ ] 分子**仅含** `post_edit_voice_override_changed`（在测试里
      pin —— `voice_selection_speaker_reassigned` 进辅助指标，不
      混进主率）
- [ ] `voice_selection_dubbing_mode_changed` v1 不进分子（在测试里 pin）
- [ ] CSV 导出加列（**per job 输出 counts，不输出全局 rates** —
      避免把全局 rate 错误地复制到每行）：
      `strong_hits` / `strong_named_hits` / `possible_auto_hits` /
      `strong_or_legacy_null_hits` / `voice_changed_speakers` /
      `unmapped_segment_count`
- [ ] 案例表格按 created_at desc 排序，最多 20 行
- [ ] 没有真实命中数据时（分母=0）显示「—」而不是 0% 或 NaN
- [ ] `unmapped_segment_count` 在 UI 顶部展示，> 5% 时 ochre

### 6.2 测试 fixture 覆盖（codex 第二轮 review 第 6 点）

合成 fixture 必须覆盖以下场景，每条至少 1 case：

| Fixture 场景 | 目的 |
|---|---|
| `evidence.match_confidence="strong_named"` | 主路径 |
| `evidence.match_confidence` 缺失 → legacy null 桶 | 不被误归入 strong |
| `evidence.auto_reused_from_possible_match=true` + reason_code | Phase 5 命中 |
| `metrics.match_confidence="strong"` (evidence 缺失) | fallback 路径 |
| `post_edit_voice_override_changed.segment_id` 映射成功 | 主分子 |
| segment_id 仅 baseline 命中 | 多级映射回退 |
| segment_id 全部 unmapped | unmapped_segment_count 累加 |
| 有 `voice_selection_speaker_reassigned` | 不进主分子 |
| 有 `voice_selection_dubbing_mode_changed` | 不进任何分子 |
| 分母为 0（无命中） | 显示「—」不 NaN |

## 7. 范围外（明确不做）

- 自动调参（看到 strong_named_change_rate>30% 自动收紧）——
  v1 只展示，决策由人做
- 区分"改成 preset" vs "改成 clone"——
  等数据量到 50+ 改动后再细分
- 实时告警 / 邮件通知——
  本看板是周期性 admin 自查
- 阈值校准（30% 是 rebaseline 写的拍脑袋数，需要数据反验）

## 8. 工作量估算（v2 上调）

- **前置 audit 修复 + 测试**：2-3 小时（不在 Task #26 原估算内）
- 后端 + tests（含 segment 映射 + 4 桶 + 辅助指标 + fixtures）：6-8 小时
- 前端 Tab + 案例表格 + unmapped 提示：3-4 小时
- 部署 + 验证（gateway + next + 手算 ground truth 比对）：2 小时
- **总计 ≈ 1.5-2 天**（v1 的"1 天"估算偏乐观；codex 给的 1-2 天上限范围内）

## 9. 开工前要 codex / 用户确认（v2）

### 9.0 前置 bug 修复

1. ✅/❌ 接受 §2.1 §6.0 提到的 audit 字段补全（修
   `_apply_smart_reused_voice_decision`） — **这是 Task #26 数据
   契约的必要前置**，否则 v1 的 audit 数据连三档都分不出来

### 9.1 指标契约

2. ✅/❌ 接受 §2.1 桶定义：`strong` / `strong_named` /
   `possible_auto` / `strong_or_legacy_null` 4 个桶
3. ✅/❌ 接受 §2.2 主分子**只**用 `post_edit_voice_override_changed`，
   `voice_selection_speaker_reassigned` 进 §3.4 辅助指标
4. ✅/❌ 接受 §2.3 segment_id→speaker_id 映射策略 + unmapped 兜底
5. ✅/❌ 接受 §3.1 改成 4+1 桶（含 legacy null）+ §3.4 辅助指标分离

### 9.2 UI / CSV / 验收

6. ✅/❌ 接受 §4 方案 A（新增 Tab 4）
7. ✅/❌ 接受 §6.1 CSV 输出 counts（不输出 per-job rate）
8. ✅/❌ 接受 §6.2 测试 fixture 清单
9. 其他建议
