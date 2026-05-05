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
]

export function middleware(request: NextRequest) {
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
