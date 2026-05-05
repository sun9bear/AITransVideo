"use client"

const DEFAULT_POST_AUTH_PATH = "/translations/new"
const SESSION_HINT_COOKIE = "avt_session_hint=1; Max-Age=604800; Path=/; SameSite=Lax; Secure"
const SESSION_READY_ATTEMPTS = 12
const SESSION_READY_DELAY_MS = 250

type SearchParamsLike = {
  get(name: string): string | null
}

const delay = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))

export function resolvePostAuthRedirect(searchParams: SearchParamsLike): string {
  return normalizeInternalRedirect(searchParams.get("from"))
}

function normalizeInternalRedirect(value: string | null): string {
  if (!value || value.startsWith("//") || !value.startsWith("/")) {
    return DEFAULT_POST_AUTH_PATH
  }

  try {
    const url = new URL(value, "https://aitrans.video")
    if (url.pathname.startsWith("/auth")) {
      return DEFAULT_POST_AUTH_PATH
    }
    return `${url.pathname}${url.search}${url.hash}`
  } catch {
    return DEFAULT_POST_AUTH_PATH
  }
}

export async function waitForSessionReady(): Promise<boolean> {
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
          return true
        }
      }
    } catch {
      // Retry below; mobile browsers can briefly lag while committing cookies.
    }
    await delay(SESSION_READY_DELAY_MS)
  }
  return false
}

export async function goToPostAuthRedirect(path: string): Promise<void> {
  const ready = await waitForSessionReady()
  if (!ready) {
    throw new Error("登录状态写入失败,请刷新页面后重试")
  }
  document.cookie = SESSION_HINT_COOKIE
  window.location.assign(path)
}

export function clearPostAuthSessionHint(): void {
  document.cookie = "avt_session_hint=; Max-Age=0; Path=/; SameSite=Lax; Secure"
}
