---
id: S1-msg-001
task: S1
from: CodeX
to: Claude-Code
type: instruction
status: ready
priority: high
reply_to: ""
requires_human: false
created_at: 2026-04-06 02:15 Asia/Shanghai
---

# v2 Staging Validation Instruction

## This file

This is a verification-only protocol for the current v2 baseline after `T0` through `T6` have been accepted.

Your job in this round is to validate staging readiness and produce a truthful report.

This is **not** a coding round.

## Current accepted baseline

- `T0` accepted: gateway owns plan / pricing / trial truth
- `T1` accepted: `marketing / auth / app` split is in place
- `T2` accepted: `/` + `/pricing` + `/trial` marketing pages are live
- `T3` accepted: phone-first public auth + trial bookkeeping + fake SMS/captcha path are in place
- `T4` accepted: minimal subscription truth + billing history truth are in place
- `T5` accepted: first paid checkout baseline is in place; fake browser path works; Alipay is truthfully gated as not live-ready
- `T6` accepted: baseline Billing UI is live at `/settings/billing`

Reference checklist:

- `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/acceptance/2026-04-06-v2-staging-go-live-checklist.md`

## Goal

Answer, with evidence:

1. is the current v2 baseline ready to enter staging verification
2. if staging is reachable from the current execution environment, does the fake auth + fake pay + billing UI path actually work there
3. what concrete blockers remain before real go-live

## This round is not

Do not:

- modify application code
- modify migrations
- polish UI
- start real SMS integration
- start real Alipay live integration
- change pricing / trial / provider facts
- rewrite docs outside the final report unless CodeX explicitly approves that follow-up

## Mandatory reading

1. `D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md`
2. `D:/Claude/AIVideoTrans_Codex_web_mvp/DESIGN.md`
3. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md`
4. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/acceptance/2026-04-06-v2-staging-go-live-checklist.md`
5. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/acceptance/2026-04-03-deployment-runtime-validation.md`
6. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/acceptance/PHASE_5_BILLING_FOUNDATION_ACCEPTANCE_NOTE.md`

## Scope

### Part A: local preflight verification

Always perform these locally in the current workspace:

1. run the core regression suite appropriate for the accepted v2 baseline
2. run `frontend-next` lint
3. run `frontend-next` build
4. run `python main.py --help`

Recommended command set:

```bash
pytest tests/test_plan_catalog.py tests/test_auth_phone.py tests/test_trial_grant_rules.py tests/test_subscriptions.py tests/test_billing.py tests/test_gateway_entitlements.py -q
```

```bash
cd frontend-next
npm run lint
npm run build
```

```bash
cd ..
python main.py --help
```

### Part B: staging reachability check

Determine whether a real staging environment is reachable from your current execution environment.

For this round, "staging reachable" means you can actually access what is needed to validate:

- staging application URL(s), and/or
- staging gateway URL, and/or
- SSH / container access, and/or
- staging Postgres + Alembic runtime where `alembic upgrade head` can be run

If staging is **not** reachable:

- do not pretend validation happened
- do not invent screenshots, logs, or route behavior
- stop after local preflight and write a blocker-style report that clearly says what access or runtime is missing

### Part C: staging migration validation

Only if staging is reachable and the current environment actually has the required access:

1. run `alembic upgrade head`
2. record the executed revision / resulting head state
3. verify that the new schema objects required by `T3` and `T4` exist

At minimum check for:

- `users.phone_number`
- `users.phone_verified_at`
- `users.trial_granted_at`
- `users.trial_ends_at`
- `phone_verification_challenges`
- `subscriptions`
- `billing_invoices`
- the DB-level uniqueness guard for one active subscription per user

If the required Alembic package / DB access / credentials are unavailable, report that honestly as a staging blocker.

### Part D: staging smoke verification

Only if staging is reachable:

#### Public routes

Verify:

- `/`
- `/pricing`
- `/trial`
- `/auth`
- `/auth/login`
- `/api/plans`

#### Auth / billing path

Verify:

- phone-first auth route can be opened
- fake captcha / fake SMS path can still be exercised in staging-safe mode
- post-login `/settings/billing` is reachable
- billing page renders the expected sections

#### Fake checkout loop

Verify the accepted fake path end-to-end:

1. create order
2. hand off to `/api/billing/fake-pay/{order_id}`
3. receive 303 redirect back to `/settings/billing?status=paid` or another accepted billing status
4. billing status banner renders
5. URL cleanup behavior is as reported in `T6`

#### Billing truth visibility

Verify that the billing page can reflect:

- current subscription summary
- trial bookkeeping without invented countdowns
- order history empty state or populated state

## Human-owned items you must not "verify closed"

If these remain unresolved, report them as human-owned go-live prerequisites rather than treating them as failed engineering tasks:

- Trial fact freeze
- pricing fact freeze
- real SMS provider choice / credentials / template approval
- real captcha provider choice / credentials
- real Alipay merchant readiness
- formal domain / callback URL / env-var injection decisions
- refund / support SOP ownership

## Allowed file changes

Only these:

- create one report file under `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/`

Do not edit any application, test, config, or migration file in this round.

## Completion report

Write a new report to:

- `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/`

Suggested filename:

- `2026-04-06_02xxxx_from-Claude-Code_to-CodeX_type-report_task-S1_v2-staging-validation.md`

Your report must include:

1. whether this round stayed verification-only
2. local preflight command results
3. whether staging was actually reachable
4. if reachable, exact staging validation steps performed
5. if not reachable, exact blocker(s)
6. migration validation result
7. public-route smoke result
8. auth + billing smoke result
9. fake checkout loop result
10. what remains human-owned before go-live
11. recommended next action
12. explicit stop status

## Success criteria

This round is successful if:

- local preflight results are real and recorded
- staging validation is performed where access exists
- missing staging access is called out honestly where it does not exist
- the report clearly separates engineering-verified items from human-owned go-live prerequisites

Stop after writing the report and wait for CodeX review.
