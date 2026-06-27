/**
 * anonymousPreview.ts — thin fetch wrappers for the anonymous preview API.
 *
 * All requests are same-origin (no explicit base URL needed) and use
 * credentials:'include' so the gateway can set/read the avt_anon cookie.
 *
 * Stream URL is simply /gateway/anonymous-preview/{id}/stream（同源路径，
 * 直接交给 <video> 的 src；前端对存储后端零感知）。
 */

// 后端 AdmissionDecision 契约枚举值（src/services/anonymous_preview_admission.py）。
// 历史 bug：曾误写成 'admit'|'deny'|'hold'，与后端 'admitted' 错位，导致任何
// 成功上传都被判 upload_denied，漏斗完全不可用（CodeX P0）。
export type UploadAdmissionDecision =
  | 'admitted'
  | 'login_required'
  | 'not_anonymous_funnel'
  | 'rejected'
  | 'failed'
export type PreviewMode = 'dubbed' | 'subtitled'
export type PreviewStatus = 'pending' | 'processing' | 'ready' | 'failed' | 'unknown'

export interface UploadResponse {
  preview_id: string
  status: string
  status_reason: string | null
  mode: PreviewMode | null
  admission_decision: UploadAdmissionDecision
}

export interface CreateResponse {
  preview_id: string
  status: 'processing'
}

export interface StatusResponse {
  preview_status: PreviewStatus
  stage: string | null
  progress: number | null
  mode: PreviewMode | null
}

// 当前活动 lane（plan 2026-06-12 §G：服务端单点解析，前端零选择）。
// 'express' = 真实快捷版管线；'free' = 免费档；null = 两 lane 都关。
export type ActiveLane = 'free' | 'express' | null

// 当前生效的匿名预览限制（gateway GET /gateway/anonymous-preview/limits，
// 管理员后台热配置）。拉取失败时面板用 DEFAULT_PREVIEW_LIMITS 兜底。
export interface PreviewLimits {
  max_upload_mb: number
  preview_seconds: number
  // plan 2026-06-12 §G：lane 三态（free/express/关闭）由 limits 下发，
  // lane 切换无需重建前端镜像。
  active_lane: ActiveLane
  master_open: boolean
  express_clone_available: boolean
}

// 兜底值与后端出厂默认严格一致（gateway/admin_settings.py APF 限制旋钮段）。
// lane 字段 fail-open（free/开）：拉取失败（网络抖动）时保持既有 UX——
// 上传面板照常渲染，真正的 gate 在服务端（上传会被 404/403 并映射文案）。
export const DEFAULT_PREVIEW_LIMITS: PreviewLimits = {
  max_upload_mb: 200,
  preview_seconds: 180,
  active_lane: 'free',
  master_open: true,
  express_clone_available: false,
}

/** Fetch the currently effective anonymous-preview limits（只读，无需会话）.
 *
 *  失败抛 Error，其 `.message` 为稳定 token（`limits_http` / `invalid_response`）。
 *  调用方（面板 mount effect）目前静默吞掉该异常并回落默认限制，故 token 不直接面向用户；
 *  保持 token 化是为了与本模块「lib 语言中立」约定一致（UI-03g）。 */
export async function getPreviewLimits(): Promise<PreviewLimits> {
  const resp = await fetch('/gateway/anonymous-preview/limits', { credentials: 'include' })
  if (!resp.ok) {
    throw new Error('limits_http')
  }
  const body = (await resp.json()) as Record<string, unknown>
  const maxUploadMb = Number(body.max_upload_mb)
  const previewSeconds = Number(body.preview_seconds)
  if (!Number.isFinite(maxUploadMb) || maxUploadMb <= 0 || !Number.isFinite(previewSeconds) || previewSeconds <= 0) {
    throw new Error('invalid_response')
  }
  // lane 三态解析：旧网关（无字段）按 free/开兼容；显式 null/未知值 = 关闭。
  const rawLane = body.active_lane
  const activeLane: ActiveLane =
    rawLane === 'express' ? 'express'
    : rawLane === 'free' ? 'free'
    : rawLane === undefined ? 'free'
    : null
  const masterOpen = body.master_open === undefined ? activeLane !== null : body.master_open === true
  const expressCloneAvailable =
    activeLane === 'express' && body.express_clone_available === true
  return {
    max_upload_mb: maxUploadMb,
    preview_seconds: previewSeconds,
    active_lane: activeLane,
    master_open: masterOpen,
    express_clone_available: expressCloneAvailable,
  }
}

/** 把预览秒数拆成语言中立的时长描述（180 → {value:3,unit:'minutes'}，
 *  90 → {value:90,unit:'seconds'}）。文案本地化在消费方（面板）经 ICU 完成，
 *  本模块不再产出任何语言相关字符串（UI-03g）。 */
export type PreviewDuration =
  | { value: number; unit: 'minutes' }
  | { value: number; unit: 'seconds' }

export function previewDurationParts(seconds: number): PreviewDuration {
  if (seconds > 0 && seconds % 60 === 0) {
    return { value: seconds / 60, unit: 'minutes' }
  }
  return { value: seconds, unit: 'seconds' }
}

/** Upload a raw video file. Returns the upload response with admission info.
 *  onProgress 第二参为已上传字节数（供调用方算实时速度），可忽略。 */
export async function uploadPreviewVideo(
  file: File,
  onProgress: (pct: number, loadedBytes?: number) => void,
  signal?: AbortSignal,
): Promise<UploadResponse> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/gateway/anonymous-preview/upload')
    xhr.withCredentials = true

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        onProgress(Math.round((e.loaded / e.total) * 100), e.loaded)
      }
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as UploadResponse)
        } catch {
          reject(new Error('invalid_response'))
        }
      } else {
        // 后端若回了真实 error code（如 "rate_limited"），原样透传 —— 面板会先
        // 经 mapUploadError 映射成文案，未命中再回落 errors.upload_http。
        let msg = 'upload_http'
        try {
          const body = JSON.parse(xhr.responseText) as { error?: string; detail?: string }
          msg = body.error ?? body.detail ?? msg
        } catch {
          // keep default token
        }
        reject(new Error(msg))
      }
    }

    xhr.onerror = () => reject(new Error('network_error'))
    // abort 由面板显式吞掉（err.name === 'AbortError' → return）；message 不面向用户。
    xhr.onabort = () => reject(new DOMException('upload_aborted', 'AbortError'))

    if (signal) {
      signal.addEventListener('abort', () => xhr.abort(), { once: true })
    }

    xhr.send(file)
  })
}

/** Confirm consent and start processing the preview.
 *
 *  plan 2026-06-14 §3.1：express lane 的"克隆我的音色"opt-in 经 ``express_consent``
 *  独立发送（与内容权利 ``anonymous_consent`` 分离）。仅当用户在 express 卡片显式
 *  勾选时传 ``autoVoiceClone:true`` → body 带 ``express_consent.auto_voice_clone``；
 *  未勾选/free lane 不带 → 后端 SOFT gate 走 CosyVoice 预设音色（绝不 MiniMax）。
 *  权威确认时间戳由服务端单一来源生成，前端**绝不**构造或发送它。
 */
export async function createPreview(
  previewId: string,
  clientConfirmedAt: string,
  opts?: { autoVoiceClone?: boolean },
): Promise<CreateResponse> {
  const consentBody: Record<string, unknown> = {
    anonymous_consent: {
      voice_rights_confirmed: true,
      client_confirmed_at: clientConfirmedAt,
    },
  }
  // 仅在显式 opt-in 时附带 express_consent（克隆授权）。strict true；不含任何
  // 服务端权威时间戳字段（后端单一来源生成）。
  if (opts?.autoVoiceClone === true) {
    consentBody.express_consent = { auto_voice_clone: true }
  }
  const resp = await fetch(`/gateway/anonymous-preview/${previewId}/create`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(consentBody),
  })

  const text = await resp.text()
  let body: Record<string, unknown> = {}
  try {
    body = JSON.parse(text) as Record<string, unknown>
  } catch {
    // keep empty body
  }

  if (!resp.ok) {
    const raw = (body.error ?? body.detail ?? `HTTP ${resp.status}`) as string
    throw new Error(mapCreateError(resp.status, raw))
  }

  return body as unknown as CreateResponse
}

/** Error from a status poll that carries the HTTP status so the caller can
 *  distinguish a 401 (session expired → stop) from a transient 429/5xx. */
export class PreviewStatusError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'PreviewStatusError'
    this.status = status
  }
}

/** Poll preview processing status. */
export async function getPreviewStatus(previewId: string): Promise<StatusResponse> {
  const resp = await fetch(`/gateway/anonymous-preview/${previewId}/status`, {
    credentials: 'include',
  })
  if (!resp.ok) {
    // message 为稳定 token；面板按 PreviewStatusError.status 分流（401 → 会话过期；
    // 其余 → 有界重试），token 文案兜底走 errors.status_http。
    throw new PreviewStatusError(resp.status, 'status_http')
  }
  return resp.json() as Promise<StatusResponse>
}

/** Returns the URL to use as a <video> src（恒为同源相对路径）. */
export function getPreviewStreamUrl(previewId: string): string {
  return `/gateway/anonymous-preview/${previewId}/stream`
}

// ── Language-neutral code mapping ─────────────────────────────────────────
//
// UI-03g：本模块不再产出任何中文/英文文案。各 mapper 只返回稳定 CODE/TOKEN，
// 由面板经 next-intl `t("<group>." + code)` 渲染，未知 code 用 `t.has` 守卫回落。
// 后端契约 code 列表（gateway anonymous_preview_api._redact_reason / admission）
// 与字典 key 一一对应；新增 code 须同步加字典 key + 此处文档。

/** 已知的 upload status_reason code 集合（仅用于文档/类型参考，运行时不做白名单过滤——
 *  面板对未知 code 用 `t.has` 守卫，回落 statusReason.fallback）。 */
export const KNOWN_STATUS_REASONS = [
  'rate_limited',
  'content_blocked',
  'file_too_large',
  'unsupported_format',
  'duration_exceeded',
  'quota_exceeded',
  'service_unavailable',
] as const

/** Return the stable status_reason CODE as-is（null → ''）. 面板映射 code→文案。 */
export function mapStatusReason(reason: string | null): string {
  return reason ?? ''
}

/** Return the stable upload-error CODE as-is（null/undefined → ''）.
 *
 *  曾覆盖 AD-8 peek / 上传预检 code（rate_limited / preview_queue_full / …）的中文
 *  映射已移到面板字典（uploadError.*）；本函数现仅透传 code，面板 `t.has` 守卫后渲染。
 */
export function mapUploadError(code: string | null | undefined): string {
  return code ?? ''
}

/** Map create-preview failure to a stable token（面板映射 createError.<token>）.
 *  保留 status/raw 分流逻辑，只把返回值从中文文案改成稳定 token。 */
function mapCreateError(status: number, raw: string): string {
  // CodeX 外审 2026-06-12 P1/P2 配套：重试重入被服务端收紧后，409 不再
  // 恒等于"处理中"——区分重试次数耗尽 / 不可重试失败，引导重新上传。
  if (raw === 'retry_exhausted') return 'retry_exhausted'
  if (status === 403) return 'need_consent'
  if (status === 409) return 'already_processing'
  if (status === 429) return 'channel_busy'
  return 'generic'
}

/** 已知的处理阶段 code（参考；未知/ null 由面板回落 stage.fallback）。 */
export const KNOWN_STAGES = [
  'queued',
  'probing',
  'transcribing',
  'translating',
  'synthesizing',
  'mixing',
  'publishing',
] as const

/** Return the stable stage CODE（null/未知 → 'fallback' 哨兵）. 面板映射 stage.<code>. */
export function mapStageLabel(stage: string | null): string {
  if (!stage) return 'fallback'
  return (KNOWN_STAGES as readonly string[]).includes(stage) ? stage : 'fallback'
}
