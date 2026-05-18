# GitNexus 项目图谱

新会话建议先读本文件，再按任务进入对应子图。生成时间：`2026-05-18`

生成方式：基于 GitNexus 最新索引、Git 历史 diff 与源代码交叉整理。

## 1. 图谱概览

| 指标 | 数值 |
| --- | ---: |
| 文件数 | 1305 |
| 节点数 | 23,717 |
| 关系数 | 53,596 |
| 聚类数 | 874 |
| 流程数 | 300 |
| 索引提交 | `68077dc` |
| 索引状态 | `up-to-date` |

本轮最需要反映的结构变化：

- Smart 已进入更严格的 P2 launch 形态：`smart_consent.py` 锁定 6 字段 consent，`compute_job_policy("smart")` 锁定 MiniMax + `speech-2.8-hd`，Gateway create path 在 consent 与 admin clone policy 都允许时做提交前 UserVoice quota 水位检查。
- Smart voice path 从“直接 clone”推进到“先查同源个人音色复用，再必要时 clone”：UserVoice 增加 source metadata，Gateway 暴露 internal match，pipeline 和 VoiceSelection UI 都能消费复用候选。
- Smart voice_id 传播修复已落地：reused/cloned/preset 决策写入 `_speaker_voices` 后，会回写 `voice_id_a / voice_id_b`，避免 2-speaker path 仍把 `auto` 传给 translator。
- 非主说话人不再遗留空 voice_id，Smart 会从 `auto_matched_voice` 自动解析 preset voice，并结合 segment-level `dubbing_mode` 聚合跳过 keep-original speaker。
- Smart handoff job 不一定有 `smart_quality_report.json`，Job API 会从 `smart_decisions.jsonl` 合成最小 quality report，让 Workspace 显示“已转人工 + 原因 + 阶段”。
- Smart cost summary 永远是 admin-only surface，pipeline 写 `audit/smart_cost_summary.json`，Gateway settlement 后通过 `cost_summary_backfill.py` 回填实际扣点和 quota 使用量。
- Smart 成功或已降级任务现在可从项目列表进入 post-edit 修改；前端入口包含 `smart`，后端仍通过 `is_editable_smart_state` 限制只允许 `completed / downgraded_to_studio`。
- Smart LLM 默认模型进入 admin 模型配置体系，未配置时 Smart mode 各 prompt 默认 Gemini 3.1 Pro。
- Voice selection 从单点 `voice-match` 进入 candidate-first：统一 `voice-candidates` API 返回 strong auto-reuse、requires-confirmation、cross-source named candidate，Studio 和 post-edit 共享这套候选优先 UI。
- Smart voice policy 由 admin settings 控制：`smart_auto_clone_enabled`、`smart_reuse_user_voice_enabled`、`smart_pause_on_possible_user_voice_match` 分别控制新克隆、复用查询和弱匹配暂停。
- Editing 页面进入 Phase 2：左视频 + ops panel / 右侧 segment list 重构，`SplitSegmentDialog` 支持 1..N cuts，后端 `split_editing_segment_many` 用 write-ahead journal 保护 `segments/status/voice_map` 三文件一致性。
- 智能切点变为用户显式触发的 LLM 功能：`suggest-split` 每段最多一次、每任务有 cap，LLM 返回 `at_text` 后再映射到 `source_index`，不再使用已移除的 v1 word-context 自动预填。
- 成本审计修正：Smart auto-clone 成功会进入 `UsageMeter.record_voice_clone`，用户拒绝候选会进入非计费 `voice_candidate_rejected`，LLM price catalog 改为 RMB-direct。
- Admin/Ops 新增磁盘扩容安全面：`admin_disk_api.py` 生成 `resize_hint`，`disk_resize_helper.py` 独立持有 raw block device，只通过 loopback + bearer token 执行 `resize2fs`。

## 2. 关键基座

| 基座 | 当前主轴 | 代表文件 |
| --- | --- | --- |
| Workflow | `SemanticBlock -> TTS -> DSP-first alignment -> cue_pipeline -> editor outputs`，Smart inline branch 挂在 review gate 前后 | `src/pipeline/process.py`, `src/services/alignment/aligner.py` |
| Smart | deterministic auto-review, consent gate, admin policy, candidate-first reuse/clone/preset, sidecar audit, Studio handoff | `src/services/smart/*`, `src/services/smart_wiring.py`, `gateway/smart_consent.py`, `src/pipeline/process.py` |
| Smart Reports | user quality report 与 admin cost summary 分离 | `src/services/smart/sidecar_emitter.py`, `src/services/smart/quality_report_synthesizer.py`, `gateway/admin_cost_api.py` |
| Review | `waiting_for_review -> WorkspacePage panels -> resume`，Smart handoff 复用 Studio gate，voice selection 支持 candidate-first clone/reuse | `src/services/review_state.py`, `src/services/jobs/review_actions.py`, `gateway/voice_selection_api.py` |
| Editing | Smart/Studio `enter-edit -> editing speakers -> split-many / suggest-split -> regenerate -> batch -> commit` | `src/services/jobs/editing_segments.py`, `src/services/jobs/editing_split_suggest.py`, `src/services/jobs/editing_batch.py`, `src/services/jobs/editing_commit.py` |
| Delivery | `materials_pack / generate_video / editor.jianying_draft_zip / R2 registry / parity` | `gateway/storage/backend_router.py`, `gateway/r2_artifact_sweeper.py`, `src/services/r2_publisher_lib/r2_parity.py` |
| Commercialization | Gateway owns plan, trial, pricing, entitlement, Smart availability, consent, fixed price and policy | `gateway/plan_catalog.py`, `gateway/entitlements.py`, `gateway/credits_service.py`, `gateway/job_intercept.py` |
| Auth | phone + email registration, reset, session | `gateway/auth_phone.py`, `gateway/auth_email.py`, `frontend-next/src/components/auth/*` |
| Calibration | manual / clone-after / review-preflight / Smart clone mirror / candidate matching / source metadata | `gateway/user_voice_api.py`, `gateway/user_voice_service.py`, `gateway/voice_calibration_hook.py`, `gateway/voice_calibration_review_preflight.py` |
| Admin/Ops | settings, Smart LLM defaults, Smart voice policy, traffic, support, cost, disk cleanup/resize, R2 sweeper | `gateway/admin_settings.py`, `src/services/llm_registry.py`, `gateway/admin_disk_api.py`, `gateway/disk_resize_helper.py`, `gateway/admin_cost_api.py`, `gateway/main.py` |
| Metering & Settlement | `UsageMeter`, voice reuse/clone/rejection meter, RMB-direct pricing, Smart credits policy, terminal settle, cost backfill | `src/services/usage_meter.py`, `gateway/cost_management.py`, `gateway/credits_service.py`, `gateway/job_terminal_mirror.py`, `gateway/cost_summary_backfill.py` |
| Offline Evaluation | `smart_shadow_eval / sim`, quality/cost reports | `scripts/smart_shadow_eval_collector.py`, `scripts/smart_shadow_sim_aggregator.py` |

## 3. 子图入口

- 图谱索引：`docs/graphs/README.md`
- 工作流内核图：`docs/graphs/GITNEXUS_WORKFLOW_CORE_GRAPH.md`
- Smart 自动审核图：`docs/graphs/GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md`
- 剪映草稿交付图：`docs/graphs/GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md`
- 审核流图：`docs/graphs/GITNEXUS_REVIEW_GRAPH.md`
- 编辑 / 后处理图：`docs/graphs/GITNEXUS_EDITING_POST_EDIT_GRAPH.md`
- 存储与交付图：`docs/graphs/GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md`
- 商业化图：`docs/graphs/GITNEXUS_COMMERCIALIZATION_GRAPH.md`
- 支持 / 通知图：`docs/graphs/GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md`
- Admin / Ops / Calibration 图：`docs/graphs/GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md`
- Benchmark / Quality / Cost 图：`docs/graphs/GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md`

## 4. 仓库结构图

```mermaid
graph TD
    Marketing["Marketing / Pricing / FAQ / SEO"] --> PlansSSR["SSR plans + trial facts"]
    Marketing --> AuthFront["Phone + Email auth"]

    Workspace["Workspace / Projects / Result UI"] --> FrontApi["Frontend API / hooks"]
    TranslationForm["TranslationForm service_mode selector"] --> FrontApi
    ReviewUI["Review panels + SmartAutoDecisionPanel"] --> FrontApi
    EditUI["Post-edit UI"] --> FrontApi
    AppShell["Bell / popup / support / admin entry"] --> FrontApi
    AdminUI["Admin / Ops UI"] --> FrontApi

    PlansSSR --> Gateway["Gateway truth + control"]
    AuthFront --> Gateway
    FrontApi --> Gateway
    FrontApi --> JobApi["Job API / artifacts / tasks"]

    Gateway --> Billing["plan / trial / credits / payment"]
    Gateway --> Entitlements["entitlements / allowed service modes"]
    Gateway --> SmartConsent["smart_consent validator"]
    Gateway --> SupportPlane["support / notifications / announcements"]
    Gateway --> Ops["settings / Smart voice policy / LLM models / costs / disk cleanup + resize"]
    Gateway --> CalibrationPlane["voice calibration + user voice quota + candidates + source metadata"]
    Gateway --> Proxy["ownership + subresource proxy"]

    JobApi --> Workflow["process.py / ProjectWorkflow"]
    Workflow --> EffectiveMode["derive_effective_pipeline_mode"]
    EffectiveMode --> SmartPlane["Smart auto review"]
    EffectiveMode --> StudioFlow["Studio review flow"]

    SmartConsent --> SmartPlane
    SmartPlane --> Eligibility["eligibility_gate"]
    Eligibility --> AutoVoice["auto_voice_review"]
    Eligibility --> AutoTranslation["auto_translation_review"]
    AutoVoice --> SmartPolicy["admin smart_auto_clone / reuse / pause policy"]
    AutoVoice --> Quota["Gateway user voice quota"]
    AutoVoice --> VoiceCandidates["UserVoice candidates API"]
    VoiceCandidates --> VoiceReuse["strong auto-reuse"]
    VoiceCandidates --> WeakPause["weak/medium/cross-source confirmation pause"]
    AutoVoice --> CloneProvider["smart_wiring MiniMax clone provider"]
    VoiceReuse --> UserVoiceMirror["UserVoice source metadata"]
    WeakPause --> Handoff
    CloneProvider --> UserVoiceMirror["register-smart user voice mirror"]
    AutoVoice --> MinorPreset["minor speaker preset auto-match"]
    MinorPreset --> VoiceIdProp["voice_id_a / voice_id_b propagation"]
    AutoTranslation --> SmartSidecar["smart_decisions / quality / cost"]
    AutoVoice --> SmartSidecar
    SmartPlane --> Handoff["handoff markers"]
    Handoff --> ReviewGate["waiting_for_review"]

    SmartSidecar --> QualityApi["Job API smart-quality-report"]
    QualityApi --> ReviewUI
    SmartSidecar --> CostApi["Admin cost API"]
    CostApi --> AdminUI

    Workflow --> ReviewGate
    ReviewGate --> ReviewUI
    ReviewUI --> ReviewApprove["review actions / voice selection approve"]
    ReviewApprove --> CalibrationPlane
    ReviewApprove --> Workflow
    ReviewUI --> CandidateUI["candidate-first voice selection"]
    CandidateUI --> CloneModal["voice clone/reuse modal"]
    CandidateUI --> CandidateAudit["voice_candidate_rejected / voice_reuse audit"]
    CloneModal --> CalibrationPlane

    EditUI --> EditingPlane["Smart/Studio edit / speakers / split / regenerate / batch / commit"]
    EditingPlane --> CloneModal
    EditingPlane --> SplitMany["SplitSegmentDialog + split-many journal"]
    EditingPlane --> SuggestSplit["LLM suggest-split explicit button"]
    EditingPlane --> Workflow
    EditingPlane --> DeliveryReset["invalidate stale deliverables"]

    Workflow --> EditOutputs["editor package / publish outputs"]
    EditOutputs --> WhisperEnsure["ensure_whisper_aligned_subtitles"]
    WhisperEnsure --> PackTask["materials_pack"]
    WhisperEnsure --> JDraftTask["generate-jianying-draft"]
    EditOutputs --> VideoTask["generate_video"]

    PackTask --> Delivery["downloads / R2 / local fallback"]
    JDraftTask --> Delivery
    VideoTask --> Delivery

    MainLife["gateway/main.py lifespan"] --> Sweeper["r2_artifact_sweeper"]
    Sweeper --> Mirror["job_terminal_mirror"]
    Mirror --> Registry["r2_artifacts + terminal PG mirror"]
    Mirror --> Settlement["settle_job_credit_ledger"]
    Settlement --> CostBackfill["cost_summary_backfill"]
    CostBackfill --> CostApi
    Sweeper --> Publisher["r2_publisher"]
    Publisher --> Registry
    Registry --> Delivery

    Cleanup["project_cleanup"] --> Parity["r2_parity_ok"]
    AdminDisk["admin_disk_api"] --> Cleanup
    AdminDisk --> ResizeHint["resize_hint"]
    ResizeHint --> ResizeHelper["disk_resize_helper loopback resize2fs"]
    AdminDisk --> DiskUI["/admin/disk"]
    Parity --> Registry

    Workflow --> UsageMeter["UsageMeter / attempt-level + clone/reuse/rejection usage"]
    UsageMeter --> CostCatalog["RMB-direct cost catalog"]
    JobApi --> JobEvents["JobEvent stream"]
    EditUI --> UserEditAudit["user_edit_events.jsonl"]
    SmartSidecar --> Shadow["smart_shadow_eval / sim"]
    UsageMeter --> Shadow
    UserEditAudit --> Shadow
    JobEvents --> Ops
    Shadow --> Ops

    Settlement --> Billing
    Entitlements --> TranslationForm
    SupportPlane --> AppShell
    Ops --> AdminUI
    CalibrationPlane --> AdminUI
```

## 5. 核心证据链

### 5.1 Smart 已经从“审核骨架”进入“入口、策略、报告”的闭环

- `frontend-next/src/components/workspace/TranslationForm.tsx` 暴露 `serviceMode = "smart"`，读取 entitlements 判断是否可用，并用 credits estimate 获取智能版单价。
- `gateway/smart_consent.py` 强制 Smart 提交携带完整 6 字段 consent，并暂时拒绝未实现的 `fail_and_refund` 结算策略。
- `gateway/job_intercept.py::compute_job_policy(...)` 对 Smart 固定 MiniMax、`speech-2.8-hd`、`requires_review=True` 和 `voice_strategy=smart_auto`。
- `src/pipeline/process.py` 在 Smart effective mode 下执行 eligibility、voice review、translation review、handoff、quality report、cost summary。
- `src/pipeline/process.py` 通过 `services.admin_settings.read_admin_setting` 读取 Smart voice policy，避免 Gateway-only settings loader 在 app runtime 中失效。
- `frontend-next/src/components/workspace/SmartAutoDecisionPanel.tsx` 用户侧只渲染 quality report，不包含任何内部成本字段。

结论：Smart 不是只存在于后端策略层，而是从提交入口、pipeline 决策、用户解释面到 admin 成本面都有显式结构。

### 5.2 Voice candidate-first 成为 Studio / Post-edit / Smart 的共用音色入口

- `gateway/alembic/versions/028_user_voice_source_metadata.py` 为 `user_voices` 增加 source hash、source speaker、source job、sample seconds 等溯源字段和索引。
- `gateway/user_voice_service.py::match_user_voices(...)` 输出 `same_source_strong / same_source_named / same_source_speaker_id_changed / cross_source_named_person`，并过滤 generic speaker name，避免“Speaker A”误匹配。
- `gateway/user_voice_api.py` 暴露 internal `/api/internal/user-voices/candidates`，`gateway/voice_selection_api.py` 暴露 public `/job-api/jobs/{job_id}/voice-candidates`。
- `VoiceSelectionPanel.tsx` 和 `VoiceModifyTab.tsx` 都按“强匹配 / 可能匹配 / 其他个人音色”顺序展示候选。
- `src/services/usage_meter.py::record_voice_reuse(...)` 与 `record_voice_candidate_rejected(...)` 都是非计费审计事件。

结论：个人音色不再只是 clone 成功后的库记录，而是进入创建、审核、后编辑、Smart 暂停决策的候选层。

### 5.3 Smart voice clone 的生产边界现在更严格

- `_fetch_smart_user_voice_quota_remaining(...)` 通过 Gateway internal API 查询用户音色库剩余额度。
- Gateway create path 只有在 consent 和 admin clone policy 都允许新克隆时，才对非 admin Smart job 做提交前 quota safety water mark 检查。
- `smart_auto_clone_enabled=False` 只禁止新 clone，不禁止强匹配复用；`smart_reuse_user_voice_enabled=False` 才会跳过候选查询。
- `smart_pause_on_possible_user_voice_match=True` 时，非强匹配候选会触发 `possible_user_voice_match_requires_confirmation`，并把候选写入 review payload。
- `build_smart_clone_provider()` 仍集中在 `src/services/smart_wiring.py`，Smart 核心包不直接导入真实 provider。
- `_register_smart_clone_in_user_voices(...)` 将 clone 成功结果镜像回 Gateway UserVoice，否则 fail-closed handoff。
- `_resolve_smart_minor_speaker_voices(...)` 为非主说话人解析 preset voice，避免 Smart approved payload 留下空 voice_id。
- Smart auto-approve 分支会把 `_speaker_voices` 回灌到 `voice_id_a / voice_id_b`，确保 2-speaker translate path 真正使用 cloned/reused voice。
- Smart auto-clone 成功现在会记录 `UsageMeter.record_voice_clone(...)`，避免 admin cost view 漏掉 MiniMax clone 成本。

结论：Smart clone 不再是单点调用 provider，而是 consent、quota snapshot、reuse match、provider composition、UserVoice mirror、preset fallback 的组合边界。

### 5.4 quality report 和 cost summary 是两条不同安全域

- `smart_quality_report.json` 是用户可见解释层，Job API 只对 Smart job 暴露。
- `quality_report_synthesizer.py` 会从 `smart_decisions.jsonl` 合成 handoff 摘要，避免用户看到误导性的“处理中”。
- `smart_cost_summary.json` 由 admin-only `GET /api/admin/jobs/{job_id}/cost` 读取。
- `cost_summary_backfill.py` 在 settlement 后回填实际扣点和 MiniMax quota 使用量。

结论：质量解释给用户，成本审计给管理员，不能把内部成本字段泄漏到 Workspace。

### 5.5 Smart 完成后进入 post-edit 的入口已打开

- `frontend-next/src/app/(app)/projects/page.tsx` 的 `EDITABLE_SERVICE_MODES` 包含 `smart`。
- `src/services/jobs/api.py` 和 `src/services/smart/state.py` 仍是后端真源，只有 `completed / downgraded_to_studio` 可编辑。
- `VoiceModifyTab.tsx` 复用主流程的 `VoiceCloneModal` 和 `SpeakerAudioAuditModal`，克隆仍必须由用户显式点击触发。
- `VoiceModifyTab.tsx` 也接入 `voice-candidates`，后编辑里同样能复用强匹配/可能匹配/其他个人音色。

结论：产品路径从“Smart 自动交付”补齐到“必要时进入 Studio post-edit 精修”。

### 5.6 Editing 分割从单切点升级到 multi-cut + LLM 建议

- `SegmentRow.tsx` 承接右侧段落行，`CurrentSegmentOpsPanel.tsx` 承接左侧当前段操作，`SplitSegmentDialog.tsx` 承接分割弹窗。
- `split_editing_segment_many(...)` 一次把一个 segment 替换成 N+1 段，并用 write-ahead journal 恢复 `segments.json / segment_status.json / voice_map.json` 的中间失败。
- `editing_split_suggest.py` 只在用户点击“智能识别说话人切点”时调用 LLM，每段最多一次、每任务有 cap，不做自动兜底或批量调用。
- 前端拖动中文切点时冻结 `source_index`，英文切点会 snap 到 word boundary，避免音频锚点被中文调整带偏。

结论：后编辑分割已经是正式编辑模型，不再是行内临时 UI。

### 5.7 Admin/Ops 已经有正式磁盘、模型、Smart voice policy 管理平面

- `gateway/admin_disk_api.py` 暴露 overview、cleanup-orphans、cleanup-expired。
- 清理入口接收 job id，不接收任意路径，并复用 `project_cleanup.py` 的 safe root 检查。
- `frontend-next/src/app/(app)/admin/disk/page.tsx` 提供容量、孤儿目录、过期目录、protected/admin 目录的 UI。
- `gateway/admin_disk_api.py` 现在还输出 `resize_hint`，并通过 `POST /api/admin/disk/resize-filesystem` 代理到 loopback helper。
- `gateway/disk_resize_helper.py` 独立持有 raw block device，要求 bearer token、`confirm=true`、ext4、`resize2fs/tune2fs` 可用，并有进程内 resize lock。
- `src/services/llm_registry.py` 为 Smart mode 定义 Gemini 3.1 Pro per-mode defaults，admin settings 的 mode-aware prompt_models 可覆盖。
- `frontend-next/src/app/(app)/admin/settings/page.tsx` 暴露 Smart 自动克隆、复用个人音色、弱匹配确认三个策略开关。

结论：项目目录清理、受控扩容、Smart LLM 模型选择和 Smart voice policy 都进入 Gateway admin 控制平面。

### 5.8 Gateway 继续是商业事实真源

- `gateway/plan_catalog.py`、`gateway/entitlements.py`、`gateway/credits_service.py` 管理 plan、allowed service modes、fixed price、credit estimate。
- 前端只消费 Gateway facts，不能把智能版可用性或价格固化成第二套真源。

结论：Smart 入口上线后，商业化约束仍是 Gateway-source-of-truth。

## 6. 按任务选图

- 要看 Smart 自动审核、consent、voice reuse/clone quota、handoff、quality report、cost summary，读 `GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md`
- 要看 phone/email auth、trial、pricing truth、Smart entry/entitlement/consent/weak-match warning，读 `GITNEXUS_COMMERCIALIZATION_GRAPH.md`
- 要看 Smart/Studio post-edit 修改入口、multi-cut、智能切点和编辑态克隆/复用音色，读 `GITNEXUS_EDITING_POST_EDIT_GRAPH.md`
- 要看 admin disk cleanup/resize、Smart voice policy、Smart LLM model config、cost summary admin page、settlement backfill、cleanup 运维面，读 `GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md`
- 要看 Smart sidecar、UsageMeter、voice reuse/clone/rejection metering、RMB-direct pricing、shadow eval、质量与成本，读 `GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md`
- 要看 review UI、candidate-first voice selection、Smart 弱匹配暂停与决策摘要，读 `GITNEXUS_REVIEW_GRAPH.md`
- 要看 workflow 内核、DSP-first 对齐、voice_id 传播与 cue pipeline，读 `GITNEXUS_WORKFLOW_CORE_GRAPH.md`
