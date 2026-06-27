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
  "AITrans.Video 服务条款：详细说明本服务的使用规则、用户权利与义务、知识产权、费用、免责及争议解决条款。"

export const metadata: Metadata = {
  // Short title — root layout template adds " · 爱译视频 AITrans.Video".
  title: "服务条款",
  description: PAGE_DESCRIPTION,
  alternates: { canonical: "/terms" },
  openGraph: {
    title: "服务条款 · 爱译视频",
    description: PAGE_DESCRIPTION,
    url: absoluteUrl("/terms"),
    type: "website",
  },
}

const UPDATED_AT = "2026-04-20"

/**
 * `/terms` — Terms of Service.
 *
 * Structured into numbered sections for clear reference by support, legal,
 * users, and payment-partner reviewers.
 */
export default function TermsPage() {
  return (
    <>
      <BreadcrumbJsonLd
        items={[
          { name: "首页", path: "/" },
          { name: "服务条款", path: "/terms" },
        ]}
      />
      <LegalPage
      eyebrow="服务条款"
      title="服务条款"
      titleEn="Terms of Service"
      updatedAt={UPDATED_AT}
      intro={
        <>
          <p>
            欢迎使用 AITrans.Video（以下简称“本服务”）。本服务由{" "}
            <span className="text-foreground">{COMPANY_NAME}</span>
            （以下简称“我们”）运营，并通过{" "}
            <span className="text-foreground">aitrans.video</span>
            {" "}及相关网页、应用程序、接口、工具和功能向用户提供 AI
            视频翻译、字幕生成、语音合成、配音、本地化处理及相关服务。
          </p>
          <p className="mt-2 text-muted-foreground">
            Welcome to AITrans.Video (the &ldquo;Service&rdquo;). The Service is
            operated by <span className="text-foreground">{COMPANY_NAME}</span>{" "}
            (&ldquo;we&rdquo; or &ldquo;us&rdquo;) and is provided to users
            through <span className="text-foreground">aitrans.video</span> and
            related web pages, applications, interfaces, tools and features,
            offering AI video translation, subtitle generation, speech
            synthesis, dubbing, localization processing and related services.
          </p>
          <p className="mt-4">
            在访问、注册、使用本服务前，请您认真阅读并充分理解本《服务条款》（以下简称“本条款”）。您一旦访问或使用本服务，即表示您已阅读、理解并同意接受本条款的全部内容；如您不同意本条款，请立即停止访问或使用本服务。
          </p>
          <p className="mt-2 text-muted-foreground">
            Before accessing, registering for, or using the Service, please read
            and fully understand these Terms of Service (the &ldquo;Terms&rdquo;).
            By accessing or using the Service, you indicate that you have read,
            understood and agree to be bound by all of these Terms; if you do not
            agree to these Terms, please stop accessing or using the Service
            immediately.
          </p>
        </>
      }
    >
      <LegalSection number="1" title="服务内容 / Scope of Service">
        <p>
          1.1
          本服务是一个基于人工智能技术的视频本地化平台，可能包含但不限于以下功能：音视频上传、语音识别、文本翻译、字幕生成、字幕对齐、语音合成、配音生成、成片导出、编辑素材包导出及其他相关功能。
        </p>
        <p className="mt-2 text-muted-foreground">
          1.1 The Service is an AI-based video localization platform that may
          include, without limitation, the following features: audio/video
          upload, speech recognition, text translation, subtitle generation,
          subtitle alignment, speech synthesis, dubbing generation, finished
          video export, editing materials pack export, and other related
          features.
        </p>
        <p className="mt-4">
          1.2
          本服务的具体功能、计费方式、可用范围、处理速度、输出格式及配套能力，以本服务届时实际提供的页面说明、产品介绍、订阅方案、购买页或其他官方说明为准。
        </p>
        <p className="mt-2 text-muted-foreground">
          1.2 The specific features, billing methods, availability, processing
          speed, output formats and supporting capabilities of the Service are
          determined by the page descriptions, product introductions,
          subscription plans, purchase pages or other official statements that
          the Service actually provides at the relevant time.
        </p>
        <p className="mt-4">
          1.3
          我们有权根据业务发展、技术升级、运营安排或合规要求，对服务内容进行新增、调整、中断、限制或下线，且无需就单项功能永久持续提供作出承诺。
        </p>
        <p className="mt-2 text-muted-foreground">
          1.3 We may, based on business development, technical upgrades,
          operational arrangements or compliance requirements, add to, adjust,
          interrupt, restrict or discontinue the Service content, and we make no
          commitment to provide any individual feature permanently or
          continuously.
        </p>
      </LegalSection>

      <LegalSection number="2" title="账户注册与使用 / Account Registration &amp; Use">
        <p>
          2.1
          您在使用本服务部分功能时，可能需要注册账户并提供真实、准确、完整、最新的注册信息。
        </p>
        <p className="mt-2 text-muted-foreground">
          2.1 To use certain features of the Service, you may need to register an
          account and provide true, accurate, complete and up-to-date
          registration information.
        </p>
        <p className="mt-4">
          2.2
          您应妥善保管账户、密码、验证码及其他登录凭证，并对该账户下发生的一切操作与后果承担责任。
        </p>
        <p className="mt-2 text-muted-foreground">
          2.2 You are responsible for safeguarding your account, password,
          verification codes and other login credentials, and you are
          responsible for all activities and consequences that occur under your
          account.
        </p>
        <p className="mt-4">
          2.3
          如您发现账户存在未经授权使用、安全漏洞或异常情况，应立即通过{" "}
          <a
            href={SUPPORT_EMAIL_HREF}
            className="text-foreground underline-offset-4 hover:underline"
          >
            {SUPPORT_EMAIL}
          </a>{" "}
          通知我们。
        </p>
        <p className="mt-2 text-muted-foreground">
          2.3 If you discover any unauthorized use, security vulnerability or
          abnormal situation involving your account, you should notify us
          immediately at {SUPPORT_EMAIL}.
        </p>
        <p className="mt-4">
          2.4
          未经我们书面许可，您不得出租、出借、转让、出售或以其他方式许可他人使用您的账户。
        </p>
        <p className="mt-2 text-muted-foreground">
          2.4 Without our written permission, you may not rent, lend, transfer,
          sell or otherwise license your account to any third party.
        </p>
      </LegalSection>

      <LegalSection number="3" title="用户上传内容 / User-Uploaded Content">
        <p>
          3.1
          您可以在本服务中上传视频、音频、字幕文件、文稿、图片、语音样本、项目资料及其他内容（以下简称“用户内容”）。
        </p>
        <p className="mt-2 text-muted-foreground">
          3.1 You may upload videos, audio, subtitle files, scripts, images,
          voice samples, project materials and other content to the Service (the
          &ldquo;User Content&rdquo;).
        </p>
        <p className="mt-4">3.2 您对用户内容及其来源、合法性、完整性、准确性承担全部责任。</p>
        <p className="mt-2 text-muted-foreground">
          3.2 You are fully responsible for the User Content and for its source,
          legality, integrity and accuracy.
        </p>
        <p className="mt-4">
          3.3
          您确认并保证，您上传、处理、合成、导出、发布或以其他方式使用用户内容及相关输出结果，已经获得必要且充分的授权、同意或许可，不侵犯任何第三方的著作权、商标权、肖像权、声音权益、隐私权、个人信息权益、商业秘密或其他合法权益。
        </p>
        <p className="mt-2 text-muted-foreground">
          3.3 You confirm and warrant that your uploading, processing, synthesis,
          export, publication or other use of the User Content and related output
          has obtained the necessary and sufficient authorization, consent or
          licenses, and does not infringe any third party&rsquo;s copyright,
          trademark, portrait rights, voice rights, privacy, personal
          information rights, trade secrets or other lawful rights and interests.
        </p>
        <p className="mt-4">
          3.4
          为向您提供本服务，您授予我们一项有限的、非独占的、可在全球范围内使用的授权，允许我们在提供、维护、优化和保护本服务所必需的范围内，对您的用户内容进行存储、处理、复制、传输、转换、渲染、分析和生成输出结果。
        </p>
        <p className="mt-2 text-muted-foreground">
          3.4 To provide the Service to you, you grant us a limited,
          non-exclusive, worldwide authorization to store, process, copy,
          transmit, convert, render, analyze and generate output from your User
          Content, to the extent necessary to provide, maintain, optimize and
          protect the Service.
        </p>
        <p className="mt-4">
          3.5
          除为向您提供服务、履行法定义务、保障系统安全、处理争议或经您另行授权外，我们不会擅自将您的用户内容用于与本服务无关的用途。
        </p>
        <p className="mt-2 text-muted-foreground">
          3.5 Except to provide the Service to you, perform statutory
          obligations, safeguard system security, handle disputes, or as
          otherwise authorized by you, we will not use your User Content for
          purposes unrelated to the Service without authorization.
        </p>
      </LegalSection>

      <LegalSection number="4" title="用户承诺与禁止行为 / User Commitments &amp; Prohibited Conduct">
        <p>
          4.1
          您在使用本服务时，应遵守适用法律法规、监管要求、公序良俗及本条款约定。
        </p>
        <p className="mt-2 text-muted-foreground">
          4.1 When using the Service, you must comply with applicable laws and
          regulations, regulatory requirements, public order and good morals, and
          these Terms.
        </p>
        <p className="mt-4">4.2 您不得利用本服务从事以下行为：</p>
        <p className="mt-1 text-muted-foreground">
          4.2 You may not use the Service to engage in any of the following:
        </p>
        <LegalClauseList
          items={[
            "上传、生成、传播违法、侵权、欺诈、诽谤、淫秽、暴力、恐怖、骚扰、歧视性内容；",
            "未经授权处理、克隆、模拟、复制他人声音、肖像、视频形象或身份特征；",
            "利用本服务制作虚假宣传、冒名内容、深度伪造诈骗材料、误导性媒体或其他不当内容；",
            "侵犯第三方知识产权、人格权、数据权益或其他合法权益；",
            "攻击、干扰、破坏、绕过本服务的正常运行、访问控制、计费逻辑、安全机制或反滥用规则；",
            "通过爬虫、脚本、逆向工程、自动化滥刷、异常调用等方式不当使用本服务；",
            "利用本服务处理法律法规禁止处理的内容，或违反您对第三方承担的保密义务、合同义务；",
            "其他我们有合理理由认定为违法、违规、侵权、滥用或存在重大风险的行为。",
          ]}
        />
        <LegalClauseList
          items={[
            "uploading, generating or disseminating illegal, infringing, fraudulent, defamatory, obscene, violent, terrorist, harassing or discriminatory content;",
            "processing, cloning, imitating or copying another person's voice, likeness, on-screen image or identity features without authorization;",
            "using the Service to create false advertising, impersonation content, deepfake scam materials, misleading media or other improper content;",
            "infringing any third party's intellectual property, personality rights, data rights or other lawful rights and interests;",
            "attacking, interfering with, disrupting or circumventing the normal operation, access controls, billing logic, security mechanisms or anti-abuse rules of the Service;",
            "improperly using the Service through crawlers, scripts, reverse engineering, automated abuse, abnormal calls or similar means;",
            "using the Service to process content whose processing is prohibited by laws and regulations, or in breach of confidentiality or contractual obligations you owe to third parties;",
            "other conduct that we reasonably determine to be illegal, non-compliant, infringing, abusive or of significant risk.",
          ]}
        />
        <p className="mt-4">
          4.3
          若我们发现或合理怀疑您存在上述行为，我们有权视情节采取限制功能、暂停处理、删除内容、冻结账户、终止服务、拒绝退款、追究法律责任等措施。
        </p>
        <p className="mt-2 text-muted-foreground">
          4.3 If we discover or reasonably suspect any of the above conduct, we
          may, depending on the circumstances, take measures such as restricting
          features, suspending processing, deleting content, freezing the
          account, terminating the Service, refusing refunds, and pursuing legal
          liability.
        </p>
      </LegalSection>

      <LegalSection number="5" title="AI 输出结果说明 / AI Output">
        <p>
          5.1
          本服务基于自动化和人工智能技术运行，输出结果可能存在识别误差、翻译偏差、时序不准、语义遗漏、音色失真、断句不自然、配音不稳定或其他瑕疵。
        </p>
        <p className="mt-2 text-muted-foreground">
          5.1 The Service operates on automated and artificial-intelligence
          technology, and its output may contain recognition errors, translation
          deviations, timing inaccuracies, semantic omissions, voice distortion,
          unnatural sentence segmentation, unstable dubbing or other defects.
        </p>
        <p className="mt-4">
          5.2
          您理解并同意，AI 输出结果仅为技术处理结果，不构成我们对结果准确性、完整性、合法性、适商性、适特定用途性的承诺。
        </p>
        <p className="mt-2 text-muted-foreground">
          5.2 You understand and agree that the AI output is merely the result of
          technical processing and does not constitute any commitment by us as to
          the accuracy, completeness, legality, merchantability or fitness for a
          particular purpose of the results.
        </p>
        <p className="mt-4">
          5.3
          您应在发布、传播、商用、提交给客户或用于其他正式用途前，自行对输出结果进行审核、校对、编辑和确认。
        </p>
        <p className="mt-2 text-muted-foreground">
          5.3 Before publishing, distributing, commercializing, submitting to
          clients or using the output for any other formal purpose, you should
          review, proofread, edit and confirm the output yourself.
        </p>
        <p className="mt-4">
          5.4
          因您未审查输出结果即对外使用而产生的风险、损失、投诉、索赔或责任，由您自行承担。
        </p>
        <p className="mt-2 text-muted-foreground">
          5.4 You bear sole responsibility for any risks, losses, complaints,
          claims or liabilities arising from your external use of the output
          without reviewing it.
        </p>
      </LegalSection>

      <LegalSection number="6" title="声音克隆、肖像与敏感内容特别说明 / Voice Cloning, Likeness &amp; Sensitive Content">
        <p>
          6.1
          若您上传语音样本、人物影像、面部资料或其他能够识别自然人身份的信息，您应确保已获得相关主体的明确授权，且该等授权覆盖上传、分析、合成、生成及后续使用等环节。
        </p>
        <p className="mt-2 text-muted-foreground">
          6.1 If you upload voice samples, images of individuals, facial data or
          other information that can identify a natural person, you must ensure
          that you have obtained the explicit authorization of the relevant
          person, and that such authorization covers the uploading, analysis,
          synthesis, generation and subsequent use.
        </p>
        <p className="mt-4">
          6.2
          您不得使用本服务对未经授权的自然人进行声音克隆、肖像模仿、身份伪装或其他可能误导公众、损害他人权益的行为。
        </p>
        <p className="mt-2 text-muted-foreground">
          6.2 You may not use the Service to perform voice cloning, likeness
          imitation, identity impersonation, or other acts that may mislead the
          public or harm others&rsquo; rights with respect to any unauthorized
          natural person.
        </p>
        <p className="mt-4">
          6.3
          您不得上传依法受特别保护的数据、内容或受严格监管的信息，除非您已确认本服务适用于该类场景，且已取得全部必要合法依据和授权。
        </p>
        <p className="mt-2 text-muted-foreground">
          6.3 You may not upload data, content or information that is specially
          protected or strictly regulated by law, unless you have confirmed that
          the Service is suitable for such scenarios and have obtained all
          necessary legal bases and authorizations.
        </p>
      </LegalSection>

      <LegalSection number="7" title="费用、订阅与点数 / Fees, Subscriptions &amp; Credits">
        <p>
          7.1
          本服务的部分功能可能为付费功能。收费标准、计费单位、点数消耗规则、套餐周期、试用政策、自动续费安排，以购买页面或产品页面届时展示的信息为准。
        </p>
        <p className="mt-2 text-muted-foreground">
          7.1 Some features of the Service may be paid features. The fee
          standards, billing units, credit-consumption rules, plan periods, trial
          policies and any auto-renewal arrangements are determined by the
          information displayed on the purchase page or product page at the
          relevant time.
        </p>
        <p className="mt-4">
          7.2
          如您购买订阅或周期套餐，除页面另有明确说明外，该等套餐按所选周期一次性付费购买；本服务不会自动续费，亦不会在周期到期时通过您的支付方式自动扣款，如需继续使用，由您主动再次购买。如我们未来推出自动续费类产品，将在购买页面单独展示并经您单独确认后方可适用。
        </p>
        <p className="mt-2 text-muted-foreground">
          7.2 If you purchase a subscription or periodic plan, then unless the
          page expressly states otherwise, such a plan is a one-time purchase for
          the selected period; the Service does not auto-renew and will not
          automatically charge your payment method when the period ends, and you
          choose to purchase again to continue. Should we introduce an
          auto-renewal product in the future, it will be displayed separately on
          the purchase page and will apply only after your separate confirmation.
        </p>
        <p className="mt-4">
          7.3
          如您购买点数、处理额度、用量包或其他一次性数字商品，除页面明确另有说明外，该等权益通常仅供您本人账户使用，不可转让、转售或兑现。
        </p>
        <p className="mt-2 text-muted-foreground">
          7.3 If you purchase credits, processing allowances, usage packs or other
          one-time digital items, then unless the page expressly states
          otherwise, such entitlements are generally for use only by your own
          account and may not be transferred, resold or cashed out.
        </p>
        <p className="mt-4">
          7.4
          您应确保支付信息真实、有效。因扣款失败、账户限制、支付争议、拒付等原因导致服务无法继续的，我们有权暂停或限制相关服务。
        </p>
        <p className="mt-2 text-muted-foreground">
          7.4 You must ensure that your payment information is true and valid.
          Where the Service cannot continue due to failed charges, account
          restrictions, payment disputes, chargebacks or similar reasons, we may
          suspend or restrict the relevant services.
        </p>
      </LegalSection>

      <LegalSection number="8" title="退款与售后 / Refunds &amp; After-Sales">
        <p>
          8.1
          本服务为数字化、算力型、按需处理型服务。一旦服务开始履行、任务进入处理流程、点数已消耗或输出结果已生成，相关费用可能不支持退款。
        </p>
        <p className="mt-2 text-muted-foreground">
          8.1 The Service is a digital, compute-based, on-demand service. Once the
          service begins, a job enters processing, credits are consumed, or output
          is generated, the related fees may not be refundable.
        </p>
        <p className="mt-4">
          8.2
          退款条件、退款时限、退款流程及不予退款的具体情形，以
          <Link
            href="/refund"
            className="text-foreground underline-offset-4 hover:underline"
          >
            《退款政策》
          </Link>
          页面约定为准。
        </p>
        <p className="mt-2 text-muted-foreground">
          8.2 The refund conditions, refund time limits, refund process and the
          specific non-refundable situations are governed by the{" "}
          <Link
            href="/refund"
            className="text-foreground underline-offset-4 hover:underline"
          >
            Refund Policy
          </Link>{" "}
          page.
        </p>
        <p className="mt-4">
          8.3
          如您对扣费、重复收费、处理失败、异常消耗等问题有异议，请及时通过{" "}
          <a
            href={SUPPORT_EMAIL_HREF}
            className="text-foreground underline-offset-4 hover:underline"
          >
            {SUPPORT_EMAIL}
          </a>{" "}
          联系我们。
        </p>
        <p className="mt-2 text-muted-foreground">
          8.3 If you dispute any charge, duplicate billing, processing failure,
          abnormal consumption or similar matter, please contact us promptly at{" "}
          {SUPPORT_EMAIL}.
        </p>
      </LegalSection>

      <LegalSection number="9" title="知识产权 / Intellectual Property">
        <p>
          9.1
          本服务及其所包含的软件、界面、页面设计、标识、商标、代码、模型调用流程、平台结构、文档资料、网站内容及相关知识产权，均归{COMPANY_NAME}或相关权利人所有。
        </p>
        <p className="mt-2 text-muted-foreground">
          9.1 The Service and the software, interfaces, page design, logos,
          trademarks, code, model-invocation workflows, platform structure,
          documentation, website content and related intellectual property it
          contains are owned by {COMPANY_NAME} or the relevant rights holders.
        </p>
        <p className="mt-4">
          9.2
          未经我们事先书面许可，您不得以任何方式复制、修改、传播、公开展示、反向工程、出售、出租、转授权或以其他方式利用本服务的任何组成部分。
        </p>
        <p className="mt-2 text-muted-foreground">
          9.2 Without our prior written permission, you may not copy, modify,
          distribute, publicly display, reverse engineer, sell, rent, sublicense
          or otherwise exploit any component of the Service in any manner.
        </p>
        <p className="mt-4">
          9.3
          您依法享有您自行上传内容的权利，但本条款另有约定或法律另有规定的除外。
        </p>
        <p className="mt-2 text-muted-foreground">
          9.3 You retain your lawful rights in the content you upload, except as
          otherwise provided in these Terms or required by law.
        </p>
      </LegalSection>

      <LegalSection number="10" title="第三方服务 / Third-Party Services">
        <p>
          10.1
          本服务可能依赖或接入第三方服务，包括但不限于云存储、支付服务、语音识别、翻译模型、语音合成、监控分析等第三方能力。
        </p>
        <p className="mt-2 text-muted-foreground">
          10.1 The Service may rely on or integrate third-party services,
          including without limitation third-party capabilities such as cloud
          storage, payment services, speech recognition, translation models,
          speech synthesis, and monitoring and analytics.
        </p>
        <p className="mt-4">
          10.2
          对于由第三方提供的产品、服务、接口、数据处理或支付结算，我们会在合理范围内选择和接入，但无法对第三方服务的持续性、稳定性、安全性、合法性或处理结果作出担保。
        </p>
        <p className="mt-2 text-muted-foreground">
          10.2 For products, services, interfaces, data processing or payment
          settlement provided by third parties, we select and integrate them
          within a reasonable scope, but we cannot guarantee the continuity,
          stability, security, legality or processing results of such third-party
          services.
        </p>
        <p className="mt-4">
          10.3
          因第三方服务故障、限制、中断、调整、停止或政策变化引发的问题，我们将在合理范围内协助处理，但不因此当然承担超出法定义务范围的责任。
        </p>
        <p className="mt-2 text-muted-foreground">
          10.3 For issues arising from third-party service faults, restrictions,
          interruptions, adjustments, discontinuation or policy changes, we will
          assist within a reasonable scope, but we do not thereby assume liability
          beyond our statutory obligations.
        </p>
        <p className="mt-4">
          10.4
          管理后台提供的「归档备份」功能可能依赖第三方云存储服务的授权与接口能力，且仅向管理员账号开放。我们不对该第三方云存储服务的存储容量、速率、单文件大小、风控、限流或其他运营策略负责；相关第三方服务的使用须遵循其自身的服务条款与政策。
        </p>
        <p className="mt-2 text-muted-foreground">
          10.4 The &ldquo;archive backup&rdquo; feature in the admin console may
          rely on the authorization and interface capabilities of third-party
          cloud storage services and is available only to administrator accounts.
          We are not responsible for the storage capacity, speed, single-file
          size, risk controls, rate limits or other operating policies of such
          third-party cloud storage services; the use of the relevant third-party
          services is subject to their own terms of service and policies.
        </p>
      </LegalSection>

      <LegalSection number="11" title="服务中断、变更与终止 / Interruption, Change &amp; Termination">
        <p>11.1 在下列情形下，我们有权中断、限制、暂停或终止全部或部分服务：</p>
        <p className="mt-1 text-muted-foreground">
          11.1 We may interrupt, restrict, suspend or terminate all or part of the
          Service in the following circumstances:
        </p>
        <LegalClauseList
          items={[
            "系统维护、升级、迁移、修复；",
            "第三方服务异常；",
            "网络、安全、设备、黑客攻击、不可抗力等原因；",
            "法律法规、监管要求、司法或行政机关要求；",
            "您违反本条款或存在风险行为；",
            "其他我们认为有必要的合理情形。",
          ]}
        />
        <LegalClauseList
          items={[
            "system maintenance, upgrades, migration or repairs;",
            "abnormalities in third-party services;",
            "network, security, equipment, hacker attacks, force majeure or similar causes;",
            "requirements of laws and regulations, regulators, or judicial or administrative authorities;",
            "your breach of these Terms or risky conduct;",
            "other reasonable circumstances we consider necessary.",
          ]}
        />
        <p className="mt-4">
          11.2
          若您停止使用本服务，或您的账户被终止，已产生的费用、已发生的责任、已形成的争议解决条款、知识产权条款、免责声明、责任限制条款等仍然有效。
        </p>
        <p className="mt-2 text-muted-foreground">
          11.2 If you stop using the Service or your account is terminated, the
          fees already incurred, liabilities already arisen, and the
          dispute-resolution, intellectual-property, disclaimer and
          limitation-of-liability clauses already in effect remain valid.
        </p>
      </LegalSection>

      <LegalSection number="12" title="免责声明 / Disclaimers">
        <p>12.1 本服务按“现状”和“可提供”状态提供。</p>
        <p className="mt-2 text-muted-foreground">
          12.1 The Service is provided on an &ldquo;as is&rdquo; and &ldquo;as
          available&rdquo; basis.
        </p>
        <p className="mt-4">
          12.2
          在适用法律允许的最大范围内，我们不对以下事项作任何明示或默示保证：
        </p>
        <p className="mt-1 text-muted-foreground">
          12.2 To the maximum extent permitted by applicable law, we make no
          express or implied warranty as to:
        </p>
        <LegalClauseList
          items={[
            "服务不中断、无错误、无延迟；",
            "输出结果完全准确、完整、稳定、可商用；",
            "服务适用于您的特定业务场景、客户要求或监管环境；",
            "任何经由本服务处理后的内容不会引发争议、侵权或损失。",
          ]}
        />
        <LegalClauseList
          items={[
            "the Service being uninterrupted, error-free or delay-free;",
            "the output being fully accurate, complete, stable or commercially usable;",
            "the Service being suitable for your particular business scenario, client requirements or regulatory environment;",
            "any content processed through the Service not giving rise to disputes, infringement or loss.",
          ]}
        />
        <p className="mt-4">
          12.3
          您理解，AI 工具本质上属于辅助工具，最终内容责任、发布责任、商业使用责任及合规责任均由您自行承担。
        </p>
        <p className="mt-2 text-muted-foreground">
          12.3 You understand that AI tools are by nature assistive tools, and
          that the ultimate responsibility for the content, its publication, its
          commercial use and compliance rests with you.
        </p>
      </LegalSection>

      <LegalSection number="13" title="责任限制 / Limitation of Liability">
        <p>
          13.1
          在适用法律允许的最大范围内，对于因使用或无法使用本服务而产生的任何间接损失、附带损失、特殊损失、惩罚性损失或后果性损失，包括但不限于利润损失、业务中断、数据丢失、客户流失、商誉受损等，我们不承担责任。
        </p>
        <p className="mt-2 text-muted-foreground">
          13.1 To the maximum extent permitted by applicable law, we are not
          liable for any indirect, incidental, special, punitive or consequential
          loss arising from the use of or inability to use the Service, including
          without limitation loss of profits, business interruption, data loss,
          loss of customers or damage to goodwill.
        </p>
        <p className="mt-4">
          13.2
          在任何情况下，我们因本服务向您承担的累计赔偿责任总额，以您在相关争议发生前十二（12）个月内就本服务实际支付给我们的费用总额为限；如您在该期间内未支付任何费用，则责任上限为人民币 500 元。
        </p>
        <p className="mt-2 text-muted-foreground">
          13.2 In any event, our aggregate liability to you in connection with the
          Service is limited to the total fees you actually paid us for the
          Service in the twelve (12) months before the relevant dispute arose; if
          you paid no fees during that period, the liability cap is RMB 500.
        </p>
        <p className="mt-4">
          13.3
          本条责任限制不适用于适用法律明确禁止限制或排除责任的情形。
        </p>
        <p className="mt-2 text-muted-foreground">
          13.3 This limitation of liability does not apply where applicable law
          expressly prohibits the limitation or exclusion of liability.
        </p>
      </LegalSection>

      <LegalSection number="14" title="违约处理与赔偿 / Breach &amp; Indemnification">
        <p>
          14.1
          若因您违反本条款、上传侵权内容、未经授权处理他人声音/肖像、滥用本服务或从事违法活动，导致我们或第三方遭受投诉、索赔、处罚、诉讼、损失或费用，您应负责解决并承担全部责任。
        </p>
        <p className="mt-2 text-muted-foreground">
          14.1 If your breach of these Terms, uploading of infringing content,
          unauthorized processing of another person&rsquo;s voice/likeness, abuse
          of the Service or engagement in illegal activities causes us or any
          third party to suffer complaints, claims, penalties, litigation, loss or
          expense, you are responsible for resolving the matter and bear full
          liability.
        </p>
        <p className="mt-4">
          14.2
          如因此给我们造成损失，您应赔偿我们的全部损失，包括但不限于赔偿金、律师费、取证费、公证费、差旅费及其他合理支出。
        </p>
        <p className="mt-2 text-muted-foreground">
          14.2 If this causes us loss, you shall indemnify us for all of our
          losses, including without limitation damages, legal fees, evidence-
          collection fees, notarization fees, travel expenses and other
          reasonable expenditures.
        </p>
      </LegalSection>

      <LegalSection number="15" title="适用法律与争议解决 / Governing Law &amp; Dispute Resolution">
        <p>
          15.1
          本条款的订立、生效、解释、履行及争议解决，适用中华人民共和国法律。
        </p>
        <p className="mt-2 text-muted-foreground">
          15.1 The formation, effectiveness, interpretation, performance and
          dispute resolution of these Terms are governed by the laws of the
          People&rsquo;s Republic of China.
        </p>
        <p className="mt-4">
          15.2
          因本条款或本服务引起的任何争议，双方应首先友好协商；协商不成的，任一方均可向武汉市洪山区人民法院提起诉讼。
        </p>
        <p className="mt-2 text-muted-foreground">
          15.2 For any dispute arising from these Terms or the Service, the
          parties should first seek to resolve it through friendly negotiation;
          if negotiation fails, either party may bring a lawsuit before the Wuhan
          Hongshan District People&rsquo;s Court.
        </p>
      </LegalSection>

      <LegalSection number="16" title="条款更新 / Updates to These Terms">
        <p>
          16.1
          我们有权根据业务发展、法律要求、监管要求或服务变化对本条款进行修改。
        </p>
        <p className="mt-2 text-muted-foreground">
          16.1 We may modify these Terms based on business development, legal
          requirements, regulatory requirements or changes to the Service.
        </p>
        <p className="mt-4">
          16.2
          修改后的条款将在网站公布，并更新“最后更新”日期。若修改涉及重大变化，我们将视情况通过站内公告、邮件或其他合理方式提示您。
        </p>
        <p className="mt-2 text-muted-foreground">
          16.2 The modified Terms will be published on the website with an updated
          &ldquo;last updated&rdquo; date. Where the changes are material, we may
          notify you through in-site announcements, email or other reasonable
          means, as appropriate.
        </p>
        <p className="mt-4">
          16.3 您在更新后的条款生效后继续使用本服务，即视为您已接受更新后的条款。
        </p>
        <p className="mt-2 text-muted-foreground">
          16.3 If you continue to use the Service after the updated Terms take
          effect, you are deemed to have accepted the updated Terms.
        </p>
      </LegalSection>

      <LegalSection number="17" title="联系我们 / Contact">
        <p>
          如您对本条款有任何疑问、意见或投诉，请通过页面底部的运营主体信息或
          <Link
            href="/contact"
            className="text-foreground underline-offset-4 hover:underline"
          >
            《联系我们》
          </Link>
          页面联系我们。
        </p>
        <p className="mt-2 text-muted-foreground">
          If you have any questions, comments or complaints about these Terms,
          please contact us via the operator details in the page footer or through
          our{" "}
          <Link
            href="/contact"
            className="text-foreground underline-offset-4 hover:underline"
          >
            Contact
          </Link>{" "}
          page.
        </p>
      </LegalSection>
    </LegalPage>
    </>
  )
}
