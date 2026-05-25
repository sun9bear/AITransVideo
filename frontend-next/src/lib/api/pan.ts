/**
 * API client for the admin pan backup endpoints (Phase 7b).
 *
 * Hits the gateway's /api/admin/pan/* surface directly — these are gateway
 * native endpoints (NOT proxied through /job-api), so we mirror the
 * support.ts pattern of using fetch + credentials:"include" rather than
 * the shared ApiClient (which is /job-api scoped).
 *
 * All calls require an admin session cookie. The gateway enforces role
 * checks server-side; the frontend just renders 403 errors gracefully.
 */

// ===========================================================================
// Types — mirror gateway/pan/admin_api.py response shapes
// ===========================================================================

export type PanConnectionState = "disconnected" | "active" | "revoked"

export interface PanQuota {
  total: number
  used: number
  free: number
}

export interface PanStatus {
  connected: boolean
  status: PanConnectionState | string
  scope: string | null
  last_refreshed_at: string | null
  connected_at: string | null
  quota: PanQuota | null
  quota_error?: string
}

export type BackupRecordStatus =
  | "uploading"
  | "uploaded"
  | "failed"
  | "deleted"
  | "restoring"
  | "restored"

export interface BackupRecord {
  id: string
  job_id: string
  /**
   * User-facing job label resolved by the backend (Job.display_name →
   * Job.title → null). When null, frontend should fall back to job_id.
   * Null commonly means the source Job was deleted (FK is intentionally
   * absent so backup tarballs survive job deletion).
   */
  job_display_name: string | null
  job_edit_generation: number
  provider: string
  remote_path: string
  size_bytes: number
  sha256: string
  md5: string
  status: BackupRecordStatus
  heartbeat_at: string | null
  completed_at: string | null
  created_at: string
  error_message?: string | null
}

export interface BackupListResponse {
  /**
   * NOTE: Backend returns this key as `items`, NOT `backups`. The shape
   * is defined in gateway/pan/admin_api.py::list_backups (response 263-268):
   *   { "items": [...], "total": N, "limit": L, "offset": O }
   * Aligning the TS contract to the source of truth (Phase 7b 2026-05-19
   * production bug: I had originally typed this as `backups` and Dashboard
   * crashed with "Cannot read property 'length' of undefined" right after
   * a successful OAuth callback).
   */
  items: BackupRecord[]
  total: number
  limit?: number
  offset?: number
}

/** Wire shape of `gateway/pan/manifest.py::build_manifest()`. The actual
 * JSON that lands in `backup_records.manifest_json` AND `manifest.json`
 * inside the tar. Top-level identity fields (job_id / user_id / edit_gen)
 * are NESTED under `job_record` — the same shape the JobRecord serializer
 * produces — not promoted to the root. Production 2026-05-25: an earlier
 * flat-root interface caused the admin manifest dialog to render blank
 * (every read was `undefined`).
 */
export interface BackupManifest {
  backup_format_version: number
  created_at_utc: string
  source_host?: string
  job_record: {
    job_id: string
    status?: string
    user_id: string
    edit_generation: number
    [k: string]: unknown
  }
  file_inventory: Array<{
    path: string
    size: number
    sha256: string
  }>
  r2_artifacts_snapshot?: Array<{
    artifact_key: string
    r2_key: string
    size?: number
  }>
}

export interface EnqueueResponse {
  task_id: string
  job_id: string
  status: string
}

export interface BatchBackupResponse {
  succeeded: Array<{ job_id: string; task_id: string }>
  failed: Array<{ job_id: string; reason: string }>
}

// ===========================================================================
// fetch helper — same shape as support.ts so error handling stays consistent
// ===========================================================================

async function panFetch<T>(url: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set("Accept", "application/json")
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json; charset=utf-8")
  }
  const res = await fetch(url, {
    ...init,
    credentials: "include",
    headers,
  })
  const raw = await res.text()
  let parsed: unknown = null
  if (raw) {
    try {
      parsed = JSON.parse(raw)
    } catch {
      parsed = raw
    }
  }
  if (!res.ok) {
    const detail =
      parsed && typeof parsed === "object" && "detail" in parsed
        ? String((parsed as { detail: unknown }).detail)
        : `pan admin API request failed: ${res.status}`
    const err = new Error(detail) as Error & {
      status?: number
      payload?: unknown
    }
    err.status = res.status
    err.payload = parsed
    throw err
  }
  return parsed as T
}

// ===========================================================================
// Endpoints
// ===========================================================================

/** GET /api/admin/pan/status — connection state + quota. */
export async function getPanStatus(): Promise<PanStatus> {
  return panFetch<PanStatus>("/api/admin/pan/status")
}

/** GET /api/admin/pan/backups — list backup records. */
export async function listBackups(params?: {
  job_id?: string
  status?: BackupRecordStatus
  limit?: number
  offset?: number
}): Promise<BackupListResponse> {
  const qs = new URLSearchParams()
  if (params?.job_id) qs.set("job_id", params.job_id)
  if (params?.status) qs.set("status", params.status)
  if (params?.limit !== undefined) qs.set("limit", String(params.limit))
  if (params?.offset !== undefined) qs.set("offset", String(params.offset))
  const suffix = qs.toString() ? `?${qs.toString()}` : ""
  return panFetch<BackupListResponse>(`/api/admin/pan/backups${suffix}`)
}

/** Envelope shape returned by `gateway/pan/admin_api.py::get_backup_manifest`.
 * The actual ``BackupManifest`` is nested under `.manifest` — callers should
 * use ``getBackupManifest()`` which unwraps it. */
export interface BackupManifestEnvelope {
  backup_id: string
  status: string
  manifest: BackupManifest
}

/** GET /api/admin/pan/backups/{id}/manifest — read tar's manifest.json.
 *
 * Backend returns `{ backup_id, status, manifest: {...} }`. We unwrap the
 * envelope so callers always receive the actual manifest. Previously the
 * envelope was returned as-is and typed as `BackupManifest`, which made
 * every field read `undefined` (admin manifest dialog rendered blank). */
export async function getBackupManifest(
  backupId: string,
): Promise<BackupManifest> {
  const envelope = await panFetch<BackupManifestEnvelope>(
    `/api/admin/pan/backups/${encodeURIComponent(backupId)}/manifest`,
  )
  return envelope.manifest
}

/** POST /api/admin/pan/backups — enqueue a single backup task. 202. */
export async function enqueueBackup(jobId: string): Promise<EnqueueResponse> {
  return panFetch<EnqueueResponse>("/api/admin/pan/backups", {
    method: "POST",
    body: JSON.stringify({ job_id: jobId }),
  })
}

/** POST /api/admin/pan/backups/batch — enqueue many. 202. */
export async function enqueueBackupBatch(
  jobIds: string[],
): Promise<BatchBackupResponse> {
  return panFetch<BatchBackupResponse>("/api/admin/pan/backups/batch", {
    method: "POST",
    body: JSON.stringify({ job_ids: jobIds }),
  })
}

/** POST /api/admin/pan/restores — enqueue a restore task. 202. */
export async function enqueueRestore(jobId: string): Promise<EnqueueResponse> {
  return panFetch<EnqueueResponse>("/api/admin/pan/restores", {
    method: "POST",
    body: JSON.stringify({ job_id: jobId }),
  })
}

/** DELETE /api/admin/pan/credentials — revoke + delete admin's pan auth. */
export async function disconnectPan(): Promise<void> {
  await panFetch<null>("/api/admin/pan/credentials", { method: "DELETE" })
}

/**
 * DELETE /api/admin/pan/backups/{id} — soft-delete a backup record.
 *
 * 412 is returned (and thrown) when this is the only recoverable copy —
 * the gateway refuses to delete the user's last safety net unless
 * confirmed via {confirm: true}.
 */
export async function deleteBackup(
  backupId: string,
  options?: { confirm?: boolean },
): Promise<void> {
  const qs = options?.confirm ? "?confirm=true" : ""
  await panFetch<null>(
    `/api/admin/pan/backups/${encodeURIComponent(backupId)}${qs}`,
    { method: "DELETE" },
  )
}

/**
 * OAuth connect entry — the gateway's POST /api/admin/pan/connect
 * returns a 302 to Baidu's authorize URL. Browsers can't follow
 * cross-origin 302 from fetch reliably, so we use a native form POST
 * with method="POST" — the browser follows the 302 itself, lands on
 * Baidu, then on /api/admin/pan/callback, then on /admin/pan/dashboard.
 *
 * Pages render this as:
 *
 *     <form action={CONNECT_URL} method="POST">
 *       <Button type="submit">连接百度网盘</Button>
 *     </form>
 *
 * Exposed as a constant rather than a fetch helper so we don't
 * accidentally trigger CORS preflight or break the redirect chain.
 */
export const CONNECT_URL = "/api/admin/pan/connect"

// ===========================================================================
// UI presentation helpers — keep style decisions out of pages
// ===========================================================================

export const BACKUP_STATUS_LABEL: Record<BackupRecordStatus, string> = {
  uploading: "上传中",
  uploaded: "已备份",
  failed: "失败",
  deleted: "已删除",
  restoring: "恢复中",
  restored: "已恢复",
}

export const BACKUP_STATUS_TONE: Record<
  BackupRecordStatus,
  "active" | "success" | "danger" | "muted" | "info"
> = {
  uploading: "active",
  uploaded: "success",
  failed: "danger",
  deleted: "muted",
  restoring: "active",
  restored: "info",
}

/** Format bytes as GB with 2 decimals (1e9 not 2^30 — matches Baidu's display). */
export function formatBytesGB(bytes: number): string {
  if (!bytes || bytes <= 0) return "0 GB"
  return `${(bytes / 1e9).toFixed(2)} GB`
}

/** ISO timestamp → human label "2026-05-19 14:30 BJT". null/undefined → "—". */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—"
  try {
    const d = new Date(iso)
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
