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
 * Middleware compatibility: `/sitemap.xml` is in `publicExactPaths`
 * (frontend-next/src/middleware.ts) so unauthenticated crawler requests
 * return 200 instead of redirecting to `/auth/login`.
 */
export default function sitemap(): MetadataRoute.Sitemap {
  const lastModified = new Date()
  return publicRoutes.map((path) => ({
    url: absoluteUrl(path),
    lastModified,
  }))
}
