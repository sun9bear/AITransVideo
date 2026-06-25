import type { MetadataRoute } from "next"
import { absoluteUrl, publicRoutes } from "@/lib/seo/site"

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
  return publicRoutes.map((path) => ({
    url: absoluteUrl(path),
    // UI-02：locale-aware alternates 结构（INERT，仅 zh 自指，0 个 en URL）。
    // `en` alternates 留给 UI-03 翻旗（Phase 1 Task 1.3）；此处只铺管线骨架，
    // 让默认 zh sitemap 结构与未来 en 扩展兼容，对当前抓取零行为变化。
    alternates: {
      languages: {
        "zh-Hans": absoluteUrl(path, "zh"),
      },
    },
  }))
}
