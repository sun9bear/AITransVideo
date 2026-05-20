# 图谱索引

新会话建议先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)，再按任务进入对应子图。

当前图谱基线：

- GitNexus 索引提交：`5dacc96`
- 索引时间：`2026-05-20T11:58:23.450Z`
- 统计概览：`1368` 文件，`24,820` 节点，`56,380` 关系，`921` 聚类，`300` 条流程

## 本轮更新重点

- 新增网盘备份子图：`gateway/pan/*` 已形成 admin-only Baidu Pan 备份/恢复系统，覆盖 OAuth、token 加密、BackgroundTask、backup/restore/residue/stale 状态机、调度器、Admin UI、pan.* observability 与通知。
- Job 状态扩展到 `archiving / archived / restoring`，`BackupRecord` 承担备份生命周期真源，`BackgroundTask` 只代表调度状态。
- 存储/交付面新增“归档到网盘后删除本地 project_dir 与 R2 artifacts，恢复时不自动回推 R2”的边界。
- Smart 2026-05-20 产品语义改为全自动：translation review 的 6 项 deterministic check 只产出审计 metrics，不再触发人工 handoff；保留的暂停/退出只限硬限制、弱音色确认和内容合规。
- Admin 内容合规命中现在是 notify-only-no-pipeline-block；非 admin 仍由早期合规 gate 失败退出。
- Pan 事件进入 `gateway/storage/event_log.py` 与 `scripts/r2_observability.py`，token revoked / backup failed / restore failed 进入通知 dispatch map。
- 成本与模型配置继续推进：Smart mode 默认 Gemini 3.1 Pro；当前工作区成本目录新增 Gemini 3.5 Flash，Gemini 3.1 Pro RMB-direct 价格按 2026-05-20 官方 tier 固化。

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
12. 想看百度网盘备份、归档/恢复、BackupRecord 状态机、Pan schedulers、pan.* observability，读 [GITNEXUS_PAN_BACKUP_GRAPH.md](./GITNEXUS_PAN_BACKUP_GRAPH.md)

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
- [GITNEXUS_PAN_BACKUP_GRAPH.md](./GITNEXUS_PAN_BACKUP_GRAPH.md)：网盘备份图。聚焦 admin pan API、Baidu OAuth、BackgroundTask、BackupRecord、backup/restore 状态机、scheduler、residue cleanup、stale reaper、pan.* observability 与通知。

## 什么时候优先先看图

- 对仓库不熟，先看总图，再看对应子图。
- 要改 `src/pipeline/process.py`、`src/services/smart/*`、`gateway/smart_consent.py`、`gateway/user_voice_api.py`、`gateway/user_voice_service.py`、`gateway/voice_selection_api.py`、`src/services/llm_registry.py`、`src/services/jobs/editing_segments.py`、`src/services/jobs/editing_split_suggest.py`、`gateway/admin_disk_api.py`、`gateway/admin_cost_api.py`、`gateway/pan/*`，先看图再读源码。
- 要判断 Smart 为什么提交被拒、为什么复用/暂停/拒绝候选音色、为什么没有 quality report、为什么 cost 只在 admin 可见、为什么 Smart 完成后能进入修改、为什么编辑分割需要重合成、为什么网盘备份卡在 archiving/restoring，先看对应图谱。
