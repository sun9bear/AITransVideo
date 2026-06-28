import type { MetadataRoute } from "next"
import { absoluteUrl, hreflangLanguages, localizedRoutes, publicRoutes } from "@/lib/seo/site"

/**
 * Generates `/sitemap.xml`.
 *
 * Hand-curated whitelist — see `publicRoutes` in `lib/seo/site.ts`. Never
 * auto-scan the App Router tree; that would leak login-protected paths like
 * `/workspace`, `/projects`, `/admin`.
 *
 * `changeFrequency` and `priority` are deliberately omitted. Google publicly
 * stated (2023) it ignores both fields, and Bing follows lastmod only —
 * spending plan effort on per-URL frequency tables yields ~zero value.
 *
 * `lastModified` is also omitted on purpose. Setting it to `new Date()` at
 * request time would lie to crawlers — every fetch would advertise that
 * every URL was just modified, even though no content changed since the
 * last build. A truthful per-page mtime would require build-time wiring
 * we don't have yet; until then, no field beats a stale `now()`.
 *
 * Middleware compatibility: `/sitemap.xml` is in `publicExactPaths`
 * (frontend-next/src/middleware.ts) so unauthenticated crawler requests
 * return 200 instead of redirecting to `/auth/login`.
 */
export default function sitemap(): MetadataRoute.Sitemap {
  return publicRoutes.map((path) => {
    // 仅**已翻旗**路由（home `/`、/pricing、/trial）挂 locale-aware alternates（含 en，由
    // hreflangLanguages 单点产出）；未翻旗路由（legal 待 UI-03c）**不挂 languages** —— 避免无意义
    // 的 zh-only 自指备选集，与 page 级 metadata 一致（@codex #67 P2）。home `/` 自 UI-03g 起加回
    // localizedRoutes，本逻辑无需改动即自动挂 en alternate。`url` 主条目仍为默认 zh 裸路径。
    const localized = (localizedRoutes as readonly string[]).includes(path)
    return {
      url: absoluteUrl(path),
      ...(localized ? { alternates: { languages: hreflangLanguages(path) } } : {}),
    }
  })
}
