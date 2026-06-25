// uiloc:zh-snapshot — 默认 zh 字节一致回归（红线 1）+ site.ts inert 等价校验。
// frontend-next 无 JS 测试运行器，故用独立 node 脚本断言关键不变量（非引入 vitest/jest）。
// 直接 import site.ts（Node 24 原生 type-stripping）；site.ts 纯净无外部依赖、无 @/ 别名。
import { readFileSync } from "node:fs"
import { fileURLToPath, pathToFileURL } from "node:url"
import path from "node:path"
import { strict as assert } from "node:assert"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")

// 1) catalog 可读
const zhCommon = JSON.parse(readFileSync(path.join(root, "messages/zh/common.json"), "utf8"))
const enCommon = JSON.parse(readFileSync(path.join(root, "messages/en/common.json"), "utf8"))
assert.equal(zhCommon.appName, "爱译视频", "messages/zh/common.json appName 漂移")
assert.equal(enCommon.appName, "AITrans.Video", "messages/en/common.json appName 漂移")

// 2) site.ts inert：默认 zh / 单参 absoluteUrl 行为与旧实现等价（红线 1），hreflang 只 zh
const SITE_URL = "https://aitrans.video" // NEXT_PUBLIC_SITE_URL 未设时 fallback
const site = await import(pathToFileURL(path.join(root, "src/lib/seo/site.ts")).href)

assert.equal(site.siteUrl, SITE_URL, "siteUrl fallback 漂移")
assert.equal(site.siteName, "爱译视频", "siteName 漂移（红线 1）")
assert.equal(site.defaultTitle, "爱译视频 · 让世界视频，开口说中文", "defaultTitle 漂移（红线 1）")

assert.equal(site.absoluteUrl("/"), SITE_URL, "absoluteUrl('/') 漂移")
assert.equal(site.absoluteUrl("/pricing"), `${SITE_URL}/pricing`, "absoluteUrl('/pricing') 漂移")
assert.equal(site.absoluteUrl("/pricing", "zh"), `${SITE_URL}/pricing`, "absoluteUrl(zh) ≠ 单参（zh 必须 inert）")
assert.equal(site.absoluteUrl("pricing"), `${SITE_URL}/pricing`, "absoluteUrl 无前导斜杠 漂移")

const hl = site.hreflangLanguages("/")
assert.deepEqual(
  hl,
  { "zh-Hans": SITE_URL, "x-default": SITE_URL },
  "hreflang 非 inert（UI-01 应只含 zh-Hans + x-default，均指 zh）"
)

// en 分支声明可用（未被消费）：前缀正确，供 UI-03 翻旗
assert.equal(site.absoluteUrl("/pricing", "en"), `${SITE_URL}/en/pricing`, "absoluteUrl en 前缀错误")

console.log("[zh-snapshot] OK — 默认 zh 不变量 + site.ts inert 全部通过")
