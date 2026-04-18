# V3 Session Handoff

## Why Start A New Session

Yes. A new session is the right move now.

The current thread has accumulated:

- the full `v2` commercialization rollout
- multiple production/staging drift investigations
- auth, billing, captcha, and pricing freeze work
- the first two `v3` phases plus review follow-ups

Starting fresh reduces context noise and lowers the risk that the next session:

- reopens already-closed `v2` issues
- confuses `v2 truth` with `v3 shadow` scope
- forgets which protocol files are already accepted
- reintroduces drift between Gateway truth and frontend display

This handoff is the working context for the next session.

## Project Snapshot

Project root:

- [AIVideoTrans_Codex_web_mvp](D:/Claude/AIVideoTrans_Codex_web_mvp)

Core project goal:

- Build a Python workflow whose main deliverable is **Jianying draft output**, not rendered MP4.

Non-negotiable architecture invariants from [AGENTS.md](D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md):

- TTS unit is `SemanticBlock`, not subtitle line.
- Alignment is DSP-first, rewrite loop second.
- Subtitle retiming is deterministic/mathematical, not LLM-driven.
- Pipeline target is Jianying draft output, not direct rendered MP4.
- Prefer minimal, testable, replaceable abstractions.

Commercialization/frontend execution-phase rules:

- Treat auth/billing/payment work as a staged migration, not a big-bang rewrite.
- Gateway remains the source of truth for plan catalog, trial rules, prices, entitlements, and now `v3` credits math.
- Frontend consumes those facts; it must not become the final source of pricing, entitlement, or credits truth.
- Prefer mocks/stubs/fakes in tests and local/default paths.
- Keep `main.py` and `pytest` runnable in a clean local environment.

## Mandatory Reading Order

The next session should start by reading these files in roughly this order:

### 1. Repo-wide operating rules

- [AGENTS.md](D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md)
- [CLAUDE.md](D:/Claude/AIVideoTrans_Codex_web_mvp/CLAUDE.md)
- [DESIGN.md](D:/Claude/AIVideoTrans_Codex_web_mvp/DESIGN.md)

### 2. V2 plan and frozen commercial facts

- [2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md)
- [2026-04-06-v3-credits-ledger-and-metering-plan.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-06-v3-credits-ledger-and-metering-plan.md)
- [2026-04-06-v3-pilot-observability-checklist.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-06-v3-pilot-observability-checklist.md)

### 3. Latest V3 protocol chain

- [2026-04-07_101500_from-CodeX_to-Claude-Code_type-instruction_task-V3-0-V3-1_shadow-ledger-bootstrap.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/Claude-Code/2026-04-07_101500_from-CodeX_to-Claude-Code_type-instruction_task-V3-0-V3-1_shadow-ledger-bootstrap.md)
- [2026-04-07_104500_from-CodeX_to-Claude-Code_type-instruction_task-V3-0-V3-1_minor-revision.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/Claude-Code/2026-04-07_104500_from-CodeX_to-Claude-Code_type-instruction_task-V3-0-V3-1_minor-revision.md)
- [2026-04-07_111500_from-CodeX_to-Claude-Code_type-instruction_task-V3-2_credits-read-surfaces.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/Claude-Code/2026-04-07_111500_from-CodeX_to-Claude-Code_type-instruction_task-V3-2_credits-read-surfaces.md)
- [2026-04-07_114500_from-CodeX_to-Claude-Code_type-instruction_task-V3-2_minor-revision.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/Claude-Code/2026-04-07_114500_from-CodeX_to-Claude-Code_type-instruction_task-V3-2_minor-revision.md)

### 4. Latest V3 completion reports

- [2026-04-07_from-Claude-Code_to-CodeX_type-completion-report_task-V3-0-V3-1_shadow-ledger-bootstrap.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/2026-04-07_from-Claude-Code_to-CodeX_type-completion-report_task-V3-0-V3-1_shadow-ledger-bootstrap.md)
- [2026-04-07_from-Claude-Code_to-CodeX_type-completion-report_task-V3-0-V3-1_minor-revision.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/2026-04-07_from-Claude-Code_to-CodeX_type-completion-report_task-V3-0-V3-1_minor-revision.md)
- [2026-04-07_from-Claude-Code_to-CodeX_type-completion-report_task-V3-2_credits-read-surfaces.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/2026-04-07_from-Claude-Code_to-CodeX_type-completion-report_task-V3-2_credits-read-surfaces.md)

## Current V3 Status

### V3 plan state

- `V3-0 / V3-1` are complete and accepted after a minor revision.
- `V3-2` has been implemented by Claude Code, but **is not yet accepted**.
- There is an open follow-up protocol for `V3-2`:
  - [2026-04-07_114500_from-CodeX_to-Claude-Code_type-instruction_task-V3-2_minor-revision.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/Claude-Code/2026-04-07_114500_from-CodeX_to-Claude-Code_type-instruction_task-V3-2_minor-revision.md)

### What V3 already has

- `CreditsBucket` and `CreditsLedger` schema
- shadow `grant / reserve / capture / release / rollback` service
- `Job.estimated_minutes`
- `Job.actual_minutes`
- partial `metering_snapshot`
- credits read APIs
- billing/workspace read surfaces for credits

### What V3 explicitly does **not** do yet

- `credits` are **not** the final billing/entitlement truth yet
- `quota.py` is **not** retired
- there is **no** top-up purchase flow yet
- there is **no** full rollback-driven entitlement productization yet
- `v2` still owns final user gating and production charging decisions

## Frozen V3 Commercial Facts

Current working values from [2026-04-06-v3-credits-ledger-and-metering-plan.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-06-v3-credits-ledger-and-metering-plan.md):

- `1 point cost-bearing capacity ~= RMB 0.015`
- `1 point retail ~= RMB 0.03`
- `Free = 500 credits`
- `Trial = 300 credits / 7 days`
- `Plus = 3500 credits / month`
- `Pro = 12000 credits / month`
- `Express = 10 credits / minute`
- `Studio basic = 15 credits / minute`
- `Studio high-quality = 30 credits / minute`
- `Studio flagship = 50 credits / minute`

Boundary:

- Current `v3` pricing **does not include voice cloning**
- voice cloning is a later separate priced add-on
- WeChat Pay is intentionally **not** in current `v3`

## Key Code To Read

### Gateway truth and current commercialization logic

- [gateway/plan_catalog.py](D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/plan_catalog.py)
- [gateway/entitlements.py](D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/entitlements.py)
- [gateway/job_intercept.py](D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/job_intercept.py)
- [gateway/quota.py](D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/quota.py)
- [gateway/billing.py](D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/billing.py)
- [gateway/subscriptions.py](D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/subscriptions.py)
- [gateway/auth_phone.py](D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth_phone.py)

### V3 schema and services

- [gateway/models.py](D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/models.py)
- [gateway/credits_service.py](D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/credits_service.py)
- [gateway/credits_read.py](D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/credits_read.py)
- [gateway/alembic/versions/009_add_credits_and_metering.py](D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/alembic/versions/009_add_credits_and_metering.py)

### Frontend read surfaces

- [frontend-next/src/app/(app)/settings/billing/page.tsx](D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(app)/settings/billing/page.tsx)
- [frontend-next/src/components/billing/credits-summary.tsx](D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/billing/credits-summary.tsx)
- [frontend-next/src/lib/billing/get-credits.ts](D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/lib/billing/get-credits.ts)
- [frontend-next/src/app/(app)/translations/new/page.tsx](D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(app)/translations/new/page.tsx)

### Tests

- [tests/test_credits_service.py](D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_credits_service.py)
- [tests/test_credits_read.py](D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_credits_read.py)
- [tests/test_gateway_job_policy.py](D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_gateway_job_policy.py)
- [tests/test_gateway_entitlements.py](D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_gateway_entitlements.py)

## The Two Most Important Open V3-2 Problems

These are the live unresolved issues that the next session should treat as the immediate blocker.

### 1. Workspace estimate must not hardcode credits facts

Problem:

- The workspace `CostEstimatePanel` recomputes credits in the frontend with local constants like `{ express: 10, studio: 15 }`.
- That violates the project rule that the Gateway is the source of truth for pricing, entitlements, and now credits math.

Required direction:

- frontend should call the Gateway estimate surface
- credits math must stay server-owned
- no duplicated credits schedule in client code

### 2. Read surfaces still lack a live grant path

Problem:

- `shadow_grant()` exists, but without a real grant path, normal users do not get real `CreditsBucket` rows
- `/api/me/credits` will therefore show `0` or hide UI for many real users
- that makes the read surfaces look complete while still not reflecting actual Free/Trial/Subscription balances

Required direction:

- add minimal live shadow grant for real users
- enough that Free / Trial / Subscription users see meaningful non-zero credits
- still do **not** switch final `v2` truth

## Easy Pitfalls To Avoid

These are the mistakes we have already seen, or nearly made, and should not repeat.

### Scope drift

- Do not smuggle `topup purchase`, `quota retirement`, or `credits becomes final truth` into a read-surface or shadow-ledger task.
- Later-stage work must stay later-stage.

### Frontend truth drift

- Do not hardcode pricing, plan, trial, entitlement, or credits math in the frontend.
- If the Gateway already has a surface for the data, consume it.

### Overclaiming observability

- Distinguish between:
  - fields actually being written in production
  - fields merely reserved in schema/comments for later use
- Reports must say which is live and which is only preallocated.

### Shadow flow safety

- Shadow ledger failures must never block main user flows in current phases.
- `shadow_safe()` isolation is intentional and must remain intact until a later cutover.

### Estimated vs actual metering confusion

- `estimated_minutes` is a planning/estimation signal
- `actual_minutes` is measured result
- do not overwrite one with the other

### File existence assumptions

- When drafting new protocol files, actually create the file before announcing that it exists.
- This already caused one avoidable coordination break.

### Preview overconfidence

- The frontend preview often lacks real auth session + running Gateway.
- "Build passes and preview renders" is not enough to claim live integration correctness.

### Doc/code drift

- If Gateway truth changes, update docs/comments/contracts together.
- We already had to repair drift around trial freeze and pricing docs.

## How To Work With Claude Code

Claude Code is being used as the implementation worker.

Expected workflow:

1. CodeX drafts a scoped protocol file into:
   - [docs/plans/AI-workgroup/inbox/Claude-Code](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/Claude-Code)
2. Claude Code executes that protocol and writes a report into:
   - [docs/plans/AI-workgroup/inbox/CodeX](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX)
3. CodeX reviews the code and report, then either:
   - accepts it
   - or writes a narrow follow-up protocol

Working rules:

- Keep Claude Code on a narrow write scope.
- Prefer one small protocol per stage.
- When reviewing, prioritize:
  - correctness
  - truth-source boundaries
  - migration safety
  - missing tests
  - scope drift

## How To Work With The Project Developer

The project developer is the commercial/product decision-maker.

The developer should be asked to confirm or freeze:

- pricing facts
- trial rules
- package sizes
- payment/operations choices
- whether a later-stage feature is actually in scope

Do not make quiet product decisions alone if they have non-obvious revenue or migration impact.

Good behavior:

- summarize tradeoffs
- propose a narrow next stage
- keep architecture reversible

Bad behavior:

- silently broadening scope
- silently changing frozen pricing
- treating `v3` shadow numbers as final production truth before cutover

## How The AI Collaboration System Works

Current collaboration pattern:

- **CodeX**: protocol drafting, review, scope control, integration judgment
- **Claude Code**: implementation worker
- **Trae**: optional design/copy/UX review sidecar when needed, not main code implementer

Directory roles:

### Protocols sent to Claude Code

- [docs/plans/AI-workgroup/inbox/Claude-Code](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/Claude-Code)

### Reports returned by Claude Code

- [docs/plans/AI-workgroup/inbox/CodeX](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX)

### Human decision templates / input

- [docs/plans/AI-workgroup/inbox/Human](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/Human)

### Optional Trae tasks

- [docs/plans/AI-workgroup/inbox/Trae](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/Trae)

General rule:

- Protocols are the scoped contract.
- Reports are not self-accepting.
- CodeX review decides whether a stage is actually done.

## Recommended Opening For The Next Session

If starting a fresh CodeX session, begin with a prompt close to this:

> We are continuing the `v3` credits/ledger migration in `D:\\Claude\\AIVideoTrans_Codex_web_mvp`. Read [AGENTS.md](D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md), [CLAUDE.md](D:/Claude/AIVideoTrans_Codex_web_mvp/CLAUDE.md), [DESIGN.md](D:/Claude/AIVideoTrans_Codex_web_mvp/DESIGN.md), [2026-04-06-v3-credits-ledger-and-metering-plan.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-06-v3-credits-ledger-and-metering-plan.md), [2026-04-06-v3-pilot-observability-checklist.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-06-v3-pilot-observability-checklist.md), and the latest `v3` protocol/report chain in [docs/plans/AI-workgroup/inbox/Claude-Code](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/Claude-Code) and [docs/plans/AI-workgroup/inbox/CodeX](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX). Treat `v2` as current truth, treat `v3` as staged migration, and pick up from the open `V3-2` minor revision before proposing the next phase.

## Immediate Next Step

The next session should start from:

- [2026-04-07_114500_from-CodeX_to-Claude-Code_type-instruction_task-V3-2_minor-revision.md](D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/Claude-Code/2026-04-07_114500_from-CodeX_to-Claude-Code_type-instruction_task-V3-2_minor-revision.md)

And should not proceed to the next `v3` stage until:

- workspace estimate consumes Gateway truth
- minimal live shadow grant path exists
- `V3-2` is re-reviewed and accepted

