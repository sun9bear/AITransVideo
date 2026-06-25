import { NextResponse } from "next/server"
import type { NextRequest } from "next/server"

// Public routes that don't require authentication
const publicPaths = ["/auth/login", "/auth/register"]

// Paths that are fully public (exact match). Marketing-layer pages must be
// reachable without a session cookie; otherwise /pricing, /trial, and the
// phone-first `/auth` entry would be redirected to /auth/login and could never
// serve their conversion role.
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

export function middleware(request: NextRequest) {
  const redirect = canonicalRedirect(request)
  if (redirect) {
    return redirect
  }

  const { pathname } = request.nextUrl

  // Skip auth check for public paths, API routes, and static assets in /public.
  //
  // Asset directories under /public (e.g. /marketing/*, /fonts/*, /icons/*) must
  // bypass auth — they're served as plain static files. Next.js usually serves
  // these without invoking middleware, but the matcher below is broad enough
  // that explicit early-out is needed once a file extension is present in the
  // URL (e.g. /marketing/hero-paper-1920.webp).
  if (
    publicExactPaths.includes(pathname) ||
    publicPaths.some((p) => pathname.startsWith(p)) ||
    pathname.startsWith("/api/") ||
    pathname.startsWith("/job-api/") ||
    pathname.startsWith("/auth/") ||
    pathname.startsWith("/gateway/") ||
    pathname.startsWith("/_next/") ||
    pathname.startsWith("/favicon") ||
    pathname.startsWith("/marketing/") ||
    pathname.startsWith("/fonts/") ||
    pathname.startsWith("/icons/") ||
    /\.(?:png|jpe?g|webp|avif|gif|svg|ico|woff2?|ttf|otf|css|js|mp4|webm)$/i.test(pathname)
  ) {
    return NextResponse.next()
  }

  // Check for session cookie
  const sessionToken = request.cookies.get("avt_session")?.value
  const sessionHint = request.cookies.get("avt_session_hint")?.value

  // No session marker -> redirect to login
  if (!sessionToken && sessionHint !== "1") {
    const loginUrl = request.nextUrl.clone()
    loginUrl.pathname = "/auth/login"
    loginUrl.searchParams.set("from", pathname)
    return NextResponse.redirect(loginUrl)
  }

  return NextResponse.next()
}

export const config = {
  matcher: [
    // Match all paths except static files and API routes
    "/((?!_next/static|_next/image|favicon.ico|job-api|api/).*)",
  ],
}
