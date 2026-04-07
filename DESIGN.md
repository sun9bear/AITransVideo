# DESIGN.md - AIVideoTrans

> Scope: project-level design baseline with three layers:
> 1. Global Foundations
> 2. Marketing Layer Rules
> 3. App / Billing / Admin Guardrails
>
> This file lives at the repository root for discoverability, but it is not a license to make every surface look identical.
> `frontend-next` marketing pages should follow it strongly.
> `(app)` workspace, billing, and admin pages should inherit the foundations and guardrails, not the full marketing expression layer.
> External references, moodboards, and inspiration sites are atmosphere inputs only. They must not be treated as a license to clone another product's UI.

## 1. Product Context

- **Primary users**: Chinese-language users.
- **Product shape**: A professional creator SaaS for video translation, dubbing, review, and Jianying draft delivery.
- **Experience goal**: trustworthy, efficient, media-capable, and conversion-friendly without looking like a generic neon AI template.
- **Design priority**: Chinese-first readability, conversion clarity, trust signals, and long-session usability.
- **Execution note**: during the current v2 staged migration, pricing, trial, and entitlement facts come from the gateway truth source. Design copy may guide structure and tone, but must leave unfrozen commercial facts configurable.

## 2. Global Foundations

### 2.1 Brand Tone

- **Keywords**: Professional, contrast-led, trustworthy, efficient, creator-focused.
- **Vibe**: A reliable studio-grade tool for creators and operators, not a playful consumer toy and not a generic Silicon Valley clone.
- **Voice**: Direct, concrete, useful. Prefer clear benefit statements over abstract slogans.
- Hero headlines and marketing copy may be expressive, but they should stay revisable until product facts and conversion strategy are frozen.

### 2.2 Color Direction

- Use a **neutral professional base**: slate, graphite, fog, and clean light surfaces.
- Primary accents should lean toward **deep blue**, **steel cyan**, or **signal teal**.
- Do **not** default to "AI purple" or loud neon gradients.
- Contrast should stay high enough for Chinese text-heavy reading, forms, pricing tables, FAQ blocks, and status panels.
- Dark surfaces are allowed where media needs to stand out, but dense reading surfaces should stay clean and readable.

### 2.3 Typography Rules (Chinese-First)

- Prefer system sans fonts that render Chinese well, such as `PingFang SC`, `Microsoft YaHei`, or equivalent platform defaults.
- Pair Chinese system fonts with a clean English sans if needed, but Chinese readability wins.
- Chinese titles should stay shorter and more direct than English landing-page slogans.
- Avoid ultra-light weights for Chinese text.
- Use slightly larger base sizes and comfortable line-height for body copy.
- Long explanations should be broken into bullets or short paragraphs instead of dense walls of text.
- Keep pricing tables, FAQs, and conversion surfaces easy to scan at a glance.

### 2.4 Spacing, Shape, and Motion

- Use a calm, orderly layout rhythm based on an 8px spacing system.
- Corners should feel modern but not playful: subtle to medium rounding is preferred.
- Shadows should be restrained and functional, not dramatic.
- Motion should be crisp, short, and informative. Avoid bounce-heavy, parallax-heavy, or novelty animations.

### 2.5 Shared Trust Principles

Across marketing, auth, billing, and admin-facing UX:

- Use explicit trust cues in Chinese where appropriate:
  - `无需绑卡`
  - `项目安全保留`
  - `支持支付宝 / 微信` only when confirmed
  - `试用结束后的处理方式` should be explained explicitly
- Prefer clear state labels, clear next steps, and low-ambiguity copy.
- Never hide important billing, trial, or account state behind decorative language.
- Any unfrozen price, quota, minute, or trial rule should stay configurable or be marked as pending confirmation rather than invented in static copy.

## 3. Marketing Layer Rules

> Applies strongly to:
> - homepage
> - pricing page
> - trial page
> - registration / conversion surfaces

### 3.1 Overall Direction

- Marketing pages should be **dark-capable** and **contrast-led**, not universally dark.
- Hero and demo sections may use deeper surfaces to make video and media previews feel premium.
- Pricing, FAQ, and forms should shift toward clearer, lighter, or neutral reading surfaces when needed.
- Premium atmosphere is welcome, but conversion clarity for Chinese users comes first.

### 3.2 Information Architecture

Preferred homepage flow:

- Hero
- product proof / demo
- feature explanation
- pricing
- FAQ
- final CTA

Use clear section boundaries and avoid overly long storytelling chains.

### 3.3 Hero Guidance

- Lead with a concise Chinese headline.
- Subheadline should explain the practical user value in plain language.
- Follow with a direct CTA and a trust cue.
- Demo should show workflow credibility: translation, dubbing, review, or Jianying draft output.
- Do not lock the hero into overly poetic or abstract slogans that outrun current product facts.

### 3.4 CTA Style

- Use direct Chinese phrasing such as:
  - `免费开始试用`
  - `立即翻译视频`
  - `查看套餐`
- Avoid vague or overly poetic copy.
- Primary CTA buttons should feel solid, confident, and fast.
- Candidate copy can be proposed in Task 2, but should still be treated as directional until the final page implementation is approved.

### 3.5 Pricing Guidance

- Marketing-layer pricing presentation should use:
  - `Free`
  - `Plus`
  - `Pro`
- `Trial` is a **state / conversion entry**, not a long-term pricing tier.
- Prefer a clear three-tier pricing comparison. If `Trial` needs to be shown, present it as a banner, tag, or conversion entry rather than a fourth permanent pricing card.
- Pricing tables should emphasize concrete benefits and limits, not abstract brand language.
- Pricing grids, FAQs, and plan comparison blocks should bias toward neutral or light reading surfaces for dense Chinese text.
- Specific prices, minutes, quotas, and payment channel claims must follow gateway truth after Task 0, not hand-maintained design copy.

### 3.6 Trial Page Guidance

- Trial should feel low-friction and reassuring.
- Copy must clearly explain what happens after trial ends and whether any billing action is required.
- Do not imply hidden charges or automatic billing by default.
- The page may use a simple benefits-plus-form structure when that helps conversion clarity, but readability and trust should outrank decorative layout decisions.
- Trial duration, minutes, and eligibility rules are product facts owned by the gateway truth source, not by static design copy.

## 4. App / Billing / Admin Guardrails

> Applies to:
> - workspace / app shell pages
> - billing pages
> - admin pages
>
> These pages should reference this DESIGN.md, but they should not blindly copy marketing layout or drama.

### 4.1 Shared Principle

- App, billing, and admin should inherit the **foundations**:
  - color direction
  - typography
  - spacing rhythm
  - motion restraint
  - Chinese-first clarity
- They should not inherit the full marketing expression layer such as hero-led drama, oversized demo-first layouts, or overly cinematic presentation.
- Operational surfaces should optimize scanability, continuity, and precision before atmosphere.

### 4.2 Workspace / App

- Prioritize focus, scanability, and task continuity.
- Layout should support repeated use and long sessions.
- Dense content is acceptable if hierarchy remains clear.
- Surfaces should stay calmer and less theatrical than the homepage.
- Avoid oversized hero headlines, slogan-led cards, and dramatic motion in core task flows.

### 4.3 Billing

- Prioritize clarity, trust, and auditability.
- Status, plan, trial, renewal, and payment information must be visually unambiguous.
- Use neutral surfaces and strong contrast before decorative expression.
- Tables, invoices, plan summaries, and checkout states should feel precise and reliable.
- Avoid dark marketing hero blocks, decorative gradients, or conversion-style slogans in payment-critical states.

### 4.4 Admin

- Prioritize operational efficiency and fast scanning.
- Use tighter but readable information density.
- Status tags, filters, forms, and tables should be consistent, plain, and easy to recover from errors.
- Avoid marketing-style oversized cards, dramatic empty space, or theatrical hierarchy that harms operational speed.

## 5. Do / Don't

### Do

- Design for Chinese reading habits first.
- Keep copy direct, short, and benefit-driven.
- Use dark surfaces where media benefits from it, not everywhere by default.
- Keep pricing and trial messaging concrete and trustworthy.
- Let marketing pages feel premium while app surfaces stay efficient.
- Treat external inspiration as atmosphere input, not a cloning target.

### Don't

- Don't default to purple AI branding.
- Don't treat `Trial` as a permanent pricing tier.
- Don't force the entire product into one cinematic dark theme.
- Don't let marketing expression reduce readability in billing, admin, or workspace surfaces.
- Don't turn app, billing, or admin flows into landing-page-style experiences.
- Don't let visual polish outrun clarity for Chinese users.
