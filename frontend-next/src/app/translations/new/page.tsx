"use client"

import { useEffect, useState, type FormEvent } from "react"
import Link from "next/link"
import { useRouter } from "next/navigation"
import { toast } from "sonner"

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"

import { StatusBadge } from "@/components/status-badge"
import { getJobDisplayTitle, getStageLabel } from "@/features/jobs/presentation"
import { ApiError } from "@/lib/api/client"
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
          <h1 className="text-2xl font-bold font-heading">新建翻译</h1>
          <p className="text-sm text-muted-foreground mt-1">
            填写视频来源与参数，创建翻译配音任务。
          </p>
        </div>
        {activeJob ? <StatusBadge status={activeJob.status} /> : null}
      </div>

      {/* Active job guard */}
      {activeJob ? (
        <Card className="border-amber-500/20 bg-amber-500/5">
          <CardContent className="pt-6">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
              <div className="space-y-1">
                <p className="text-xs font-semibold text-amber-400">当前有未完成的任务</p>
                <p className="font-semibold">请先完成或取消当前任务，再创建新的翻译。</p>
                <p className="text-sm text-muted-foreground">
                  {getJobDisplayTitle(activeJob)} · {getStageLabel(activeJob.currentStage)}
                </p>
              </div>
              <div className="flex gap-2">
                <Button onClick={() => router.push(`/workspace/${activeJob.id}`)}>去处理当前任务</Button>
                <Button variant="outline" className="border-red-500/30 text-red-400 hover:bg-red-500/10" onClick={async () => {
                  if (!window.confirm('确定要取消当前任务吗？')) return
                  try {
                    const { cancelCurrentJob } = await import('@/lib/api/reviews')
                    await cancelCurrentJob()
                    setActiveJob(null)
                    toast.success('任务已取消，现在可以创建新任务。')
                  } catch { toast.error('取消失败，请稍后重试。') }
                }}>取消该任务</Button>
              </div>
            </div>
          </CardContent>
        </Card>
      ) : null}

      <div className="grid gap-6 xl:grid-cols-[1fr_320px]">
        {/* Form */}
        <Card>
          <CardHeader>
            <CardTitle>任务输入</CardTitle>
          </CardHeader>
          <CardContent>
            <form className="space-y-6" onSubmit={handleSubmit}>
              {/* Source type toggle */}
              <div className="flex gap-2">
                <Button
                  type="button"
                  variant={sourceType === "youtube_url" ? "default" : "outline"}
                  size="sm"
                  onClick={() => setSourceType("youtube_url")}
                >
                  YouTube 链接
                </Button>
                <Button
                  type="button"
                  variant={sourceType === "local_file" ? "default" : "outline"}
                  size="sm"
                  onClick={() => setSourceType("local_file")}
                >
                  上传视频
                </Button>
              </div>

              {/* YouTube URL input */}
              {sourceType === "youtube_url" ? (
                <div className="space-y-2">
                  <Label htmlFor="youtube-url">YouTube 链接</Label>
                  <Input
                    id="youtube-url"
                    type="url"
                    placeholder="https://www.youtube.com/watch?v=..."
                    value={youtubeUrl}
                    onChange={(e) => {
                      setYoutubeUrl(e.target.value)
                      if (submitState !== "idle") setSubmitState("idle")
                    }}
                    disabled={isBlockedByActiveJob || submitState === "submitting"}
                  />
                  {validationError ? (
                    <p className="text-sm text-destructive">{validationError}</p>
                  ) : null}
                </div>
              ) : (
                <div className="space-y-2">
                  <Label htmlFor="video-upload">选择视频文件</Label>
                  {uploadedFilePath ? (
                    <div className="flex items-center gap-3 rounded-lg border border-success/30 bg-success/10 px-4 py-3">
                      <span className="text-sm font-medium text-success">{uploadFileName}</span>
                      <button
                        className="text-xs text-muted-foreground hover:text-destructive"
                        onClick={() => { setUploadedFilePath(""); setUploadFileName("") }}
                        type="button"
                      >
                        移除
                      </button>
                    </div>
                  ) : (
                    <Input
                      id="video-upload"
                      type="file"
                      accept="video/*"
                      disabled={isBlockedByActiveJob || submitState === "submitting" || isUploading}
                      onChange={async (event) => {
                        const file = event.target.files?.[0]
                        if (!file) return
                        setIsUploading(true)
                        setUploadProgress(`正在上传 ${file.name}...`)
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
                  )}
                  {uploadProgress ? (
                    <p className="text-sm text-muted-foreground">{uploadProgress}</p>
                  ) : null}
                </div>
              )}

              <Separator />

              {/* Options */}
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label>转录方案</Label>
                  <Select
                    value={transcriptionMethod}
                    onValueChange={(v) => setTranscriptionMethod(v as "assemblyai" | "gemini")}
                    disabled={isBlockedByActiveJob || submitState === "submitting"}
                  >
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="assemblyai">AssemblyAI（音频上传）</SelectItem>
                      <SelectItem value="gemini">Gemini 多模态（≤30分钟）</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                <div className="space-y-2">
                  <Label>说话人数</Label>
                  <Select
                    value={speakers}
                    onValueChange={(v) => setSpeakers(v as "1" | "2" | "auto")}
                    disabled={isBlockedByActiveJob || submitState === "submitting"}
                  >
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="auto">自动</SelectItem>
                      <SelectItem value="1">1 人</SelectItem>
                      <SelectItem value="2">2 人</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>

              {savedVoices.length > 0 ? (
                <p className="text-sm text-muted-foreground">
                  音色将在后续"音色确认"阶段配置，可选择已有音色或克隆新音色。
                </p>
              ) : null}

              <Button
                type="submit"
                disabled={Boolean(validationError) || isBlockedByActiveJob || submitState === "submitting" || isLoadingGuard}
                className="w-full sm:w-auto bg-gradient-to-r from-violet-600 to-violet-500 hover:from-violet-500 hover:to-violet-400 text-white shadow-lg shadow-violet-500/25"
              >
                {submitState === "submitting" ? "创建中..." : "创建任务"}
              </Button>
            </form>
          </CardContent>
        </Card>

        {/* Cost estimate */}
        <CostEstimatePanel transcriptionMethod={transcriptionMethod} />
      </div>
    </div>
  )
}

function CostEstimatePanel({ transcriptionMethod }: { transcriptionMethod: "assemblyai" | "gemini" }) {
  const estimates = [3, 10, 30]
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">费用预估</CardTitle>
        <p className="text-xs text-muted-foreground">根据视频时长预估，仅供参考。</p>
      </CardHeader>
      <CardContent className="space-y-3">
        {estimates.map((minutes) => {
          const result = estimateCosts({
            videoDurationMinutes: minutes,
            transcriptionMethod,
            needsVoiceClone: true,
            speakerCount: 1,
          })
          return (
            <div key={minutes} className="rounded-lg border p-3 space-y-1">
              <p className="text-sm font-semibold">{minutes} 分钟视频</p>
              {result.stages.map((stage) => (
                <div key={stage.stage} className="flex justify-between text-xs text-muted-foreground">
                  <span>{stage.label}（{stage.model}）</span>
                  <span>{formatCostCny(stage.estimatedCostCny)}</span>
                </div>
              ))}
              <Separator className="my-1" />
              <div className="flex justify-between text-sm font-semibold">
                <span>预估总计</span>
                <span>{formatCostCny(result.totalCny)}</span>
              </div>
            </div>
          )
        })}
        <p className="text-xs text-muted-foreground">
          实际费用取决于视频内容和处理结果。已有音色可跳过克隆费用。
        </p>
      </CardContent>
    </Card>
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

function getErrorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return "请求失败，请稍后重试。"
}
