# 图谱索引

新会话建议先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)，再按任务进入对应子图。

当前图谱基线：

- GitNexus 索引提交：`1934901`
- 索引时间：`2026-05-01`
- 统计概览：`961` 文件、`16,761` 节点、`40,645` 关系、`300` 条流程

## 使用顺序

1. 总览先读 [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)
2. 主流程、内容合规 gate、Draft-first、对齐后重发链路读 [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)
3. 审核 gate、speaker edits、voice selection quality tier、resume 语义读 [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)
4. Studio 修改、单段重合成、overwrite / copy_as_new 读 [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)
5. 下载、R2 重定向、local fallback、文件名派生读 [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)
6. 商业化、营销首页 narrative/proof、定价/试用 SSR、工作台点数预估/预扣、套餐、计费、支付、法律页读 [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)
7. Admin 定价、成本管理、credits 观测、保留期清理、S2 监控、日志分析、background tasks、voice calibration 任务读 [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)
8. metering、quality tier、live credit guard、价格目录、margin 估算、provider breakdown、benchmark sidecar 任务读 [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)

## 文件说明

- [GITNEXUS_PROJECT_GRAPH.md](./GITNEXUS_PROJECT_GRAPH.md)：项目总图，适合第一次进入仓库时快速建立整体结构感。
- [GITNEXUS_WORKFLOW_CORE_GRAPH.md](./GITNEXUS_WORKFLOW_CORE_GRAPH.md)：工作流内核，聚焦 `Ingestion -> Media -> Content Compliance -> Translation -> SemanticBlock -> TTS -> Alignment -> Draft`，以及对齐后重发与异步导出平面。
- [GITNEXUS_REVIEW_GRAPH.md](./GITNEXUS_REVIEW_GRAPH.md)：审核流图，聚焦 `reviewGate`、`WorkspacePage`、translation review 里的 speaker edits、`voice_selection_review` 与 gate/resume。
- [GITNEXUS_EDITING_POST_EDIT_GRAPH.md](./GITNEXUS_EDITING_POST_EDIT_GRAPH.md)：编辑后处理图，聚焦 `VideoEditPage`、`VoiceModifyTab`、`editor/editing/`、segment regenerate、`overwrite / copy_as_new`。
- [GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md](./GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md)：存储与交付图，聚焦 `projects` / `workspace` 下载表面、Gateway 路由决策、R2 redirect、local fallback、`display_name` 文件名派生。
- [GITNEXUS_COMMERCIALIZATION_GRAPH.md](./GITNEXUS_COMMERCIALIZATION_GRAPH.md)：商业化图，聚焦营销前门 narrative/proof、Gateway pricing/runtime 真源、workspace credit guard、plan/trial/credits/payment 与前端消费边界。
- [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](./GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)：控制平面子图，聚焦 admin pricing、admin costs、credits observability、retention cleanup、S2 monitor、job logs、background tasks、voice speed calibration。
- [GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md](./GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md)：基准与成本子图，聚焦 `UsageMeter`、`metering_snapshot`、quality tier、live reserve/capture/release、cost catalog、revenue/margin read model、cost/provider breakdown。

## 什么时候该看图谱

- 对仓库不熟，先看总图再看子图。
- 要动架构敏感代码，先看对应子图。
- 要判断内容合规 gate、review gate、editing buffer、下载路由、营销页如何消费套餐真源、live credit guard、TTL/`purged` 语义、Gateway 真源位置，优先看图谱再读源码。
