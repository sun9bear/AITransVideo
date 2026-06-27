# UI-03c Legal Human Review

Date: 2026-06-26
Reviewer: Codex product/legal-copy review
Scope: `terms`, `privacy`, `refund`, `contact`, and `components/marketing/legal-page.tsx`

> Boundary: this is not licensed legal advice. This review checks product-truth consistency, translation risk, user-facing promise boundaries, and whether the English legal copy can safely be produced from the current Chinese source.

## Verdict

**Do not approve UI-03c for merge yet.** The current Chinese legal source contains product-truth conflicts and over-specific implementation promises. English translation should wait until the blockers below are fixed, otherwise `/en` will publish inconsistent legal commitments.

## Hard Blockers

1. **Terms 7.2 contradicts the current billing model.**

   Evidence:
   - `frontend-next/src/app/[locale]/(marketing)/terms/page.tsx:196` says subscriptions may auto-renew and charge each period.
   - `frontend-next/src/app/[locale]/(marketing)/refund/page.tsx:94-100` says paid plans do not auto-renew.
   - `frontend-next/src/components/billing/checkout-card.tsx:373` says the payment only creates the selected order and will not auto-renew.
   - `docs/plans/2026-06-08-payment-mor-paddle-integration-plan.md:69` says Paddle is configured as one-time products and the current model is pay-per-period without auto-renewal.

   Required source fix before translation:
   - Replace the auto-renewal clause with a one-time-period purchase clause.
   - Keep room for a future auto-renew product only if separately displayed and accepted.

2. **Privacy 10.1 over-promises Baidu Pan disconnect behavior.**

   Evidence:
   - `frontend-next/src/app/[locale]/(marketing)/privacy/page.tsx:305` says disconnect immediately clears stored tokens and automatically notifies Baidu Pan to revoke authorization.
   - `gateway/pan/admin_api.py:497-513` only soft-disconnects by setting `PanCredentials.status='revoked'`; the row and encrypted tokens remain for audit, and there is no Baidu revoke call.
   - `docs/plans/2026-05-13-admin-pan-backup-design.md:298` defines disconnect as marking `status='revoked'`.

   Required source fix before translation:
   - Say disconnect marks the credential unavailable and stops further backup/restore calls.
   - Do not promise token deletion or Baidu-side revocation unless implemented.

3. **Privacy 10.1 under-discloses what may be transferred to Baidu Pan.**

   Evidence:
   - `frontend-next/src/app/[locale]/(marketing)/privacy/page.tsx:282` says ordinary users do not touch the flow and no ordinary-user identity information is transferred.
   - The same paragraph describes archiving completed task project packages to the admin's Baidu Pan. Those packages may contain user-uploaded media, generated files, subtitles, or project materials.

   Required source fix before translation:
   - Clarify that the admin-only archive may transfer task project packages and their contained user content/generated outputs to the admin-authorized Baidu Pan directory.
   - Keep the narrower statement only for login identity/profile data if that is the intended claim.

## Should-Fix Before English Launch

1. **Refund 9.2 is too channel-specific.**

   Evidence:
   - `frontend-next/src/app/[locale]/(marketing)/refund/page.tsx:304-312` says international payments are refunded by Paddle.com and mainland QR payments through WeChat Pay.
   - Gateway/provider code can expose multiple rails (`wechatpay`, `paddle`, existing provider labels), and provider availability is gateway-owned.

   Recommendation:
   - Use "original payment method / original payment provider" as the main rule.
   - Mention Paddle only for payments actually processed by Paddle as Merchant of Record.
   - Avoid hardcoding WeChat as the only mainland QR refund channel.

2. **Terms 15.2 court venue needs owner/legal confirmation.**

   Evidence:
   - Current operator identity is `武汉市洪山区九俊电子经营部`.
   - `frontend-next/src/app/[locale]/(marketing)/terms/page.tsx:353` selects `武汉市江岸区人民法院`.

   Recommendation:
   - Confirm the selected court has an actual connection to the operator, contract performance, or agreed jurisdiction strategy.
   - If not confirmed, do not translate this as a fixed English venue yet.

## Translation Guardrails

- Keep `COMPANY_NAME`, `SUPPORT_EMAIL`, and `COMPANY_ADDRESS` verbatim as identity/content fields.
- Do not translate user content, job titles, uploaded media text, or any pipeline language fields.
- Use cautious legal English: "may", "generally", "to the extent permitted by applicable law", "subject to review", "where applicable".
- Avoid adding rights, refunds, support SLAs, payment channels, cancellation paths, or data-deletion mechanics that are not implemented.
- Keep the no-auto-renew model consistent across Terms, Refund, pricing/billing UI, and Paddle one-time product configuration.

## Review Status

- Current source text: **not approved for English publication**.
- UI-03c implementation can proceed after the hard blockers are corrected in Chinese source and mirrored in English copy.
- PR signoff text should not say "legal en 译文已人审" until the corrected English text is reviewed against the fixed source.

## 项目主决策（2026-06-26，AskUserQuestion）

针对本 review，项目主拍板如下，实施者据此修中文源（再做英文）：

1. **百度网盘（Hard Blocker #2/#3）→ 从条款删除百度网盘专条**。
   - 但 ⚠️ 实施约束：管理员归档「已完成任务工程包」确会把**用户上传/生成内容**外流到运营方百度网盘（真实数据流，已由 `gateway/pan/admin_api.py` 核实为 admin-only 但搬运用户内容）。
   - 故**不是纯静默删除**：privacy 删掉百度网盘/token-revoke 等**过度具体且与代码不符**的措辞后，须以**通用第三方存储措辞兜底**——如「运营方可能使用第三方云存储服务对已完成任务的相关材料进行归档/备份」——避免对真实的用户内容外流**少披露**。不点名百度、不承诺 token 删除/第三方撤销。
   - （备选：若产品侧后续停止把用户内容归档到百度网盘，则可彻底不提——属单独后端任务，超出 i18n 方案范围。）

2. **Terms 7.2 自动续费（Hard Blocker #1）→ 改为非自动续费的一次性周期购买**条款（与 refund/checkout-card/Paddle one-time 配置一致）。**自动续费措辞的最终定稿留项目主/律师确认**（是否预留未来自动续费产品的单独展示+勾选条款）。

3. **Refund 9.2 渠道（Should-Fix）→ 改「原路退回 / 原支付渠道」为主**，仅对 Paddle 实际作为 MoR 处理的支付提 Paddle；不把微信写死为唯一大陆退款渠道。

4. **Terms 15.2 江岸区法院管辖（Should-Fix）→ 留项目主/律师确认**与运营主体的实际连接，未确认前**不**译为固定英文管辖条款。

> 执行顺序：先按上述修**中文源** → cjk-baseline 因 legal 页中文变更须重生成 → 基于修正后中文做**英文 legal** → 项目主/CodeX **二轮人审签出**后方可合并 UI-03c。
