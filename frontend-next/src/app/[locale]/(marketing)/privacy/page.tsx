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
  "AITrans.Video 隐私政策：说明我们如何收集、使用、存储、共享、保护您的个人信息，以及您依法享有的相关权利。Privacy Policy for AITrans.Video (中 / EN)."

export const metadata: Metadata = {
  // Short title — root layout template adds " · 爱译视频 AITrans.Video".
  title: "隐私政策",
  description: PAGE_DESCRIPTION,
  alternates: { canonical: "/privacy" },
  openGraph: {
    title: "隐私政策 · 爱译视频",
    description: PAGE_DESCRIPTION,
    url: absoluteUrl("/privacy"),
    type: "website",
  },
}

const UPDATED_AT = "2026-04-20"

/**
 * `/privacy` — Privacy Policy.
 *
 * Structured so auditors, regulators, and users can quickly locate what we
 * collect, why, where, and for how long.
 */
export default function PrivacyPage() {
  return (
    <>
      <BreadcrumbJsonLd
        items={[
          { name: "首页", path: "/" },
          { name: "隐私政策", path: "/privacy" },
        ]}
      />
      <LegalPage
      eyebrow="隐私政策"
      title="隐私政策"
      titleEn="Privacy Policy"
      updatedAt={UPDATED_AT}
      intro={
        <>
          <p>
            <span className="text-foreground">{COMPANY_NAME}</span>
            （以下简称“我们”）非常重视您的个人信息与数据安全。本《隐私政策》旨在说明，当您访问{" "}
            <span className="text-foreground">aitrans.video</span>{" "}
            或使用 AITrans.Video 相关服务时，我们如何收集、使用、存储、共享、保护您的个人信息，以及您依法享有的相关权利。
          </p>
          <p className="mt-2 text-muted-foreground">
            {COMPANY_NAME} (&ldquo;we&rdquo;) takes the security of your personal
            information and data seriously. This Privacy Policy explains how we
            collect, use, store, share and protect your personal information,
            and the rights you may have under applicable law, when you visit
            aitrans.video or use AITrans.Video services.
          </p>
          <p className="mt-4">在使用本服务前，请您认真阅读并充分理解本隐私政策。</p>
          <p className="mt-2 text-muted-foreground">
            Please read and fully understand this Privacy Policy before using
            the Service.
          </p>
        </>
      }
    >
      <LegalSection number="1" title="我们收集的信息 / Information We Collect">
        <p>在您使用本服务过程中，我们可能会根据功能需要收集以下信息：</p>
        <p className="mt-1 text-muted-foreground">
          When you use the Service, we may collect the following information as
          needed for relevant features:
        </p>

        <div className="mt-4">
          <h3 className="text-base font-semibold text-foreground">
            1.1 账户信息 / Account Information
          </h3>
          <p className="mt-2">
            包括您的邮箱地址、用户名、登录信息、账户标识、公司名称（如有）、国家/地区等。
          </p>
          <p className="mt-2 text-muted-foreground">
            Including your email address, username, login information, account
            identifier, company name (if any), and country/region.
          </p>
        </div>

        <div>
          <h3 className="text-base font-semibold text-foreground">
            1.2 支付与订单信息 / Payment &amp; Order Information
          </h3>
          <p className="mt-2">
            当您购买付费服务时，我们可能会收集订单编号、套餐信息、支付状态、交易时间、账单信息、支付方式类型等。
          </p>
          <p className="mt-2 text-muted-foreground">
            When you purchase paid services, we may collect the order number,
            plan information, payment status, transaction time, billing
            information, and payment method type.
          </p>
          <p className="mt-2">
            请注意：完整的银行卡号、支付账户密码等敏感支付信息通常由第三方支付服务商处理，我们一般不会直接保存完整支付凭证。
          </p>
          <p className="mt-2 text-muted-foreground">
            Please note: sensitive payment information such as full card numbers
            and payment-account passwords is generally handled by third-party
            payment service providers, and we generally do not directly store
            complete payment credentials.
          </p>
        </div>

        <div>
          <h3 className="text-base font-semibold text-foreground">
            1.3 您主动上传或提交的信息 / Information You Actively Upload or Submit
          </h3>
          <p className="mt-2">包括但不限于：</p>
          <p className="mt-1 text-muted-foreground">Including but not limited to:</p>
          <ul className="mt-2 ml-5 list-disc space-y-1">
            <li>视频文件 / video files</li>
            <li>音频文件 / audio files</li>
            <li>字幕文件 / subtitle files</li>
            <li>文稿、脚本、翻译文本 / documents, scripts, translation text</li>
            <li>语音样本 / voice samples</li>
            <li>图片或封面资料 / images or cover materials</li>
            <li>
              项目名称、任务备注、输出配置 / project names, job notes, output
              configurations
            </li>
            <li>
              您在客服、工单、反馈中提交的信息 / information you submit in customer
              support, tickets or feedback
            </li>
          </ul>
        </div>

        <div>
          <h3 className="text-base font-semibold text-foreground">
            1.4 设备与使用信息 / Device &amp; Usage Information
          </h3>
          <p className="mt-2">为保障服务运行和安全，我们可能收集：</p>
          <p className="mt-1 text-muted-foreground">
            To support the operation and security of the Service, we may collect:
          </p>
          <ul className="mt-2 ml-5 list-disc space-y-1">
            <li>IP 地址 / IP address</li>
            <li>浏览器类型 / browser type</li>
            <li>设备类型 / device type</li>
            <li>操作系统 / operating system</li>
            <li>访问页面 / pages visited</li>
            <li>功能使用记录 / feature usage records</li>
            <li>时间戳 / timestamps</li>
            <li>日志信息 / log information</li>
            <li>崩溃记录 / crash records</li>
            <li>性能监控数据 / performance monitoring data</li>
          </ul>
        </div>

        <div>
          <h3 className="text-base font-semibold text-foreground">
            1.5 Cookies 与类似技术 / Cookies &amp; Similar Technologies
          </h3>
          <p className="mt-2">
            我们可能使用 Cookies、本地存储、会话标识、分析工具等技术，以实现登录状态维持、偏好记忆、性能分析、安全控制、流量统计等目的。
          </p>
          <p className="mt-2 text-muted-foreground">
            We may use technologies such as cookies, local storage, session
            identifiers and analytics tools to maintain login state, remember
            preferences, analyze performance, enforce security controls, and
            measure traffic.
          </p>
        </div>
      </LegalSection>

      <LegalSection number="2" title="我们如何使用这些信息 / How We Use This Information">
        <p>我们收集和使用您的信息，主要用于以下目的：</p>
        <p className="mt-1 text-muted-foreground">
          We collect and use your information mainly for the following purposes:
        </p>
        <LegalClauseList
          items={[
            "为您提供、维护、优化本服务；",
            "处理您上传的媒体内容，并生成翻译、字幕、配音、导出结果等输出；",
            "创建和管理您的账户；",
            "完成支付、结算、对账、风控与反欺诈；",
            "处理客户支持、售后、退款、投诉和反馈；",
            "监测异常、保障系统稳定性与平台安全；",
            "改进产品质量、功能体验和运营效率；",
            "遵守法律法规、履行法定义务、响应监管或司法要求；",
            "执行本服务条款、保护我们及用户的合法权益。",
          ]}
        />
        <LegalClauseList
          items={[
            "to provide, maintain and optimize the Service;",
            "to process the media you upload and generate outputs such as translations, subtitles, dubbing and exported results;",
            "to create and manage your account;",
            "to complete payment, settlement, reconciliation, risk control and anti-fraud;",
            "to handle customer support, after-sales, refunds, complaints and feedback;",
            "to detect anomalies and safeguard system stability and platform security;",
            "to improve product quality, feature experience and operational efficiency;",
            "to comply with laws and regulations, fulfill legal obligations, and respond to regulatory or judicial requirements;",
            "to enforce these Terms of Service and protect the lawful rights and interests of us and our users.",
          ]}
        />
      </LegalSection>

      <LegalSection number="3" title="关于 AI 处理的特别说明 / Special Notes on AI Processing">
        <p>
          3.1
          当您上传视频、音频、文本、字幕或语音样本时，这些内容可能会被用于完成自动语音识别、机器翻译、字幕对齐、语音合成、视频渲染、文件导出等处理流程。
        </p>
        <p className="mt-2 text-muted-foreground">
          3.1 When you upload video, audio, text, subtitles or voice samples,
          this content may be used to perform processing such as automatic
          speech recognition, machine translation, subtitle alignment, speech
          synthesis, video rendering and file export.
        </p>
        <p className="mt-4">
          3.2
          上述处理可能通过我们自有系统或经我们接入的第三方服务提供能力完成，但仅限于实现本服务所必需的范围。
        </p>
        <p className="mt-2 text-muted-foreground">
          3.2 Such processing may be carried out through our own systems or
          through third-party services we have integrated, but only to the
          extent necessary to provide the Service.
        </p>
        <p className="mt-4">
          3.3
          请您不要上传您无权处理的内容，也不要在未评估风险和取得必要授权的情况下上传机密、敏感、受监管或高风险数据。
        </p>
        <p className="mt-2 text-muted-foreground">
          3.3 Please do not upload content you are not entitled to process, and
          do not upload confidential, sensitive, regulated or high-risk data
          without assessing the risks and obtaining the necessary authorization.
        </p>
      </LegalSection>

      <LegalSection number="4" title="我们共享信息的情形 / When We Share Information">
        <p>
          我们不会将您的个人信息出售给第三方。在以下情形下，我们可能会共享您的信息：
        </p>
        <p className="mt-1 text-muted-foreground">
          We do not sell your personal information to third parties. We may share
          your information in the following situations:
        </p>

        <div className="mt-4">
          <h3 className="text-base font-semibold text-foreground">
            4.1 与服务提供商共享 / Sharing with Service Providers
          </h3>
          <p className="mt-2">
            为了实现网站托管、存储、支付、日志分析、语音识别、翻译、语音合成、通知发送、技术支持等功能，我们可能会将必要信息共享给受托的第三方服务提供商。
          </p>
          <p className="mt-2 text-muted-foreground">
            To provide functions such as website hosting, storage, payment, log
            analysis, speech recognition, translation, speech synthesis,
            notification delivery and technical support, we may share necessary
            information with entrusted third-party service providers.
          </p>
        </div>

        <div>
          <h3 className="text-base font-semibold text-foreground">
            4.2 法律要求或风险控制 / Legal Requirements or Risk Control
          </h3>
          <p className="mt-2">
            在法律法规要求、司法机关或行政机关依法要求、重大风险防控、维权取证或执行平台规则时，我们可能依法披露必要信息。
          </p>
          <p className="mt-2 text-muted-foreground">
            Where required by laws and regulations, lawfully requested by
            judicial or administrative authorities, or necessary for major risk
            prevention, evidence collection for rights protection, or enforcing
            platform rules, we may disclose necessary information as permitted by
            law.
          </p>
        </div>

        <div>
          <h3 className="text-base font-semibold text-foreground">
            4.3 公司交易情形 / Corporate Transactions
          </h3>
          <p className="mt-2">
            如发生合并、分立、重组、收购、资产转让或类似交易，您的相关信息可能作为交易的一部分被转移，但我们会要求接收方继续依法保护您的信息。
          </p>
          <p className="mt-2 text-muted-foreground">
            In the event of a merger, division, reorganization, acquisition,
            asset transfer or similar transaction, your relevant information may
            be transferred as part of the transaction, but we will require the
            recipient to continue to protect your information in accordance with
            applicable law.
          </p>
        </div>
      </LegalSection>

      <LegalSection number="5" title="信息存储与保存期限 / Information Storage &amp; Retention">
        <p>
          5.1
          我们会在实现本政策所述目的所必要的期限内保存您的信息，除非法律法规另有要求。
        </p>
        <p className="mt-2 text-muted-foreground">
          5.1 We retain your information for as long as necessary to fulfill the
          purposes described in this Policy, unless laws and regulations require
          otherwise.
        </p>
        <p className="mt-4">
          5.2 不同类别信息的保存期限可能不同，通常会根据以下因素确定：
        </p>
        <p className="mt-1 text-muted-foreground">
          5.2 Retention periods may differ by category of information and are
          generally determined based on the following factors:
        </p>
        <ul className="ml-5 list-disc space-y-1">
          <li>
            您是否仍持有账户； / whether you still hold an account;
          </li>
          <li>
            您的项目和任务是否仍需下载、查看或售后支持； / whether your projects
            and jobs still require download, viewing or after-sales support;
          </li>
          <li>
            法律法规对财务、交易、合规记录的保存要求； / legal and regulatory
            requirements for retaining financial, transaction and compliance
            records;
          </li>
          <li>
            争议处理、维权和安全审计需要。 / needs for dispute handling, rights
            protection and security auditing.
          </li>
        </ul>
        <p className="mt-4">
          5.3
          超过必要保存期限后，我们会按照适用法律和内部规则删除、匿名化处理相关信息，或采取其他符合法律要求的处理方式。
        </p>
        <p className="mt-2 text-muted-foreground">
          5.3 After the necessary retention period has passed, we will delete or
          anonymize the relevant information in accordance with applicable law
          and our internal rules, or handle it in another manner that complies
          with legal requirements.
        </p>
      </LegalSection>

      <LegalSection number="6" title="国际传输 / International Transfers">
        <p>
          如您所在地区与我们服务器所在地不同，或我们使用了跨境技术服务商，您的信息可能会在不同国家或地区被处理、存储或传输。在适用法律要求的情况下，我们会采取合理措施保护跨境传输过程中的数据安全。
        </p>
        <p className="mt-2 text-muted-foreground">
          If your region differs from where our servers are located, or if we use
          cross-border technology service providers, your information may be
          processed, stored or transferred in different countries or regions.
          Where required by applicable law, we will take reasonable measures to
          protect the security of data during cross-border transfers.
        </p>
      </LegalSection>

      <LegalSection number="7" title="您的权利 / Your Rights">
        <p>在适用法律法规允许的范围内，您可能享有以下权利：</p>
        <p className="mt-1 text-muted-foreground">
          To the extent permitted by applicable laws and regulations, you may
          have the following rights:
        </p>
        <LegalClauseList
          items={[
            "查询、访问您的个人信息；",
            "更正或更新不准确的信息；",
            "删除您的个人信息或账户；",
            "撤回同意；",
            "要求解释本政策及相关处理规则；",
            "在符合法律规定的情况下获得您的相关数据副本；",
            "提出投诉、异议或维权请求。",
          ]}
        />
        <LegalClauseList
          items={[
            "to query and access your personal information;",
            "to correct or update inaccurate information;",
            "to delete your personal information or account;",
            "to withdraw consent;",
            "to request an explanation of this Policy and the related processing rules;",
            "to obtain a copy of your relevant data where permitted by law;",
            "to raise complaints, objections or rights-protection requests.",
          ]}
        />
        <p className="mt-4">
          如您希望行使上述权利，请发送邮件至{" "}
          <a
            href={SUPPORT_EMAIL_HREF}
            className="text-foreground underline-offset-4 hover:underline"
          >
            {SUPPORT_EMAIL}
          </a>
          。为保障安全，我们可能会要求您完成身份验证后再处理相关请求。
        </p>
        <p className="mt-2 text-muted-foreground">
          To exercise the above rights, please email {SUPPORT_EMAIL}. For
          security, we may require you to complete identity verification before
          we process the request.
        </p>
      </LegalSection>

      <LegalSection number="8" title="信息安全 / Information Security">
        <p>
          8.1
          我们会采取合理的管理、技术和组织措施保护您的信息安全，包括但不限于访问控制、权限管理、日志审计、安全传输、风险监控等。
        </p>
        <p className="mt-2 text-muted-foreground">
          8.1 We take reasonable managerial, technical and organizational
          measures to protect the security of your information, including but not
          limited to access control, permission management, log auditing, secure
          transmission and risk monitoring.
        </p>
        <p className="mt-4">
          8.2
          但请您理解，任何互联网传输、电子存储或系统环境都无法保证绝对安全，我们无法对非因我们原因导致的泄露、篡改、丢失或攻击承担绝对担保责任。
        </p>
        <p className="mt-2 text-muted-foreground">
          8.2 However, please understand that no internet transmission,
          electronic storage or system environment can be guaranteed to be
          absolutely secure, and we cannot provide an absolute guarantee against
          leakage, tampering, loss or attacks not caused by us.
        </p>
        <p className="mt-4">
          8.3
          如发生可能影响您合法权益的安全事件，我们将依法采取补救措施，并在法律法规要求的范围内履行通知义务。
        </p>
        <p className="mt-2 text-muted-foreground">
          8.3 If a security incident occurs that may affect your lawful rights
          and interests, we will take remedial measures in accordance with law
          and fulfill our notification obligations to the extent required by laws
          and regulations.
        </p>
      </LegalSection>

      <LegalSection number="9" title="未成年人保护 / Protection of Minors">
        <p>本服务主要面向具有完全民事行为能力的用户。</p>
        <p className="mt-2 text-muted-foreground">
          The Service is primarily intended for users with full capacity for
          civil conduct.
        </p>
        <p className="mt-4">如您为未成年人，请在监护人同意和指导下使用本服务。</p>
        <p className="mt-2 text-muted-foreground">
          If you are a minor, please use the Service with the consent and
          guidance of a guardian.
        </p>
        <p className="mt-4">
          如我们发现存在未经适当授权收集未成年人信息的情况，我们将依法尽快处理。
        </p>
        <p className="mt-2 text-muted-foreground">
          If we become aware that information of a minor has been collected
          without appropriate authorization, we will address it as soon as
          possible in accordance with law.
        </p>
      </LegalSection>

      <LegalSection number="10" title="第三方网站与服务 / Third-Party Websites &amp; Services">
        <p>本网站可能包含第三方网站、支付页面、插件或外部服务链接。</p>
        <p className="mt-2 text-muted-foreground">
          This website may contain links to third-party websites, payment pages,
          plugins or external services.
        </p>
        <p className="mt-4">
          第三方服务由其独立运营，其数据处理规则不受本隐私政策直接约束。您在使用第三方服务前，应自行查阅其相关条款和隐私政策。
        </p>
        <p className="mt-2 text-muted-foreground">
          Third-party services are operated independently, and their data
          processing rules are not directly governed by this Privacy Policy.
          Before using a third-party service, you should review its applicable
          terms and privacy policy yourself.
        </p>

        <div className="mt-4">
          <h3 className="text-base font-semibold text-foreground">
            10.1 第三方云存储归档（运营方侧）/ Third-Party Cloud Storage Archiving (Operator Side)
          </h3>
          <p className="mt-2">
            为支持长期归档与跨地域容灾，运营方可能使用第三方云存储服务，对已完成任务的相关材料进行归档或备份。该归档由运营方在管理侧发起，普通用户不会接触到该流程，运营方也不会因此向第三方传输您的登录身份或账户资料。
          </p>
          <p className="mt-2 text-muted-foreground">
            To support long-term archiving and cross-region disaster recovery,
            the operator may use third-party cloud storage services to archive or
            back up materials relating to completed tasks. Such archiving is
            initiated by the operator on the administrative side; ordinary users
            do not come into contact with this process, and the operator does not
            thereby transmit your login identity or account information to any
            third party.
          </p>
          <p className="mt-2">
            需要说明的是，被归档的「已完成任务工程包」可能包含您上传的媒体、生成的文件、字幕或项目素材等任务内容。我们会按本政策约定的安全措施处理该等归档，并仅在确有归档或恢复需要时发起相应调用；第三方云存储服务对其自身的数据处理规则，由该服务的条款与隐私政策约束。
          </p>
          <p className="mt-2 text-muted-foreground">
            Please note that an archived &ldquo;completed-task project
            package&rdquo; may contain task content such as the media you
            uploaded, generated files, subtitles or project materials. We handle
            such archives in accordance with the security measures set out in
            this Policy, and only initiate the relevant calls where there is a
            genuine archiving or recovery need; the third-party cloud storage
            service&rsquo;s own data processing rules are governed by that
            service&rsquo;s terms and privacy policy.
          </p>
        </div>
      </LegalSection>

      <LegalSection number="11" title="隐私政策的更新 / Updates to This Privacy Policy">
        <p>我们可能根据业务变化、法律法规要求或合规需要更新本隐私政策。</p>
        <p className="mt-2 text-muted-foreground">
          We may update this Privacy Policy in light of business changes, legal
          and regulatory requirements, or compliance needs.
        </p>
        <p className="mt-4">更新后的版本将公布于本页面，并更新“最后更新”日期。</p>
        <p className="mt-2 text-muted-foreground">
          The updated version will be published on this page, and the &ldquo;last
          updated&rdquo; date will be revised.
        </p>
        <p className="mt-4">
          若发生重大变化，我们会视情况通过站内公告、邮件或其他合理方式进行提示。
        </p>
        <p className="mt-2 text-muted-foreground">
          In the event of material changes, we will, as appropriate, provide
          notice through in-site announcements, email or other reasonable means.
        </p>
      </LegalSection>

      <LegalSection number="12" title="联系我们 / Contact Us">
        <p>
          如果您对本隐私政策、您的个人信息、数据删除申请或其他隐私相关问题有任何疑问，请通过页面底部的运营主体信息或
          <Link
            href="/contact"
            className="text-foreground underline-offset-4 hover:underline"
          >
            《联系我们》
          </Link>
          页面联系我们。
        </p>
        <p className="mt-2 text-muted-foreground">
          If you have any questions about this Privacy Policy, your personal
          information, a data deletion request, or other privacy-related matters,
          please contact us via the operator details in the page footer or
          through our Contact page.
        </p>
      </LegalSection>
    </LegalPage>
    </>
  )
}
