# 智能版自动视频翻译流程执行基线

- 创建日期：2026-05-24
- 适用范围：`docs/plans/2026-05-04-smart-auto-pipeline-plan.md` 的执行状态重基线、路线调整和后续推进依据
- 状态：当前执行基线
- 关联文档：
  - `docs/plans/2026-05-04-smart-auto-pipeline-plan.md`
  - `docs/plans/2026-05-13-smart-mvp-p2-implementation-plan.md`
  - `docs/plans/2026-05-15-smart-mvp-p3-decisions.md`
  - `docs/plans/2026-05-22-smart-analytics-v1.md`
  - `docs/audits/2026-05-24-smart-auto-pipeline-audit.md`
  - `docs/graphs/GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md`

## 1. 结论

截至 2026-05-24，Smart Auto Pipeline 已从原始方案进入实装阶段，但不能简单标为全部完成。

当前基线：

| 阶段 | 状态 | 说明 |
|---|---|---|
| P0 离线评估 | DONE | shadow evaluator / P0 results 已形成进入后续阶段的依据。 |
| P1 shadow simulator | DONE | 模拟器、聚合器和 P1 done note 已完成。 |
| P2 Smart MVP | IN_PROGRESS | 主体代码、Gateway 入口、pipeline 集成、sidecar 和测试已大规模落地，但仍有 launch blocker。 |
| P3 multimodal verifier | DEFERRED | 不按原方案直接做完整独立 verifier；先做 shadow / only-report 试点。 |
| P4 verifier 自动接入 | DEFERRED | 不推进无人确认的 verifier -> auto re-TTS 路径。 |
| P5 规模化优化 | PARTIAL STARTED | Smart analytics v1、possible-match auto-reuse、strong_named 等已开始落地。 |

原则上，后续开发应以本文件作为当前路线依据；2026-05-04 主纲保留为历史设计基线。

## 2. 已完成事实

### 2.1 Gateway / 产品入口

- `service_mode="smart"` 已进入 Gateway 创建链路、plan catalog、entitlements 和 job policy。
- Smart job 创建时会校验 `smart_consent` 6 字段 payload。
- Smart policy 已映射到 MiniMax HD、`requires_review=True`、`voice_clone_enabled=True`、`voice_strategy="smart_auto"`、`quality_tier="standard"`。
- Plus / Pro 当前已包含 `smart` allowed service mode。

### 2.2 Frontend

- 提交页已存在 Smart 卡片入口。
- 前端在提交 Smart job 时自动携带完整 `smart_consent` payload。
- Workspace 已存在 Smart quality report / auto decision 面板。
- Admin 已存在 Smart analytics 和单任务 Smart cost view。

注意：当前 consent 体验是“选择 Smart 卡片即按默认策略提交”，不是独立弹窗式逐项确认。

### 2.3 Pipeline / Smart 模块

已落地的 Smart 模块包括：

- `eligibility_gate.py`
- `auto_voice_review.py`
- `auto_translation_review.py`
- `retry_budget.py`
- `sidecar_emitter.py`
- `quality_report_synthesizer.py`
- `state.py`
- `handoff.py`
- `contracts.py`

主 pipeline 已接入 Smart inline 分支、eligibility gate、voice review、translation review、Smart handoff marker、terminal marker、quality report 和 cost summary 写入。

### 2.4 Voice reuse / clone

已完成并超出原始主纲的能力：

- 同源 strong match 自动复用。
- 跨视频同名唯一 strong_named 自动复用。
- NULL-hash 历史音色参与匹配。
- possible-match top candidate 可按后台策略自动复用。
- MiniMax 配额、余额不足、`1008` / `余额不足` 等 provider exhaustion 类错误进入可审计处理。

### 2.5 Sidecar / 报告 / 分析

已落地三件套：

- `audit/smart_decisions.jsonl`
- `audit/smart_quality_report.json`
- `audit/smart_cost_summary.json`

用户侧质量报告不展示成本；成本和聚合分析进入 admin-only 视图。

### 2.6 Post-edit / Jianying

- Smart succeeded 且 `smart_state` 为可编辑状态时，允许进入 post-edit。
- Jianying draft 生成入口已允许 Smart job，但仍依赖 Smart state gate。

## 3. Launch Blockers / 未完成项

### 3.1 Smart kill switch / 灰度门禁

原方案和 P2 实施计划要求 `AVT_ENABLE_SMART_MODE=false` 默认关闭，并配合 admin runtime toggle / allowlist 控制灰度。

当前代码事实：

- `AVT_ENABLE_SMART_MODE` 只在文档中出现，代码中没有真实 gate。
- `smart_mode_enabled`、`user.smart_enabled`、`get_effective_allowed_service_modes()` 等多层灰度机制仍停留在实施计划文本中。
- Plus / Pro 当前直接暴露 Smart entitlement。

结论：这是 P2 launch blocker。上线前必须实现真实 kill switch，或更新产品策略明确 Smart 已不再按灰度默认关闭。

### 3.2 Clean-local Smart 定价 fallback

原方案要求 Smart 固定 100 credits/source minute，P2 计划进一步要求 clean-local fallback 包含 `smart.standard: 100`。

当前代码事实：

- 生产 runtime pricing 文档记录已包含 `smart.standard: 100`。
- repo 默认 fallback 的 `gateway/credits_service.py` / `gateway/pricing_schema.py` 仍缺 `smart.standard: 100`。
- clean-local 默认估价会回退到非 Smart 默认值。

结论：必须补齐默认 pricing payload 和相关测试，避免本地、测试、fallback 路径与生产事实漂移。

### 3.3 `fail_and_refund` settlement —— DEFERRED（2026-05-24 Task #25 决断）

**当前状态（不是 launch blocker）**：

- `gateway/smart_consent.py::validate_smart_consent` 明确拒绝
  `on_budget_exhausted="fail_and_refund"`，返回 400 + 解释信息
  指向 `_BLOCKED_BUDGET_POLICIES`。
- 前端 `frontend-next/src/lib/api/jobs.ts::createJob` 硬编码提交
  `on_budget_exhausted: 'degraded_delivery_with_report'`，用户没有
  UI 选项可以选 `fail_and_refund`。
- 用户可见文案（Smart 卡片描述、QA report、admin 设置）**无**任何
  "失败退款"作为可选项的暗示。卡片只承诺 "100 点/分钟固定价，AI
  自动审核翻译并自动克隆音色"。
- `SmartAutoDecisionPanel.tsx` 里 `'fail_and_refunded'` status badge
  是历史 status 显示分支（dead code 在生产路径，但保留作为
  backend-contract 防御层）。
- `frontend-next/src/types/smart.ts::SmartStateFinal.status` 类型保留
  `'fail_and_refunded'` 字面量作为 backend contract 文档化，不暗示
  用户可选。

**决断（Task #25, 2026-05-24）**：

`fail_and_refund` 路径 **暂不实现**。原因：

1. 当前 `degraded_delivery_with_report` 已经能 cover 所有预算耗尽场景。
2. 实现完整三步 settlement（release reserve / reverse captured clone /
   partial capture actual cost capped at Studio price）需要触碰
   `credits_service.py` 的账务结算路径，风险显著高于其他两个 P2
   blocker。
3. 当前数据：90 天 13 个 succeeded smart 任务，0 个失败任务，没有
   实际场景在驱动 `fail_and_refund` 需求。

**保留**：

- Validator 的 `_BLOCKED_BUDGET_POLICIES = frozenset({"fail_and_refund"})`
  hard block + 测试覆盖（`tests/test_smart_consent_validator.py` /
  `tests/test_smart_skeleton_acceptance.py`）—— 防止以后某个 PR
  误重新启用。
- 主纲 §5.3 / §6.3 / Codex 第四十轮 P1.2 等历史设计记录不删。
  fail_and_refund 是已知未来能力，但**当前阶段** out-of-scope。

**真要实现的触发条件**：
- 出现真实"用户希望退款而非降级交付"的产品需求。
- 或：连续 N 个 smart 任务命中预算耗尽 + 用户对 degraded delivery
  不满。
此时另起一个独立 PR / task 设计完整三步 settlement，**不在 Smart
MVP launch 路径上**。

### 3.4 Smart 专属 TTS / LLM provider 闭环

`retry_budget.py` 已实现预算公式和判定，但 `smart_wiring.py` 中 Smart `TTSProvider` / `LLMProvider` 仍是 stub 或 verifier 预留。

结论：当前状态更准确描述为“复用现有 TTS / 修复路径，并增加 Smart retry budget 约束与报告”，不是完整独立 Smart TTS / rewrite provider 闭环。

### 3.5 文档和注释漂移

Jianying draft 部分历史注释仍有 “service_mode != studio skipped” 的旧描述，但当前 Job API 已允许 Smart。

结论：不影响主路径，但应在后续文档清理中修正。

## 4. 已发生的路线变更

### 4.1 `smart_consent` 不再包含客户端价格字段

原方案示例包含 `fixed_rate_credits_per_minute: 100`。后续 P2 方案删除该字段，理由是 Gateway 必须是 pricing 唯一事实源。

当前基线：

- 前端不提交价格字段。
- Gateway 按 runtime pricing / reserve snapshot 管理价格事实。
- 任何重新加入客户端价格字段的实现都应视为架构倒退。

### 4.2 字幕 / 音频一致性字段复用既有链路

原方案曾提出 `final_spoken_text` / `tts_payload_text` / `subtitle_source_text` 等字段拆分。

当前基线：

- 不为 P2 强制新增这组三字段。
- 继续复用已落地的 `tts_input_cn_text`、`merged_cn_text`、`text_audio_drift` 和 deliverable-time Whisper alignment 能力。
- 如后续确需更强一致性证据，应先补小范围 schema 设计和迁移评估。

### 4.3 Translation review 更偏全自动

原方案强调自动审批但保留若干 handoff 风险边界。当前实现中，很多 deterministic review 指标已变成 advisory / audit-only，hard gate 之外不阻断主流程。

当前基线：

- Smart 的产品预期是尽量全自动。
- 内容合规、provider exhaustion、预算 / consent 边界仍应保持硬门禁。

### 4.4 Voice strategy 转向 candidate-first reuse

原方案重点是主 speaker 样本选择、克隆和缓存复用。当前实现已演进为候选优先：

- 先查用户已有个人音色。
- strong same-source / strong_named 可自动复用。
- possible-match 可按后台策略自动复用。
- 只有确需新音色且 consent / quota / admin policy 允许时才调用 clone provider。

当前基线：这一变更合理，能降低成本和 handoff，但必须持续监控误复用。

### 4.5 P5 优先级重排

原方案 P5 首项偏向真实毛利分析。当前执行先做了 Smart analytics v1，把重点放在 handoff 分布、对齐质量、用户返工和 edit event 分布。

当前基线：v1 降级合理，但毛利分析必须有触发条件，不能无限期依赖单任务 cost 页。

## 5. P3 / P4 新决策

### 5.1 P3 不做完整大模块先行

不立即推进原方案中的完整独立 multimodal verifier 模块、large benchmark 和自动修复接入。

原因：

- 当前 Smart 主要瓶颈已转向 voice reuse / handoff / post-edit 数据，不是已证明的 verifier 缺失。
- verifier 的误报会直接带来人工成本或后续 TTS 成本。
- 用户未编辑不等于结果一定正确，需要 shadow 数据和主动抽样标注先证明价值。

### 5.2 P3 改为 shadow / only-report 试点

推荐后续 Task：

- 只读输入：原音频、TTS 音频、speaker metadata、Smart decisions。
- 只写输出：shadow verifier report，不改 pipeline，不触发 re-TTS。
- 指标：
  - verifier proposal vs 用户最终 edit 选择一致率。
  - verifier proposal vs 主动人工抽样标注一致率。
  - false positive 率。
  - false negative 率。
  - 单 job verifier 调用成本。

主动抽样要求：

- 随机抽 20-50 段 verifier 标记为应修改或高风险的片段。
- 人工实际听审，区分“用户容忍 / 未发现”与“verifier 真的无效”。

P3 正式产品化触发条件：

- 积累至少 50-100 个 Smart job 或等价片段样本。
- 出现可复现的 speaker wrong / mixed-speaker / voice drift 问题。
- shadow precision 达到可接受阈值，并且成本可控。

### 5.3 P4 不推进无人确认自动 re-TTS

原方案 P4 的 verifier 自动接入 Smart pipeline 暂不推进，特别是不做：

```text
verifier detects issue -> automatically re-TTS / rewrite -> deliver silently
```

原因：

- 误报会烧付费 API 或内部成本。
- 自动修复可能把可接受结果改坏。
- 与 Smart MVP 的付费 API 守卫和成本可控原则冲突。

允许的未来方向：

```text
verifier detects issue -> user-facing warning / admin report -> user confirms -> one-click repair
```

这属于 P3 only-report 的产品化，不属于无人确认 P4。

## 6. P5 后续触发条件

### 6.1 毛利分析触发条件

满足任一条件时启动 P5 毛利分析：

- 月成交 Smart 任务数 >= 100。
- Smart 单任务平均源视频时长 > 60 分钟。
- 连续 3 个 Smart 任务出现 estimated internal cost > recognized revenue。
- Admin cost 页面或 usage summary 出现 provider cost 异常尖峰。

毛利分析输入：

- `smart_cost_summary.json`
- `smart_decisions.jsonl`
- `usage_events.jsonl`
- Gateway debit / capture / refund ledger
- 任务源视频时长和 provider 成本

### 6.2 Threshold 调整触发条件

满足任一条件时启动按内容类型调阈值：

- Smart job 样本 >= 50。
- 同一内容类型下 handoff 率 > 10%。
- 同一内容类型下 post-edit 率 > 40%。
- 某类内容的 alignment / duration drift 明显高于整体平均。

### 6.3 Voice auto-reuse 收紧触发条件

需要新增或持续维护的看板指标：

- strong_named 命中后用户改音色比例。
- possible_match_auto_reused 命中后用户改音色比例。
- auto-reuse 后进入 post-edit 的比例。
- auto-reuse 后被用户改回 preset / clone 的比例。

建议阈值：

- strong_named 命中后改音色比例 > 30%：收紧 strong_named，要求增加音色相似度或来源一致性证据。
- possible_match_auto_reused 命中后改音色比例 > 30%：关闭默认 auto-reuse，回到 pause / confirm。

## 7. 短期执行清单

优先级从高到低：

1. 实现或正式废弃 Smart kill switch / 灰度门禁策略。
2. 补齐 clean-local `smart.standard: 100` fallback 和测试。
3. ~~决定 `fail_and_refund`：实现 settlement，或从当前产品面移除。~~
   **已决断（Task #25, 2026-05-24）**：deferred / not implemented，详见 §3.3。
   `fail_and_refund` 不在 Smart MVP launch 路径；validator + 前端均已 hard
   block。要真做时另起 PR 设计完整三步 settlement。
4. 修正文档 / 注释漂移，尤其是 Jianying draft 对 Smart 的支持描述。
5. 在 Smart analytics 中补 voice auto-reuse 后改音色比例指标。
6. 设计 P3 shadow verifier 的 only-report schema 和人工抽样流程。
7. 为毛利分析补触发条件监控，而不是立即做完整毛利系统。

## 8. 审计口径

后续评估 Smart 相关工作时，建议使用以下状态定义：

- DONE：代码路径、测试、默认配置、文档和本地 fallback 均对齐。
- PARTIAL：主代码存在，但默认路径、测试、配置或 UI 之一缺失。
- DEFERRED：明确不在当前阶段推进，且有触发条件。
- DRIFT：文档或注释仍描述旧行为，但主代码已改变。
- BLOCKER：影响灰度 / 生产发布安全性的缺口。

按此口径，当前最重要的结论不是“Smart 是否已做”，而是“Smart 已大量完成，但发布安全边界还没有完全闭环”。
