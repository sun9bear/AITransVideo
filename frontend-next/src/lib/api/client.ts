import { buildBackendUrl, resolveJobApiBaseUrl } from '@/lib/api/config'

type RequestBody = BodyInit | Record<string, unknown> | undefined

type RequestOptions = Omit<RequestInit, 'body'> & {
  body?: RequestBody
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
    const headers = new Headers(init.headers)
    const body = this.serializeBody(init.body, headers)
    const response = await fetch(this.buildUrl(path), {
      ...init,
      body,
      headers,
    })
    const payload = await this.parsePayload(response)

    if (!response.ok) {
      const message =
        payload && typeof payload === 'object' && 'message' in payload
          ? String(payload.message)
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
