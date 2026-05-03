import type { MetadataRoute } from "next"
import { absoluteUrl, blockedRoutes } from "@/lib/seo/site"

/**
 * Generates `/robots.txt`.
 *
 * Strategy: search-result crawlers (Googlebot/Bingbot/OAI-SearchBot/
 * PerplexityBot) get public marketing; training crawlers (GPTBot,
 * Google-Extended) are fully disallowed until business decides content
 * opt-in is worth it.
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
      { userAgent: "*", ...sharedRules },
      // Search-result crawlers — same allow list, explicit so they're not
      // accidentally caught under a stricter rule order in the future.
      { userAgent: "OAI-SearchBot", ...sharedRules },
      { userAgent: "PerplexityBot", ...sharedRules },
      // Training crawlers — fully blocked. Re-evaluate per business call,
      // not per technical default.
      { userAgent: "GPTBot", disallow: ["/"] },
      { userAgent: "Google-Extended", disallow: ["/"] },
    ],
    sitemap: absoluteUrl("/sitemap.xml"),
  }
}
