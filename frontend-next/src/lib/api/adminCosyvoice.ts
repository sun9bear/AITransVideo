export interface AdminCosyvoiceSettings {
  cosyvoice_runtime_endpoint_mode: string
  cosyvoice_offline_endpoint_mode: string
  cosyvoice_clone_worker_enabled: boolean
  cosyvoice_clone_default_target_model: string
  cosyvoice_clone_user_allowlist: string[]
  cosyvoice_clone_general_availability_enabled: boolean
  cosyvoice_clone_max_voices_per_user: number
  express_cosyvoice_auto_clone_enabled: boolean
  express_cosyvoice_auto_clone_user_allowlist: string[]
  express_cosyvoice_auto_clone_main_speaker_min_ratio: number
  express_cosyvoice_auto_clone_main_speaker_min_lines: number
  express_cosyvoice_auto_clone_sample_max_seconds: number
  express_cosyvoice_auto_clone_target_model: string
  express_cosyvoice_auto_clone_per_user_daily_cap: number
  express_cosyvoice_auto_clone_per_user_active_temp_cap: number
  express_cosyvoice_auto_clone_reservation_ttl_minutes: number
}

export interface AdminCosyvoiceRuntimeStatus {
  manual_clone_runtime_ready: boolean
  manual_clone_runtime_unavailable_code: string | null
  mainland_worker: {
    effective_enabled: boolean
    config_ready: boolean
    url_configured: boolean
    hmac_key_id_configured: boolean
    hmac_secret_configured: boolean
  }
  sample_uploader: {
    backend: string
    production_ready: boolean
    missing_config_fields: string[]
  }
  cleanup: {
    dry_run_env: string | null
    dry_run_effective: boolean
  }
}

export interface AdminCosyvoiceControlResponse {
  settings: AdminCosyvoiceSettings
  runtime: AdminCosyvoiceRuntimeStatus
}

async function parseAdminCosyvoiceResponse(resp: Response) {
  const body = await resp.json().catch(() => null)
  if (!resp.ok) {
    const detail =
      body && typeof body === 'object' && 'detail' in body
        ? JSON.stringify((body as { detail?: unknown }).detail)
        : `HTTP ${resp.status}`
    throw new Error(detail)
  }
  return body as AdminCosyvoiceControlResponse
}

export async function getAdminCosyvoiceControl(): Promise<AdminCosyvoiceControlResponse> {
  const resp = await fetch('/api/admin/cosyvoice-control', {
    credentials: 'include',
  })
  return parseAdminCosyvoiceResponse(resp)
}

export async function updateAdminCosyvoiceControl(
  settings: AdminCosyvoiceSettings,
): Promise<AdminCosyvoiceControlResponse> {
  const resp = await fetch('/api/admin/cosyvoice-control', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  })
  return parseAdminCosyvoiceResponse(resp)
}
