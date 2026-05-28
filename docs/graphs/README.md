# 图谱索引

新会话建议先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)，再按任务进入对应子图。

当前图谱基线：

- GitNexus 索引提交：`0ba02c77`
- 索引时间：`2026-05-28T08:24:28+08:00`
- 统计概览：`1504` 文件，`27,534` 节点，`62,770` 关系，`1030` 聚类，`300` 条流程

## 本轮更新重点

- CosyVoice 克隆进入独立付费能力：`/api/voice/cosyvoice/clone-gate` 负责展示层 gate，`POST /api/voice/cosyvoice/clone` 是唯一用户显式触发的克隆入口，支持 sample upload 与 `source_segments` 段落拼接两种输入。
- 国内 mainland worker 成为 CosyVoice clone/TTS 的 RPC 边界：Gateway 侧提供 admin status/healthz、HMAC client factory，worker 侧提供 `/cosyvoice/clone`、`/cosyvoice/synthesize-batch`、`DELETE /cosyvoice/voices/{voice_id}`。
- `user_voices` 增加 worker 路由与审计事实：`requires_worker`、`target_model`、`region_constraint`、`clone_worker_request_id`、`temporary_expires_at`，并通过 candidates、approve、editing voice-map、commit/copy-as-new 传播到 `segments.json`。
- TTS worker path 明确 fail-closed：`requires_worker=True` 强制 CosyVoice provider，不允许静默 fallback；worker 返回的 billed chars 是 authoritative；segment regenerate 禁止 worker 段落的 final retry loop。
- 前端 CosyVoice 克隆面补齐：`CosyVoiceCloneModal`、`CosyVoiceConsentModal`、`CosyVoiceSegmentPicker`、`cosyvoiceClone.ts`，并收窄 Next dev rewrite，避免 clone/clone-gate 被代理到生产。
- Smart 上线门禁补强：两层 kill switch 覆盖 entitlements、job creation 和 admin UI；`fail_and_refund` 继续被 validator 显式阻断；pricing consistency/fallback 有回归守卫。
- Smart analytics 新增 voice auto-reuse quality：summary payload 和 admin Tab 4 聚合 `strong / strong_named / possible_auto` 复用命中与后续人工改动。
- Pan backup 经历生产级硬化：HTTP API feature gate、全局非阻塞 advisory lock、`AVT_PAN_TMP_DIR`、free-space preflight、tail range probe、stale reaper 与 residue cleanup 状态责任边界都已固定。

## 使用顺序

1. 总览先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)
2. 想看 `SemanticBlock -> TTS -> DSP-first alignment -> cue_pipeline -> deliverables`，读 [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)
3. 想看 CosyVoice clone、国内 worker、HMAC RPC、source segment sample、worker routing、用户显式付费克隆，读 [GITNEXUS_COSYVOICE_MAINLAND_WORKER_GRAPH.md](./GITNEXUS_COSYVOICE_MAINLAND_WORKER_GRAPH.md)
4. 想看 Smart 自动审核、降级到 Studio、P5 possible-match auto-reuse、candidate-first voice policy、quality report、cost summary、user voice quota，读 [GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md](./GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md)
5. 想看 Studio / Smart 成功任务如何进入剪映草稿交付，读 [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)
6. 想看 review gate、speaker edits、candidate-first voice selection、Smart handoff、Smart 决策摘要，读 [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)
7. 想看 Smart/Studio 修改入口、editing speakers、split-many、智能切点、segment regenerate、batch re-TTS、overwrite / copy-as-new，读 [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)
8. 想看 `publish.dubbed_video`、`materials_pack`、`editor.jianying_draft_zip`、R2 registry、lazy fallback、sweeper、parity cleanup，读 [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)
9. 想看 pricing / trial / phone auth / email auth / Smart entry / entitlement truth / payment production gate / CSRF，读 [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)
10. 想看帮助中心、客服浮窗、通知中心、系统公告、新注册用户 live 分发与支持面 CSRF/polling，读 [GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md](./GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md)
11. 想看 admin settings、Smart voice policy、Smart analytics、report analysis、Phase 1b flags、mainland worker health、CSRF、polling governance、voice calibration、cost management、admin disk resize、cleanup、R2 sweeper 与 parity 运维面，读 [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)
12. 想看 `UsageMeter`、voice clone/reuse/rejection audit、RMB-direct pricing、Smart analytics、Phase 1a/1b reports、Smart sidecar、worker billed chars、`smart_shadow_eval / sim`、quality / cost / margin，读 [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)
13. 想看百度网盘备份、归档/恢复、BackupRecord 状态机、Pan schedulers、pan.* observability，读 [GITNEXUS_PAN_BACKUP_GRAPH.md](./GITNEXUS_PAN_BACKUP_GRAPH.md)

## 文件说明

- [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)：项目总图。适合第一次进入仓库时快速建立“前门 / Gateway / Job API / Workflow / Smart / Review / Editing / Delivery / Ops”整体结构。
- [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)：工作流内核图。聚焦 `SemanticBlock`、DSP-first 对齐、Smart inline branch、candidate-first voice policy、voice_id 传播、paid fallback、cue pipeline、deliverable-time whisper sidecar 与 Phase 1a/1b report sidecars。
- [GITNEXUS_COSYVOICE_MAINLAND_WORKER_GRAPH.md](./GITNEXUS_COSYVOICE_MAINLAND_WORKER_GRAPH.md)：CosyVoice / Mainland Worker 图。聚焦用户显式 clone、clone-gate、source_segments 样本拼接、OSS/R2 uploader、HMAC worker、worker routing、`requires_worker` TTS dispatch 与付费 API fail-closed 边界。
- [GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md](./GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md)：Smart 自动审核图。聚焦 deterministic eligibility、consent、translation review、candidate-first voice reuse/clone/preset orchestration、P5 possible-match auto-reuse、quota/balance exhaustion、handoff、quality report、cost summary。
- [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)：剪映草稿交付图。聚焦 `generate-jianying-draft`、`JianyingDraftRunner`、`substep`、`fingerprint`、`display_name`、claim guard、orphan rescue。
- [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)：审核流图。聚焦 `WorkspacePage`、review gate、translation/voice panels、candidate-first voice selection、Smart handoff 后重新进入 Studio，以及 Smart 决策摘要面板。
- [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)：编辑 / 后处理图。聚焦 Smart/Studio 修改入口、editing speakers registry、SegmentRow / ops panel、multi-cut split、智能切点、single/batch re-TTS、克隆/复用音色、`editing_audio_sync_required`、overwrite/copy-as-new、交付物失效。
- [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)：存储与交付图。聚焦 `materials_pack`、`editor.jianying_draft_zip`、`r2_artifacts`、`r2_artifact_sweeper`、`job_terminal_mirror`、R2 parity cleanup。
- [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)：商业化图。聚焦 Gateway plan truth、套餐事实、trial、支付、fake payment production gate、CSRF、phone/email auth 前门、Smart fixed price 与 entitlements。
- [GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md](./GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md)：支持 / 通知图。聚焦帮助中心、客服会话、通知中心、popup feed、系统公告、人工接管、支持面 CSRF 与 visibility-aware polling。
- [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)：Admin / Ops / Calibration 图。聚焦 alignment / whisper settings、Smart LLM model config、Smart voice policy、Smart analytics、report analysis、Phase 1b flags、CSRF、polling governance、voice calibration、traffic / cost / cleanup、admin disk cleanup/resize、R2 sweeper / parity / observability。
- [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)：质量与成本图。聚焦 `UsageMeter`、attempt 级事件、Smart sidecar、Smart analytics、Phase 1a/1b reports、voice reuse/clone/rejection metering、RMB-direct LLM pricing、edit audit、shadow eval/sim、quality / cost 报告。
- [GITNEXUS_PAN_BACKUP_GRAPH.md](./GITNEXUS_PAN_BACKUP_GRAPH.md)：网盘备份图。聚焦 admin pan API、Baidu OAuth、BackgroundTask、BackupRecord、backup/restore 状态机、scheduler、residue cleanup、stale reaper、pan.* observability 与通知。

## 什么时候优先先看图

- 对仓库不熟，先看总图，再看对应子图。
- 要改 `src/pipeline/process.py`、`src/services/smart/*`、`src/services/mainland_worker/*`、`src/services/runtime_flags.py`、`src/services/translation_quality.py`、`src/services/speaker_evidence.py`、`gateway/csrf.py`、`gateway/admin_smart_analytics_api.py`、`gateway/smart_consent.py`、`gateway/cosyvoice_clone/*`、`gateway/mainland_voice_worker.py`、`gateway/user_voice_api.py`、`gateway/user_voice_service.py`、`gateway/voice_selection_api.py`、`src/services/llm_registry.py`、`src/services/jobs/editing_segments.py`、`src/services/jobs/editing_split_suggest.py`、`gateway/admin_disk_api.py`、`gateway/admin_cost_api.py`、`gateway/pan/*`，先看图再读源码。
- 要判断 Smart 为什么提交被拒、为什么自动复用 possible-match、为什么复用/暂停/拒绝候选音色、为什么 CosyVoice clone-gate ready 但 POST 失败、为什么 worker routing 没传到 TTS、为什么 MiniMax balance 触发暂停、为什么没有 quality report、为什么 cost 只在 admin 可见、为什么 report analysis 的 flags 没生效、为什么 CSRF 拦截写请求、为什么 Smart 完成后能进入修改、为什么编辑分割需要重合成、为什么网盘备份卡在 archiving/restoring，先看对应图谱。
