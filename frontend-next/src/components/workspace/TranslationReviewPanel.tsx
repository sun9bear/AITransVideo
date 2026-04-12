"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { getErrorMessage } from '@/lib/api/errors'
import {
  approveTranslationReview,
  cloneVoiceForReview,
  getTranslationReview,
  previewSegmentForJob,
  previewSourceAudioForJob,
  splitSegment,
} from '@/lib/api/reviews'
import { getVoiceLibrary, type VoiceLibraryEntry } from '@/lib/api/voiceLibrary'
import type { TranslationReviewResource } from '@/types/reviews'

/* ---------- Types ---------- */

type TranslationSegmentState = Record<
  string,
  { cnText: string; rewriteRequested: boolean; translationConfirmed: boolean; updatedAt: string }
>

interface SpeakerVoiceConfig {
  voiceId: string
  isCloning: boolean
  cloneError: string | null
}

interface TranslationReviewPanelProps {
  jobId: string
  onAdvanced: () => void
}

/* ---------- Main Component ---------- */

export function TranslationReviewPanel({ jobId, onAdvanced }: TranslationReviewPanelProps) {
  const [resource, setResource] = useState<TranslationReviewResource | null>(null)
  const [segments, setSegments] = useState<TranslationSegmentState>({})
  const [segmentSpeakers, setSegmentSpeakers] = useState<Record<string, string>>({})
  const [splittingSegmentId, setSplittingSegmentId] = useState<string | null>(null)
  const [splitSourcePos, setSplitSourcePos] = useState(0)
  const [splitCnPos, setSplitCnPos] = useState(0)
  const [splitSpeakerA, setSplitSpeakerA] = useState('')
  const [splitSpeakerB, setSplitSpeakerB] = useState('')
  const [isSplitting, setIsSplitting] = useState(false)
  const [pageSize, setPageSize] = useState(20)
  const [page, setPage] = useState(1)
  const [isLoading, setIsLoading] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [pageError, setPageError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)

  // Speaker voice config
  const [speakerVoices, setSpeakerVoices] = useState<Record<string, SpeakerVoiceConfig>>({})
  const [speakerNames, setSpeakerNames] = useState<Record<string, string>>({})
  const [allVoices, setAllVoices] = useState<VoiceLibraryEntry[]>([])

  // Preview state per segment
  const [previewingSegmentId, setPreviewingSegmentId] = useState<string | null>(null)
  const [previewError, setPreviewError] = useState<Record<string, string>>({})
  const audioRef = useRef<HTMLAudioElement | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      setIsLoading(true)
      try {
        const nextResource = await getTranslationReview(jobId)
        if (cancelled) return
        setResource(nextResource)
        setPageSize(nextResource.defaultPageSize)
        setPage(1)
        setSegments(
          Object.fromEntries(
            nextResource.items.map((item) => [
              item.segmentId,
              {
                cnText: item.cnText,
                rewriteRequested: item.rewriteRequested,
                translationConfirmed: item.translationConfirmed,
                cnText: item.cnText,
                updatedAt: item.reviewUpdatedAt ?? '',
              },
            ]),
          ),
        )

        // Initialize speaker names from speakerOptions
        setSpeakerNames(
          Object.fromEntries(nextResource.speakerOptions.map((o) => [o.id, o.displayName])),
        )

        // Initialize voice configs per speaker (empty initially)
        const voiceConfigs: Record<string, SpeakerVoiceConfig> = {}
        for (const option of nextResource.speakerOptions) {
          voiceConfigs[option.id] = { voiceId: '', isCloning: false, cloneError: null }
        }
        setSpeakerVoices(voiceConfigs)

        // Load voice library
        try {
          const lib = await getVoiceLibrary()
          if (!cancelled) setAllVoices(lib.voices)
        } catch { /* non-critical */ }

        setPageError(null)
      } catch (error) {
        if (!cancelled) setPageError(getErrorMessage(error))
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    void load()
    return () => { cancelled = true }
  }, [jobId])

  // Compute eligible preview segments: for each speaker, first 2 segments with duration > 5s
  const eligiblePreviewSegments = useMemo(() => {
    if (!resource) return new Set<string>()
    const eligible = new Set<string>()
    const speakerCounts: Record<string, number> = {}

    for (const item of resource.items) {
      const spk = segmentSpeakers[item.segmentId] ?? item.speakerId
      const durationMs = item.endMs - item.startMs
      if (durationMs <= 5000) continue
      const count = speakerCounts[spk] ?? 0
      if (count >= 2) continue
      speakerCounts[spk] = count + 1
      eligible.add(item.segmentId)
    }

    return eligible
  }, [resource, segmentSpeakers])

  const updateSpeakerVoice = useCallback((speakerId: string, update: Partial<SpeakerVoiceConfig>) => {
    setSpeakerVoices((prev) => ({ ...prev, [speakerId]: { ...prev[speakerId], ...update } }))
  }, [])

  const handleClone = useCallback(async (speakerId: string) => {
    if (!resource) return
    updateSpeakerVoice(speakerId, { isCloning: true, cloneError: null })
    try {
      const name = speakerNames[speakerId] ?? speakerId
      const result = await cloneVoiceForReview(speakerId, name, '', resource.projectDir, jobId)
      updateSpeakerVoice(speakerId, { voiceId: result.voiceId, isCloning: false })
      try { const lib = await getVoiceLibrary(); setAllVoices(lib.voices) } catch { /* non-critical */ }
    } catch (error) {
      updateSpeakerVoice(speakerId, { isCloning: false, cloneError: getErrorMessage(error) })
    }
  }, [jobId, resource, speakerNames, updateSpeakerVoice])

  const handlePreviewSource = useCallback(async (segmentId: string) => {
    if (!resource) return
    const item = resource.items.find((i) => i.segmentId === segmentId)
    if (!item) return

    setPreviewingSegmentId(segmentId)
    setPreviewError((prev) => { const n = { ...prev }; delete n[segmentId]; return n })
    try {
      const result = await previewSourceAudioForJob(jobId, {
        segmentId: Number(segmentId),
        sourceStartMs: item.startMs,
        sourceEndMs: item.endMs,
      })
      if (result.sourceAudioBase64 && audioRef.current) {
        audioRef.current.src = `data:audio/wav;base64,${result.sourceAudioBase64}`
        void audioRef.current.play()
      } else {
        setPreviewError((prev) => ({ ...prev, [segmentId]: '原文音频提取失败，请确认项目源音频存在' }))
      }
    } catch (error) {
      setPreviewError((prev) => ({ ...prev, [segmentId]: getErrorMessage(error) }))
    } finally {
      setPreviewingSegmentId(null)
    }
  }, [jobId, resource])

  const handlePreviewSegment = useCallback(async (segmentId: string) => {
    if (!resource) return
    const item = resource.items.find((i) => i.segmentId === segmentId)
    if (!item) return
    const spk = segmentSpeakers[segmentId] ?? item.speakerId
    const voiceId = speakerVoices[spk]?.voiceId
    if (!voiceId) {
      setPreviewError((prev) => ({ ...prev, [segmentId]: '请先为该说话人选择音色' }))
      return
    }
    const currentSegment = segments[segmentId]
    const cnText = currentSegment?.cnText ?? item.cnText

    setPreviewingSegmentId(segmentId)
    setPreviewError((prev) => { const n = { ...prev }; delete n[segmentId]; return n })
    try {
      const result = await previewSegmentForJob(jobId, {
        segmentId: Number(segmentId),
        sourceStartMs: item.startMs,
        sourceEndMs: item.endMs,
        cnText: cnText,
        voiceId: voiceId,
      })
      if (result.ttsAudioBase64 && audioRef.current) {
        audioRef.current.src = `data:audio/wav;base64,${result.ttsAudioBase64}`
        audioRef.current.play()
      }
    } catch (error) {
      setPreviewError((prev) => ({ ...prev, [segmentId]: getErrorMessage(error) }))
    } finally {
      setPreviewingSegmentId(null)
    }
  }, [jobId, resource, segmentSpeakers, speakerVoices, segments])

  const totalItems = resource?.items.length ?? 0
  const totalPages = Math.max(1, Math.ceil(totalItems / pageSize))
  const currentPage = Math.min(page, totalPages)
  const hasPagination = totalItems > 20
  const visibleItems = resource?.items.slice((currentPage - 1) * pageSize, currentPage * pageSize) ?? []

  const handleApprove = async () => {
    if (!resource) return
    setIsSubmitting(true)
    setSubmitError(null)
    try {
      const now = new Date().toISOString()
      await approveTranslationReview({
        jobId,
        projectDir: resource.projectDir,
        segmentSpeakers,
        speakerNames,
        segments: Object.fromEntries(
          resource.items.map((item) => {
            const current = segments[item.segmentId] ?? {
              cnText: item.cnText, rewriteRequested: item.rewriteRequested,
              translationConfirmed: item.translationConfirmed, cnText: item.cnText,
              updatedAt: item.reviewUpdatedAt ?? '',
            }
            return [item.segmentId, { cnText: current.cnText, rewriteRequested: current.rewriteRequested, translationConfirmed: current.translationConfirmed, cnText: current.cnText, updatedAt: current.updatedAt || now }]
          }),
        ),
      })
      onAdvanced()
    } catch (error) {
      setSubmitError(getErrorMessage(error))
    } finally {
      setIsSubmitting(false)
    }
  }

  if (isLoading && !resource) {
    return <PanelLoading message="正在读取翻译审核内容..." />
  }
  if (pageError && !resource) {
    return <PanelError message={pageError} />
  }
  if (!resource) {
    return <PanelError message="当前没有可展示的翻译审核内容。" />
  }

  return (
    <div className="space-y-5">
      <audio ref={audioRef} className="hidden" />

      {/* Action bar */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          确认翻译与配音文本，共 {resource.items.length} 条。
        </p>
        <button
          className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-primary to-primary/80 px-6 py-2.5 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110 disabled:opacity-50"
          disabled={isSubmitting}
          onClick={() => { void handleApprove() }}
          type="button"
        >
          {isSubmitting ? '提交中...' : '确认并继续'}
        </button>
      </div>

      {submitError ? <ErrorBanner message={submitError} /> : null}

      {/* Speaker identity confirmation */}
      {resource.speakerOptions.length > 1 ? (
        <section className="surface-card p-4">
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-foreground">说话人确认</h3>
              <span className="text-xs text-muted-foreground">确认每位说话人的名字，可直接修改</span>
            </div>
            <div className="flex flex-wrap gap-3">
              {resource.speakerOptions.map((option, index) => (
                <div key={option.id} className="flex items-center gap-2 rounded-lg border border-border bg-background px-3 py-2">
                  <div className="flex h-7 w-7 items-center justify-center rounded-full bg-slate-200 dark:bg-slate-700 text-xs font-bold text-slate-600 dark:text-slate-300 shrink-0">
                    {String.fromCharCode(65 + index)}
                  </div>
                  <input
                    className="h-7 w-[140px] rounded border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 px-2 text-sm text-foreground"
                    value={speakerNames[option.id] ?? option.displayName}
                    onChange={(e) => {
                      const newName = e.target.value
                      setSpeakerNames((prev) => ({ ...prev, [option.id]: newName }))
                    }}
                    placeholder={option.id}
                  />
                  <span className="text-xs text-muted-foreground">{option.id}</span>
                </div>
              ))}
            </div>
          </div>
        </section>
      ) : null}

      {/* Pagination */}
      {hasPagination ? (
        <Pagination
          currentPage={currentPage} pageSize={pageSize} pageSizeOptions={resource.pageSizeOptions}
          totalItems={totalItems} totalPages={totalPages}
          onPageChange={setPage} onPageSizeChange={(s) => { setPageSize(s); setPage(1) }}
        />
      ) : null}

      {/* Segment list */}
      <section className="surface-card p-5">
        <div className="space-y-4">
          {visibleItems.map((item) => {
            const current = segments[item.segmentId] ?? {
              cnText: item.cnText, rewriteRequested: item.rewriteRequested,
              translationConfirmed: item.translationConfirmed, cnText: item.cnText,
              updatedAt: item.reviewUpdatedAt ?? '',
            }
            const isEligibleForPreview = eligiblePreviewSegments.has(item.segmentId)
            const isPreviewingThis = previewingSegmentId === item.segmentId
            const segPreviewError = previewError[item.segmentId]

            return (
              <article key={item.segmentId} className="rounded-2xl border border-border bg-card p-5">
                {/* Header: segment id + speaker + actions */}
                <div className="flex flex-wrap items-center gap-3">
                  <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">片段 {item.segmentId}</p>
                  {resource.speakerOptions.length > 1 ? (
                    <select
                      className="form-input min-w-[140px] py-1 text-sm font-semibold"
                      onChange={(event) => {
                        const nextSpeakerId = event.currentTarget.value
                        const currentSpeakerId = segmentSpeakers[item.segmentId] ?? item.speakerId
                        if (nextSpeakerId === currentSpeakerId) return
                        setSegmentSpeakers((prev) => ({ ...prev, [item.segmentId]: nextSpeakerId }))
                      }}
                      value={segmentSpeakers[item.segmentId] ?? item.speakerId}
                    >
                      {resource.speakerOptions.map((o) => <option key={o.id} value={o.id}>{speakerNames[o.id] ?? o.displayName}</option>)}
                    </select>
                  ) : (
                    <p className="text-sm font-semibold text-foreground">{speakerNames[item.speakerId] ?? (item.displayName || item.speakerId)}</p>
                  )}

                  <div className="ml-auto flex items-center gap-2">
                    {/* Split button */}
                    <button
                      className="rounded-lg border border-cyan-500/30 bg-cyan-500/10 px-3 py-1 text-xs font-medium text-cyan-400 transition hover:bg-cyan-500/20 hover:border-cyan-500/50"
                      onClick={() => {
                        if (splittingSegmentId === item.segmentId) { setSplittingSegmentId(null) } else {
                          setSplittingSegmentId(item.segmentId)
                          setSplitSourcePos(Math.floor((item.sourceText || '').length / 2))
                          setSplitCnPos(Math.floor((current.cnText || '').length / 2))
                          setSplitSpeakerA(segmentSpeakers[item.segmentId] ?? item.speakerId)
                          setSplitSpeakerB(resource.speakerOptions.length > 1 ? resource.speakerOptions.find(o => o.id !== item.speakerId)?.id ?? item.speakerId : item.speakerId)
                        }
                      }}
                      type="button"
                    >
                      {splittingSegmentId === item.segmentId ? '取消拆分' : '拆分'}
                    </button>
                  </div>
                </div>

                {/* Preview error */}
                {segPreviewError ? (
                  <p className="mt-1 text-xs text-red-400">{segPreviewError}</p>
                ) : null}

                {/* Source text + play source button */}
                <div className="mt-3">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs font-medium text-muted-foreground">原文</span>
                    <button
                      className="rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-1 text-xs font-medium text-sky-400 transition hover:bg-sky-500/20 hover:border-sky-500/50 disabled:opacity-50"
                      disabled={isPreviewingThis}
                      onClick={() => { void handlePreviewSource(item.segmentId) }}
                      type="button"
                      title="播放该段的原始音频（用于核对发言人）"
                    >
                      {isPreviewingThis ? '加载中...' : '▶ 播放原文'}
                    </button>
                  </div>
                  <div className="rounded-xl border border-border bg-muted/30 px-4 py-3 text-sm leading-6 text-foreground/70">
                    {item.sourceText || '-'}
                  </div>
                </div>

                {/* Translation textarea + preview tts button */}
                <div className="mt-3">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs font-medium text-muted-foreground">译文</span>
                  </div>
                  <div className="group rounded-xl border border-border bg-muted/30 transition hover:border-primary/30 hover:bg-primary/5 focus-within:border-primary/40 focus-within:bg-primary/5">
                    <textarea
                      className="w-full min-h-[2lh] resize-none rounded-xl bg-transparent px-4 py-2 text-sm leading-6 text-foreground placeholder:text-muted-foreground/60 focus:outline-none input-focus-ring overflow-hidden"
                      onChange={(event) => {
                        const v = event.currentTarget.value
                        setSegments((s) => ({ ...s, [item.segmentId]: { ...(s[item.segmentId] ?? current), cnText: v, cnText: v, updatedAt: new Date().toISOString() } }))
                      }}
                      onInput={(event) => { const el = event.currentTarget; el.style.height = 'auto'; el.style.height = `${el.scrollHeight}px` }}
                      ref={(el) => { if (el) { el.style.height = 'auto'; el.style.height = `${el.scrollHeight}px` } }}
                      value={current.cnText}
                    />
                  </div>
                </div>

                {/* Split panel */}
                {splittingSegmentId === item.segmentId ? (
                  <div className="mt-3 rounded-xl border-2 border-amber-500/20 bg-amber-500/5 p-4 space-y-4">
                    <p className="text-sm font-semibold text-foreground">拆分片段 {item.segmentId}</p>
                    <div className="space-y-2">
                      <p className="form-label">原文拆分位置（{splitSourcePos}）</p>
                      <input className="w-full" max={(item.sourceText || '').length} min={1} onChange={(e) => setSplitSourcePos(Number(e.currentTarget.value))} type="range" value={splitSourcePos} />
                      <div className="grid grid-cols-2 gap-2 text-xs">
                        <div className="rounded-lg bg-muted/30 p-2 border border-border"><p className="text-foreground/70">{(item.sourceText || '').slice(0, splitSourcePos)}</p></div>
                        <div className="rounded-lg bg-muted/30 p-2 border border-border"><p className="text-foreground/70">{(item.sourceText || '').slice(splitSourcePos)}</p></div>
                      </div>
                    </div>
                    <div className="space-y-2">
                      <p className="form-label">译文拆分位置（{splitCnPos}）</p>
                      <input className="w-full" max={(current.cnText || '').length} min={1} onChange={(e) => setSplitCnPos(Number(e.currentTarget.value))} type="range" value={splitCnPos} />
                      <div className="grid grid-cols-2 gap-2 text-xs">
                        <div className="rounded-lg bg-muted/30 p-2 border border-border"><p className="text-foreground/70">{(current.cnText || '').slice(0, splitCnPos)}</p></div>
                        <div className="rounded-lg bg-muted/30 p-2 border border-border"><p className="text-foreground/70">{(current.cnText || '').slice(splitCnPos)}</p></div>
                      </div>
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                      <label className="space-y-1">
                        <span className="form-label">片段 A 发言人</span>
                        <select className="form-input text-sm" onChange={(e) => setSplitSpeakerA(e.currentTarget.value)} value={splitSpeakerA}>
                          {resource.speakerOptions.map((o) => <option key={o.id} value={o.id}>{speakerNames[o.id] ?? o.displayName}</option>)}
                        </select>
                      </label>
                      <label className="space-y-1">
                        <span className="form-label">片段 B 发言人</span>
                        <select className="form-input text-sm" onChange={(e) => setSplitSpeakerB(e.currentTarget.value)} value={splitSpeakerB}>
                          {resource.speakerOptions.map((o) => <option key={o.id} value={o.id}>{speakerNames[o.id] ?? o.displayName}</option>)}
                        </select>
                      </label>
                    </div>
                    <button className="primary-button text-sm" disabled={isSplitting} onClick={async () => {
                      setIsSplitting(true)
                      try {
                        const result = await splitSegment({
                          jobId, projectDir: resource.projectDir, segmentId: item.segmentId,
                          splitSourceIndex: splitSourcePos, splitCnIndex: splitCnPos,
                          speakerA: splitSpeakerA, speakerB: splitSpeakerB,
                          stage: 'translation_review', pendingSpeakerChanges: segmentSpeakers,
                        })
                        if (result.success) { window.location.reload(); return }
                        setSubmitError('拆分未生效。')
                      } catch (error) { setSubmitError(`拆分失败: ${getErrorMessage(error)}`) } finally { setIsSplitting(false) }
                    }} type="button">
                      {isSplitting ? '拆分中...' : '确认拆分'}
                    </button>
                  </div>
                ) : null}
              </article>
            )
          })}
        </div>
      </section>

      {/* Pagination (bottom) */}
      {hasPagination ? (
        <Pagination
          currentPage={currentPage} pageSize={pageSize} pageSizeOptions={resource.pageSizeOptions}
          totalItems={totalItems} totalPages={totalPages}
          onPageChange={setPage} onPageSizeChange={(s) => { setPageSize(s); setPage(1) }}
        />
      ) : null}
    </div>
  )
}

/* ---------- Shared ---------- */

function PanelLoading({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="mb-4 h-8 w-8 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      <p className="text-sm text-muted-foreground">{message}</p>
    </div>
  )
}

function PanelError({ message }: { message: string }) {
  return (
    <div className="rounded-2xl border border-coral-200 bg-coral-50 p-6 text-center dark:border-red-500/20 dark:bg-red-500/8">
      <p className="text-sm text-coral-700 dark:text-red-400">{message}</p>
    </div>
  )
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="rounded-2xl border border-coral-200 bg-coral-50 p-4 text-sm text-coral-700 dark:border-red-500/20 dark:bg-red-500/8 dark:text-red-400">
      {message}
    </div>
  )
}

function Pagination({
  currentPage, pageSize, pageSizeOptions, totalItems, totalPages,
  onPageChange, onPageSizeChange,
}: {
  currentPage: number; pageSize: number; pageSizeOptions: number[];
  totalItems: number; totalPages: number;
  onPageChange: (p: number) => void; onPageSizeChange: (s: number) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
      <span>第 {currentPage} / {totalPages} 页</span>
      <span>显示 {Math.min((currentPage - 1) * pageSize + 1, totalItems)}-{Math.min(currentPage * pageSize, totalItems)} / {totalItems}</span>
      <label className="flex items-center gap-2">
        <span>每页</span>
        <select className="form-input min-w-[96px] py-2" onChange={(e) => onPageSizeChange(Number(e.currentTarget.value))} value={pageSize}>
          {pageSizeOptions.map((opt) => <option key={opt} value={opt}>{opt}</option>)}
        </select>
      </label>
      <div className="flex gap-2">
        <button className="secondary-button px-4 py-2" disabled={currentPage <= 1} onClick={() => { onPageChange(Math.max(1, currentPage - 1)); window.scrollTo({ top: 0, behavior: 'smooth' }) }} type="button">上一页</button>
        <button className="secondary-button px-4 py-2" disabled={currentPage >= totalPages} onClick={() => { onPageChange(Math.min(totalPages, currentPage + 1)); window.scrollTo({ top: 0, behavior: 'smooth' }) }} type="button">下一页</button>
      </div>
    </div>
  )
}
