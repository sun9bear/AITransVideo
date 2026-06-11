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

/** Upload a raw video file. Returns the upload response with admission info. */
export async function uploadPreviewVideo(
  file: File,
  onProgress: (pct: number) => void,
  signal?: AbortSignal,
): Promise<UploadResponse> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/gateway/anonymous-preview/upload')
    xhr.withCredentials = true

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        onProgress(Math.round((e.loaded / e.total) * 100))
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

/** Confirm consent and start processing the preview. */
export async function createPreview(
  previewId: string,
  clientConfirmedAt: string,
): Promise<CreateResponse> {
  const resp = await fetch(`/gateway/anonymous-preview/${previewId}/create`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      anonymous_consent: {
        voice_rights_confirmed: true,
        client_confirmed_at: clientConfirmedAt,
      },
    }),
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
    file_too_large: '文件超过 200MB 限制',
    unsupported_format: '不支持的视频格式，请上传 mp4、mov、m4v 或 webm',
    quota_exceeded: '系统预览配额已满，请稍后再试',
    service_unavailable: '预览服务暂不可用，请稍后再试',
  }
  return MAP[reason] ?? `上传被拒绝（${reason}）`
}

function mapCreateError(status: number, raw: string): string {
  if (status === 403) return '需要先确认版权声明才能继续'
  if (status === 409) return '该预览已在处理中，请稍候'
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
