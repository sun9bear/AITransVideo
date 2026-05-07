# 图谱索引

新会话建议先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)，再按任务进入对应子图。

当前图谱基线：
- GitNexus 索引提交：`5c88213`
- 索引时间：`2026-05-07`
- 统计概览：`1080` 文件、`19,481` 节点、`47,352` 关系、`838` 聚类、`300` 条流程

## 这轮更新重点

- `smart_shadow_eval` 与 `smart_shadow_sim` 已形成稳定的离线影子评估平面：collector / analyzer / simulator / aggregator 读取项目工件、`UsageMeter`、`user_edit_events.jsonl`，输出真实质量、成本、行为报告
- `editing_commit.py` 现在会对“文本改了但没重生成 TTS”的 segment 直接抛出 `editing_audio_sync_required`，post-edit text/audio sync 已从软提示升级为 commit hard gate
- `effective_marker.marked_event_ids` 已成为“哪些用户意图最终存活到提交结果”的正式 sidecar 语义，供离线 join 与行为归因使用
- `JianyingDraftRunner` 的 fingerprint 已纳入 `display_name`；项目改名、S2 自动改名、用户改名都会让旧 draft zip 失效
- admin 成本面已经扩成 job-level `LLM / TTS / voice_clone` 成本与毛利读侧，`voice_clone` 首次 T2A 收费规则已进入控制面
- whisper 运行能力现在明确拆成两层：部署层的 `.[whisper] / INSTALL_WHISPER / HF_HOME`，以及运行时的 admin policy `enabled / trigger / skip_cache / model`

## 使用顺序

1. 总览先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)
2. 想看 `SemanticBlock -> TTS -> DSP-first alignment -> cue_pipeline -> deliverables`，读 [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)
3. 想看 Studio 成功任务如何进入剪映草稿交付，以及 `display_name / fingerprint / skip_cache / orphan rescue`，读 [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)
4. 想看 review gate、speaker edits、voice selection、resume 语义，读 [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)
5. 想看 post-edit、单段重生成、`overwrite / copy_as_new`、`editing_audio_sync_required`、以及 commit 对交付物的影响，读 [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)
6. 想看 `publish.dubbed_video`、`materials_pack`、`editor.jianying_draft_zip`、下载白名单、R2 / local fallback，读 [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)
7. 想看 marketing narrative、剪映草稿承诺、SSR 套餐真源、auth/captcha 前门，读 [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)
8. 想看 admin settings、whisper 部署能力、traffic analytics、cost management、cleanup、orphan diagnosis，读 [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)
9. 想看 `UsageMeter`、attempt-level audit、`user_edit_events.jsonl`、`smart_shadow_eval / sim`、quality/cost 报告，读 [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)

## 文件说明

- [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)：项目总图。适合第一次进入仓库时快速建立“营销前门 / Auth / Gateway / Job API / Workflow / Delivery / Offline evaluation / Audit sinks”的总结构。
- [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)：工作流内核图。聚焦 `SemanticBlock`、`cue_pipeline`、deliverable-time whisper sidecar、以及字幕交付前的二次校正边界。
- [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)：剪映草稿交付图。聚焦 `generate-jianying-draft`、`JianyingDraftRunner`、`aligning_subtitles` 子步骤、`display_name` 感知 fingerprint、cache hit/miss、以及 `user_draft_root`。
- [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)：审核流图。聚焦 `reviewGate`、`WorkspacePage`、`TranslationReviewPanel`、`VoiceSelectionPanel`、resume 语义。
- [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)：编辑 / 后处理图。聚焦 `VideoEditPage`、`editor/editing/`、segment regenerate、`editing_audio_sync_required`、`overwrite / copy_as_new`、`effective_marker.marked_event_ids`、以及 deliverable invalidation。
- [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)：存储与交付图。聚焦 `materials_pack`、`generate_video`、`editor.jianying_draft_zip`、manifest resolve、R2 / local fallback、以及 pre-pack whisper ensure。
- [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)：商业化图。聚焦 marketing promise、`/api/plans` SSR、剪映草稿承诺、FAQ JSON-LD、以及 captcha-backed auth 前门。
- [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)：控制平面图。聚焦 whisper capability + policy、traffic analytics、credits / costs、cleanup、runner orphan diagnosis、audit failure alarms。
- [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)：质量与成本图。聚焦 `UsageMeter`、attempt-level events、`user_edit_events.jsonl`、`effective_marker.marked_event_ids`、`smart_shadow_eval / sim`、quality/cost/margin 报告。

## 什么情况下先看图

- 对仓库不熟，先看总图，再看对应子图。
- 要改 `cue_pipeline.py`、`ensure_whisper_alignment.py`、`editing_commit.py`、`jianying_draft_runner.py`、`smart_shadow_eval_*`、`smart_shadow_sim_*`、`cost_management.py`、或 admin whisper 配置，优先先看图再读源码。
- 要判断 `display_name` 对 draft cache 的影响、`editing_audio_sync_required` 的边界、`marked_event_ids` 的离线语义、或 whisper 部署能力与 runtime policy 的分工时，优先先看图。
