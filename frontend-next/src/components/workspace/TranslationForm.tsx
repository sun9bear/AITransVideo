"use client"

import { useEffect, useState, type FormEvent } from "react"
import { toast } from "sonner"

import { StatusBadge } from "@/components/status-badge"
import { getJobDisplayTitle, getStageLabel } from "@/features/jobs/presentation"
import { ApiError } from "@/lib/api/client"
import { getErrorMessage } from "@/lib/api/errors"
import { getEntitlements, type UserEntitlements } from "@/lib/api/entitlements"
import { listJobs, submitTranslationJob } from "@/lib/api/jobs"
import { getVoiceLibrary, type VoiceLibraryEntry } from "@/lib/api/voiceLibrary"
import { usePollingTask } from "@/lib/react/usePollingTask"
import { ACTIVE_JOB_STATUSES, type JobSummary } from "@/types/jobs"

export interface TranslationFormProps {
  /** Called when job is created successfully. Container decides what happens next. */
  onCreated: (job: { id: string; title: string }) => void
  /** 'page' mode = container handles redirect; 'dialog' mode = container handles close */
  mode: "page" | "dialog"
  /** Pre-fill YouTube URL (e.g., from "recreate" on a failed job) */
  initialSourceUrl?: string
}

export function TranslationForm({ onCreated, mode, initialSourceUrl }: TranslationFormProps) {
  const [sourceType, setSourceType] = useState<"youtube_url" | "local_video">("youtube_url")
  const [youtubeUrl, setYoutubeUrl] = useState(initialSourceUrl ?? "")
  const [uploadedFilePath, setUploadedFilePath] = useState("")
  const [uploadFileName, setUploadFileName] = useState("")
  const [isUploading, setIsUploading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState("")
  const [speakers, setSpeakers] = useState<string>("auto")
  const [transcriptionMethod, setTranscriptionMethod] = useState<"assemblyai" | "gemini">("assemblyai")
  const [serviceMode, setServiceMode] = useState<"express" | "studio">("express")
  const [entitlements, setEntitlements] = useState<UserEntitlements | null>(null)
  const [savedVoices, setSavedVoices] = useState<VoiceLibraryEntry[]>([])
  const [activeJobs, setActiveJobs] = useState<JobSummary[]>([])
  const [isLoadingGuard, setIsLoadingGuard] = useState(true)
  const [submitState, setSubmitState] = useState<"error" | "idle" | "submitting" | "success">("idle")

  const validationError =
    sourceType === "youtube_url"
      ? validateYoutubeUrl(youtubeUrl)
      : !uploadedFilePath
        ? "请先上传视频文件。"
        : null

  const maxConcurrentJobs = entitlements?.limits.max_concurrent_jobs ?? 1
  const activeJobCount = activeJobs.length
  const isBlockedByConcurrency = activeJobCount >= maxConcurrentJobs
  // For UI display: show the most recent active job if blocked
  const latestActiveJob = activeJobs.length > 0 ? activeJobs[0] : null

  const loadActiveJobs = async (silent = false) => {
    if (!silent) setIsLoadingGuard(true)
    try {
      const allJobs = await listJobs()
      const active = allJobs.filter((j) => ACTIVE_JOB_STATUSES.includes(j.status))
      setActiveJobs(active)
    } catch {
      // ignore
    } finally {
      setIsLoadingGuard(false)
    }
  }

  usePollingTask(() => loadActiveJobs(!isLoadingGuard), { intervalMs: 5000 })

  useEffect(() => {
    getVoiceLibrary()
      .then((lib) => setSavedVoices(lib.voices))
      .catch(() => {})
    getEntitlements()
      .then((ent) => setEntitlements(ent))
      .catch(() => {})
  }, [])

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (validationError) {
      toast.error(validationError)
      return
    }
    setSubmitState("submitting")
    try {
      const createdJob = await submitTranslationJob({
        speakers,
        voiceA: undefined,
        voiceB: undefined,
        youtubeUrl: sourceType === "youtube_url" ? youtubeUrl.trim() : "",
        sourceType,
        localFilePath: sourceType === "local_video" ? uploadedFilePath : undefined,
        localFileName: sourceType === "local_video" ? (uploadFileName || undefined) : undefined,
        transcriptionMethod: sourceType === "local_video" ? "assemblyai" : transcriptionMethod,
        service_mode: serviceMode,
      })
      setActiveJobs((prev) => [createdJob, ...prev])
      setSubmitState("success")
      toast.success(`任务已创建：${getJobDisplayTitle(createdJob)}`)
      // Store latest job ID for /tasks/current fallback
      try { localStorage.setItem("avt_latest_job_id", createdJob.id) } catch {}
      onCreated({ id: createdJob.id, title: getJobDisplayTitle(createdJob) })
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await loadActiveJobs(true)
      }
      setSubmitState("error")
      const msg = getErrorMessage(error)
      if (msg.includes("still active")) {
        toast.error("当前有未完成的任务，请先完成或取消后再创建新翻译。")
      } else {
        toast.error(msg)
      }
    }
  }

  return (
    <div className="space-y-6">
      {/* Concurrency limit guard */}
      {isBlockedByConcurrency && latestActiveJob ? (
        <section className="rounded-2xl border border-amber-500/20 bg-amber-500/5 p-5">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="space-y-1">
              <p className="text-xs font-semibold text-amber-400">
                已达到并发上限（{activeJobCount}/{maxConcurrentJobs}）
              </p>
              <p className="font-semibold text-foreground">请先完成或取消当前任务，再创建新的翻译。</p>
              <p className="text-sm text-muted-foreground">
                {getJobDisplayTitle(latestActiveJob)} · {getStageLabel(latestActiveJob.currentStage)}
              </p>
            </div>
            {latestActiveJob ? (
              <div className="flex gap-2">
                <ConcurrencyActionLink jobId={latestActiveJob.id} label="去处理当前任务" mode={mode} />
              </div>
            ) : null}
          </div>
        </section>
      ) : activeJobCount > 0 && latestActiveJob ? (
        <section className="rounded-2xl border border-border bg-muted/20 p-4">
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">
              当前有 {activeJobCount} 个进行中的任务，仍可创建新任务（上限 {maxConcurrentJobs}）。
            </p>
            <ConcurrencyActionLink jobId={latestActiveJob.id} label="查看任务" mode={mode} variant="subtle" />
          </div>
        </section>
      ) : null}

      {/* Form */}
      <section className="rounded-2xl border border-border bg-card p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-foreground">任务输入</h2>
          {latestActiveJob ? <StatusBadge status={latestActiveJob.status} /> : null}
        </div>
        <form className="space-y-6" onSubmit={handleSubmit}>
          {/* Source type toggle */}
          <div className="flex gap-2">
            <button
              type="button"
              className={`rounded-lg px-4 py-2 text-sm font-medium transition ${sourceType === "youtube_url" ? "bg-primary text-white" : "border border-border bg-muted/30 text-muted-foreground hover:bg-muted/50"}`}
              onClick={() => setSourceType("youtube_url")}
            >
              YouTube 链接
            </button>
            <button
              type="button"
              className={`rounded-lg px-4 py-2 text-sm font-medium transition ${sourceType === "local_video" ? "bg-primary text-white" : "border border-border bg-muted/30 text-muted-foreground hover:bg-muted/50"}`}
              onClick={() => setSourceType("local_video")}
            >
              上传视频
            </button>
          </div>

          {/* YouTube URL input */}
          {sourceType === "youtube_url" ? (
            <div className="space-y-2">
              <span className="text-xs font-medium text-muted-foreground block">YouTube 链接</span>
              <div className="group rounded-xl border border-border bg-muted/30 transition hover:border-primary/30 focus-within:border-primary/40">
                <input
                  className="w-full rounded-xl bg-transparent px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-none input-focus-ring"
                  type="url"
                  placeholder="https://www.youtube.com/watch?v=…"
                  value={youtubeUrl}
                  onChange={(e) => {
                    setYoutubeUrl(e.target.value)
                    if (submitState !== "idle") setSubmitState("idle")
                  }}
                  disabled={isBlockedByConcurrency || submitState === "submitting"}
                />
              </div>
              {validationError && youtubeUrl ? (
                <p className="text-xs text-red-400">{validationError}</p>
              ) : null}
              <p className="text-xs text-muted-foreground/80">
                仅用于翻译您本人或已获授权的视频内容；使用前请确认拥有合法授权，不得用于侵权用途。
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              <span className="text-xs font-medium text-muted-foreground block">选择视频文件</span>
              {uploadedFilePath ? (
                <div className="flex items-center gap-3 rounded-xl border border-emerald-500/20 bg-emerald-500/5 px-4 py-3">
                  <span className="text-sm font-medium text-emerald-400">{uploadFileName}</span>
                  <button
                    className="text-xs text-muted-foreground hover:text-red-400 transition"
                    onClick={() => { setUploadedFilePath(""); setUploadFileName("") }}
                    type="button"
                  >
                    移除
                  </button>
                </div>
              ) : (
                <div className="group rounded-xl border border-border bg-muted/30 transition hover:border-primary/30 focus-within:border-primary/40">
                  <input
                    className="w-full rounded-xl bg-transparent px-4 py-3 text-sm text-foreground file:mr-3 file:rounded-lg file:border-0 file:bg-primary/20 file:px-3 file:py-1 file:text-xs file:font-medium file:text-primary focus:outline-none input-focus-ring"
                    type="file"
                    accept="video/*"
                    disabled={isBlockedByConcurrency || submitState === "submitting" || isUploading}
                    onChange={async (event) => {
                      const file = event.target.files?.[0]
                      if (!file) return
                      setIsUploading(true)
                      setUploadProgress(`正在上传 ${file.name}…`)
                      try {
                        const formData = new FormData()
                        formData.append("file", file)
                        const response = await fetch("/gateway/upload-video", {
                          method: "POST",
                          body: formData,
                        })
                        if (!response.ok) {
                          const err = await response.json().catch(() => ({ error: "上传失败" }))
                          throw new Error(err.error || "上传失败")
                        }
                        const result = await response.json()
                        setUploadedFilePath(result.file_path)
                        setUploadFileName(file.name)
                        setUploadProgress("")
                      } catch (err) {
                        setUploadProgress(err instanceof Error ? err.message : "上传失败")
                      } finally {
                        setIsUploading(false)
                      }
                    }}
                  />
                </div>
              )}
              {uploadProgress ? (
                <p className="text-xs text-muted-foreground">{uploadProgress}</p>
              ) : null}
            </div>
          )}

          <div className="h-px bg-muted/40" />

          {/* Service plan selection */}
          <div className="space-y-3">
            <span className="text-xs font-medium text-muted-foreground block">任务方案</span>
            <div className="grid gap-3 sm:grid-cols-2">
              {/* Express mode */}
              <button
                type="button"
                className={`relative rounded-xl border-2 p-4 text-left transition ${serviceMode === "express" ? "border-primary/50 bg-primary/5 ring-2 ring-primary/20" : "border-border bg-muted/20 hover:border-primary/30"}`}
                disabled={isBlockedByConcurrency || submitState === "submitting"}
                onClick={() => setServiceMode("express")}
              >
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-sm font-semibold text-foreground">快捷版</span>
                  <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold text-emerald-400">Express</span>
                </div>
                <p className="text-xs text-muted-foreground leading-relaxed">全自动流程，AI 识别说话人、翻译、配音，无需人工操作。</p>
                {serviceMode === "express" && (
                  <div className="absolute top-3 right-3 h-4 w-4 rounded-full bg-primary flex items-center justify-center">
                    <svg className="h-2.5 w-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}><path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" /></svg>
                  </div>
                )}
              </button>

              {/* Studio mode — locked unless plan allows it */}
              {(() => {
                const studioAllowed = entitlements?.limits.allowed_service_modes.includes("studio") ?? false
                return studioAllowed ? (
                  <button
                    type="button"
                    className={`relative rounded-xl border-2 p-4 text-left transition ${serviceMode === "studio" ? "border-primary/50 bg-primary/5 ring-2 ring-primary/20" : "border-border bg-muted/20 hover:border-primary/30"}`}
                    disabled={isBlockedByConcurrency || submitState === "submitting"}
                    onClick={() => setServiceMode("studio")}
                  >
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-sm font-semibold text-foreground">工作台版</span>
                      <span className="rounded-full bg-cyan-500/15 px-2 py-0.5 text-[10px] font-semibold text-cyan-400">Studio</span>
                    </div>
                    <p className="text-xs text-muted-foreground leading-relaxed">可审核译文、克隆原声音色，更高质量的定制化配音。</p>
                    {serviceMode === "studio" && (
                      <div className="absolute top-3 right-3 h-4 w-4 rounded-full bg-primary flex items-center justify-center">
                        <svg className="h-2.5 w-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}><path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" /></svg>
                      </div>
                    )}
                  </button>
                ) : (
                  <div className="relative rounded-xl border border-border bg-muted/20 p-4 text-left opacity-60 cursor-not-allowed">
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-sm font-semibold text-foreground">工作台版</span>
                      <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold text-amber-400">Studio</span>
                    </div>
                    <p className="text-xs text-muted-foreground leading-relaxed">可审核译文、克隆原声音色，更高质量的定制化配音。</p>
                    <div className="absolute top-3 right-3 rounded-full bg-muted/50 px-2 py-0.5 text-[10px] text-muted-foreground">
                      {entitlements?.ui.allow_upgrade ? "升级解锁" : "即将开放"}
                    </div>
                  </div>
                )
              })()}
            </div>
            {/* Quota info for free users */}
            {entitlements?.plan_code === "free" && entitlements.limits.free_jobs_quota_remaining != null && (
              <p className="text-xs text-muted-foreground">
                免费额度：已用 {entitlements.limits.free_jobs_quota_used ?? 0} / {entitlements.limits.free_jobs_quota_total ?? 5} 次
              </p>
            )}
          </div>

          <div className="h-px bg-muted/40" />

          {/* Options */}
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <span className="text-xs font-medium text-muted-foreground block">转录方案</span>
              <div className="group rounded-xl border border-border bg-muted/30 transition hover:border-primary/30 focus-within:border-primary/40">
                <select
                  className="w-full rounded-xl bg-transparent px-4 py-3 text-sm text-foreground focus:outline-none input-focus-ring"
                  value={transcriptionMethod}
                  onChange={(e) => setTranscriptionMethod(e.target.value as "assemblyai" | "gemini")}
                  disabled={isBlockedByConcurrency || submitState === "submitting"}
                >
                  <option value="assemblyai">AssemblyAI（音频上传）</option>
                  <option value="gemini">Gemini 多模态（≤30分钟）</option>
                </select>
              </div>
            </div>

            <div className="space-y-2">
              <span className="text-xs font-medium text-muted-foreground block">说话人数</span>
              <div className="group rounded-xl border border-border bg-muted/30 transition hover:border-primary/30 focus-within:border-primary/40">
                <select
                  className="w-full rounded-xl bg-transparent px-4 py-3 text-sm text-foreground focus:outline-none input-focus-ring"
                  value={speakers}
                  onChange={(e) => setSpeakers(e.target.value)}
                  disabled={isBlockedByConcurrency || submitState === "submitting"}
                >
                  <option value="auto">自动</option>
                  <option value="1">1 人</option>
                  <option value="2">2 人</option>
                  <option value="3">3 人</option>
                  <option value="4">4 人</option>
                  <option value="5">5 人</option>
                  <option value="6">6 人</option>
                </select>
              </div>
            </div>
          </div>

          <p className="text-xs text-muted-foreground/60">
            快捷版自动完成全部流程，无需人工操作。工作台版可审核译文、克隆原声音色。
          </p>

          <button
            type="submit"
            disabled={Boolean(validationError) || isBlockedByConcurrency || submitState === "submitting" || isLoadingGuard}
            className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-primary to-primary/80 px-6 py-2.5 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110 disabled:opacity-50"
          >
            {submitState === "submitting" ? "创建中…" : "创建任务"}
          </button>
        </form>
      </section>
    </div>
  )
}

/* ---------- internal helpers ---------- */

function validateYoutubeUrl(value: string) {
  const v = value.trim()
  if (!v) return "请输入 YouTube 链接。"
  try {
    const url = new URL(v)
    const host = url.hostname.toLowerCase()
    const isYt = host === "youtu.be" || host.endsWith("youtube.com") || host.endsWith("youtube-nocookie.com")
    if (!["http:", "https:"].includes(url.protocol) || !isYt) return "当前只支持有效的 YouTube 链接。"
    return null
  } catch {
    return "请输入有效的链接。"
  }
}

/**
 * Small helper so the concurrency banners can navigate in page mode
 * (via next/navigation) but remain inert in dialog mode where the
 * container controls navigation.
 */
function ConcurrencyActionLink({
  jobId,
  label,
  mode,
  variant = "primary",
}: {
  jobId: string
  label: string
  mode: "page" | "dialog"
  variant?: "primary" | "subtle"
}) {
  if (mode === "dialog") return null

  // Dynamic import is not needed — we can conditionally render an <a> tag
  // which works fine for in-app navigation via Next.js link behavior.
  const href = `/workspace/${jobId}`

  if (variant === "subtle") {
    return (
      <a className="text-xs text-primary hover:underline" href={href}>
        {label}
      </a>
    )
  }

  return (
    <a
      className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-primary to-primary/80 px-5 py-2 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110"
      href={href}
    >
      {label}
    </a>
  )
}
