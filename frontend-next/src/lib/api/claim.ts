/**
 * claim.ts — 匿名预览 → 登录认领（post-login）薄 fetch 封装.
 *
 * plan 2026-06-15-anonymous-preview-claim-binding-plan.md §7 / D3.
 *
 * 登录/注册成功后，凭浏览器既有的 avt_anon HttpOnly cookie 把那次匿名预览
 * 绑定到新账户。前端**不传任何 token / preview_id**——body 为 {}，服务端从
 * avt_anon cookie 自行派生（HttpOnly 前端读不到；server-only / 防提权）。
 *
 * gateway-native 路由（/gateway/*），用裸相对路径 + credentials:'include'，
 * **不**走 apiClient（其前缀 /job-api）。失败一律吞掉（fire-and-forget）——
 * 漏掉一次认领是静默 no-op，绝不可阻断登录跳转。
 */

// 非敏感提示位：仅用于「登录后是否值得调 /claim」的客户端判断。真凭证是
// HttpOnly avt_anon cookie，这里**不存任何凭证**。预览 ready 时写、认领后清。
const CLAIM_HINT_KEY = "avt_anon_preview_pending"
const CLAIM_REQUEST_TIMEOUT_MS = 1500

export interface ClaimResult {
  claimed: boolean
  count: number
  preview_ids?: string[]
}

/** claimAnonymousPreview 的结果：server 响应 + settled 终态标志。 */
export interface ClaimOutcome extends ClaimResult {
  /**
   * true = 服务端返回 200 终态（成功 claimed:true，或确定性 no-op claimed:false：
   *   无 cookie / session 过期 / 无可认领 / 已被他人认领）——可安全清 hint。
   * false = **可重试**失败（403/429/503/网络/异常）——保留 hint，下次登录自动重试，
   *   对齐后端「503 retryable」语义（CodeX P2：不把可重试失败变成永久丢失）。
   */
  settled: boolean
}

/** 预览 ready 时调用：记下「本会话有可认领预览」。previewId 仅作存在性提示。 */
export function setAnonClaimHint(previewId: string): void {
  try {
    window.localStorage.setItem(CLAIM_HINT_KEY, previewId)
  } catch {
    // localStorage 不可用（隐私模式/配额）——忽略，认领走 fire-and-forget。
  }
}

export function hasAnonClaimHint(): boolean {
  try {
    return Boolean(window.localStorage.getItem(CLAIM_HINT_KEY))
  } catch {
    return false
  }
}

export function clearAnonClaimHint(): void {
  try {
    window.localStorage.removeItem(CLAIM_HINT_KEY)
  } catch {
    // 忽略
  }
}

/**
 * POST /gateway/anonymous-preview/claim — 必须在 waitForSessionReady() 返回
 * true 之后调用（否则 gateway 见不到 avt_session → 401，认领被静默丢弃）。
 *
 * 永不抛错：任何失败（网络/403/429/503/200 no-op）都返回一个安全结果，
 * 绝不冒泡进登录流程。200 {claimed:false} 是正常静默路径（无 cookie / session
 * 过期 / 无可认领 / 已被他人认领），**不是**错误。
 */
export async function claimAnonymousPreview(): Promise<ClaimOutcome> {
  const controller = new AbortController()
  const timeoutId = window.setTimeout(() => {
    controller.abort()
  }, CLAIM_REQUEST_TIMEOUT_MS)

  try {
    const response = await fetch("/gateway/anonymous-preview/claim", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: "{}",
    })
    if (!response.ok) {
      // 403/429/503 等非 2xx → **可重试**，保留 hint（settled:false）。
      return { claimed: false, count: 0, settled: false }
    }
    const data = (await response.json().catch(() => null)) as ClaimResult | null
    if (!data || typeof data.claimed !== "boolean") {
      // 200 但响应体畸形 → 当终态（避免对坏响应无限重试）。
      return { claimed: false, count: 0, settled: true }
    }
    // 200 终态（成功 或 确定性 no-op）→ 可清 hint。
    return {
      claimed: data.claimed,
      count: typeof data.count === "number" ? data.count : 0,
      preview_ids: data.preview_ids,
      settled: true,
    }
  } catch {
    // 网络/异常 → **可重试**，保留 hint。
    return { claimed: false, count: 0, settled: false }
  } finally {
    window.clearTimeout(timeoutId)
  }
}

/**
 * 登录/注册成功后的认领尝试（仅当本会话曾预览 → hint 存在时触发，省去对从未
 * 预览用户的多余请求）。awaited-but-swallowed，调用方放在 window.location
 * 跳转**之前**。无论结果如何都清除 hint。
 */
export async function maybeClaimAnonPreviewAfterLogin(): Promise<void> {
  if (!hasAnonClaimHint()) {
    return
  }
  let settled = false
  try {
    const outcome = await claimAnonymousPreview()
    settled = outcome.settled
  } catch {
    // 永不阻断登录跳转；异常视为可重试 → 保留 hint。
    settled = false
  }
  // 仅服务端 200 终态后清 hint；可重试失败（403/429/503/网络）**保留** hint，
  // 下次登录自动重试（CodeX P2：不把可重试失败变成永久丢失）。
  if (settled) {
    clearAnonClaimHint()
  }
}
