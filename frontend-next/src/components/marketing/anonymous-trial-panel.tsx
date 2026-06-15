"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { UploadCloud, CheckCircle2, AlertCircle, Loader2, Film } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { buttonVariants } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import {
  uploadPreviewVideo,
  createPreview,
  getPreviewStatus,
  getPreviewStreamUrl,
  getPreviewLimits,
  formatPreviewDuration,
  mapStatusReason,
  mapUploadError,
  mapStageLabel,
  DEFAULT_PREVIEW_LIMITS,
  PreviewStatusError,
  type PreviewLimits,
  type UploadResponse,
  type PreviewStatus,
} from "@/lib/api/anonymousPreview"
import { setAnonClaimHint } from "@/lib/api/claim"
import {
  getChunkedUploadLimits,
  uploadFileInChunksAnonymous,
  ANONYMOUS_CHUNKED_PREFIX,
  type ChunkedUploadLimits,
} from "@/lib/upload/chunkedUpload"

// ── Constants ─────────────────────────────────────────────────────────────

// 大小/时长限制不再硬编码：mount 时从 /gateway/anonymous-preview/limits 拉取
// （admin 后台热配置），拉不到回落 DEFAULT_PREVIEW_LIMITS（200MB / 180s）。
const ACCEPTED_TYPES = ['video/mp4', 'video/quicktime', 'video/x-m4v', 'video/webm']
const ACCEPTED_EXTENSIONS = ['.mp4', '.mov', '.m4v', '.webm']
const POLL_INTERVAL_MS = 5_000
// Stop polling after this many consecutive transient (429/5xx/network) errors
// so a degraded gateway leaves the user with a clear message, not an endless
// "等待处理…" spinner. A 401 (session expired) stops immediately.
const MAX_POLL_ERRORS = 4
const MAX_POLL_BACKOFF_MS = 30_000

// ── Types ─────────────────────────────────────────────────────────────────

type PanelStep =
  | 'idle'
  | 'uploading'
  | 'upload_denied'
  | 'consent'
  | 'processing'
  | 'ready'
  // 任务诚实失败（plan 2026-06-12 §E）：record/teaser 保留，可一键重试
  // （复用 preview_id 重新 create，无需重新上传）。
  | 'failed'
  | 'error'

interface PanelState {
  step: PanelStep
  uploadPct: number
  /** 上传阶段文案：单请求恒"上传中…"；分片路径区分 校验文件/上传/合并校验 */
  uploadStageLabel: string
  /** 实时上传速度文案（如 "2.3 MB/s"）；非上传阶段为空串 */
  uploadSpeed: string
  deniedReason: string
  errorMsg: string
  previewId: string
  stageLabel: string
  progress: number | null
  consentChecked: boolean
  /** plan 2026-06-14 §3.1：express lane "克隆我的音色" opt-in（默认未勾选，
   *  与内容权利 consentChecked 分离）。仅 express lane 渲染该勾选。 */
  expressAutoVoiceClone: boolean
}

const INITIAL_STATE: PanelState = {
  step: 'idle',
  uploadPct: 0,
  uploadStageLabel: '上传中…',
  uploadSpeed: '',
  deniedReason: '',
  errorMsg: '',
  previewId: '',
  stageLabel: '',
  progress: null,
  consentChecked: false,
  expressAutoVoiceClone: false,
}

// ── Helpers ───────────────────────────────────────────────────────────────

function formatSpeed(bytesPerSecond: number): string {
  if (bytesPerSecond >= 1024 * 1024) {
    return `${(bytesPerSecond / 1024 / 1024).toFixed(1)} MB/s`
  }
  return `${Math.max(1, Math.round(bytesPerSecond / 1024))} KB/s`
}

function validateFile(file: File, maxUploadMb: number): string | null {
  if (!ACCEPTED_TYPES.includes(file.type) && !ACCEPTED_EXTENSIONS.some((ext) => file.name.toLowerCase().endsWith(ext))) {
    return `不支持的格式"${file.name.split('.').pop()?.toUpperCase() ?? '?'}"，请上传 MP4、MOV、M4V 或 WebM`
  }
  if (file.size > maxUploadMb * 1024 * 1024) {
    const mb = (file.size / 1024 / 1024).toFixed(1)
    return `文件大小 ${mb}MB 超过 ${maxUploadMb}MB 限制`
  }
  return null
}

// ── Sub-components ────────────────────────────────────────────────────────

function UploadZone({
  onFile,
  disabled,
  maxUploadMb,
}: {
  onFile: (file: File) => void
  disabled: boolean
  maxUploadMb: number
}) {
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const handleDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault()
      setDragging(false)
      if (disabled) return
      const file = e.dataTransfer.files[0]
      if (file) onFile(file)
    },
    [onFile, disabled],
  )

  return (
    <div
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-label="点击或拖放视频文件到此处上传"
      aria-disabled={disabled}
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed px-6 py-10 text-center transition-colors cursor-pointer select-none",
        dragging
          ? "border-primary/70 bg-primary/5"
          : "border-border hover:border-primary/40 hover:bg-muted/40",
        disabled && "pointer-events-none opacity-50",
      )}
      onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      onClick={() => !disabled && inputRef.current?.click()}
      onKeyDown={(e) => {
        if (!disabled && (e.key === 'Enter' || e.key === ' ')) {
          e.preventDefault()
          inputRef.current?.click()
        }
      }}
    >
      <UploadCloud className="h-9 w-9 text-primary/70" aria-hidden="true" />
      <div>
        <p className="text-sm font-semibold text-foreground">点击选择或拖放视频</p>
        <p className="text-xs text-muted-foreground mt-1">MP4 · MOV · M4V · WebM · 最大 {maxUploadMb}MB</p>
      </div>
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPTED_EXTENSIONS.join(',')}
        className="sr-only"
        tabIndex={-1}
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (file) onFile(file)
          // reset so same file can be re-selected
          e.target.value = ''
        }}
      />
    </div>
  )
}

function ProgressBar({ pct }: { pct: number }) {
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-border" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
      <div
        className="h-full rounded-full bg-gradient-to-r from-primary to-cyan-400 transition-[width] duration-300"
        style={{ width: `${pct}%` }}
      />
    </div>
  )
}

function ErrorBlock({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-start gap-3 rounded-xl border border-destructive/20 bg-destructive/5 p-4">
        <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" aria-hidden="true" />
        <p className="text-sm text-destructive">{message}</p>
      </div>
      <button
        type="button"
        className={cn(buttonVariants({ variant: "outline", size: "sm" }), "self-start")}
        onClick={onRetry}
      >
        重新上传
      </button>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────

export function AnonymousTrialPanel({ className }: { className?: string }) {
  const [open, setOpen] = useState(false)
  const [state, setState] = useState<PanelState>(INITIAL_STATE)
  // 服务端热配置的大小/时长限制；拉取失败保持出厂默认（200MB / 180s）。
  const [limits, setLimits] = useState<PreviewLimits>(DEFAULT_PREVIEW_LIMITS)
  // 匿名分片通道（plan §9.5）：A6 limits 拉不到/enabled=false → null，
  // 大文件继续走单请求路径（会被 CF 边缘拦，但不破坏现状）。
  const [chunkedLimits, setChunkedLimits] = useState<ChunkedUploadLimits | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pollErrorsRef = useRef(0)
  // Poll generation: bumped on reset/unmount so an in-flight status fetch
  // that resolves afterwards can't setState + re-arm the timer (轮询复活).
  const pollGenRef = useRef(0)
  // 上传速度估算：EMA 平滑 + ≥700ms 节流（进度回调可能很密）。
  const speedRef = useRef<{ t: number; b: number; ema: number } | null>(null)

  function trackSpeed(bytesDone: number): string | null {
    const now = Date.now()
    const prev = speedRef.current
    if (!prev || bytesDone < prev.b) {
      speedRef.current = { t: now, b: bytesDone, ema: 0 }
      return null
    }
    const dtMs = now - prev.t
    if (dtMs < 700) return null
    const inst = ((bytesDone - prev.b) * 1000) / dtMs
    const ema = prev.ema > 0 ? prev.ema * 0.7 + inst * 0.3 : inst
    speedRef.current = { t: now, b: bytesDone, ema }
    return formatSpeed(ema)
  }

  // mount 时拉取当前生效限制（admin 后台改完即时反映到文案与本地校验）
  useEffect(() => {
    let cancelled = false
    getPreviewLimits()
      .then((l) => {
        if (!cancelled) setLimits(l)
      })
      .catch(() => {
        // 拉不到（flag 关 / 网络异常）→ 保持 DEFAULT_PREVIEW_LIMITS 兜底
      })
    // 匿名分片 A6 limits（三与门任一关 → enabled:false → 保持 null）
    getChunkedUploadLimits(ANONYMOUS_CHUNKED_PREFIX)
      .then((l) => {
        if (!cancelled && l && l.enabled) setChunkedLimits(l)
      })
      .catch(() => {
        // 静默：分片不可用时大文件仍走单请求路径
      })
    return () => {
      cancelled = true
    }
  }, [])

  // cleanup on unmount / close
  useEffect(() => {
    return () => {
      abortRef.current?.abort()
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current)
      pollGenRef.current += 1
    }
  }, [])

  function resetPanel() {
    abortRef.current?.abort()
    if (pollTimerRef.current) clearTimeout(pollTimerRef.current)
    pollGenRef.current += 1
    pollErrorsRef.current = 0
    speedRef.current = null
    setState(INITIAL_STATE)
  }

  function handleOpenChange(next: boolean, eventDetails?: { reason?: string }) {
    // 上传/处理中误触弹窗外区域或 Esc 不关闭（2026-06-12 用户反馈：桌面点
    // 空白处弹窗关闭、上传作废；手机点遮罩同源）——关闭会 resetPanel 丢掉
    // 进行中的上传/轮询。右上角 X（close-press）保留为显式退出通道。
    if (
      !next &&
      (state.step === 'uploading' || state.step === 'processing') &&
      (eventDetails?.reason === 'outside-press' || eventDetails?.reason === 'escape-key')
    ) {
      return
    }
    setOpen(next)
    if (!next) resetPanel()
  }

  // ── File selected ──

  async function handleFile(file: File) {
    const validationError = validateFile(file, limits.max_upload_mb)
    if (validationError) {
      setState((s) => ({ ...s, step: 'error', errorMsg: validationError }))
      return
    }

    // 分片选路（plan §9.5）：三与门开 且 文件超阈值（95MB，CF 边缘安全线）
    // → 走分片；否则维持单请求 XHR。分片路径无 abort 语义，用 generation
    // guard 防 reset 后 setState 复活（同 doPoll 纪律）。
    //
    // A6 在选文件时实时重拉（2026-06-12 现网事故）：mount 快照会把 admin
    // 热开关锁死在页面打开那一刻——开关保存前就打开的标签页拿到
    // enabled:false，>95MB 文件回落单请求、死在 CF 边缘报"网络错误"。
    // getChunkedUploadLimits 失败返回 null，此时回落 mount 快照。
    const freshChunked = await getChunkedUploadLimits(ANONYMOUS_CHUNKED_PREFIX)
    const activeChunked: ChunkedUploadLimits | null = freshChunked
      ? (freshChunked.enabled ? freshChunked : null)
      : chunkedLimits
    if (freshChunked) setChunkedLimits(activeChunked)
    const useChunked =
      activeChunked !== null &&
      activeChunked.enabled &&
      file.size > activeChunked.threshold_mb * 1024 * 1024

    // CF 免费版单请求体硬限 100MB：分片通道不可用时大文件必死在边缘
    // （浏览器只能看到连接被切 →"网络错误"，极具误导性）。改为前置明示。
    if (!useChunked && file.size > 100 * 1024 * 1024) {
      setState((s) => ({
        ...s,
        step: 'error',
        errorMsg: '大文件上传通道暂未开放，请将视频压缩到 95MB 以内后重试',
      }))
      return
    }

    let uploadResp: UploadResponse
    if (useChunked) {
      const gen = pollGenRef.current
      speedRef.current = null
      setState((s) => ({
        ...s, step: 'uploading', uploadPct: 0, errorMsg: '', uploadSpeed: '',
        uploadStageLabel: '校验文件…',
      }))
      try {
        const body = await uploadFileInChunksAnonymous(file, activeChunked, (p) => {
          if (gen !== pollGenRef.current) return
          const label =
            p.phase === 'hashing' ? '校验文件…'
            : p.phase === 'merging' ? '合并校验中…'
            : '上传中…'
          let speed: string | null = null
          if (p.phase === 'uploading' && typeof p.bytesDone === 'number') {
            speed = trackSpeed(p.bytesDone)
          } else if (p.phase !== 'uploading') {
            speedRef.current = null
          }
          setState((s) => ({
            ...s,
            uploadPct: p.percent,
            uploadStageLabel: label,
            uploadSpeed: p.phase === 'uploading' ? (speed ?? s.uploadSpeed) : '',
          }))
        })
        if (gen !== pollGenRef.current) return
        uploadResp = body as unknown as UploadResponse
      } catch (err) {
        if (gen !== pollGenRef.current) return
        const raw = err instanceof Error ? err.message : '上传失败，请重试'
        setState((s) => ({ ...s, step: 'error', errorMsg: mapUploadError(raw) || raw }))
        return
      }
    } else {
      const controller = new AbortController()
      abortRef.current = controller

      speedRef.current = null
      setState((s) => ({
        ...s, step: 'uploading', uploadPct: 0, errorMsg: '', uploadSpeed: '',
        uploadStageLabel: '上传中…',
      }))

      try {
        uploadResp = await uploadPreviewVideo(
          file,
          (pct, loadedBytes) => {
            const speed =
              typeof loadedBytes === 'number' ? trackSpeed(loadedBytes) : null
            setState((s) => ({
              ...s, uploadPct: pct, uploadSpeed: speed ?? s.uploadSpeed,
            }))
          },
          controller.signal,
        )
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') return
        const raw = err instanceof Error ? err.message : '上传失败，请重试'
        setState((s) => ({ ...s, step: 'error', errorMsg: mapUploadError(raw) || raw }))
        return
      }
    }

    if (uploadResp.admission_decision !== 'admitted' || uploadResp.status !== 'ready_for_mode') {
      const reason = mapStatusReason(uploadResp.status_reason)
      setState((s) => ({
        ...s,
        step: 'upload_denied',
        deniedReason: reason || '上传被拒绝，请稍后重试',
      }))
      return
    }

    // admitted → show consent
    setState((s) => ({
      ...s,
      step: 'consent',
      previewId: uploadResp.preview_id,
      consentChecked: false,
      expressAutoVoiceClone: false,
    }))
  }

  // ── Consent confirmed → create ──

  async function handleCreate() {
    if (!state.consentChecked || !state.previewId) return
    // Same generation discipline as doPoll: if the panel is reset while
    // createPreview is in flight, schedulePoll would otherwise re-arm with
    // the NEW generation and revive polling for the abandoned preview.
    const gen = pollGenRef.current
    const previewId = state.previewId

    // express lane 才把克隆 opt-in 传给后端（free lane 不显示该勾选、恒 false）。
    const autoVoiceClone =
      limits.active_lane === 'express' &&
      limits.express_clone_available &&
      state.expressAutoVoiceClone
    setState((s) => ({ ...s, step: 'processing', stageLabel: '等待处理…', progress: null }))

    try {
      await createPreview(previewId, new Date().toISOString(), { autoVoiceClone })
    } catch (err) {
      if (gen !== pollGenRef.current) return
      const msg = err instanceof Error ? err.message : '创建预览失败，请重试'
      setState((s) => ({ ...s, step: 'error', errorMsg: msg }))
      return
    }

    if (gen !== pollGenRef.current) return
    schedulePoll(previewId)
  }

  // ── Failed → retry（复用 preview_id，不重新上传） ──

  async function handleRetry() {
    if (!state.previewId) return
    const gen = pollGenRef.current
    const previewId = state.previewId
    const autoVoiceClone =
      limits.active_lane === 'express' &&
      limits.express_clone_available &&
      state.expressAutoVoiceClone
    setState((s) => ({ ...s, step: 'processing', stageLabel: '等待处理…', progress: null, errorMsg: '' }))
    try {
      // create 端仅 failed 终态可重入（服务端原子抢占防并发双重试）；
      // 重试仍走全部闸判定（in-flight / lane 开关 / admission）。
      await createPreview(previewId, new Date().toISOString(), { autoVoiceClone })
    } catch (err) {
      if (gen !== pollGenRef.current) return
      const msg = err instanceof Error ? err.message : '重试失败，请稍后再试'
      setState((s) => ({ ...s, step: 'error', errorMsg: msg }))
      return
    }
    if (gen !== pollGenRef.current) return
    schedulePoll(previewId)
  }

  // ── Polling ──

  function schedulePoll(previewId: string, delayMs: number = POLL_INTERVAL_MS) {
    if (pollTimerRef.current) clearTimeout(pollTimerRef.current)
    const gen = pollGenRef.current
    pollTimerRef.current = setTimeout(() => void doPoll(previewId, gen), delayMs)
  }

  async function doPoll(previewId: string, gen: number) {
    if (gen !== pollGenRef.current) return
    // Tab hidden → skip the request, re-check next interval.
    if (typeof document !== 'undefined' && document.hidden) {
      schedulePoll(previewId)
      return
    }
    let statusResp: Awaited<ReturnType<typeof getPreviewStatus>>
    try {
      statusResp = await getPreviewStatus(previewId)
      if (gen !== pollGenRef.current) return
      pollErrorsRef.current = 0
    } catch (err) {
      if (gen !== pollGenRef.current) return
      // 401 = session expired → stop and tell the user; never silently spin.
      const httpStatus = err instanceof PreviewStatusError ? err.status : 0
      if (httpStatus === 401) {
        setState((s) => ({ ...s, step: 'error', errorMsg: '会话已过期，请刷新页面后重试' }))
        return
      }
      // 429 / 5xx / network — bounded retries with backoff, then surface.
      pollErrorsRef.current += 1
      if (pollErrorsRef.current >= MAX_POLL_ERRORS) {
        setState((s) => ({ ...s, step: 'error', errorMsg: '网络不稳定，预览状态查询多次失败，请稍后重试' }))
        return
      }
      const backoff = Math.min(
        POLL_INTERVAL_MS * 2 ** (pollErrorsRef.current - 1),
        MAX_POLL_BACKOFF_MS,
      )
      schedulePoll(previewId, backoff)
      return
    }

    const ps: PreviewStatus = statusResp.preview_status

    if (ps === 'ready') {
      // 匿名预览→登录认领 hint（plan 2026-06-15 §7）：预览就绪即记下「本会话有
      // 可认领预览」（localStorage，跨 /auth 导航与弹窗关闭存活）。登录成功后
      // post-auth-redirect 据此触发 /claim。非敏感——真凭证是 HttpOnly avt_anon。
      setAnonClaimHint(previewId)
      setState((s) => ({ ...s, step: 'ready', stageLabel: '预览就绪', progress: 100 }))
      return
    }

    if (ps === 'failed') {
      // §E 诚实失败：record 与已上传文件保留（沿用 TTL），展示「重试」
      // 按钮——复用同一 preview_id 重新 create，无需重新上传。
      setState((s) => ({ ...s, step: 'failed', errorMsg: '预览生成失败' }))
      return
    }

    // pending / processing / unknown → keep polling
    setState((s) => ({
      ...s,
      step: 'processing',
      stageLabel: mapStageLabel(statusResp.stage),
      progress: statusResp.progress,
    }))
    schedulePoll(previewId)
  }

  // ── Render helpers ──

  function renderBody() {
    const { step } = state

    // lane 三态（plan 2026-06-12 §G）：两 lane 都关 → 「暂未开放」。
    // 生命周期端点对 lane 开关零感知——进行中/已就绪的预览不受影响，
    // 所以只在 idle（新 intake 入口）拦。
    if (!limits.master_open && step === 'idle') {
      return (
        <div className="flex flex-col items-center gap-3 rounded-xl border border-border bg-muted/30 px-6 py-10 text-center">
          <AlertCircle className="h-8 w-8 text-muted-foreground" aria-hidden="true" />
          <p className="text-sm font-medium text-foreground">免注册试用暂未开放</p>
          <p className="text-xs text-muted-foreground">
            可以先
            <a href="/auth" className="mx-1 text-primary hover:underline font-medium">
              注册账号
            </a>
            体验完整的视频翻译配音
          </p>
        </div>
      )
    }

    if (step === 'idle') {
      return <UploadZone onFile={(f) => void handleFile(f)} disabled={false} maxUploadMb={limits.max_upload_mb} />
    }

    if (step === 'uploading') {
      return (
        <div className="flex flex-col gap-4">
          <UploadZone onFile={() => undefined} disabled maxUploadMb={limits.max_upload_mb} />
          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span className="flex items-center gap-1.5">
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                {state.uploadStageLabel}
              </span>
              <span>
                {state.uploadSpeed ? `${state.uploadSpeed} · ` : ''}
                {state.uploadPct}%
              </span>
            </div>
            <ProgressBar pct={state.uploadPct} />
          </div>
        </div>
      )
    }

    if (step === 'upload_denied') {
      return (
        <div className="flex flex-col gap-3">
          <div className="flex items-start gap-3 rounded-xl border border-amber-500/20 bg-amber-500/5 p-4">
            <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-amber-500" aria-hidden="true" />
            <p className="text-sm text-amber-600 dark:text-amber-400">{state.deniedReason}</p>
          </div>
          <button
            type="button"
            className={cn(buttonVariants({ variant: "outline", size: "sm" }), "self-start")}
            onClick={resetPanel}
          >
            重新上传
          </button>
        </div>
      )
    }

    if (step === 'consent') {
      return (
        <div className="flex flex-col gap-4">
          <div className="rounded-xl border border-border bg-muted/30 p-4">
            <div className="flex items-center gap-2 text-sm text-muted-foreground mb-2">
              <CheckCircle2 className="h-4 w-4 text-emerald-500 shrink-0" aria-hidden="true" />
              <span className="font-medium text-foreground">视频上传成功</span>
            </div>
            <p className="text-xs text-muted-foreground">
              {limits.active_lane === 'express'
                ? `我们将用快捷版完整管线（含说话人音色匹配）为您生成带水印的前 ${formatPreviewDuration(limits.preview_seconds)}中文配音预览。`
                : `我们将为您生成带水印的前 ${formatPreviewDuration(limits.preview_seconds)}中文配音预览。`}
              继续前请确认以下声明：
            </p>
          </div>

          <label className="flex items-start gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
            <input
              type="checkbox"
              checked={state.consentChecked}
              onChange={(e) =>
                setState((s) => ({ ...s, consentChecked: e.target.checked }))
              }
              className="mt-0.5 h-4 w-4 shrink-0 rounded border-border"
            />
            <span className="text-sm text-foreground leading-relaxed">
              我确认对该视频内容及其中人声拥有必要权利，同意将其用于本次预览合成。
            </span>
          </label>

          {/* plan 2026-06-14 §3.1：express lane 才显示"克隆我的音色"opt-in（可选，
              不 gate 按钮）。CosyVoice 国内免费克隆；未勾选走预设。绝不 MiniMax。
              是否真克隆还取决于服务端 admin 主开关 + 全局 cap + worker。 */}
          {limits.active_lane === 'express' && limits.express_clone_available && (
            <label className="flex items-start gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
              <input
                type="checkbox"
                checked={state.expressAutoVoiceClone}
                onChange={(e) =>
                  setState((s) => ({ ...s, expressAutoVoiceClone: e.target.checked }))
                }
                className="mt-0.5 h-4 w-4 shrink-0 rounded border-border"
              />
              <span className="text-sm text-foreground leading-relaxed">
                <span className="font-medium">（可选）克隆我的原声音色</span>
                <span className="mt-1 block text-xs text-muted-foreground">
                  勾选后将用视频中主说话人的声音生成更接近原声的配音（免费，仅用于本次预览）。不勾选则使用预设音色。
                </span>
              </span>
            </label>
          )}

          <button
            type="button"
            disabled={!state.consentChecked}
            className={cn(
              buttonVariants({ variant: "default", size: "lg" }),
              "h-11 w-full",
              !state.consentChecked && "opacity-50 cursor-not-allowed",
            )}
            onClick={() => void handleCreate()}
          >
            生成配音预览
          </button>
        </div>
      )
    }

    if (step === 'processing') {
      const pct = state.progress ?? 0
      return (
        <div className="flex flex-col items-center gap-5 py-4">
          <div className="relative flex h-16 w-16 items-center justify-center">
            <div className="absolute inset-0 animate-spin rounded-full border-2 border-border border-t-primary" />
            <Film className="h-7 w-7 text-primary/70" aria-hidden="true" />
          </div>
          <div className="w-full space-y-2">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>{state.stageLabel}</span>
              {state.progress !== null && <span>{pct}%</span>}
            </div>
            {state.progress !== null && <ProgressBar pct={pct} />}
          </div>
          <p className="text-xs text-muted-foreground text-center">
            配音预览生成中，通常需要 2–5 分钟，请稍候
          </p>
        </div>
      )
    }

    if (step === 'ready') {
      const streamUrl = getPreviewStreamUrl(state.previewId)
      return (
        <div className="flex flex-col gap-4">
          <div className="flex items-center gap-2 text-sm">
            <CheckCircle2 className="h-4 w-4 text-emerald-500 shrink-0" aria-hidden="true" />
            <span className="font-medium text-foreground">配音预览已就绪</span>
            <span className="text-xs text-muted-foreground">（带水印，前 {formatPreviewDuration(limits.preview_seconds)}）</span>
          </div>
          <div className="overflow-hidden rounded-xl border border-border bg-black">
            {/* No download attribute, no download button, controlsList nodownload */}
            <video
              key={streamUrl}
              src={streamUrl}
              controls
              playsInline
              controlsList="nodownload noremoteplayback"
              disablePictureInPicture
              onContextMenu={(e) => e.preventDefault()}
              className="w-full"
            >
              您的浏览器不支持 video 标签。
            </video>
          </div>
          <p className="text-xs text-muted-foreground">
            预览效果满意？
            <a
              href={`/auth?from=${encodeURIComponent('/translations/new')}`}
              className="ml-1 text-primary hover:underline font-medium"
            >
              注册账号体验完整翻译
            </a>
            ，登录后还可使用
            <a
              href={`/auth?from=${encodeURIComponent('/translations/new')}`}
              className="mx-1 text-primary hover:underline font-medium"
            >
              智能版
            </a>
            获得更高质量的全自动翻译
          </p>
          <button
            type="button"
            className={cn(buttonVariants({ variant: "outline", size: "sm" }), "self-start")}
            onClick={resetPanel}
          >
            上传另一个视频
          </button>
        </div>
      )
    }

    if (step === 'failed') {
      // §E 诚实失败态：「重试」复用 preview_id 重新 create（不重新上传，
      // per-scope 配额已由服务端退还）；「重新上传」回 idle。
      return (
        <div className="flex flex-col gap-3">
          <div className="flex items-start gap-3 rounded-xl border border-destructive/20 bg-destructive/5 p-4">
            <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" aria-hidden="true" />
            <div>
              <p className="text-sm text-destructive font-medium">{state.errorMsg || '预览生成失败'}</p>
              <p className="text-xs text-muted-foreground mt-1">
                已上传的视频仍然保留，点击「重试」无需重新上传。
              </p>
            </div>
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              className={cn(buttonVariants({ variant: "default", size: "sm" }))}
              onClick={() => void handleRetry()}
            >
              重试
            </button>
            <button
              type="button"
              className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
              onClick={resetPanel}
            >
              重新上传
            </button>
          </div>
        </div>
      )
    }

    if (step === 'error') {
      return <ErrorBlock message={state.errorMsg} onRetry={resetPanel} />
    }

    return null
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger
        className={cn(
          buttonVariants({ variant: "default", size: "lg" }),
          "h-11 px-6 text-base",
          className,
        )}
      >
        立即试用
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="text-lg">免注册上传试用</DialogTitle>
        </DialogHeader>

        <div className="mt-1">
          {renderBody()}
        </div>

        <p className="text-center text-xs text-muted-foreground">
          本地视频 · 前 {formatPreviewDuration(limits.preview_seconds)}预览 · 带水印
          {limits.active_lane === 'express' ? ' · 快捷版真实管线' : ''}
        </p>
      </DialogContent>
    </Dialog>
  )
}
