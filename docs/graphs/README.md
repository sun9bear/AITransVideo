# 图谱索引

新会话建议先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)，再按任务进入对应子图。

当前图谱基线：

- GitNexus 索引提交：`e78a686`
- 索引时间：`2026-05-17T01:51:14.094Z`
- 统计概览：`1290` 文件，`23,201` 节点，`52,487` 关系，`861` 聚类，`300` 条流程

## 本轮更新重点

- Smart 提交入口被进一步锁紧：`smart_consent.py` 校验 6 字段 consent，`fail_and_refund` 因 settlement stub 暂时在入口拒绝，Gateway create path 只持久化 canonical consent。
- Smart 执行策略被 Gateway 强制固定：`compute_job_policy("smart")` 锁定 MiniMax、`speech-2.8-hd`、`requires_review=True`、`voice_strategy=smart_auto`，不继承 express/studio admin TTS provider。
- Smart 音色链路新增“先复用个人音色，再必要时克隆”：UserVoice source metadata、same-source match、`record_voice_reuse`、clone lock、Smart reused voice decision 和 voice_id 传播修复已经进入代码。
- Smart 作业完成后可以像 Studio 一样进入 post-edit：前端 `projects/page.tsx` 的修改入口包含 `smart`，后端继续用 `is_editable_smart_state` fail-closed。
- Admin/Ops 增加智能版模型配置边界：`llm_registry.py` 为 Smart mode 默认使用 Gemini 3.1 Pro，admin prompt/model UI 可按 mode 覆盖。
- 图谱保持原有入口结构，没有新增文件名；本轮刷新总图和 Smart、工作流、审核、商业化、编辑、Admin/Ops、质量成本子图。

## 使用顺序

1. 总览先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)
2. 想看 `SemanticBlock -> TTS -> DSP-first alignment -> cue_pipeline -> deliverables`，读 [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)
3. 想看 Smart 自动审核、降级到 Studio、quality report、cost summary、user voice quota，读 [GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md](./GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md)
4. 想看 Studio / Smart 成功任务如何进入剪映草稿交付，读 [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)
5. 想看 review gate、speaker edits、voice selection、Smart handoff、Smart 决策摘要，读 [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)
6. 想看 Smart/Studio 修改入口、editing speakers、voice profile inference、segment regenerate、batch re-TTS、overwrite / copy-as-new，读 [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)
7. 想看 `publish.dubbed_video`、`materials_pack`、`editor.jianying_draft_zip`、R2 registry、lazy fallback、sweeper、parity cleanup，读 [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)
8. 想看 pricing / trial / phone auth / email auth / Smart entry / entitlement truth，读 [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)
9. 想看帮助中心、客服浮窗、通知中心、系统公告与新注册用户 live 分发，读 [GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md](./GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md)
10. 想看 admin settings、voice calibration、traffic analytics、cost management、admin disk、cleanup、R2 sweeper 与 parity 运维面，读 [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)
11. 想看 `UsageMeter`、attempt-level audit、Smart sidecar、`smart_shadow_eval / sim`、quality / cost / margin，读 [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)

## 文件说明

- [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)：项目总图。适合第一次进入仓库时快速建立“前门 / Gateway / Job API / Workflow / Smart / Review / Editing / Delivery / Ops”整体结构。
- [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)：工作流内核图。聚焦 `SemanticBlock`、DSP-first 对齐、Smart inline branch、voice_id 传播、paid fallback、cue pipeline、deliverable-time whisper sidecar。
- [GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md](./GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md)：Smart 自动审核图。聚焦 deterministic eligibility、consent、translation review、voice reuse/clone/preset orchestration、quota、handoff、quality report、cost summary。
- [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)：剪映草稿交付图。聚焦 `generate-jianying-draft`、`JianyingDraftRunner`、`substep`、`fingerprint`、`display_name`、claim guard、orphan rescue。
- [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)：审核流图。聚焦 `WorkspacePage`、review gate、translation/voice panels、voice selection、Smart handoff 后重新进入 Studio，以及 Smart 决策摘要面板。
- [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)：编辑 / 后处理图。聚焦 Smart/Studio 修改入口、editing speakers registry、speaker profile 推断、single/batch re-TTS、克隆/复用音色、`editing_audio_sync_required`、overwrite/copy-as-new、交付物失效。
- [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)：存储与交付图。聚焦 `materials_pack`、`editor.jianying_draft_zip`、`r2_artifacts`、`r2_artifact_sweeper`、`job_terminal_mirror`、R2 parity cleanup。
- [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)：商业化图。聚焦 Gateway plan truth、套餐事实、trial、支付、phone/email auth 前门、Smart fixed price 与 entitlements。
- [GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md](./GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md)：支持 / 通知图。聚焦帮助中心、客服会话、通知中心、popup feed、系统公告、人工接管。
- [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)：Admin / Ops / Calibration 图。聚焦 alignment / whisper settings、Smart LLM model config、voice calibration、traffic / cost / cleanup、admin disk、R2 sweeper / parity / observability。
- [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)：质量与成本图。聚焦 `UsageMeter`、attempt 级事件、Smart sidecar、voice reuse/clone metering、edit audit、shadow eval/sim、quality / cost 报告。

## 什么时候优先先看图

- 对仓库不熟，先看总图，再看对应子图。
- 要改 `src/pipeline/process.py`、`src/services/smart/*`、`gateway/smart_consent.py`、`gateway/user_voice_api.py`、`gateway/user_voice_service.py`、`gateway/voice_selection_api.py`、`src/services/llm_registry.py`、`gateway/admin_disk_api.py`、`gateway/admin_cost_api.py`，先看图再读源码。
- 要判断 Smart 为什么提交被拒、为什么复用旧音色、为什么没有 quality report、为什么 cost 只在 admin 可见、为什么 Smart 完成后能进入修改，先看对应图谱。
