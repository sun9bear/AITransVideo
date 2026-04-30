import type { Metadata } from "next"
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

export const metadata: Metadata = {
  title: "隐私政策 · AITrans.Video",
  description:
    "AITrans.Video 隐私政策：说明我们如何收集、使用、存储、共享、保护您的个人信息，以及您依法享有的相关权利。",
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
          <p className="mt-4">在使用本服务前，请您认真阅读并充分理解本隐私政策。</p>
        </>
      }
    >
      <LegalSection number="1" title="我们收集的信息">
        <p>在您使用本服务过程中，我们可能会根据功能需要收集以下信息：</p>

        <div className="mt-4">
          <h3 className="text-base font-semibold text-foreground">1.1 账户信息</h3>
          <p className="mt-2">
            包括您的邮箱地址、用户名、登录信息、账户标识、公司名称（如有）、国家/地区等。
          </p>
        </div>

        <div>
          <h3 className="text-base font-semibold text-foreground">
            1.2 支付与订单信息
          </h3>
          <p className="mt-2">
            当您购买付费服务时，我们可能会收集订单编号、套餐信息、支付状态、交易时间、账单信息、支付方式类型等。
          </p>
          <p className="mt-2">
            请注意：完整的银行卡号、支付账户密码等敏感支付信息通常由第三方支付服务商处理，我们一般不会直接保存完整支付凭证。
          </p>
        </div>

        <div>
          <h3 className="text-base font-semibold text-foreground">
            1.3 您主动上传或提交的信息
          </h3>
          <p className="mt-2">包括但不限于：</p>
          <ul className="mt-2 ml-5 list-disc space-y-1">
            <li>视频文件</li>
            <li>音频文件</li>
            <li>字幕文件</li>
            <li>文稿、脚本、翻译文本</li>
            <li>语音样本</li>
            <li>图片或封面资料</li>
            <li>项目名称、任务备注、输出配置</li>
            <li>您在客服、工单、反馈中提交的信息</li>
          </ul>
        </div>

        <div>
          <h3 className="text-base font-semibold text-foreground">
            1.4 设备与使用信息
          </h3>
          <p className="mt-2">为保障服务运行和安全，我们可能收集：</p>
          <ul className="mt-2 ml-5 list-disc space-y-1">
            <li>IP 地址</li>
            <li>浏览器类型</li>
            <li>设备类型</li>
            <li>操作系统</li>
            <li>访问页面</li>
            <li>功能使用记录</li>
            <li>时间戳</li>
            <li>日志信息</li>
            <li>崩溃记录</li>
            <li>性能监控数据</li>
          </ul>
        </div>

        <div>
          <h3 className="text-base font-semibold text-foreground">
            1.5 Cookies 与类似技术
          </h3>
          <p className="mt-2">
            我们可能使用 Cookies、本地存储、会话标识、分析工具等技术，以实现登录状态维持、偏好记忆、性能分析、安全控制、流量统计等目的。
          </p>
        </div>
      </LegalSection>

      <LegalSection number="2" title="我们如何使用这些信息">
        <p>我们收集和使用您的信息，主要用于以下目的：</p>
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
      </LegalSection>

      <LegalSection number="3" title="关于 AI 处理的特别说明">
        <p>
          3.1
          当您上传视频、音频、文本、字幕或语音样本时，这些内容可能会被用于完成自动语音识别、机器翻译、字幕对齐、语音合成、视频渲染、文件导出等处理流程。
        </p>
        <p>
          3.2
          上述处理可能通过我们自有系统或经我们接入的第三方服务提供能力完成，但仅限于实现本服务所必需的范围。
        </p>
        <p>
          3.3
          请您不要上传您无权处理的内容，也不要在未评估风险和取得必要授权的情况下上传机密、敏感、受监管或高风险数据。
        </p>
      </LegalSection>

      <LegalSection number="4" title="我们共享信息的情形">
        <p>
          我们不会将您的个人信息出售给第三方。在以下情形下，我们可能会共享您的信息：
        </p>

        <div className="mt-4">
          <h3 className="text-base font-semibold text-foreground">
            4.1 与服务提供商共享
          </h3>
          <p className="mt-2">
            为了实现网站托管、存储、支付、日志分析、语音识别、翻译、语音合成、通知发送、技术支持等功能，我们可能会将必要信息共享给受托的第三方服务提供商。
          </p>
        </div>

        <div>
          <h3 className="text-base font-semibold text-foreground">
            4.2 法律要求或风险控制
          </h3>
          <p className="mt-2">
            在法律法规要求、司法机关或行政机关依法要求、重大风险防控、维权取证或执行平台规则时，我们可能依法披露必要信息。
          </p>
        </div>

        <div>
          <h3 className="text-base font-semibold text-foreground">4.3 公司交易情形</h3>
          <p className="mt-2">
            如发生合并、分立、重组、收购、资产转让或类似交易，您的相关信息可能作为交易的一部分被转移，但我们会要求接收方继续依法保护您的信息。
          </p>
        </div>
      </LegalSection>

      <LegalSection number="5" title="信息存储与保存期限">
        <p>
          5.1
          我们会在实现本政策所述目的所必要的期限内保存您的信息，除非法律法规另有要求。
        </p>
        <p>
          5.2 不同类别信息的保存期限可能不同，通常会根据以下因素确定：
        </p>
        <ul className="ml-5 list-disc space-y-1">
          <li>您是否仍持有账户；</li>
          <li>您的项目和任务是否仍需下载、查看或售后支持；</li>
          <li>法律法规对财务、交易、合规记录的保存要求；</li>
          <li>争议处理、维权和安全审计需要。</li>
        </ul>
        <p>
          5.3
          超过必要保存期限后，我们会按照适用法律和内部规则删除、匿名化处理相关信息，或采取其他符合法律要求的处理方式。
        </p>
      </LegalSection>

      <LegalSection number="6" title="国际传输">
        <p>
          如您所在地区与我们服务器所在地不同，或我们使用了跨境技术服务商，您的信息可能会在不同国家或地区被处理、存储或传输。在适用法律要求的情况下，我们会采取合理措施保护跨境传输过程中的数据安全。
        </p>
      </LegalSection>

      <LegalSection number="7" title="您的权利">
        <p>在适用法律法规允许的范围内，您可能享有以下权利：</p>
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
        <p>
          如您希望行使上述权利，请发送邮件至{" "}
          <a
            href={SUPPORT_EMAIL_HREF}
            className="text-foreground underline-offset-4 hover:underline"
          >
            {SUPPORT_EMAIL}
          </a>
          。为保障安全，我们可能会要求您完成身份验证后再处理相关请求。
        </p>
      </LegalSection>

      <LegalSection number="8" title="信息安全">
        <p>
          8.1
          我们会采取合理的管理、技术和组织措施保护您的信息安全，包括但不限于访问控制、权限管理、日志审计、安全传输、风险监控等。
        </p>
        <p>
          8.2
          但请您理解，任何互联网传输、电子存储或系统环境都无法保证绝对安全，我们无法对非因我们原因导致的泄露、篡改、丢失或攻击承担绝对担保责任。
        </p>
        <p>
          8.3
          如发生可能影响您合法权益的安全事件，我们将依法采取补救措施，并在法律法规要求的范围内履行通知义务。
        </p>
      </LegalSection>

      <LegalSection number="9" title="未成年人保护">
        <p>本服务主要面向具有完全民事行为能力的用户。</p>
        <p>如您为未成年人，请在监护人同意和指导下使用本服务。</p>
        <p>
          如我们发现存在未经适当授权收集未成年人信息的情况，我们将依法尽快处理。
        </p>
      </LegalSection>

      <LegalSection number="10" title="第三方网站与服务">
        <p>本网站可能包含第三方网站、支付页面、插件或外部服务链接。</p>
        <p>
          第三方服务由其独立运营，其数据处理规则不受本隐私政策直接约束。您在使用第三方服务前，应自行查阅其相关条款和隐私政策。
        </p>
      </LegalSection>

      <LegalSection number="11" title="隐私政策的更新">
        <p>我们可能根据业务变化、法律法规要求或合规需要更新本隐私政策。</p>
        <p>更新后的版本将公布于本页面，并更新“最后更新”日期。</p>
        <p>
          若发生重大变化，我们会视情况通过站内公告、邮件或其他合理方式进行提示。
        </p>
      </LegalSection>

      <LegalSection number="12" title="联系我们">
        <p>
          如果您对本隐私政策、您的个人信息、数据删除申请或其他隐私相关问题有任何疑问，请通过页面底部的运营主体信息或
          <a
            href="/contact"
            className="text-foreground underline-offset-4 hover:underline"
          >
            《联系我们》
          </a>
          页面联系我们。
        </p>
      </LegalSection>
    </LegalPage>
  )
}
