import { NextResponse } from "next/server"
import type { NextRequest } from "next/server"

// Public routes that don't require authentication
const publicPaths = ["/auth/login", "/auth/register"]

// Paths that are fully public (exact match). Marketing-layer pages must be
// reachable without a session cookie; otherwise /pricing, /trial, and the
// phone-first `/auth` entry would be redirected to /auth/login and could never
// serve their conversion role.
const publicExactPaths = ["/", "/pricing", "/trial", "/auth"]

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl

  // Skip auth check for public paths and API routes
  if (
    publicExactPaths.includes(pathname) ||
    publicPaths.some((p) => pathname.startsWith(p)) ||
    pathname.startsWith("/api/") ||
    pathname.startsWith("/job-api/") ||
    pathname.startsWith("/auth/") ||
    pathname.startsWith("/gateway/") ||
    pathname.startsWith("/_next/") ||
    pathname.startsWith("/favicon")
  ) {
    return NextResponse.next()
  }

  // Check for session cookie
  const sessionToken = request.cookies.get("avt_session")?.value

  // No session cookie → redirect to login
  if (!sessionToken) {
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
