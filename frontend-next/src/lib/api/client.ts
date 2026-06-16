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
          : `API request failed with status ${response.status}`
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

    return JSON.parse(raw) as unknown
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
  try {
    return JSON.stringify(detail)
  } catch {
    return String(detail)
  }
}
