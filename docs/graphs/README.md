# 图谱索引

新会话建议先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)，再按任务进入对应子图。

当前图谱基线：

- GitNexus 索引提交：`68077dc`
- 索引时间：`2026-05-18T08:44:16.341Z`
- 统计概览：`1305` 文件，`23,717` 节点，`53,596` 关系，`874` 聚类，`300` 条流程

## 本轮更新重点

- 编辑页进入 Phase 2a/2b：`SegmentRow`、`CurrentSegmentOpsPanel`、`SplitSegmentDialog` 重构完成，后端新增 `split_editing_segment_many` + write-ahead journal，智能切点改为用户显式触发的 LLM `suggest-split`。
- 音色选择进入 candidate-first：Gateway 新增统一 `voice-candidates` API，UserVoice matching 支持 same-source strong / named / speaker-id-changed 与 cross-source named candidate，Studio 与 post-edit UI 都优先展示个人音色候选。
- Smart voice policy 进入 admin 可控：`smart_auto_clone_enabled`、`smart_reuse_user_voice_enabled`、`smart_pause_on_possible_user_voice_match` 暴露到 admin settings，pipeline 使用 app-safe `read_admin_setting`，弱匹配暂停只在 admin opt-in 后触发。
- Smart 弱匹配暂停补齐审计：pipeline 会把 `smart_offered_candidates` 写入 review payload，用户拒绝候选时 Gateway 记录非计费 `voice_candidate_rejected` usage event。
- 成本口径修正：Smart auto-clone 成功后写 `UsageMeter.record_voice_clone`，LLM 价格目录切到 RMB-direct，Gemini 3.1 Pro 使用官方 ≤200K tier 的人民币单价。
- Admin disk 从清理面扩展到受控 ext4 扩容面：`admin_disk_api.py` 输出 `resize_hint`，新增 loopback `disk_resize_helper.py`，Compose 将 raw block device 只挂给 helper。
- 图谱保持原有入口结构，没有新增文件名；本轮刷新总图和 Smart、审核、编辑、商业化、Admin/Ops、质量成本、工作流子图。

## 使用顺序

1. 总览先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)
2. 想看 `SemanticBlock -> TTS -> DSP-first alignment -> cue_pipeline -> deliverables`，读 [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)
3. 想看 Smart 自动审核、降级到 Studio、candidate-first voice policy、quality report、cost summary、user voice quota，读 [GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md](./GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md)
4. 想看 Studio / Smart 成功任务如何进入剪映草稿交付，读 [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)
5. 想看 review gate、speaker edits、candidate-first voice selection、Smart handoff、Smart 决策摘要，读 [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)
6. 想看 Smart/Studio 修改入口、editing speakers、split-many、智能切点、segment regenerate、batch re-TTS、overwrite / copy-as-new，读 [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)
7. 想看 `publish.dubbed_video`、`materials_pack`、`editor.jianying_draft_zip`、R2 registry、lazy fallback、sweeper、parity cleanup，读 [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)
8. 想看 pricing / trial / phone auth / email auth / Smart entry / entitlement truth，读 [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)
9. 想看帮助中心、客服浮窗、通知中心、系统公告与新注册用户 live 分发，读 [GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md](./GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md)
10. 想看 admin settings、Smart voice policy、voice calibration、cost management、admin disk resize、cleanup、R2 sweeper 与 parity 运维面，读 [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)
11. 想看 `UsageMeter`、voice clone/reuse/rejection audit、RMB-direct pricing、Smart sidecar、`smart_shadow_eval / sim`、quality / cost / margin，读 [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)

## 文件说明

- [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)：项目总图。适合第一次进入仓库时快速建立“前门 / Gateway / Job API / Workflow / Smart / Review / Editing / Delivery / Ops”整体结构。
- [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)：工作流内核图。聚焦 `SemanticBlock`、DSP-first 对齐、Smart inline branch、candidate-first voice policy、voice_id 传播、paid fallback、cue pipeline、deliverable-time whisper sidecar。
- [GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md](./GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md)：Smart 自动审核图。聚焦 deterministic eligibility、consent、translation review、candidate-first voice reuse/clone/preset orchestration、weak-match pause、quota、handoff、quality report、cost summary。
- [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)：剪映草稿交付图。聚焦 `generate-jianying-draft`、`JianyingDraftRunner`、`substep`、`fingerprint`、`display_name`、claim guard、orphan rescue。
- [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)：审核流图。聚焦 `WorkspacePage`、review gate、translation/voice panels、candidate-first voice selection、Smart handoff 后重新进入 Studio，以及 Smart 决策摘要面板。
- [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)：编辑 / 后处理图。聚焦 Smart/Studio 修改入口、editing speakers registry、SegmentRow / ops panel、multi-cut split、智能切点、single/batch re-TTS、克隆/复用音色、`editing_audio_sync_required`、overwrite/copy-as-new、交付物失效。
- [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)：存储与交付图。聚焦 `materials_pack`、`editor.jianying_draft_zip`、`r2_artifacts`、`r2_artifact_sweeper`、`job_terminal_mirror`、R2 parity cleanup。
- [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)：商业化图。聚焦 Gateway plan truth、套餐事实、trial、支付、phone/email auth 前门、Smart fixed price 与 entitlements。
- [GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md](./GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md)：支持 / 通知图。聚焦帮助中心、客服会话、通知中心、popup feed、系统公告、人工接管。
- [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)：Admin / Ops / Calibration 图。聚焦 alignment / whisper settings、Smart LLM model config、Smart voice policy、voice calibration、traffic / cost / cleanup、admin disk cleanup/resize、R2 sweeper / parity / observability。
- [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)：质量与成本图。聚焦 `UsageMeter`、attempt 级事件、Smart sidecar、voice reuse/clone/rejection metering、RMB-direct LLM pricing、edit audit、shadow eval/sim、quality / cost 报告。

## 什么时候优先先看图

- 对仓库不熟，先看总图，再看对应子图。
- 要改 `src/pipeline/process.py`、`src/services/smart/*`、`gateway/smart_consent.py`、`gateway/user_voice_api.py`、`gateway/user_voice_service.py`、`gateway/voice_selection_api.py`、`src/services/llm_registry.py`、`src/services/jobs/editing_segments.py`、`src/services/jobs/editing_split_suggest.py`、`gateway/admin_disk_api.py`、`gateway/admin_cost_api.py`，先看图再读源码。
- 要判断 Smart 为什么提交被拒、为什么复用/暂停/拒绝候选音色、为什么没有 quality report、为什么 cost 只在 admin 可见、为什么 Smart 完成后能进入修改、为什么编辑分割需要重合成，先看对应图谱。
