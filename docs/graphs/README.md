# 图谱索引

新会话建议先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)，再按任务进入对应子图。

当前图谱基线：
- GitNexus 索引提交：`78eea96`
- 索引时间：`2026-05-09`
- 统计概览：`1185` 文件、`21,209` 节点、`51,229` 关系、`942` 聚类、`300` 条流程

## 这轮更新重点

- `support / notifications / announcements` 已经形成一条独立产品轴：`SupportWidget`、帮助中心、通知中心、管理员客服控制台、系统公告 live audience、以及新注册用户自动分发都已经落地
- workflow 对齐内核长出了真实的并行与控制面语义：`ThreadPoolExecutor + paid_fallback semaphore`、`force_dsp_alignment`、`capped_dsp_underflow -> high severity`
- 手机号登录注册路径已经重写成统一前门：直接 captcha 校验、`verify-code -> registration_token -> complete-registration`、trusted-proxy IP 边界、wrong-code attempt 限额
- `AppShell` 现在已经把 `NotificationBell`、popup modal、帮助中心入口、客服浮窗和管理员在线状态切换接进主应用壳层
- Jianying draft 与 post-edit 状态机继续加固：runner 的 substep / final write 走 `update_job` claim guard，overwrite 会清空 stale `attempt_id / substep / fingerprint`
- edit 侧新增了 `preview-source cache + stream endpoint`，帮助编辑界面直接回放原始段音频而不再每次现场切片

## 使用顺序

1. 总览先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)
2. 想看 `SemanticBlock -> TTS -> DSP-first alignment -> cue_pipeline -> deliverables`，读 [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)
3. 想看 Studio 成功任务如何进入剪映草稿交付，以及 `display_name / fingerprint / claim guard / orphan rescue`，读 [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)
4. 想看 review gate、speaker edits、voice selection、resume 语义，读 [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)
5. 想看 post-edit、单段重生成、`overwrite / copy_as_new`、`editing_audio_sync_required`、preview-source cache、以及 commit 对交付物的影响，读 [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)
6. 想看 `publish.dubbed_video`、`materials_pack`、`editor.jianying_draft_zip`、下载白名单、R2 / local fallback，读 [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)
7. 想看 pricing / trial 真源、手机号注册、trial 发放、auth noindex、以及新注册用户 onboarding 触点，读 [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)
8. 想看帮助中心、客服浮窗、通知中心、系统公告、人工接管、WeChat QR、以及新注册用户 live audience 分发，读 [GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md](./GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md)
9. 想看 admin settings、对齐控制面、客服管理、系统公告后台、traffic analytics、cost management、cleanup、orphan diagnosis，读 [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)
10. 想看 `UsageMeter`、attempt-level audit、`user_edit_events.jsonl`、`smart_shadow_eval / sim`、quality/cost 报告，读 [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)

## 文件说明

- [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)：项目总图。适合第一次进入仓库时快速建立“营销前门 / Auth / Gateway / Job API / Workflow / Delivery / Support / Offline evaluation / Audit sinks”的总结构。
- [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)：工作流内核图。聚焦 `SemanticBlock`、DSP-first + parallel alignment、`force_dsp` review 语义、deliverable-time whisper sidecar、以及 deterministic subtitle timing 边界。
- [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)：剪映草稿交付图。聚焦 `generate-jianying-draft`、`JianyingDraftRunner`、`aligning_subtitles` 子步骤、`display_name` 感知 fingerprint、claim guard、cache hit/miss、以及 `user_draft_root`。
- [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)：审核流图。聚焦 `reviewGate`、`WorkspacePage`、`TranslationReviewPanel`、`VoiceSelectionPanel`、resume 语义。
- [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)：编辑 / 后处理图。聚焦 `VideoEditPage`、`editor/editing/`、segment regenerate、`editing_audio_sync_required`、preview-source cache、`overwrite / copy_as_new`、`effective_marker.marked_event_ids`、以及 deliverable invalidation。
- [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)：存储与交付图。聚焦 `materials_pack`、`generate_video`、`editor.jianying_draft_zip`、manifest resolve、R2 / local fallback、以及 pre-pack whisper ensure。
- [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)：商业化图。聚焦 Gateway plan truth、手机号 auth / trial front door、公开 SEO 边界、以及新注册用户 lifecycle onboarding。
- [GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md](./GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md)：支持 / 通知图。聚焦帮助中心、`SupportWidget`、`support_service`、FAQ / plan facts / job-context 回答链、人工接管、公告 fan-out、通知中心与 popup feed。
- [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)：控制平面图。聚焦 alignment / whisper settings、客服管理、系统公告后台、traffic analytics、credits / costs、cleanup、runner orphan diagnosis、audit failure alarms。
- [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)：质量与成本图。聚焦 `UsageMeter`、attempt-level events、`user_edit_events.jsonl`、`effective_marker.marked_event_ids`、`smart_shadow_eval / sim`、quality/cost/margin 报告。

## 什么情况下先看图

- 对仓库不熟，先看总图，再看对应子图。
- 要改 `aligner.py`、`process.py`、`support_api.py`、`support_service.py`、`notifications_api.py`、`system_announcements_service.py`、`auth_phone.py`、`editing_commit.py`、`jianying_draft_runner.py`、或 `admin_support_api.py`，优先先看图再读源码。
- 要判断 `force_dsp` review 语义、`registration_token` 注册边界、通知 popup 与 bell 的关系、support AI 与人工接管边界、或 stale Jianying worker 为什么不能再覆盖 idle 记录时，优先先看图。
