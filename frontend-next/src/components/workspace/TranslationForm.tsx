"use client"

import { useEffect, useState, type FormEvent } from "react"
import { toast } from "sonner"

import { StatusBadge } from "@/components/status-badge"
import { getJobDisplayTitle, getStageLabel } from "@/features/jobs/presentation"
import { ApiError } from "@/lib/api/client"
import { getErrorMessage } from "@/lib/api/errors"
import {
  getEntitlements,
  getExpressAutoCloneAvailability,
  type UserEntitlements,
} from "@/lib/api/entitlements"
import { listJobs, submitTranslationJob } from "@/lib/api/jobs"
import { getCreditsEstimate, getMyCredits, type CreditsResponse } from "@/lib/billing/get-credits"
import { getVoiceLibrary, type VoiceLibraryEntry } from "@/lib/api/voiceLibrary"
import { getVoiceSelectionPricing } from "@/lib/api/voiceSelection"
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
  const [serviceMode, setServiceMode] = useState<"express" | "studio" | "smart">("express")
  // Phase 4.3a PR3 — Express auto-clone consent. Both default false (opt-in).
  // `expressAutoCloneAvailable` is server-gated (admin flag + allowlist) and
  // fail-closed; the checkbox only renders when express + available.
  const [expressAutoCloneAvailable, setExpressAutoCloneAvailable] = useState(false)
  const [expressAutoVoiceClone, setExpressAutoVoiceClone] = useState(false)
  const [entitlements, setEntitlements] = useState<UserEntitlements | null>(null)
  const [credits, setCredits] = useState<CreditsResponse | null>(null)
  const [creditRates, setCreditRates] = useState<{
    expressStandard: number | null
    studioStandard: number | null
    studioHigh: number | null
    studioFlagship: number | null
    smartStandard: number | null
  }>({
    expressStandard: null,
    studioStandard: null,
    studioHigh: null,
    studioFlagship: null,
    smartStandard: null,
  })
  const [savedVoices, setSavedVoices] = useState<VoiceLibraryEntry[]>([])
  const [activeJobs, setActiveJobs] = useState<JobSummary[]>([])
  const [isLoadingGuard, setIsLoadingGuard] = useState(true)
  const [submitState, setSubmitState] = useState<"error" | "idle" | "submitting" | "success">("idle")
  // Phase 4 (plan 2026-05-17-user-voice-candidate-first §Smart 弱匹配
  // 暂停): when admin enables smart_pause_on_possible_user_voice_match,
  // Smart jobs may pause for human confirmation if a possible
  // personal-voice candidate is found. Surface this in the submission
  // UI so users aren't surprised by a mid-job pause.
  const [smartPauseWarningEnabled, setSmartPauseWarningEnabled] = useState(false)

  const validationError =
    sourceType === "youtube_url"
      ? validateYoutubeUrl(youtubeUrl)
      : !uploadedFilePath
        ? "请先上传视频文件。"
        : null

  const isUnlimitedConcurrency = entitlements?.limits.max_concurrent_jobs === null
  const maxConcurrentJobs = entitlements?.limits.max_concurrent_jobs ?? 1
  const concurrencyLimitLabel = isUnlimitedConcurrency ? "无限制" : String(maxConcurrentJobs)
  const activeJobCount = activeJobs.length
  const isBlockedByConcurrency = !isUnlimitedConcurrency && activeJobCount >= maxConcurrentJobs
  const currentRate =
    serviceMode === "smart"
      ? creditRates.smartStandard
      : serviceMode === "studio"
        ? creditRates.studioStandard
        : creditRates.expressStandard
  const balanceLabel = credits ? `${credits.total_available} 点` : "读取中"
  const rateLabel = currentRate != null ? `${currentRate} 点/分钟` : "读取中"
  // For UI display: show the most recent active job if blocked
  const latestActiveJob = activeJobs.length > 0 ? activeJobs[0] : null
  const planCardBaseClass = "relative rounded-xl border-2 p-4 text-left transition"
  const planCardSelectedClass = `${planCardBaseClass} border-transparent`
  const planCardIdleClass = `${planCardBaseClass} border-border bg-muted/20 hover:border-primary/30`
  const selectedPlanStyle = {
    borderColor: "color-mix(in oklab, var(--primary) 52%, transparent)",
    backgroundColor: "color-mix(in oklab, var(--primary) 7%, transparent)",
    boxShadow: "0 0 0 2px color-mix(in oklab, var(--primary) 18%, transparent)",
  }

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
    getMyCredits()
      .then((value) => setCredits(value))
      .catch(() => {})
    Promise.all([
      getCreditsEstimate(1, "express", "standard"),
      getCreditsEstimate(1, "studio", "standard"),
      getCreditsEstimate(1, "studio", "high"),
      getCreditsEstimate(1, "studio", "flagship"),
      // Smart MVP P2: fixed 100 credits/min per source duration.
      // Master plan §2.2 — single user-facing price; quality_tier
      // internally stays "standard" for compat with the 2D pricing
      // table (Plan §5.1).
      getCreditsEstimate(1, "smart", "standard").catch(() => null),
    ])
      .then(([expressStandard, studioStandard, studioHigh, studioFlagship, smartStandard]) => {
        setCreditRates({
          expressStandard: expressStandard.estimated_credits,
          studioStandard: studioStandard.estimated_credits,
          studioHigh: studioHigh.estimated_credits,
          studioFlagship: studioFlagship.estimated_credits,
          smartStandard: smartStandard?.estimated_credits ?? null,
        })
      })
      .catch(() => {})
    // Phase 4: read admin smart_pause_warning_enabled off the pricing
    // endpoint. The flag piggybacks on this endpoint to avoid a new
    // public admin-policy endpoint. Defaults to false (no warning)
    // if the field is absent or the call fails.
    getVoiceSelectionPricing()
      .then((pricing) => {
        setSmartPauseWarningEnabled(Boolean(pricing.smart_pause_warning_enabled))
      })
      .catch(() => setSmartPauseWarningEnabled(false))
    // Phase 4.3a PR3: Express auto-clone availability (admin flag + allowlist).
    // Fail-closed in the client; default state is already false.
    getExpressAutoCloneAvailability()
      .then((a) => setExpressAutoCloneAvailable(a.available === true))
      .catch(() => setExpressAutoCloneAvailable(false))
  }, [])

  // Phase 4.3a PR3 (spec §2.6): consent must never linger as true. Leaving
  // Express, or losing availability, force-resets the checkbox to false so a
  // stale opt-in can't ride into a later submit / a non-express job.
  useEffect(() => {
    if (serviceMode !== "express" || !expressAutoCloneAvailable) {
      setExpressAutoVoiceClone(false)
    }
  }, [serviceMode, expressAutoCloneAvailable])

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
        // spec §2.6: force false unless currently in Express, so a stale
        // checkbox (mode switched away then back) can't trigger a paid clone.
        expressAutoVoiceClone: serviceMode === "express" ? expressAutoVoiceClone : false,
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
                已达到并发上限（{activeJobCount}/{concurrencyLimitLabel}）
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
              当前有 {activeJobCount} 个进行中的任务，仍可创建新任务（上限 {concurrencyLimitLabel}）。
            </p>
            <ConcurrencyActionLink jobId={latestActiveJob.id} label="查看任务" mode={mode} variant="subtle" />
          </div>
        </section>
      ) : null}

      {/* Form */}
      <section className="min-w-0 rounded-2xl border border-border bg-card p-4 sm:p-6">
        <div className="mb-5 flex min-w-0 items-center justify-between gap-3">
          <h2 className="text-lg font-semibold text-foreground">任务输入</h2>
          {latestActiveJob ? <StatusBadge status={latestActiveJob.status} /> : null}
        </div>
        <form className="space-y-6" onSubmit={handleSubmit}>
          {/* Source type toggle */}
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <button
              type="button"
              className={`min-w-0 break-keep rounded-lg px-4 py-2 text-sm font-medium transition ${sourceType === "youtube_url" ? "bg-primary text-white" : "border border-border bg-muted/30 text-muted-foreground hover:bg-muted/50"}`}
              onClick={() => setSourceType("youtube_url")}
            >
              YouTube 链接
            </button>
            <button
              type="button"
              className={`min-w-0 break-keep rounded-lg px-4 py-2 text-sm font-medium transition ${sourceType === "local_video" ? "bg-primary text-white" : "border border-border bg-muted/30 text-muted-foreground hover:bg-muted/50"}`}
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
                <div
                  className="flex items-center gap-3 rounded-xl px-4 py-3"
                  style={{
                    backgroundColor: "color-mix(in oklab, var(--bamboo) 10%, transparent)",
                    border: "1px solid color-mix(in oklab, var(--bamboo) 28%, transparent)",
                  }}
                >
                  <span className="text-sm font-medium" style={{ color: "var(--bamboo)" }}>
                    {uploadFileName}
                  </span>
                  <button
                    className="text-xs text-muted-foreground transition hover:text-[color:var(--cinnabar)]"
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
                        // P2-18E (audit 2026-05-07, F-HIGH-3): send the
                        // session cookie. Pre-fix the upload-video fetch
                        // omitted ``credentials``, which on browsers
                        // configured to NOT send cookies cross-site by
                        // default (Safari ITP, some Chromium privacy
                        // modes, Edge with strict tracking prevention)
                        // landed at gateway as anonymous → 401. Other
                        // mutating endpoints in this codebase already
                        // pass ``credentials: 'include'``; aligning
                        // here closes the inconsistency.
                        const response = await fetch("/gateway/upload-video", {
                          method: "POST",
                          credentials: "include",
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

          {/* Service plan selection — vertical stack so each option's
            * description has room to breathe (cramped grid layout
            * forced 4-char vertical text wrapping on narrow widths). */}
          <div className="space-y-3">
            <span className="text-xs font-medium text-muted-foreground block">任务方案</span>
            <div className="grid gap-3">
              {/* Express mode */}
              <button
                type="button"
                className={serviceMode === "express" ? planCardSelectedClass : planCardIdleClass}
                style={serviceMode === "express" ? selectedPlanStyle : undefined}
                disabled={isBlockedByConcurrency || submitState === "submitting"}
                onClick={() => setServiceMode("express")}
              >
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-sm font-semibold text-foreground">快捷版</span>
                  <span
                    className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
                    style={{
                      backgroundColor: "color-mix(in oklab, var(--bamboo) 14%, transparent)",
                      color: "var(--bamboo)",
                      border: "1px solid color-mix(in oklab, var(--bamboo) 30%, transparent)",
                    }}
                  >
                    Express
                  </span>
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
                    className={serviceMode === "studio" ? planCardSelectedClass : planCardIdleClass}
                    style={serviceMode === "studio" ? selectedPlanStyle : undefined}
                    disabled={isBlockedByConcurrency || submitState === "submitting"}
                    onClick={() => setServiceMode("studio")}
                  >
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-sm font-semibold text-foreground">工作台版</span>
                      <span
                        className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
                        style={{
                          backgroundColor: "color-mix(in oklab, var(--ochre) 14%, transparent)",
                          color: "var(--ochre)",
                          border: "1px solid color-mix(in oklab, var(--ochre) 32%, transparent)",
                        }}
                      >
                        Studio
                      </span>
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
                      <span
                        className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
                        style={{
                          backgroundColor: "color-mix(in oklab, var(--ochre) 14%, transparent)",
                          color: "var(--ochre)",
                          border: "1px solid color-mix(in oklab, var(--ochre) 32%, transparent)",
                        }}
                      >
                        Studio
                      </span>
                    </div>
                    <p className="text-xs text-muted-foreground leading-relaxed">可审核译文、克隆原声音色，更高质量的定制化配音。</p>
                    <div className="absolute top-3 right-3 rounded-full bg-muted/50 px-2 py-0.5 text-[10px] text-muted-foreground">
                      {entitlements?.ui.allow_upgrade ? "升级解锁" : "即将开放"}
                    </div>
                  </div>
                )
              })()}

              {/* Smart mode — locked unless plan allows it. Smart MVP P2:
                * 100 credits/min fixed price, AI auto-decisions for translation
                * review + voice cloning. plan_catalog gates plus + pro. */}
              {(() => {
                const smartAllowed = entitlements?.limits.allowed_service_modes.includes("smart") ?? false
                return smartAllowed ? (
                  <button
                    type="button"
                    className={serviceMode === "smart" ? planCardSelectedClass : planCardIdleClass}
                    style={serviceMode === "smart" ? selectedPlanStyle : undefined}
                    disabled={isBlockedByConcurrency || submitState === "submitting"}
                    onClick={() => setServiceMode("smart")}
                  >
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-sm font-semibold text-foreground">智能版</span>
                      <span
                        className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
                        style={{
                          backgroundColor: "color-mix(in oklab, var(--primary) 14%, transparent)",
                          color: "var(--primary)",
                          border: "1px solid color-mix(in oklab, var(--primary) 32%, transparent)",
                        }}
                      >
                        Smart
                      </span>
                    </div>
                    <p className="text-xs text-muted-foreground leading-relaxed">100 点/分钟固定价，AI 自动审核翻译并自动克隆音色，无需人工操作。</p>
                    {serviceMode === "smart" && (
                      <div className="absolute top-3 right-3 h-4 w-4 rounded-full bg-primary flex items-center justify-center">
                        <svg className="h-2.5 w-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}><path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" /></svg>
                      </div>
                    )}
                  </button>
                ) : (
                  <div className="relative rounded-xl border border-border bg-muted/20 p-4 text-left opacity-60 cursor-not-allowed">
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-sm font-semibold text-foreground">智能版</span>
                      <span
                        className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
                        style={{
                          backgroundColor: "color-mix(in oklab, var(--primary) 14%, transparent)",
                          color: "var(--primary)",
                          border: "1px solid color-mix(in oklab, var(--primary) 32%, transparent)",
                        }}
                      >
                        Smart
                      </span>
                    </div>
                    <p className="text-xs text-muted-foreground leading-relaxed">100 点/分钟固定价，AI 自动审核翻译并自动克隆音色，无需人工操作。</p>
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

          <section className="rounded-xl border border-border bg-muted/20 p-4">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <p className="text-sm font-medium text-foreground">扣点标准</p>
              <p className="text-xs text-muted-foreground">当前可用：{balanceLabel}</p>
            </div>
            {serviceMode === "express" ? (
              <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                快捷版按源视频时长扣点，当前标准为 {rateLabel}。创建任务时会按可识别的时长预扣；点数不足会停止创建并提示充值或升级。
              </p>
            ) : serviceMode === "smart" ? (
              <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                智能版按源视频时长固定扣点，当前标准为 {rateLabel}。AI 自动审核翻译并按需克隆主说话人音色，系统内部的重试、克隆、TTS 调用都计入这个固定价，不会另外扣点。当前阶段限制：主说话人不超过 3 位；若不满足条件会自动降级到工作台版或退点。
              </p>
            ) : (
              <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                工作台版按源视频时长扣点，基础标准为 {rateLabel}；后续选择高级/旗舰音质时分别按 {creditRates.studioHigh ?? "读取中"} / {creditRates.studioFlagship ?? "读取中"} 点/分钟扣除。音色克隆为单次独立扣点，克隆弹窗会再次确认费用。
              </p>
            )}
          </section>

          {/* Phase 4.3a PR3: Express auto-clone consent checkbox. Renders ONLY
              when express mode AND the server reports availability (admin flag +
              allowlist; fail-closed). Default unchecked (opt-in). Copy must not
              promise a deletion deadline — the cleanup sweeper is Phase 4.3b. */}
          {serviceMode === "express" && expressAutoCloneAvailable ? (
            <section
              className="rounded-xl border p-4"
              style={{
                backgroundColor: "color-mix(in oklab, var(--bamboo) 8%, transparent)",
                borderColor: "color-mix(in oklab, var(--bamboo) 32%, transparent)",
              }}
            >
              <label className="flex cursor-pointer items-start gap-3">
                <input
                  type="checkbox"
                  className="mt-1 h-4 w-4 shrink-0 accent-[color:var(--primary)]"
                  checked={expressAutoVoiceClone}
                  disabled={isBlockedByConcurrency || submitState === "submitting"}
                  onChange={(e) => setExpressAutoVoiceClone(e.target.checked)}
                />
                <span className="block space-y-1.5">
                  <span className="flex items-center gap-2">
                    <span className="text-sm font-medium text-foreground">自动克隆主说话人音色</span>
                    <span
                      className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
                      style={{
                        backgroundColor: "color-mix(in oklab, var(--bamboo) 14%, transparent)",
                        color: "var(--bamboo)",
                        border: "1px solid color-mix(in oklab, var(--bamboo) 30%, transparent)",
                      }}
                    >
                      实验性
                    </span>
                  </span>
                  <span className="block text-xs leading-relaxed text-muted-foreground">
                    勾选后，系统会用视频中占比最高的说话人的一小段语音（约 10–20 秒）克隆一个临时音色用于本次配音，让主说话人的声音更贴近原片。
                  </span>
                  <span className="block space-y-0.5 text-xs leading-relaxed text-muted-foreground">
                    <span className="block">· 该音色为本次任务临时使用，不进入你的永久音色库；系统后续会按清理策略处理</span>
                    <span className="block">· 会占用一次音色克隆配额</span>
                    <span className="block">· 失败时自动改用预设音色，不影响配音完成</span>
                  </span>
                </span>
              </label>
            </section>
          ) : null}

          {/* Phase 4 (plan 2026-05-17-user-voice-candidate-first §Smart 弱匹配暂停):
              warn the user when admin has enabled the "weak match confirmation"
              mode and they're picking Smart. Without this, a Smart job that
              hits a possible (non-strong) personal voice candidate would
              pause for human confirmation and surprise the user who expected
              full automation. */}
          {serviceMode === "smart" && smartPauseWarningEnabled ? (
            <section
              className="rounded-xl border p-4 text-xs leading-relaxed"
              style={{
                backgroundColor: "color-mix(in oklab, var(--ochre) 8%, transparent)",
                borderColor: "color-mix(in oklab, var(--ochre) 40%, transparent)",
                color: "var(--foreground)",
              }}
              role="status"
            >
              <p className="font-medium">弱匹配确认模式已开启</p>
              <p className="mt-1 text-muted-foreground">
                管理员已开启“弱匹配确认”策略：如果系统在你的个人音色库中发现可能匹配的音色（但相似度不够强），任务会暂停在音色审核页面，等你确认是否复用，再继续后续步骤。复用个人音色不消耗克隆点；如果不想复用，可以选择官方音色或重新克隆。
              </p>
            </section>
          ) : null}

          {/* Advanced options (转录方案 / 说话人数) — temporarily hidden per
              user request: 目前暂时用不到. The selects stay mounted so their
              default state values (assemblyai / auto) flow through to
              submitJob(). Restore by removing the `hidden` wrapper. */}
          <div className="hidden">
            <div className="h-px bg-muted/40" />

            {/* Options */}
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
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
          </div>

          <p className="text-xs text-muted-foreground/60">
            快捷版自动完成全部流程，无需人工操作。工作台版可审核译文、克隆原声音色。
          </p>

          <button
            type="submit"
            disabled={Boolean(validationError) || isBlockedByConcurrency || submitState === "submitting" || isLoadingGuard}
            className="inline-flex w-full items-center justify-center gap-2 rounded-[var(--radius)] border border-transparent bg-gradient-to-r from-primary to-primary/80 px-6 py-2.5 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110 disabled:cursor-not-allowed disabled:border-border disabled:bg-muted disabled:bg-none disabled:text-muted-foreground disabled:shadow-none disabled:hover:brightness-100 disabled:hover:shadow-none sm:w-auto"
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
      className="inline-flex items-center gap-2 rounded-[var(--radius)] bg-gradient-to-r from-primary to-primary/80 px-5 py-2 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110"
      href={href}
    >
      {label}
    </a>
  )
}
