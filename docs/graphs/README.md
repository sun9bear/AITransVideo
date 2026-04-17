# 图谱索引

新会话建议先读 [GITNEXUS_PROJECT_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_PROJECT_GRAPH.md)，再按任务进入对应子图。

当前图谱基线：

- GitNexus 索引提交：`490cce8`
- 索引时间：`2026-04-16`
- 统计概览：`796` 文件、`13,529` 节点、`32,194` 关系、`300` 条流程

## 使用顺序

1. 总览先读 [GITNEXUS_PROJECT_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_PROJECT_GRAPH.md)
2. 主流程任务读 [GITNEXUS_WORKFLOW_CORE_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_WORKFLOW_CORE_GRAPH.md)
3. 商业化、套餐、计费、支付、settings 任务读 [GITNEXUS_COMMERCIALIZATION_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_COMMERCIALIZATION_GRAPH.md)
4. 审核、ReviewState、审核页、暂停恢复任务读 [GITNEXUS_REVIEW_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_REVIEW_GRAPH.md)
5. Admin 定价、S2 监控、credits 观测、日志分析、voice calibration 任务读 [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)

## 文件说明

- [GITNEXUS_PROJECT_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_PROJECT_GRAPH.md)：项目总图，适合第一次进入仓库时快速建立整体结构感。
- [GITNEXUS_WORKFLOW_CORE_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_WORKFLOW_CORE_GRAPH.md)：工作流内核，聚焦 `Ingestion -> Translation -> SemanticBlock -> TTS -> Alignment -> Retiming -> Draft`。
- [GITNEXUS_COMMERCIALIZATION_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_COMMERCIALIZATION_GRAPH.md)：商业化图，聚焦 Gateway pricing/runtime 真源、plan/trial/credits/payment 与前端消费边界。
- [GITNEXUS_REVIEW_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_REVIEW_GRAPH.md)：审核流图，聚焦 `speaker_review`、`translation_review`、`voice_selection_review`、`audio_alignment_review` 与 gate/resume。
- [GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md)：管理与运维子图，聚焦 admin pricing、S2 monitor、credits observability、job logs、voice speed calibration。

## 什么时候该看图谱

- 对仓库不熟，先看总图再看子图。
- 要动架构敏感代码，先看对应子图。
- 要判断模块边界、数据流、阶段顺序、Gateway 真源位置、admin/ops sidecar 位置，优先看图谱再读源码。
