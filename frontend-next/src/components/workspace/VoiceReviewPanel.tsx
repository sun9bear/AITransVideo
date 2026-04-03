"use client"

import { useCallback, useEffect, useState } from 'react'
import { toast } from 'sonner'

import { approveVoiceReview } from '@/lib/api/reviews'
import { getJob } from '@/lib/api/jobs'
import type { ApiWebUiStateResponse } from '@/types/api'
import type { JobSummary } from '@/types/jobs'
import { apiClient } from '@/lib/api/client'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface Speaker {
  speakerId: string
  speakerName: string
}

interface VoiceOption {
  voiceId: string
  displayName: string
  gender: string
}

interface VoiceReviewPanelProps {
  jobId: string
  onAdvanced: (job: JobSummary) => void
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function VoiceReviewPanel({ jobId, onAdvanced }: VoiceReviewPanelProps) {
  const [speakers, setSpeakers] = useState<Speaker[]>([])
  const [voiceOptions, setVoiceOptions] = useState<VoiceOption[]>([])
  const [selections, setSelections] = useState<Record<string, string>>({})
  const [projectDir, setProjectDir] = useState('')
  const [isLoading, setIsLoading] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Load voice review data
  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        setIsLoading(true)
        setError(null)

        const [reviewState, job] = await Promise.all([
          apiClient.get<{
            job_id: string
            status: string
            review_gate: Record<string, unknown> | null
            results: ApiWebUiStateResponse['results']
          }>(`/jobs/${jobId}/review-state`),
          getJob(jobId),
        ])

        if (cancelled) return

        // Extract speakers from voice_review stage
        const voiceReview = reviewState.results?.voice_library?.active_review
        const rawSpeakers = voiceReview?.speakers ?? []

        const loadedSpeakers: Speaker[] = rawSpeakers.map((s: { speaker_id: string; speaker_name?: string | null; speaker_label?: string | null }) => ({
          speakerId: String(s.speaker_id ?? ''),
          speakerName: String(s.speaker_name ?? s.speaker_label ?? s.speaker_id ?? ''),
        })).filter((s: Speaker) => s.speakerId)

        // Extract VolcEngine 2.0 voices
        const raw2_0 = voiceReview?.volcengine_2_0_voices ?? []
        const options: VoiceOption[] = raw2_0.map((v: { voice_id: string; display_name: string; gender: string }) => ({
          voiceId: String(v.voice_id ?? ''),
          displayName: String(v.display_name ?? v.voice_id ?? ''),
          gender: String(v.gender ?? ''),
        })).filter((v: VoiceOption) => v.voiceId)

        // Resolve project_dir
        const pDir =
          String(reviewState.results?.project_dir ?? '') ||
          String(job.projectDir ?? '')

        setSpeakers(loadedSpeakers)
        setVoiceOptions(options)
        setProjectDir(pDir)

        // If no 2.0 voices available (non-volcengine or express job),
        // auto-approve all speakers with "auto" immediately
        if (options.length === 0 && loadedSpeakers.length > 0 && pDir) {
          try {
            const autoResult = await approveVoiceReview({
              jobId,
              projectDir: pDir,
              voiceIdA: loadedSpeakers.some((s: Speaker) => s.speakerId === 'speaker_a') ? 'auto' : null,
              voiceIdB: loadedSpeakers.some((s: Speaker) => s.speakerId === 'speaker_b') ? 'auto' : null,
            })
            onAdvanced(autoResult.job)
            return
          } catch {
            // Auto-approve failed — fall through to show empty panel
          }
        }

        // Initialize selections as empty — user must explicitly choose
        setSelections({})
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err))
        }
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }

    load()
    return () => { cancelled = true }
  }, [jobId, onAdvanced])

  const handleSelect = useCallback((speakerId: string, value: string) => {
    setSelections(prev => ({ ...prev, [speakerId]: value }))
  }, [])

  const handleSubmit = useCallback(async () => {
    // Validate: every speaker must have a selection
    const missing = speakers.filter(s => !selections[s.speakerId])
    if (missing.length > 0) {
      const names = missing.map(s => s.speakerName).join(', ')
      alert(`请为以下说话人选择音色或"自动匹配"：${names}`)
      return
    }

    setIsSubmitting(true)
    try {
      const result = await approveVoiceReview({
        jobId,
        projectDir,
        voiceIdA: selections['speaker_a'] || null,
        voiceIdB: selections['speaker_b'] || null,
      })
      toast.success('音色确认成功')
      onAdvanced(result.job)
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      toast.error(`音色确认失败: ${msg}`)
    } finally {
      setIsSubmitting(false)
    }
  }, [jobId, projectDir, speakers, selections, onAdvanced])

  // --- Render ---

  if (isLoading) {
    return (
      <section className="surface-card p-8 text-center">
        <div className="mx-auto mb-4 h-10 w-10 animate-spin rounded-full border-3 border-primary border-t-transparent" />
        <h3 className="text-lg font-semibold text-foreground">加载音色选择...</h3>
      </section>
    )
  }

  if (error) {
    return (
      <section className="surface-card p-6 border border-red-200 dark:border-red-500/20">
        <h3 className="text-lg font-semibold text-red-700 dark:text-red-400">加载失败</h3>
        <p className="mt-2 text-sm text-red-600 dark:text-red-400/80">{error}</p>
      </section>
    )
  }

  // Group voice options by gender for better UX
  const maleVoices = voiceOptions.filter(v => v.gender === 'male')
  const femaleVoices = voiceOptions.filter(v => v.gender === 'female')
  const otherVoices = voiceOptions.filter(v => v.gender !== 'male' && v.gender !== 'female')

  return (
    <section className="surface-card p-6">
      <h3 className="text-lg font-semibold text-foreground mb-1">工作台音色选择</h3>
      <p className="text-sm text-muted-foreground mb-6">
        请为每个说话人选择豆包 2.0 音色，或选择「自动匹配」由系统根据说话人特征自动选择。
      </p>

      <div className="space-y-6">
        {speakers.map(speaker => (
          <div key={speaker.speakerId} className="rounded-xl border border-border p-4">
            <label className="block text-sm font-medium text-foreground mb-2">
              {speaker.speakerName}
            </label>
            <select
              className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
              value={selections[speaker.speakerId] ?? ''}
              onChange={(e) => handleSelect(speaker.speakerId, e.target.value)}
            >
              <option value="">-- 请选择音色 --</option>
              <option value="auto">自动匹配（系统根据说话人特征选择）</option>
              {femaleVoices.length > 0 && (
                <optgroup label="女声">
                  {femaleVoices.map(v => (
                    <option key={v.voiceId} value={v.voiceId}>{v.displayName}</option>
                  ))}
                </optgroup>
              )}
              {maleVoices.length > 0 && (
                <optgroup label="男声">
                  {maleVoices.map(v => (
                    <option key={v.voiceId} value={v.voiceId}>{v.displayName}</option>
                  ))}
                </optgroup>
              )}
              {otherVoices.length > 0 && (
                <optgroup label="其他">
                  {otherVoices.map(v => (
                    <option key={v.voiceId} value={v.voiceId}>{v.displayName}</option>
                  ))}
                </optgroup>
              )}
            </select>
            {!selections[speaker.speakerId] && (
              <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">必须选择</p>
            )}
          </div>
        ))}
      </div>

      <div className="mt-6 flex justify-end">
        <button
          className="primary-button"
          disabled={isSubmitting}
          onClick={handleSubmit}
        >
          {isSubmitting ? '提交中...' : '确认并继续'}
        </button>
      </div>
    </section>
  )
}
