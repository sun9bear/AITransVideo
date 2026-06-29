// uiloc:intl-guard — UI-07 regression guard for active-locale Intl formatters.
//
// Always-on user pages must render dates/numbers in the *active* UI locale, not
// a hardcoded "zh-CN". This guard fails if any in-scope user file reintroduces a
// hardcoded-locale formatter, and sanity-checks the shared mapper.
//
// OUT OF SCOPE (intentionally NOT scanned — these keep "zh-CN" by design):
//   - app/[locale]/(app)/admin/**           operator-only, stays Chinese
//   - lib/format.ts, lib/api/pan.ts          consumed only by admin + workspace
//                                            (UI-06); converted with those units
//   - components/billing/credits-summary.tsx bare toLocaleString() (no locale
//                                            arg) — integer grouping is
//                                            locale-invariant, nothing to map
//
// Deterministic: scans a fixed allowlist of source files, no base-ref / git.

import { readFileSync, existsSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { dirname, join } from "node:path"

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..")

// User-facing files converted by UI-07. Adding a new always-on user page that
// formats dates/numbers? Add it here so the guard keeps it locale-aware.
const IN_SCOPE = [
  "src/app/[locale]/(app)/projects/page.tsx",
  "src/app/[locale]/(app)/projects/[jobId]/page.tsx",
  "src/app/[locale]/(app)/notifications/page.tsx",
  "src/app/[locale]/(app)/settings/page.tsx",
  "src/app/[locale]/(app)/voices/page.tsx",
  "src/components/billing/subscription-summary.tsx",
  "src/components/billing/order-history.tsx",
]

// A formatter call pinned to a literal locale, e.g. toLocaleDateString("zh-CN")
// or new Intl.DateTimeFormat('zh-CN'). These must read the active locale.
const HARDCODED_FORMATTER =
  /(?:toLocaleDateString|toLocaleString|toLocaleTimeString|DateTimeFormat|NumberFormat|RelativeTimeFormat)\(\s*["'][a-z]{2}-[A-Z]{2}["']/g

const MAPPER = "src/lib/intl-locale.ts"

let failed = false

function fail(msg) {
  console.error(`  ✗ ${msg}`)
  failed = true
}

// 1) No hardcoded-locale formatters on in-scope user pages.
for (const rel of IN_SCOPE) {
  const abs = join(ROOT, rel)
  if (!existsSync(abs)) {
    fail(`in-scope file missing: ${rel} (update IN_SCOPE if it moved/was deleted)`)
    continue
  }
  // Scan the whole file (not line-by-line): a hardcoded locale literal can sit
  // on its own line in a multi-line formatter call, e.g.
  //   d.toLocaleDateString(\n    "zh-CN",\n    { ... },\n  )
  // \s* between "(" and the literal spans newlines only against the full source.
  const src = readFileSync(abs, "utf8")
  for (const m of src.matchAll(HARDCODED_FORMATTER)) {
    const lineNo = src.slice(0, m.index).split("\n").length
    fail(`${rel}:${lineNo} hardcoded-locale formatter — use useIntlLocale(): ${m[0].replace(/\s+/g, " ")}`)
  }
}

// 2) The shared mapper exists and keeps zh the byte-identical default.
const mapperAbs = join(ROOT, MAPPER)
if (!existsSync(mapperAbs)) {
  fail(`shared mapper missing: ${MAPPER}`)
} else {
  const m = readFileSync(mapperAbs, "utf8")
  if (!/["']en-US["']/.test(m)) fail(`${MAPPER}: missing "en-US" branch`)
  if (!/["']zh-CN["']/.test(m)) fail(`${MAPPER}: missing "zh-CN" default`)
  if (!/===\s*["']en["']/.test(m)) fail(`${MAPPER}: expected \`locale === "en"\` discriminator`)
}

if (failed) {
  console.error("uiloc:intl-guard FAILED")
  process.exit(1)
}
console.log(`uiloc:intl-guard OK (${IN_SCOPE.length} user files locale-aware, mapper verified)`)
