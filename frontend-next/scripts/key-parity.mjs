// uiloc:key-parity — 校验 messages/zh/<ns>.json 与 messages/en/<ns>.json 的 key 集合逐 namespace 完全一致。
// 漏译/多键即非零退出。UI-01 立机制，UI-03/UI-04 填 key 后复用（namespace 自动纳入）。
import { readdirSync, readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import path from "node:path"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const msgDir = path.join(root, "messages")
const LOCALES = ["zh", "en"]

function flatKeys(obj, prefix = "") {
  return Object.entries(obj).flatMap(([k, v]) =>
    v && typeof v === "object" && !Array.isArray(v)
      ? flatKeys(v, `${prefix}${k}.`)
      : [`${prefix}${k}`]
  )
}

const namespaces = new Set()
for (const loc of LOCALES) {
  for (const f of readdirSync(path.join(msgDir, loc))) {
    if (f.endsWith(".json")) namespaces.add(f)
  }
}

let failed = false
for (const ns of [...namespaces].sort()) {
  const keys = {}
  for (const loc of LOCALES) {
    try {
      keys[loc] = new Set(flatKeys(JSON.parse(readFileSync(path.join(msgDir, loc, ns), "utf8"))))
    } catch (e) {
      console.error(`[key-parity] ${loc}/${ns} 缺失/无法解析: ${e.message}`)
      failed = true
    }
  }
  const [a, b] = LOCALES
  const onlyA = [...(keys[a] ?? [])].filter((k) => !keys[b]?.has(k))
  const onlyB = [...(keys[b] ?? [])].filter((k) => !keys[a]?.has(k))
  if (onlyA.length || onlyB.length) {
    failed = true
    console.error(`[key-parity] namespace ${ns} key 不一致:`)
    if (onlyA.length) console.error(`  仅 ${a}: ${onlyA.join(", ")}`)
    if (onlyB.length) console.error(`  仅 ${b}: ${onlyB.join(", ")}`)
  }
}

if (failed) {
  console.error("[key-parity] FAIL")
  process.exit(1)
}
console.log("[key-parity] OK — 全 namespace zh↔en key 一致")
