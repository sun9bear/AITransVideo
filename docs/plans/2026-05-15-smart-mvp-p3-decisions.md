# Smart MVP P3 — Decision Log

**Date**: 2026-05-15  
**Scope**: P3 follow-up to Smart MVP P2 (PR#3C-b3a-g, all merged to main).  
**Style**: lightweight decision log, NOT a full plan. Codex 第三十四轮 lesson:
real E2E catches what plan documents miss; over-investing in design docs
trades implementation/testing time for diminishing returns. Keep this short.

P3 is plumbing work — three already-implemented helpers
(`write_smart_quality_report`, `write_smart_cost_summary`,
`retry_budget`) need to be wired to existing pipeline / frontend hooks.
All schema versions are already locked in `sidecar_emitter.py`. This
log only captures the 3 decisions that aren't pre-determined by the
spec, plus the sub-PR sequence + acceptance criteria.

---

## Decision 1 — `smart_quality_report.json` payload schema

**Context**: `services.smart.sidecar_emitter.write_smart_quality_report()`
takes any `Mapping[str, Any]` and stamps `schema_version=1`. Plan §4.5
lists the field categories but no concrete shape. P3-c renderer will
read this — schema must be locked before P3-a writes anything.

**Decision**: lock the v1 payload as:

```jsonc
{
  "schema_version": 1,
  "job_id": "...",
  "user_id": "...",
  "service_mode": "smart",
  "smart_state_final": {
    "status": "completed" | "downgraded_to_studio" | "fail_and_refunded",
    "credits_policy": "capture_full" | "refund_full" | "capture_partial",
    "reason": "..."  // present when status != completed
  },
  "speaker_summary": {
    "main_speaker_count": 2,
    "main_speaker_ids": ["speaker_a", "speaker_b"],
    "excluded_speakers": [{"speaker_id": "...", "reason": "dubbing_mode_keep_original"}]
  },
  "voice_decisions": [
    {
      "speaker_id": "speaker_a",
      "choice": "cloned" | "preset",
      "voice_id": "vt_speaker_a_...",
      "clone_provider": "minimax_voice_clone",
      "sample_seconds": 29.1,
      "smart_decision_id": "..."  // links to sidecar JSONL event
    }
  ],
  "translation_review": {
    "auto_approved": true,
    "failed_check": null,
    "metrics": { /* full TranslationReviewDecision.metrics */ }
  },
  "retry_summary": {
    "rewrite_attempts_used": 0,
    "retts_attempts_used": 0,
    "budget_remaining_minutes": 12.3
  },
  "handoff_history": [
    // empty for happy path; populated when job hit handoff at any stage
    {"stage": "voice_selection_review", "reason": "...", "occurred_at": "..."}
  ],
  "generated_at": "2026-05-15T..."
}
```

**Rationale**: each field maps directly to data already produced in
the smart inline branch (eligibility decision / voice review decisions
/ translation review decision / retry budget). Renderer can compose
the full audit view from this single file without re-parsing the
JSONL. JSONL stays as the append-only source of truth; quality_report
is the pre-aggregated view.

**Alternative considered**: leave schema fully open and let renderer
re-aggregate from JSONL. Rejected because JSONL parsing on every page
load is wasteful and fragile (line-by-line schema drift).

---

## Decision 2 — `smart_cost_summary.json` admin boundary

**Context**: plan §7.3 + Codex Q2 say cost summary is admin-only —
shows internal margin / per-stage cost breakdown the user must NOT
see. Need to decide: where in the admin UI does it surface?

**Decision**:
- File written to `{project_dir}/audit/smart_cost_summary.json` for
  every smart job, regardless of completion (so admin can
  retrospectively diagnose handoff jobs).
- Frontend exposure: **`/admin/jobs/{job_id}/cost`** — new sub-route
  under existing `frontend-next/src/app/(app)/admin/`, server-side
  protected by admin role check.
- User-facing workspace (`/workspace/{id}`) NEVER shows cost data,
  even when accessed by admin. The admin route is the single
  authoritative view to prevent role-confusion bugs.

**Rationale**: matches the existing `/admin/disk` pattern (admin
sub-route under admin namespace). User-facing page stays focused on
the deliverable; admin gets the full cost picture.

**Schema** (compact):
```jsonc
{
  "schema_version": 1,
  "job_id": "...",
  "service_mode": "smart",
  "minutes_processed": 12.5,
  "credits_charged": 1250,
  "credits_policy": "capture_full",
  "cost_breakdown_internal_only": {
    "asr_seconds": 45.2,
    "llm_translation_chars": 5234,
    "tts_chars": 8120,
    "voice_clone_calls": 1,
    "minimax_quota_used_after": 1
  },
  "generated_at": "..."
}
```

---

## Decision 3 — QA renderer location on workspace page

**Context**: `/workspace/{id}/page.tsx` is single-page (no tabs).
Need to add the smart QA panel without disrupting existing layout.

**Decision**:
- New component: `<SmartAutoDecisionPanel />` in
  `frontend-next/src/components/workspace/`
- Conditionally rendered: only when
  `job.service_mode === "smart"`
- Position: **between `<ResultMediaCard />` and
  `<ResultDownloadList />`** (after media preview, before downloads)
- Collapsible card; default expanded for smart jobs (so user sees
  what was auto-decided without extra click)
- Reads ONLY `smart_quality_report.json` (Decision 1 schema). Does
  NOT read JSONL (audit is admin's job, not user-facing detail).
- API: new `GET /job-api/jobs/{id}/smart-quality-report` Job API
  endpoint that returns the JSON file content (or 404 for
  non-smart jobs / pre-P3 jobs).

**Rationale**: user-facing intent is "what did smart decide on my
behalf, and why" — quality_report is exactly that. JSONL detail is
audit-trail noise for the user.

---

## Sub-PR Sequence + Acceptance Criteria

Sequence is **P3-a → P3-b → P3-d → P3-c**: get all three sink data
flowing first (a/b), then retry budget integration adds the final
data point (d), then renderer (c) gets a complete dataset.

### P3-a — quality_report write
- Add `_emit_smart_quality_report(project_dir, ...)` helper in
  process.py paralleling `_emit_smart_audit`.
- Call at TWO sites:
  - `_emit_smart_terminal_completion_marker` (happy path completion)
  - Each `emit_handoff_markers()` call site (handoff is also terminal
    from smart's POV)
- Payload built from:
  - `_smart_eligibility` decision (b3b)
  - `_smart_voice_review.decisions` (b3d/e)
  - `_smart_translation_decision.metrics` (b3c)
  - `_smart_clone_mirror_failures` (b3e-fix)
  - retry budget snapshot (will be 0/0 for happy path until P3-d)

**Acceptance**: real E2E (re-submit job_ff21053d... pattern) writes
non-empty `audit/smart_quality_report.json` with all sections
populated. Schema-conformance unit test using a frozen
`SmartQualityReportV1` TypedDict.

### P3-b — cost_summary write
- Pull from existing UsageMeter at terminal point (UsageMeter already
  tracks per-stage credit consumption).
- Same call sites as P3-a (terminal + handoff branches).
- Add `gateway/admin_cost_api.py` thin route: `GET
  /api/admin/jobs/{id}/cost` returns the JSON.
- Frontend: new `frontend-next/src/app/(app)/admin/jobs/[id]/cost/`
  page (matches `/admin/disk` pattern).

**Acceptance**: real E2E job's cost_summary.json contains non-zero
credits_charged + matching minimax_quota_used_after value visible
via /admin/jobs/{id}/cost.

### P3-d — retry budget integration
- `services/smart/retry_budget.py` is implemented but NOT consumed
  by process.py rewrite/re-TTS retry loops.
- Wire 2 call sites:
  - Pre-TTS rewrite loop (process.py around line ~3530, "S5
    rewrite" stage)
  - Re-TTS loop (process.py around line ~5xxx, post-alignment retry)
- Each retry attempt MUST consume budget; budget exhausted →
  `emit_smart_decision(decision_type="budget_exhausted", ...)` +
  fall through to current behavior (no auto-retry, accept the
  current segment as-is or hand off to user via `needs_review=true`).

**Acceptance**: unit test pinning that retry loops check
`retry_budget.try_consume()` before each attempt; functional test
with intentionally over-budget payload triggers
`budget_exhausted` sidecar event + falls through correctly.

### P3-c — QA renderer on workspace page
- `<SmartAutoDecisionPanel />` component reads
  `smart_quality_report.json` via new API endpoint.
- Sections (collapsible):
  1. 智能版决策摘要 (top-line: status / credits / handoff if any)
  2. 说话人识别 (eligibility output)
  3. 音色决策 (voice_decisions table: speaker → cloned/preset + voice_id)
  4. 翻译审核 (6-check pass/fail)
  5. 重试统计 (retry_summary)
  6. 异常历史 (handoff_history; hidden if empty)
- Conditional render: `service_mode === "smart"`.

**Acceptance**: real smart job's workspace page shows the panel with
all 5-6 sections populated correctly. Non-smart jobs don't show the
panel. API returns 404 for non-smart jobs (frontend handles
gracefully).

---

## Out of Scope (P3+1 / future)

- Smart admin dashboard aggregating cost_summary across users
  (decision: defer until 50+ smart jobs in production, unclear ROI)
- Sidecar JSONL viewer in admin UI (admin can curl the file; no
  dedicated UI needed yet)
- `quality_report.json` downloadable artifact for users (defer until
  user requests it; on-page panel covers most cases)

---

## Codex Review Checkpoints

After each sub-PR commit, send to Codex with the same `CodeX审核意见`
pattern as P2. Pre-flag risks for each:

- **P3-a**: schema drift between writer payload and renderer reader
  (mitigate: TypedDict + functional integration test using fake
  payload)
- **P3-b**: cost data leak to non-admin (mitigate: AST guard that
  `/admin/jobs/{id}/cost` route handler has admin-role check)
- **P3-d**: retry loop infinite recursion if budget logic forgets to
  decrement (mitigate: hard cap at module level — already enforced
  by `_per_segment_cap` in retry_budget.py)
- **P3-c**: 渲染 non-smart jobs 时 API 404 让前端崩 (mitigate:
  swallow 404 → render nothing)

---

## When this log is wrong

Real E2E found P0s in P2 that 1700-line plan missed (b3g + b3g-fix).
If P3 implementation reveals a decision here is wrong, **update this
log inline + commit**, don't write a new plan. Single source of
truth.
