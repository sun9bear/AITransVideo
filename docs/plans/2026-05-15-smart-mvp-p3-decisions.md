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
  "pending_credits_charged": 1250,    // null until Gateway runs settle
  "credits_policy": "capture_full" | "pending_settle",
  "cost_breakdown_internal_only": {
    "asr_seconds": 45.2,
    "llm_translation_chars": 5234,
    "tts_chars": 8120,
    "voice_clone_calls": 1,
    "pending_minimax_quota_used_after": 1  // null until Gateway queries quota
  },
  "generated_at": "..."
}
```

**Field rename — Codex 第三十六轮 P2 (2026-05-15, P3-b-fix):**

- `credits_charged` → `pending_credits_charged`
- `cost_breakdown_internal_only.minimax_quota_used_after` →
  `cost_breakdown_internal_only.pending_minimax_quota_used_after`

Both fields are determined by Gateway AFTER pipeline terminal
(`settle_job_credit_ledger` runs post-pipeline; minimax quota is
queried via `/user-voices/quota`). Pipeline writes `None` for these.
Without the explicit `pending_` prefix, admin UI consumers may misread
`credits_charged: null` as "no credits charged" (free job). The prefix
signals "settle hasn't happened yet, value will appear after
backfill".

**Phase 2 backfill (P3-b-follow-up, not yet implemented):** Gateway
mirror_job_terminal_state hook should read the cost_summary.json
file after settling credit ledger + querying minimax quota, then
overwrite `pending_credits_charged` and
`pending_minimax_quota_used_after` with the real values. Until then,
admin UI must render `null` values as "Pending settle" UX state
rather than "0" / "—".

**Handoff vs happy-path — Codex 第三十六轮 P1 (2026-05-15, P3-b-fix):**

`credits_policy` values:

- `"capture_full"` — written at happy-path smart terminal (full
  pipeline ran to completion; user gets billed for full minutes).
- `"pending_settle"` — written at every smart handoff return site
  (eligibility reject / sample fail / quota brake / voice review /
  mirror fail / voice expiry / translation review). Gateway settle
  determines the actual policy (capture_full / capture_partial /
  refund_full) based on what work completed; Phase 2 backfill
  overwrites this field too.

Handoff sites write cost_summary via `_emit_smart_cost_summary_from_meter`
(meter probe wrapper around `_emit_smart_cost_summary`). All 7
``emit_handoff_markers`` call sites in process.py are wired —
regression-pinned by ``test_every_smart_handoff_site_writes_cost_summary``
and ``test_quota_brake_handoff_writes_cost_summary``.

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

### P3-a — quality_report write (happy-path ONLY)

**Scope-down (2026-05-15, during implementation):** original plan
called for emission at BOTH terminal AND handoff sites. Reduced to
happy-path terminal ONLY for these reasons:

  - Handoff sites already emit `downgrade_handoff` JSONL events via
    PR#3C-b3f sidecar wiring (with full reason + stage + timestamp +
    job_id audit fields).
  - Writing quality_report on handoff would be REDUNDANT with JSONL
    data — most sections (voice_decisions, translation_review) would
    be empty/None for early-exit jobs.
  - P3-c renderer naturally handles the bifurcation: read
    quality_report for happy-path (rich aggregate); fall back to
    JSONL events for handoff jobs (already structured as
    `{stage, reason, occurred_at}` triples).

**Implementation:**

- Add `_emit_smart_quality_report(project_dir, ...)` module-level
  helper in process.py paralleling `_emit_smart_audit`.
- Call at the MAIN-RUN happy-path return only (line ~4709, right
  after `_emit_smart_terminal_completion_marker`).
- Resume publish-only path (line ~5256, post-edit copy_as_new /
  overwrite) does NOT call — would clobber the original audit with
  empty re-publish data.
- Payload built from locals() at terminal:
  - `_smart_eligibility` (b3b) — speaker_summary
  - `_smart_voice_review.decisions` (b3d/e) — voice_decisions
  - `_smart_translation_decision.metrics` (b3c) — translation_review
  - `_smart_per_speaker_sample_seconds` — sample_seconds per speaker
  - retry budget zeros (P3-d will populate real values)
  - `handoff_history=[]` (happy-path; handoff data lives in JSONL)
- Use `locals().get(...)` for safe access — `requires_review=False`
  smart jobs reach terminal without smart inline branches having
  populated those vars.

**Helper schema** still supports the handoff-style payload (with
populated `handoff_history` + `smart_state_final.reason`); cycle-2
unit tests pin this as forward-compat for any future caller that
chooses to also write quality_report on early exit.

**Acceptance**: real E2E (re-submit job_ff21053d... pattern) writes
non-empty `audit/smart_quality_report.json` with happy-path sections
populated. Schema-conformance unit tests in
`test_smart_quality_report_writer.py` cover both happy-path and
handoff-shape payloads (latter exercises helper API without
process.py wiring it at handoff sites).

### P3-b — cost_summary write
- Pull from existing UsageMeter at terminal point (UsageMeter already
  tracks per-stage credit consumption).
- **Call sites (Codex 第三十六轮 P1, expanded scope):**
  - Happy-path smart terminal (same gate as quality_report:
    `service_mode==smart AND effective_pipeline_mode==smart`).
    `credits_policy="capture_full"`.
  - **All 7 smart handoff returns** (eligibility / sample / quota /
    voice review / mirror / voice expiry / translation review).
    `credits_policy="pending_settle"`.
- Add `gateway/admin_cost_api.py` thin route: `GET
  /api/admin/jobs/{id}/cost` returns the JSON.
- Frontend (P3-c scope): new
  `frontend-next/src/app/(app)/admin/jobs/[id]/cost/` page
  (matches `/admin/disk` pattern).

**Acceptance (Phase 1 — current P3-b-fix):**

- Every smart job (happy-path + handoff) has `audit/smart_cost_summary.json`
  written.
- File contains `pending_credits_charged: null` and
  `pending_minimax_quota_used_after: null` until Phase 2 backfill.
- Admin endpoint `/api/admin/jobs/{id}/cost` returns 200 + payload
  for any smart job (no more 404s on handoff jobs).
- Real E2E verified on quota-brake handoff + happy-path completion.

**Phase 2 follow-up (NOT in P3-b scope):**

- Gateway post-settle hook reads cost_summary.json, overwrites
  `pending_credits_charged` with `JobRecord.credits_captured` and
  `pending_minimax_quota_used_after` with `/user-voices/quota`
  response.
- Acceptance: settle-dependent fields become non-null after
  Gateway settle completes.

### P3-d — retry budget integration

**Scope-down (2026-05-15, during implementation):** the original
§P3-d assumed two retry loops in process.py needing
`retry_budget.try_consume()` wiring. Actual architecture (discovered
during exploration):

  - **Re-TTS retries live in `SegmentAligner.align_all()`** (aligner.py),
    not process.py. Gating is done by
    `PostTTSBudgetTracker.try_consume_for_segment()` with a hard
    `max_extra_tts_per_root` cap. The smart retry budget formula
    (`compute_total_budget_minutes`) is conceptually equivalent but
    not currently consulted.
  - **Pre-TTS rewrite is `_pre_rewrite_obvious_overshoot_segments_before_tts()`
    in process.py — a single-pass scan, not a retry loop.** Each segment
    either gets rewritten once or not at all. There's no "try again"
    branch to gate.
  - **`retry_budget.py` exports `evaluate_retry_request(snapshot, kind)`,
    NOT `try_consume()`.** It's a pure decision function; caller maintains
    state. Decision log's `try_consume()` reference was wrong.

**Revised scope (P3-d implementation):**

User-visible goal: smart quality_report has REAL `retry_summary` values
(not always-zero) + `budget_exhausted` sidecar events when alignment-stage
caps are hit, so renderer (P3-c) shows accurate retry history.

Implementation:

1. Add public `PostTTSBudgetTracker.usage_summary()` method exposing
   `{consumed_roots: dict[int, int], total_consumed: int, cap: int,
   exhausted_root_ids: list[int]}` (no more reaching into private
   `_usage_by_root`).
2. Add `_aggregate_smart_retry_stats(*, segments, post_tts_budget_tracker,
   source_minutes) -> dict` helper in process.py that builds
   `retry_summary` from real data:
   - `rewrite_attempts_used` — count of segments with
     `pre_tts_rewrite_retry_attempted=True`
   - `retts_attempts_used` — total from `usage_summary().total_consumed`
   - `budget_remaining_minutes` — from
     `compute_total_budget_minutes(source_minutes)` minus rough estimate
     of consumed minutes
3. Add `_emit_smart_budget_exhausted_events(*, project_dir,
   post_tts_budget_tracker, ...) -> int` helper that emits one
   `budget_exhausted` sidecar event per exhausted root segment.
4. Wire both at smart inline branch, AFTER alignment, BEFORE terminal:
   - Build real `retry_summary` and pass to `_emit_smart_quality_report`
     (replaces the always-zero placeholder).
   - Emit `budget_exhausted` events for each exhausted root.

**Deferred (P3-d+ future):**

Deep-wire `retry_budget.evaluate_retry_request()` into
`SegmentAligner.align_all()` so smart-mode jobs gate on the whole-task
budget formula in addition to per-segment caps. Current implementation
relies on PostTTSBudgetTracker's per-segment cap (which is functionally
similar but doesn't enforce the whole-task `min(1.5*minutes,
minutes+30)` rule). Defer until E2E shows the per-segment cap is
insufficient.

**Acceptance**: unit test for `_aggregate_smart_retry_stats` with
synthetic budget tracker + segments produces correct counts; unit
test for `_emit_smart_budget_exhausted_events` emits N events for N
exhausted roots; source-anchor test pins `retry_summary` at smart
terminal is built from `_aggregate_smart_retry_stats` (not zeros
hardcoded); E2E real smart job's quality_report has non-zero
retry counts when alignment had retries.

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
