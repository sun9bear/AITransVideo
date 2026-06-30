# UI-09 · 客户端错误层集中 + error-code map（✅ 已实施 always-on 层，2026-06-30）

> **状态：✅ 已实施（机制 + always-on 21 call points）**。项目主 2026-06-29 放行 Phase 2/3
> （§9.2 Q2=是，备海外）。CodeX 审核建议：UI-09 **前置于**核心工作台铺文案——先把错误显示
> 策略定死，否则英文 UI 在失败路径会突然掉中文/技术错误。本文档据真实 error 层
> （`lib/api/client.ts`）细化并据实施结果回填。
>
> **实施回填（2026-06-30）**——两点与初稿不同，均为正确性收敛：
> 1. **解析顺序：后端 message 让位在 status 兜底【之前】**（初稿伪码写成 status 在 message
>    前会回归 zh：带具体后端中文 message 的 401/403/404/5xx 会被泛化 status 串覆盖）。正确序见
>    下「架构决策」。为 DRY + 字节一致，client.ts 导出 `resolveBackendMessage`，抛出层与显示层
>    共用同一后端-message 解析。
> 2. **`errors.code.*` 本单元【不落条目】**（机制就绪、namespace 暂空）。Discovery 实测：40 个
>    后端 error_code 全在 `gateway/job_intercept.py` 的 job-create 路径（工作台切片），且唯一
>    always-on 带码路径（projects 重命名）走原生 fetch、不产生 `ApiError.errorCode`。故按「机制
>    无 in-scope caller → 显式 deferral 到 owning unit」原则，code.* 由工作台切片按真实 call site
>    逐条核对后端后增量补入。多数 job-create 码还是 **动态 message**（`{exc.required}` 等），需
>    UI-BE-01 发结构化 params 才能本地化。

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
2. **新模块 `lib/api/error-localization.ts`** + **新 namespace `errors`**（`messages/{zh,en}/errors.json`）。
   **实际解析顺序（已实施，R1-safe）**：
   ```
   localizeApiError(t /* useTranslations('errors') */, error): string
     ApiError:
       1) errorCode 且 t.has(`code.${errorCode}`) → t(`code.${errorCode}`)   // 本单元 namespace 暂空，恒不命中
       2) resolveBackendMessage(payload) 有值 → 原样返回                       // 后端 message 先于 status 兜底！
                                                                             //   zh 与改造前字节一致；en 对未编码错误漏中文=已知缺口
       3) status===0 → t('timeout', { seconds: payload.timeoutSeconds })     // 超时（client.ts 写入 timeoutSeconds）
       4) 401/403/404/5xx → t('status.*')                                    // 仅在【无】后端 message 时兜底，zh 值＝硬编码串照搬
       5) → t('generic')
     Error（非 ApiError）→ error.message ?? t('generic')
     else → t('generic')
   ```
   **关键：步骤 2 在步骤 4 之前**——若反过来（status 先），带具体后端中文 message 的 401/403/404/5xx
   在 zh 下会被泛化 status 串覆盖 = R1 回归。`code.*` 暂空（见上「实施回填 2」），步骤 1 恒走步骤 2。
   动态 key `code.${errorCode}` 走 `t.has()` 守门 + 类型 cast（同 part1 typed-key 处理）。
3. **client hook `useApiErrorMessage()`** 返回 `(error)=>localizeApiError(t,error)`；server 组件用 `getTranslations('errors')`。
4. **`errors` namespace 值**：`status.*`/`timeout`/`generic` 的 **zh 值 = 现 client.ts/errors.ts 硬编码中文逐字节照搬**（红线1）；en 为译文。`code.*` 见下「discovery」。

## 实施步骤（已完成 ✅，2026-06-30）

1. **Discovery（已做，workflow `wf_9f768dbe`）**：枚举后端 `error_code` = 40 个，全在
   `gateway/job_intercept.py` job-create 路径（工作台），且唯一 always-on 带码路径（projects 重命名）
   走原生 fetch 无 `ApiError.errorCode`。→ **结论：本单元 `code.*` 不落条目**（机制就绪、deferral 到
   工作台切片，见「实施回填 2」）。
2. ✅ `ApiError` 加 `errorCode`（`client.ts`）；新增导出 `extractErrorCode`（detail.error_code/顶层
   error_code/网关 body.error，从宽提取，t.has 守门兜底）+ `resolveBackendMessage`（presence-based，
   与历史 message 解析字节一致）。timeout ApiError payload 改携 `{timeoutSeconds}`。
3. ✅ 新建 `lib/api/error-localization.ts`（`localizeApiError` + `useApiErrorMessage` hook，useCallback 稳定）
   + `messages/{zh,en}/errors.json`（status.*/timeout/generic）+ 注册 request.ts/global.d.ts。
4. ✅ **重构 21 个 always-on 显示点**（7 文件）：`err instanceof Error ? err.message : t(fallback)` →
   `… ? localizeError(err) : t(fallback)`（保留本地化 fallback）。**工作台（27 个 getErrorMessage 点）+
   admin 不动**——工作台是独立切片（含 5 处硬编码中文前缀「拆分失败」等待迁）。
5. ✅ gate：tsc 0 / eslint 0（仅预存 warning）/ next build 0 / 5 个 uiloc 守卫全绿（zh-snapshot 加
   errors §8 R1 pin；key-parity 自动纳入；cjk-guard 无新增——新 CJK 全在注释/英文 key，无需 baseline 重生成）。
6. ✅ 对抗评审：R1 zh 字节一致 / resolveBackendMessage 与旧内联解析 byte-parity / R5 passthrough / 边缘
   （timeout 秒数、空 code.* t.has 安全、hook 稳定）。

## 必守不变量
- 红线1 默认 zh 字节一致（status/timeout/generic 的 zh 值逐字节照搬现有硬编码串；**解析顺序后端
  message 先于 status 兜底**才不回归；zh-snapshot §8 钉死）。
- **诚实记录（DoD 硬要求，已落）**：未编码后端错误（raw 中文 detail/message 无 code）在 UI-BE-01 之前
  在 **en 下仍显示后端中文**——这是【已知缺口】，**不得**对外称「失败路径已全英文」。覆盖的是
  status/timeout/网络/generic 兜底 + 将来的 code.*。另：`stringifyErrorDetail` 的 422 前缀「请求参数有误：」
  是 client.ts 自有中文，en 下也漏（同缺口，工作台/BE-01 收）。
- 不碰 admin；不碰 pipeline 语言字段；纯表现层（付费 API 不碰）。

## 与 UI-BE-01 的边界
UI-09 承接**已有 HTTP status + timeout + 将来的 code**；UI-BE-01（独立后端轨）负责给那 ~239 条无码
中文 detail 补 `error_code`/envelope。两者解耦：UI-09 先上、压住 status/timeout/网络可见错误；后端补码后
UI-09 的 `code.*` map 增量扩充即可（机制已就绪、namespace 加条目即生效），无需重构。

## 后续（owning unit = 工作台切片 / UI-BE-01）
- 工作台切片：把 27 个 `getErrorMessage(err)` 点 + 5 处硬编码中文前缀路由到 localizeError；按真实
  call site 给命中的（多为动态 message）job-create 码补 `errors.code.*`（静态码可直接 verbatim；动态码
  待 UI-BE-01 发 params）。
- settings 改密/绑邮箱、projects 重命名、voices 删除走原生 fetch，本单元已统一路由到 localizeError
  作单一 chokepoint（当前对 plain Error 等价 pass-through；若将来迁 apiClient 则自动获益）。
