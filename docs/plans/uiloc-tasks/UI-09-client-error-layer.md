# UI-09 · 客户端错误层集中 + error-code map 推广（枚举占位）

> **状态：☐ 枚举占位（决策门）** — 待项目主在[主方案 §9.2](../2026-06-25-ui-page-locale-switch-plan.md) 决策 Phase 3 后细化。**现在不实施、不细化。**

- **目标 / 价值**：把分散的客户端错误兜底集中到单一可本地化模块，并推广「server error-code → 本地化串」map，为 Phase 4 后端 error-code 衔接做好前端侧承接。
- **关联**：主方案 Phase 3 · Task 3.4；§1.9。
- **建议分支**：`uiloc/client-error-layer`
- **前置依赖**：UI-02、UI-01；**与 [UI-BE-01](UI-BE-01-backend-error-codes.md)（Phase 4）耦合**——前端 code→串映射要等后端发 code 才真正生效。
- **决策门**：§9.2。
- **预估工时**：M

## 范围概要（待细化）

- 集中 `lib/api/client.ts`（`statusFallbackMessage`/timeout/`stringifyErrorDetail`）+ `lib/api/errors.ts` 默认 + 各调用点 `|| "<中文>"` 兜底到单一可本地化模块。
- 推广 `password-login-form.tsx` 的 `LOGIN_ERROR_MESSAGES` 成共享「server error-code → 本地化串」map；raw `detail` 降为最后兜底（衔接 UI-BE-01）。

## 必守不变量

- 红线 1 默认 zh 字节一致。
- **诚实记录**：未编码的后端错误在 UI-BE-01 之前仍漏中文——DoD 必须显式标注此已知缺口，不得伪装成「已全英文」。

## 细化条件

Phase 3 启动 + UI-BE-01 排期确定后细化。
