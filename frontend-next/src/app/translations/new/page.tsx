"use client"

import { useEffect, useState, type FormEvent } from "react"
import { useRouter } from "next/navigation"
import { toast } from "sonner"

import { StatusBadge } from "@/components/status-badge"
import { getJobDisplayTitle, getStageLabel } from "@/features/jobs/presentation"
import { ApiError } from "@/lib/api/client"
import { getErrorMessage } from '@/lib/api/errors'
import { estimateCosts, formatCostCny } from "@/lib/cost/estimator"
import { getCurrentJob, submitTranslationJob } from "@/lib/api/jobs"
import { getVoiceLibrary, type VoiceLibraryEntry } from "@/lib/api/voiceLibrary"
import { usePollingTask } from "@/lib/react/usePollingTask"
import type { JobSummary } from "@/types/jobs"

export default function NewTranslationPage() {
  const router = useRouter()
  const [sourceType, setSourceType] = useState<"youtube_url" | "local_file">("youtube_url")
  const [youtubeUrl, setYoutubeUrl] = useState("")
  const [uploadedFilePath, setUploadedFilePath] = useState("")
  const [uploadFileName, setUploadFileName] = useState("")
  const [isUploading, setIsUploading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState("")
  const [speakers, setSpeakers] = useState<"1" | "2" | "auto">("auto")
  const [transcriptionMethod, setTranscriptionMethod] = useState<"assemblyai" | "gemini">("assemblyai")
  const [savedVoices, setSavedVoices] = useState<VoiceLibraryEntry[]>([])
  const [activeJob, setActiveJob] = useState<JobSummary | null>(null)
  const [isLoadingGuard, setIsLoadingGuard] = useState(true)
  const [submitState, setSubmitState] = useState<"error" | "idle" | "submitting" | "success">("idle")

  const validationError =
    sourceType === "youtube_url"
      ? validateYoutubeUrl(youtubeUrl)
      : !uploadedFilePath
        ? "请先上传视频文件。"
        : null
  const isBlockedByActiveJob = Boolean(activeJob)

  const loadActiveJob = async (silent = false) => {
    if (!silent) setIsLoadingGuard(true)
    try {
      const nextJob = await getCurrentJob()
      setActiveJob(nextJob)
    } catch {
      // ignore
    } finally {
      setIsLoadingGuard(false)
    }
  }

  usePollingTask(() => loadActiveJob(!isLoadingGuard), { intervalMs: 5000 })

  useEffect(() => {
    getVoiceLibrary()
      .then((lib) => setSavedVoices(lib.voices))
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
        localFilePath: sourceType === "local_file" ? uploadedFilePath : undefined,
        transcriptionMethod: sourceType === "local_file" ? "assemblyai" : transcriptionMethod,
      })
      setActiveJob(createdJob)
      setSubmitState("success")
      toast.success(`任务已创建：${getJobDisplayTitle(createdJob)}`)
      router.push("/tasks/current")
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await loadActiveJob(true)
      }
      setSubmitState("error")
      const msg = getErrorMessage(error)
      if (msg.includes('still active')) {
        toast.error('当前有未完成的任务，请先完成或取消后再创建新翻译。')
      } else {
        toast.error(msg)
      }
    }
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold font-heading text-foreground">新建翻译</h1>
          <p className="text-sm text-muted-foreground mt-1">
            填写视频来源与参数，创建翻译配音任务。
          </p>
        </div>
        {activeJob ? <StatusBadge status={activeJob.status} /> : null}
      </div>

      {/* Active job guard */}
      {activeJob ? (
        <section className="rounded-2xl border border-amber-500/20 bg-amber-500/5 p-5">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="space-y-1">
              <p className="text-xs font-semibold text-amber-400">当前有未完成的任务</p>
              <p className="font-semibold text-foreground">请先完成或取消当前任务，再创建新的翻译。</p>
              <p className="text-sm text-muted-foreground">
                {getJobDisplayTitle(activeJob)} · {getStageLabel(activeJob.currentStage)}
              </p>
            </div>
            <div className="flex gap-2">
              <button
                className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-primary to-primary/80 px-5 py-2 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110"
                onClick={() => router.push(`/workspace/${activeJob.id}`)}
                type="button"
              >
                去处理当前任务
              </button>
              <button
                className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2 text-sm font-medium text-red-400 transition hover:bg-red-500/20"
                onClick={async () => {
                  if (!window.confirm('确定要取消当前任务吗？')) return
                  try {
                    const { cancelCurrentJob } = await import('@/lib/api/reviews')
                    await cancelCurrentJob()
                    setActiveJob(null)
                    toast.success('任务已取消，现在可以创建新任务。')
                  } catch { toast.error('取消失败，请稍后重试。') }
                }}
                type="button"
              >
                取消该任务
              </button>
            </div>
          </div>
        </section>
      ) : null}

      <div className="grid gap-6 xl:grid-cols-[1fr_320px]">
        {/* Form */}
        <section className="rounded-2xl border border-border bg-card p-6">
          <h2 className="text-lg font-semibold text-foreground mb-5">任务输入</h2>
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
                className={`rounded-lg px-4 py-2 text-sm font-medium transition ${sourceType === "local_file" ? "bg-primary text-white" : "border border-border bg-muted/30 text-muted-foreground hover:bg-muted/50"}`}
                onClick={() => setSourceType("local_file")}
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
                    disabled={isBlockedByActiveJob || submitState === "submitting"}
                  />
                </div>
                {validationError && youtubeUrl ? (
                  <p className="text-xs text-red-400">{validationError}</p>
                ) : null}
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
                      disabled={isBlockedByActiveJob || submitState === "submitting" || isUploading}
                      onChange={async (event) => {
                        const file = event.target.files?.[0]
                        if (!file) return
                        setIsUploading(true)
                        setUploadProgress(`正在上传 ${file.name}…`)
                        try {
                          const formData = new FormData()
                          formData.append("file", file)
                          const response = await fetch("/web-ui-api/api/upload-video", {
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

            {/* Options */}
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <span className="text-xs font-medium text-muted-foreground block">转录方案</span>
                <div className="group rounded-xl border border-border bg-muted/30 transition hover:border-primary/30 focus-within:border-primary/40">
                  <select
                    className="w-full rounded-xl bg-transparent px-4 py-3 text-sm text-foreground focus:outline-none input-focus-ring"
                    value={transcriptionMethod}
                    onChange={(e) => setTranscriptionMethod(e.target.value as "assemblyai" | "gemini")}
                    disabled={isBlockedByActiveJob || submitState === "submitting"}
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
                    onChange={(e) => setSpeakers(e.target.value as "1" | "2" | "auto")}
                    disabled={isBlockedByActiveJob || submitState === "submitting"}
                  >
                    <option value="auto">自动</option>
                    <option value="1">1 人</option>
                    <option value="2">2 人</option>
                  </select>
                </div>
              </div>
            </div>

            {savedVoices.length > 0 ? (
              <p className="text-xs text-muted-foreground/60">
                音色将在后续"音色确认"阶段配置，可选择已有音色或克隆新音色。
              </p>
            ) : null}

            {/* 长视频提示 */}
            <div className="rounded-xl border border-blue-500/20 bg-blue-500/5 p-4 text-sm text-blue-400/80">
              <p className="font-medium text-blue-400 mb-1">处理时长参考</p>
              <ul className="space-y-1 text-xs">
                <li>10 分钟以内：约 5-15 分钟</li>
                <li>10-30 分钟：约 15-45 分钟</li>
                <li>30-60 分钟：约 1-2 小时（长视频，处理完成后将通知您）</li>
                <li>60 分钟以上：约 2-4 小时（超长视频，建议分段处理）</li>
              </ul>
              <p className="mt-2 text-xs text-muted-foreground">超过 3 小时的视频暂不支持，请裁剪后重试。</p>
            </div>

            <button
              type="submit"
              disabled={Boolean(validationError) || isBlockedByActiveJob || submitState === "submitting" || isLoadingGuard}
              className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-primary to-primary/80 px-6 py-2.5 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110 disabled:opacity-50"
            >
              {submitState === "submitting" ? "创建中…" : "创建任务"}
            </button>
          </form>
        </section>

        {/* Cost estimate */}
        <CostEstimatePanel transcriptionMethod={transcriptionMethod} />
      </div>
    </div>
  )
}

function CostEstimatePanel({ transcriptionMethod }: { transcriptionMethod: "assemblyai" | "gemini" }) {
  const estimates = [3, 10, 30]
  return (
    <section className="rounded-2xl border border-border bg-card p-5 h-fit">
      <h3 className="text-base font-semibold text-foreground">费用预估</h3>
      <p className="text-xs text-muted-foreground/60 mt-1">根据视频时长预估，仅供参考。</p>
      <div className="mt-4 space-y-3">
        {estimates.map((minutes) => {
          const result = estimateCosts({
            videoDurationMinutes: minutes,
            transcriptionMethod,
            needsVoiceClone: true,
            speakerCount: 1,
          })
          return (
            <div key={minutes} className="rounded-xl border border-border bg-muted/30 p-3 space-y-1 tabular-nums">
              <p className="text-sm font-semibold text-foreground/80">{minutes} 分钟视频</p>
              {result.stages.map((stage) => (
                <div key={stage.stage} className="flex justify-between text-xs text-muted-foreground">
                  <span>{stage.label}（{stage.model}）</span>
                  <span>{formatCostCny(stage.estimatedCostCny)}</span>
                </div>
              ))}
              <div className="h-px bg-muted/40 my-1" />
              <div className="flex justify-between text-sm font-semibold text-foreground/80">
                <span>预估总计</span>
                <span>{formatCostCny(result.totalCny)}</span>
              </div>
            </div>
          )
        })}
        <p className="text-xs text-muted-foreground/60">
          实际费用取决于视频内容和处理结果。已有音色可跳过克隆费用。
        </p>
      </div>
    </section>
  )
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
