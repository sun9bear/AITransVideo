import type { MetadataRoute } from "next"
import { absoluteUrl, blockedRoutes } from "@/lib/seo/site"

/**
 * Generates `/robots.txt`.
 *
 * Strategy: every documented bot — search-result crawlers AND training
 * crawlers — gets the same public-marketing allowance. The previous
 * stance disallowed GPTBot and Google-Extended on the conservative
 * default of "training crawlers blocked until business opts in"; that
 * was reversed once Cloudflare AI Crawl Control (which sits in front of
 * the origin) was explicitly toggled to Allow for those same bots. For
 * a marketing site whose content exists to be quoted, training-data
 * inclusion is upside, not a leak.
 *
 * IMPORTANT: robots.txt is NOT a reliable index-removal mechanism. Auth
 * pages carry page-level `noindex` in `(auth)/layout.tsx` so external
 * inbound links can't produce a stub indexed entry just because we
 * disallowed crawl here. See plan §6.2 / §7.2.
 *
 * Middleware compatibility: `/robots.txt` is in `publicExactPaths`
 * (frontend-next/src/middleware.ts) so unauthenticated crawler requests
 * return 200 instead of redirecting to `/auth/login`.
 */
export default function robots(): MetadataRoute.Robots {
  const sharedRules = {
    allow: ["/"],
    disallow: [...blockedRoutes],
  }

  return {
    rules: [
      // Default: allow public marketing, block app/admin/api/auth surfaces.
      // The wildcard rule covers every crawler we don't enumerate
      // explicitly, including Bytespider, ClaudeBot, Claude-SearchBot,
      // CCBot, GPTBot, Google-Extended, PerplexityBot, OAI-SearchBot,
      // PetalBot, Manus Bot, TikTok Spider, etc.
      { userAgent: "*", ...sharedRules },
    ],
    sitemap: absoluteUrl("/sitemap.xml"),
  }
}
