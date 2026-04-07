# Report: Auth and Marketing Conversion Review (WG5)

## 1. Overall judgment

- **Is the current public conversion path already good enough?** Yes, it is highly effective and ready for the next stage. The handoff from the marketing pages to the new phone-first `/auth` page feels natural, low-friction, and trustworthy.
- **Alignment with `DESIGN.md`**: The new `/auth` and `/auth/register` (notice) pages align perfectly with the guardrails. They are restrained, Chinese-first, and prioritize clarity over marketing drama. However, the legacy `/auth/login` page is a glaring exception that still carries old visual baggage.

## 2. Must-fix-before-charging-milestone

- **Clean up `/auth/login` visual baggage**: The legacy email login page (`frontend-next/src/app/(auth)/auth/login/page.tsx`) is visually out of sync with the rest of the app. It still uses the old "AI purple template" elements: `bg-violet-500/10` and `bg-cyan-500/10` background blur blobs, `focus:border-violet-500`, and deprecated custom color tokens (`bg-surface-lowest`, `text-on-surface-dim`). This breaks the visual consistency of the auth layer and must be refactored to match the clean, standard `shadcn/ui` card layout of the new `/auth` page before we move into billing UI work.

## 3. Later polish

- **Auth page layout enhancement**: The current centered card on `/auth` is perfectly functional and restrained. In the future (post-milestone), to make it feel even more like a mature enterprise SaaS, we could consider a split-screen layout (e.g., left side: auth form, right side: a subtle product shot, trust badges, or a customer quote). This is strictly low-priority polish and should not block Task 4.
- **Real Captcha UI integration**: When replacing the fake `CaptchaGate` with a real vendor (like Geetest or Tencent Captcha), we just need to ensure the vendor's injected UI doesn't break the clean aesthetic of the form.

## 4. Page-by-page suggestions

- **Homepage (`/`)**: **Keep**. The CTA routing to `/auth` is correct and natural.
- **Pricing (`/pricing`)**: **Keep**.
- **Trial (`/trial`)**: **Keep**.
- **`/auth` (Phone main path)**: **Keep**. The trust cues ("无需绑卡", "无需密码", "试用结束不会自动扣费") are excellent, highly localized for Chinese users, and placed exactly where they are needed to reduce friction.
- **`/auth/login` (Legacy email)**: **Adjust**. Strip out the purple/cyan blur blobs and old color tokens. Rewrite the markup to match the clean, standard card UI of `/auth/page.tsx`.
- **`/auth/register` (Notice page)**: **Keep**. The explanation is reassuring, makes the switch to phone-first feel intentional, and the CTA hierarchy (primary to phone, secondary to legacy login) is perfect.

## 5. Do-not-change notes

- **Do not add passwords to phone auth**: The passwordless OTP flow is the standard, lowest-friction path for Chinese users. Do not complicate it by asking users to set a password.
- **Do not resurrect email registration**: The notice page handles legacy links and SEO perfectly. Leave it as is.
- **Do not inject marketing drama into auth**: Keep the auth pages clean and focused. Do not add the `marketing-hero-surface` or large feature grids to the auth forms. The transition from "premium marketing" to "secure, quiet auth" is currently working exactly as it should.

## 6. CodeX-forwardable summary

- **Refactor `/auth/login` visuals**: Remove the legacy `bg-violet-500` blur blobs and deprecated `surface` color tokens. Make it visually match the clean, standard `shadcn/ui` card layout of the new `/auth` page.
- **Proceed to Task 4**: Once the legacy login page is visually cleaned up, the auth/marketing conversion path is solid. No further structural or design changes are needed before starting billing work.