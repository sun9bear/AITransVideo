# 图谱索引

新会话建议先读 [GITNEXUS_PROJECT_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_PROJECT_GRAPH.md)，再按任务进入对应子图。

## 使用顺序

1. 总览先读 [GITNEXUS_PROJECT_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_PROJECT_GRAPH.md)
2. 工作流任务读 [GITNEXUS_WORKFLOW_CORE_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_WORKFLOW_CORE_GRAPH.md)
3. 商业化、套餐、计费、权益任务读 [GITNEXUS_COMMERCIALIZATION_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_COMMERCIALIZATION_GRAPH.md)
4. 审核、ReviewState、审核页面、挂起恢复任务读 [GITNEXUS_REVIEW_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_REVIEW_GRAPH.md)

## 文件说明

- [GITNEXUS_PROJECT_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_PROJECT_GRAPH.md)：项目总图，适合第一次进入仓库时快速建立全局结构感。
- [GITNEXUS_WORKFLOW_CORE_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_WORKFLOW_CORE_GRAPH.md)：工作流内核，聚焦 `Ingestion -> Translation -> TTS -> Alignment -> Draft`。
- [GITNEXUS_COMMERCIALIZATION_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_COMMERCIALIZATION_GRAPH.md)：商业化图，聚焦营销页、账单页、Gateway 定价真源、积分与支付边界。
- [GITNEXUS_REVIEW_GRAPH.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/graphs/GITNEXUS_REVIEW_GRAPH.md)：审核流图，聚焦 `speaker_review`、`translation_review`、`voice_selection_review`、`ReviewStateManager` 与 pipeline gate。

## 什么时候该看图谱

- 对仓库不熟，先看总图再看子图。
- 要动架构敏感代码，先看对应子图。
- 要判断模块边界、数据流、阶段顺序、Gateway 真源位置，优先看图谱再读源码。
