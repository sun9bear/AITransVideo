// Smart MVP types — schema_version=1 contracts for the
// quality_report + cost_summary sidecars. Mirrors the locked shape in
// docs/plans/2026-05-15-smart-mvp-p3-decisions.md (§1, §2).

export interface SmartStateFinal {
  status: 'completed' | 'downgraded_to_studio' | 'fail_and_refunded' | string
  credits_policy:
    | 'capture_full'
    | 'capture_partial'
    | 'refund_full'
    | 'pending_settle'
    | string
  reason?: string | null
}

export interface SmartSpeakerSummary {
  main_speaker_count: number
  main_speaker_ids: string[]
  excluded_speakers: Array<{
    speaker_id: string
    reason: string
  }>
}

export interface SmartVoiceDecision {
  speaker_id: string
  choice: 'cloned' | 'preset' | string
  voice_id: string | null
  clone_provider?: string | null
  sample_seconds?: number | null
  smart_decision_id?: string | null
  fallback_reason?: string | null
}

export interface SmartTranslationReview {
  auto_approved: boolean
  failed_check: string | null
  metrics: Record<string, unknown>
}

export interface SmartRetrySummary {
  rewrite_attempts_used: number
  retts_attempts_used: number
  budget_remaining_minutes: number
}

export interface SmartHandoffEvent {
  stage: string
  reason: string
  occurred_at?: string
}

export interface SmartQualityReport {
  schema_version: 1
  job_id: string
  user_id: string
  service_mode: 'smart' | string
  smart_state_final: SmartStateFinal
  speaker_summary: SmartSpeakerSummary
  voice_decisions: SmartVoiceDecision[]
  translation_review: SmartTranslationReview | null
  retry_summary: SmartRetrySummary
  handoff_history: SmartHandoffEvent[]
  generated_at: string
}

// NOTE: admin-only ``SmartCostSummary`` shape lives in the admin route
// file ``app/(app)/admin/jobs/[id]/cost/page.tsx`` to satisfy the
// AST leak-guard ``test_no_cost_data_leak_in_workspace_frontend``
// (decision log §2: cost data MUST stay inside /admin/ subroutes).
