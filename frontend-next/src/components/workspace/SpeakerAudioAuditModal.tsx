'use client'

// Moved verbatim out of VoiceSelectionPanel.tsx (2026-06-11) — the modal
// shares no state with the panel; it talks to the speaker-audio endpoints
// directly. Consumers: VoiceSelectionPanel and the editing-state voice Tab
// (VoiceModifyTab, readOnly mode).
// Editing 模式 reassign + keep-original 后端端点要求 voice_selection_review
// 未 approved → editing 状态会 409。readOnly=true 隐藏这两个控件，只保留
// 播放 + 段信息显示。

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { getErrorMessage } from '@/lib/api/errors'
import { formatTimecode } from '@/lib/format'
import {
  getSpeakerAudioSegments,
  reassignSpeakerAudioSegment,
  updateSpeakerAudioDubbingMode,
  type SpeakerAudioSegment,
  type SpeakerAudioReassignResult,
} from '@/lib/api/voiceSelection'

export interface SpeakerAudioAuditModalSpeakerRef {
  speakerId: string
  speakerName: string
}

export interface SpeakerAudioAuditModalProps {
  jobId: string
  speaker: SpeakerAudioAuditModalSpeakerRef
  speakerOptions: Array<{ speakerId: string; speakerName: string }>
  onClose: () => void
  onReassigned: (result: SpeakerAudioReassignResult) => void
  /** 2026-05-09: editing 模式只读 — 隐藏 reassign / keep-original 控件,
   * 只保留播放 + 段信息显示。reassign/keep-original 改动用 翻译修改 Tab。 */
  readOnly?: boolean
}

export function SpeakerAudioAuditModal({
  jobId,
  speaker,
  speakerOptions,
  onClose,
  onReassigned,
  readOnly = false,
}: SpeakerAudioAuditModalProps) {
  const [segments, setSegments] = useState<SpeakerAudioSegment[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reassigningSegmentId, setReassigningSegmentId] = useState<number | null>(null)
  const [updatingDubbingModeSegmentId, setUpdatingDubbingModeSegmentId] = useState<number | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [playingSegmentId, setPlayingSegmentId] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        setIsLoading(true)
        setError(null)
        const result = await getSpeakerAudioSegments(jobId, speaker.speakerId)
        if (!cancelled) {
          setSegments([...result.segments].sort((a, b) => a.startMs - b.startMs))
        }
      } catch (err) {
        if (!cancelled) setError(getErrorMessage(err))
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    load()
    return () => { cancelled = true }
  }, [jobId, speaker.speakerId])

  const playSegment = useCallback((seg: SpeakerAudioSegment) => {
    if (audioRef.current) { audioRef.current.pause(); audioRef.current = null }
    if (playingSegmentId === seg.segmentId) { setPlayingSegmentId(null); return }
    const audio = new Audio(seg.audioUrl)
    audio.onended = () => setPlayingSegmentId(null)
    audio.onerror = () => setPlayingSegmentId(null)
    audio.play().catch(() => setPlayingSegmentId(null))
    audioRef.current = audio
    setPlayingSegmentId(seg.segmentId)
  }, [playingSegmentId])

  const handleReassign = useCallback(async (seg: SpeakerAudioSegment, toSpeakerId: string) => {
    if (!toSpeakerId || toSpeakerId === speaker.speakerId || reassigningSegmentId) return
    setReassigningSegmentId(seg.segmentId)
    setError(null)
    try {
      const result = await reassignSpeakerAudioSegment({
        jobId,
        segmentId: seg.segmentId,
        fromSpeakerId: speaker.speakerId,
        toSpeakerId,
      })
      onReassigned(result)
      setSegments((prev) => prev.filter((item) => item.segmentId !== seg.segmentId))
      if (audioRef.current && playingSegmentId === seg.segmentId) {
        audioRef.current.pause()
        audioRef.current = null
        setPlayingSegmentId(null)
      }
    } catch (err) {
      setError(getErrorMessage(err))
    } finally {
      setReassigningSegmentId(null)
    }
  }, [jobId, onReassigned, playingSegmentId, reassigningSegmentId, speaker.speakerId])

  const handleDubbingModeChange = useCallback(async (seg: SpeakerAudioSegment, keepOriginal: boolean) => {
    if (updatingDubbingModeSegmentId) return
    const nextMode = keepOriginal ? 'keep_original' : 'dub'
    if (seg.dubbingMode === nextMode) return
    setUpdatingDubbingModeSegmentId(seg.segmentId)
    setError(null)
    setSegments((prev) => prev.map((item) => (
      item.segmentId === seg.segmentId ? { ...item, dubbingMode: nextMode } : item
    )))
    try {
      const result = await updateSpeakerAudioDubbingMode({
        jobId,
        segmentId: seg.segmentId,
        speakerId: speaker.speakerId,
        dubbingMode: nextMode,
      })
      setSegments((prev) => prev.map((item) => (
        item.segmentId === result.segmentId
          ? { ...item, dubbingMode: result.dubbingMode }
          : item
      )))
    } catch (err) {
      setSegments((prev) => prev.map((item) => (
        item.segmentId === seg.segmentId ? { ...item, dubbingMode: seg.dubbingMode } : item
      )))
      setError(getErrorMessage(err))
    } finally {
      setUpdatingDubbingModeSegmentId(null)
    }
  }, [jobId, speaker.speakerId, updatingDubbingModeSegmentId])

  useEffect(() => {
    return () => { if (audioRef.current) { audioRef.current.pause(); audioRef.current = null } }
  }, [])

  const totalDuration = useMemo(() => {
    return segments.reduce((sum, seg) => sum + seg.durationS, 0)
  }, [segments])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="w-full max-w-3xl max-h-[85vh] flex flex-col rounded-xl bg-card border border-border shadow-xl">
        <div className="flex items-center justify-between p-4 border-b border-border">
          <h3 className="text-base font-semibold text-foreground">核对原音 — {speaker.speakerName}</h3>
          <button className="text-slate-400 hover:text-foreground transition" onClick={onClose} type="button">
            <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M6 18L18 6M6 6l12 12" strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} /></svg>
          </button>
        </div>
        <div className="flex items-center gap-4 px-4 py-2 bg-slate-50/50 dark:bg-slate-800/30">
          <span className="text-xs text-slate-500">共 <span className="font-medium text-foreground">{segments.length}</span> 段</span>
          <span className="text-xs text-slate-500">总时长 <span className="font-medium text-foreground">{totalDuration.toFixed(1)}s</span></span>
          <span className="text-xs text-slate-400">按时间排序，修改后会立即保存。</span>
        </div>
        <div className="flex-1 overflow-y-auto p-4 space-y-1">
          {isLoading ? (
            <div className="text-center py-8 text-sm text-slate-400">加载原音片段...</div>
          ) : segments.length === 0 ? (
            <div className="text-center py-8 text-sm text-slate-400">当前说话人没有待核对片段</div>
          ) : segments.map((seg) => {
            const isPlaying = playingSegmentId === seg.segmentId
            const isReassigning = reassigningSegmentId === seg.segmentId
            const isUpdatingMode = updatingDubbingModeSegmentId === seg.segmentId
            return (
              <div className="flex items-center gap-3 rounded-lg border border-transparent px-3 py-2 transition hover:bg-slate-50 dark:hover:bg-slate-800/40" key={seg.segmentId}>
                <button className="h-7 w-7 rounded-full border border-border flex items-center justify-center shrink-0 hover:bg-muted transition" onClick={() => playSegment(seg)} type="button">
                  {isPlaying ? <svg className="h-3 w-3 text-[color:var(--cinnabar)]" fill="currentColor" viewBox="0 0 24 24"><rect height="16" rx="1" width="4" x="6" y="4" /><rect height="16" rx="1" width="4" x="14" y="4" /></svg> : <svg className="h-3 w-3 text-slate-500" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>}
                </button>
                <span className="w-12 shrink-0 text-xs tabular-nums text-slate-400">{formatTimecode(seg.startMs)}</span>
                <span className="min-w-0 flex-1 truncate text-xs text-foreground">{seg.sourceText || `片段 ${seg.segmentId}`}</span>
                <span className="w-12 shrink-0 text-right text-xs text-slate-400">{seg.durationS.toFixed(1)}s</span>
                {readOnly ? null : (
                  <>
                    <label className="flex h-8 w-[102px] shrink-0 items-center justify-center gap-1 rounded border border-slate-300 dark:border-slate-600 px-2 text-xs text-slate-600 dark:text-slate-300">
                      <input
                        checked={seg.dubbingMode === 'keep_original'}
                        className="h-3.5 w-3.5 accent-[color:var(--cinnabar)]"
                        disabled={isUpdatingMode || updatingDubbingModeSegmentId !== null}
                        onChange={(event) => { void handleDubbingModeChange(seg, event.target.checked) }}
                        type="checkbox"
                      />
                      保留原音
                    </label>
                    <select
                      className="h-8 w-[150px] shrink-0 rounded border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 px-2 text-xs text-foreground disabled:opacity-50"
                      disabled={isReassigning || reassigningSegmentId !== null}
                      onChange={(event) => { void handleReassign(seg, event.target.value) }}
                      value={speaker.speakerId}
                    >
                      {speakerOptions.map((option) => (
                        <option key={option.speakerId} value={option.speakerId}>
                          {option.speakerName || option.speakerId}
                        </option>
                      ))}
                    </select>
                  </>
                )}
              </div>
            )
          })}
        </div>
        <div className="flex items-center justify-between p-4 border-t border-border">
          <span className="text-xs text-slate-400">
            {readOnly
              ? '试听原音以核对说话人归属。需修改归属或保留原音请到「翻译修改」Tab 在段落上操作。'
              : '可修改说话人归属，也可让片段跳过翻译配音并保留原音。'}
          </span>
          <div className="flex items-center gap-2">
            {error ? <span className="text-xs text-[color:var(--cinnabar)] max-w-[280px] truncate">{error}</span> : null}
            <button className="h-8 rounded px-4 text-sm text-slate-500 transition hover:text-foreground" onClick={onClose} type="button">关闭</button>
          </div>
        </div>
      </div>
    </div>
  )
}
