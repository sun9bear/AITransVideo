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

  const [
    common,
    marketing,
    auth,
    seo,
    billing,
    app,
    appProjects,
    appVoices,
    appSettings,
    appHelp,
    appNotifications,
    appBilling,
    appWorkspace,
    appResultMedia,
    appSmartPreviewConfirm,
    appJianyingDraft,
    appSmartPreviewResult,
    errors,
  ] = await Promise.all([
    import(`../../messages/${locale}/common.json`),
    import(`../../messages/${locale}/marketing.json`),
    import(`../../messages/${locale}/auth.json`),
    import(`../../messages/${locale}/seo.json`),
    import(`../../messages/${locale}/billing.json`),
    import(`../../messages/${locale}/app.json`),
    import(`../../messages/${locale}/appProjects.json`),
    import(`../../messages/${locale}/appVoices.json`),
    import(`../../messages/${locale}/appSettings.json`),
    import(`../../messages/${locale}/appHelp.json`),
    import(`../../messages/${locale}/appNotifications.json`),
    import(`../../messages/${locale}/appBilling.json`),
    import(`../../messages/${locale}/appWorkspace.json`),
    import(`../../messages/${locale}/appResultMedia.json`),
    import(`../../messages/${locale}/appSmartPreviewConfirm.json`),
    import(`../../messages/${locale}/appJianyingDraft.json`),
    import(`../../messages/${locale}/appSmartPreviewResult.json`),
    import(`../../messages/${locale}/errors.json`),
  ])

  return {
    locale,
    messages: {
      common: common.default,
      marketing: marketing.default,
      auth: auth.default,
      seo: seo.default,
      billing: billing.default,
      app: app.default,
      appProjects: appProjects.default,
      appVoices: appVoices.default,
      appSettings: appSettings.default,
      appHelp: appHelp.default,
      appNotifications: appNotifications.default,
      appBilling: appBilling.default,
      appWorkspace: appWorkspace.default,
      appResultMedia: appResultMedia.default,
      appSmartPreviewConfirm: appSmartPreviewConfirm.default,
      appJianyingDraft: appJianyingDraft.default,
      appSmartPreviewResult: appSmartPreviewResult.default,
      errors: errors.default,
    },
  }
})
