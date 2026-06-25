// 用 zh 各 namespace 文件作 messages 形状真源（与 i18n/request.ts 的 merge 结构对齐）。
// 让 t("common.appName") 等有补全、打错 key 编译失败（方案 §1.4）。
// 注：这是 .d.ts 声明文件，下列 import 仅用于类型位置（typeof），不产生运行时导入。
import common from "../messages/zh/common.json"
import marketing from "../messages/zh/marketing.json"
import auth from "../messages/zh/auth.json"
import seo from "../messages/zh/seo.json"

type Messages = {
  common: typeof common
  marketing: typeof marketing
  auth: typeof auth
  seo: typeof seo
}

declare global {
  // next-intl v4 全局 messages 类型契约
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type
  interface IntlMessages extends Messages {}
}

export {}
