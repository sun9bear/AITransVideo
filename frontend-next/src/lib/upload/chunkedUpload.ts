/**
 * 大文件分片上传客户端 — plan 2026-06-11 §3.9（P2 前端）。
 *
 * 选路阈值 / 切片大小 / 2GB 上限全部从 R6 limits 端点动态拉取（不硬编码，
 * 沿用 APF limits 先例）。上传流程：
 *
 *   Web Worker 增量 SHA-256（整文件 + 每片，一遍读盘）
 *     → POST init（命中四元组续传 → 按位图补传）
 *     → 并发 3 片 PUT，片级失败指数退避重试 3 次
 *     → POST complete → ready（或 202 轮询 status 至 ready，"正在合并校验…"）
 *     → 返回 opaque upload ref（``chunked:{upload_id}``，不是文件路径）
 *
 * 失败不自动回退单请求路径——大文件回 CF 单请求必 413（plan §3.9）。
 */

export interface ChunkedUploadLimits {
  enabled: boolean
  threshold_mb: number
  max_file_mb: number
  chunk_mb: number
}

export type ChunkedUploadPhase = 'hashing' | 'uploading' | 'merging'

export interface ChunkedUploadProgress {
  phase: ChunkedUploadPhase
  /** 0-100；merging 阶段无百分比语义，恒为 100（Q1 落定：只显示文案） */
  percent: number
  /** 已完成字节数（hashing=已哈希、uploading=已上传；merging 不带）。
   *  供调用方计算实时速度。 */
  bytesDone?: number
}

export interface ChunkedUploadResult {
  /** opaque upload ref：job create 的 source.value 原样传它 */
  uploadRef: string
  fileName: string
}

/**
 * 分片上传错误：同时携带稳定 `code`（token）与原始 `message`（中文）。两个消费方各取所需，
 * 互不破坏（plan 2026-06-28 uiloc Phase 1 收尾 / UI-03g 5-lens LOW #2）：
 *  - 匿名预览面板读 `.code` → `t("uploadError." + code, params)` 渲染本地化文案，消除 /en 中文泄漏；
 *  - 尚未本地化的登录态工作台（TranslationForm）读 `.message` → 原中文，字节一致保留旧行为。
 *
 * `params` 是 code 对应字典模板的 ICU 占位值（status / partIndex / maxMb / detail），**一律传字符串**
 * 以走 ICU 纯文本替换（避免 number 占位被 Intl.NumberFormat 加千分位，破坏与原中文 throw 串的字节一致）。
 */
export class ChunkedUploadError extends Error {
  readonly code: string
  readonly params: Record<string, string>
  constructor(code: string, message: string, params: Record<string, string> = {}) {
    super(message)
    this.name = 'ChunkedUploadError'
    this.code = code
    this.params = params
  }
}

const PART_CONCURRENCY = 3
const PART_MAX_RETRIES = 3
const STATUS_POLL_INTERVAL_MS = 2000
/** complete 轮询保险丝：2GB 合并实测数十秒，10 分钟仍未 ready 视为异常 */
const MERGE_POLL_TIMEOUT_MS = 10 * 60 * 1000

// 端点前缀（plan §9.5）：注册档 / 匿名档共用同一套切片协议，只换前缀。
const REGISTERED_PREFIX = '/gateway/uploads/chunked'
export const ANONYMOUS_CHUNKED_PREFIX = '/gateway/anonymous-preview/chunked'

/** limits 拉取失败 → null（调用方视为通道不可用，回单请求路径） */
export async function getChunkedUploadLimits(
  endpointPrefix: string = REGISTERED_PREFIX,
): Promise<ChunkedUploadLimits | null> {
  try {
    const res = await fetch(`${endpointPrefix}/limits`, {
      credentials: 'include',
    })
    if (!res.ok) return null
    const body = (await res.json()) as ChunkedUploadLimits
    if (typeof body.enabled !== 'boolean' || typeof body.threshold_mb !== 'number') {
      return null
    }
    return body
  } catch {
    return null
  }
}

interface HashResult {
  fileHash: string
  chunkHashes: string[]
}

function hashFileInWorker(
  file: File,
  chunkSize: number,
  onProgress: (bytesHashed: number) => void,
): Promise<HashResult> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('./sha256.worker.ts', import.meta.url))
    worker.onmessage = (event: MessageEvent) => {
      const msg = event.data as
        | { type: 'progress'; bytesHashed: number }
        | { type: 'done'; fileHash: string; chunkHashes: string[] }
        | { type: 'error'; message: string }
      if (msg.type === 'progress') {
        onProgress(msg.bytesHashed)
      } else if (msg.type === 'done') {
        worker.terminate()
        resolve({ fileHash: msg.fileHash, chunkHashes: msg.chunkHashes })
      } else {
        worker.terminate()
        reject(new ChunkedUploadError('hash_failed', msg.message))
      }
    }
    worker.onerror = (event) => {
      worker.terminate()
      reject(new ChunkedUploadError('hash_worker_failed', event.message || '哈希 Worker 启动失败'))
    }
    worker.postMessage({ file, chunkSize })
  })
}

async function readErrorMessage(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json()
    if (typeof body?.message === 'string' && body.message) return body.message
    if (typeof body?.error === 'string' && body.error) return body.error
  } catch {
    // ignore — 非 JSON 错误体
  }
  return fallback
}

interface InitResponse {
  upload_id: string
  chunk_size: number
  total_parts: number
  received_parts: number[]
  resumed: boolean
}

async function initUpload(
  file: File,
  fileHash: string,
  chunkSize: number,
  endpointPrefix: string,
): Promise<InitResponse> {
  const res = await fetch(`${endpointPrefix}/init`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      size: file.size,
      sha256: fileHash,
      chunk_size: chunkSize,
      file_name: file.name,
    }),
  })
  if (!res.ok) {
    throw new ChunkedUploadError(
      'chunk_init_failed',
      await readErrorMessage(res, `上传初始化失败（${res.status}）`),
      { status: String(res.status) },
    )
  }
  return (await res.json()) as InitResponse
}

async function uploadPartWithRetry(
  uploadId: string,
  file: File,
  chunkSize: number,
  partIndex: number,
  chunkHash: string,
  endpointPrefix: string,
): Promise<void> {
  const start = partIndex * chunkSize
  const piece = file.slice(start, Math.min(start + chunkSize, file.size))
  let lastError: Error | null = null
  for (let attempt = 0; attempt <= PART_MAX_RETRIES; attempt += 1) {
    if (attempt > 0) {
      // 指数退避：1s / 2s / 4s
      await new Promise((r) => setTimeout(r, 1000 * 2 ** (attempt - 1)))
    }
    try {
      const res = await fetch(
        `${endpointPrefix}/${uploadId}/part/${partIndex}`,
        {
          method: 'PUT',
          credentials: 'include',
          headers: { 'x-chunk-sha256': chunkHash },
          body: piece,
        },
      )
      if (res.ok) return
      // 429 / 5xx 可重试；4xx 协议错误（404/409/413/422）重试无意义，立即失败
      if (res.status === 429 || res.status >= 500) {
        lastError = new ChunkedUploadError(
          'part_upload_failed',
          await readErrorMessage(res, `分片 ${partIndex} 上传失败（${res.status}）`),
          { partIndex: String(partIndex), status: String(res.status) },
        )
        continue
      }
      throw new ChunkedUploadError(
        'part_upload_failed',
        await readErrorMessage(res, `分片 ${partIndex} 上传失败（${res.status}）`),
        { partIndex: String(partIndex), status: String(res.status) },
      )
    } catch (err) {
      if (err instanceof TypeError) {
        // 网络层失败（断网 / CF 瞬断）→ 重试
        lastError = new ChunkedUploadError(
          'part_network_retried',
          `分片 ${partIndex} 网络错误，已重试`,
          { partIndex: String(partIndex) },
        )
        continue
      }
      throw err
    }
  }
  throw lastError ?? new ChunkedUploadError(
    'part_upload_failed_final',
    `分片 ${partIndex} 上传失败`,
    { partIndex: String(partIndex) },
  )
}

async function completeAndWaitReady(
  uploadId: string,
  onProgress: (p: ChunkedUploadProgress) => void,
): Promise<void> {
  const res = await fetch(`${REGISTERED_PREFIX}/${uploadId}/complete`, {
    method: 'POST',
    credentials: 'include',
  })
  if (res.status === 200) return
  if (res.status !== 202) {
    throw new ChunkedUploadError(
      'merge_verify_failed',
      await readErrorMessage(res, `合并校验失败（${res.status}）`),
      { detail: String(res.status) },
    )
  }
  // 202 in_progress：轮询 R4 至 ready（completing 期间 UI 显示"正在合并校验…"）
  const deadline = Date.now() + MERGE_POLL_TIMEOUT_MS
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, STATUS_POLL_INTERVAL_MS))
    onProgress({ phase: 'merging', percent: 100 })
    const statusRes = await fetch(`${REGISTERED_PREFIX}/${uploadId}/status`, {
      credentials: 'include',
    })
    if (!statusRes.ok) {
      throw new ChunkedUploadError('merge_status_failed', await readErrorMessage(statusRes, '查询合并状态失败'))
    }
    const body = await statusRes.json()
    if (body.state === 'ready') return
    if (body.state !== 'completing') {
      throw new ChunkedUploadError(
        'merge_verify_failed',
        `合并校验失败（${body.failure_reason ?? body.state}）`,
        { detail: String(body.failure_reason ?? body.state) },
      )
    }
  }
  throw new ChunkedUploadError('merge_verify_timeout', '合并校验超时，请稍后在任务页重试')
}

/**
 * 匿名档 complete（plan §9.1 A3）：200 响应体 = /upload 同形 UploadResponse
 * （complete 即消费，无 ready 滞留态）。202 只在服务端崩溃残留 completing
 * 时出现 → 轮询 A4 等 completing 结束后重发 complete（consumed 幂等返回
 * 已存响应，intake 不会二跑）。
 */
async function completeAnonymousAndWait(
  uploadId: string,
  onProgress: (p: ChunkedUploadProgress) => void,
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + MERGE_POLL_TIMEOUT_MS
  for (;;) {
    const res = await fetch(`${ANONYMOUS_CHUNKED_PREFIX}/${uploadId}/complete`, {
      method: 'POST',
      credentials: 'include',
    })
    if (res.status === 200) {
      return (await res.json()) as Record<string, unknown>
    }
    if (res.status !== 202) {
      throw new ChunkedUploadError(
        'merge_verify_failed',
        await readErrorMessage(res, `合并校验失败（${res.status}）`),
        { detail: String(res.status) },
      )
    }
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, STATUS_POLL_INTERVAL_MS))
      onProgress({ phase: 'merging', percent: 100 })
      const statusRes = await fetch(`${ANONYMOUS_CHUNKED_PREFIX}/${uploadId}/status`, {
        credentials: 'include',
      })
      if (!statusRes.ok) {
        throw new ChunkedUploadError('merge_status_failed', await readErrorMessage(statusRes, '查询合并状态失败'))
      }
      const body = await statusRes.json()
      if (body.state !== 'completing') break // ready/consumed/receiving → 重发 complete 定夺
    }
    if (Date.now() >= deadline) {
      throw new ChunkedUploadError('merge_verify_timeout_anon', '合并校验超时，请稍后重试')
    }
  }
}

/**
 * 分片上传入口。返回 opaque upload ref（``chunked:{upload_id}``）。
 * 失败抛 ChunkedUploadError（`.code` 给面板本地化，`.message` 中文给旧工作台）；
 * **不**自动回退单请求路径。
 */
export async function uploadFileInChunks(
  file: File,
  limits: ChunkedUploadLimits,
  onProgress: (p: ChunkedUploadProgress) => void,
): Promise<ChunkedUploadResult> {
  const chunkSize = limits.chunk_mb * 1024 * 1024
  if (file.size > limits.max_file_mb * 1024 * 1024) {
    throw new ChunkedUploadError(
      'file_too_large_chunked',
      `文件超过 ${limits.max_file_mb}MB 上限，请压缩后重试`,
      { maxMb: String(limits.max_file_mb) },
    )
  }

  // 1. 哈希（Web Worker 增量，2GB 约 10-20s，单独显示进度）
  onProgress({ phase: 'hashing', percent: 0 })
  const { fileHash, chunkHashes } = await hashFileInWorker(file, chunkSize, (bytes) => {
    onProgress({ phase: 'hashing', percent: Math.floor((bytes / file.size) * 100), bytesDone: bytes })
  })

  // 2. init + 3. 并发上传（注册档/匿名档共享）
  const init = await hashlessInitAndUploadParts(
    file, fileHash, chunkHashes, chunkSize, REGISTERED_PREFIX, onProgress,
  )

  // 4. complete（202 → 轮询至 ready）
  onProgress({ phase: 'merging', percent: 100 })
  await completeAndWaitReady(init.upload_id, onProgress)

  return { uploadRef: `chunked:${init.upload_id}`, fileName: file.name }
}

async function hashlessInitAndUploadParts(
  file: File,
  fileHash: string,
  chunkHashes: string[],
  chunkSize: number,
  endpointPrefix: string,
  onProgress: (p: ChunkedUploadProgress) => void,
): Promise<InitResponse> {
  // init（命中续传 → received_parts 位图补传）
  const init = await initUpload(file, fileHash, chunkSize, endpointPrefix)
  const received = new Set(init.received_parts)
  const pending = Array.from({ length: init.total_parts }, (_, n) => n).filter(
    (n) => !received.has(n),
  )

  // 并发 3 片上传
  let uploadedBytes = init.received_parts.reduce(
    (acc, n) => acc + Math.min(chunkSize, file.size - n * chunkSize),
    0,
  )
  onProgress({
    phase: 'uploading',
    percent: Math.floor((uploadedBytes / file.size) * 100),
  })
  let cursor = 0
  const runWorker = async () => {
    while (cursor < pending.length) {
      const myIndex = cursor
      cursor += 1
      const partIndex = pending[myIndex]
      await uploadPartWithRetry(
        init.upload_id, file, chunkSize, partIndex, chunkHashes[partIndex],
        endpointPrefix,
      )
      uploadedBytes += Math.min(chunkSize, file.size - partIndex * chunkSize)
      onProgress({
        phase: 'uploading',
        percent: Math.floor((uploadedBytes / file.size) * 100),
        bytesDone: uploadedBytes,
      })
    }
  }
  await Promise.all(
    Array.from({ length: Math.min(PART_CONCURRENCY, pending.length) }, runWorker),
  )
  return init
}

/**
 * 匿名档分片上传入口（plan §9.5）。返回 complete 的 200 响应体——
 * /upload 同形 UploadResponse（调用方按 admission_decision/status 接现有
 * consent → create → 轮询流程）。失败抛 ChunkedUploadError；不自动回退单请求路径。
 */
export async function uploadFileInChunksAnonymous(
  file: File,
  limits: ChunkedUploadLimits,
  onProgress: (p: ChunkedUploadProgress) => void,
): Promise<Record<string, unknown>> {
  const chunkSize = limits.chunk_mb * 1024 * 1024
  if (file.size > limits.max_file_mb * 1024 * 1024) {
    throw new ChunkedUploadError(
      'file_too_large_chunked',
      `文件超过 ${limits.max_file_mb}MB 上限，请压缩后重试`,
      { maxMb: String(limits.max_file_mb) },
    )
  }

  onProgress({ phase: 'hashing', percent: 0 })
  const { fileHash, chunkHashes } = await hashFileInWorker(file, chunkSize, (bytes) => {
    onProgress({ phase: 'hashing', percent: Math.floor((bytes / file.size) * 100), bytesDone: bytes })
  })

  const init = await hashlessInitAndUploadParts(
    file, fileHash, chunkHashes, chunkSize, ANONYMOUS_CHUNKED_PREFIX, onProgress,
  )

  onProgress({ phase: 'merging', percent: 100 })
  return completeAnonymousAndWait(init.upload_id, onProgress)
}
