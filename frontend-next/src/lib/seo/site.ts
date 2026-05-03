/**
 * Site-level SEO config — single source of truth for canonical origin, brand
 * names, and route classification. Consumed by:
 *
 *   - app/sitemap.ts        — which URLs to expose to crawlers
 *   - app/robots.ts         — which paths to disallow
 *   - app/layout.tsx        — metadataBase + default OG locale/siteName
 *   - components/seo/*.tsx  — Organization / WebSite / SoftwareApplication
 *
 * `siteUrl` MUST point at the same canonical origin as gateway
 * `SITE_URL` (gateway/notifications.py). docker-compose.yml sets both from
 * the same `${SITE_URL}` / `${NEXT_PUBLIC_SITE_URL}` shell variables to keep
 * email-link host, sitemap canonical, OG urls and JSON-LD aligned.
 *
 * See docs/plans/2026-05-03-geo-optimization-plan.md §6.4 / §7.3.
 */

const FALLBACK_SITE_URL = "https://aitrans.video"

/** Canonical public origin, no trailing slash. */
export const siteUrl = (
  process.env.NEXT_PUBLIC_SITE_URL?.trim() || FALLBACK_SITE_URL
).replace(/\/+$/, "")

/**
 * Primary brand name — displayed in Chinese-language search results, OG cards,
 * and JSON-LD `name`. The latin form lives in `brandNames` for `alternateName`
 * so Knowledge Graph / AI search dedupe both writings as one entity.
 */
export const siteName = "爱译视频"

export const brandNames = ["爱译视频", "AITrans.Video"] as const

export const defaultTitle = "爱译视频 · 让世界视频，开口说中文"

export const defaultDescription =
  "把英文长视频变成可发布的中文配音版。AI 视频翻译、中文配音、AI 字幕、YouTube 视频翻译、SRT 字幕导出、长视频本地化工作台。支持最长 3 小时视频，逐句修改、单句重生成。"

/**
 * Public routes exposed in sitemap.xml. Hand-curated whitelist — never
 * auto-scan the App Router file tree, otherwise login-protected paths like
 * `/workspace` and `/admin` would leak. Phase 2 content pages (guides,
 * compares, use-cases) join this list as they ship.
 */
export const publicRoutes = [
  "/",
  "/pricing",
  "/trial",
  "/contact",
  "/terms",
  "/privacy",
  "/refund",
] as const

/**
 * Crawler disallow list for robots.txt. Mirrors the auth boundary in
 * frontend-next/src/middleware.ts plus the gateway-internal API surfaces
 * proxied through the Next origin.
 *
 * Two design choices:
 *
 *   1. NO trailing slash. robots.txt prefix matching with `/admin/` only
 *      catches `/admin/foo`, not `/admin` itself. Dropping the slash covers
 *      both exact and sub-path. Safe here because no public route shares
 *      a prefix with these (no `/administration` etc).
 *
 *   2. `/auth` is NOT in this list. Auth pages get page-level `noindex`
 *      via `(auth)/layout.tsx` instead. If we both `Disallow:` AND
 *      `noindex`, crawlers can't fetch the page (robots wins) and
 *      therefore can't see the noindex meta — leaving the URL eligible
 *      for "blocked by robots.txt" stub indexing. Allowing the fetch lets
 *      the noindex actually do its job.
 */
export const blockedRoutes = [
  "/api",
  "/job-api",
  "/gateway",
  "/admin",
  "/workspace",
  "/projects",
  "/settings",
  "/tasks",
  "/notifications",
  "/usage",
  "/voices",
] as const

/** Build an absolute URL for a path. `/` returns siteUrl with no trailing slash. */
export function absoluteUrl(path: string): string {
  if (path === "/" || path === "") return siteUrl
  return `${siteUrl}${path.startsWith("/") ? path : `/${path}`}`
}
