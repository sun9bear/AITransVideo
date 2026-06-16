// Shared display-formatting helpers, consolidated from per-component copies
// (2026-06-11). Behavior is verbatim from the originals — do not "improve"
// edge cases here without auditing every call site.
//
// NOTE: `lib/api/pan.ts` keeps its own `formatTimestamp` on purpose — its
// null/undefined → "—" and invalid-date semantics differ from the version
// here. Don't merge them blindly.

/** Milliseconds → "M:SS" (minutes unpadded, clamped at 0). */
export function formatTimecode(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${seconds.toString().padStart(2, "0")}`
}

/** Milliseconds → "MM:SS" (both padded). undefined → "". */
export function formatMs(ms: number | undefined): string {
  if (ms === undefined) return ""
  const total = Math.floor(ms / 1000)
  const m = Math.floor(total / 60).toString().padStart(2, "0")
  const s = (total % 60).toString().padStart(2, "0")
  return `${m}:${s}`
}

/** Milliseconds → "1.2s" (one decimal). Non-number → "". */
export function formatDurationSeconds(ms: number | null | undefined): string {
  if (typeof ms !== "number") return ""
  return `${(ms / 1000).toFixed(1)}s`
}

/** ISO timestamp → zh-CN readable "2026/06/11 14:30". Unparseable → input as-is. */
export function formatTimestamp(iso: string): string {
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    })
  } catch {
    return iso
  }
}
