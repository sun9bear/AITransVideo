---
id: H2-msg-001
task: H2
from: CodeX
to: Claude-Code
type: instruction
status: ready
priority: high
reply_to: H1-msg-001
requires_human: false
created_at: 2026-04-06 05:30 Asia/Shanghai
---

# H2: Trial / Pricing 冻结事实收口

## This file

This is the formal rollout instruction for the newly frozen Trial / Pricing facts.

Human has now confirmed that the decision template in:

- `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/Human/2026-04-06_040000_from-CodeX_to-Human_type-decision_task-H1_trial-pricing-freeze-template.md`

should be treated as the new frozen truth for the current stage, and should override the current values in gateway truth surfaces where they differ.

## Current accepted baseline

- `T0` accepted: gateway owns plan / pricing / trial truth
- `T1` accepted: frontend `marketing / auth / app` split is in place
- `T2` accepted: marketing pages are live
- `T3` accepted: phone-first public auth + trial bookkeeping + risk-control baseline are in place
- `T4` accepted: minimal subscription truth + billing history truth are in place
- `T5` accepted: first paid checkout baseline is in place
- `T6` accepted: baseline Billing UI is in place
- `S2` accepted: staging deployment drift is repaired
- real SMS staging integration is now working, but SMS credential rotation / report redaction remains outside this task

## Goal

Roll the newly frozen Trial / Pricing facts into the current codebase and user-facing surfaces, while preserving the current architecture boundaries:

1. gateway remains the source of truth
2. frontend consumes truth rather than redefining it
3. trial / pricing copy becomes consistent with the newly frozen facts
4. staging `/api/plans` and current marketing/billing consumption no longer drift from the approved business facts

## Newly frozen facts

These are the new frozen facts for the current stage.

### Trial

- Trial remains enabled
- Trial duration: `7 days`
- Trial source minutes: `20 minutes`
- Trial includes `Studio`
- Trial grant conditions: `phone + captcha + risk control`
- same phone can receive Trial only once
- same IP can apply for Trial only once in its lifetime
- Trial expiry falls back to `Free`
- Trial does **not** auto-charge

### Pricing

Plan ladder remains:

- `Free`
- `Plus`
- `Pro`

Approved pricing:

- `Plus monthly = ¥99`
- `Plus quarterly = ¥269`
- `Plus annual = ¥999`
- `Pro monthly = ¥299`
- `Pro quarterly = ¥799`
- `Pro annual = ¥2999`

Approved capability boundaries:

- `Free`
  - single-video max duration: `10 minutes`
  - concurrent jobs: `1`
  - service modes: `express only`

- `Plus`
  - single-video max duration: `45 minutes`
  - concurrent jobs: `3`
  - service modes: `express + studio`

- `Pro`
  - single-video max duration: `180 minutes`
  - concurrent jobs: `5`
  - service modes: `express + studio`

Both `Plus` and `Pro` remain self-serve purchasable.

### Allowed external wording

Current-stage external wording is now allowed to say:

- supports Alipay
- SMS registration grants Trial
- Trial includes Studio

Only use wording that remains consistent with the actual frozen facts above.

## Scope

### Part A: gateway truth update

Update the gateway source of truth first.

Primary target:

- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/plan_catalog.py`

Any derived views must continue to flow from gateway truth:

- `billing.py`
- `job_intercept.py`
- `/api/plans`

The frontend must not become the new source of truth.

### Part B: trial rule rollout

Where trial bookkeeping or public trial copy is still intentionally generic due to previous unfrozen state, update it to reflect the newly frozen facts.

This applies only where the product should now explicitly consume the frozen facts.

Examples of likely touch points:

- trial-related marketing copy
- pricing-related copy
- billing or auth surfaces that mention trial eligibility or duration

### Part C: risk-control rollout

The newly frozen rule:

- same IP can apply for Trial only once in its lifetime

must be reflected in the current engineering truth path if it is not already.

This does **not** mean building a full fraud system.
It means the current implementation should no longer remain ambiguous about the IP-based Trial rule.

If the current schema / logic already supports an equivalent durable check, use it.
If not, implement the smallest truthful mechanism that fits the existing T3/T4 architecture.

Keep it:

- incremental
- testable
- migration-aware
- minimal

### Part D: frontend truth consumption update

Update current frontend consumers so they no longer drift from the newly frozen gateway truth.

This likely includes:

- `/pricing`
- `/trial`
- billing pricing display
- any helper types relying on old pricing assumptions

Do not hardcode pricing facts into multiple frontend places.
Prefer continuing to read from `/api/plans` where that contract already exists.

## Allowed file changes

### Gateway

- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/plan_catalog.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/billing.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/job_intercept.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/risk_control.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth_phone.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/subscriptions.py`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/models.py`
- Optional: Alembic migration only if the IP lifetime Trial rule truly requires durable schema support

### Frontend

- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/lib/billing/types.ts`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/lib/billing/get-plans.ts`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/marketing/pricing-grid.tsx`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/marketing/trial-banner.tsx`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/marketing/trial-details.tsx`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(marketing)/pricing/page.tsx`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(marketing)/trial/page.tsx`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(app)/settings/billing/page.tsx`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/billing/checkout-card.tsx`
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/billing/subscription-summary.tsx`

### Tests / docs

- Modify: relevant gateway tests
- Modify: relevant frontend tests if present
- Modify: `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/specs/2026-04-04-pricing-and-plans-api-contract.md`

## Do not modify

- do not start real Alipay live implementation
- do not start WeChat Pay
- do not build auto-renew / mandate lifecycle
- do not add usage ledger
- do not add team seats / reviewer seats
- do not merge `/auth/login` visual polish sidecar into this task
- do not add speculative anti-abuse frameworks beyond the now-frozen IP Trial rule

## Important rollout rules

### 1. Gateway first

Any public or frontend-facing fact must flow from gateway truth.

That means:

- update `plan_catalog.py` first
- verify `/api/plans`
- only then update frontend wording and rendering

### 2. Pricing values must be consistent end-to-end

If the newly frozen values differ from current repo truth, the final state after this task must be internally consistent:

- gateway truth
- `/api/plans`
- billing UI
- pricing UI
- trial page wording

No mixed old/new prices.

### 3. Trial wording can now be explicit

Because Trial is now frozen, the previous generic fallback wording can be tightened.

But keep it factual:

- no invented urgency
- no auto-charge implication
- no promise beyond frozen facts

### 4. IP lifetime Trial rule must be explicit in code and tests

This is now a business fact, not a vague note.

If the current implementation only rate-limits IPs temporarily, that is no longer sufficient.

Implement the smallest durable interpretation that can be defended.

### 5. Preserve current v2 staged boundaries

This task is still a rollout / truth-alignment task, not a new milestone.

Do not expand into:

- real payment provider rollout
- real captcha rollout
- go-live ops tooling
- support tooling

## Required verification

Run at least:

```bash
pytest tests/test_plan_catalog.py tests/test_auth_phone.py tests/test_trial_grant_rules.py tests/test_billing.py tests/test_subscriptions.py tests/test_gateway_entitlements.py -q
```

```bash
cd frontend-next
npm run lint
npm run build
```

Also verify the resulting staging-facing truth locally:

- `/api/plans` response shape
- pricing display values
- trial display values
- billing checkout display values

If you add a migration, also run:

```bash
alembic upgrade head
```

and report the actual result honestly.

## Completion report

Write a new report to:

- `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/`

Suggested filename:

- `2026-04-06_05xxxx_from-Claude-Code_to-CodeX_type-report_task-H2_trial-pricing-freeze-rollout.md`

Your report must include:

1. exact frozen facts rolled into code
2. gateway files changed
3. frontend files changed
4. whether a migration was needed
5. how the IP lifetime Trial rule was implemented
6. `/api/plans` final truth summary
7. pricing/trial copy changes
8. test results
9. lint/build results
10. any residual risk
11. explicit stop status

## Success criteria

This round is successful if:

- the newly frozen Trial / Pricing facts are reflected in gateway truth
- frontend no longer drifts from gateway truth
- the IP lifetime Trial rule is explicit and defended
- tests/build stay green

Stop after completion and wait for CodeX review.
