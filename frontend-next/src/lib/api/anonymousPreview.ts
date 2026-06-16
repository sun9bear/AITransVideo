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

/** Fetch the currently effective anonymous-preview limits（只读，无需会话）. */
export async function getPreviewLimits(): Promise<PreviewLimits> {
  const resp = await fetch('/gateway/anonymous-preview/limits', { credentials: 'include' })
  if (!resp.ok) {
    throw new Error(`限制查询失败（HTTP ${resp.status}）`)
  }
  const body = (await resp.json()) as Record<string, unknown>
  const maxUploadMb = Number(body.max_upload_mb)
  const previewSeconds = Number(body.preview_seconds)
  if (!Number.isFinite(maxUploadMb) || maxUploadMb <= 0 || !Number.isFinite(previewSeconds) || previewSeconds <= 0) {
    throw new Error('限制响应格式无效')
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

/** 把预览秒数格式化成提示文案用的时长（180 → "3 分钟"，90 → "90 秒"）. */
export function formatPreviewDuration(seconds: number): string {
  if (seconds > 0 && seconds % 60 === 0) {
    return `${seconds / 60} 分钟`
  }
  return `${seconds} 秒`
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
          reject(new Error('服务器返回了无效的响应格式'))
        }
      } else {
        let msg = `上传失败（HTTP ${xhr.status}）`
        try {
          const body = JSON.parse(xhr.responseText) as { error?: string; detail?: string }
          msg = body.error ?? body.detail ?? msg
        } catch {
          // keep default message
        }
        reject(new Error(msg))
      }
    }

    xhr.onerror = () => reject(new Error('网络错误，请检查连接后重试'))
    xhr.onabort = () => reject(new DOMException('上传已取消', 'AbortError'))

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
    throw new PreviewStatusError(resp.status, `状态查询失败（HTTP ${resp.status}）`)
  }
  return resp.json() as Promise<StatusResponse>
}

/** Returns the URL to use as a <video> src（恒为同源相对路径）. */
export function getPreviewStreamUrl(previewId: string): string {
  return `/gateway/anonymous-preview/${previewId}/stream`
}

// ── Chinese error message mapping ─────────────────────────────────────────

/** Map upload status_reason codes to user-facing Chinese text. */
export function mapStatusReason(reason: string | null): string {
  if (!reason) return ''
  const MAP: Record<string, string> = {
    rate_limited: '今日预览次数已用完，请明天再来',
    content_blocked: '内容未通过合规检查，无法预览',
    // 上限是 admin 热配置（limits 端点），此处不硬编码具体数值
    file_too_large: '文件超过大小限制，请压缩后重试',
    unsupported_format: '不支持的视频格式，请上传 mp4、mov、m4v 或 webm',
    // 源视频时长超过匿名预览上限（gateway _redact_reason 细分出的 code）→ 引导换更短视频。
    duration_exceeded: '视频时长超限，请更换视频再上传',
    quota_exceeded: '系统预览配额已满，请稍后再试',
    service_unavailable: '预览服务暂不可用，请稍后再试',
  }
  return MAP[reason] ?? `上传被拒绝（${reason}）`
}

/** Map upload-time HTTP error codes (429/403/413/…) to friendly Chinese.
 *
 *  2026-06-13：上传 XHR 失败时原本把后端原始 error code（如 "rate_limited"）
 *  直接抛给用户，绕过了 mapStatusReason，UI 显示生硬英文。本函数覆盖
 *  AD-8 peek / 上传预检会返回的 code；未知 code 原样返回（调用方再兜底）。
 */
export function mapUploadError(code: string | null | undefined): string {
  if (!code) return ''
  const MAP: Record<string, string> = {
    rate_limited: '今日免费预览次数已用完，请明天再来',
    preview_queue_full: '预览通道繁忙，请稍后再试',
    file_too_large: '文件超过大小限制，请压缩后重试',
    unsupported_media_type: '不支持的视频格式，请上传 mp4、mov、m4v 或 webm',
    gate_unavailable: '预览服务暂时不可用，请稍后再试',
    storage_error: '服务器存储繁忙，请稍后再试',
    csrf_origin_rejected: '请求来源校验失败，请刷新页面后重试',
    feature_not_available: '免注册试用暂未开放',
    anonymous_preview_disabled: '免注册试用暂未开放',
  }
  return MAP[code] ?? code
}

function mapCreateError(status: number, raw: string): string {
  // CodeX 外审 2026-06-12 P1/P2 配套：重试重入被服务端收紧后，409 不再
  // 恒等于"处理中"——区分重试次数耗尽 / 不可重试失败，引导重新上传。
  if (raw === 'retry_exhausted') return '重试次数已用完，请重新上传一个视频'
  if (status === 403) return '需要先确认版权声明才能继续'
  if (status === 409) return '该预览已在处理中或无法重试，请稍候或重新上传'
  if (status === 429) return '预览通道繁忙，请稍后再试'
  return raw || `创建预览失败（HTTP ${status}）`
}

/** Map processing stage codes to readable progress descriptions. */
export function mapStageLabel(stage: string | null): string {
  if (!stage) return '处理中…'
  const MAP: Record<string, string> = {
    queued: '等待处理…',
    probing: '分析视频…',
    transcribing: '转录音频…',
    translating: '翻译中…',
    synthesizing: '合成配音…',
    mixing: '混音合成…',
    publishing: '生成预览…',
  }
  return MAP[stage] ?? '处理中…'
}
