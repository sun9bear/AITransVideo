# UI-05 · App 中央字典本地化（枚举占位）

> **状态：☐ 枚举占位（决策门）** — 本单元待项目主在[主方案 §9.2](../2026-06-25-ui-page-locale-switch-plan.md) 决策「英文是否进核心工作台（Phase 2）」后，再细化为可执行 Step。**现在不实施、不细化。**

- **目标 / 价值**：把全 App 状态/阶段/错误 chrome 的「中央字典」本地化——这是 App 层最高杠杆点（翻 ~4 个文件即覆盖大部分状态/阶段/错误显示）。
- **关联**：主方案 Phase 2 · Task 2.1；§0.2「App 层中央字典 seam」。
- **建议分支**：`uiloc/central-dictionaries`
- **前置依赖**：UI-02（`[locale]` 路由）、UI-01（catalog）。
- **决策门**：主方案 §9.2 Q1/Q2（是否做工作台英文）。
- **预估工时**：M

## 范围概要（待细化）

- `frontend-next/src/types/jobs.ts` → `JOB_STATUS_LABELS`（11 状态）。
- `frontend-next/src/features/jobs/presentation.ts` → `stageLabels`（9 阶段）/ `reviewStageDescriptions` / 错误分类 `{label,suggestion}` / `sanitizedProgressMessages`（**必须保留 null-过滤语义**：含 `Web UI`/`fallback`/`legacy` 仍返回 null）/ 一组 `getStageLabel`/`getReviewPrompt`/`getErrorCategory`/`getJobDisplayTitle` helper。
- `frontend-next/src/features/jobs/stageMetadata.ts` → 阶段描述。
- `frontend-next/src/features/jobs/expiry.ts` → `即将删除`/`N 天后过期`（**ICU 复数**，不拼接）。
- `frontend-next/src/components/status-badge.tsx` → 内联 `重合成中 · 第 N 次修改`（ICU）。

## 必守不变量

- 红线 1 默认 zh 字节一致；红线 5 content 不译（`getJobDisplayTitle` 的 `未命名视频` fallback、`YouTube 视频 ·` 前缀是 chrome，job 标题本身是 content 透传）；保留 `sanitizedProgressMessages` 的内部串 null-过滤。

## 细化条件

项目主在 §9.2 拍板「做工作台英文」后：补 Step 0 现状 + 分步 + DoD + 回滚。**注**：UI-05 与 UI-06 同属 Phase 2，需与 **TU-11**（语音选择共享）/ 产品 **PR #38**（target_language）协调文件 owner（主方案 §0.5/§0.6）。
