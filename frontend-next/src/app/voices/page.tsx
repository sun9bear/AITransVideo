"use client"

import { useEffect, useState } from "react"
import { EmptyState } from "@/components/empty-state"
import { getVoiceLibrary, type VoiceLibrarySummary } from "@/lib/api/voiceLibrary"

export default function VoiceLibraryPage() {
  const [summary, setSummary] = useState<VoiceLibrarySummary | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  useEffect(() => {
    getVoiceLibrary()
      .then(setSummary)
      .catch(() => {})
      .finally(() => setIsLoading(false))
  }, [])

  if (isLoading) return <EmptyState title="加载中" description="正在加载音色库..." />
  if (!summary || summary.voices.length === 0) {
    return <EmptyState title="音色库为空" description="在翻译任务中克隆音色后会自动保存到这里。" actionLabel="新建翻译" actionTo="/translations/new" />
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
          <span className="rounded-full bg-primary/15 px-3 py-1 text-primary/80">{summary.clonedVoiceCount} 个克隆</span>
        </div>
      </div>

      <div className="space-y-3">
        {summary.voices.map((voice) => (
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
                  {voice.createdAt ? <span>· {new Date(voice.createdAt).toLocaleDateString("zh-CN")}</span> : null}
                </div>
              </div>
              <span className={`shrink-0 rounded-full px-3 py-1 text-xs font-semibold ${voice.voiceType === "cloned" ? "bg-primary/15 text-primary/80" : "bg-muted/40 text-muted-foreground"}`}>
                {voice.voiceType === "cloned" ? "克隆" : "内置"}
              </span>
            </div>
          </article>
        ))}
      </div>
    </div>
  )
}
