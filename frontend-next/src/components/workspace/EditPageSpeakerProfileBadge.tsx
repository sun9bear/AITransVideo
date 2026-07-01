"use client"

import { useTranslations } from "next-intl"

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
  {
    key: Parameters<ReturnType<typeof useTranslations<"appSpeakerBadge">>>[0]
    variant: "default" | "secondary" | "outline" | "destructive"
  }
> = {
  pending_segments: { key: "status.pending_segments", variant: "outline" },
  inferring: { key: "status.inferring", variant: "secondary" },
  ready: { key: "status.ready", variant: "default" },
  failed: { key: "status.failed", variant: "destructive" },
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
  const t = useTranslations("appSpeakerBadge")
  if (speaker.source === "baseline") return null

  const meta = STATUS_MAP[speaker.profile_status]
  if (!meta) return null

  return (
    <span className="inline-flex items-center gap-2">
      <Badge variant={meta.variant}>{t(meta.key)}</Badge>
      {speaker.profile_status === "failed" && onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="text-xs text-blue-500 hover:underline"
        >
          {t("retry")}
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
