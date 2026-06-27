import type { MetadataRoute } from "next"
import { absoluteUrl, hreflangLanguages, publicRoutes } from "@/lib/seo/site"

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
    // UI-03d-1：locale-aware alternates 由 hreflangLanguages 单点产出。已翻旗路由
    // （/、/pricing、/trial）自动获得 `en` 备选；legal 路由（不在 localizedRoutes）
    // 只挂 `zh-Hans` + `x-default`（无 en），待 UI-03c 翻译后再纳入。`url` 主条目仍为
    // 默认 zh 裸路径，对当前抓取的主 URL 集合零变化。
    alternates: {
      languages: hreflangLanguages(path),
    },
  }))
}
