"use client"

import { useEffect, useState } from 'react'
import { ChevronDown, ChevronUp, Loader2, Sparkles, CheckCircle2, AlertTriangle, XCircle } from 'lucide-react'

import { getSmartQualityReport } from '@/lib/api/jobs'
import { formatTimestamp } from '@/lib/format'
import type { SmartQualityReport } from '@/types/smart'

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
          <span>正在加载智能版决策摘要…</span>
        </header>
      </section>
    )
  }

  if (state.kind === 'error') {
    return (
      <section className="surface-card p-6 border border-border">
        <header className="flex items-center gap-2 text-sm text-muted-foreground">
          <AlertTriangle className="h-4 w-4 text-amber-500" />
          <span>智能版决策摘要加载失败：{state.message}</span>
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
            智能版尚未到达终态，决策摘要将在完成或转人工后显示。
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
            智能版决策摘要
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
            生成于 {formatTimestamp(report.generated_at)}
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
  const policyLabel = creditsPolicyLabel(report.smart_state_final.credits_policy)
  const reason = report.smart_state_final.reason
  return (
    <div className="rounded-lg border border-border bg-muted/30 p-4">
      <div className="flex flex-wrap gap-4 text-sm">
        <span>
          <span className="text-muted-foreground">计费策略：</span>
          <span className="font-medium text-foreground">{policyLabel}</span>
        </span>
        {reason ? (
          <span>
            <span className="text-muted-foreground">原因：</span>
            <span className="font-medium text-foreground">{reason}</span>
          </span>
        ) : null}
      </div>
    </div>
  )
}

function SpeakerSummarySection({ report }: { report: SmartQualityReport }) {
  const { speaker_summary } = report
  return (
    <div>
      <h4 className="mb-2 text-sm font-medium text-foreground">说话人识别</h4>
      <div className="rounded-lg border border-border bg-muted/20 p-3 text-sm">
        <p>
          <span className="text-muted-foreground">主说话人数量：</span>
          <span className="font-medium text-foreground">
            {speaker_summary.main_speaker_count}
          </span>
        </p>
        {speaker_summary.main_speaker_ids.length > 0 ? (
          <p className="mt-1">
            <span className="text-muted-foreground">主说话人：</span>
            <span className="font-medium text-foreground">
              {speaker_summary.main_speaker_ids.join('、')}
            </span>
          </p>
        ) : null}
        {speaker_summary.excluded_speakers.length > 0 ? (
          <div className="mt-2">
            <p className="text-muted-foreground">已排除：</p>
            <ul className="ml-4 list-disc">
              {speaker_summary.excluded_speakers.map((e) => (
                <li key={e.speaker_id}>
                  {e.speaker_id}（{e.reason}）
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
  if (report.voice_decisions.length === 0) {
    return null
  }
  return (
    <div>
      <h4 className="mb-2 text-sm font-medium text-foreground">音色决策</h4>
      <div className="overflow-hidden rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead className="bg-muted/40">
            <tr>
              <th className="px-3 py-2 text-left font-medium text-muted-foreground">
                说话人
              </th>
              <th className="px-3 py-2 text-left font-medium text-muted-foreground">
                决策
              </th>
              <th className="px-3 py-2 text-left font-medium text-muted-foreground">
                音色 ID
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
  const tr = report.translation_review
  if (!tr) {
    return null
  }
  return (
    <div>
      <h4 className="mb-2 text-sm font-medium text-foreground">翻译审核</h4>
      <div className="rounded-lg border border-border bg-muted/20 p-3 text-sm">
        <div className="flex items-center gap-2">
          {tr.auto_approved ? (
            <CheckCircle2 className="h-4 w-4 text-green-600" />
          ) : (
            <XCircle className="h-4 w-4 text-red-500" />
          )}
          <span className="font-medium text-foreground">
            {tr.auto_approved ? '自动通过' : `未通过：${tr.failed_check ?? '未知检查'}`}
          </span>
        </div>
      </div>
    </div>
  )
}

function RetrySummarySection({ report }: { report: SmartQualityReport }) {
  const rs = report.retry_summary
  return (
    <div>
      <h4 className="mb-2 text-sm font-medium text-foreground">重试统计</h4>
      <div className="grid grid-cols-3 gap-3">
        <RetryStat label="改写重试" value={rs.rewrite_attempts_used} unit="次" />
        <RetryStat label="重新合成" value={rs.retts_attempts_used} unit="次" />
        <RetryStat
          label="剩余预算"
          value={rs.budget_remaining_minutes}
          unit="分钟"
        />
      </div>
    </div>
  )
}

function HandoffHistorySection({ report }: { report: SmartQualityReport }) {
  if (report.handoff_history.length === 0) {
    return null
  }
  return (
    <div>
      <h4 className="mb-2 text-sm font-medium text-foreground">异常历史</h4>
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
  const meta = statusMeta(status)
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${meta.classes}`}>
      {meta.label}
    </span>
  )
}

function VoiceChoicePill({ choice }: { choice: string }) {
  if (choice === 'cloned') {
    return (
      <span className="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
        克隆
      </span>
    )
  }
  if (choice === 'preset') {
    return (
      <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
        预设
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

function statusMeta(status: string): { label: string; classes: string } {
  if (status === 'completed') {
    return {
      label: '已完成',
      classes:
        'bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-400',
    }
  }
  if (status === 'downgraded_to_studio') {
    return {
      label: '已转人工',
      classes:
        'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-400',
    }
  }
  if (status === 'fail_and_refunded') {
    return {
      label: '已退款',
      classes:
        'bg-red-100 text-red-700 dark:bg-red-500/15 dark:text-red-400',
    }
  }
  return {
    label: status,
    classes: 'bg-muted text-muted-foreground',
  }
}

function creditsPolicyLabel(policy: string): string {
  if (policy === 'capture_full') return '正常计费'
  if (policy === 'capture_partial') return '部分计费'
  if (policy === 'refund_full') return '全额退款'
  if (policy === 'pending_settle') return '待结算'
  return policy
}
