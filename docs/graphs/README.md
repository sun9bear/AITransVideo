# 图谱索引

新会话建议先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)，再按任务进入对应子图。

当前图谱基线：
- GitNexus 索引提交：`2128003`
- 索引时间：`2026-05-10`
- 统计概览：`1215` 文件、`21,691` 节点、`49,871` 关系、`807` 聚类、`300` 条流程

## 这轮更新重点

- `voice CPS auto-calibration` 已经形成完整控制面：手动校准、clone 后自动校准、review 提交前预热校准三条入口并存。
- editing 面不再只有 segment mutate 和 commit，还长出了独立的 `speakers.json`、speaker voice profile 推断、`preview-source` 回放侧路。
- 存储与交付面已经从“下载时再想办法取文件”升级成 `proactive R2 publisher + terminal mirror + edit_generation-scoped registry`。

## 使用顺序

1. 总览先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)
2. 想看 `SemanticBlock -> TTS -> DSP-first alignment -> cue_pipeline -> deliverables`，读 [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)
3. 想看 Studio 成功任务如何进入剪映草稿交付，以及 `attempt_id / substep / fingerprint / claim guard`，读 [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)
4. 想看 review gate、speaker edits、voice selection、review-submit calibration preflight、resume 语义，读 [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)
5. 想看 editing speakers、voice profile inference、segment regenerate、`overwrite / copy_as_new`、`editing_audio_sync_required`、以及交付物失效，读 [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)
6. 想看 `publish.dubbed_video`、`materials_pack`、`editor.jianying_draft_zip`、R2 registry redirect、lazy fallback、sweeper 与 terminal mirror，读 [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)
7. 想看 pricing / trial / auth 前门 / SEO / plan truth，读 [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)
8. 想看帮助中心、客服浮窗、通知中心、系统公告与新注册用户 live 分发，读 [GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md](./GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md)
9. 想看 admin settings、voice calibration control plane、traffic analytics、cost management、cleanup、R2 sweeper 运维面，读 [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)
10. 想看 `UsageMeter`、attempt-level audit、`user_edit_events.jsonl`、`smart_shadow_eval / sim`、quality / cost / margin，读 [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)

## 文件说明

- [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)：项目总图。适合第一次进入仓库时快速建立“前门 / Gateway / Job API / Workflow / Review / Editing / Delivery / Ops”整体结构。
- [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)：工作流内核图。聚焦 `SemanticBlock`、DSP-first 对齐、paid fallback、cue pipeline、deliverable-time whisper sidecar。
- [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)：剪映草稿交付图。聚焦 `generate-jianying-draft`、`JianyingDraftRunner`、`substep`、`fingerprint`、`display_name`、claim guard、orphan rescue。
- [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)：审核流图。聚焦 `WorkspacePage`、review gate、translation/voice panels、voice selection、review-submit calibration preflight。
- [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)：编辑 / 后处理图。聚焦 editing speakers registry、speaker profile 推断、segment regenerate、`editing_audio_sync_required`、overwrite/copy-as-new、交付物失效。
- [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)：存储与交付图。聚焦 `materials_pack`、`editor.jianying_draft_zip`、`r2_artifacts`、`r2_artifact_sweeper`、`job_terminal_mirror`、R2 / local fallback。
- [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)：商业化图。聚焦 Gateway plan truth、套餐事实、trial、支付与 auth 前门。
- [GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md](./GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md)：支持 / 通知图。聚焦帮助中心、客服会话、通知中心、popup feed、系统公告、人工接管。
- [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)：Admin / Ops / Calibration 图。聚焦 alignment / whisper settings、voice calibration、traffic / cost / cleanup、sweeper 与运维诊断。
- [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)：质量与成本图。聚焦 `UsageMeter`、attempt 级事件、edit audit、shadow eval/sim、quality / cost 报告。

## 什么时候优先先看图

- 对仓库不熟，先看总图，再看对应子图。
- 要改 `voice_selection_api.py`、`voice_calibration_review_preflight.py`、`editing_commit.py`、`editing_speakers.py`、`r2_artifact_sweeper.py`、`job_terminal_mirror.py`、`backend_router.py`，先看图再读源码。
- 要判断 review-submit 为什么会先校准、editing speaker profile 为什么会异步推断、R2 为什么会主动发布、旧草稿为什么会在 overwrite 后失效，先看图。
