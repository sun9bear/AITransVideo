"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslations } from "next-intl"
import { Link } from "@/i18n/navigation"
import { UploadCloud, CheckCircle2, AlertCircle, Loader2, Film, Minus, X } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { buttonVariants } from "@/components/ui/button"
import { useConfirmDialog } from "@/components/ui/confirm-dialog"
import { cn } from "@/lib/utils"
import {
  uploadPreviewVideo,
  createPreview,
  getPreviewStatus,
  getPreviewStreamUrl,
  getPreviewLimits,
  previewDurationParts,
  mapStatusReason,
  mapUploadError,
  mapStageLabel,
  DEFAULT_PREVIEW_LIMITS,
  PreviewStatusError,
  type PreviewLimits,
  type UploadResponse,
  type PreviewStatus,
} from "@/lib/api/anonymousPreview"

// next-intl t function type alias for passing the translator into module-level
// helpers (the namespace is "marketing.anonymousTrial").
type Translator = ReturnType<typeof useTranslations>

/** 把预览秒数渲染成本地化时长（"3 分钟" / "90 秒" 或 en "3 min" / "90 sec"）。
 *  调用方传入已绑定 "marketing.anonymousTrial" 的 t。 */
function previewDuration(t: Translator, seconds: number): string {
  const parts = previewDurationParts(seconds)
  return t("previewDuration", { value: parts.value, unit: parts.unit })
}
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
  // 恒在 step:'uploading' 进入前被显式覆盖（uploadStage.*）；置空串移除模块级 CJK，行为安全。
  uploadStageLabel: '',
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

/** 本地文件校验结果（语言中立）：null = 通过；否则带 code + ICU 参数，由 handleFile 经 t 渲染。 */
type ValidationFailure =
  | { code: 'unsupportedFormat'; ext: string }
  | { code: 'tooLarge'; mb: string; maxUploadMb: number }

function validateFile(file: File, maxUploadMb: number): ValidationFailure | null {
  if (!ACCEPTED_TYPES.includes(file.type) && !ACCEPTED_EXTENSIONS.some((ext) => file.name.toLowerCase().endsWith(ext))) {
    return { code: 'unsupportedFormat', ext: file.name.split('.').pop()?.toUpperCase() ?? '?' }
  }
  if (file.size > maxUploadMb * 1024 * 1024) {
    return { code: 'tooLarge', mb: (file.size / 1024 / 1024).toFixed(1), maxUploadMb }
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
  const t = useTranslations("marketing.anonymousTrial")
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
      aria-label={t("uploadZone.aria")}
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
        <p className="text-sm font-semibold text-foreground">{t("uploadZone.title")}</p>
        <p className="text-xs text-muted-foreground mt-1">{t("uploadZone.hint", { maxUploadMb })}</p>
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
  const t = useTranslations("marketing.anonymousTrial")
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
        {t("retryUpload")}
      </button>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────

export function AnonymousTrialPanel({ className }: { className?: string }) {
  const t = useTranslations("marketing.anonymousTrial")
  const [open, setOpen] = useState(false)
  const [minimized, setMinimized] = useState(false)
  const [state, setState] = useState<PanelState>(INITIAL_STATE)
  const { confirm, confirmDialog } = useConfirmDialog()

  // ── lib code → 文案桥（UI-03g）：lib mapper/throw 返回稳定 code/token，下面用
  //    t.has 守卫后映射成本地化文案，未命中回落到对应 fallback。
  //    next-intl v4 的 t/t.has 接受字面量 message key；运行时拼接的 key 是受控集合
  //    （字典已声明全部 code/token + fallback），故用 dynKey 旁路字面量类型约束。 ──
  type MsgKey = Parameters<typeof t>[0]
  const dynKey = (k: string): MsgKey => k as MsgKey
  const hasKey = (k: string): boolean => t.has(dynKey(k))

  /** 处理阶段 code（mapStageLabel 已规范成 KNOWN_STAGES ∪ {'fallback'}）→ 文案。 */
  function resolveStage(code: string): string {
    const key = `stage.${code}`
    return hasKey(key) ? t(dynKey(key)) : t("stage.fallback")
  }
  /** upload status_reason code → 文案；未知 code 回落 statusReason.fallback（带 reason）。 */
  function resolveStatusReason(reason: string | null): string {
    const code = mapStatusReason(reason)
    if (!code) return ''
    const key = `statusReason.${code}`
    return hasKey(key) ? t(dynKey(key)) : t("statusReason.fallback", { reason: code })
  }
  /** upload-time error code/原始字符串 → 文案；未知则原样回显（保留旧 `|| raw` 语义）。 */
  function resolveUploadError(raw: string): string {
    const code = mapUploadError(raw)
    if (!code) return t("uploadFailedFallback")
    const key = `uploadError.${code}`
    if (hasKey(key)) return t(dynKey(key))
    // lib 抛出的传输失败 token（network_error / upload_http / invalid_response）登记在
    // errors.* 组——必须在原样回显前查它。否则裸 token 漏给用户，且 zh 从
    // 「网络错误，请检查连接后重试」退化成「network_error」，破红线 1（@codex 审查 #1）。
    const errKey = `errors.${code}`
    if (hasKey(errKey)) return t(dynKey(errKey))
    // 后端真实 error 字符串未命中字典 → 原样回显（与旧 `mapUploadError(raw) || raw` 一致）。
    return code
  }
  /** createPreview 抛出的 token（createError.<token>）→ 文案。 */
  function resolveCreateError(token: string): string {
    const key = `createError.${token}`
    return hasKey(key) ? t(dynKey(key)) : t("createError.generic")
  }
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
    setMinimized(false)
    setState(INITIAL_STATE)
  }

  const panelBusy = state.step === 'uploading' || state.step === 'processing'
  const canMinimize = panelBusy

  function handleOpenChange(next: boolean) {
    if (next) {
      setMinimized(false)
      setOpen(true)
      return
    }
    // Ignore backdrop clicks and Escape. The explicit X button handles
    // confirmation before clearing upload/polling state.
  }

  async function requestClose() {
    if (panelBusy) {
      const confirmed = await confirm({
        title: t("confirmClose.title"),
        description: t("confirmClose.description"),
        confirmLabel: t("confirmClose.confirmLabel"),
        cancelLabel: t("confirmClose.cancelLabel"),
        destructive: true,
      })
      if (!confirmed) return
    }
    setOpen(false)
    setMinimized(false)
    resetPanel()
  }

  function minimizePanel() {
    if (!canMinimize) return
    setOpen(false)
    setMinimized(true)
  }

  function restorePanel() {
    setMinimized(false)
    setOpen(true)
  }

  // ── File selected ──

  async function handleFile(file: File) {
    const validationError = validateFile(file, limits.max_upload_mb)
    if (validationError) {
      const errorMsg =
        validationError.code === 'unsupportedFormat'
          ? t("validate.unsupportedFormat", { ext: validationError.ext })
          : t("validate.tooLarge", { mb: validationError.mb, maxUploadMb: validationError.maxUploadMb })
      setState((s) => ({ ...s, step: 'error', errorMsg }))
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
        errorMsg: t("largeFileUnavailable"),
      }))
      return
    }

    let uploadResp: UploadResponse
    if (useChunked) {
      const gen = pollGenRef.current
      speedRef.current = null
      setState((s) => ({
        ...s, step: 'uploading', uploadPct: 0, errorMsg: '', uploadSpeed: '',
        uploadStageLabel: t("uploadStage.hashing"),
      }))
      try {
        const body = await uploadFileInChunksAnonymous(file, activeChunked, (p) => {
          if (gen !== pollGenRef.current) return
          const label =
            p.phase === 'hashing' ? t("uploadStage.hashing")
            : p.phase === 'merging' ? t("uploadStage.merging")
            : t("uploadStage.uploading")
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
        const raw = err instanceof Error ? err.message : ''
        setState((s) => ({ ...s, step: 'error', errorMsg: resolveUploadError(raw) }))
        return
      }
    } else {
      const controller = new AbortController()
      abortRef.current = controller

      speedRef.current = null
      setState((s) => ({
        ...s, step: 'uploading', uploadPct: 0, errorMsg: '', uploadSpeed: '',
        uploadStageLabel: t("uploadStage.uploading"),
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
        const raw = err instanceof Error ? err.message : ''
        setState((s) => ({ ...s, step: 'error', errorMsg: resolveUploadError(raw) }))
        return
      }
    }

    if (uploadResp.admission_decision !== 'admitted' || uploadResp.status !== 'ready_for_mode') {
      const reason = resolveStatusReason(uploadResp.status_reason)
      setState((s) => ({
        ...s,
        step: 'upload_denied',
        deniedReason: reason || t("uploadDeniedFallback"),
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
    setState((s) => ({ ...s, step: 'processing', stageLabel: t("stage.queued"), progress: null }))

    try {
      await createPreview(previewId, new Date().toISOString(), { autoVoiceClone })
    } catch (err) {
      if (gen !== pollGenRef.current) return
      const token = err instanceof Error ? err.message : 'generic'
      setState((s) => ({ ...s, step: 'error', errorMsg: resolveCreateError(token) }))
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
    setState((s) => ({ ...s, step: 'processing', stageLabel: t("stage.queued"), progress: null, errorMsg: '' }))
    try {
      // create 端仅 failed 终态可重入（服务端原子抢占防并发双重试）；
      // 重试仍走全部闸判定（in-flight / lane 开关 / admission）。
      await createPreview(previewId, new Date().toISOString(), { autoVoiceClone })
    } catch (err) {
      if (gen !== pollGenRef.current) return
      // createPreview 抛 createError token；retry 路径未命中时回落「重试失败」。
      const token = err instanceof Error ? err.message : ''
      const key = `createError.${token}`
      const msg = hasKey(key) ? t(dynKey(key)) : t("errors.retryFailed")
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
        setState((s) => ({ ...s, step: 'error', errorMsg: t("errors.sessionExpired") }))
        return
      }
      // 429 / 5xx / network — bounded retries with backoff, then surface.
      pollErrorsRef.current += 1
      if (pollErrorsRef.current >= MAX_POLL_ERRORS) {
        setState((s) => ({ ...s, step: 'error', errorMsg: t("errors.pollFailed") }))
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
      setState((s) => ({ ...s, step: 'ready', stageLabel: t("ready.stageLabel"), progress: 100 }))
      return
    }

    if (ps === 'failed') {
      // §E 诚实失败：record 与已上传文件保留（沿用 TTL），展示「重试」
      // 按钮——复用同一 preview_id 重新 create，无需重新上传。
      setState((s) => ({ ...s, step: 'failed', errorMsg: t("failed.fallback") }))
      return
    }

    // pending / processing / unknown → keep polling
    setState((s) => ({
      ...s,
      step: 'processing',
      stageLabel: resolveStage(mapStageLabel(statusResp.stage)),
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
          <p className="text-sm font-medium text-foreground">{t("masterClosed.title")}</p>
          <p className="text-xs text-muted-foreground">
            {t("masterClosed.before")}
            <Link href="/auth" className="mx-1 text-primary hover:underline font-medium">
              {t("masterClosed.registerLink")}
            </Link>
            {t("masterClosed.after")}
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
            {t("retryUpload")}
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
              <span className="font-medium text-foreground">{t("consent.uploadSuccess")}</span>
            </div>
            <p className="text-xs text-muted-foreground">
              {limits.active_lane === 'express'
                ? t("consent.introExpress", { duration: previewDuration(t, limits.preview_seconds) })
                : t("consent.introDefault", { duration: previewDuration(t, limits.preview_seconds) })}
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
              {t("consent.rightsLabel")}
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
                <span className="font-medium">{t("consent.cloneOptInTitle")}</span>
                <span className="mt-1 block text-xs text-muted-foreground">
                  {t("consent.cloneOptInDetail")}
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
            {t("consent.submit")}
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
            {t("processing.hint")}
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
            <span className="font-medium text-foreground">{t("ready.title")}</span>
            <span className="text-xs text-muted-foreground">{t("ready.watermarkNote", { duration: previewDuration(t, limits.preview_seconds) })}</span>
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
              {t("ready.videoFallback")}
            </video>
          </div>
          <p className="text-xs text-muted-foreground">
            {t("ready.ctaLead")}
            <a
              href={`/auth?from=${encodeURIComponent('/translations/new')}`}
              className="ml-1 text-primary hover:underline font-medium"
            >
              {t("ready.ctaRegisterLink")}
            </a>
            {t("ready.ctaMiddle")}
            <a
              href={`/auth?from=${encodeURIComponent('/translations/new')}`}
              className="mx-1 text-primary hover:underline font-medium"
            >
              {t("ready.ctaStudioLink")}
            </a>
            {t("ready.ctaTail")}
          </p>
          <button
            type="button"
            className={cn(buttonVariants({ variant: "outline", size: "sm" }), "self-start")}
            onClick={resetPanel}
          >
            {t("ready.uploadAnother")}
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
              <p className="text-sm text-destructive font-medium">{state.errorMsg || t("failed.fallback")}</p>
              <p className="text-xs text-muted-foreground mt-1">
                {t("failed.keepNote")}
              </p>
            </div>
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              className={cn(buttonVariants({ variant: "default", size: "sm" }))}
              onClick={() => void handleRetry()}
            >
              {t("failed.retry")}
            </button>
            <button
              type="button"
              className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
              onClick={resetPanel}
            >
              {t("failed.reupload")}
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

  function renderMinimizedWidget() {
    if (!minimized) return null

    const isUpload = state.step === 'uploading'
    const isProcessing = state.step === 'processing'
    const isConsent = state.step === 'consent'
    const isReady = state.step === 'ready'
    const pct = isUpload ? state.uploadPct : (state.progress ?? (isConsent || isReady ? 100 : null))
    const title = isUpload
      ? t("minimized.titleUpload")
      : isProcessing
        ? t("minimized.titleProcessing")
        : isConsent
          ? t("minimized.titleConsent")
          : isReady
            ? t("minimized.titleReady")
            : state.step === 'failed'
              ? t("minimized.titleFailed")
              : t("minimized.titleDefault")
    const detail = isUpload
      ? `${state.uploadSpeed ? `${state.uploadSpeed} · ` : ''}${state.uploadPct}%`
      : isProcessing
        ? (state.stageLabel || t("minimized.detailProcessingFallback"))
        : isConsent
          ? t("minimized.detailConsent")
          : isReady
            ? t("minimized.detailReady")
            : t("minimized.detailDefault")

    const Icon = isConsent || isReady ? CheckCircle2 : state.step === 'failed' || state.step === 'error' ? AlertCircle : Loader2

    return (
      <button
        type="button"
        className="fixed bottom-5 right-5 z-[70] w-[min(20rem,calc(100vw-2rem))] rounded-lg border border-border bg-background p-3 text-left text-sm shadow-xl ring-1 ring-foreground/10 transition hover:border-primary/40 hover:shadow-2xl"
        onClick={restorePanel}
        aria-label={t("minimized.expandAria")}
      >
        <div className="flex items-start gap-3">
          <Icon
            className={cn(
              "mt-0.5 h-5 w-5 shrink-0",
              isConsent || isReady ? "text-emerald-500" : state.step === 'failed' || state.step === 'error' ? "text-destructive" : "animate-spin text-primary",
            )}
            aria-hidden="true"
          />
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-3">
              <span className="font-medium text-foreground">{title}</span>
              <span className="text-xs text-primary">{t("minimized.expandLabel")}</span>
            </div>
            <p className="mt-0.5 truncate text-xs text-muted-foreground">{detail}</p>
            {pct !== null && (
              <div className="mt-2">
                <ProgressBar pct={Math.max(0, Math.min(100, pct))} />
              </div>
            )}
          </div>
        </div>
      </button>
    )
  }

  return (
    <>
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogTrigger
          className={cn(
            buttonVariants({ variant: "default", size: "lg" }),
            "h-11 px-6 text-base",
            className,
          )}
        >
          {t("dialogTrigger")}
        </DialogTrigger>
        <DialogContent className="sm:max-w-lg" showCloseButton={false}>
          <DialogHeader>
            <div className="flex items-center justify-between gap-3">
              <DialogTitle className="text-lg">{t("dialogTitle")}</DialogTitle>
              <div className="flex items-center gap-1">
                {canMinimize && (
                  <button
                    type="button"
                    className={cn(buttonVariants({ variant: "ghost", size: "icon-sm" }))}
                    onClick={minimizePanel}
                    aria-label={t("minimizeAria")}
                    title={t("minimizeTitle")}
                  >
                    <Minus className="h-4 w-4" aria-hidden="true" />
                  </button>
                )}
                <button
                  type="button"
                  className={cn(buttonVariants({ variant: "ghost", size: "icon-sm" }))}
                  onClick={() => void requestClose()}
                  aria-label={t("closeAria")}
                  title={t("closeTitle")}
                >
                  <X className="h-4 w-4" aria-hidden="true" />
                </button>
              </div>
            </div>
          </DialogHeader>

          <div className="mt-1">
            {renderBody()}
          </div>

          <p className="text-center text-xs text-muted-foreground">
            {t("footer", {
              duration: previewDuration(t, limits.preview_seconds),
              expressSuffix: limits.active_lane === 'express' ? t("footerExpressSuffix") : '',
            })}
          </p>
        </DialogContent>
      </Dialog>
      {renderMinimizedWidget()}
      {confirmDialog}
    </>
  )
}
