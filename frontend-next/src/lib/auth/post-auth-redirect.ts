"use client"

import { maybeClaimAnonPreviewAfterLogin } from "@/lib/api/claim"

const DEFAULT_POST_AUTH_PATH = "/translations/new"
const SESSION_HINT_COOKIE = "avt_session_hint=1; Max-Age=604800; Path=/; SameSite=Lax; Secure"
const SESSION_READY_ATTEMPTS = 12
const SESSION_READY_DELAY_MS = 250

type SearchParamsLike = {
  get(name: string): string | null
}

const delay = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))

/**
 * Default post-auth target, prefixed for the active UI locale (UI-04 Step 5.6).
 * `localePrefix: "as-needed"` → zh stays bare (`/translations/new`), en is
 * prefixed (`/en/translations/new`) so the English funnel doesn't drop back to
 * Chinese after login. A valid `from` already carries its own locale prefix
 * (injected by the UI-02 proxy) and is used verbatim — this only covers the
 * no-`from` default branch.
 */
function localizedDefaultTarget(locale?: string): string {
  return locale && locale !== "zh"
    ? `/${locale}${DEFAULT_POST_AUTH_PATH}`
    : DEFAULT_POST_AUTH_PATH
}

export function resolvePostAuthRedirect(
  searchParams: SearchParamsLike,
  locale?: string,
): string {
  return normalizeInternalRedirect(searchParams.get("from"), locale)
}

function normalizeInternalRedirect(value: string | null, locale?: string): string {
  const fallback = localizedDefaultTarget(locale)
  if (!value || value.startsWith("//") || !value.startsWith("/")) {
    return fallback
  }

  try {
    const url = new URL(value, "https://aitrans.video")
    if (url.pathname.startsWith("/auth")) {
      return fallback
    }
    return `${url.pathname}${url.search}${url.hash}`
  } catch {
    return fallback
  }
}

export async function waitForSessionReady(): Promise<string | null> {
  for (let attempt = 0; attempt < SESSION_READY_ATTEMPTS; attempt += 1) {
    try {
      const response = await fetch("/auth/me", {
        cache: "no-store",
        credentials: "include",
        headers: { Accept: "application/json" },
      })
      if (response.ok) {
        const data = await response.json().catch(() => null)
        if (data?.user?.id) {
          return data.user.id
        }
      }
    } catch {
      // Retry below; mobile browsers can briefly lag while committing cookies.
    }
    await delay(SESSION_READY_DELAY_MS)
  }
  return null
}

export async function goToPostAuthRedirect(path: string): Promise<void> {
  const userId = await waitForSessionReady()
  if (!userId) {
    throw new Error("登录状态写入失败,请刷新页面后重试")
  }
  // 匿名预览→登录认领（plan 2026-06-15 §7）：此刻 avt_session（新用户）与
  // avt_anon（匿名 session）cookie 同时在场，正是 /claim 所需。必须在硬跳转
  // （window.location.assign 会丢掉所有内存态）之前 fire；内部 fire-and-forget +
  // 仅在 hint 存在时触发，绝不阻断登录跳转。集中在此覆盖三种登录表单。
  await maybeClaimAnonPreviewAfterLogin(userId)
  document.cookie = SESSION_HINT_COOKIE
  window.location.assign(path)
}

export function clearPostAuthSessionHint(): void {
  document.cookie = "avt_session_hint=; Max-Age=0; Path=/; SameSite=Lax; Secure"
}
