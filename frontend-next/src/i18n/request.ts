import { getRequestConfig } from "next-intl/server"
import { hasLocale } from "next-intl"
import { routing } from "./routing"

/**
 * 固定 namespace import + merge（方案 §1.4 / CodeX 二审 #1）。
 * 每个 messages/<locale>/<ns>.json 挂到自己的 namespace 键下，
 * 故 t("marketing.hero.title") 等可正确解析。绝不用 glob、不用单文件。
 * 加新 namespace（Phase 2 的 "app"）= 在下面 Promise.all 与返回对象各加一行。
 */
export default getRequestConfig(async ({ requestLocale }) => {
  const requested = await requestLocale
  const locale = hasLocale(routing.locales, requested)
    ? requested
    : routing.defaultLocale

  const [common, marketing, auth, seo] = await Promise.all([
    import(`../../messages/${locale}/common.json`),
    import(`../../messages/${locale}/marketing.json`),
    import(`../../messages/${locale}/auth.json`),
    import(`../../messages/${locale}/seo.json`),
  ])

  return {
    locale,
    messages: {
      common: common.default,
      marketing: marketing.default,
      auth: auth.default,
      seo: seo.default,
    },
  }
})
