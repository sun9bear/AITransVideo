"use client"

import { useEffect, useState } from 'react'

import { getErrorMessage } from '@/lib/api/errors'
import { approveTranslationReview, getTranslationReview, splitSegment } from '@/lib/api/reviews'
import type { TranslationReviewResource } from '@/types/reviews'

type TranslationSegmentState = Record<
  string,
  { cnText: string; rewriteRequested: boolean; translationConfirmed: boolean; ttsCnText: string; updatedAt: string }
>

interface TranslationReviewPanelProps {
  jobId: string
  onAdvanced: () => void
}

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
                ttsCnText: item.ttsCnText,
                updatedAt: item.reviewUpdatedAt ?? '',
              },
            ]),
          ),
        )
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
        segments: Object.fromEntries(
          resource.items.map((item) => {
            const current = segments[item.segmentId] ?? {
              cnText: item.cnText, rewriteRequested: item.rewriteRequested,
              translationConfirmed: item.translationConfirmed, ttsCnText: item.ttsCnText,
              updatedAt: item.reviewUpdatedAt ?? '',
            }
            return [item.segmentId, { cnText: current.cnText, rewriteRequested: current.rewriteRequested, translationConfirmed: current.translationConfirmed, ttsCnText: current.ttsCnText, updatedAt: current.updatedAt || now }]
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
    return <PanelLoading message="正在读取翻译审核内容…" />
  }
  if (pageError && !resource) {
    return <PanelError message={pageError} />
  }
  if (!resource) {
    return <PanelError message="当前没有可展示的翻译审核内容。" />
  }

  return (
    <div className="space-y-5">
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
          {isSubmitting ? '提交中…' : '✓ 确认并继续'}
        </button>
      </div>

      {submitError ? <ErrorBanner message={submitError} /> : null}

      {hasPagination ? (
        <Pagination
          currentPage={currentPage} pageSize={pageSize} pageSizeOptions={resource.pageSizeOptions}
          totalItems={totalItems} totalPages={totalPages}
          onPageChange={setPage} onPageSizeChange={(s) => { setPageSize(s); setPage(1) }}
        />
      ) : null}

      <section className="surface-card p-5">
        <div className="space-y-4">
          {visibleItems.map((item) => {
            const current = segments[item.segmentId] ?? {
              cnText: item.cnText, rewriteRequested: item.rewriteRequested,
              translationConfirmed: item.translationConfirmed, ttsCnText: item.ttsCnText,
              updatedAt: item.reviewUpdatedAt ?? '',
            }

            return (
              <article key={item.segmentId} className="rounded-2xl border border-border bg-card p-5">
                {/* Header: segment id + speaker */}
                <div className="flex flex-wrap items-center gap-3">
                  <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">片段 {item.segmentId}</p>
                  {resource.speakerOptions.length > 1 ? (
                    <select
                      className="form-input min-w-[140px] py-1 text-sm font-semibold"
                      onChange={(event) => {
                        const nextSpeakerId = event.currentTarget.value
                        const currentSpeakerId = segmentSpeakers[item.segmentId] ?? item.speakerId
                        if (nextSpeakerId === currentSpeakerId) return
                        const currentIdx = resource.items.findIndex((i) => i.segmentId === item.segmentId)
                        const subsequentItems = resource.items.slice(currentIdx)
                        const affectedCount = subsequentItems.filter((i) => {
                          const spk = segmentSpeakers[i.segmentId] ?? i.speakerId
                          return spk === currentSpeakerId || spk === nextSpeakerId
                        }).length

                        if (affectedCount > 1 && window.confirm(
                          `是否将后续所有 "${resource.speakerOptions.find(o => o.id === currentSpeakerId)?.displayName || currentSpeakerId}" 替换为 "${resource.speakerOptions.find(o => o.id === nextSpeakerId)?.displayName || nextSpeakerId}"，同时互换？\n\n将影响 ${affectedCount} 个片段。\n\n确定=批量互换，取消=仅修改当前。`
                        )) {
                          setSegmentSpeakers((prev) => {
                            const updated = { ...prev }
                            for (const sub of subsequentItems) {
                              const spk = updated[sub.segmentId] ?? sub.speakerId
                              if (spk === currentSpeakerId) updated[sub.segmentId] = nextSpeakerId
                              else if (spk === nextSpeakerId) updated[sub.segmentId] = currentSpeakerId
                            }
                            return updated
                          })
                        } else {
                          setSegmentSpeakers((prev) => ({ ...prev, [item.segmentId]: nextSpeakerId }))
                        }
                      }}
                      value={segmentSpeakers[item.segmentId] ?? item.speakerId}
                    >
                      {resource.speakerOptions.map((o) => <option key={o.id} value={o.id}>{o.displayName}</option>)}
                    </select>
                  ) : (
                    <p className="text-sm font-semibold text-foreground">{item.displayName || item.speakerId}</p>
                  )}
                  <button
                    className="ml-auto rounded-lg border border-cyan-500/30 bg-cyan-500/10 px-3 py-1 text-xs font-medium text-cyan-400 transition hover:bg-cyan-500/20 hover:border-cyan-500/50"
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

                {/* Source text */}
                <div className="mt-3 rounded-xl border border-border bg-muted/30 px-4 py-3 text-sm leading-6 text-foreground/70">
                  {item.sourceText || '-'}
                </div>

                {/* Translation textarea — styled like source block, hover highlight */}
                <div className="mt-3">
                  <span className="text-xs font-medium text-muted-foreground mb-1 block">译文</span>
                  <div className="group rounded-xl border border-border bg-muted/30 transition hover:border-primary/30 hover:bg-primary/5 focus-within:border-primary/40 focus-within:bg-primary/5">
                    <textarea
                      className="w-full min-h-[2lh] resize-none rounded-xl bg-transparent px-4 py-2 text-sm leading-6 text-foreground placeholder:text-muted-foreground/60 focus:outline-none input-focus-ring overflow-hidden"
                      onChange={(event) => {
                        const v = event.currentTarget.value
                        setSegments((s) => ({ ...s, [item.segmentId]: { ...(s[item.segmentId] ?? current), cnText: v, ttsCnText: v, updatedAt: new Date().toISOString() } }))
                      }}
                      onInput={(event) => { const el = event.currentTarget; el.style.height = 'auto'; el.style.height = `${el.scrollHeight}px` }}
                      ref={(el) => { if (el) { el.style.height = 'auto'; el.style.height = `${el.scrollHeight}px` } }}
                      value={current.cnText}
                    />
                  </div>
                </div>

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
                          {resource.speakerOptions.map((o) => <option key={o.id} value={o.id}>{o.displayName}</option>)}
                        </select>
                      </label>
                      <label className="space-y-1">
                        <span className="form-label">片段 B 发言人</span>
                        <select className="form-input text-sm" onChange={(e) => setSplitSpeakerB(e.currentTarget.value)} value={splitSpeakerB}>
                          {resource.speakerOptions.map((o) => <option key={o.id} value={o.id}>{o.displayName}</option>)}
                        </select>
                      </label>
                    </div>
                    <button className="primary-button text-sm" disabled={isSplitting} onClick={async () => {
                      setIsSplitting(true)
                      try {
                        const result = await splitSegment({
                          projectDir: resource.projectDir, segmentId: item.segmentId,
                          splitSourceIndex: splitSourcePos, splitCnIndex: splitCnPos,
                          speakerA: splitSpeakerA, speakerB: splitSpeakerB,
                          stage: 'translation_review', pendingSpeakerChanges: segmentSpeakers,
                        })
                        if (result.success) { window.location.reload(); return }
                        setSubmitError('拆分未生效。')
                      } catch (error) { setSubmitError(`拆分失败: ${getErrorMessage(error)}`) } finally { setIsSplitting(false) }
                    }} type="button">
                      {isSplitting ? '拆分中…' : '确认拆分'}
                    </button>
                  </div>
                ) : null}
              </article>
            )
          })}
        </div>
      </section>
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

function formatDateTime(value: string) {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(parsed)
}
