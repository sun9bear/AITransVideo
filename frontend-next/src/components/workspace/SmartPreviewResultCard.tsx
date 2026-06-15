"use client"

import { useState } from "react"
import Link from "next/link"
import { useRouter } from "next/navigation"
import { toast } from "sonner"
import { Sparkles, ArrowRight, Loader2 } from "lucide-react"

import { Button } from "@/components/ui/button"
import { buildStreamUrl } from "@/lib/api/downloads"
import {
  convertPreviewToFull,
  mapSmartPreviewReuseError,
  SMART_PREVIEW_CLONE_CREDITS,
} from "@/lib/api/smartPreviewClone"
import type { CreateTranslationJobInput, JobSummary } from "@/types/jobs"

/**
 * 智能版 3 分钟预览结果面（P3e-4c 切片 4/5）.
 *
 * 仅对 ``job.smartPreviewMode === true`` 的预览任务渲染（结果页据此分支，
 * 取代普通 ResultMediaCard + 下载列表）。预览是 **stream-only**：后端把
 * download / materials / jianying / edit / export 全 403（P3e-3d/4a/4a-2），
 * 所以这里**只**给在线 teaser 播放器 + 「转完整」CTA，绝不渲染任何下载 / 导出口。
 *
 * teaser 视频走与正式成片相同的 stream 端点（``buildStreamUrl(job.id, "video")``）——
 * 后端对预览任务在该端点回的是 3 分钟带水印片段（P3e-3b）。
 *
 * 「转完整」走 ``convertPreviewToFull``（只发 reuse_preview_job_id；源 / 音色 /
 * consent 由服务端从预览任务派生）。免费 plan 缺 smart 的用户会被决策 A 拦为
 * ``smart_upgrade_required`` → 这里渲染「升级 Plus / Pro」CTA，而非死路。
 */
interface SmartPreviewResultCardProps {
  job: JobSummary
}

export function SmartPreviewResultCard({ job }: SmartPreviewResultCardProps) {
  const router = useRouter()
  const [started, setStarted] = useState(false)
  const [playerError, setPlayerError] = useState<string | null>(null)
  const [converting, setConverting] = useState(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [upgradeRequired, setUpgradeRequired] = useState(false)

  const posterUrl = buildStreamUrl(job.id, "poster")
  const videoUrl = buildStreamUrl(job.id, "video")

  /**
   * 由预览任务的 JobSummary 构造转完整入参。source / voice / consent **会被服务端
   * 从被复用的预览任务覆盖**（job_intercept reuse override，发生在任何源校验之前），
   * 故此处 source 仅占位；语向不被 override，需透传以保持同一翻译方向。
   */
  function buildReuseInput(): CreateTranslationJobInput {
    const sourceType: "youtube_url" | "local_video" =
      job.sourceType === "local_video" ? "local_video" : "youtube_url"
    return {
      youtubeUrl: sourceType === "youtube_url" ? job.sourceRef : "",
      sourceType,
      localFilePath: sourceType === "local_video" ? job.sourceRef : undefined,
      speakers: job.speakers,
      // 音色由服务端从被复用的预览任务派生，转完整不送（convertPreviewToFull 亦显式剔除）。
      voiceA: undefined,
      voiceB: undefined,
      sourceLanguage: job.sourceLanguage ?? undefined,
      targetLanguage: job.targetLanguage ?? undefined,
      service_mode: "smart",
    }
  }

  async function handleConvert() {
    if (converting) return
    setConverting(true)
    setErrorMessage(null)
    setUpgradeRequired(false)
    try {
      const fullJob = await convertPreviewToFull(buildReuseInput(), job.id)
      toast.success("正在转完整成片，按分钟正常扣点…")
      router.push(`/workspace/${fullJob.id}`)
    } catch (error) {
      const mapped = mapSmartPreviewReuseError(error)
      setErrorMessage(mapped.message)
      setUpgradeRequired(mapped.reason === "upgrade_required")
    } finally {
      setConverting(false)
    }
  }

  return (
    <section className="surface-card overflow-hidden border border-primary/30">
      {/* Header banner */}
      <div className="flex items-center gap-2 border-b border-border bg-primary/[0.06] px-5 py-3">
        <Sparkles className="h-4 w-4 text-primary" />
        <span className="text-sm font-semibold text-foreground">智能版 · 3 分钟预览</span>
        <span className="ml-auto rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
          带水印 · 仅在线播放
        </span>
      </div>

      <div className="space-y-4 p-5">
        {/* Teaser player — 与正式成片同 stream 端点；预览任务后端回 3min 带水印片段。
            点击前只加载 poster（省带宽），点击后才拉视频流。 */}
        <div
          className="canvas-texture relative rounded-lg p-1.5"
          style={{
            border: "1px solid var(--border)",
            outline: "1px solid color-mix(in oklab, var(--cinnabar) 25%, transparent)",
            outlineOffset: "3px",
            boxShadow: "inset 0 0 48px var(--gallery-vignette, rgba(0, 0, 0, 0.18))",
          }}
        >
          {started ? (
            <video
              className="w-full rounded-lg bg-black aspect-video"
              controls
              autoPlay
              playsInline
              preload="metadata"
              poster={posterUrl}
              src={videoUrl}
              onError={() => {
                // 流式拉取失败（产物未落盘 / 网络中断 / 瞬时 404）→ 退回可点击的
                // poster 态并给出可恢复提示，避免卡在坏掉的播放器控件上。
                setStarted(false)
                setPlayerError("预览加载失败，请稍后重试。")
              }}
            />
          ) : (
            <button
              type="button"
              onClick={() => {
                setPlayerError(null)
                setStarted(true)
              }}
              className="group relative aspect-video w-full overflow-hidden rounded-lg bg-muted"
              aria-label="播放 3 分钟预览"
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={posterUrl}
                alt=""
                loading="lazy"
                className="h-full w-full object-cover"
                onError={(e) => {
                  ;(e.currentTarget as HTMLImageElement).style.display = "none"
                }}
              />
              <div className="absolute inset-0 flex items-center justify-center bg-black/30 transition group-hover:bg-black/20">
                <div className="flex h-14 w-14 items-center justify-center rounded-full bg-white/90 transition group-hover:scale-110">
                  <svg viewBox="0 0 24 24" className="ml-1 h-7 w-7 text-black" fill="currentColor">
                    <path d="M8 5v14l11-7z" />
                  </svg>
                </div>
              </div>
            </button>
          )}
        </div>

        {playerError ? (
          <p className="text-xs leading-relaxed text-[color:var(--cinnabar)]">{playerError}</p>
        ) : null}

        <p className="text-xs leading-relaxed text-muted-foreground">
          这是用克隆音色生成的前 3 分钟带水印预览，仅供在线试看，不提供下载 / 导出 / 修改。满意后可转完整成片，去掉水印、生成全长内容。
        </p>

        {/* 转完整 CTA */}
        <div className="rounded-lg border border-border bg-muted/30 p-4">
          {upgradeRequired ? (
            <div className="space-y-3">
              <p className="text-sm leading-relaxed text-foreground">
                {errorMessage ??
                  "转完整智能版需升级到 Plus / Pro 套餐后再试。复用不会重复扣除预览已支付的克隆费用。"}
              </p>
              <Link
                href="/pricing"
                className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:bg-primary/90"
              >
                升级 Plus / Pro
                <ArrowRight className="h-4 w-4" />
              </Link>
            </div>
          ) : (
            <div className="space-y-2.5">
              <p className="text-sm font-medium text-foreground">满意这个效果？</p>
              <p className="text-xs leading-relaxed text-muted-foreground">
                转完整成片将复用同一视频与已克隆音色，按分钟正常扣点，不再重复扣预览的{" "}
                {SMART_PREVIEW_CLONE_CREDITS} 点克隆费。
              </p>
              {errorMessage ? (
                <p className="text-xs leading-relaxed text-[color:var(--cinnabar)]">{errorMessage}</p>
              ) : null}
              <Button onClick={handleConvert} disabled={converting} className="gap-2">
                {converting ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    提交中…
                  </>
                ) : (
                  <>
                    转完整成片
                    <ArrowRight className="h-4 w-4" />
                  </>
                )}
              </Button>
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
