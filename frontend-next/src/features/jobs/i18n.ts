/**
 * UI-05（方案 §0.2 / Task 2.1）：App 层「中央字典」本地化的 translator 接缝。
 *
 * 字典纯函数（presentation.ts / stageMetadata.ts / expiry.ts）被 server 与 client
 * 组件共用，故**不能**在内部调用 useTranslations/getTranslations。改为把 translator
 * 作为第一个参数线程化传入；本模块导出共享的 translator 类型，确保各纯函数签名一致。
 *
 * `useTranslations("app")` 与 `getTranslations("app")` 返回的 translator 已 scope 到
 * `app` namespace，故纯函数内用相对键（`t("stage.draft")` → `app.stage.draft`）。
 * 这是 type-only 导入：`typeof useTranslations` 不产生运行时依赖（pure .ts 模块仍可被
 * server component 引用，零 client JS）。
 */
import type { useTranslations } from "next-intl"

/** Translator scoped to the `app` namespace (relative keys). */
export type AppTranslator = ReturnType<typeof useTranslations<"app">>
