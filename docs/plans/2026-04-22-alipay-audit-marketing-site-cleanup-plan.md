# Alipay Audit Marketing Site Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the public marketing site present clear product proof, explicit deliverables, and consistent operator identity for Alipay website-payment review.

**Architecture:** Keep the existing marketing page composition and legal-page shell, add one focused proof section to the homepage, add one trust/explainer section to pricing, and centralize operator metadata in a shared module so footer/contact/legal pages do not drift.

**Tech Stack:** Next.js App Router, React Server Components, TypeScript, Tailwind CSS, Lucide icons.

---

### Task 1: Centralize operator identity

**Files:**
- Create: `frontend-next/src/components/marketing/company-info.ts`
- Modify: `frontend-next/src/components/marketing/site-footer.tsx`
- Modify: `frontend-next/src/components/marketing/legal-page.tsx`
- Modify: `frontend-next/src/app/(marketing)/contact/page.tsx`
- Modify: `frontend-next/src/app/(marketing)/terms/page.tsx`
- Modify: `frontend-next/src/app/(marketing)/privacy/page.tsx`

- [ ] Add a shared constant module for company name, support email, address, and generic payment wording.
- [ ] Switch footer and legal contact block to shared constants.
- [ ] Replace old operator name/email/address in public legal copy.
- [ ] Remove Paddle-specific public footer/legal wording.

### Task 2: Add homepage product-proof section

**Files:**
- Create: `frontend-next/src/components/marketing/product-proof.tsx`
- Modify: `frontend-next/src/app/(marketing)/page.tsx`

- [ ] Build a new marketing section with realistic task-creation and results-delivery proof panels.
- [ ] Add an explicit deliverables list for what paid users receive.
- [ ] Insert the section into the homepage flow without disrupting existing feature/pricing sections.

### Task 3: Strengthen pricing trust content

**Files:**
- Create: `frontend-next/src/components/marketing/pricing-assurance.tsx`
- Modify: `frontend-next/src/app/(marketing)/pricing/page.tsx`

- [ ] Add a concise section that explains purchase outcome, delivery model, and checkout/payment expectations.
- [ ] Keep numeric facts sourced from Gateway and avoid duplicating live plan values.

### Task 4: Update support/legal page details

**Files:**
- Modify: `frontend-next/src/app/(marketing)/contact/page.tsx`
- Modify: `frontend-next/src/app/(marketing)/refund/page.tsx`
- Modify: `frontend-next/src/app/(marketing)/terms/page.tsx`
- Modify: `frontend-next/src/app/(marketing)/privacy/page.tsx`

- [ ] Update contact page support copy to the new mailbox and address.
- [ ] Adjust public payment language to generic "checkout page shown payment methods" wording where needed.
- [ ] Keep long-form policy structure intact.

### Task 5: Verify

**Files:**
- Verify only

- [ ] Run targeted ESLint on changed marketing files.
- [ ] Run `npx tsc --noEmit`.
- [ ] Review for any remaining public mentions of the old operator identity or Paddle wording.
