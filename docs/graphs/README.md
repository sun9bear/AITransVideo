# 图谱索引

新会话建议先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)，再按任务进入对应子图。

当前图谱基线：

- GitNexus 索引提交：`83f4ddd`
- 索引时间：`2026-05-03`
- 统计概览：`1006` 文件、`17,730` 节点、`43,324` 关系、`741` 聚类、`300` 条流程

## 使用顺序

1. 总览先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)
2. 主流水线、字幕 cue v2、对齐后输出、按需剪映草稿的派生位置，读 [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)
3. Studio 成功任务如何进入剪映草稿生成、状态机如何跑、`user_draft_root` 如何影响绝对路径，读 [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)
4. review gate、speaker edits、voice selection、resume 语义，读 [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)
5. post-edit、单段重生成、`overwrite / copy_as_new`，读 [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)
6. `publish.dubbed_video`、`materials_pack`、`editor.jianying_draft_zip`、下载白名单、R2 / local fallback，读 [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)
7. marketing narrative / proof、剪映草稿承诺、套餐真源、workspace credit guard，读 [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)
8. admin pricing、credits observability、retention cleanup、后台任务、voice calibration，读 [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)
9. metering、quality tier、live credit guard、margin / provider breakdown，读 [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)

## 文件说明

- [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)：项目总图。适合第一次进入仓库时快速建立“营销前门 / Gateway / Job API / Workflow / Delivery”总结构。
- [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)：工作流内核图。聚焦 `SemanticBlock -> TTS -> Alignment -> Subtitle Cue V2 -> Editor outputs`，并说明剪映草稿位于主流水线之后的派生交付层。
- [GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md](./GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md)：剪映草稿交付图。聚焦 `job state -> runner -> gateway proxy -> user_draft_root -> zip/report -> result UI`。
- [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)：审核流图。聚焦 `reviewGate`、`WorkspacePage`、`TranslationReviewPanel`、`VoiceSelectionPanel`、resume 语义。
- [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)：编辑 / 后处理图。聚焦 `VideoEditPage`、`editor/editing/`、segment regenerate、`overwrite / copy_as_new`。
- [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)：存储与交付图。聚焦结果页下载、`editor.jianying_draft_zip`、`publish.dubbed_video`、R2 redirect、local fallback、下载白名单。
- [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)：商业化图。聚焦 marketing 前门 narrative/proof、Gateway pricing/runtime 真源、workspace credit guard、plan / trial / credits / payment。
- [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)：控制平面图。聚焦 admin pricing、admin costs、credits observability、retention cleanup、background tasks、voice calibration。
- [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)：质量与成本图。聚焦 `UsageMeter`、`metering_snapshot`、quality tier、live reserve/capture/release、margin read model。

## 什么情况下先看图

- 对仓库不熟，先看总图，再看对应子图。
- 要改 `OutputDispatcher`、`jobs/api.py`、`job_intercept.py`、workspace result surface、marketing narrative 这类架构敏感代码，先看图再读源码。
- 要判断字幕 cue v2、剪映草稿状态机、`user_draft_root` 绝对路径模式、下载白名单、Gateway 真源边界，优先先看图。
