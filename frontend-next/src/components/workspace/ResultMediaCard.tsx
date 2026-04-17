"use client"

import { useEffect, useState } from "react"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import {
  buildStreamUrl,
  buildResultDownloadUrl,
  buildTaskDownloadUrl,
  computeParamsFingerprint,
  fetchMaterialsAvailability,
  type MaterialsAvailability,
} from "@/lib/api/downloads"
import { useBackgroundTask } from "@/lib/react/useBackgroundTask"
import { Download, Video, Music, Package, X, Film, Loader2, RotateCcw, CheckCircle2 } from "lucide-react"
import { toast } from "sonner"

interface ResultMediaCardProps {
  jobId: string
}

const MATERIAL_OPTIONS = [
  { key: "source_video", label: "原始视频" },
  { key: "dubbed_video", label: "完整中文视频" },
  { key: "dubbed_audio", label: "完整中文音频" },
  { key: "segments", label: "分段音频包" },
  { key: "subtitles", label: "字幕包（中/英/双语）" },
] as const

type MaterialItemKey = (typeof MATERIAL_OPTIONS)[number]["key"]

export function ResultMediaCard({ jobId }: ResultMediaCardProps) {
  const [availability, setAvailability] = useState<MaterialsAvailability | null>(null)
  const [loading, setLoading] = useState(true)
  const [showPackDialog, setShowPackDialog] = useState(false)
  const [selectedItems, setSelectedItems] = useState<Set<MaterialItemKey>>(new Set())
  const [packFingerprint, setPackFingerprint] = useState<string | undefined>(undefined)

  useEffect(() => {
    fetchMaterialsAvailability(jobId)
      .then((data) => {
        setAvailability(data)
        const available = new Set<MaterialItemKey>()
        for (const opt of MATERIAL_OPTIONS) {
          if (isItemAvailable(opt.key, data)) available.add(opt.key)
        }
        setSelectedItems(available)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [jobId])

  // Recompute fingerprint whenever selection changes. The empty-selection
  // case resolves to `undefined` via the async branch to keep setState
  // out of the effect's synchronous body.
  useEffect(() => {
    const items = Array.from(selectedItems).sort()
    let cancelled = false
    const promise = items.length === 0
      ? Promise.resolve<string | undefined>(undefined)
      : computeParamsFingerprint({ items }).catch(() => undefined)
    promise.then((fp) => {
      if (!cancelled) setPackFingerprint(fp)
    })
    return () => {
      cancelled = true
    }
  }, [selectedItems])

  // Async tasks: materials pack + video generation
  const packTask = useBackgroundTask({
    jobId,
    taskType: "materials_pack",
    paramsFingerprint: packFingerprint,
    pollIntervalMs: 2500,
  })

  const videoTask = useBackgroundTask({
    jobId,
    taskType: "generate_video",
    // generate_video has empty params; stable fingerprint of "{}"
    paramsFingerprint: EMPTY_FINGERPRINT,
    pollIntervalMs: 4000,
  })

  // When video task finishes, refresh availability so the player appears
  useEffect(() => {
    if (videoTask.status === "completed") {
      fetchMaterialsAvailability(jobId).then(setAvailability).catch(() => {})
    }
  }, [videoTask.status, jobId])

  if (loading) {
    return (
      <Card>
        <CardContent className="pt-6">
          <div className="animate-pulse space-y-3">
            <div className="aspect-video rounded bg-muted" />
            <div className="h-8 w-40 rounded bg-muted" />
          </div>
        </CardContent>
      </Card>
    )
  }

  const hasVideo = availability?.dubbed_video ?? false
  const hasAudio = availability?.dubbed_audio ?? false
  const videoDownloadUrl = buildResultDownloadUrl({ downloadKey: "publish.dubbed_video", jobId })
  const audioDownloadUrl = buildResultDownloadUrl({ downloadKey: "editor.dubbed_audio_complete", jobId })

  async function handleStartPack() {
    if (selectedItems.size === 0) {
      toast.error("请至少选择一项")
      return
    }
    const items = Array.from(selectedItems).sort()
    const id = await packTask.startTask({ items })
    if (id) {
      toast.success("素材包正在打包，完成后自动可下载")
      setShowPackDialog(false)
    } else if (packTask.error) {
      toast.error(packTask.error)
    }
  }

  function handleDownloadPack() {
    if (!packTask.taskId) return
    const a = document.createElement("a")
    a.href = buildTaskDownloadUrl(jobId, packTask.taskId)
    a.style.display = "none"
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
  }

  async function handleStartVideo() {
    await videoTask.startTask({})
  }

  function toggleItem(key: MaterialItemKey) {
    setSelectedItems((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <Card>
      <CardContent className="pt-4">
        <div className="space-y-3">
          {/* Player area */}
          <div className="min-w-0">
            {hasVideo ? (
              <video
                className="w-full rounded-lg bg-black aspect-video"
                controls
                preload="metadata"
                src={buildStreamUrl(jobId, "video")}
              />
            ) : hasAudio ? (
              <div className="flex flex-col items-center justify-center rounded-lg bg-muted/30 border border-dashed border-border aspect-video gap-3">
                <Film className="h-10 w-10 text-muted-foreground/50" />
                <VideoGenerationControl
                  status={videoTask.status}
                  progress={videoTask.progress}
                  error={videoTask.error}
                  stalled={videoTask.stalled}
                  onStart={handleStartVideo}
                />
              </div>
            ) : null}
          </div>

          {/* Download buttons */}
          <div className="flex flex-wrap gap-2">
            {hasVideo && videoDownloadUrl && (
              <a href={videoDownloadUrl} download>
                <Button variant="outline" size="sm" className="gap-2">
                  <Video className="h-4 w-4" />
                  配音视频
                  <Download className="h-3 w-3" />
                </Button>
              </a>
            )}
            {hasAudio && audioDownloadUrl && (
              <a href={audioDownloadUrl} download>
                <Button variant="outline" size="sm" className="gap-2">
                  <Music className="h-4 w-4" />
                  配音音频
                  <Download className="h-3 w-3" />
                </Button>
              </a>
            )}
            <MaterialsPackButton
              task={packTask}
              onOpenDialog={() => setShowPackDialog(true)}
              onDownload={handleDownloadPack}
            />
          </div>
        </div>

        {showPackDialog && (
          <div className="mt-4 rounded-lg border border-border bg-card p-4 space-y-3">
            <div className="flex items-center justify-between">
              <h4 className="text-sm font-medium">选择素材</h4>
              <button onClick={() => setShowPackDialog(false)} className="text-muted-foreground hover:text-foreground">
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="space-y-2">
              {MATERIAL_OPTIONS.map((opt) => {
                const available = isItemAvailable(opt.key, availability)
                return (
                  <label
                    key={opt.key}
                    className={`flex items-center gap-2 text-sm ${available ? "cursor-pointer" : "opacity-40 cursor-not-allowed"}`}
                  >
                    <input
                      type="checkbox"
                      disabled={!available}
                      checked={selectedItems.has(opt.key)}
                      onChange={() => available && toggleItem(opt.key)}
                      className="rounded"
                    />
                    {opt.label}
                    {!available && <span className="text-xs text-muted-foreground">(不可用)</span>}
                  </label>
                )
              })}
            </div>
            <Button
              size="sm"
              onClick={handleStartPack}
              disabled={selectedItems.size === 0 || packTask.isActive}
            >
              {packTask.isActive ? "打包中..." : "开始打包"}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

// Fingerprint of empty params object `{}` (matches canonicalJson + sha256)
// Computed once at module load.
const EMPTY_FINGERPRINT =
  "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"

function MaterialsPackButton({
  task,
  onOpenDialog,
  onDownload,
}: {
  task: ReturnType<typeof useBackgroundTask>
  onOpenDialog: () => void
  onDownload: () => void
}) {
  if (task.status === "completed") {
    return (
      <Button
        variant="outline"
        size="sm"
        className="gap-2 border-emerald-500/60 text-emerald-500 hover:bg-emerald-500/10"
        onClick={onDownload}
      >
        <CheckCircle2 className="h-4 w-4" />
        素材包可下载
        <Download className="ml-1 h-3 w-3" />
      </Button>
    )
  }
  if (task.isActive) {
    const pct = typeof task.progress?.percent === "number" ? task.progress.percent : null
    return (
      <Button variant="outline" size="sm" className="gap-2" disabled>
        <Loader2 className="h-4 w-4 animate-spin" />
        {pct !== null ? `素材打包中 ${pct}%` : "素材打包中..."}
      </Button>
    )
  }
  if (task.status === "failed") {
    return (
      <Button
        variant="outline"
        size="sm"
        className="gap-2 border-destructive/60 text-destructive"
        onClick={onOpenDialog}
      >
        <RotateCcw className="h-4 w-4" />
        打包失败 · 重试
      </Button>
    )
  }
  return (
    <Button variant="outline" size="sm" className="gap-2" onClick={onOpenDialog}>
      <Package className="h-4 w-4" />
      素材包
      <Download className="ml-auto h-3 w-3" />
    </Button>
  )
}

function VideoGenerationControl({
  status,
  progress,
  error,
  stalled,
  onStart,
}: {
  status: string
  progress: ReturnType<typeof useBackgroundTask>["progress"]
  error: string | null
  stalled: boolean
  onStart: () => void
}) {
  if (status === "pending" || status === "running") {
    const stage = typeof progress?.stage === "string" ? progress.stage : ""
    const pct = typeof progress?.percent === "number" ? progress.percent : 0
    const label = stageLabel(stage)
    return (
      <div className="flex flex-col items-center gap-2">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          {label} · {pct}%
        </div>
        <div className="w-48 h-1.5 rounded-full bg-muted overflow-hidden">
          <div
            className="h-full bg-primary transition-all"
            style={{ width: `${Math.max(5, pct)}%` }}
          />
        </div>
        {stalled && (
          <p className="text-xs text-amber-500">任务耗时较长，仍在进行中</p>
        )}
      </div>
    )
  }
  if (status === "failed") {
    return (
      <div className="flex flex-col items-center gap-2">
        <p className="text-sm text-destructive">{error || "视频生成失败"}</p>
        <Button size="sm" variant="outline" onClick={onStart}>
          <RotateCcw className="h-3.5 w-3.5 mr-1" />
          重试
        </Button>
      </div>
    )
  }
  return (
    <>
      <p className="text-sm text-muted-foreground">暂未生成视频</p>
      <Button size="sm" variant="outline" onClick={onStart}>
        <Video className="h-3.5 w-3.5 mr-1" />
        生成视频
      </Button>
    </>
  )
}

function stageLabel(stage: string): string {
  switch (stage) {
    case "starting":
      return "正在准备"
    case "muxing":
      return "正在合成"
    case "finalizing":
      return "正在收尾"
    case "done":
      return "完成"
    default:
      return "处理中"
  }
}

function isItemAvailable(key: MaterialItemKey | string, availability: MaterialsAvailability | null): boolean {
  if (!availability) return false
  switch (key) {
    case "source_video": return availability.source_video
    case "dubbed_video": return availability.dubbed_video
    case "dubbed_audio": return availability.dubbed_audio
    case "segments": return availability.segments
    case "subtitles": return availability.subtitles_zh || availability.subtitles_en || availability.subtitles_bilingual
    default: return false
  }
}
