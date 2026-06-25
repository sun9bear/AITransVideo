import { NextResponse } from "next/server"
import type { NextRequest } from "next/server"
import createMiddleware from "next-intl/middleware"
import { routing } from "@/i18n/routing"

// Next 16 renamed `middleware` → `proxy` (nodejs runtime only). This file merges
// three concerns in a FIXED order (UI-02 / 方案 §1.3 / 红线 R2):
//   1. canonical-origin redirect (308)
//   2. locale resolution / normalization (next-intl, localeDetection:false)
//   3. session auth gate (locale-aware)
// Order matters: canonical must win first (so SEO/redirect targets are on the
// canonical host); locale must resolve before auth (so the auth gate sees a
// normalized path and the login redirect keeps the visitor's locale); auth runs
// last so it can short-circuit protected pages without losing the locale rewrite.

// Public routes that don't require authentication (prefix match, locale-stripped)
const publicPaths = ["/auth/login", "/auth/register"]

// Paths that are fully public (exact match, locale-stripped). Marketing-layer
// pages must be reachable without a session cookie; otherwise /pricing, /trial,
// and the phone-first `/auth` entry would be redirected to /auth/login and could
// never serve their conversion role.
//
// Legal pages (/terms, /privacy, /refund, /contact) must also be public —
// payment partners (Paddle, Airwallex, etc.) audit these pages without a
// logged-in session, and logged-out visitors must be able to read the policies
// before signing up.
const publicExactPaths = [
  "/",
  "/pricing",
  "/trial",
  "/auth",
  "/terms",
  "/privacy",
  "/refund",
  "/contact",
  // SEO discovery surfaces — must serve unauthenticated, otherwise crawler
  // requests get 302'd to /auth/login. The static-asset early-out below only
  // matches image/font/css/js/video extensions; .xml and .txt are not in that
  // list, so without explicit publicExactPath entries the rules at the
  // bottom would block them. See docs/plans/2026-05-03-geo-optimization-plan.md
  // §3.3 / §7.0.
  "/sitemap.xml",
  "/robots.txt",
  // Dedicated Docker healthcheck target; keep it independent from auth pages.
  "/healthz.txt",
]

const canonicalSiteOrigin = process.env.NEXT_PUBLIC_SITE_URL?.trim()

// next-intl locale middleware. Configured via `routing` (localePrefix:'as-needed',
// localeDetection:false). We invoke it MANUALLY inside proxy() rather than as a
// standalone middleware so it composes deterministically with canonical + auth
// (R2 ordering). It handles: `/zh/x` → `/x` (308 normalization), `/en/x` locale
// rewrite, and `/` → zh (NO cookie/Accept-Language redirect, localeDetection:false).
const intlMiddleware = createMiddleware(routing)

function firstHeaderValue(value: string | null): string | null {
  const first = value?.split(",", 1)[0]?.trim()
  return first || null
}

function hostWithoutPort(host: string): string {
  if (host.startsWith("[") && host.includes("]")) {
    return host.slice(1, host.indexOf("]")).toLowerCase()
  }
  return host.split(":", 1)[0].toLowerCase()
}

function isLocalHost(host: string): boolean {
  const normalized = hostWithoutPort(host)
  return normalized === "localhost" || normalized === "127.0.0.1" || normalized === "::1"
}

function cloudflareVisitorProtocol(value: string | null): string | null {
  if (!value) {
    return null
  }

  try {
    const parsed = JSON.parse(value) as { scheme?: unknown }
    if (parsed.scheme === "http" || parsed.scheme === "https") {
      return `${parsed.scheme}:`
    }
  } catch {
    return null
  }

  return null
}

function canonicalRedirect(request: NextRequest): NextResponse | null {
  if (!canonicalSiteOrigin) {
    return null
  }

  let canonical: URL
  try {
    canonical = new URL(canonicalSiteOrigin)
  } catch {
    return null
  }

  const forwardedHost = firstHeaderValue(request.headers.get("x-forwarded-host"))
  const currentHost = forwardedHost ?? request.headers.get("host") ?? request.nextUrl.host
  if (!currentHost || isLocalHost(currentHost)) {
    return null
  }

  const cloudflareProtocol = cloudflareVisitorProtocol(request.headers.get("cf-visitor"))
  const forwardedProto = firstHeaderValue(request.headers.get("x-forwarded-proto"))
  const currentProtocol = cloudflareProtocol ?? (forwardedProto
    ? `${forwardedProto.toLowerCase()}:`
    : request.nextUrl.protocol.toLowerCase())

  if (
    currentHost.toLowerCase() === canonical.host.toLowerCase() &&
    currentProtocol === canonical.protocol.toLowerCase()
  ) {
    return null
  }

  const canonicalUrl = request.nextUrl.clone()
  canonicalUrl.protocol = canonical.protocol
  canonicalUrl.hostname = canonical.hostname
  canonicalUrl.port = canonical.port
  return NextResponse.redirect(canonicalUrl, 308)
}

/**
 * 剥掉非默认 locale 前缀，归一化路径用于白名单匹配（单点，红线 R2）。
 * `/en/pricing` → `/pricing`；`/pricing` → `/pricing`；`/en` → `/`。
 * 默认 locale（zh）是裸路径（as-needed），不会带前缀；防御性地也剥 `/zh`。
 */
function stripLocalePrefix(pathname: string): string {
  const seg = pathname.split("/")[1]
  if ((routing.locales as readonly string[]).includes(seg)) {
    const stripped = pathname.slice(seg.length + 1)
    return stripped === "" ? "/" : stripped
  }
  return pathname
}

/**
 * 返回访客当前路径的非默认 locale 前缀（`/en` 或 `""`），用于让登录重定向保留语言，
 * 避免英文访客被甩回中文登录页。默认 locale（zh）裸路径返回 `""`。
 */
function localePrefix(pathname: string): string {
  const seg = pathname.split("/")[1]
  return (routing.locales as readonly string[]).includes(seg) && seg !== routing.defaultLocale
    ? `/${seg}`
    : ""
}

export function proxy(request: NextRequest) {
  // ── Stage 1: canonical-origin redirect (308) ────────────────────────────────
  const redirect = canonicalRedirect(request)
  if (redirect) {
    return redirect
  }

  const { pathname } = request.nextUrl

  // Static assets + backend proxy surfaces bypass BOTH locale and auth: they are
  // not page routes under `[locale]`, so running next-intl on them would wrongly
  // rewrite/redirect (e.g. /robots.txt, /marketing/*.webp). The matcher already
  // excludes /_next/static, /_next/image, favicon.ico, job-api, api/; this body
  // early-out covers the rest that still reach proxy.
  if (
    pathname.startsWith("/api/") ||
    pathname.startsWith("/job-api/") ||
    pathname.startsWith("/gateway/") ||
    pathname.startsWith("/_next/") ||
    pathname.startsWith("/favicon") ||
    pathname.startsWith("/marketing/") ||
    pathname.startsWith("/fonts/") ||
    pathname.startsWith("/icons/") ||
    pathname === "/sitemap.xml" ||
    pathname === "/robots.txt" ||
    pathname === "/healthz.txt" ||
    /\.(?:png|jpe?g|webp|avif|gif|svg|ico|woff2?|ttf|otf|css|js|mp4|webm)$/i.test(pathname)
  ) {
    return NextResponse.next()
  }

  // ── Stage 2: locale resolution / normalization (next-intl) ──────────────────
  // intlResponse is EITHER a redirect (locale normalization, e.g. /zh/x → /x) OR
  // a next() carrying the internal locale rewrite headers. We must return THIS
  // response on the allow-paths below — returning a fresh NextResponse.next()
  // would drop the locale rewrite and break [locale] segment resolution.
  const intlResponse = intlMiddleware(request)
  if (intlResponse.headers.has("location")) {
    return intlResponse
  }

  // ── Stage 3: session auth gate (locale-aware) ───────────────────────────────
  // Normalize once: strip the locale prefix so a single whitelist covers both
  // `/pricing` and `/en/pricing` (no per-route `/en` duplication, R2).
  const normalizedPath = stripLocalePrefix(pathname)
  if (
    publicExactPaths.includes(normalizedPath) ||
    publicPaths.some((p) => normalizedPath.startsWith(p)) ||
    normalizedPath.startsWith("/auth/")
  ) {
    return intlResponse
  }

  // Check for session cookie
  const sessionToken = request.cookies.get("avt_session")?.value
  const sessionHint = request.cookies.get("avt_session_hint")?.value

  // No session marker -> redirect to login, preserving the visitor's locale
  // prefix so an English visitor lands on /en/auth/login (not the zh login).
  if (!sessionToken && sessionHint !== "1") {
    const loginUrl = request.nextUrl.clone()
    loginUrl.pathname = `${localePrefix(pathname)}/auth/login`
    loginUrl.searchParams.set("from", pathname)
    return NextResponse.redirect(loginUrl)
  }

  return intlResponse
}

export const config = {
  matcher: [
    // Match all paths except static files and API routes
    "/((?!_next/static|_next/image|favicon.ico|job-api|api/).*)",
  ],
}
