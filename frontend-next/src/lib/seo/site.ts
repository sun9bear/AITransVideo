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

/**
 * 界面语言（UI page locale）。默认 zh 裸路径，en 走 `/en`（方案 §1.2）。
 */
export type Locale = "zh" | "en"

/**
 * Per-locale SEO 文案。`zh` 镜像上方既有顶层常量以保持兼容；`en` 由 UI-03 填充真实
 * 英文并在翻旗时启用。本单元（UI-01）仅声明，无任何消费方 —— INERT，对默认 zh 渲染零影响。
 */
export const localeSeo: Record<
  Locale,
  { siteName: string; defaultTitle: string; defaultDescription: string }
> = {
  zh: { siteName, defaultTitle, defaultDescription },
  // UI-03a：真实英文文案，与 messages/en/seo.json 的 site.* 同源同值。
  en: {
    siteName: "AITrans.Video",
    defaultTitle: "AITrans.Video · Make the world's videos speak Chinese",
    defaultDescription:
      "Turn long English videos into publish-ready Chinese-dubbed versions. AI video translation, Chinese voiceover, AI subtitles, YouTube video translation, SRT subtitle export, and a long-form localization workbench. Supports videos up to 3 hours, with sentence-by-sentence editing and single-sentence regeneration.",
  },
}

/**
 * Build an absolute URL for a path. `/` returns siteUrl with no trailing slash.
 *
 * 可选 `locale`：非默认 locale 加 `/<locale>` 前缀（默认 zh = 裸路径）。
 * **zh / 省略 locale 时输出与旧实现逐字节等价**（红线 1：既有 sitemap/robots/layout/seo
 * 调用点全部单参调用，行为不变）。
 */
export function absoluteUrl(path: string, locale?: Locale): string {
  const prefix = locale && locale !== "zh" ? `/${locale}` : ""
  if (path === "/" || path === "") return `${siteUrl}${prefix}`
  return `${siteUrl}${prefix}${path.startsWith("/") ? path : `/${path}`}`
}

/**
 * 已翻旗（body 100% 英文、可对爬虫宣告 en）的路由白名单。
 * `/pricing`、`/trial` 在 03a/b/e/f 后整页英文就绪 → 互惠挂 en hreflang + sitemap en 条目。
 *
 * **`/`（home）暂移出（UI-03d-1-followup，@codex #66 P2 + 项目主 2026-06-27 决策「先 drop /en home」）**：
 * 当 `NEXT_PUBLIC_ENABLE_ANONYMOUS_PREVIEW=1` 时 home 渲染 `AnonymousTrialPanel`（仍内联中文
 * consent/上传文案）；宣告 /en 会让爬虫/用户落到 `html lang="en"` 的半中文页。待 **UI-03g** 本地化
 * 该面板后再把 `/` 加回（届时同步更新 hreflang-check / zh-snapshot 的 home 断言）。home 仍走
 * 自指 canonical（en→/en，遵「en canonical 绝不指 zh」红线），只是不挂 en hreflang / 不进 sitemap en。
 *
 * legal 页（/contact /terms /privacy /refund）正文仍中文，待 UI-03c 翻译后才加入；不在列 → 自动只挂 zh。
 */
export const localizedRoutes = ["/pricing", "/trial"] as const

/**
 * hreflang `languages` map for a path（方案 §1.8）。
 * 路由感知（UI-03d-1）：始终产 `zh-Hans` + `x-default`（均指 zh 自指主市场）；**仅当 path 属于
 * `localizedRoutes`** 时追加 `en`（指 `/en` 前缀的英文版）。未翻旗路由（legal）只挂 zh，无 en。
 * hreflang 必须互惠 + 自指 + 恰好一个 x-default（指 zh 主市场）。
 */
export function hreflangLanguages(path: string): Record<string, string> {
  const languages: Record<string, string> = {
    "zh-Hans": absoluteUrl(path, "zh"),
    "x-default": absoluteUrl(path, "zh"),
  }
  if ((localizedRoutes as readonly string[]).includes(path)) {
    languages["en"] = absoluteUrl(path, "en")
  }
  return languages
}
