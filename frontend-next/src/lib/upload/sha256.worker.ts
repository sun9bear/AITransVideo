/**
 * 分片上传哈希 Worker — plan 2026-06-11 §3.9 / Q3。
 *
 * `crypto.subtle.digest` 是 one-shot 接口，吃不下 2GB —— 这里用 hash-wasm
 * 的增量 API 在 Worker 线程里分块喂入，主线程不卡。一遍读盘同时产出：
 *  - 整文件 SHA-256（init 声明 + 服务端合并后比对）
 *  - 每个上传分片的 SHA-256（R2 的 X-Chunk-SHA256 头，r3 片级完整性）
 *
 * 消息协议：
 *  in : { file: File, chunkSize: number }
 *  out: { type: 'progress', bytesHashed: number }
 *     | { type: 'done', fileHash: string, chunkHashes: string[] }
 *     | { type: 'error', message: string }
 */
import { createSHA256 } from 'hash-wasm'

// 读盘子块 8MB：内存占用与喂哈希器的吞吐折中（与上传分片大小无关）。
const READ_BLOCK_BYTES = 8 * 1024 * 1024

interface HashRequest {
  file: File
  chunkSize: number
}

type HashResponse =
  | { type: 'progress'; bytesHashed: number }
  | { type: 'done'; fileHash: string; chunkHashes: string[] }
  | { type: 'error'; message: string }

// tsconfig 带 DOM lib，window.postMessage 与 worker postMessage 签名冲突——
// 用窄接口断言绕开（worker 运行时 self 即 DedicatedWorkerGlobalScope）。
const ctx = self as unknown as {
  onmessage: ((event: MessageEvent<HashRequest>) => void) | null
  postMessage: (msg: HashResponse) => void
}

ctx.onmessage = async (event: MessageEvent<HashRequest>) => {
  const { file, chunkSize } = event.data
  try {
    const fileHasher = await createSHA256()
    const chunkHasher = await createSHA256()
    const chunkHashes: string[] = []

    const totalParts = Math.ceil(file.size / chunkSize)
    let bytesHashed = 0

    for (let part = 0; part < totalParts; part += 1) {
      const partStart = part * chunkSize
      const partEnd = Math.min(partStart + chunkSize, file.size)
      chunkHasher.init()
      for (let offset = partStart; offset < partEnd; offset += READ_BLOCK_BYTES) {
        const blockEnd = Math.min(offset + READ_BLOCK_BYTES, partEnd)
        const buf = new Uint8Array(await file.slice(offset, blockEnd).arrayBuffer())
        fileHasher.update(buf)
        chunkHasher.update(buf)
        bytesHashed += buf.byteLength
        ctx.postMessage({ type: 'progress', bytesHashed })
      }
      chunkHashes.push(chunkHasher.digest('hex'))
    }

    ctx.postMessage({
      type: 'done',
      fileHash: fileHasher.digest('hex'),
      chunkHashes,
    })
  } catch (err) {
    ctx.postMessage({
      type: 'error',
      message: err instanceof Error ? err.message : '文件哈希计算失败',
    })
  }
}
