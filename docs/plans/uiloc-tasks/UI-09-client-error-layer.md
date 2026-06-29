# UI-09 · 客户端错误层集中 + error-code map（已细化设计，2026-06-30）

> **状态：◐ 设计完成、待实施**。项目主 2026-06-29 放行 Phase 2/3（§9.2 Q2=是，备海外）。
> CodeX 审核建议：UI-09 **前置于**核心工作台铺文案——先把错误显示策略定死，否则英文 UI
> 在失败路径会突然掉中文/技术错误。本文档据真实 error 层（`lib/api/client.ts`）细化。

- **目标**：失败路径上，英文 UI 显示英文错误（而非后端中文 `detail` 或硬编码中文兜底）。
- **建议分支**：`uiloc/client-error-layer`（已建，stacked on `uiloc/app-user-flows` / UI-06 part1）。
- **前置**：UI-01/02（已合 main 流程中）；与 [UI-BE-01] 耦合但**不阻塞**——前端先承接已有 code + status，未编码错误降兜底（诚实记录漏中文，留 Phase 4）。

## 真实现状（Step 0 已核，`frontend-next/src/lib/api/client.ts`）

- `request()` 失败时消息解析顺序（client.ts:95-104）：`payload.message` → `stringifyErrorDetail(payload.detail)` → `payload.error` → `statusFallbackMessage(status)`。
- `stringifyErrorDetail`（:158）：结构化业务错误 `{error_code, message}` **当前丢弃 `error_code`、只取 `message`（后端中文）**；422 数组取首条 `msg`。
- `statusFallbackMessage`（:183）：401/403/404/5xx/timeout **硬编码中文**。
- timeout（:86）：硬编码中文「请求超时…」。
- `lib/api/errors.ts::getErrorMessage`：兜底「请求失败，请稍后重试。」（中文）。
- ⚠️ 主方案提到的 `password-login-form.tsx::LOGIN_ERROR_MESSAGES` **已不存在**（grep 0 命中）——「推广它」的前提作废，改为**新建**共享层。

## 架构决策（关键）

**本地化必须在「显示层」做，不在「抛出层」**：`client.ts` 不是 React 组件、拿不到 translator。所以：

1. **`ApiError` 携带 `errorCode`**：在 client.ts 失败分支提取 `payload.detail.error_code ?? payload.error_code ?? null` 存入 `ApiError`（保留 `message` 原文作最后兜底、保留 `status`）。这是唯一改 client.ts 的地方；`statusFallbackMessage`/timeout 中文串**保留**（非组件路径仍可用），但显示层优先走本地化。
2. **新模块 `lib/api/error-localization.ts`** + **新 namespace `errors`**（`messages/{zh,en}/errors.json`）：
   ```
   localizeApiError(t /* useTranslations('errors') */, error): string
     ApiError:
       errorCode 且 t.has(`code.${errorCode}`) → t(`code.${errorCode}`)
       status→key 且 t.has(key) → t(key)            // status.401/403/404/badGateway/server...
       status===0 → t('timeout')
       message → return message                       // 最后兜底（后端原文，可能中文）
     Error → error.message
     else → t('generic')
   ```
   动态 key `code.${errorCode}` 走 `t.has()` 守门 + 类型 cast（同 part1 typed-key 处理）。
3. **client hook `useApiErrorMessage()`** 返回 `(error)=>localizeApiError(t,error)`；server 组件用 `getTranslations('errors')`。
4. **`errors` namespace 值**：`status.*`/`timeout`/`generic` 的 **zh 值 = 现 client.ts/errors.ts 硬编码中文逐字节照搬**（红线1）；en 为译文。`code.*` 见下「discovery」。

## 实施步骤

1. **Discovery（先做）**：枚举后端 `error_code`。`grep -rnE "error_code" gateway/ src/`，收已知码（如 `consent_required`、扣费门 insufficient/credits 类、403 gate 类），为每个建 `code.<code>` 的 zh（=后端该码当前中文 message，照搬）+ en。**没有 code 的后端错误不进 map**（降 message 兜底、诚实漏中文）。
2. `ApiError` 加 `errorCode` 字段 + client.ts 失败分支提取。
3. 建 `lib/api/error-localization.ts` + `errors.json`（zh/en）+ 注册 request.ts/global.d.ts。
4. **重构显示调用点**（bulk）：`toast.error(err.message)` / `setError(...err.message...)` / `getErrorMessage(err)` / `data?.detail || "中文"` → `localizeError(err)`。grep `err.message|getErrorMessage|\.detail \|\|` 定位；**只改 always-on + 工作台显示点**，admin 不动。
5. gate：tsc/lint/build + 全 uiloc 守卫（zh-snapshot 须含 errors.json zh 字节一致；cjk-baseline 重生成）。
6. 对抗评审：zh 字节一致 / 未把后端 message 误丢 / 动态 code key 存在性 / passthrough（不本地化用户内容）。

## 必守不变量
- 红线1 默认 zh 字节一致（status/timeout/generic 的 zh 值逐字节照搬现有硬编码串）。
- **诚实记录（DoD 硬要求）**：未编码后端错误（raw 中文 detail 无 code）在 UI-BE-01 之前仍漏中文——**不得**对外称「失败路径已全英文」。这是已知缺口，UI-BE-01 补。
- 不碰 admin；不碰 pipeline 语言字段；纯表现层（付费 API 不碰）。

## 与 UI-BE-01 的边界
UI-09 承接**已有 code + HTTP status**；UI-BE-01（独立后端轨）负责给那 ~239 条无码中文 detail 补 `error_code`/envelope。两者解耦：UI-09 先上、压住可见错误；后端补码后 UI-09 的 `code.*` map 增量扩充即可，无需重构。
