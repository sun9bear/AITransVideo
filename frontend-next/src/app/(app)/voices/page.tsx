"use client"

import { useCallback, useEffect, useState } from "react"
import { EmptyState } from "@/components/empty-state"
import {
  calibrateVoiceSpeed,
  getVoiceLibrary,
  type VoiceLibraryEntry,
  type VoiceLibrarySummary,
} from "@/lib/api/voiceLibrary"

export default function VoiceLibraryPage() {
  const [summary, setSummary] = useState<VoiceLibrarySummary | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  // Per-voice UI state. Use two dicts instead of a discriminated union
  // so 1) state shape is obvious, 2) running/error are independent.
  const [running, setRunning] = useState<Record<string, true>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})

  useEffect(() => {
    getVoiceLibrary()
      .then(setSummary)
      .catch(() => {})
      .finally(() => setIsLoading(false))
  }, [])

  const handleCalibrate = useCallback(async (voiceId: string) => {
    setRunning((s) => ({ ...s, [voiceId]: true }))
    setErrors((e) => {
      if (!(voiceId in e)) return e
      const next = { ...e }
      delete next[voiceId]
      return next
    })
    try {
      const result = await calibrateVoiceSpeed(voiceId)
      // Patch only the calibrated voice in local state — full re-fetch
      // would re-load the entire /voice-library payload for one row's
      // change.
      setSummary((s) => patchVoiceCalibration(s, voiceId, result))
    } catch (err) {
      const message = err instanceof Error ? err.message : "校准失败"
      setErrors((e) => ({ ...e, [voiceId]: message }))
    } finally {
      setRunning((s) => {
        const next = { ...s }
        delete next[voiceId]
        return next
      })
    }
  }, [])

  if (isLoading) return <EmptyState title="加载中" description="正在加载音色库…" />
  if (!summary || summary.voices.length === 0) {
    return (
      <EmptyState
        title="音色库为空"
        description="在翻译任务中克隆音色后会自动保存到这里。"
        actionLabel="新建翻译"
        actionTo="/translations/new"
      />
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold font-heading text-foreground">我的音色</h1>
          <p className="text-sm text-muted-foreground mt-1">管理已克隆和内置的音色。</p>
        </div>
        <div className="flex gap-3 text-xs text-muted-foreground">
          <span className="rounded-full bg-muted/40 px-3 py-1">{summary.voiceCount} 个音色</span>
          <span className="rounded-full bg-primary/15 px-3 py-1 text-primary/80">
            {summary.clonedVoiceCount} 个克隆
          </span>
        </div>
      </div>

      <div className="space-y-3">
        {summary.voices.map((voice) => {
          const isCloned = voice.voiceType === "cloned"
          const isRunning = !!running[voice.voiceId]
          const errorMsg = errors[voice.voiceId]
          const buttonLabel = isRunning
            ? "标定中…"
            : voice.charsPerSecond != null
              ? "重新测试"
              : "测试语速"
          return (
            <article
              key={voice.voiceId}
              className="rounded-2xl border border-border bg-card p-5 transition hover:border-border"
            >
              <div className="flex items-center justify-between gap-4">
                <div className="space-y-1 min-w-0">
                  <h3 className="font-semibold text-foreground truncate">
                    {voice.label || voice.voiceId}
                  </h3>
                  <p className="text-xs font-mono text-muted-foreground/60 truncate">{voice.voiceId}</p>
                  <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
                    {voice.speakerName ? <span>发言人：{voice.speakerName}</span> : null}
                    {voice.createdAt ? (
                      <span>· {new Date(voice.createdAt).toLocaleDateString("zh-CN")}</span>
                    ) : null}
                    <SpeedBadge cps={voice.charsPerSecond} calibratedAt={voice.speedCalibratedAt} />
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {isCloned ? (
                    <button
                      type="button"
                      onClick={() => handleCalibrate(voice.voiceId)}
                      disabled={isRunning}
                      className="rounded-full border border-border bg-muted/40 px-3 py-1 text-xs font-semibold text-muted-foreground transition hover:border-primary/40 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      {buttonLabel}
                    </button>
                  ) : null}
                  <span
                    className={`rounded-full px-3 py-1 text-xs font-semibold ${
                      isCloned ? "bg-primary/15 text-primary/80" : "bg-muted/40 text-muted-foreground"
                    }`}
                  >
                    {isCloned ? "克隆" : "内置"}
                  </span>
                </div>
              </div>
              {errorMsg ? (
                <p className="mt-3 text-xs text-destructive">校准失败：{errorMsg}</p>
              ) : null}
            </article>
          )
        })}
      </div>
    </div>
  )
}

function patchVoiceCalibration(
  summary: VoiceLibrarySummary | null,
  voiceId: string,
  result: Awaited<ReturnType<typeof calibrateVoiceSpeed>>,
): VoiceLibrarySummary | null {
  if (!summary) return summary
  const cps = result.calibration?.cps ?? null
  const calibratedAt = result.voice?.speed_calibrated_at ?? new Date().toISOString()
  return {
    ...summary,
    voices: summary.voices.map((v): VoiceLibraryEntry =>
      v.voiceId === voiceId
        ? { ...v, charsPerSecond: cps, speedCalibratedAt: calibratedAt }
        : v,
    ),
  }
}

function SpeedBadge({
  cps,
  calibratedAt,
}: {
  cps: number | null
  calibratedAt: string | null
}) {
  if (cps == null) {
    return <span className="text-muted-foreground/60">· 未标定</span>
  }
  const tier = cpsTier(cps)
  const ago = relativeAgo(calibratedAt)
  return (
    <span className={tier.colorClass}>
      · {cps.toFixed(1)} 字/秒（{tier.label}）{ago ? ` · ${ago}` : ""}
    </span>
  )
}

function cpsTier(cps: number): { label: string; colorClass: string } {
  if (cps < 3.5) return { label: "慢", colorClass: "text-amber-500/80" }
  if (cps >= 4.5) return { label: "快", colorClass: "text-cyan-500/80" }
  return { label: "中速", colorClass: "" }
}

function relativeAgo(iso: string | null): string | null {
  if (!iso) return null
  const ts = Date.parse(iso)
  if (!Number.isFinite(ts)) return null
  const deltaSec = Math.max(0, Math.floor((Date.now() - ts) / 1000))
  if (deltaSec < 60) return "刚刚标定"
  if (deltaSec < 3600) return `${Math.floor(deltaSec / 60)}分钟前标定`
  if (deltaSec < 86400) return `${Math.floor(deltaSec / 3600)}小时前标定`
  return `${Math.floor(deltaSec / 86400)}天前标定`
}
