import type { Metadata } from "next"
import { Link } from "@/i18n/navigation"
import {
  LegalPage,
  LegalSection,
  LegalClauseList,
} from "@/components/marketing/legal-page"
import {
  COMPANY_NAME,
  SUPPORT_EMAIL,
  SUPPORT_EMAIL_HREF,
} from "@/components/marketing/company-info"
import { BreadcrumbJsonLd } from "@/components/seo/breadcrumb-json-ld"
import { absoluteUrl } from "@/lib/seo/site"

const PAGE_DESCRIPTION =
  "AITrans.Video 退款政策：详细说明订阅、点数购买、处理失败、异常扣费等情形下的退款规则与申请方式。Refund Policy for AITrans.Video paid plans (中 / EN)."

export const metadata: Metadata = {
  // Short title — root layout template adds " · 爱译视频 AITrans.Video".
  title: "退款政策",
  description: PAGE_DESCRIPTION,
  alternates: { canonical: "/refund" },
  openGraph: {
    title: "退款政策 · 爱译视频",
    description: PAGE_DESCRIPTION,
    url: absoluteUrl("/refund"),
    type: "website",
  },
}

const UPDATED_AT = "2026-06-08"

/**
 * `/refund` — Refund Policy (bilingual 中文 / English).
 *
 * Covers subscriptions, one-off quotas, processing failures, and billing
 * dispute handling for digital services. Bilingual so domestic users and
 * payment-partner reviewers (Paddle, Merchant of Record) can both read it.
 * Reflects the real model: per-period one-time purchase, no auto-renewal;
 * credit-based digital service where consumed credits are non-refundable.
 * Linked from `/terms` §8.2.
 */
export default function RefundPage() {
  return (
    <>
      <BreadcrumbJsonLd
        items={[
          { name: "首页", path: "/" },
          { name: "退款政策", path: "/refund" },
        ]}
      />
      <LegalPage
        eyebrow="退款政策"
        title="退款政策"
        titleEn="Refund Policy"
        updatedAt={UPDATED_AT}
        intro={
          <>
            <p>
              本《退款政策》适用于您通过{" "}
              <span className="text-foreground">aitrans.video</span>{" "}
              购买 {COMPANY_NAME} 运营的 AITrans.Video 相关付费服务、套餐、点数、处理额度或其他数字化权益的情形。请您在购买前认真阅读本政策。
            </p>
            <p className="mt-2 text-muted-foreground">
              This Refund Policy applies when you purchase paid AITrans.Video
              services, plans, credits, processing allowances or other digital
              entitlements at aitrans.video, operated by {COMPANY_NAME}. Please
              read it carefully before purchasing.
            </p>
          </>
        }
      >
        <LegalSection number="1" title="一般说明 / Overview">
          <p>
            1.1 本服务属于数字化服务、在线算力服务或按需处理型服务。与传统实物商品不同，一旦服务开始履行、任务进入处理流程、点数发生消耗或输出结果已生成，相关费用通常不支持无条件退款。
          </p>
          <p className="mt-2 text-muted-foreground">
            1.1 This is a digital, compute-based, on-demand service. Unlike
            physical goods, once the service begins, a job enters processing,
            credits are consumed, or output is generated, the related fees are
            generally not unconditionally refundable.
          </p>
          <p className="mt-4">
            1.2 我们会根据具体产品形态、购买方式、任务状态、消耗情况、异常原因及适用法律规定，对退款申请进行审核处理。
          </p>
          <p className="mt-2 text-muted-foreground">
            1.2 We review refund requests based on the product form, purchase
            method, job status, consumption, the cause of any issue, and
            applicable law.
          </p>
        </LegalSection>

        <LegalSection number="2" title="套餐与计费（不自动续费）/ Plans &amp; Billing (No Auto-Renewal)">
          <p>
            2.1 付费套餐（Plus / Pro）按您所选周期（按月 / 按季 / 按年）一次性付费购买。本服务{" "}
            <strong className="text-foreground">不自动续费</strong>
            ：周期到期后不会自动扣款，因此不存在意外续费扣费的情形；如需继续使用，由您主动再次购买。
          </p>
          <p className="mt-2 text-muted-foreground">
            2.1 Paid plans (Plus / Pro) are purchased as a single, one-time
            payment for the period you select (monthly / quarterly / annual). The
            service does{" "}
            <strong className="text-foreground">not auto-renew</strong>: you are
            not charged automatically when a period ends, so there are no
            surprise renewal charges. You choose to purchase again to continue.
          </p>
          <p className="mt-4">
            2.2 除适用法律另有强制规定，或符合第 3 条「未使用退款」条件外，已开始生效的计费周期费用，原则上不因未使用、使用较少、主观不满意或中途停止使用而按比例退款。
          </p>
          <p className="mt-2 text-muted-foreground">
            2.2 Except where required by applicable law, or where the
            &ldquo;unused refund&rdquo; conditions in Section 3 are met, fees for
            a billing period that has started are generally not refunded
            pro-rata for non-use, light use, subjective dissatisfaction, or
            stopping use partway.
          </p>
          <p className="mt-4">
            2.3 如您认为存在误扣费、重复扣费或未经授权扣费，请在扣费发生后 7 日内联系{" "}
            <a
              href={SUPPORT_EMAIL_HREF}
              className="text-foreground underline-offset-4 hover:underline"
            >
              {SUPPORT_EMAIL}
            </a>{" "}
            申请核查。
          </p>
          <p className="mt-2 text-muted-foreground">
            2.3 If you believe a charge was incorrect, duplicated or
            unauthorized, contact {SUPPORT_EMAIL} within 7 days of the charge to
            request a review.
          </p>
        </LegalSection>

        <LegalSection number="3" title="点数、额度包或按量购买 / Credits &amp; Usage-Based Purchases">
          <p>
            3.1 如您购买的是点数、处理额度、任务包、一次性数字权益或其他按量消耗型商品：
          </p>
          <p className="mt-1 text-muted-foreground">
            3.1 If you purchase credits, processing allowances, job packs,
            one-off digital entitlements or other usage-based items:
          </p>
          <LegalClauseList
            items={[
              "未使用退款：未使用的点数或额度，在购买后 7 日内可申请退款，是否通过以实际审核结果为准。 / Unused refund: unused credits or allowances may be refunded within 7 days of purchase, subject to review.",
              "已消耗不退：已使用、部分已使用、已消耗、已过期、活动赠送、补偿发放或不可转让的点数或额度，不支持退款。 / No refund once used: credits or allowances that are used, partly used, consumed, expired, promotional, granted as compensation, or non-transferable are not refundable.",
              "处理即视为开始履行：任务一旦提交并进入处理流程，相关点数或额度可能视为已开始履行，不再适用无条件退款。 / Processing = service begun: once a job is submitted and enters processing, the related credits may be treated as service begun and are no longer unconditionally refundable.",
            ]}
          />
          <p className="mt-4">
            3.2 如购买页面、活动页、套餐说明页另有特殊约定，以相关页面说明为准。
          </p>
          <p className="mt-2 text-muted-foreground">
            3.2 Where a purchase page, promotion page or plan page states
            specific terms, those terms prevail.
          </p>
        </LegalSection>

        <LegalSection number="4" title="处理失败与异常情况 / Processing Failures &amp; Exceptions">
          <p>
            4.1 若您提交的付费任务因经我们核实确认的系统技术故障、平台错误、服务异常而未能正常完成，且未向您交付可用结果，我们可根据实际情况选择以下一种或多种方式处理：
          </p>
          <p className="mt-1 text-muted-foreground">
            4.1 If a paid job fails to complete due to a system, platform or
            service fault confirmed by us, and no usable result was delivered, we
            may do one or more of the following:
          </p>
          <LegalClauseList
            items={[
              "重新处理任务； / re-process the job;",
              "返还相应点数或额度； / restore the relevant credits or allowance;",
              "提供部分退款； / issue a partial refund;",
              "提供全额退款。 / issue a full refund.",
            ]}
          />
          <p className="mt-4">4.2 以下情况通常不视为平台责任，不当然构成退款依据：</p>
          <p className="mt-1 text-muted-foreground">
            4.2 The following are generally not platform faults and do not by
            themselves justify a refund:
          </p>
          <LegalClauseList
            items={[
              "源视频或音频质量差、噪音大、语音不清、多人混杂导致识别效果不佳； / poor source video/audio, noise, unclear speech, or overlapping speakers reducing recognition quality;",
              "翻译风格、措辞、字幕习惯、音色相似度、语速效果未完全符合您的主观预期； / translation style, wording, subtitle conventions, voice similarity, or pacing not matching your subjective expectations;",
              "您上传了错误文件、错误版本或错误配置； / you uploaded the wrong file, version or configuration;",
              "输出结果仍需人工校对、剪辑或二次编辑； / output still needs human proofreading, editing or post-processing;",
              "第三方平台限制、源内容本身问题、网络环境异常等非平台直接原因导致效果不理想。 / suboptimal results caused by third-party limits, the source content itself, or network conditions rather than the platform directly.",
            ]}
          />
        </LegalSection>

        <LegalSection number="5" title="不支持退款的情形 / Non-Refundable Situations">
          <p>在适用法律允许的范围内，以下情形通常不支持退款：</p>
          <p className="mt-1 text-muted-foreground">
            To the extent permitted by law, refunds are generally not available
            for:
          </p>
          <LegalClauseList
            items={[
              "已使用或已消耗的套餐权益、点数、额度、处理资源； / plan benefits, credits, allowances or processing resources already used or consumed;",
              "因您自身原因导致的误操作、误购、重复提交任务； / mistakes, accidental purchases or duplicate job submissions caused by you;",
              "因您违反服务条款、平台规则或适用法律而导致账户被限制、冻结或终止； / accounts restricted, frozen or terminated for violating the Terms, platform rules or law;",
              "活动商品、特价商品、赠送权益、测试资格、邀请码权益等页面已注明“不支持退款”的情形； / promotional items, discounted items, gifts, trial eligibility or invite-code benefits marked “non-refundable”;",
              "超出退款申请时限的请求； / requests made after the applicable refund window;",
              "无法提供有效订单信息、付款凭证或必要说明，导致无法核实的请求。 / requests that cannot be verified for lack of valid order info, proof of payment or necessary details.",
            ]}
          />
        </LegalSection>

        <LegalSection number="6" title="重复收费与支付异常 / Duplicate Charges &amp; Payment Issues">
          <p>
            6.1 如出现重复扣费、订单金额异常、支付成功但权益未到账等情况，请尽快联系{" "}
            <a
              href={SUPPORT_EMAIL_HREF}
              className="text-foreground underline-offset-4 hover:underline"
            >
              {SUPPORT_EMAIL}
            </a>
            。
          </p>
          <p className="mt-2 text-muted-foreground">
            6.1 For duplicate charges, incorrect order amounts, or successful
            payment without entitlement delivery, contact {SUPPORT_EMAIL} as soon
            as possible.
          </p>
          <p className="mt-4">
            6.2 经核实属于平台或支付链路异常的，我们将为您处理补发权益、返还差额、恢复点数或退款。
          </p>
          <p className="mt-2 text-muted-foreground">
            6.2 Where verified as a platform or payment-path issue, we will
            re-deliver entitlements, refund the difference, restore credits, or
            issue a refund.
          </p>
        </LegalSection>

        <LegalSection number="7" title="拒付与争议处理 / Chargebacks &amp; Disputes">
          <p>
            7.1 如您对账单有疑问，建议您先通过{" "}
            <a
              href={SUPPORT_EMAIL_HREF}
              className="text-foreground underline-offset-4 hover:underline"
            >
              {SUPPORT_EMAIL}
            </a>{" "}
            联系我们，我们将尽力协助解决。
          </p>
          <p className="mt-2 text-muted-foreground">
            7.1 If you have a billing question, please contact us first at{" "}
            {SUPPORT_EMAIL} and we will do our best to resolve it.
          </p>
          <p className="mt-4">
            7.2 如您在未经沟通的情况下直接发起恶意拒付、欺诈性争议或明显滥用退款政策，我们有权暂停或限制您的账户、服务权限及后续购买资格。
          </p>
          <p className="mt-2 text-muted-foreground">
            7.2 If you initiate a malicious chargeback, a fraudulent dispute, or
            clearly abuse this policy without contacting us first, we may suspend
            or limit your account, service access and future purchase
            eligibility.
          </p>
          <p className="mt-4">
            7.3 对于已核实存在欺诈、盗刷、套现、滥用活动规则等行为的账户，我们有权拒绝退款并追究责任。
          </p>
          <p className="mt-2 text-muted-foreground">
            7.3 For accounts verified to involve fraud, stolen-card use, cash-out
            abuse, or promotion abuse, we may refuse refunds and pursue
            liability.
          </p>
        </LegalSection>

        <LegalSection number="8" title="退款申请方式 / How to Request a Refund">
          <p>
            如需申请退款，请通过邮箱{" "}
            <a
              href={SUPPORT_EMAIL_HREF}
              className="text-foreground underline-offset-4 hover:underline"
            >
              {SUPPORT_EMAIL}
            </a>{" "}
            提交申请，并尽量提供以下信息：
          </p>
          <p className="mt-1 text-muted-foreground">
            To request a refund, email {SUPPORT_EMAIL} and include where possible:
          </p>
          <ul className="ml-5 list-disc space-y-1">
            <li>账户邮箱 / account email</li>
            <li>订单编号或交易编号 / order or transaction number</li>
            <li>购买时间 / purchase date</li>
            <li>付款金额 / amount paid</li>
            <li>退款原因 / reason for the refund</li>
            <li>
              相关截图、报错信息、任务链接或其他辅助材料 / screenshots, error
              messages, job links or other supporting materials
            </li>
          </ul>
        </LegalSection>

        <LegalSection number="9" title="审核、退款方式与到账时间 / Review, Method &amp; Settlement Time">
          <p>9.1 我们通常会在收到完整申请材料后的 3-7 个工作日内完成审核。</p>
          <p className="mt-2 text-muted-foreground">
            9.1 We typically complete review within 3–7 business days of
            receiving a complete request.
          </p>
          <p className="mt-4">
            9.2 若退款申请通过，退款将<strong className="text-foreground">原路返回</strong>至您的原支付方式。国际信用卡及海外支付方式由我们的记录商户（Merchant of Record）Paddle.com 处理并退款；中国大陆扫码支付经微信支付原路退回。到账时间取决于支付机构、银行或第三方支付渠道，通常需要额外 3-15 个工作日。
          </p>
          <p className="mt-2 text-muted-foreground">
            9.2 If approved, refunds are returned to your{" "}
            <strong className="text-foreground">original payment method</strong>.
            International card and overseas payments are processed and refunded
            by our Merchant of Record, Paddle.com; mainland China QR payments are
            returned via WeChat Pay. The time to appear depends on the payment
            provider, bank or channel, usually an additional 3–15 business days.
          </p>
          <p className="mt-4">9.3 实际到账时间以支付机构处理结果为准。</p>
          <p className="mt-2 text-muted-foreground">
            9.3 The actual arrival time is determined by the payment provider.
          </p>
        </LegalSection>

        <LegalSection number="10" title="特别说明 / Statutory Rights">
          <p>10.1 本退款政策不排除或限制适用法律赋予消费者的强制性权利。</p>
          <p className="mt-2 text-muted-foreground">
            10.1 This policy does not exclude or limit any mandatory consumer
            rights granted by applicable law.
          </p>
          <p className="mt-4">
            10.2 若您所在司法辖区对数字服务的撤回权、退款权或自动续费规则有特殊要求，我们将依法适用相关规则。
          </p>
          <p className="mt-2 text-muted-foreground">
            10.2 Where your jurisdiction has specific rules on withdrawal rights,
            refund rights or auto-renewal for digital services, we apply those
            rules as required by law.
          </p>
        </LegalSection>

        <LegalSection number="11" title="联系我们 / Contact">
          <p>
            如您对本退款政策有任何疑问，请通过{" "}
            <a
              href={SUPPORT_EMAIL_HREF}
              className="text-foreground underline-offset-4 hover:underline"
            >
              {SUPPORT_EMAIL}
            </a>
            、页面底部的运营主体信息或{" "}
            <Link
              href="/contact"
              className="text-foreground underline-offset-4 hover:underline"
            >
              《联系我们》
            </Link>{" "}
            页面联系我们。
          </p>
          <p className="mt-2 text-muted-foreground">
            For any questions about this Refund Policy, contact us at{" "}
            {SUPPORT_EMAIL}, via the operator details in the page footer, or
            through our Contact page.
          </p>
        </LegalSection>
      </LegalPage>
    </>
  )
}
