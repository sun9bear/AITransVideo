# 图谱索引

新会话建议先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)，再按任务进入对应子图。

当前图谱基线：
- GitNexus 索引提交：`d56ac1c`
- 索引时间：`2026-05-14`
- 统计概览：`1256` 文件、`22,670` 节点、`52,154` 关系、`850` 聚类、`300` 条流程

## 这轮更新重点

- `Smart MVP P2` 已经形成独立自动审核轴：eligibility gate、auto translation review、auto voice review、handoff marker、smart state、sidecar audit、smart credit policy 都有清晰边界；`completed / downgraded_to_studio` 的 Smart job 也已接入 Studio editing 与 Jianying draft 门禁。
- Jianying draft 的 Smart 门禁已经补上 pre-lock 与 post-lock 双重检查，HTTP 层也会透传 runner 的真实拒绝 reason。
- email auth 已经接入 Gateway auth 前门：邮箱注册验证码、registration token、complete registration、password reset、fake/resend provider、rate limit 与 captcha 形成一条并行于 phone auth 的注册路径。
- R2 交付面继续前进：proactive publisher 之上新增 parity gate、cleanup 前 R2 HEAD 校验、download/stream observability 聚合脚本。
- editing 面新增批量 re-TTS 编排，继续保持“用户显式触发付费/重合成动作”的边界。

## 使用顺序

1. 总览先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)
2. 想看 `SemanticBlock -> TTS -> DSP-first alignment -> cue_pipeline -> deliverables`，读 [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)
3. 想看 Smart 自动审核、降级到 Studio、smart state、sidecar audit、credits policy，读 [GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md](./GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md)
4. 想看 Studio 成功任务如何进入剪映草稿交付，以及 `attempt_id / substep / fingerprint / claim guard`，读 [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)
5. 想看 review gate、speaker edits、voice selection、review-submit calibration preflight、resume 语义，读 [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)
6. 想看 editing speakers、voice profile inference、segment regenerate、batch re-TTS、`overwrite / copy_as_new`、`editing_audio_sync_required`，读 [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)
7. 想看 `publish.dubbed_video`、`materials_pack`、`editor.jianying_draft_zip`、R2 registry redirect、lazy fallback、sweeper、terminal mirror、parity cleanup，读 [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)
8. 想看 pricing / trial / phone auth / email auth / SEO / plan truth，读 [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)
9. 想看帮助中心、客服浮窗、通知中心、系统公告与新注册用户 live 分发，读 [GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md](./GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md)
10. 想看 admin settings、voice calibration、traffic analytics、cost management、cleanup、R2 sweeper 与 parity 运维面，读 [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)
11. 想看 `UsageMeter`、attempt-level audit、Smart sidecar、`smart_shadow_eval / sim`、quality / cost / margin，读 [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)

## 文件说明

- [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)：项目总图。适合第一次进入仓库时快速建立“前门 / Gateway / Job API / Workflow / Smart / Review / Editing / Delivery / Ops”整体结构。
- [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)：工作流内核图。聚焦 `SemanticBlock`、DSP-first 对齐、paid fallback、cue pipeline、deliverable-time whisper sidecar，以及 Smart effective mode 入口。
- [GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md](./GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md)：Smart 自动审核图。聚焦 deterministic eligibility、translation auto review、voice clone/preset orchestration、handoff marker、smart_state 与 Smart credits policy。
- [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)：剪映草稿交付图。聚焦 `generate-jianying-draft`、`JianyingDraftRunner`、`substep`、`fingerprint`、`display_name`、claim guard、orphan rescue。
- [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)：审核流图。聚焦 `WorkspacePage`、review gate、translation/voice panels、voice selection、Smart handoff 后重新进入 Studio。
- [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)：编辑 / 后处理图。聚焦 editing speakers registry、speaker profile 推断、single/batch re-TTS、`editing_audio_sync_required`、overwrite/copy-as-new、交付物失效。
- [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)：存储与交付图。聚焦 `materials_pack`、`editor.jianying_draft_zip`、`r2_artifacts`、`r2_artifact_sweeper`、`job_terminal_mirror`、R2 parity cleanup。
- [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)：商业化图。聚焦 Gateway plan truth、套餐事实、trial、支付、phone/email auth 前门。
- [GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md](./GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md)：支持 / 通知图。聚焦帮助中心、客服会话、通知中心、popup feed、系统公告、人工接管。
- [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)：Admin / Ops / Calibration 图。聚焦 alignment / whisper settings、voice calibration、traffic / cost / cleanup、R2 sweeper / parity / observability。
- [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)：质量与成本图。聚焦 `UsageMeter`、attempt 级事件、Smart sidecar、edit audit、shadow eval/sim、quality / cost 报告。

## 什么时候优先先看图

- 对仓库不熟，先看总图，再看对应子图。
- 要改 `src/services/smart/*`、`smart_wiring.py`、`auth_email.py`、`credits_service.py`、`r2_parity.py`、`project_cleanup.py`、`editing_batch.py`、`jianying_draft_runner.py`，先看图再读源码。
- 要判断 Smart 为什么降级到 Studio、邮箱注册为什么分成 verify 和 complete、项目清理为什么等 R2 parity、批量 re-TTS 为什么只扫 dirty segment，先看图。
