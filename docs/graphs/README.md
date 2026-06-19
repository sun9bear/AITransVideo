# 图谱索引

新会话建议先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)，再按任务进入对应子图。

当前图谱基线：

- GitNexus 索引提交：`ecadcb9f`
- 索引时间：`2026-06-16T11:36:00+08:00`
- 统计概览：`1737` 文件，`32,849` 节点，`76,841` 关系，`1353` 聚类，`300` 条流程

## 本轮更新重点

- 匿名预览漏斗成型：marketing trial panel、anonymous session、direct/chunked upload、admission、probe、stream-only teaser、post-login claim 与 `reuse_anonymous_preview_id` 转完整进入独立子图。
- Smart Preview clone/billing 成型：3 分钟 teaser 可在 admin gate 后走 MiniMax clone，600 点 reservation/capture/release/carryover 避免预览转完整重复扣点。
- Chunked upload 进入 Gateway 控制面：支持匿名上传 TTL、daily GB、disk floor、per-user/global inflight 等限流，仍只是传输层，不是新存储真源。
- 商业化支付面扩展到 Paddle MoR、WeChat Native、refund closure 和 billing reconciliation；fake payment 仍由生产门禁保护。
- 语言字段与 registry 进入 job model：source/target language facts 由 Gateway/API 暴露，前端消费事实而不是重建语言规则。
- 编辑面新增 bulk replace：post-edit 可做批量文本替换，仍要通过编辑态同步、commit 与交付物失效边界。
- Admin/Ops 新增 APF、chunked upload、Smart preview clone、payment reconciliation、rotating logs/watchdog 等运维面。
- 旧 Free tier、Express auto-clone、R2/Pan、Smart analytics 图谱保留，并补上与匿名预览和 Smart preview 的新边界。

## 使用顺序

1. 总览先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)
2. 想看 `SemanticBlock -> TTS -> DSP-first alignment -> cue_pipeline -> deliverables`，读 [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)
3. 想看 CosyVoice clone、国内 worker、HMAC RPC、source segment sample、worker routing、用户显式付费克隆，读 [GITNEXUS_COSYVOICE_MAINLAND_WORKER_GRAPH.md](./GITNEXUS_COSYVOICE_MAINLAND_WORKER_GRAPH.md)
4. 想看 Express 快捷版自动克隆、availability、consent、reservation、临时音色 cleanup，读 [GITNEXUS_EXPRESS_COSYVOICE_AUTO_CLONE_GRAPH.md](./GITNEXUS_EXPRESS_COSYVOICE_AUTO_CLONE_GRAPH.md)
5. 想看 Smart 自动审核、降级到 Studio、P5 possible-match auto-reuse、candidate-first voice policy、quality report、cost summary、user voice quota，读 [GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md](./GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md)
6. 想看未登录匿名预览、direct/chunked upload、APF admission、stream-only teaser、claim 与转完整，读 [GITNEXUS_ANONYMOUS_PREVIEW_FUNNEL_GRAPH.md](./GITNEXUS_ANONYMOUS_PREVIEW_FUNNEL_GRAPH.md)
7. 想看 Smart Preview 3 分钟 teaser、MiniMax clone、600 点 reservation/carryover 与预览转完整，读 [GITNEXUS_SMART_PREVIEW_CLONE_BILLING_GRAPH.md](./GITNEXUS_SMART_PREVIEW_CLONE_BILLING_GRAPH.md)
8. 想看 Studio / Smart 成功任务如何进入剪映草稿交付，读 [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)
9. 想看 review gate、speaker edits、candidate-first voice selection、Smart handoff、Smart 决策摘要，读 [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)
10. 想看 Smart/Studio 修改入口、editing speakers、split-many、智能切点、bulk replace、segment regenerate、batch re-TTS、overwrite / copy-as-new，读 [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)
11. 想看 `publish.dubbed_video`、`materials_pack`、`editor.jianying_draft_zip`、R2 registry、lazy fallback、sweeper、parity cleanup、anonymous preview stream-only，读 [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)
12. 想看 pricing / trial / phone auth / email auth / Smart / Smart Preview / Express / Free entitlement truth / Paddle / WeChat / payment reconciliation / CSRF，读 [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)
13. 想看免费档入口、voice-rights consent、日配额、MiMo voiceclone、时长限制、水印和下载限制，读 [GITNEXUS_FREE_TIER_GRAPH.md](./GITNEXUS_FREE_TIER_GRAPH.md)
14. 想看帮助中心、客服浮窗、通知中心、系统公告、新注册用户 live 分发与支持面 CSRF/polling，读 [GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md](./GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md)
15. 想看 admin settings、APF、chunked upload、Smart preview clone caps、Smart voice policy、Smart analytics、report analysis、mainland worker health、Express reservation/cleanup、payment reconciliation、CSRF、polling governance、voice calibration、cost management、admin disk resize、cleanup、R2 sweeper 与 parity 运维面，读 [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)
16. 想看 `UsageMeter`、voice clone/reuse/rejection audit、Smart preview 600 点 offset、RMB-direct pricing、MiMo v2.5/voiceclone usage/cost、Smart analytics、Phase 1a/1b reports、Smart sidecar、worker billed chars、`smart_shadow_eval / sim`、quality / cost / margin，读 [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)
17. 想看百度网盘备份、归档/恢复、BackupRecord 状态机、Pan schedulers、pan.* observability，读 [GITNEXUS_PAN_BACKUP_GRAPH.md](./GITNEXUS_PAN_BACKUP_GRAPH.md)

## 文件说明

- [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)：项目总图。适合第一次进入仓库时快速建立“前门 / Gateway / Job API / Workflow / Smart / Review / Editing / Delivery / Ops”整体结构。
- [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)：工作流内核图。聚焦 `SemanticBlock`、DSP-first 对齐、Smart inline branch、candidate-first voice policy、voice_id 传播、paid fallback、cue pipeline、deliverable-time whisper sidecar 与 Phase 1a/1b report sidecars。
- [GITNEXUS_COSYVOICE_MAINLAND_WORKER_GRAPH.md](./GITNEXUS_COSYVOICE_MAINLAND_WORKER_GRAPH.md)：CosyVoice / Mainland Worker 图。聚焦用户显式 clone、clone-gate、source_segments 样本拼接、OSS/R2 uploader、HMAC worker、worker routing、`requires_worker` TTS dispatch 与付费 API fail-closed 边界。
- [GITNEXUS_EXPRESS_COSYVOICE_AUTO_CLONE_GRAPH.md](./GITNEXUS_EXPRESS_COSYVOICE_AUTO_CLONE_GRAPH.md)：Express CosyVoice Auto-Clone 图。聚焦快捷版 availability、consent、atomic reservation、pipeline 自动克隆、临时音色入库、TTL reservation 回收、到期临时音色 cleanup 和手动 CLI。
- [GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md](./GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md)：Smart 自动审核图。聚焦 deterministic eligibility、consent、translation review、candidate-first voice reuse/clone/preset orchestration、P5 possible-match auto-reuse、quota/balance exhaustion、handoff、quality report、cost summary。
- [GITNEXUS_ANONYMOUS_PREVIEW_FUNNEL_GRAPH.md](./GITNEXUS_ANONYMOUS_PREVIEW_FUNNEL_GRAPH.md)：Anonymous Preview Funnel 图。聚焦匿名试用、direct/chunked upload、APF admission、probe/compliance、stream-only teaser、claim 与 `reuse_anonymous_preview_id` 转完整。
- [GITNEXUS_SMART_PREVIEW_CLONE_BILLING_GRAPH.md](./GITNEXUS_SMART_PREVIEW_CLONE_BILLING_GRAPH.md)：Smart Preview Clone / Billing 图。聚焦 Smart 预览、MiniMax clone、600 点 reservation/capture/release/carryover、预览转完整抵扣与 stream-only 边界。
- [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)：剪映草稿交付图。聚焦 `generate-jianying-draft`、`JianyingDraftRunner`、`substep`、`fingerprint`、`display_name`、claim guard、orphan rescue。
- [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)：审核流图。聚焦 `WorkspacePage`、review gate、translation/voice panels、candidate-first voice selection、Smart handoff 后重新进入 Studio，以及 Smart 决策摘要面板。
- [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)：编辑 / 后处理图。聚焦 Smart/Studio 修改入口、editing speakers registry、SegmentRow / ops panel、multi-cut split、智能切点、single/batch re-TTS、克隆/复用音色、`editing_audio_sync_required`、overwrite/copy-as-new、交付物失效。
- [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)：存储与交付图。聚焦 `materials_pack`、`editor.jianying_draft_zip`、`r2_artifacts`、`r2_artifact_sweeper`、`job_terminal_mirror`、R2 parity cleanup。
- [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)：商业化图。聚焦 Gateway plan truth、套餐事实、trial、支付、fake payment production gate、CSRF、phone/email auth 前门、Smart fixed price 与 entitlements。
- [GITNEXUS_FREE_TIER_GRAPH.md](./GITNEXUS_FREE_TIER_GRAPH.md)：Free Tier 图。聚焦 `service_mode=free`、feature flag、voice-rights consent、daily quota ledger、MiMo voiceclone、paid API guard、10 分钟上限、水印与下载限制。
- [GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md](./GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md)：支持 / 通知图。聚焦帮助中心、客服会话、通知中心、popup feed、系统公告、人工接管、支持面 CSRF 与 visibility-aware polling。
- [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)：Admin / Ops / Calibration 图。聚焦 alignment / whisper settings、Smart LLM model config、Smart voice policy、Smart analytics、report analysis、Phase 1b flags、CSRF、polling governance、voice calibration、traffic / cost / cleanup、admin disk cleanup/resize、R2 sweeper / parity / observability。
- [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)：质量与成本图。聚焦 `UsageMeter`、attempt 级事件、Smart sidecar、Smart analytics、Phase 1a/1b reports、voice reuse/clone/rejection metering、RMB-direct LLM pricing、edit audit、shadow eval/sim、quality / cost 报告。
- [GITNEXUS_PAN_BACKUP_GRAPH.md](./GITNEXUS_PAN_BACKUP_GRAPH.md)：网盘备份图。聚焦 admin pan API、Baidu OAuth、BackgroundTask、BackupRecord、backup/restore 状态机、scheduler、residue cleanup、stale reaper、pan.* observability 与通知。

## 什么时候优先先看图

- 对仓库不熟，先看总图，再看对应子图。
- 要改 `src/pipeline/process.py`、`src/services/smart/*`、`src/services/express/*`、`src/services/anonymous_preview_*`、`src/services/mainland_worker/*`、`src/services/tts/mimo_tts_provider.py`、`src/services/tts/voiceclone_reference.py`、`src/services/r2_publisher_lib/downloadable_keys.py`、`src/services/runtime_flags.py`、`src/services/translation_quality.py`、`src/services/speaker_evidence.py`、`gateway/csrf.py`、`gateway/admin_smart_analytics_api.py`、`gateway/smart_consent.py`、`gateway/free_consent.py`、`gateway/free_service_quota.py`、`gateway/anonymous_preview_*`、`gateway/smart_clone_reservation_service.py`、`gateway/smart_clone_reservation_sweeper.py`、`gateway/chunked_upload_*`、`gateway/cosyvoice_clone/*`、`gateway/mainland_voice_worker.py`、`gateway/express_reservation_service.py`、`gateway/express_voice_cleanup_service.py`、`gateway/user_voice_api.py`、`gateway/user_voice_service.py`、`gateway/voice_selection_api.py`、`src/services/llm_registry.py`、`src/services/jobs/editing_segments.py`、`src/services/jobs/editing_split_suggest.py`、`src/services/jobs/editing_bulk_replace.py`、`gateway/admin_disk_api.py`、`gateway/admin_cost_api.py`、`gateway/cost_management.py`、`gateway/payment_provider_paddle.py`、`gateway/payment_provider_wechat.py`、`gateway/billing_reconciliation.py`、`gateway/pan/*`，先看图再读源码。
- 要判断 Smart 为什么提交被拒、Smart Preview 为什么没有自动克隆、600 点为什么 capture/release/carryover、匿名预览为什么被限流或不能转完整、chunked upload 为什么被拒、free 为什么不可用或 403、free 日配额为什么占用、free voiceclone 为什么 fallback、免费视频为什么没水印、free 下载为什么缺 materials/editor draft、Express 为什么没自动克隆、reservation 为什么没释放、临时音色为什么没 cleanup、为什么自动复用 possible-match、为什么复用/暂停/拒绝候选音色、为什么 CosyVoice clone-gate ready 但 POST 失败、为什么 worker routing 没传到 TTS、为什么 MiniMax balance 触发暂停、为什么 Paddle/WeChat/reconciliation 状态不一致、为什么 MiMo 成本为 promotional/missing-rate、为什么没有 quality report、为什么 cost 只在 admin 可见、为什么 report analysis 的 flags 没生效、为什么 CSRF 拦截写请求、为什么 Smart 完成后能进入修改、为什么 bulk replace 后需要重新提交、为什么编辑分割需要重合成、为什么网盘备份卡在 archiving/restoring，先看对应图谱。
