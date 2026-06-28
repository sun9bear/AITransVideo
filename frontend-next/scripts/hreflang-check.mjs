// uiloc:hreflang-check — hreflang 互惠/自指/x-default 正确性守卫（UI-03d-1，SEO 去索引风险关键）。
// 错误的 canonical/hreflang 会让生产站被搜索引擎去索引，故把 hreflang 不变量从人审升级为机器守卫。
//
// 直接 import site.ts（Node 原生 type-stripping），复用 zh-snapshot 的 `pathToFileURL` +
// `delete NEXT_PUBLIC_SITE_URL` 模式，使 siteUrl 确定性回退到 fallback、与具体 origin 无关。
//
// 断言（逐 localizedRoute）：
//   1. 互惠：zh-Hans + en + x-default 三键齐全。
//   2. en === absoluteUrl(path, "en")（`/en` 前缀的英文版自指）。
//   3. x-default === absoluteUrl(path, "zh")（指 zh 主市场）。
//   4. 恰好一个 x-default（hreflang 规范：x-default 必须唯一）。
//   5. x-default 指向 zh URL（== zh-Hans，自洽）。
// 以及 legal 路由（/terms）：NO en 键（legal 未翻旗，留待 UI-03c）。
import { fileURLToPath, pathToFileURL } from "node:url"
import path from "node:path"
import { strict as assert } from "node:assert"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")

// 清掉 env，让 siteUrl 确定性回退到 fallback（与 CI/Compose 注入的 origin 无关）。
delete process.env.NEXT_PUBLIC_SITE_URL
const SITE_URL = "https://aitrans.video"
const site = await import(pathToFileURL(path.join(root, "src/lib/seo/site.ts")).href)

assert.equal(site.siteUrl, SITE_URL, "siteUrl fallback 漂移（hreflang-check 前置）")

const violations = []
const fail = (msg) => violations.push(msg)

// localizedRoutes 必须非空（含已翻旗页 home `/` + /pricing + /trial；home 自 UI-03g 加回，见 site.ts）。
const routes = site.localizedRoutes
assert.ok(Array.isArray(routes) && routes.length > 0, "localizedRoutes 为空或非数组")

for (const route of routes) {
  const hl = site.hreflangLanguages(route)
  const keys = Object.keys(hl)

  // 1) 互惠：三键齐全
  for (const k of ["zh-Hans", "en", "x-default"]) {
    if (!(k in hl)) fail(`${route}: 缺 hreflang 键 "${k}"（应互惠 zh-Hans+en+x-default）`)
  }

  // 2) en === absoluteUrl(path, "en")
  const expectedEn = site.absoluteUrl(route, "en")
  if (hl["en"] !== expectedEn) {
    fail(`${route}: en="${hl["en"]}" ≠ absoluteUrl(path,"en")="${expectedEn}"`)
  }

  // 3) x-default === absoluteUrl(path, "zh")
  const expectedZh = site.absoluteUrl(route, "zh")
  if (hl["x-default"] !== expectedZh) {
    fail(`${route}: x-default="${hl["x-default"]}" ≠ absoluteUrl(path,"zh")="${expectedZh}"`)
  }

  // 4) 恰好一个 x-default 键
  const xDefaultCount = keys.filter((k) => k === "x-default").length
  if (xDefaultCount !== 1) {
    fail(`${route}: x-default 键数=${xDefaultCount}（必须恰好 1）`)
  }

  // 5) x-default 指向 zh（与 zh-Hans 一致 → 自洽指主市场）
  if (hl["x-default"] !== hl["zh-Hans"]) {
    fail(`${route}: x-default="${hl["x-default"]}" 应指 zh（== zh-Hans="${hl["zh-Hans"]}"）`)
  }
}

// legal 路由：未翻旗 → NO en 键（en 留待 UI-03c）。
const legalRoute = "/terms"
assert.ok(
  !routes.includes(legalRoute),
  `${legalRoute} 不应在 localizedRoutes（legal 未翻旗，本断言前提失效）`,
)
const hlLegal = site.hreflangLanguages(legalRoute)
if ("en" in hlLegal) {
  fail(`${legalRoute}: 不应含 en 键（legal 未翻旗，hreflang=${JSON.stringify(hlLegal)}）`)
}
if (!("zh-Hans" in hlLegal) || !("x-default" in hlLegal)) {
  fail(`${legalRoute}: 应含 zh-Hans + x-default（hreflang=${JSON.stringify(hlLegal)}）`)
}

// home 路由（`/`）：UI-03g 已翻旗（AnonymousTrialPanel + anonymousPreview 本地化、/en home 整页英文）
// → 现属 localizedRoutes，互惠断言由上方主循环统一覆盖（zh-Hans+en+x-default 全齐 + 自指 + 唯一 x-default）。
// 故此处不再单列 home no-en 断言（早先 @codex #66 P2 临时移出已收回）。
const homeRoute = "/"
assert.ok(
  routes.includes(homeRoute),
  `${homeRoute}（home）应在 localizedRoutes（UI-03g 翻旗后；若移除会丢 /en home 互惠 hreflang）`,
)

if (violations.length) {
  console.error("[hreflang-check] FAIL — hreflang 不变量违例：")
  for (const v of violations) console.error("  " + v)
  process.exit(1)
}
console.log(
  `[hreflang-check] OK — ${routes.length} 条 localizedRoute（含 home ${homeRoute}）互惠/自指/x-default 全通过；legal(${legalRoute}) 无 en`,
)
