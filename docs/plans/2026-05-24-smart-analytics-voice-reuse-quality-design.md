# Smart analytics — voice auto-reuse 质量指标（Task #26 短设计）

- 创建日期：2026-05-24
- 状态：设计草稿，待 codex / 用户 review
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

| 命中类型 | 识别规则 | 已有数据吗 |
|---|---|---|
| **strong same-source** | `reason_code=reused_user_voice` AND `metrics.match_confidence in ("strong", null)` | ✅ |
| **strong_named** | `reason_code=reused_user_voice` AND `metrics.match_confidence="strong_named"` | ✅（从 commit 598e137 起） |
| **possible auto-reused** | `reason_code="possible_user_voice_match_auto_reused"` | ✅（从 commit 4df2b92 起） |

每条命中记录都有 `speaker_id` + `cloned_voice_id`（被复用的 voice）。

### 2.2 改音色事件 — 来自 `audit/user_edit_events.jsonl`

| event_type | 含义 | 算"改音色"吗 |
|---|---|---|
| `post_edit_voice_override_changed` | 后编辑改音色 override | ✅ **核心信号** |
| `voice_selection_speaker_reassigned` | 审核阶段改音色指派 | ✅（早期信号） |
| `voice_selection_dubbing_mode_changed` | keep_original / mute 切换 | ⚠️ 边界 — 改成"不配音"也算"否定 auto-reuse"，但语义不是"换音色" |
| `post_edit_tts_regenerated` | 重生成 TTS 但不改音色 | ❌（音色保持） |
| `post_edit_segment_speaker_changed` | 改段落归属的说话人 | ❌（不是改 voice） |

**判断"该 speaker 被改音色"**：该 speaker_id 至少出现在
`post_edit_voice_override_changed` OR `voice_selection_speaker_reassigned`
的 payload 里。`voice_selection_dubbing_mode_changed` 起 v1 不算
（避免把 keep_original 用户当 "auto-reuse 错"），只在 sample
job 表里展示一个边注。

## 3. 指标定义

### 3.1 主指标（4 个，speaker 级）

| 指标 | 分子 | 分母 |
|---|---|---|
| `strong_change_rate` | speakers where (命中 strong) AND (改音色) | 命中 strong 的 speaker 总数 |
| `strong_named_change_rate` | speakers where (命中 strong_named) AND (改音色) | 命中 strong_named 的 speaker 总数 |
| `possible_auto_change_rate` | speakers where (命中 possible_auto) AND (改音色) | 命中 possible_auto 的 speaker 总数 |
| `auto_reuse_overall_change_rate` | 上面三档分子之和 | 三档分母之和 |

**说明**：
- "speaker" 指 `(job_id, speaker_id)` 唯一对，不去重跨 job
- 用户改了又改回原值 → 仍计 1 次"改"（这是用户表达"auto-reuse
  选错"的信号，即使最终回到原值）
- v1 不细分"改成 preset" vs "改成 clone" — 等 1-2 个月数据再做

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

## 6. 验收标准

实现完成时必须满足：

- [ ] 指标按 §3.1 / §3.2 公式计算，与手算样本一致（用 2-3 个真实 job
      做 ground truth）
- [ ] 阈值在 UI 上可见，颜色随实际值变（< 30% 默认色，≥ 30% ochre，
      ≥ 50% cinnabar）
- [ ] 当前 smart-analytics 现有 3 Tab 行为不变（回归测试）
- [ ] `voice_selection_dubbing_mode_changed` v1 不进分子（在测试里 pin）
- [ ] CSV 导出加上 4 列：`strong_change_rate` /
      `strong_named_change_rate` / `possible_auto_change_rate` /
      `voice_changed_by_user`（per job 的 speaker 改音色数量）
- [ ] 案例表格按 created_at desc 排序，最多 20 行
- [ ] 没有真实命中数据时（分母=0）显示「—」而不是 0% 或 NaN

## 7. 范围外（明确不做）

- 自动调参（看到 strong_named_change_rate>30% 自动收紧）——
  v1 只展示，决策由人做
- 区分"改成 preset" vs "改成 clone"——
  等数据量到 50+ 改动后再细分
- 实时告警 / 邮件通知——
  本看板是周期性 admin 自查
- 阈值校准（30% 是 rebaseline 写的拍脑袋数，需要数据反验）

## 8. 工作量估算

- 后端 + tests：4-6 小时
- 前端 Tab + 案例表格：3-4 小时
- 部署 + 验证：1 小时
- **总计 ≈ 1 天**（codex 估的 1-2 天上限）

## 9. 开工前要 codex / 用户确认

1. ✅/❌ 接受 §3 指标定义（4 主指标 + 2 衍生 + 不含 dubbing_mode）
2. ✅/❌ 接受 §4 推荐方案 A（新增 Tab 4）
3. ✅/❌ 接受 §6 验收标准（手算 ground truth + 阈值上色 +
   CSV 加列 + 边界行为 pin）
4. 其他建议
