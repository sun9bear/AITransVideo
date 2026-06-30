import { buildBackendUrl, resolveJobApiBaseUrl } from '@/lib/api/config'

type RequestBody = BodyInit | Record<string, unknown> | undefined

type RequestOptions = Omit<RequestInit, 'body'> & {
  body?: RequestBody
  /**
   * 请求超时（毫秒），传 0 禁用。
   * 默认 DEFAULT_TIMEOUT_MS；body 为 FormData / Blob / ArrayBuffer（上传）
   * 时默认不超时——大文件上传耗时无法预估，由调用方显式设定。
   */
  timeoutMs?: number
}

const DEFAULT_TIMEOUT_MS = 30_000

function isUploadBody(body: RequestBody): boolean {
  return (
    body instanceof FormData ||
    body instanceof Blob ||
    body instanceof ArrayBuffer
  )
}

export class ApiError extends Error {
  readonly status: number
  readonly payload: unknown
  /**
   * 机器可读的后端错误码（如 `insufficient_credits` / `csrf_origin_rejected`）。
   * 从失败响应体提取（detail.error_code / 顶层 error_code / 网关 _error_response 的
   * body.error）。显示层用它查 `errors.code.<code>` 本地化串；查不到则降级到
   * 后端 message / status 兜底。无码时为 null。详见 lib/api/error-localization.ts。
   */
  readonly errorCode: string | null

  constructor(
    message: string,
    status: number,
    payload: unknown,
    errorCode: string | null = null,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
    this.errorCode = errorCode
  }
}

export class ApiClient {
  private readonly baseUrl: string

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl
  }

  async get<T>(path: string, init?: Omit<RequestOptions, 'method'>) {
    return this.request<T>(path, {
      ...init,
      method: 'GET',
    })
  }

  async post<T>(path: string, init?: Omit<RequestOptions, 'method'>) {
    return this.request<T>(path, {
      ...init,
      method: 'POST',
    })
  }

  async request<T>(path: string, init: RequestOptions = {}): Promise<T> {
    const { timeoutMs, ...rest } = init
    const headers = new Headers(rest.headers)
    const body = this.serializeBody(rest.body, headers)

    const effectiveTimeout =
      timeoutMs ?? (isUploadBody(rest.body) ? 0 : DEFAULT_TIMEOUT_MS)
    let signal = rest.signal ?? null
    if (effectiveTimeout > 0) {
      const timeoutSignal = AbortSignal.timeout(effectiveTimeout)
      signal = signal ? AbortSignal.any([signal, timeoutSignal]) : timeoutSignal
    }

    let response: Response
    try {
      response = await fetch(this.buildUrl(path), {
        ...rest,
        body,
        headers,
        signal,
      })
    } catch (error) {
      if (error instanceof DOMException && error.name === 'TimeoutError') {
        const timeoutSeconds = Math.round(effectiveTimeout / 1000)
        // payload 携带 timeoutSeconds 供显示层本地化（status===0 分支）；message 保留
        // 中文串供非组件消费方（getErrorMessage / 直接读 err.message 的旧路径）。
        throw new ApiError(
          `请求超时（${timeoutSeconds} 秒无响应），请检查网络后重试`,
          0,
          { timeoutSeconds },
        )
      }
      throw error
    }
    const payload = await this.parsePayload(response)

    if (!response.ok) {
      const message =
        resolveBackendMessage(payload) ?? statusFallbackMessage(response.status)
      throw new ApiError(
        message,
        response.status,
        payload,
        extractErrorCode(payload),
      )
    }

    return payload as T
  }

  private buildUrl(path: string) {
    return buildBackendUrl(this.baseUrl, path)
  }

  private async parsePayload(response: Response) {
    const raw = await response.text()
    if (!raw) {
      return null
    }

    // 网关重启 / Cloudflare 5xx 时上游返回 HTML 错误页——不是 JSON 就不解析，
    // 交给 status 分支生成中文兜底消息，而不是把 SyntaxError 抛给用户。
    const contentType = response.headers.get('content-type') ?? ''
    if (!contentType.includes('json')) {
      return null
    }

    try {
      return JSON.parse(raw) as unknown
    } catch {
      return null
    }
  }

  private serializeBody(body: RequestBody, headers: Headers) {
    if (body === undefined) {
      return undefined
    }

    if (
      typeof body === 'string' ||
      body instanceof FormData ||
      body instanceof URLSearchParams
    ) {
      return body
    }

    if (body instanceof Blob || body instanceof ArrayBuffer) {
      return body
    }

    headers.set('Content-Type', 'application/json; charset=utf-8')
    return JSON.stringify(body)
  }
}

export const apiClient = new ApiClient(resolveJobApiBaseUrl())

/**
 * 提取后端给出的人类可读错误消息（message / detail / error），无则返回 null
 * 让调用方降级到 status 兜底。**presence-based** 语义与历史失败分支逐字节一致
 * （`'message' in payload` 取 String(message)，即便为空串），故非组件消费方
 * （直接读 err.message）行为不变（红线 R1）。显示层（error-localization）复用本函数
 * 判断「是否有后端消息」——有则原样显示（zh 字节一致 / en 已知漏中文缺口，待
 * UI-BE-01 补码），无才本地化 status 串。
 */
export function resolveBackendMessage(payload: unknown): string | null {
  if (payload && typeof payload === 'object') {
    if ('message' in payload) {
      return String((payload as { message: unknown }).message)
    }
    if ('detail' in payload) {
      return stringifyErrorDetail((payload as { detail: unknown }).detail)
    }
    if ('error' in payload) {
      return String((payload as { error: unknown }).error)
    }
  }
  return null
}

/**
 * 从失败响应体提取机器可读错误码。三种后端约定都覆盖：
 *   - FastAPI 结构化 detail：{ detail: { error_code, message } }
 *   - 顶层 error_code
 *   - 网关 _error_response：{ error: <code>, message: <msg> }（code 放 body.error）
 * 提取从宽即可——显示层用 `errors.code.<code>` 的 t.has() 守门，命不中（含把散文
 * message 误当 code 的情况）自动降级，故误提取无害。无码返回 null。
 */
export function extractErrorCode(payload: unknown): string | null {
  if (!payload || typeof payload !== 'object') {
    return null
  }
  const p = payload as {
    detail?: unknown
    error_code?: unknown
    error?: unknown
  }
  if (p.detail && typeof p.detail === 'object' && 'error_code' in p.detail) {
    const code = (p.detail as { error_code?: unknown }).error_code
    if (typeof code === 'string' && code) {
      return code
    }
  }
  if (typeof p.error_code === 'string' && p.error_code) {
    return p.error_code
  }
  if (typeof p.error === 'string' && p.error) {
    return p.error
  }
  return null
}

function stringifyErrorDetail(detail: unknown): string {
  if (typeof detail === 'string') {
    return detail
  }
  // 结构化业务错误（如 402 扣费门）的 detail 是 {error_code, message, ...}，
  // 用户只需要 message。
  if (detail && typeof detail === 'object' && 'message' in detail) {
    return String((detail as { message: unknown }).message)
  }
  // FastAPI 422 校验错误的 detail 是数组，取首条 msg 而不是整串 JSON。
  if (Array.isArray(detail)) {
    const first = detail.find(
      (item) => item && typeof item === 'object' && 'msg' in item,
    ) as { msg?: unknown } | undefined
    if (first?.msg) {
      return `请求参数有误：${String(first.msg)}`
    }
  }
  try {
    return JSON.stringify(detail)
  } catch {
    return String(detail)
  }
}

function statusFallbackMessage(status: number): string {
  if (status === 401) return '登录已过期，请重新登录'
  if (status === 403) return '没有权限执行此操作'
  if (status === 404) return '请求的资源不存在'
  if (status === 502 || status === 503 || status === 504) {
    return '服务暂时不可用，请稍后重试'
  }
  if (status >= 500) return '服务器开小差了，请稍后重试'
  return `请求失败（${status}）`
}
