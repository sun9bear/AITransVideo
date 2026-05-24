---
id: T4-msg-001
task: T4
from: CodeX
to: Claude-Code
type: instruction
status: ready
priority: high
reply_to: T3-msg-003
requires_human: false
created_at: 2026-04-05 23:45 Asia/Shanghai
---

# v2 Task 4: minimal subscription truth source and PaymentOrder migration layer

## This file

This is the formal execution instruction for `Task 4`.

Current accepted baseline:

- `T0` accepted: gateway plan / trial truth source exists
- `T1` accepted: `marketing / auth / app` route split exists
- `T2` accepted: marketing pages are live at current-stage quality
- `T3` accepted: phone-first public auth path and trial bookkeeping exist

Your job in this round is to move the project from "can register" toward "can charge",
but only by building the smallest subscription truth source needed for later checkout and billing UI.

This round is not `Task 5`.
This round is not `Task 6`.
Do not start real payment-channel work.
Do not start billing UI.

## Core goal

Build a minimal, migration-safe billing truth layer so the system can express:

- current paid subscription state
- payment history / invoice history
- the compatibility relationship between `PaymentOrder` and the new subscription layer

without deleting or hard-replacing the existing `PaymentOrder + PaymentWebhookEvent` generation.

## Mandatory reading

1. `D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md`
2. `D:/Claude/AIVideoTrans_Codex_web_mvp/DESIGN.md`
3. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md`
4. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/specs/2026-04-04-pricing-and-plans-api-contract.md`
5. `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/models.py`
6. `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/billing.py`
7. `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/plan_catalog.py`
8. `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth.py`
9. `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/main.py`
10. `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/alembic/versions/007_add_phone_and_trial_fields.py`
11. `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_billing.py`
12. `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_gateway_entitlements.py`

## Current-state constraints

You must preserve these facts:

- gateway remains the source of truth
- frontend does not become the final subscription or pricing truth source
- `PaymentOrder` still exists and is still the checkout / webhook compatibility shell
- `trial_granted_at` and `trial_ends_at` already exist from `T3`
- `trial_ends_at` may still be `NULL`
- `plan_catalog.TRIAL_CONFIG.frozen` is still `false`
- current code must remain runnable in a clean local environment

## What Task 4 must accomplish

Complete the smallest backend truth layer that allows later tasks to rely on:

1. a stable paid-subscription record
2. a stable billing-history record
3. authenticated read APIs for current subscription state and billing history
4. webhook settlement that updates this new truth layer without breaking existing `PaymentOrder` behavior

## Critical architectural decisions

### 1. `subscriptions` is the paid-subscription source of truth

Introduce a minimal `subscriptions` model/table.

It should represent paid subscription state only.

It must not become a dumping ground for:

- full usage ledger
- team seats
- reviewer seats
- mandate lifecycle
- second payment channel
- top-up balance

Keep it intentionally small and replaceable.

### 2. `billing_invoices` is the billing-history source of truth

Introduce a minimal `billing_invoices` model/table.

It should support later Billing UI needs such as:

- invoice/order history listing
- amount
- provider
- billing period
- plan
- paid / failed / refunded style status
- created / paid timestamps

Do not try to implement a full accounting system in this round.

### 3. `PaymentOrder` stays as a compatibility shell

This is a hard boundary.

Do not remove `PaymentOrder`.
Do not remove `PaymentWebhookEvent`.
Do not rewrite the payment layer as a big-bang migration.

Task 4 should instead make the role split explicit:

- `PaymentOrder`: checkout and webhook compatibility shell
- `PaymentWebhookEvent`: idempotency and audit for provider callbacks
- `subscriptions`: current paid subscription truth
- `billing_invoices`: user-visible billing history truth

### 4. Trial remains separate from paid subscription

Do not silently map trial to `plus`.
Do not create fake paid subscriptions for trial users.
Do not freeze trial duration, quota, or entitlement details here.

For this round:

- trial stays managed by `users.trial_granted_at / trial_ends_at`
- `subscriptions` should be created only after a successful paid order settles
- if `trial_ends_at` is `NULL`, do not invent a countdown or synthetic end date

If current business facts are insufficient to express a true `trialing` state safely,
prefer an honest minimal response shape over inventing new rules.

## Recommended minimal API shape

### 1. `GET /api/me/subscription`

Authenticated endpoint.

Purpose:

- provide the current user's subscription-related state to later billing UI
- make the current paid subscription explicit
- surface trial bookkeeping only as bookkeeping, not as frozen commercial claims

Recommended output shape:

- `plan_code`
- `subscription_status`
- `subscription`
- `trial`

Where:

- `subscription_status` should stay small and deterministic
- `trial` may expose bookkeeping facts such as `granted_at` and `ends_at`
- if trial end facts are not frozen, return `ends_at: null` rather than inventing one

### 2. `GET /api/billing/history`

Authenticated endpoint.

Purpose:

- expose a stable list for later Billing UI
- allow later Task 6 to render history without scraping `PaymentOrder` directly

Keep it minimal.
Do not add filters, pagination frameworks, exports, or admin-only variants unless required for tests.

## Allowed file changes

### Gateway

- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/models.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/billing.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/main.py`
- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/subscriptions.py`
- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/alembic/versions/008_add_subscriptions_minimal.py`

### Tests

- Create: `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_subscriptions.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_billing.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/tests/test_gateway_entitlements.py`

### Optional only if truly needed

- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth.py`

Only do this if the authenticated response shape cannot stay compatible otherwise.

## Do not modify

- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/plan_catalog.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/job_intercept.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth_phone.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/risk_control.py`
- any payment provider adapter implementation
- any marketing page
- any auth page
- any billing UI or app page in `frontend-next`

Do not expand `Task 4` into `Task 5` or `Task 6`.

## Required execution decisions

### 1. Migration numbering

The v2 plan text still mentions `007_add_subscriptions_minimal.py`,
but `007` has already been used by `T3` for phone auth.

Therefore this round must use:

- `008_add_subscriptions_minimal.py`

Do not reuse or overwrite the existing `007` migration.

### 2. Keep schema minimal

Recommended `subscriptions` fields should stay near the minimum set needed for current work, such as:

- id
- user_id
- plan_code
- billing_period
- provider
- status
- started_at
- current_period_start
- current_period_end
- cancelled_at
- created_at
- updated_at

Recommended `billing_invoices` fields should stay near the minimum set needed for user-visible history, such as:

- id
- user_id
- subscription_id
- payment_order_id
- provider
- provider_order_id
- plan_code
- billing_period
- amount_cny
- currency
- status
- issued_at
- paid_at
- created_at
- updated_at

You may trim or slightly adjust this set if the final shape is cleaner,
but do not inflate it with later-stage scope.

### 3. Settlement order

When a paid payment event is processed:

1. update `PaymentOrder`
2. write or update `billing_invoices`
3. create or update `subscriptions`
4. only then expose updated user-visible subscription state

Preserve webhook idempotency.
Duplicate webhook events must not create duplicate invoices or duplicate subscriptions.

### 4. User entitlement compatibility

This round may need to keep `user.plan_code` compatible with existing gates.

If you continue to update `user.plan_code` after successful payment, that is acceptable for this phase,
but the new truth source must still be `subscriptions`, not the reverse.

In other words:

- `subscriptions` becomes the canonical paid-state record
- `user.plan_code` may remain a compatibility projection for current gates

## Required test coverage

### `tests/test_subscriptions.py`

At minimum cover:

1. successful first payment creates a subscription
2. successful first payment creates a billing invoice
3. duplicate webhook does not create duplicate subscription rows
4. duplicate webhook does not create duplicate invoice rows
5. `GET /api/me/subscription` returns a deterministic authenticated shape
6. `GET /api/billing/history` returns user-scoped history only
7. trial bookkeeping is not silently converted into a paid subscription

### Existing tests to update or extend

Ensure `tests/test_billing.py` and `tests/test_gateway_entitlements.py` continue to prove:

- `PaymentOrder` compatibility still works
- order settlement remains idempotent
- user-visible entitlement projection does not regress

## Verification requirements

Run at least:

1. `pytest tests/test_subscriptions.py tests/test_billing.py tests/test_gateway_entitlements.py -q`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp`
2. `python main.py --help`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp`
3. `alembic upgrade head`
   - workdir: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway`

If local DB configuration prevents full migration verification, report the exact blocker.
Do not claim migration verification if it did not actually run.

## Completion report

Write a new report back to:

- `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/`

Suggested filename:

- `2026-04-05_23xxxx_from-Claude-Code_to-CodeX_type-report_task-T4_stage-complete.md`

Your report must include at least:

1. execution scope
2. final schema decision for `subscriptions` and `billing_invoices`
3. whether `user.plan_code` remains a compatibility projection
4. how `PaymentOrder` and `PaymentWebhookEvent` were preserved
5. final API shape of `GET /api/me/subscription`
6. final API shape of `GET /api/billing/history`
7. how trial boundary safety was preserved
8. exact files changed
9. `pytest` results
10. `python main.py --help` result
11. `alembic upgrade head` result
12. blockers / residual risk
13. explicit stop status

## Success criteria

This round is successful if:

- a minimal paid-subscription truth source exists
- a minimal billing-history truth source exists
- `PaymentOrder` remains compatible and not hard-replaced
- gateway exposes authenticated read APIs for subscription and billing history
- trial facts remain unfrozen where still unfrozen
- no Task 5 or Task 6 scope is smuggled in

Stop after completion and wait for CodeX review.
