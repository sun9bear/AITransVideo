---
id: T6-msg-001
task: T6
from: CodeX
to: Claude-Code
type: instruction
status: ready
priority: high
reply_to: T5-msg-004
requires_human: false
created_at: 2026-04-06 01:45 Asia/Shanghai
---

# v2 Task 6: baseline Billing UI on the existing `/settings/billing` route

## This file

This is the formal execution instruction for `Task 6`.

Current accepted baseline:

- `T0` accepted: gateway owns plan / trial / pricing truth
- `T1` accepted: `marketing / auth / app` split is in place
- `T2` accepted: marketing pages are live at current-stage quality
- `T3` accepted: phone-first public auth and trial bookkeeping are in place
- `T4` accepted: minimal subscription truth + billing-history truth are in place
- `T5` accepted: first paid checkout loop exists, fake path stays green, Alipay is truthfully gated

There is also an existing sidecar protocol for `/auth/login` visual cleanup:

- `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/Claude-Code/2026-04-06_004600_from-CodeX_to-Claude-Code_type-instruction_task-P1_auth-login-visual-polish.md`

That sidecar remains independent and must **not** be merged into `Task 6`.

## Core goal

Build the baseline Billing UI on top of the route and APIs that already exist.

This round should turn `/settings/billing` from:

- subscription snapshot + checkout card

into a basic, user-facing billing center that can already answer:

1. what plan the user is currently on
2. whether there is any current paid subscription state
3. what the recent billing history looks like
4. what just happened after a fake checkout redirect
5. where the user should click next if they want to upgrade

without expanding into full subscription-management scope.

## This round is not

This round is not:

- a payment-provider task
- a refund UX task
- a cancellation task
- an auto-renew / mandate task
- a usage-ledger task
- an auth polish task
- an admin billing task

Do not reopen `Task 5` unless you discover a concrete blocker in accepted code.

## Mandatory reading

1. `D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md`
2. `D:/Claude/AIVideoTrans_Codex_web_mvp/DESIGN.md`
3. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md`
4. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(app)/settings/billing/page.tsx`
5. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/billing/checkout-card.tsx`
6. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/lib/billing/get-subscription.ts`
7. `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/subscriptions.py`
8. `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/billing.py`
9. `D:/Claude/AIVideoTrans_Codex_web_mvp/DESIGN.md`
10. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/2026-04-05_235000_from-Trae_to-CodeX_type-report_task-WG5_auth-marketing-conversion-review.md`

## Current-stage constraints

You must preserve these facts:

- gateway remains the source of truth for plan, subscription, billing, and provider facts
- frontend consumes truth; it does not redefine commercial facts
- `trial` remains bookkeeping-only where facts are still unfrozen
- `T5` fake checkout redirect path and query params are already accepted behavior
- `PaymentOrder + PaymentWebhookEvent` remain compatibility shell
- `subscriptions + billing_invoices` remain billing truth layer
- Chinese payment-facing copy should read naturally and feel trustworthy

## What Task 6 must accomplish

Complete the smallest billing center that:

1. keeps using `/settings/billing` as the route
2. reads current subscription state from `/api/me/subscription`
3. reads billing history from `/api/billing/history`
4. handles the fake checkout redirect query params from `T5`
5. keeps the existing checkout card in place as the upgrade action area

## Critical architectural decisions

### 1. Build on the existing route, do not create a second billing entry

Hard boundary:

Do not create a new route such as:

- `/settings/subscription`
- `/billing`
- `/account/billing`

`/settings/billing` is already the accepted route from `T5`.
Extend it. Do not fork it.

### 2. Prefer frontend-only work unless a read-shape blocker is real

The current gateway already exposes:

- `GET /api/me/subscription`
- `GET /api/billing/history`
- `GET /api/billing/checkout-config`

Preferred direction:

- keep this round frontend-only
- add frontend helpers/components around the existing API shapes

Acceptable only if truly needed:

- small additive changes to authenticated **read** endpoint shapes in
  `gateway/subscriptions.py` or `gateway/billing.py`

If you touch gateway in this round, keep it:

- additive
- read-only
- migration-free
- truth-preserving

Do not touch settlement or provider logic.

### 3. Trial copy must remain honest

When rendering the billing center:

- do not invent trial duration or remaining-day numbers if `trial.ends_at` is null
- do not silently map trial to `Plus`
- do not imply auto-renew

If trial facts are partially absent, show a calm, honest fallback rather than synthetic precision.

### 4. Billing history should stay minimal

This round should show history clearly, but not overbuild.

Good scope:

- list/table of recent entries
- plan
- billing period
- amount
- provider
- status
- created / paid / issued time as available

Out of scope:

- pagination framework
- CSV/PDF export
- refund actions
- invoice download
- tax / 发票 workflow
- filters / search

### 5. Payment-result feedback belongs here now

`T5` introduced redirect query params such as:

- `?status=paid`
- `?status=already_settled`
- `?status=error&reason=order_not_found`

`Task 6` should consume these and present a minimal user-facing result:

- banner, toast, or equivalent small state surface

Do not leave the query params unused after this round.

## UI / UX expectations

Billing UI should follow `DESIGN.md` guardrails for app / billing / admin, not marketing:

- restrained
- workmanlike
- Chinese-first clarity
- no hero drama
- no oversized marketing typography
- no exaggerated motion

Trust cues are good if they are factual.
Examples:

- no auto-renew promise if still true
- provider currently available
- current plan state

Avoid:

- poetic selling language
- fake urgency
- visual treatment copied from marketing hero sections

## Recommended page structure

The exact component split is your call, but a good default is:

1. page title + short description
2. billing status banner
   - reacts to query params from `T5`
3. subscription summary
   - current plan / current period / provider / trial bookkeeping if relevant
4. checkout card
   - existing upgrade action area
5. order / invoice history

Keep the route readable on desktop and mobile.

## Allowed file changes

### Frontend page / components

- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(app)/settings/billing/page.tsx`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/billing/checkout-card.tsx`
- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/billing/subscription-summary.tsx`
- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/billing/order-history.tsx`
- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/billing/billing-status-banner.tsx`

### Frontend data helpers

- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/lib/billing/get-subscription.ts`
- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/lib/billing/get-order-history.ts`

### Optional only if truly needed

- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/globals.css`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/subscriptions.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/billing.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_billing.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_subscriptions.py`

Only touch gateway/tests if the current read shapes are a real blocker.

## Do not modify

- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/payment_providers.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/payment_provider_alipay.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth_phone.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/risk_control.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/plan_catalog.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/job_intercept.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/models.py`
- any Alembic migration
- any marketing page
- any `/auth/login` page work
- any admin page

Do not merge in the `/auth/login` sidecar.

## Required execution decisions

### 1. Client fetch remains the default in this round

Prefer client fetch for:

- `/api/me/subscription`
- `/api/billing/history`
- `/api/plans`
- `/api/billing/checkout-config`

Do not widen scope with SSR-safe helpers unless you hit a real blocker.

### 2. Order history empty state matters

If the user has no billing history yet:

- show a calm empty state
- do not show a broken/blank table

### 3. Status query params should be consumed then cleared or safely tolerated

You may choose either:

- read the query params and leave them in the URL harmlessly, or
- read them and then clean the URL client-side

Either is acceptable.

What matters is:

- the user sees a meaningful result state
- refresh/revisit behavior is not confusing

### 4. Subscription summary must stay factual

Good:

- current plan
- provider
- current billing period
- current period end if present
- trial bookkeeping when it exists

Not acceptable:

- invented remaining trial countdown from unfrozen facts
- implying cancellation or refund capability that doesn't exist yet

## Required test / verification coverage

### Frontend verification is mandatory

Run at least:

1. `npm run lint`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next`
2. `npm run build`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next`

### Gateway tests are only mandatory if you touch gateway

If you modify gateway read endpoints or their tests, also run:

3. `pytest tests/test_billing.py tests/test_subscriptions.py -q`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp`

### Browser/runtime check

Do one runtime/browser-style verification for `/settings/billing`:

- current subscription area renders
- empty or non-empty billing history area renders
- redirect status message from `?status=paid` or another accepted query param is visible
- console stays clean

If the preview environment blocks a full real verification, say exactly what was and was not verified.

### CLI baseline

Also run:

4. `python main.py --help`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp`

Report the actual exit/result honestly.

## Completion report

Write a new report back to:

- `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/`

Suggested filename:

- `2026-04-06_01xxxx_from-Claude-Code_to-CodeX_type-report_task-T6_stage-complete.md`

Your report must include at least:

1. execution scope
2. final `/settings/billing` page structure
3. whether gateway changes were avoided or not
4. how status query params are handled
5. how empty-state billing history is handled
6. how trial bookkeeping is presented without overclaiming
7. exact files changed
8. lint/build results
9. pytest results if gateway was touched
10. browser/runtime verification result
11. `python main.py --help` result
12. residual risks
13. explicit stop status

## Success criteria

This round is successful if:

- `/settings/billing` becomes a usable baseline billing center
- it shows current subscription state and billing history clearly
- it handles `T5` redirect status feedback in a user-facing way
- it stays inside app / billing guardrails rather than drifting toward marketing style
- it does not smuggle in cancellation, refund, auto-renew, mandate, or auth-polish scope

Stop after completion and wait for CodeX review.
