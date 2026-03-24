"use client"

import { useEffect, useState } from 'react'

import { ApiError } from '@/lib/api/client'
import { approveSpeakerReview, getSpeakerReview, splitSegment } from '@/lib/api/reviews'
import type { SpeakerReviewResource } from '@/types/reviews'

interface SpeakerReviewPanelProps {
  jobId: string
  onAdvanced: () => void
}

export function SpeakerReviewPanel({ jobId, onAdvanced }: SpeakerReviewPanelProps) {
  const [resource, setResource] = useState<SpeakerReviewResource | null>(null)
  const [speakerNames, setSpeakerNames] = useState<Record<string, string>>({})
  const [segmentSpeakers, setSegmentSpeakers] = useState<Record<string, string>>({})
  const [pageSize, setPageSize] = useState(20)
  const [page, setPage] = useState(1)
  const [isLoading, setIsLoading] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [pageError, setPageError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [splittingSegmentId, setSplittingSegmentId] = useState<string | null>(null)
  const [splitSourcePos, setSplitSourcePos] = useState(0)
  const [splitSpeakerA, setSplitSpeakerA] = useState('')
  const [splitSpeakerB, setSplitSpeakerB] = useState('')
  const [isSplitting, setIsSplitting] = useState(false)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      setIsLoading(true)
      try {
        const nextResource = await getSpeakerReview(jobId)
        if (cancelled) return
        setResource(nextResource)
        setPageSize(nextResource.defaultPageSize)
        setPage(1)
        setSpeakerNames(Object.fromEntries(nextResource.speakerOptions.map((o) => [o.id, o.displayName])))
        setSegmentSpeakers(Object.fromEntries(nextResource.items.map((item) => [item.segmentId, item.speakerId])))
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
      await approveSpeakerReview({
        confirmations: Object.fromEntries(
          resource.items.map((item) => [item.segmentId, { speakerConfirmed: true, transcriptConfirmed: true, updatedAt: now }]),
        ),
        jobId,
        projectDir: resource.projectDir,
        segmentSpeakers,
        speakerNames,
      })
      onAdvanced()
    } catch (error) {
      setSubmitError(getErrorMessage(error))
    } finally {
      setIsSubmitting(false)
    }
  }

  if (isLoading && !resource) {
    return <PanelLoading message="正在读取说话人审核内容..." />
  }
  if (pageError && !resource) {
    return <PanelError message={pageError} />
  }
  if (!resource) {
    return <PanelError message="当前没有可展示的说话人审核内容。" />
  }

  return (
    <div className="space-y-5">
      {/* Action bar */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          确认说话人归属与转写，共 {resource.items.length} 条。
        </p>
        <button
          className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-primary to-primary/80 px-6 py-2.5 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110 disabled:opacity-50"
          disabled={isSubmitting}
          onClick={() => { void handleApprove() }}
          type="button"
        >
          {isSubmitting ? '提交中...' : '✓ 确认并继续'}
        </button>
      </div>

      {submitError ? <ErrorBanner message={submitError} /> : null}

      {/* Speaker name editing */}
      <section className="surface-card p-5">
        <h3 className="text-lg font-semibold text-white/90 mb-4">说话人名称</h3>
        <div className="flex flex-wrap gap-4">
          {resource.speakerOptions.map((option) => (
            <label key={option.id} className="flex items-center gap-2">
              <span className="text-sm font-medium text-white/50">{option.id}</span>
              <div className="group rounded-xl border border-white/8 bg-white/5 transition hover:border-primary/30 focus-within:border-primary/40">
                <input
                  className="w-48 rounded-xl bg-transparent px-3 py-2 text-sm text-white/90 focus:outline-none"
                  onChange={(e) => setSpeakerNames((c) => ({ ...c, [option.id]: e.currentTarget.value }))}
                  value={speakerNames[option.id] ?? option.displayName}
                />
              </div>
            </label>
          ))}
        </div>
      </section>

      {/* Pagination */}
      {hasPagination ? (
        <Pagination
          currentPage={currentPage} pageSize={pageSize} pageSizeOptions={resource.pageSizeOptions}
          totalItems={totalItems} totalPages={totalPages}
          onPageChange={setPage} onPageSizeChange={(size) => { setPageSize(size); setPage(1) }}
        />
      ) : null}

      {/* Segment list */}
      <div className="space-y-4">
        {visibleItems.map((item) => (
          <article key={item.segmentId} className="rounded-2xl border border-white/10 bg-white/[0.03] p-5">
            {/* Header */}
            <div className="flex flex-wrap items-center gap-3">
              <p className="text-xs font-semibold uppercase tracking-wider text-white/40">片段 {item.segmentId}</p>
              <button
                className="ml-auto rounded-lg border border-cyan-500/30 bg-cyan-500/10 px-3 py-1 text-xs font-medium text-cyan-400 transition hover:bg-cyan-500/20 hover:border-cyan-500/50"
                onClick={() => {
                  if (splittingSegmentId === item.segmentId) {
                    setSplittingSegmentId(null)
                  } else {
                    setSplittingSegmentId(item.segmentId)
                    setSplitSourcePos(Math.floor((item.sourceText || '').length / 2))
                    setSplitSpeakerA(segmentSpeakers[item.segmentId] ?? item.speakerId)
                    setSplitSpeakerB(
                      resource.speakerOptions.length > 1
                        ? resource.speakerOptions.find(o => o.id !== (segmentSpeakers[item.segmentId] ?? item.speakerId))?.id ?? item.speakerId
                        : item.speakerId,
                    )
                  }
                }}
                type="button"
              >
                {splittingSegmentId === item.segmentId ? '取消拆分' : '拆分'}
              </button>
            </div>

            {/* Source text */}
            <div className="mt-3 rounded-xl border border-white/8 bg-white/5 px-4 py-3 text-sm leading-6 text-white/70">
              {item.sourceText || '-'}
            </div>

            {/* Speaker select */}
            <div className="mt-3 flex items-center gap-3">
              <span className="text-xs font-medium text-white/40">说话人</span>
              <select
                className="form-input border border-primary/30 bg-primary/5 text-sm font-medium text-white/90"
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

                  if (resource.speakerOptions.length > 1 && affectedCount > 1 && window.confirm(
                    `是否将后续所有 "${speakerNames[currentSpeakerId] ?? currentSpeakerId}" 替换为 "${speakerNames[nextSpeakerId] ?? nextSpeakerId}"，同时互换？\n\n将影响 ${affectedCount} 个片段。\n\n确定=批量互换，取消=仅修改当前。`
                  )) {
                    setSegmentSpeakers((c) => {
                      const u = { ...c }
                      for (const sub of subsequentItems) {
                        const s = u[sub.segmentId] ?? sub.speakerId
                        if (s === currentSpeakerId) u[sub.segmentId] = nextSpeakerId
                        else if (s === nextSpeakerId) u[sub.segmentId] = currentSpeakerId
                      }
                      return u
                    })
                  } else {
                    setSegmentSpeakers((c) => ({ ...c, [item.segmentId]: nextSpeakerId }))
                  }
                }}
                value={segmentSpeakers[item.segmentId] ?? item.speakerId}
              >
                {resource.speakerOptions.map((o) => (
                  <option key={o.id} value={o.id}>{speakerNames[o.id] ?? o.displayName}</option>
                ))}
              </select>
            </div>

            {/* Split panel */}
            {splittingSegmentId === item.segmentId ? (
              <div className="mt-3 rounded-xl border-2 border-amber-500/20 bg-amber-500/5 p-4 space-y-4">
                <p className="text-sm font-semibold text-white/90">拆分片段 {item.segmentId}</p>
                <div className="space-y-2">
                  <p className="text-xs font-medium text-white/50">原文拆分位置（{splitSourcePos}）</p>
                  <input className="w-full" max={(item.sourceText || '').length} min={1} onChange={(e) => setSplitSourcePos(Number(e.currentTarget.value))} type="range" value={splitSourcePos} />
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div className="rounded-lg bg-white/5 p-2 border border-white/10"><p className="font-semibold text-white/50 mb-1">A</p><p className="text-white/70">{(item.sourceText || '').slice(0, splitSourcePos)}</p></div>
                    <div className="rounded-lg bg-white/5 p-2 border border-white/10"><p className="font-semibold text-white/50 mb-1">B</p><p className="text-white/70">{(item.sourceText || '').slice(splitSourcePos)}</p></div>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <label className="space-y-1"><span className="text-xs font-medium text-white/50">A 发言人</span><select className="form-input text-sm" onChange={(e) => setSplitSpeakerA(e.currentTarget.value)} value={splitSpeakerA}>{resource.speakerOptions.map((o) => <option key={o.id} value={o.id}>{speakerNames[o.id] ?? o.displayName}</option>)}</select></label>
                  <label className="space-y-1"><span className="text-xs font-medium text-white/50">B 发言人</span><select className="form-input text-sm" onChange={(e) => setSplitSpeakerB(e.currentTarget.value)} value={splitSpeakerB}>{resource.speakerOptions.map((o) => <option key={o.id} value={o.id}>{speakerNames[o.id] ?? o.displayName}</option>)}</select></label>
                </div>
                <button className="inline-flex rounded-lg bg-primary/80 px-4 py-2 text-sm font-semibold text-white transition hover:bg-primary disabled:opacity-50" disabled={isSplitting} onClick={async () => {
                  setIsSplitting(true)
                  try {
                    const result = await splitSegment({ projectDir: resource.projectDir, segmentId: item.segmentId, splitSourceIndex: splitSourcePos, splitCnIndex: 0, speakerA: splitSpeakerA, speakerB: splitSpeakerB, stage: 'speaker_review' })
                    if (result.success) { window.location.reload(); return }
                    setSubmitError('拆分未生效。')
                  } catch (error) { setSubmitError(`拆分失败: ${getErrorMessage(error)}`) } finally { setIsSplitting(false) }
                }} type="button">
                  {isSplitting ? '拆分中...' : '确认拆分'}
                </button>
              </div>
            ) : null}
          </article>
        ))}
      </div>
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
    <div className="rounded-2xl border border-red-500/20 bg-red-500/8 p-6 text-center">
      <p className="text-sm text-red-400">{message}</p>
    </div>
  )
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="rounded-2xl border border-red-500/20 bg-red-500/8 p-4 text-sm text-red-400">
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
        <button className="rounded-lg border border-white/10 bg-white/5 px-4 py-2 text-xs text-white/60 transition hover:bg-white/10 disabled:opacity-30" disabled={currentPage <= 1} onClick={() => { onPageChange(Math.max(1, currentPage - 1)); window.scrollTo({ top: 0, behavior: 'smooth' }) }} type="button">上一页</button>
        <button className="rounded-lg border border-white/10 bg-white/5 px-4 py-2 text-xs text-white/60 transition hover:bg-white/10 disabled:opacity-30" disabled={currentPage >= totalPages} onClick={() => { onPageChange(Math.min(totalPages, currentPage + 1)); window.scrollTo({ top: 0, behavior: 'smooth' }) }} type="button">下一页</button>
      </div>
    </div>
  )
}

function getErrorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '请求失败，请稍后重试。'
}
