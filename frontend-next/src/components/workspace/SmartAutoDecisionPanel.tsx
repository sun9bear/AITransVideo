"use client"

import { useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { ChevronDown, ChevronUp, Loader2, Sparkles, CheckCircle2, AlertTriangle, XCircle } from 'lucide-react'

import { getSmartQualityReport } from '@/lib/api/jobs'
import { formatTimestamp } from '@/lib/format'
import type { SmartQualityReport } from '@/types/smart'

/** Translator scoped to the `appSmartAutoDecision` namespace（UI-06 part2 W3a）。 */
type SmartDecisionTranslator = ReturnType<typeof useTranslations<"appSmartAutoDecision">>

/**
 * Smart MVP P3-c: user-facing summary panel that renders the
 * ``smart_quality_report.json`` schema (decision log §1).
 *
 * Surface contract:
 *   - Conditional render: ``serviceMode === "smart"`` (caller passes
 *     this prop). For non-smart jobs the caller doesn't mount this
 *     component.
 *   - Endpoint resolution: the panel does its OWN fetch via
 *     ``getSmartQualityReport(jobId)``. The discriminated-union return
 *     lets us silently hide on ``not_smart``, show a "处理中" hint on
 *     ``not_written`` (handoff or in-flight), and render full sections
 *     on ``ok``.
 *   - Default expanded: smart users SHOULD see what was auto-decided
 *     without an extra click.
 *   - Never shows cost data — that's admin-only (Codex Q2 + decision
 *     log §2). Schema fields with admin semantics are absent from
 *     SmartQualityReport TypeScript shape.
 */

interface SmartAutoDecisionPanelProps {
  jobId: string
}

type FetchState =
  | { kind: 'loading' }
  | { kind: 'ok'; report: SmartQualityReport }
  | { kind: 'hidden' }  // not_smart — render nothing
  | { kind: 'in_flight' }  // not_written — show hint
  | { kind: 'error'; message: string }

export function SmartAutoDecisionPanel({ jobId }: SmartAutoDecisionPanelProps) {
  const ts = useTranslations('appSmartAutoDecision')
  const [state, setState] = useState<FetchState>({ kind: 'loading' })
  const [expanded, setExpanded] = useState(true)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const result = await getSmartQualityReport(jobId)
        if (cancelled) return
        if (result.kind === 'ok') {
          setState({ kind: 'ok', report: result.report })
        } else if (result.kind === 'not_smart') {
          setState({ kind: 'hidden' })
        } else if (result.kind === 'not_written') {
          setState({ kind: 'in_flight' })
        } else {
          setState({ kind: 'hidden' })  // unknown job → silently hide
        }
      } catch (err) {
        if (cancelled) return
        const message =
          err instanceof Error ? err.message : 'unknown error'
        setState({ kind: 'error', message })
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [jobId])

  if (state.kind === 'hidden') {
    return null
  }

  if (state.kind === 'loading') {
    return (
      <section className="surface-card p-6 border border-border">
        <header className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          <span>{ts('loading')}</span>
        </header>
      </section>
    )
  }

  if (state.kind === 'error') {
    return (
      <section className="surface-card p-6 border border-border">
        <header className="flex items-center gap-2 text-sm text-muted-foreground">
          <AlertTriangle className="h-4 w-4 text-amber-500" />
          <span>{ts('loadFailed', { msg: state.message })}</span>
        </header>
      </section>
    )
  }

  if (state.kind === 'in_flight') {
    // Codex 第三十八轮 P1: this state now only triggers for truly
    // in-flight smart jobs (no quality_report.json AND no
    // downgrade_handoff JSONL events). Handoff jobs flow through the
    // ``ok`` branch with status=downgraded_to_studio + populated
    // handoff_history, rendering the actionable "已转人工" view.
    return (
      <section className="surface-card p-6 border border-border">
        <header className="flex items-center gap-2 text-sm text-muted-foreground">
          <Sparkles className="h-4 w-4 text-primary" />
          <span>
            {ts('inFlight')}
          </span>
        </header>
      </section>
    )
  }

  const { report } = state
  return (
    <section className="surface-card p-6 border border-border">
      <header
        className="flex cursor-pointer items-center justify-between"
        onClick={() => setExpanded((v) => !v)}
      >
        <div className="flex items-center gap-2">
          <Sparkles className="h-5 w-5 text-primary" />
          <h3 className="text-lg font-semibold text-foreground">
            {ts('title')}
          </h3>
          <StatusPill status={report.smart_state_final.status} />
        </div>
        {expanded ? (
          <ChevronUp className="h-4 w-4 text-muted-foreground" />
        ) : (
          <ChevronDown className="h-4 w-4 text-muted-foreground" />
        )}
      </header>

      {expanded ? (
        <div className="mt-4 space-y-5">
          <TopSummarySection report={report} />
          <SpeakerSummarySection report={report} />
          <VoiceDecisionsSection report={report} />
          <TranslationReviewSection report={report} />
          <RetrySummarySection report={report} />
          <HandoffHistorySection report={report} />
          <footer className="text-xs text-muted-foreground">
            {ts('generatedAt', { time: formatTimestamp(report.generated_at) })}
          </footer>
        </div>
      ) : null}
    </section>
  )
}

// ===========================================================================
// Subsections — render small, contained, easy to test visually
// ===========================================================================

function TopSummarySection({ report }: { report: SmartQualityReport }) {
  const ts = useTranslations('appSmartAutoDecision')
  const policyLabel = creditsPolicyLabel(ts, report.smart_state_final.credits_policy)
  const reason = report.smart_state_final.reason
  return (
    <div className="rounded-lg border border-border bg-muted/30 p-4">
      <div className="flex flex-wrap gap-4 text-sm">
        <span>
          <span className="text-muted-foreground">{ts('billingPolicy')}</span>
          <span className="font-medium text-foreground">{policyLabel}</span>
        </span>
        {reason ? (
          <span>
            <span className="text-muted-foreground">{ts('reason')}</span>
            <span className="font-medium text-foreground">{reason}</span>
          </span>
        ) : null}
      </div>
    </div>
  )
}

function SpeakerSummarySection({ report }: { report: SmartQualityReport }) {
  const ts = useTranslations('appSmartAutoDecision')
  const { speaker_summary } = report
  return (
    <div>
      <h4 className="mb-2 text-sm font-medium text-foreground">{ts('speakerRecognition')}</h4>
      <div className="rounded-lg border border-border bg-muted/20 p-3 text-sm">
        <p>
          <span className="text-muted-foreground">{ts('mainSpeakerCount')}</span>
          <span className="font-medium text-foreground">
            {speaker_summary.main_speaker_count}
          </span>
        </p>
        {speaker_summary.main_speaker_ids.length > 0 ? (
          <p className="mt-1">
            <span className="text-muted-foreground">{ts('mainSpeakers')}</span>
            <span className="font-medium text-foreground">
              {speaker_summary.main_speaker_ids.join(ts('listSeparator'))}
            </span>
          </p>
        ) : null}
        {speaker_summary.excluded_speakers.length > 0 ? (
          <div className="mt-2">
            <p className="text-muted-foreground">{ts('excluded')}</p>
            <ul className="ml-4 list-disc">
              {speaker_summary.excluded_speakers.map((e) => (
                <li key={e.speaker_id}>
                  {ts('excludedItem', { id: e.speaker_id, reason: e.reason })}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </div>
  )
}

function VoiceDecisionsSection({ report }: { report: SmartQualityReport }) {
  const ts = useTranslations('appSmartAutoDecision')
  if (report.voice_decisions.length === 0) {
    return null
  }
  return (
    <div>
      <h4 className="mb-2 text-sm font-medium text-foreground">{ts('voiceDecisions')}</h4>
      <div className="overflow-hidden rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead className="bg-muted/40">
            <tr>
              <th className="px-3 py-2 text-left font-medium text-muted-foreground">
                {ts('speaker')}
              </th>
              <th className="px-3 py-2 text-left font-medium text-muted-foreground">
                {ts('decision')}
              </th>
              <th className="px-3 py-2 text-left font-medium text-muted-foreground">
                {ts('voiceId')}
              </th>
            </tr>
          </thead>
          <tbody>
            {report.voice_decisions.map((d) => (
              <tr key={d.speaker_id} className="border-t border-border">
                <td className="px-3 py-2 text-foreground">{d.speaker_id}</td>
                <td className="px-3 py-2">
                  <VoiceChoicePill choice={d.choice} />
                </td>
                <td className="px-3 py-2 font-mono text-xs text-foreground/80">
                  {d.voice_id || '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function TranslationReviewSection({ report }: { report: SmartQualityReport }) {
  const ts = useTranslations('appSmartAutoDecision')
  const tr = report.translation_review
  if (!tr) {
    return null
  }
  return (
    <div>
      <h4 className="mb-2 text-sm font-medium text-foreground">{ts('translationReview')}</h4>
      <div className="rounded-lg border border-border bg-muted/20 p-3 text-sm">
        <div className="flex items-center gap-2">
          {tr.auto_approved ? (
            <CheckCircle2 className="h-4 w-4 text-green-600" />
          ) : (
            <XCircle className="h-4 w-4 text-red-500" />
          )}
          <span className="font-medium text-foreground">
            {tr.auto_approved ? ts('autoApproved') : ts('notApproved', { check: tr.failed_check ?? ts('unknownCheck') })}
          </span>
        </div>
      </div>
    </div>
  )
}

function RetrySummarySection({ report }: { report: SmartQualityReport }) {
  const ts = useTranslations('appSmartAutoDecision')
  const rs = report.retry_summary
  return (
    <div>
      <h4 className="mb-2 text-sm font-medium text-foreground">{ts('retryStats')}</h4>
      <div className="grid grid-cols-3 gap-3">
        <RetryStat label={ts('rewriteRetry')} value={rs.rewrite_attempts_used} unit={ts('times')} />
        <RetryStat label={ts('resynthesize')} value={rs.retts_attempts_used} unit={ts('times')} />
        <RetryStat
          label={ts('budgetRemaining')}
          value={rs.budget_remaining_minutes}
          unit={ts('minutes')}
        />
      </div>
    </div>
  )
}

function HandoffHistorySection({ report }: { report: SmartQualityReport }) {
  const ts = useTranslations('appSmartAutoDecision')
  if (report.handoff_history.length === 0) {
    return null
  }
  return (
    <div>
      <h4 className="mb-2 text-sm font-medium text-foreground">{ts('handoffHistory')}</h4>
      <ul className="space-y-1 text-sm">
        {report.handoff_history.map((h, idx) => (
          <li
            key={idx}
            className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 dark:border-amber-500/30 dark:bg-amber-500/5"
          >
            <AlertTriangle className="mt-0.5 h-4 w-4 text-amber-500" />
            <div>
              <span className="font-medium text-foreground">{h.stage}</span>
              <span className="ml-2 text-muted-foreground">{h.reason}</span>
              {h.occurred_at ? (
                <span className="ml-2 text-xs text-muted-foreground/70">
                  {formatTimestamp(h.occurred_at)}
                </span>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}

// ===========================================================================
// Small presentational helpers
// ===========================================================================

function StatusPill({ status }: { status: string }) {
  const ts = useTranslations('appSmartAutoDecision')
  // 已知终态 → 本地化标签；未知 status 透传原值（content）。classes 与标签解耦（纯样式 helper）。
  const label =
    status === 'completed' ? ts('status.completed')
    : status === 'downgraded_to_studio' ? ts('status.downgraded')
    : status === 'fail_and_refunded' ? ts('status.refunded')
    : status
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${statusClasses(status)}`}>
      {label}
    </span>
  )
}

function VoiceChoicePill({ choice }: { choice: string }) {
  const ts = useTranslations('appSmartAutoDecision')
  if (choice === 'cloned') {
    return (
      <span className="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
        {ts('cloned')}
      </span>
    )
  }
  if (choice === 'preset') {
    return (
      <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
        {ts('preset')}
      </span>
    )
  }
  return (
    <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
      {choice}
    </span>
  )
}

function RetryStat({
  label,
  value,
  unit,
}: {
  label: string
  value: number
  unit: string
}) {
  return (
    <div className="rounded-lg border border-border bg-muted/20 p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-lg font-semibold text-foreground">
        {value}
        <span className="ml-1 text-xs font-normal text-muted-foreground">{unit}</span>
      </p>
    </div>
  )
}

function statusClasses(status: string): string {
  if (status === 'completed') {
    return 'bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-400'
  }
  if (status === 'downgraded_to_studio') {
    return 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-400'
  }
  if (status === 'fail_and_refunded') {
    return 'bg-red-100 text-red-700 dark:bg-red-500/15 dark:text-red-400'
  }
  return 'bg-muted text-muted-foreground'
}

// translator 线程化（纯函数拿不到 hook，沿用 part1 首参 translator 约定）；未知 policy 透传原值。
function creditsPolicyLabel(ts: SmartDecisionTranslator, policy: string): string {
  if (policy === 'capture_full') return ts('policy.full')
  if (policy === 'capture_partial') return ts('policy.partial')
  if (policy === 'refund_full') return ts('policy.refund')
  if (policy === 'pending_settle') return ts('policy.pending')
  return policy
}
