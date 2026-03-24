"use client"

import { useEffect, useState } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { EmptyState } from "@/components/empty-state"
import { getVoiceLibrary, type VoiceLibrarySummary, type VoiceLibraryEntry } from "@/lib/api/voiceLibrary"

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
        <h1 className="text-2xl font-bold">我的音色</h1>
        <div className="flex gap-3 text-sm text-muted-foreground">
          <span>共 {summary.voiceCount} 个音色</span>
          <span>·</span>
          <span>{summary.clonedVoiceCount} 个克隆</span>
        </div>
      </div>

      <div className="space-y-3">
        {summary.voices.map((voice) => (
          <Card key={voice.voiceId}>
            <CardContent className="pt-4">
              <div className="flex items-center justify-between">
                <div className="space-y-1">
                  <h3 className="font-semibold">{voice.label || voice.voiceId}</h3>
                  <p className="text-xs font-mono text-muted-foreground">{voice.voiceId}</p>
                  <div className="flex gap-2 text-xs text-muted-foreground">
                    {voice.speakerName ? <span>发言人：{voice.speakerName}</span> : null}
                    {voice.createdAt ? <span>· {new Date(voice.createdAt).toLocaleDateString("zh-CN")}</span> : null}
                  </div>
                </div>
                <Badge variant={voice.voiceType === "cloned" ? "default" : "outline"}>
                  {voice.voiceType === "cloned" ? "克隆" : "内置"}
                </Badge>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}
