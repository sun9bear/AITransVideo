"use client"

import type { EditingSpeaker } from "@/lib/api/editing"
import { Badge } from "@/components/ui/badge"

interface Props {
  speaker: EditingSpeaker
  /**
   * Click handler for the "重试" button — only rendered when
   * ``speaker.profile_status === 'failed'`` AND ``onRetry`` is supplied.
   * Parent is responsible for the actual ``retryEditingSpeakerProfile``
   * call + refetching the speakers list.
   */
  onRetry?: () => void
}

const STATUS_MAP: Record<
  EditingSpeaker["profile_status"],
  { text: string; variant: "default" | "secondary" | "outline" | "destructive" }
> = {
  pending_segments: { text: "待归属段落", variant: "outline" },
  inferring: { text: "音色画像推断中...", variant: "secondary" },
  ready: { text: "音色画像就绪", variant: "default" },
  failed: { text: "推断失败", variant: "destructive" },
}

/**
 * Profile-status pill for editing-mode speakers (Task 7 plan
 * ``2026-05-09-studio-editing-add-speaker``).
 *
 * Baseline speakers (``source === 'baseline'``) never go through the
 * editing-mode profile inference flow, so no pill is shown for them.
 * Editing-mode speakers always show a pill matching their backend
 * ``profile_status``; ``failed`` adds a "重试" button + a hover-tooltip
 * for the raw error string.
 */
export function EditPageSpeakerProfileBadge({ speaker, onRetry }: Props) {
  if (speaker.source === "baseline") return null

  const meta = STATUS_MAP[speaker.profile_status]
  if (!meta) return null

  return (
    <span className="inline-flex items-center gap-2">
      <Badge variant={meta.variant}>{meta.text}</Badge>
      {speaker.profile_status === "failed" && onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="text-xs text-blue-500 hover:underline"
        >
          重试
        </button>
      )}
      {speaker.profile_status === "failed" && speaker.profile_error && (
        <span
          className="text-xs text-muted-foreground truncate max-w-[200px]"
          title={speaker.profile_error}
        >
          {speaker.profile_error}
        </span>
      )}
    </span>
  )
}
