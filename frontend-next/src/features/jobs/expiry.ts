/**
 * Job expiry helpers (plan 2026-04-18 §11.4 / D12).
 *
 * Priority:
 *   1. explicit `expiresAt` (populated by migration 015 + Gateway TTL rules)
 *   2. fallback legacy rule: updatedAt + 7 days
 *
 * UI colour grading:
 *   - > 3 days remaining  → muted grey
 *   - 1..3 days           → amber (warn)
 *   - < 1 day             → red (danger)
 *   - already expired     → dark red with "即将删除"
 *
 * Mirrors the backend logic in `src/services/web_ui/cleanup.py::_resolve_expires_at`.
 */

import type { JobSummary } from "@/types/jobs"

const LEGACY_RETENTION_DAYS = 7
const MS_PER_HOUR = 60 * 60 * 1000
const MS_PER_DAY = 24 * MS_PER_HOUR

export interface ExpiryInfo {
  /** Absolute expiry time, or null if we can't determine one. */
  expiresAt: Date | null
  /** Milliseconds until expiry. Negative when already expired. */
  msLeft: number
  /** UI bucket — matches tailwind class chosen by `expiryColorClass()`. */
  tier: "safe" | "warn" | "danger" | "expired" | "unknown"
}

function parseIso(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const t = new Date(iso).getTime()
  return Number.isNaN(t) ? null : new Date(t)
}

export function computeExpiryInfo(
  job: Pick<JobSummary, "expiresAt" | "updatedAt" | "createdAt">,
  now: number = Date.now(),
): ExpiryInfo {
  // Explicit expires_at wins.
  const explicit = parseIso(job.expiresAt ?? null)
  if (explicit) {
    const msLeft = explicit.getTime() - now
    return { expiresAt: explicit, msLeft, tier: tierFor(msLeft) }
  }
  // Legacy fallback.
  const base = parseIso(job.updatedAt) ?? parseIso(job.createdAt)
  if (!base) {
    return { expiresAt: null, msLeft: 0, tier: "unknown" }
  }
  const fallback = new Date(base.getTime() + LEGACY_RETENTION_DAYS * MS_PER_DAY)
  const msLeft = fallback.getTime() - now
  return { expiresAt: fallback, msLeft, tier: tierFor(msLeft) }
}

function tierFor(msLeft: number): ExpiryInfo["tier"] {
  if (msLeft <= 0) return "expired"
  if (msLeft < MS_PER_DAY) return "danger"
  if (msLeft < 3 * MS_PER_DAY) return "warn"
  return "safe"
}

export function expiryColorClass(tier: ExpiryInfo["tier"]): string {
  switch (tier) {
    case "expired":
      return "text-red-600 font-medium"
    case "danger":
      return "text-red-500 font-medium"
    case "warn":
      return "text-amber-500"
    case "safe":
      return "text-muted-foreground"
    case "unknown":
    default:
      return "text-muted-foreground"
  }
}

export function expiryLabel(info: ExpiryInfo): string {
  if (info.tier === "unknown" || info.expiresAt === null) return ""
  if (info.tier === "expired") return "即将删除"
  const msLeft = info.msLeft
  const days = Math.floor(msLeft / MS_PER_DAY)
  if (days >= 1) return `${days} 天后过期`
  const hours = Math.floor(msLeft / MS_PER_HOUR)
  if (hours >= 1) return `${hours} 小时后过期`
  return "不到 1 小时后过期"
}
