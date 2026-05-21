"use client"

import { useEffect, useRef, useState } from "react"
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
import { buildBackendUrl, resolveJobApiBaseUrl } from "@/lib/api/config"
import {
  generateJianyingDraft,
  getJianyingDraftStatus,
  type JianyingDraftStatusResponse,
} from "@/lib/api/jobs"
import { JianyingDraftPathDialog } from "@/components/workspace/JianyingDraftPathDialog"
import { useBackgroundTask } from "@/lib/react/useBackgroundTask"
import { usePollingTask } from "@/lib/react/usePollingTask"
import { Download, Video, Music, Package, X, Film, Loader2, RotateCcw, CheckCircle2, RefreshCw, Clapperboard } from "lucide-react"
import Link from "next/link"
import { toast } from "sonner"

const RESULT_VIDEO_PLAY_EVENT = "aivt:result-video-play"

interface ResultMediaCardProps {
  jobId: string
  /**
   * Service mode — Express 隐藏"配音音频"和"素材包"按钮；保留视频播放器、
   * "配音视频"下载和"生成视频"fallback。Studio 完整 UI。
   * 见 docs/plans/2026-04-18-express-studio-output-filter-plan.md
   */
  serviceMode?: 'express' | 'studio' | 'smart'
  /**
   * When provided, renders a "修改" shortcut button at the right end of the
   * download row (ml-auto). Lets the projects-list card move 修改 out of the
   * cramped top header and into the bottom action row. Pass `null` / undefined
   * to hide. Plan D43 (post-edit feature flag gate enforced by caller).
   */
  editHref?: string
}

const MATERIAL_OPTIONS = [
  { key: "source_video", label: "原始视频" },
  { key: "dubbed_video", label: "完整中文视频" },
  { key: "dubbed_audio", label: "完整中文音频" },
  { key: "segments", label: "分段音频包" },
  { key: "subtitles", label: "字幕包（中/英/双语）" },
] as const

type MaterialItemKey = (typeof MATERIAL_OPTIONS)[number]["key"]

export function ResultMediaCard({ jobId, serviceMode, editHref }: ResultMediaCardProps) {
  const isExpress = serviceMode === 'express'
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
          // source_video 是原始素材里体积最大的（长视频动辄 1-2 GB），
          // 默认不勾减少触发 5 GB 上限的概率。用户需要时显式勾上即可。
          if (opt.key === "source_video") continue
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
              <LazyVideoPlayer jobId={jobId} />
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

          {/* Download buttons + (optional) 修改 shortcut on the right */}
          <div className="flex flex-wrap items-center gap-2">
            {hasVideo && videoDownloadUrl && (
              <a href={videoDownloadUrl} download>
                <Button variant="outline" size="sm" className="gap-2">
                  <Video className="h-4 w-4" />
                  配音视频
                  <Download className="h-3 w-3" />
                </Button>
              </a>
            )}
            {!isExpress && hasAudio && audioDownloadUrl && (
              <a href={audioDownloadUrl} download>
                <Button variant="outline" size="sm" className="gap-2">
                  <Music className="h-4 w-4" />
                  配音音频
                  <Download className="h-3 w-3" />
                </Button>
              </a>
            )}
            {!isExpress && (
              <MaterialsPackButton
                task={packTask}
                onOpenDialog={() => setShowPackDialog(true)}
                onDownload={handleDownloadPack}
              />
            )}
            {editHref && (
              // ml-auto pushes 修改 to the right edge of the download row.
              // Cinnabar tinted soft pill style — distinct from the gray
              // download buttons but quieter than a primary CTA.
              <Link
                href={editHref}
                className="ml-auto inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors"
                style={{
                  backgroundColor: "var(--cinnabar-soft, rgba(199,62,58,0.10))",
                  color: "var(--cinnabar, #C73E3A)",
                  border: "1px solid color-mix(in oklab, var(--cinnabar) 35%, transparent)",
                }}
                title="修改此任务"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                修改
              </Link>
            )}
          </div>

          {/* 剪映草稿区块 — 仅 Studio 任务可见 (plan §11.2.5) */}
          {!isExpress && (
            <JianyingDraftSection jobId={jobId} />
          )}
        </div>

        {!isExpress && showPackDialog && (
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
            {/* 2026-04-21: 磁盘上 zip 体积可达 GB 级，Gateway 每小时扫一次，
             *  超过 24h 的已完成 pack task 会被 cleanup_expired_pack_zips
             *  标记为 expired 并删除磁盘文件。前端应告知用户保留窗口。 */}
            <p className="text-xs text-muted-foreground">
              素材包仅保存 24 小时，请及时下载；超时后可重新打包，不额外扣点。
            </p>
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
  // 2026-04-21: Gateway periodic cleanup flips 24h-old completed tasks to
  // 'expired' and deletes the underlying zip. UI keeps the task card
  // visible (users may remember they packed before) but swaps the primary
  // CTA to a "re-pack" prompt so they understand re-clicking has no extra
  // cost beyond a fresh zip.
  if (task.status === "expired") {
    return (
      <Button
        variant="outline"
        size="sm"
        className="gap-2 border-amber-500/60 text-amber-500"
        onClick={onOpenDialog}
        title="素材包已过保留期（24 小时）被自动清理，重新打包不再扣点"
      >
        <RotateCcw className="h-4 w-4" />
        素材包已过期 · 重新打包不扣点
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

// ---------------------------------------------------------------------------
// 剪映草稿区块 — Studio-only, plan §11 (K7+K8)
// ---------------------------------------------------------------------------

const JIANYING_DEFAULT_STATE: JianyingDraftStatusResponse = {
  status: 'idle',
  started_at: null,
  completed_at: null,
  error: null,
  draft_zip_path: null,
  draft_zip_size_bytes: null,
  artifact_key: null,
}

/**
 * Format bytes to a human-readable string (MB, max 1 decimal).
 * Returns null for null/0 input.
 */
function formatBytes(bytes: number | null): string | null {
  if (bytes === null || bytes === 0) return null
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/**
 * Elapsed time in seconds since started_at. Returns null if not running/no start time.
 */
function elapsedSeconds(startedAt: string | null): number | null {
  if (!startedAt) return null
  const diff = Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000)
  return diff > 0 ? diff : null
}

/**
 * 剪映草稿区块 — visible only for Studio jobs (caller guards with !isExpress).
 *
 * Mount: fetches current status so returning users see succeeded/failed state.
 * Running: polls every 2.5s via usePollingTask (stops when terminal reached).
 * Click: calls generateJianyingDraft → transitions to running → starts polling.
 * Succeeded: shows download button with zip size hint.
 * Failed: shows error + retry button.
 */
function JianyingDraftSection({ jobId }: { jobId: string }) {
  const [draftState, setDraftState] = useState<JianyingDraftStatusResponse>(JIANYING_DEFAULT_STATE)
  const [triggering, setTriggering] = useState(false)
  const [elapsedSec, setElapsedSec] = useState<number | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [dialogError, setDialogError] = useState<string | undefined>(undefined)

  // Initial fetch on mount — recovers state when user re-opens the tab
  useEffect(() => {
    let cancelled = false
    getJianyingDraftStatus(jobId)
      .then((data) => {
        if (!cancelled) setDraftState(data)
      })
      .catch(() => {
        // 404 / network — treat as idle; don't surface an error to the user
        if (!cancelled) setDraftState(JIANYING_DEFAULT_STATE)
      })
    return () => {
      cancelled = true
    }
  }, [jobId])

  // Polling — only active while status is 'running'
  usePollingTask(
    async () => {
      try {
        const fresh = await getJianyingDraftStatus(jobId)
        setDraftState(fresh)
      } catch {
        // transient network error — keep last state, polling continues
      }
    },
    {
      enabled: draftState.status === 'running',
      immediate: false,
      intervalMs: 2500,
    },
  )

  // Elapsed-time ticker — updates every second while running
  useEffect(() => {
    if (draftState.status !== 'running') {
      setElapsedSec(null)
      return
    }
    const update = () => setElapsedSec(elapsedSeconds(draftState.started_at))
    update()
    const id = window.setInterval(update, 1000)
    return () => window.clearInterval(id)
  }, [draftState.status, draftState.started_at])

  const downloadUrl = buildBackendUrl(
    resolveJobApiBaseUrl(),
    `/jobs/${jobId}/download/editor.jianying_draft_zip`,
  )

  function handleGenerateClick() {
    setDialogError(undefined)
    setDialogOpen(true)
  }

  async function handleDialogConfirm(userDraftRoot: string) {
    if (triggering) return
    setTriggering(true)
    try {
      const res = await generateJianyingDraft(jobId, { userDraftRoot })
      // Success — close dialog and start polling
      setDialogOpen(false)
      setDialogError(undefined)
      // Merge returned state so polling starts immediately if running
      setDraftState((prev) => ({
        ...prev,
        status: res.status,
        started_at: res.started_at ?? prev.started_at,
        completed_at: res.completed_at ?? prev.completed_at,
        artifact_key: (res.artifact_key as 'editor.jianying_draft_zip' | null) ?? prev.artifact_key,
      }))
      if (res.status === 'succeeded') {
        // Server returned an existing zip — refresh full state for size hint
        getJianyingDraftStatus(jobId).then(setDraftState).catch(() => {})
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : '触发失败，请稍后重试'
      // Surface the error inside the dialog so the user can correct and retry.
      // Do NOT close the dialog on error.
      setDialogError(msg)
    } finally {
      setTriggering(false)
    }
  }

  const { status, error, draft_zip_size_bytes } = draftState
  const sizeHint = formatBytes(draft_zip_size_bytes)

  return (
    <div className="border-t border-border pt-3 mt-1">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground select-none flex items-center gap-1">
          <Clapperboard className="h-3.5 w-3.5" />
          剪映草稿
        </span>

        {/* idle */}
        {status === 'idle' && (
          <Button
            variant="outline"
            size="sm"
            className="gap-2"
            onClick={handleGenerateClick}
            disabled={triggering}
            title="生成可用剪映打开的草稿包，5-30 秒"
          >
            {triggering ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Clapperboard className="h-4 w-4" />
            )}
            生成剪映草稿
          </Button>
        )}

        {/* running */}
        {status === 'running' && (
          <Button variant="outline" size="sm" className="gap-2" disabled>
            <Loader2 className="h-4 w-4 animate-spin" />
            生成中
            {elapsedSec !== null && (
              <span className="text-muted-foreground text-xs">· {elapsedSec}s</span>
            )}
          </Button>
        )}

        {/* succeeded */}
        {status === 'succeeded' && (
          <div className="flex flex-col gap-0.5">
            <a href={downloadUrl} download>
              <Button
                variant="outline"
                size="sm"
                className="gap-2 border-emerald-500/60 text-emerald-500 hover:bg-emerald-500/10"
              >
                <CheckCircle2 className="h-4 w-4" />
                下载剪映草稿
                {sizeHint && (
                  <span className="text-xs text-muted-foreground">{sizeHint}</span>
                )}
                <Download className="h-3 w-3 ml-1" />
              </Button>
            </a>
            <p className="text-xs text-muted-foreground pl-0.5">
              下载后解压，将草稿文件夹放入剪映草稿目录，再在剪映中打开。
            </p>
          </div>
        )}

        {/* failed */}
        {status === 'failed' && (
          <div className="flex flex-col gap-1">
            <Button
              variant="outline"
              size="sm"
              className="gap-2 border-destructive/60 text-destructive"
              onClick={handleGenerateClick}
              disabled={triggering}
              title={error ?? undefined}
            >
              {triggering ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <RotateCcw className="h-4 w-4" />
              )}
              重试生成
            </Button>
            {error && (
              <p className="text-xs text-destructive/80 max-w-xs truncate pl-0.5" title={error}>
                {error}
              </p>
            )}
          </div>
        )}
      </div>

      <JianyingDraftPathDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        onConfirm={handleDialogConfirm}
        errorMessage={dialogError}
      />
    </div>
  )
}

/**
 * Lazy video player: shows a poster image (with native lazy-loading) until
 * clicked, then swaps to a <video> element. Saves bandwidth when many cards
 * are rendered on the main projects page — posters only load on scroll,
 * and video data never loads unless the user clicks play.
 */
function LazyVideoPlayer({ jobId }: { jobId: string }) {
  const [hasStarted, setHasStarted] = useState(false)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const posterUrl = buildStreamUrl(jobId, "poster")
  const videoUrl = buildStreamUrl(jobId, "video")

  useEffect(() => {
    const handleOtherVideoPlay = (event: Event) => {
      const activeJobId = (event as CustomEvent<{ jobId?: string }>).detail?.jobId
      if (activeJobId && activeJobId !== jobId) {
        videoRef.current?.pause()
      }
    }
    window.addEventListener(RESULT_VIDEO_PLAY_EVENT, handleOtherVideoPlay)
    return () => window.removeEventListener(RESULT_VIDEO_PLAY_EVENT, handleOtherVideoPlay)
  }, [jobId])

  const handlePlay = () => {
    window.dispatchEvent(
      new CustomEvent(RESULT_VIDEO_PLAY_EVENT, { detail: { jobId } }),
    )
  }

  if (hasStarted) {
    return (
      <video
        ref={videoRef}
        className="w-full rounded-lg bg-black aspect-video"
        controls
        autoPlay
        playsInline
        preload="metadata"
        poster={posterUrl}
        src={videoUrl}
        onPlay={handlePlay}
      />
    )
  }

  return (
    <button
      type="button"
      onClick={() => setHasStarted(true)}
      className="relative w-full aspect-video rounded-lg overflow-hidden bg-muted group"
      aria-label="播放视频"
    >
      {/* Native lazy-loading: browser only fetches image when it scrolls into view */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={posterUrl}
        alt=""
        loading="lazy"
        className="w-full h-full object-cover"
        onError={(e) => {
          // If poster missing (old job), hide image, keep dark background
          ;(e.currentTarget as HTMLImageElement).style.display = "none"
        }}
      />
      <div className="absolute inset-0 flex items-center justify-center bg-black/30 group-hover:bg-black/20 transition">
        <div className="flex h-14 w-14 items-center justify-center rounded-full bg-white/90 group-hover:scale-110 transition">
          <svg viewBox="0 0 24 24" className="h-7 w-7 text-black ml-1" fill="currentColor">
            <path d="M8 5v14l11-7z" />
          </svg>
        </div>
      </div>
    </button>
  )
}
