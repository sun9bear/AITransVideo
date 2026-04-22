# Alipay Audit Marketing Site Cleanup

Date: 2026-04-22

## Goal

Make the public AIVideoTrans marketing site look like a real, operating product website instead of a template landing page, so Alipay's `电脑网站支付` review has clearer evidence of genuine goods/services, deliverables, and operator identity.

## Current Problems

- The homepage and pricing page explain the product in generic SaaS language but do not show enough concrete product proof.
- The site footer and legal pages still use an older operator identity and contain Paddle-specific collection wording that conflicts with Alipay direct-collection review.
- The purchase path is explained weakly: visitors can see plans, but auditors do not get a clear "what is bought, what is delivered, what happens after payment" narrative.
- Contact and legal pages do not consistently present the same operator details.

## Requirements

- Keep Gateway as the pricing and entitlement source of truth.
- Do not hardcode plan prices or quotas in marketing copy beyond descriptive examples already supported by the runtime plans API.
- Optimize for Chinese-language auditors and customers.
- Replace public operator details with:
  - Company name: `武汉九骏电子商务有限公司`
  - Email: `sxz999@proton.me`
  - Address: `武汉市江岸区韦桑路100号`
- Remove Paddle-specific public collection wording from footer and legal contact blocks.
- Preserve the existing legal-page structure and marketing layout patterns.

## Proposed Changes

### 1. Add product-proof content to the homepage

Add a new homepage section that shows realistic product evidence:

- A task-creation proof panel that mirrors the real "新建翻译" workflow.
- A project-results proof panel that mirrors the real "项目列表 / 下载交付物" workflow.
- A deliverables block that explicitly lists what buyers receive:
  - 配音视频
  - 配音音频
  - 字幕/素材包
  - 剪映草稿工程
  - 人工复核工作台
  - 增量重生成能力

The section should read as product proof, not marketing fluff.

### 2. Strengthen pricing-page trust copy

Add a pricing trust/explainer section clarifying:

- What a paid plan unlocks
- What users receive after purchase
- That payment methods are shown on the checkout page
- That service delivery is digital and account-based
- That trial does not auto-charge

### 3. Centralize operator information

Create one shared source for operator identity and support contact so the footer, contact page, and legal pages stay consistent.

### 4. Unify legal and footer copy with Alipay-facing positioning

Update public-facing legal and support copy to:

- Use the new company name, email, and address
- Remove Paddle-specific merchant-of-record wording
- Keep payment wording general enough to remain correct if multiple payment methods exist

## Non-Goals

- No changes to Gateway pricing truth, billing APIs, or Alipay integration code
- No fake ICP number
- No broad redesign of the marketing site
- No new frontend testing framework just for this content pass

## Remaining Risk After This Cleanup

- `ICP备案` is still missing. Content cleanup can address the "template site" rejection, but it does not remove production review risk related to filing/record requirements for website payments.
