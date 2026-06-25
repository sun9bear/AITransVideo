"use client"

import { maybeClaimAnonPreviewAfterLogin } from "@/lib/api/claim"
import { routing } from "@/i18n/routing"

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

/**
 * Strip a leading non-default locale segment (`/en/...` → `/...`) so the auth-page
 * loop guard below treats `/en/auth/login` the same as `/auth/login`. zh is the
 * bare default (no prefix) so zh `from` values pass through unchanged → R1
 * byte-identical (only en-prefixed paths are newly caught).
 */
function deLocalizePath(pathname: string): string {
  const seg = pathname.split("/")[1]
  if ((routing.locales as readonly string[]).includes(seg)) {
    const rest = pathname.slice(seg.length + 1)
    return rest === "" ? "/" : rest
  }
  return pathname
}

function normalizeInternalRedirect(value: string | null, locale?: string): string {
  const fallback = localizedDefaultTarget(locale)
  if (!value || value.startsWith("//") || !value.startsWith("/")) {
    return fallback
  }

  try {
    const url = new URL(value, "https://aitrans.video")
    // Loop guard: never redirect back to a login/auth page. Must be locale-aware —
    // `/en/auth/login` would otherwise slip past a bare `startsWith("/auth")` and
    // bounce the just-logged-in en visitor straight back to login (UI-04 review).
    if (deLocalizePath(url.pathname).startsWith("/auth")) {
      return fallback
    }
    // `from` kept verbatim — it already carries the correct locale prefix (proxy).
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
    // TODO(UI-09): 此中文 throw（rare session-write-failure 路径）在 en 漏斗仍会 toast 中文，
    // 与 client.ts/errors.ts 同属非组件模块的客户端错误层，集中本地化留 UI-09（见 DoD 已知缺口）。
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
