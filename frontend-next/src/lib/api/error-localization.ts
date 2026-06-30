import { useCallback } from "react"
import { useTranslations } from "next-intl"
import { ApiError, resolveBackendMessage } from "@/lib/api/client"

/**
 * 客户端错误「显示层」本地化（UI-09，方案 §3 / docs/plans/uiloc-tasks/UI-09-*）。
 *
 * 为什么在显示层而非抛出层：`lib/api/client.ts` 不是 React 组件、拿不到 translator，
 * 故它仍把中文 status/timeout 兜底烘进 `ApiError.message`（供非组件消费方）；真正的
 * 「当前界面语言」解析在这里、由组件持有的 translator 完成。
 *
 * 红线 R1（默认 zh 必须与本地化前逐字节一致）：`errors.status.*` / `errors.timeout`
 * 的 zh 值＝client.ts `statusFallbackMessage` / timeout 串照搬；`errors.generic` zh
 * ＝`lib/api/errors.ts::getErrorMessage` 兜底串照搬。这些都是「前端自有」串（非后端
 * 文案），故无后端漂移风险。
 *
 * 已知缺口（DoD 诚实记录）：带具体后端 message 的「未编码」错误（无 error_code）在
 * UI-BE-01 之前在 en 下仍显示后端中文——见 `resolveBackendMessage` 分支注释。
 */

/** Translator scoped to the `errors` namespace（与 UI-06 part1 同 typed-key 模式）。 */
type ErrorsTranslator = ReturnType<typeof useTranslations<"errors">>
type ErrorsKey = Parameters<ErrorsTranslator>[0]

/** HTTP status → 本地化 status namespace 键；非映射状态返回 null（让位后端 message）。 */
function statusFallbackKey(status: number): ErrorsKey | null {
  if (status === 401) return "status.unauthorized"
  if (status === 403) return "status.forbidden"
  if (status === 404) return "status.notFound"
  if (status === 502 || status === 503 || status === 504) {
    return "status.serviceUnavailable"
  }
  if (status >= 500) return "status.serverError"
  return null
}

/** 从 timeout ApiError 的 payload（client.ts 写入 {timeoutSeconds}）取秒数。 */
function timeoutSeconds(payload: unknown): number | null {
  if (payload && typeof payload === "object" && "timeoutSeconds" in payload) {
    const s = (payload as { timeoutSeconds: unknown }).timeoutSeconds
    if (typeof s === "number" && Number.isFinite(s)) {
      return s
    }
  }
  return null
}

/**
 * 把任意 catch 到的错误解析成「当前界面语言」的可显示串。解析顺序：
 *   1. ApiError.errorCode 命中 `code.<code>`（前端自有本地化串）→ 用之。
 *      ⚠ 本单元 `errors` namespace 暂无 `code.*` 条目：唯一 always-on 的带码路径
 *      （projects 重命名）走原生 fetch、不产生 ApiError.errorCode；其余带码错误都在
 *      工作台 job-create 路径（独立切片）。机制已就绪，code.* 由工作台切片按真实
 *      call site 逐条核对后端后增量补入（方案 §UI-BE-01 边界）。故此分支当前恒不命中。
 *   2. 后端 message（resolveBackendMessage）→ 原样显示。zh 与改造前一致；en 对「未
 *      编码」后端错误仍漏中文 = 已知缺口（UI-BE-01 补）。status 串让位给它（步骤 2 在
 *      步骤 3 前），保证带具体后端文案的 4xx 在 zh 下不被泛化 status 串替换。
 *   3. 无后端 message 时本地化兜底：status===0 → 超时串（带秒数）；401/403/404/5xx →
 *      status 串（zh 值＝client.ts 硬编码串照搬）。
 *   4. generic 兜底。
 */
export function localizeApiError(t: ErrorsTranslator, error: unknown): string {
  if (error instanceof ApiError) {
    if (error.errorCode) {
      const key = `code.${error.errorCode}` as ErrorsKey
      if (t.has(key)) {
        return t(key)
      }
    }
    // presence-based（!== null，非 truthiness）：与改造前内联三元逐字节一致——后端发空串
    // message（{message:""}）旧路径显示 ""，这里也必须返回 "" 而非掉到 status 兜底（红线 R1）。
    // timeout 的 payload {timeoutSeconds} 无 message/detail/error 键 → 返 null → 落下方 status===0 分支。
    const backend = resolveBackendMessage(error.payload)
    if (backend !== null) {
      return backend
    }
    if (error.status === 0) {
      const seconds = timeoutSeconds(error.payload)
      if (seconds != null) {
        return t("timeout", { seconds })
      }
      return error.message || t("generic")
    }
    const statusKey = statusFallbackKey(error.status)
    if (statusKey) {
      return t(statusKey)
    }
    // 其余非映射 status（400/409/422/429…）且无后端 message：本地化 client.ts 自有兜底
    // `请求失败（{status}）`（@codex PR #86 P2）。这是【前端自有】中文（非后端文案），不依赖
    // UI-BE-01；zh 值与 statusFallbackMessage 的 `请求失败（${status}）` 逐字节一致（红线 R1）。
    if (error.status > 0) {
      return t("status.generic", { status: error.status })
    }
    return error.message || t("generic")
  }
  if (error instanceof Error && error.message) {
    return error.message
  }
  return t("generic")
}

/**
 * Client hook：返回绑定到 `errors` namespace 的本地化函数，供组件在 catch 分支调用：
 *   const localizeError = useApiErrorMessage()
 *   ...catch (err) { toast.error(localizeError(err)) }
 * Server 组件可直接 `localizeApiError(await getTranslations("errors"), err)`。
 */
export function useApiErrorMessage(): (error: unknown) => string {
  const t = useTranslations("errors")
  // useCallback 稳定引用：调用点把 localizeError 放进 useCallback/useEffect 依赖数组，
  // 引用稳定才不破坏 memo（next-intl 的 t 按 namespace 稳定）。
  return useCallback((error: unknown) => localizeApiError(t, error), [t])
}
