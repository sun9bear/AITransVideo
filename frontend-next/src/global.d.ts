// next-intl v4 类型增强（方案 §1.4）。
// v4 通过 `declare module "next-intl"` 的 AppConfig 读取 Messages / Locale 类型——
// **不是** v3 的全局 `IntlMessages` 接口（后者在 v4 被忽略，typo 保护会失效，CodeX CLI 复审指出）。
// 用 zh 各 namespace 文件作 messages 形状真源（与 i18n/request.ts 的 merge 结构对齐）：
// t("common.appName") 有补全；t("common.notThere") 编译失败。
// 注：这是 .d.ts 声明文件，下列 import 仅用于类型位置，不产生运行时导入。
import common from "../messages/zh/common.json"
import marketing from "../messages/zh/marketing.json"
import auth from "../messages/zh/auth.json"
import seo from "../messages/zh/seo.json"
import billing from "../messages/zh/billing.json"
import app from "../messages/zh/app.json"
import appProjects from "../messages/zh/appProjects.json"
import appVoices from "../messages/zh/appVoices.json"
import appSettings from "../messages/zh/appSettings.json"
import appHelp from "../messages/zh/appHelp.json"
import appNotifications from "../messages/zh/appNotifications.json"
import appBilling from "../messages/zh/appBilling.json"
import appWorkspace from "../messages/zh/appWorkspace.json"
import appResultMedia from "../messages/zh/appResultMedia.json"
import appSmartPreviewConfirm from "../messages/zh/appSmartPreviewConfirm.json"
import appJianyingDraft from "../messages/zh/appJianyingDraft.json"
import appSmartPreviewResult from "../messages/zh/appSmartPreviewResult.json"
import errors from "../messages/zh/errors.json"
import { routing } from "./i18n/routing"

type Messages = {
  common: typeof common
  marketing: typeof marketing
  auth: typeof auth
  seo: typeof seo
  billing: typeof billing
  app: typeof app
  appProjects: typeof appProjects
  appVoices: typeof appVoices
  appSettings: typeof appSettings
  appHelp: typeof appHelp
  appNotifications: typeof appNotifications
  appBilling: typeof appBilling
  appWorkspace: typeof appWorkspace
  appResultMedia: typeof appResultMedia
  appSmartPreviewConfirm: typeof appSmartPreviewConfirm
  appJianyingDraft: typeof appJianyingDraft
  appSmartPreviewResult: typeof appSmartPreviewResult
  errors: typeof errors
}

declare module "next-intl" {
  interface AppConfig {
    Messages: Messages
    Locale: (typeof routing.locales)[number]
  }
}
