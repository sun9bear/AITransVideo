// uiloc:cjk-guard — 禁止新增内联 CJK 字面量（方案 §5.1，CodeX 二审收紧）。
//   - AST 扫描（typescript 包），只命中 JSX text 节点 + 面向用户的字符串/模板字面量；
//     **排除注释/JSDoc**（ts.createSourceFile 把注释当 trivia，不入节点，天然排除）。
//   - baseline-snapshot：只阻断"新增未登记"的 (文件, 文本) 对；迁移完成的旧串从 baseline
//     移除（baseline 只减不增）。
//   - 排除 out-of-scope 目录：(app)/admin/**、workspace/[jobId]/edit/**。
//   - allowlist：content/品牌词（scripts/cjk-allowlist.json）放行。
// 用法：`node scripts/cjk-guard.mjs`（check）；`node scripts/cjk-guard.mjs --update`（重写 baseline）。
import { readdirSync, readFileSync, writeFileSync, existsSync } from "node:fs"
import { fileURLToPath } from "node:url"
import path from "node:path"
import ts from "typescript"

const scriptsDir = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(scriptsDir, "..")
const srcDir = path.join(root, "src")
const baselinePath = path.join(scriptsDir, "cjk-baseline.json")
const allowlistPath = path.join(scriptsDir, "cjk-allowlist.json")

const CJK = /[　-〿㐀-䶿一-鿿豈-﫿＀-￯]/
const EXTS = new Set([".ts", ".tsx", ".js", ".jsx", ".mjs"])
// out-of-scope 目录（UI-02 迁入 [locale] 后子串仍匹配）：admin、post-edit 编辑页
const EXCLUDE_RE = /(\(app\)[\\/]admin[\\/])|([\\/]workspace[\\/]\[jobId\][\\/]edit[\\/])/

const allowlist = existsSync(allowlistPath) ? JSON.parse(readFileSync(allowlistPath, "utf8")) : []

function* walk(dir) {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const fp = path.join(dir, e.name)
    if (e.isDirectory()) yield* walk(fp)
    else if (EXTS.has(path.extname(e.name))) yield fp
  }
}

const rel = (fp) => path.relative(root, fp).replace(/\\/g, "/")

function hasCjk(s) {
  let t = s
  for (const w of allowlist) if (w) t = t.split(w).join("")
  return CJK.test(t)
}

function collect(fp) {
  const sf = ts.createSourceFile(
    fp,
    readFileSync(fp, "utf8"),
    ts.ScriptTarget.Latest,
    /* setParentNodes */ false,
    ts.ScriptKind.TSX
  )
  const out = []
  const visit = (node) => {
    let raw = null
    if (ts.isStringLiteralLike(node)) raw = node.text
    else if (ts.isJsxText(node)) raw = node.text
    else if (ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) raw = node.text
    if (raw != null) {
      const norm = raw.replace(/\s+/g, " ").trim()
      if (norm && hasCjk(norm)) out.push(norm)
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return out
}

function scan() {
  const map = {}
  for (const fp of walk(srcDir)) {
    const rp = rel(fp)
    if (EXCLUDE_RE.test(rp)) continue
    const found = collect(fp)
    if (found.length) map[rp] = [...new Set(found)].sort()
  }
  return map
}

const current = scan()
const update = process.argv.includes("--update") || process.env.UILOC_CJK_UPDATE === "1"

if (update || !existsSync(baselinePath)) {
  const sorted = Object.fromEntries(Object.entries(current).sort(([a], [b]) => a.localeCompare(b)))
  writeFileSync(baselinePath, JSON.stringify(sorted, null, 2) + "\n")
  const n = Object.values(current).reduce((s, a) => s + a.length, 0)
  console.log(`[cjk-guard] baseline 写入：${Object.keys(current).length} 文件 / ${n} occurrences`)
  process.exit(0)
}

const baseline = JSON.parse(readFileSync(baselinePath, "utf8"))
const violations = []
for (const [rp, texts] of Object.entries(current)) {
  const base = new Set(baseline[rp] || [])
  for (const t of texts) if (!base.has(t)) violations.push(`${rp}: ${JSON.stringify(t.slice(0, 60))}`)
}

if (violations.length) {
  console.error("[cjk-guard] FAIL — 新增未登记的内联 CJK（请改用 message key / 字典）：")
  for (const v of violations) console.error("  " + v)
  console.error(
    `\n共 ${violations.length} 处。合法 content/品牌词 → 加 scripts/cjk-allowlist.json；` +
      `迁移完成的旧串从 baseline 移除（只减不增），勿手填新增。`
  )
  process.exit(1)
}
console.log("[cjk-guard] OK — 无新增内联 CJK")
