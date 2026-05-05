# 图谱索引

新会话建议先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)，再按任务进入对应子图。

当前图谱基线：
- GitNexus 索引提交：`482b315`
- 索引时间：`2026-05-06`
- 统计概览：`1057` 文件、`18,993` 节点、`46,249` 关系、`822` 聚类、`300` 条流程

## 这轮更新重点

- `deliverable-time whisper alignment` 已经形成稳定 sidecar：Jianying 草稿和 `materials_pack` 共用 `ensure_whisper_aligned_subtitles`
- `cue_pipeline` 现在有完整的 `env capability + admin policy` 双闸门，以及 `publish / deliverable / manual` trigger 语义
- `tts_input_cn_text` 已成为“当前音频是否与当前中文文本同步”的显式 drift 见证；post-edit commit 会在 draft wav promoted 时重打标
- `JianyingDraftRunner` 的 fingerprint 已纳入 whisper policy snapshot；`skip_cache=true` 还会绕过外层 `succeeded` cache-hit
- Gateway admin 面已经扩成 `whisper` 设置组 + `traffic analytics` + `materials_pack` 预打包 whisper delegation
- overwrite commit 现在会同时失效两类交付副产物：Jianying draft 和 Gateway 侧 `materials_pack`

## 使用顺序

1. 总览先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)
2. 想看 `SemanticBlock -> TTS -> DSP-first alignment -> cue_pipeline -> deliverables`，读 [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)
3. 想看 Studio 成功任务如何进入剪映草稿交付，以及 `fingerprint / skip_cache / orphan rescue`，读 [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)
4. 想看 review gate、speaker edits、voice selection、resume 语义，读 [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)
5. 想看 post-edit、单段重生成、`overwrite / copy_as_new`、以及 commit 对交付物的影响，读 [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)
6. 想看 `publish.dubbed_video`、`materials_pack`、`editor.jianying_draft_zip`、下载白名单、R2 / local fallback，读 [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)
7. 想看 marketing narrative、剪映草稿承诺、SSR 套餐真源、auth/captcha 前门，读 [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)
8. 想看 admin settings、traffic analytics、credits observability、retention cleanup、orphan diagnosis，读 [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)
9. 想看 `UsageMeter`、attempt-level LLM/TTS audit、`user_edit_events.jsonl`、quality/cost sidecar，读 [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)

## 文件说明

- [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)：项目总图。适合第一次进入仓库时快速建立“营销前门 / Auth / Gateway / Job API / Workflow / Delivery / Audit sinks”的总结构。
- [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)：工作流内核图。聚焦 `SemanticBlock`、`cue_pipeline`、`tts_input_cn_text` drift gate、以及 deliverable-time whisper alignment 侧路。
- [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)：剪映草稿交付图。聚焦 `generate-jianying-draft`、`JianyingDraftRunner`、`aligning_subtitles` 子步骤、fingerprint、cache hit/miss、以及 `user_draft_root`。
- [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)：审核流图。聚焦 `reviewGate`、`WorkspacePage`、`TranslationReviewPanel`、`VoiceSelectionPanel`、resume 语义。
- [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)：编辑 / 后处理图。聚焦 `VideoEditPage`、`editor/editing/`、segment regenerate、`overwrite / copy_as_new`、`tts_input_cn_text` commit stamp、以及 deliverable invalidation。
- [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)：存储与交付图。聚焦 `materials_pack`、`generate_video`、`editor.jianying_draft_zip`、manifest resolve、R2 / local fallback、以及 pre-pack whisper ensure。
- [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)：商业化图。聚焦 marketing promise、`/api/plans` SSR、剪映草稿承诺、FAQ JSON-LD、以及 captcha-backed auth 前门。
- [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)：控制平面图。聚焦 admin whisper settings、traffic analytics、credits / costs、cleanup、runner orphan diagnosis、audit failure alarms。
- [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)：质量与成本图。聚焦 `UsageMeter`、attempt-level events、`user_edit_events.jsonl`、quality tier、live reserve/capture/release、margin read model。

## 什么情况下先看图

- 对仓库不熟，先看总图，再看对应子图。
- 要改 `cue_pipeline.py`、`ensure_whisper_alignment.py`、`editing_commit.py`、`jianying_draft_runner.py`、`background_task_executors.py`、`traffic_analytics.py`、或营销 / auth 前门，优先先看图再读源码。
- 要判断 `deliverable-time whisper alignment`、`tts_input_cn_text` drift、`skip_cache`、`materials_pack` 失效、`traffic analytics` 边界时，优先先看图。
