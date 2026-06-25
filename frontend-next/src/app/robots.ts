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
  // UI-02：locale-aware disallow。route group 迁入 `[locale]` 后，app/admin 路由
  // 在 `/en` 前缀下同样可达（如 `/en/workspace`、`/en/admin`）。若只 disallow 裸路径，
  // en locale 下的受保护路由会对爬虫暴露。单点 map 生成 `/en` 变体（不手抄）；
  // 对不存在的 `/en/api` 等后端代理路径 disallow 是无害 no-op。
  const localizedBlocked = blockedRoutes.map((route) => `/en${route}`)
  const sharedRules = {
    allow: ["/"],
    disallow: [...blockedRoutes, ...localizedBlocked],
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
