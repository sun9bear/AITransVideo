# UI-08 · 共享 UI 收尾英文化（枚举占位）

> **状态：☐ 枚举占位（决策门）** — 待项目主在[主方案 §9.2](../2026-06-25-ui-page-locale-switch-plan.md) 决策 Phase 3 后细化。**现在不实施、不细化。**

- **目标 / 价值**：闭合共享 UI 原语的本地化（auth/captcha 已在 [UI-04](UI-04-min-auth-en.md) 完成，不在本单元）。
- **关联**：主方案 Phase 3 · Task 3.3。
- **建议分支**：`uiloc/shared-ui`
- **前置依赖**：UI-02、UI-01。（跟随 Phase 2 / UI-05~07。）
- **决策门**：§9.2。
- **预估工时**：M

## 范围概要（待细化）

- shadcn a11y 串（`Close`/`Toggle Sidebar`/`Sidebar`/`Displays the mobile sidebar` — 今天就是英文，统一进字典）。
- `confirm-dialog.tsx` 默认 props（`请确认`/`确定`/`取消` 改 locale-aware）、`empty-state.tsx`（`页面提示`）、`log-viewer.tsx`（默认 props + 级别 map `错误/信息/提醒` + 计数模板 ICU）。
- `session-provider.tsx` 内联错误（`登录状态加载失败，请重试。`）。
- `components/support/**` 残留内联中文（~10 文件 110 处，把仍内联的 banner 收进 `support-copy.ts` 风格字典）。

## 必守不变量

- 红线 1 默认 zh 字节一致。

## 细化条件

Phase 3 启动时细化。
