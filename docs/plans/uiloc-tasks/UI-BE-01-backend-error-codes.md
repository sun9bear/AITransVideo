# UI-BE-01 · 后端 error-code envelope + Accept-Language（指针 · 跨栈后端轨）

> **状态：☐ 指针（独立后端轨，非 frontend uiloc lane）** — 本单元是**跨栈后端工作**，不在前端 `uiloc/` 车道内执行。归后端 backlog / 与代码质量·后端轨协调。**待项目主在[主方案 §9.2](../2026-06-25-ui-page-locale-switch-plan.md) Q7 排期。**

- **目标 / 价值**：让 UI 直接渲染的 server-emitted 中文（error `detail` / 邮件 / 客服 / 公告）可被本地化——这是前端阶段**无法独立解决**的部分（主方案 §1.9）。
- **关联**：主方案 Phase 4；§1.9；§9.2 Q7。
- **建议分支**：后端轨约定（非 `uiloc/`）。
- **前置依赖**：无（但与 [UI-09](UI-09-client-error-layer.md) 耦合：后端发 code，前端才能 code→串）。
- **决策门**：§9.2 Q7（是否/何时排期；**若做 Phase 2 工作台英文，高频路径的 code 必须与之同排**）。
- **预估工时**：L（跨栈、payment/auth 敏感、回归面大）。

## 范围概要（待细化，归后端）

- gateway HTTPException 加稳定 error-code envelope `{code, detail}`（现 ~239 处 `detail="<中文>"` 多为裸 prose 无 code），从高频用户路径起步：`auth.py`、`auth_email.py`、`job_intercept.py`、`billing.py`、`support_api.py`。
- 邮件模板（`auth_email.py _email_html`）、客服 chatbot 文案（`support_api.py`）、存储的公告/通知 → per-locale 模板或 Accept-Language。

## 必守不变量

- **付费 API 硬约束**：不得为本地化在 fallback/except/retry 引入自动付费调用。
- 后端改动不触碰 pipeline 语言字段语义（与产品 target_language 工作正交）。

## 细化条件

项目主排期后，归后端执行轨细化（与代码质量 TU 后端单元协调文件 owner）。**前端只负责 [UI-09] 的承接层。**
