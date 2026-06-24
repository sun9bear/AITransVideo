"use client"

import { useEffect, useRef, useState, type FormEvent } from "react"
import Link from "next/link"
import { toast } from "sonner"

import { useSession } from "@/components/providers/session-provider"
import { StatusBadge } from "@/components/status-badge"
import { SmartPreviewConfirmDialog } from "@/components/workspace/SmartPreviewConfirmDialog"
import { getJobDisplayTitle, getStageLabel } from "@/features/jobs/presentation"
import { ApiError } from "@/lib/api/client"
import { getErrorMessage } from "@/lib/api/errors"
import {
  getEntitlements,
  getExpressAutoCloneAvailability,
  type UserEntitlements,
} from "@/lib/api/entitlements"
import { listJobs, submitTranslationJob } from "@/lib/api/jobs"
import { clearAnonConvertReady, getAnonConvertReady, subscribeAnonConvertReady } from "@/lib/api/claim"
import { isSmartPreviewCloneEntryEnabled } from "@/lib/api/smartPreviewClone"
import { getCreditsEstimate, getMyCredits, type CreditsResponse } from "@/lib/billing/get-credits"
import { getVoiceLibrary, type VoiceLibraryEntry } from "@/lib/api/voiceLibrary"
import { getVoiceSelectionPricing } from "@/lib/api/voiceSelection"
import {
  getLanguageFacts,
  GA_DEFAULT_LANGUAGE_FACT,
  type LanguagePairFact,
} from "@/lib/api/languageFacts"
import {
  getChunkedUploadLimits,
  uploadFileInChunks,
  type ChunkedUploadLimits,
} from "@/lib/upload/chunkedUpload"
import { usePollingTask } from "@/lib/react/usePollingTask"
import { ACTIVE_JOB_STATUSES, type CreateTranslationJobInput, type JobSummary } from "@/types/jobs"

export interface TranslationFormProps {
  /** Called when job is created successfully. Container decides what happens next. */
  onCreated: (job: { id: string; title: string }) => void
  /** 'page' mode = container handles redirect; 'dialog' mode = container handles close */
  mode: "page" | "dialog"
  /** Pre-fill YouTube URL (e.g., from "recreate" on a failed job) */
  initialSourceUrl?: string
}

type ServiceMode = "express" | "studio" | "smart" | "free"

export function TranslationForm({ onCreated, mode, initialSourceUrl }: TranslationFormProps) {
  const { user } = useSession()
  const [sourceType, setSourceType] = useState<"youtube_url" | "local_video">("youtube_url")
  const [youtubeUrl, setYoutubeUrl] = useState(initialSourceUrl ?? "")
  const [uploadedFilePath, setUploadedFilePath] = useState("")
  const [uploadFileName, setUploadFileName] = useState("")
  const [isUploading, setIsUploading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState("")
  // 分片上传选路参数（plan 2026-06-11 §3.9）：阈值 / 切片大小从 R6 limits
  // 端点动态拉取，不硬编码。null = 端点不可用 → 一律走单请求路径。
  const [chunkedLimits, setChunkedLimits] = useState<ChunkedUploadLimits | null>(null)
  const [speakers, setSpeakers] = useState<string>("auto")
  const [transcriptionMethod, setTranscriptionMethod] = useState<"assemblyai" | "gemini">("assemblyai")
  const [serviceMode, setServiceMode] = useState<ServiceMode>("express")
  // PR-A part 2 §7: language direction. Facts are entitlement-filtered by the
  // gateway (default-only for most users → selector stays hidden); the selector
  // appears only when the account has access to a 内测 direction.
  const [languageFacts, setLanguageFacts] = useState<LanguagePairFact[]>([GA_DEFAULT_LANGUAGE_FACT])
  const [languagePairKey, setLanguagePairKey] = useState<string>(GA_DEFAULT_LANGUAGE_FACT.pair_key)
  // Phase 2a free tier — entry gated by NEXT_PUBLIC_ENABLE_FREE_TIER (internal
  // until the consent/legal launch gate clears). Mirrors POST_EDIT_ENABLED.
  const freeTierEnabled = process.env.NEXT_PUBLIC_ENABLE_FREE_TIER === "1"
  // Phase 4.3a PR3 — Express auto-clone consent. Both default false (opt-in).
  // `expressAutoCloneAvailable` is server-gated (admin flag + allowlist) and
  // fail-closed; the checkbox only renders when express + available.
  const [expressAutoCloneAvailable, setExpressAutoCloneAvailable] = useState(false)
  const [expressAutoVoiceClone, setExpressAutoVoiceClone] = useState(false)
  const [smartPaidCloneAccepted, setSmartPaidCloneAccepted] = useState(false)
  // Phase 2a LAUNCH GATE: free voice-rights attestation (《民法典》1023). Must be
  // checked before a free job may be submitted; reset on mode switch.
  const [freeVoiceRightsConfirmed, setFreeVoiceRightsConfirmed] = useState(false)
  const [entitlements, setEntitlements] = useState<UserEntitlements | null>(null)
  // 扣费门拦截（点数不足/额度用尽）不走转瞬即逝的 toast，落成持久 banner，
  // 带升级入口——这是购买意图最高的时刻。
  const [creditGateError, setCreditGateError] = useState<string | null>(null)
  // A 方案（转化漏斗 UX）：转完整时原视频超套餐时长上限被后端 pre-flight 闸拦 →
  // 落成持久 banner（而非转瞬即逝的 toast——这是购买意图最高的时刻）。两档（CodeX
  // P1）：``canUpgrade`` true=超出当前套餐但升级可解决 → 给 /pricing CTA；false=超过
  // 最高自助套餐（Pro 180min），升级也没用 → 只提示用更短视频 / 联系客服，**不**给
  // /pricing。两档都保留转完整模式（源有效，升级 / 换更短视频后可重试）。
  const [durationBlock, setDurationBlock] = useState<{ message: string; canUpgrade: boolean } | null>(null)
  const [creditsLoadFailed, setCreditsLoadFailed] = useState(false)
  const [ratesLoadFailed, setRatesLoadFailed] = useState(false)
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
  const [voiceCloneCostCredits, setVoiceCloneCostCredits] = useState<number | null>(null)
  const [smartPreviewCloneCostCredits, setSmartPreviewCloneCostCredits] = useState<number | null>(null)
  const [voiceCloneCostLoadFailed, setVoiceCloneCostLoadFailed] = useState(false)
  // P3e-4c：智能版 3 分钟预览克隆入口（免费 / 未获 smart 的登录用户）。展示闸由
  // Next flag 控制；真正的 gate 在服务端（admin smart_preview_clone_enabled +
  // lane exemption + clone reservation）。flag 关 → 入口不渲染（默认 inert）。
  const smartPreviewEntryEnabled = isSmartPreviewCloneEntryEnabled()
  const [smartPreviewOpen, setSmartPreviewOpen] = useState(false)
  const [smartPreviewInput, setSmartPreviewInput] = useState<CreateTranslationJobInput | null>(null)
  // D7 匿名预览转完整：登录认领后，创建页用认领的**完整原视频**作源（免重新上传）。
  // mount 时从 localStorage（认领成功写入 avt_anon_convert_ready）读 preview_id；非空 →
  // 渲染认领来源 banner、跳过源校验、提交带 reuse_anonymous_preview_id（服务端覆盖 source
  // 走正常付费流程，用户照常选模式付费）。
  const [reuseAnonPreviewId, setReuseAnonPreviewId] = useState<string | null>(null)
  // 双击守卫（CodeX P2）：submitState 异步，按钮禁用前的快速双击可能触发两次提交→
  // 两个付费任务。ref 级 guard 同步拦截（普通 create + 转完整都防）。
  const submittingRef = useRef(false)

  const sourceValidationError = reuseAnonPreviewId
    ? null
    : sourceType === "youtube_url"
      ? validateYoutubeUrl(youtubeUrl)
      : !uploadedFilePath
        ? "请先上传视频文件。"
        : null
  const allowedServiceModes = entitlements?.limits.allowed_service_modes ?? []
  const isServiceModeSelectable = (mode: ServiceMode) =>
    entitlements
      ? allowedServiceModes.includes(mode) && (mode !== "free" || freeTierEnabled)
      : mode === "express"
  const expressAllowed = isServiceModeSelectable("express")
  const freeAllowed = isServiceModeSelectable("free")
  const studioAllowed = isServiceModeSelectable("studio")
  const smartAllowed = isServiceModeSelectable("smart")
  const isAdminUser = entitlements?.role === "admin"
  const hasPaidPlan = entitlements?.plan_code === "plus" || entitlements?.plan_code === "pro"
  const hasStudioPlanEntitlement = isAdminUser || hasPaidPlan || entitlements?.ui.in_trial === true
  const hasSmartPlanEntitlement = isAdminUser || hasPaidPlan
  const studioRolloutOffline = Boolean(entitlements) && hasStudioPlanEntitlement && !studioAllowed
  const smartRolloutOffline = Boolean(entitlements) && hasSmartPlanEntitlement && !smartAllowed
  const hasAnyServiceMode = expressAllowed || freeAllowed || studioAllowed || smartAllowed
  const serviceModeUnavailableError =
    entitlements && !isServiceModeSelectable(serviceMode)
      ? "当前选择的任务方案暂未上线，请选择其它方案。"
      : null
  // Phase 2a LAUNCH GATE: a free job requires the voice-rights attestation
  // (《民法典》1023). Keep submit blocked until it is checked — the backend
  // HARD-fails (403 consent_required) otherwise.
  const validationError =
    sourceValidationError ??
    (!hasAnyServiceMode ? "当前没有可用的任务方案，请联系管理员。" : null) ??
    serviceModeUnavailableError ??
    (serviceMode === "free" && !freeVoiceRightsConfirmed
      ? "请先阅读并勾选免费版声音授权声明。"
      : null)

  const isUnlimitedConcurrency = entitlements?.limits.max_concurrent_jobs === null
  const maxConcurrentJobs = entitlements?.limits.max_concurrent_jobs ?? 1
  const concurrencyLimitLabel = isUnlimitedConcurrency ? "无限制" : String(maxConcurrentJobs)
  const activeJobCount = activeJobs.length
  const isBlockedByConcurrency = !isUnlimitedConcurrency && activeJobCount >= maxConcurrentJobs
  const currentRate =
    serviceMode === "free"
      ? 0
      : serviceMode === "smart"
        ? creditRates.smartStandard
        : serviceMode === "studio"
          ? creditRates.studioStandard
          : creditRates.expressStandard
  const balanceLabel = credits
    ? `${credits.total_available} 点`
    : creditsLoadFailed
      ? "暂时无法读取"
      : "读取中"
  const rateLabel =
    currentRate != null
      ? `${currentRate} 点/分钟`
      : ratesLoadFailed
        ? "暂时无法读取"
        : "读取中"
  const voiceCloneCostLabel =
    voiceCloneCostCredits != null
      ? `${voiceCloneCostCredits} 点`
      : voiceCloneCostLoadFailed
        ? "暂时无法读取"
        : "读取中"
  const smartPreviewCloneCostLabel =
    smartPreviewCloneCostCredits != null
      ? `${smartPreviewCloneCostCredits} 点`
      : voiceCloneCostLoadFailed
        ? "暂时无法读取"
        : "读取中"
  // For UI display: show the most recent active job if blocked
  const latestActiveJob = activeJobs.length > 0 ? activeJobs[0] : null
  const planCardBaseClass = "relative rounded-xl border-2 p-4 text-left transition"
  const planCardSelectedClass = `${planCardBaseClass} border-transparent`
  const planCardIdleClass = `${planCardBaseClass} border-border bg-muted/20 hover:border-primary/30`
  const planCardDisabledClass = `${planCardBaseClass} border-border bg-muted/20 opacity-60 cursor-not-allowed`
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
      .catch(() => setCreditsLoadFailed(true))
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
      .catch(() => setRatesLoadFailed(true))
    // Phase 4: read admin smart_pause_warning_enabled off the pricing
    // endpoint. The flag piggybacks on this endpoint to avoid a new
    // public admin-policy endpoint. Defaults to false (no warning)
    // if the field is absent or the call fails.
    getVoiceSelectionPricing()
      .then((pricing) => {
        setSmartPauseWarningEnabled(Boolean(pricing.smart_pause_warning_enabled))
        setVoiceCloneCostCredits(pricing.voice_clone_cost_credits)
        setSmartPreviewCloneCostCredits(pricing.smart_preview_clone_cost_credits)
        setVoiceCloneCostLoadFailed(false)
      })
      .catch(() => {
        setSmartPauseWarningEnabled(false)
        setVoiceCloneCostCredits(null)
        setSmartPreviewCloneCostCredits(null)
        setVoiceCloneCostLoadFailed(true)
      })
    // Phase 4.3a PR3: Express auto-clone availability (admin flag + allowlist).
    // Fail-closed in the client; default state is already false.
    getExpressAutoCloneAvailability()
      .then((a) => setExpressAutoCloneAvailable(a.available === true))
      .catch(() => setExpressAutoCloneAvailable(false))
    // 分片上传通道可用性（admin 开关 + 阈值）。失败 → null（走单请求路径）。
    getChunkedUploadLimits()
      .then((l) => setChunkedLimits(l))
      .catch(() => setChunkedLimits(null))
    // PR-A part 2 §7: language directions the account may pick. Fail-closed in
    // the client (getLanguageFacts → GA default only on any error). The GA
    // default pair_key is always present, so the initial selection stays valid;
    // handleSubmit also falls back to the default for any unmatched key.
    getLanguageFacts()
      .then((facts) => setLanguageFacts(facts.length > 0 ? facts : [GA_DEFAULT_LANGUAGE_FACT]))
      .catch(() => setLanguageFacts([GA_DEFAULT_LANGUAGE_FACT]))
  }, [])

  useEffect(() => {
    if (!entitlements) return
    const canUseMode = (mode: ServiceMode) =>
      entitlements.limits.allowed_service_modes.includes(mode) &&
      (mode !== "free" || freeTierEnabled)
    if (canUseMode(serviceMode)) return
    const fallback = (["express", "studio", "smart", "free"] as ServiceMode[])
      .find((mode) => canUseMode(mode))
    if (fallback) {
      setServiceMode(fallback)
    }
  }, [entitlements, freeTierEnabled, serviceMode])

  // D7：mount 时读认领成功写入的 preview_id（avt_anon_convert_ready）→ 进入「转完整」
  // 模式（用认领的完整原视频作源，免重新上传）。localStorage key 在转完整成功 或
  // 用户「更换视频」时才清，故刷新页面不丢（提交失败可重试）。
  useEffect(() => {
    const syncConvertReady = () => {
      setReuseAnonPreviewId(getAnonConvertReady(user?.id))
    }
    const timerId = window.setTimeout(syncConvertReady, 0)
    const unsubscribe = subscribeAnonConvertReady(syncConvertReady)
    return () => {
      window.clearTimeout(timerId)
      unsubscribe()
    }
  }, [user?.id])

  // Phase 4.3a PR3 (spec §2.6): consent must never linger as true. Leaving
  // Express, or losing availability, force-resets the checkbox to false so a
  // stale opt-in can't ride into a later submit / a non-express job.
  useEffect(() => {
    if (serviceMode !== "express" || !expressAutoCloneAvailable) {
      setExpressAutoVoiceClone(false)
    }
  }, [serviceMode, expressAutoCloneAvailable])

  // Phase 2a LAUNCH GATE: leaving free mode clears the attestation so a stale
  // consent can't ride into a later submit.
  useEffect(() => {
    if (serviceMode !== "free") {
      setFreeVoiceRightsConfirmed(false)
    }
  }, [serviceMode])

  useEffect(() => {
    if (serviceMode !== "smart" || voiceCloneCostCredits == null) {
      setSmartPaidCloneAccepted(false)
    }
  }, [serviceMode, voiceCloneCostCredits])

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (submittingRef.current) return // 双击守卫（CodeX P2）：同步拦截快速重复提交
    if (validationError) {
      toast.error(validationError)
      return
    }
    submittingRef.current = true
    setSubmitState("submitting")
    setCreditGateError(null)
    setDurationBlock(null)
    // PR-A part 2 §7: resolve the selected direction. Only send the language
    // fields for a NON-default pair, so default submissions stay byte-identical
    // to pre-i18n requests (zero-regression).
    const selectedPair =
      languageFacts.find((f) => f.pair_key === languagePairKey) ?? GA_DEFAULT_LANGUAGE_FACT
    // 只在「非默认方向 且 管线就绪」时才发送语言字段。未就绪（即将上线）的方向在
    // selector 里已 disabled，这里再加一道护栏：即使被强行选中也回落默认，与后端
    // 的 409 language_pair_not_yet_available 硬闸保持一致。
    const sendPair = !selectedPair.is_default && selectedPair.pipeline_ready
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
        smartPaidCloneConfirmed:
          serviceMode === "smart" && voiceCloneCostCredits != null
            ? smartPaidCloneAccepted
            : false,
        freeVoiceRightsConfirmed: serviceMode === "free" ? freeVoiceRightsConfirmed : false,
        sourceLanguage: sendPair ? selectedPair.source_language : undefined,
        targetLanguage: sendPair ? selectedPair.target_language : undefined,
        // D7：有认领预览时带 reuse_anonymous_preview_id，服务端用认领的完整原视频
        // 覆盖 source（走正常付费流程）。source 字段照常发送但被后端覆盖。
        reuseAnonPreviewId: reuseAnonPreviewId ?? undefined,
      })
      setActiveJobs((prev) => [createdJob, ...prev])
      setSubmitState("success")
      // 转完整成功 → 清 convert-ready key（避免返回创建页再次进入转完整模式）。
      if (reuseAnonPreviewId) clearAnonConvertReady()
      toast.success(`任务已创建：${getJobDisplayTitle(createdJob)}`)
      // Store latest job ID for /tasks/current fallback
      try { localStorage.setItem("avt_latest_job_id", createdJob.id) } catch {}
      onCreated({ id: createdJob.id, title: getJobDisplayTitle(createdJob) })
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await loadActiveJobs(true)
      }
      setSubmitState("error")
      // D7（CodeX P2）：转完整失败且是「预览不可复用」（认领过期/越权/源失效，
      // anon_preview_* 系列）→ 清转完整模式 + 提示重新上传，避免卡在用不了的认领来源。
      if (reuseAnonPreviewId && isAnonConvertRejected(error)) {
        setReuseAnonPreviewId(null)
        clearAnonConvertReady()
        toast.error("该预览已无法转完整（可能已过期），请重新上传视频创建。")
        return
      }
      // A 方案：转完整因原视频超套餐时长上限被拦 → 持久 banner。两档分流（CodeX P1）：
      // upgrade=升级可解决（→ /pricing CTA）；over_max=超过最高自助套餐、升级也没用
      // （只提示用更短视频 / 联系客服）。**保留**转完整模式：源有效，升级 / 换更短
      // 视频后可重试，不清 reuseAnonPreviewId / convert-ready key。
      const durationReason = readDurationBlockReason(error)
      if (durationReason) {
        // 后端 body.message 含具名套餐推荐（minimum_self_serve_plan_for）；这里 fallback
        // 仅在 message 缺失时兜底，故用**不具名**通用文案，避免再误导买某档（CodeX P1）。
        const fallback =
          durationReason === "over_max"
            ? "原视频时长超过当前最高套餐上限，请改用更短的视频，或联系客服。"
            : "原视频时长超出当前套餐上限，升级套餐即可处理更长视频。"
        setDurationBlock({
          message: readGatewayErrorMessage(error) ?? fallback,
          canUpgrade: durationReason === "upgrade",
        })
        return
      }
      const msg = getErrorMessage(error)
      if (isCreditGateError(error)) {
        setCreditGateError(msg)
      } else if (msg.includes("still active")) {
        toast.error("当前有未完成的任务，请先完成或取消后再创建新翻译。")
      } else {
        toast.error(msg)
      }
    } finally {
      submittingRef.current = false // 失败可重试；成功已 onCreated 跳转（组件卸载）
    }
  }

  // P3e-4c：打开智能版 3 分钟预览的预扣确认弹窗。校验源（与主提交同一道
  // sourceValidationError），构造与普通创建一致的共享 job 配置交给弹窗；smart /
  // preview / 克隆具体项由 createSmartPreviewJob 强制（前端只送共享配置）。
  function openSmartPreview() {
    if (sourceValidationError) {
      toast.error(sourceValidationError)
      return
    }
    const selectedPair =
      languageFacts.find((f) => f.pair_key === languagePairKey) ?? GA_DEFAULT_LANGUAGE_FACT
    const sendPair = !selectedPair.is_default && selectedPair.pipeline_ready
    setSmartPreviewInput({
      speakers,
      youtubeUrl: sourceType === "youtube_url" ? youtubeUrl.trim() : "",
      sourceType,
      localFilePath: sourceType === "local_video" ? uploadedFilePath : undefined,
      localFileName: sourceType === "local_video" ? (uploadFileName || undefined) : undefined,
      transcriptionMethod: sourceType === "local_video" ? "assemblyai" : transcriptionMethod,
      service_mode: "smart",
      sourceLanguage: sendPair ? selectedPair.source_language : undefined,
      targetLanguage: sendPair ? selectedPair.target_language : undefined,
    })
    setSmartPreviewOpen(true)
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
          {/* D7 转完整：认领来源 banner（有认领预览时替代源选择 + 上传输入） */}
          {reuseAnonPreviewId ? (
            <div
              className="rounded-xl border p-4"
              style={{
                backgroundColor: "color-mix(in oklab, var(--bamboo) 8%, transparent)",
                borderColor: "color-mix(in oklab, var(--bamboo) 32%, transparent)",
              }}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="space-y-1">
                  <p className="text-sm font-medium text-foreground">使用你刚才预览的视频转完整版</p>
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    将用你预览过的完整原始视频生成正式成片，无需重新上传。下面选择方案后按所选档位正常扣点创建。
                  </p>
                </div>
                <button
                  type="button"
                  className="shrink-0 whitespace-nowrap text-xs text-muted-foreground transition hover:text-[color:var(--cinnabar)]"
                  onClick={() => {
                    setReuseAnonPreviewId(null)
                    clearAnonConvertReady()
                    // CodeX P3：换视频要清掉旧的时长 banner，否则残留误导。
                    setDurationBlock(null)
                  }}
                >
                  改用其它视频
                </button>
              </div>
            </div>
          ) : (
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
          )}

          {/* YouTube URL / 上传输入（转完整模式下隐藏，源由认领的预览视频提供） */}
          {reuseAnonPreviewId ? null : sourceType === "youtube_url" ? (
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
                      try {
                        // 选路（plan 2026-06-11 §3.9）：> 阈值且分片通道开启
                        // → 分片上传（绕过 CF 免费版 100MB 单请求体限制）；
                        // 否则走现有单请求路径（通道关闭时保持旧行为，
                        // 不在前端硬拦——非 CF 部署单请求仍可用）。
                        const thresholdBytes =
                          (chunkedLimits?.threshold_mb ?? 95) * 1024 * 1024
                        if (chunkedLimits?.enabled && file.size > thresholdBytes) {
                          const result = await uploadFileInChunks(file, chunkedLimits, (p) => {
                            if (p.phase === "hashing") {
                              setUploadProgress(`正在校验文件完整性… ${p.percent}%`)
                            } else if (p.phase === "uploading") {
                              setUploadProgress(`正在分片上传 ${file.name}… ${p.percent}%`)
                            } else {
                              // Q1 落定：合并阶段无百分比，但不能像卡死
                              setUploadProgress("正在合并校验…（大文件需要数十秒，请勿关闭页面）")
                            }
                          })
                          // opaque upload ref（chunked:{upload_id}），创建任务时
                          // 由 gateway 解析为服务端 final_path——前端不接触路径。
                          setUploadedFilePath(result.uploadRef)
                          setUploadFileName(file.name)
                          setUploadProgress("")
                          return
                        }
                        setUploadProgress(`正在上传 ${file.name}…`)
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
                        // 分片失败不自动回退单请求（大文件回 CF 单请求必 413，
                        // plan §3.9）——失败文案直接展示，用户可重选文件重试
                        // （同文件续传：init 四元组命中后按位图补传）。
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
                className={!expressAllowed ? planCardDisabledClass : serviceMode === "express" ? planCardSelectedClass : planCardIdleClass}
                style={expressAllowed && serviceMode === "express" ? selectedPlanStyle : undefined}
                disabled={!expressAllowed || isBlockedByConcurrency || submitState === "submitting"}
                onClick={() => {
                  if (expressAllowed) setServiceMode("express")
                }}
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
                  {!expressAllowed ? (
                    <span className="ml-auto rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
                      已下线
                    </span>
                  ) : null}
                </div>
                <p className="text-xs text-muted-foreground leading-relaxed">全自动流程，AI 识别说话人、翻译、配音，无需人工操作。</p>
                {serviceMode === "express" && (
                  <div className="absolute top-3 right-3 h-4 w-4 rounded-full bg-primary flex items-center justify-center">
                    <svg className="h-2.5 w-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}><path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" /></svg>
                  </div>
                )}
              </button>

              {/* Free mode — Phase 2a, gated by NEXT_PUBLIC_ENABLE_FREE_TIER */}
              {freeTierEnabled && (
                <button
                  type="button"
                  className={!freeAllowed ? planCardDisabledClass : serviceMode === "free" ? planCardSelectedClass : planCardIdleClass}
                  style={freeAllowed && serviceMode === "free" ? selectedPlanStyle : undefined}
                  disabled={!freeAllowed || isBlockedByConcurrency || submitState === "submitting"}
                  onClick={() => {
                    if (freeAllowed) setServiceMode("free")
                  }}
                >
                  <div className="flex items-center gap-2 mb-2">
                    <span className="text-sm font-semibold text-foreground">免费版</span>
                    <span
                      className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
                      style={{
                        backgroundColor: "color-mix(in oklab, var(--bamboo) 14%, transparent)",
                        color: "var(--bamboo)",
                        border: "1px solid color-mix(in oklab, var(--bamboo) 30%, transparent)",
                      }}
                      >
                        Free
                      </span>
                      {!freeAllowed ? (
                        <span className="ml-auto rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
                          已下线
                        </span>
                      ) : null}
                    </div>
                  <p className="text-xs text-muted-foreground leading-relaxed">免费保留原声 AI 配音（限时），每日 1 次、单条 ≤10 分钟，成品带水印。</p>
                  {serviceMode === "free" && (
                    <div className="absolute top-3 right-3 h-4 w-4 rounded-full bg-primary flex items-center justify-center">
                      <svg className="h-2.5 w-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}><path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" /></svg>
                    </div>
                  )}
                </button>
              )}

              {/* Studio mode — locked unless plan allows it */}
              {(() => {
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
                    {studioRolloutOffline ? (
                      <div className="absolute top-3 right-3 rounded-full bg-muted/50 px-2 py-0.5 text-[10px] text-muted-foreground">
                        已下线
                      </div>
                    ) : entitlements?.ui.allow_upgrade ? (
                      <Link
                        href="/settings/billing"
                        className="absolute top-3 right-3 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary transition-colors hover:bg-primary/20"
                      >
                        升级解锁
                      </Link>
                    ) : (
                      <div className="absolute top-3 right-3 rounded-full bg-muted/50 px-2 py-0.5 text-[10px] text-muted-foreground">
                        即将开放
                      </div>
                    )}
                  </div>
                )
              })()}

              {/* Smart mode — locked unless plan allows it. Smart MVP P2:
                * 100 credits/min fixed price, AI auto-decisions for translation
                * review + voice cloning. plan_catalog gates plus + pro. */}
              {(() => {
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
                ) : !smartRolloutOffline && smartPreviewEntryEnabled && !reuseAnonPreviewId ? (
                  // P3e-4c：免费 / 未获 smart 的登录用户的预览入口。点击校验源 →
                  // 打开预扣确认弹窗（付费克隆的用户显式触发面）。
                  // 转完整模式隐藏（用户已预览过，不再二次预览；源也不是 fresh upload）。
                  <div className="relative rounded-xl border border-primary/30 bg-primary/[0.05] p-4 text-left">
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
                        预览
                      </span>
                    </div>
                    <p className="text-xs text-muted-foreground leading-relaxed">
                      克隆主说话人音色，先看前 3 分钟带水印预览（预扣 {smartPreviewCloneCostLabel}）。满意再转完整成片，按分钟正常扣点。
                    </p>
                    <button
                      type="button"
                      disabled={isBlockedByConcurrency || submitState === "submitting"}
                      onClick={openSmartPreview}
                      className="mt-3 w-full rounded-lg bg-primary px-3 py-2 text-xs font-semibold text-white transition hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      试用 3 分钟预览
                    </button>
                  </div>
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
                    {smartRolloutOffline ? (
                      <div className="absolute top-3 right-3 rounded-full bg-muted/50 px-2 py-0.5 text-[10px] text-muted-foreground">
                        已下线
                      </div>
                    ) : entitlements?.ui.allow_upgrade ? (
                      <Link
                        href="/settings/billing"
                        className="absolute top-3 right-3 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary transition-colors hover:bg-primary/20"
                      >
                        升级解锁
                      </Link>
                    ) : (
                      <div className="absolute top-3 right-3 rounded-full bg-muted/50 px-2 py-0.5 text-[10px] text-muted-foreground">
                        即将开放
                      </div>
                    )}
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
            {serviceMode === "free" ? (
              <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                免费版当前不扣点（限时免费），保留原声 AI 配音。每日 1 次、单条 ≤10 分钟，成品视频带水印。后续如需「后编辑」或「剪映草稿」为付费 add-on，将另行计点。
              </p>
            ) : serviceMode === "express" ? (
              <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                快捷版按源视频时长扣点，当前标准为 {rateLabel}。创建任务时会按可识别的时长预扣；点数不足会停止创建，可前往账单页升级套餐。
              </p>
            ) : serviceMode === "smart" ? (
              <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                智能版按源视频时长扣点，当前基础标准为 {rateLabel}。AI 自动审核翻译并按需处理主说话人音色；若需要自动新克隆音色，会额外预扣 {voiceCloneCostLabel}，未发生新克隆时会释放。当前阶段限制：主说话人不超过 3 位；若不满足条件会自动降级到工作台版或退点。
              </p>
            ) : (
              <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                工作台版按源视频时长扣点，基础标准为 {rateLabel}；后续选择高级/旗舰音质时分别按 {creditRates.studioHigh ?? "读取中"} / {creditRates.studioFlagship ?? "读取中"} 点/分钟扣除。音色克隆为单次独立扣点，克隆弹窗会再次确认费用。
              </p>
            )}
          </section>

          {/* Phase 2a LAUNCH GATE: free voice-rights attestation (《民法典》1023).
              The free voiceclone reproduces the source speaker's voice; the user
              must confirm they hold the rights. The backend HARD-fails (403
              consent_required) without it. NOTE: the wording below is a
              PLACEHOLDER pending legal sign-off (plan 2026-05-30-launch-gate §4.1). */}
          {serviceMode === "free" && freeTierEnabled ? (
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
                  checked={freeVoiceRightsConfirmed}
                  disabled={isBlockedByConcurrency || submitState === "submitting"}
                  onChange={(e) => setFreeVoiceRightsConfirmed(e.target.checked)}
                />
                <span className="block space-y-1.5">
                  <span className="block text-sm font-medium text-foreground">声音授权声明（必读必勾）</span>
                  <span className="block text-xs leading-relaxed text-muted-foreground">
                    我确认：我已获得该视频内容及其中所有说话人声音的合法授权，或该使用属于法律允许的范围；因使用本服务声音克隆功能产生的肖像权 / 声音权纠纷由我自行承担。
                  </span>
                </span>
              </label>
            </section>
          ) : null}

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

          {serviceMode === "smart" ? (
            <section
              className="rounded-xl border p-4"
              style={{
                backgroundColor: "color-mix(in oklab, var(--bamboo) 8%, transparent)",
                borderColor: "color-mix(in oklab, var(--bamboo) 32%, transparent)",
              }}
            >
              <label
                className={`flex items-start gap-3 ${
                  voiceCloneCostCredits == null ? "cursor-not-allowed opacity-70" : "cursor-pointer"
                }`}
              >
                <input
                  type="checkbox"
                  className="mt-1 h-4 w-4 shrink-0 accent-[color:var(--primary)]"
                  checked={smartPaidCloneAccepted}
                  disabled={
                    isBlockedByConcurrency ||
                    submitState === "submitting" ||
                    voiceCloneCostCredits == null
                  }
                  onChange={(e) => setSmartPaidCloneAccepted(e.target.checked)}
                />
                <span className="block space-y-1.5">
                  <span className="block text-sm font-medium text-foreground">确认智能版自动克隆扣点</span>
                  <span className="block text-xs leading-relaxed text-muted-foreground">
                    我确认：如果本次智能版需要自动新克隆主说话人音色，将额外预扣 {voiceCloneCostLabel}；未发生新克隆或任务未消耗该克隆时会释放。
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

          {/* Language direction (PR-A part 2 §7). Rendered only when the account
              has access to a non-default direction (内测); default-only users see
              no change at all (zero-regression UI). */}
          {languageFacts.length > 1 ? (
            <div className="space-y-2">
              <span className="text-xs font-medium text-muted-foreground block">语言方向</span>
              <div className="group rounded-xl border border-border bg-muted/30 transition hover:border-primary/30 focus-within:border-primary/40">
                <select
                  className="w-full rounded-xl bg-transparent px-4 py-3 text-sm text-foreground focus:outline-none input-focus-ring"
                  value={languagePairKey}
                  onChange={(e) => setLanguagePairKey(e.target.value)}
                  disabled={isBlockedByConcurrency || submitState === "submitting"}
                >
                  {languageFacts.map((f) => (
                    <option key={f.pair_key} value={f.pair_key} disabled={!f.pipeline_ready}>
                      {f.label}
                      {f.is_default ? "" : f.pipeline_ready ? "（内测）" : "（即将上线）"}
                    </option>
                  ))}
                </select>
              </div>
            </div>
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

          {creditGateError && (
            <section className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <p className="text-sm leading-relaxed text-foreground">{creditGateError}</p>
                <Link
                  href="/settings/billing"
                  className="inline-flex h-9 shrink-0 items-center justify-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
                >
                  去升级
                </Link>
              </div>
            </section>
          )}

          {durationBlock && (
            <section className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <p className="text-sm leading-relaxed text-foreground">{durationBlock.message}</p>
                {durationBlock.canUpgrade ? (
                  <Link
                    href="/pricing"
                    className="inline-flex h-9 shrink-0 items-center justify-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
                  >
                    升级套餐
                  </Link>
                ) : null}
              </div>
            </section>
          )}

          <button
            type="submit"
            disabled={Boolean(validationError) || isBlockedByConcurrency || submitState === "submitting" || isLoadingGuard}
            className="inline-flex w-full items-center justify-center gap-2 rounded-[var(--radius)] border border-transparent bg-gradient-to-r from-primary to-primary/80 px-6 py-2.5 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110 disabled:cursor-not-allowed disabled:border-border disabled:bg-muted disabled:bg-none disabled:text-muted-foreground disabled:shadow-none disabled:hover:brightness-100 disabled:hover:shadow-none sm:w-auto"
          >
            {submitState === "submitting" ? "创建中…" : "创建任务"}
          </button>
        </form>
      </section>

      {smartPreviewEntryEnabled ? (
        <SmartPreviewConfirmDialog
          open={smartPreviewOpen}
          onOpenChange={setSmartPreviewOpen}
          jobInput={smartPreviewInput}
          availableCredits={credits?.total_available ?? null}
          cloneCostCredits={smartPreviewCloneCostCredits}
          cloneCostLoadFailed={voiceCloneCostLoadFailed}
          onCreated={onCreated}
        />
      ) : null}
    </div>
  )
}

/* ---------- internal helpers ---------- */

// Gateway 扣费门错误码（402/403 top-level error 或 detail.error_code），命中时改走持久 banner。
const CREDIT_GATE_ERROR_CODES = new Set([
  "insufficient_credits",
  "quota_exhausted",
  "free_daily_quota_exceeded",
])

function isCreditGateError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  if (!error.payload || typeof error.payload !== "object") return false
  const payload = error.payload as { detail?: unknown; error?: unknown }
  const topLevelCode = payload.error
  if (typeof topLevelCode === "string" && CREDIT_GATE_ERROR_CODES.has(topLevelCode)) {
    return true
  }
  const detail = payload.detail
  if (!detail || typeof detail !== "object") return false
  const code = (detail as { error_code?: unknown }).error_code
  return typeof code === "string" && CREDIT_GATE_ERROR_CODES.has(code)
}

// D7：「预览不可复用」错误（gateway _error_response 把 code 放 body.error）。
// anon_preview_not_found/forbidden/source_unavailable → 认领过期/越权/源失效 →
// 转完整模式应清除并提示重新上传。
function isAnonConvertRejected(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  if (!error.payload || typeof error.payload !== "object") return false
  const code = (error.payload as { error?: unknown }).error
  return typeof code === "string" && code.startsWith("anon_preview")
}

// A 方案（转化漏斗 UX）：转完整时原视频超套餐时长上限 → gateway pre-flight 闸返回
// 两档可区分 reason（_error_response 把 code 放 body.error、文案放 body.message）：
//   - duration_upgrade_required（≤ 最高自助套餐，升级可解决）→ "upgrade"（给 /pricing）
//   - duration_over_max_plan（超过最高自助套餐，升级也没用）→ "over_max"（不给 /pricing）
function readDurationBlockReason(error: unknown): "upgrade" | "over_max" | null {
  if (!(error instanceof ApiError)) return null
  if (!error.payload || typeof error.payload !== "object") return null
  const code = (error.payload as { error?: unknown }).error
  if (code === "duration_upgrade_required") return "upgrade"
  if (code === "duration_over_max_plan") return "over_max"
  return null
}

// gateway _error_response 的 body.message（友好文案，含具体分钟数 / cap）。
function readGatewayErrorMessage(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null
  if (!error.payload || typeof error.payload !== "object") return null
  const msg = (error.payload as { message?: unknown }).message
  return typeof msg === "string" && msg.trim() ? msg : null
}

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
