# UI-07 · Intl 日期/数字格式器参数化（枚举占位）

> **状态：☐ 枚举占位（决策门）** — 待项目主在[主方案 §9.2](../2026-06-25-ui-page-locale-switch-plan.md) 决策 Phase 2 后细化。**现在不实施、不细化。**

- **目标 / 价值**：把硬编码 `zh-CN` 的格式器换成 active locale，避免英文界面里仍渲染中文格式日期。
- **关联**：主方案 Phase 2 · Task 2.3；§0.2「硬编码 locale 格式器」。
- **建议分支**：`uiloc/intl-formatters`
- **前置依赖**：UI-02（active locale 可读）、UI-01。
- **决策门**：§9.2 Q1/Q2。
- **预估工时**：S

## 范围概要（待细化）

- 替换 `new Intl.DateTimeFormat('zh-CN')` / `toLocaleDateString('zh-CN')`（出现在 `projects/page.tsx`、`settings/page.tsx` 等）为读 active locale 的格式器。
- 货币格式（`¥`/CNY）保持真值不变，只按 locale 调标签/单位（与 UI-03 货币策略一致，主方案 §1.6）；**不做币种换算**。

## 必守不变量

- 红线 1 默认 zh 字节一致（zh locale 下格式输出必须与今天一致）。

## 细化条件

§9.2 拍板后细化；可与 UI-06 同分支或独立小 PR。
