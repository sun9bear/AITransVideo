import { createNavigation } from "next-intl/navigation"
import { routing } from "./routing"

/**
 * 本地化导航 API（locale-aware）。后续单元（UI-02+）用这里的 Link/useRouter/usePathname
 * 替换 next/link · next/navigation，否则导航会丢 `/en` 前缀。本单元只导出，不替换调用点。
 */
export const { Link, redirect, usePathname, useRouter, getPathname } =
  createNavigation(routing)
