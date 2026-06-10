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
  mapStatusReason,
  mapStageLabel,
  PreviewStatusError,
  type UploadResponse,
  type PreviewStatus,
} from "@/lib/api/anonymousPreview"

// ── Constants ─────────────────────────────────────────────────────────────

const MAX_FILE_BYTES = 200 * 1024 * 1024 // 200 MB
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
  | 'error'

interface PanelState {
  step: PanelStep
  uploadPct: number
  deniedReason: string
  errorMsg: string
  previewId: string
  stageLabel: string
  progress: number | null
  consentChecked: boolean
}

const INITIAL_STATE: PanelState = {
  step: 'idle',
  uploadPct: 0,
  deniedReason: '',
  errorMsg: '',
  previewId: '',
  stageLabel: '',
  progress: null,
  consentChecked: false,
}

// ── Helpers ───────────────────────────────────────────────────────────────

function validateFile(file: File): string | null {
  if (!ACCEPTED_TYPES.includes(file.type) && !ACCEPTED_EXTENSIONS.some((ext) => file.name.toLowerCase().endsWith(ext))) {
    return `不支持的格式"${file.name.split('.').pop()?.toUpperCase() ?? '?'}"，请上传 MP4、MOV、M4V 或 WebM`
  }
  if (file.size > MAX_FILE_BYTES) {
    const mb = (file.size / 1024 / 1024).toFixed(1)
    return `文件大小 ${mb}MB 超过 200MB 限制`
  }
  return null
}

// ── Sub-components ────────────────────────────────────────────────────────

function UploadZone({
  onFile,
  disabled,
}: {
  onFile: (file: File) => void
  disabled: boolean
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
        <p className="text-xs text-muted-foreground mt-1">MP4 · MOV · M4V · WebM · 最大 200MB</p>
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
  const abortRef = useRef<AbortController | null>(null)
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pollErrorsRef = useRef(0)

  // cleanup on unmount / close
  useEffect(() => {
    return () => {
      abortRef.current?.abort()
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current)
    }
  }, [])

  function resetPanel() {
    abortRef.current?.abort()
    if (pollTimerRef.current) clearTimeout(pollTimerRef.current)
    pollErrorsRef.current = 0
    setState(INITIAL_STATE)
  }

  function handleOpenChange(next: boolean) {
    setOpen(next)
    if (!next) resetPanel()
  }

  // ── File selected ──

  async function handleFile(file: File) {
    const validationError = validateFile(file)
    if (validationError) {
      setState((s) => ({ ...s, step: 'error', errorMsg: validationError }))
      return
    }

    const controller = new AbortController()
    abortRef.current = controller

    setState((s) => ({ ...s, step: 'uploading', uploadPct: 0, errorMsg: '' }))

    let uploadResp: UploadResponse
    try {
      uploadResp = await uploadPreviewVideo(
        file,
        (pct) => setState((s) => ({ ...s, uploadPct: pct })),
        controller.signal,
      )
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      const msg = err instanceof Error ? err.message : '上传失败，请重试'
      setState((s) => ({ ...s, step: 'error', errorMsg: msg }))
      return
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
    }))
  }

  // ── Consent confirmed → create ──

  async function handleCreate() {
    if (!state.consentChecked || !state.previewId) return

    setState((s) => ({ ...s, step: 'processing', stageLabel: '等待处理…', progress: null }))

    try {
      await createPreview(state.previewId, new Date().toISOString())
    } catch (err) {
      const msg = err instanceof Error ? err.message : '创建预览失败，请重试'
      setState((s) => ({ ...s, step: 'error', errorMsg: msg }))
      return
    }

    schedulePoll(state.previewId)
  }

  // ── Polling ──

  function schedulePoll(previewId: string, delayMs: number = POLL_INTERVAL_MS) {
    if (pollTimerRef.current) clearTimeout(pollTimerRef.current)
    pollTimerRef.current = setTimeout(() => void doPoll(previewId), delayMs)
  }

  async function doPoll(previewId: string) {
    let statusResp: Awaited<ReturnType<typeof getPreviewStatus>>
    try {
      statusResp = await getPreviewStatus(previewId)
      pollErrorsRef.current = 0
    } catch (err) {
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
      setState((s) => ({ ...s, step: 'ready', stageLabel: '预览就绪', progress: 100 }))
      return
    }

    if (ps === 'failed') {
      setState((s) => ({ ...s, step: 'error', errorMsg: '预览生成失败，请重新上传一个视频试试' }))
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

    if (step === 'idle') {
      return <UploadZone onFile={(f) => void handleFile(f)} disabled={false} />
    }

    if (step === 'uploading') {
      return (
        <div className="flex flex-col gap-4">
          <UploadZone onFile={() => undefined} disabled />
          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span className="flex items-center gap-1.5">
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                上传中…
              </span>
              <span>{state.uploadPct}%</span>
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
              我们将为您生成带水印的前 3 分钟中文配音预览。
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
            <span className="text-xs text-muted-foreground">（带水印，前 3 分钟）</span>
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
            <a href="/auth" className="ml-1 text-primary hover:underline font-medium">
              注册账号体验完整翻译
            </a>
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
          本地视频 · 前 3 分钟预览 · 带水印
        </p>
      </DialogContent>
    </Dialog>
  )
}
