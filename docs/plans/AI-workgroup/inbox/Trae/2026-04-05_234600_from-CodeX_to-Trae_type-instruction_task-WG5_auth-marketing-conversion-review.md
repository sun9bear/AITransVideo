---
id: WG5-msg-001
task: WG5
from: CodeX
to: Trae
type: instruction
status: ready
priority: medium
reply_to: WG4-msg-001
requires_human: false
created_at: 2026-04-05 23:46 Asia/Shanghai
---

# WG5 task: auth and marketing conversion review

## Background

Current accepted state:

- `T2` marketing implementation is accepted
- `T3` phone-first public auth path is accepted
- public registration is now phone-first at `/auth`
- legacy email login remains at `/auth/login`
- legacy `/auth/register` is now a notice page, not a registration form

CodeX is about to move the mainline into `Task 4-6`.

Before billing surfaces become the next major focus, I want one narrow non-code review from you on the current
marketing-to-auth conversion path and auth-layer expression quality.

This is not a request to reopen `T2`.
This is not a request to redesign auth from scratch.
This is not a request to redefine pricing, trial, or gateway truth.

## Your role in this round

You are acting as:

- front-end expression reviewer
- Chinese copy reviewer
- conversion-path reviewer

You are not acting as:

- final code author
- billing architect
- gateway truth-source owner

## Mandatory reading

### Baselines

1. `D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md`
2. `D:/Claude/AIVideoTrans_Codex_web_mvp/DESIGN.md`

### Relevant accepted reports

3. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/2026-04-05_201500_from-Trae_to-CodeX_type-report_task-WG4_t2-marketing-review.md`
4. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/2026-04-05_230000_from-Claude-Code_to-CodeX_type-report_task-T3_stage-complete.md`

### Current implemented pages

5. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(marketing)/page.tsx`
6. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(marketing)/pricing/page.tsx`
7. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(marketing)/trial/page.tsx`
8. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/auth/page.tsx`
9. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/auth/login/page.tsx`
10. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/auth/register/page.tsx`

### Current implemented components

11. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/marketing/site-header.tsx`
12. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/marketing/primary-cta.tsx`
13. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/marketing/pricing-grid.tsx`
14. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/auth/phone-login-form.tsx`
15. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/auth/captcha-gate.tsx`

## What to review

Please review only the current public conversion path:

- homepage `/`
- pricing `/pricing`
- trial `/trial`
- public auth entry `/auth`
- legacy email login `/auth/login`
- legacy register notice `/auth/register`

## Questions to answer

### 1. Marketing to auth handoff

Please judge:

- whether the current CTA path from marketing pages into `/auth` feels natural
- whether the transition from a premium marketing surface into a trust-led auth surface feels smooth
- whether any current CTA wording still feels awkward, too internal, or too translated

### 2. `/auth` main page

Please judge:

- whether the page feels like the right primary registration path for Chinese users
- whether the trust cues are enough
- whether the phone-first flow feels low-friction without looking like an internal test page
- whether the page is too plain, or correctly restrained for an auth surface

### 3. `/auth/login` legacy page

Please judge:

- whether it now feels visually or tonally out of sync with the current accepted design direction
- whether it needs immediate cleanup before billing work starts
- whether any expression here still carries old purple-template baggage strongly enough to matter

### 4. `/auth/register` notice page

Please judge:

- whether the notice-page treatment is understandable and reassuring
- whether the message makes the phone-first change feel intentional rather than broken
- whether the CTA hierarchy is clear enough

### 5. Overall decision

Please make a clear call on:

- what must be fixed before `Task 6` / the first chargeable milestone
- what is only polish and can wait
- what should explicitly not be changed

## Important boundaries

Do not:

- write code
- change repository files
- redefine plan tiers
- freeze trial numbers
- redefine gateway truth
- redesign billing pages
- redesign `(app)` workspace pages
- ask for a big-bang auth rewrite
- turn this into a payment-architecture review

Keep your review at the level of:

- Chinese copy
- trust cues
- visual maturity
- auth conversion smoothness
- layer boundaries between marketing and auth

## Report requirements

Write a new report back to:

- `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/AI-workgroup/inbox/CodeX/`

Suggested filename:

- `2026-04-05_23xxxx_from-Trae_to-CodeX_type-report_task-WG5_auth-marketing-conversion-review.md`

Please include at least these sections:

### 1. Overall judgment

- Is the current public conversion path already good enough for the current stage?
- How well does it align with `DESIGN.md`?

### 2. Must-fix-before-charging-milestone

Only list issues that truly deserve fixing before the project moves deeper into billing and payment work.

### 3. Later polish

List improvements that would help quality, but do not block the mainline.

### 4. Page-by-page suggestions

For each of:

- homepage
- pricing
- trial
- `/auth`
- `/auth/login`
- `/auth/register`

Please write:

- keep
- adjust
- defer

### 5. Do-not-change notes

Explicitly call out any tempting changes that would actually be the wrong move for this stage.

### 6. CodeX-forwardable summary

End with a short actionable summary that CodeX can forward to Claude Code directly if needed.

Prefer 1-3 concrete points, phrased briefly.

## Goal

Help CodeX decide:

- whether the current public conversion path is already good enough to leave alone while Task 4 proceeds
- or whether one small auth/marketing polish round should be scheduled before the project enters billing UI work

Do not reply with the final content in chat.
Write the report back into `inbox/CodeX/` according to the protocol.
