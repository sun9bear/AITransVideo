# UI-06 · always-on 用户页英文化（枚举占位）

> **状态：☐ 枚举占位（决策门）** — 待项目主在[主方案 §9.2](../2026-06-25-ui-page-locale-switch-plan.md) 决策 Phase 2 后细化。**现在不实施、不细化。**

- **目标 / 价值**：让英文用户真正能用产品——翻译 always-on 的核心用户流页面。
- **关联**：主方案 Phase 2 · Task 2.2。（Intl 格式器参数化拆到 [UI-07](UI-07-intl-formatters.md)。）
- **建议分支**：`uiloc/app-user-flows`
- **前置依赖**：UI-05（中央字典）、UI-02、UI-01。
- **决策门**：§9.2 Q1/Q2；**且必须与 Phase 4（UI-BE-01）error-code 同排期**——否则工作台 toast/错误大面积漏中文（主方案 §3 Phase 2 行）。
- **预估工时**：L

## 范围概要（待细化）

- 创建流：`TranslationForm.tsx`（含民法典1023 consent，**人审**）。
- workspace 审校路径：`workspace/[jobId]/page.tsx`、`VoiceSelectionPanel`、`VoiceReviewPanel`、`TranslationReviewPanel`、`VoiceCloneModal`、Smart* 面板、`ResultMediaCard`。
- `projects/page.tsx`、`voices/page.tsx`、`settings`+`settings/billing`、`help/page.tsx`、`notifications`、`components/billing/**`。
- （日期/数字格式器参数化见 [UI-07](UI-07-intl-formatters.md)。）

## ⚠️ 协调（强制）

- **文件重叠**：`VoiceSelectionPanel` 等与 **TU-11**（代码质量·语音选择共享）重叠；`TranslationForm.tsx`/`mappers.ts`/`SegmentRow.tsx` 与产品 **PR #38**（target_language）重叠。**排在 TU-11 + PR #38 合并之后**，或与项目主约定非重叠 owner（主方案 §0.5/§0.6）。

## 必守不变量

- 红线 1/2/3/5；content（job 标题、转录、译文、voice 名、`display_title_zh`、说话人名）一律透传不译。

## 细化条件

§9.2 拍板 + TU-11/PR #38 落地后细化。
