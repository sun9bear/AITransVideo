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

  constructor(
    message: string,
    status: number,
    payload: unknown,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
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
        throw new ApiError(
          `请求超时（${Math.round(effectiveTimeout / 1000)} 秒无响应），请检查网络后重试`,
          0,
          null,
        )
      }
      throw error
    }
    const payload = await this.parsePayload(response)

    if (!response.ok) {
      const message =
        payload && typeof payload === 'object' && 'message' in payload
          ? String(payload.message)
          : payload && typeof payload === 'object' && 'detail' in payload
            ? stringifyErrorDetail(payload.detail)
          : payload && typeof payload === 'object' && 'error' in payload
            ? String(payload.error)
          : statusFallbackMessage(response.status)
      throw new ApiError(message, response.status, payload)
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
