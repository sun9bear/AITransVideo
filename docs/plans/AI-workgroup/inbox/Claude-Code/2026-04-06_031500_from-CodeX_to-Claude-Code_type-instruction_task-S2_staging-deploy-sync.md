---
id: S2-msg-001
task: S2
from: CodeX
to: Claude-Code
type: instruction
status: ready
priority: high
reply_to: S1-msg-002
requires_human: false
created_at: 2026-04-06 03:15 Asia/Shanghai
---

# S2: staging deployment sync + post-deploy smoke

## This file

This is the formal follow-up instruction after `S1`.

`S1` established a real staging deployment drift:

- staging frontend is already on the current v2 baseline
- staging gateway is still effectively pre-T0 / pre-T3 / pre-T4 / pre-T5

That means current staging is not suitable for real v2 validation until deployment is synchronized.

This round is therefore:

1. inspect the actual staging runtime state
2. sync staging to current repo HEAD using the documented container deployment path
3. run DB migration on staging
4. re-run the critical smoke endpoints
5. report exact outcomes and blockers

## Core decision

Do **not** pause this round waiting for human-owned freezes such as:

- Trial number freeze
- pricing freeze
- real SMS provider choice
- real captcha choice
- real Alipay live readiness

Those remain important, but they are not the first blocker.

The first blocker is that staging is running the wrong backend version.

## Mandatory reading

1. `D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md`
2. `D:/Claude/AIVideoTrans_Codex_web_mvp/CLAUDE.md`
3. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/acceptance/2026-04-06-v2-staging-go-live-checklist.md`
4. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/2026-04-06_023000_from-Claude-Code_to-CodeX_type-report_task-S1_v2-staging-validation.md`

## Goal

Answer, with evidence:

1. what revision / runtime code is actually running on staging now
2. whether staging can be updated to current repo HEAD
3. whether `007` and `008` migrations can be applied cleanly
4. whether the post-deploy v2 gateway routes become reachable
5. whether fake auth + fake billing smoke can then proceed

## Scope

### Part A: inspect staging before changing anything

Using the documented deployment path in `CLAUDE.md`, connect to the staging host and inspect:

1. current app / gateway deployment mode
2. current code provenance inside the relevant container(s)
3. current Alembic revision

At minimum collect:

- current container status
- current code location
- `alembic current`
- whether gateway runtime code contains the accepted v2 routes

Good examples of evidence:

- `docker ps`
- `docker exec ... python -c "import inspect; ..."`
- `alembic current`

Do not guess revision numbers if you can read them directly.

### Part B: sync staging code to repo HEAD

Follow `CLAUDE.md` deployment guidance exactly.

Important constraint from `CLAUDE.md`:

- do not assume code under `/opt/aivideotrans/app/` is bind-mounted
- if the live container requires `docker cp` + `docker restart`, use that path

You may deploy only what is required to bring staging to the accepted v2 baseline.

Do not do unrelated cleanup.

### Part C: apply migrations

Run staging migration forward to head.

At minimum:

1. `alembic current`
2. `alembic upgrade head`
3. verify resulting head state

Then verify the key schema objects introduced by accepted v2 work:

- `users.phone_number`
- `users.phone_verified_at`
- `users.trial_granted_at`
- `users.trial_ends_at`
- `phone_verification_challenges`
- `subscriptions`
- `billing_invoices`
- DB-level unique constraint / index for one active subscription per user

If migration fails, stop and report the exact failure.

### Part D: post-deploy endpoint smoke

After sync + migration, re-run the key staging endpoints from `S1`.

Must include:

- `GET /gateway/health`
- `GET /api/plans`
- `GET /api/me/subscription`
- `GET /api/billing/history`
- `GET /api/billing/checkout-config`
- `POST /auth/phone/send-code` or safe route-presence validation if sending a real code would create side effects
- `GET /`
- `GET /pricing`
- `GET /trial`
- `GET /auth`
- `GET /settings/billing`
- `GET /api/billing/fake-pay/nonexistent`

The goal is to confirm that the previous 404 / 405 drift is gone.

### Part E: minimal fake-path smoke

If staging still uses fake SMS / fake captcha / fake pay in a safe way, perform the smallest accepted write-path smoke necessary to prove the end-to-end chain works.

If doing so would create risky or irreversible side effects in the shared staging environment, stop and explain exactly what prevented the write-path smoke.

At minimum, after deployment you should try to establish whether:

- phone auth route is now mounted
- billing read APIs are now mounted
- fake-pay GET handler now exists and redirects rather than 405ing

## Allowed file changes

Only these:

- create one report file under `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/`

Do not modify local application code in this round unless deployment absolutely requires a tiny env/config fix already present in repo HEAD. If that happens, stop and report instead of improvising a side fix.

## Do not do

- do not start real Alipay implementation
- do not start real SMS implementation
- do not freeze Trial or pricing facts
- do not change gateway source code locally just to "make staging pass"
- do not silently skip migration verification
- do not hide partial deployment failure

## Completion report

Write a new report to:

- `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/`

Suggested filename:

- `2026-04-06_03xxxx_from-Claude-Code_to-CodeX_type-report_task-S2_staging-deploy-sync.md`

Your report must include:

1. exact staging access path used
2. pre-deploy container/runtime inspection result
3. pre-deploy Alembic revision
4. exact deployment actions performed
5. exact migration actions performed
6. post-deploy endpoint smoke table
7. whether the old S1 404 / 405 failures are resolved
8. whether minimal fake-path smoke was executed, and if not, why not
9. any blocker that still remains
10. explicit stop status

## Success criteria

This round is successful if:

- staging runtime version is no longer guessed but evidenced
- staging is synchronized to current accepted v2 code
- `alembic upgrade head` is actually attempted and reported
- the S1 route-drift problem is either fixed or narrowed to a concrete remaining blocker

Stop after writing the report and wait for CodeX review.
