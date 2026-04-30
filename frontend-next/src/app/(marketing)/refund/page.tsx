import type { Metadata } from "next"
import {
  LegalPage,
  LegalSection,
  LegalClauseList,
} from "@/components/marketing/legal-page"
import {
  SUPPORT_EMAIL,
  SUPPORT_EMAIL_HREF,
} from "@/components/marketing/company-info"

export const metadata: Metadata = {
  title: "退款政策 · AITrans.Video",
  description:
    "AITrans.Video 退款政策：详细说明订阅、点数购买、处理失败、异常扣费等情形下的退款规则与申请方式。",
}

const UPDATED_AT = "2026-04-20"

/**
 * `/refund` — Refund Policy.
 *
 * Covers subscriptions, one-off quotas, processing failures, and billing
 * dispute handling for digital services.
 */
export default function RefundPage() {
  return (
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
            购买 AITrans.Video 相关付费服务、订阅、点数、处理额度或其他数字化权益的情形。请您在购买前认真阅读本政策。
          </p>
        </>
      }
    >
      <LegalSection number="1" title="一般说明">
        <p>
          1.1
          本服务属于数字化服务、在线算力服务或按需处理型服务。与传统实物商品不同，一旦服务开始履行、任务进入处理流程、点数发生消耗或输出结果已生成，相关费用通常不支持无条件退款。
        </p>
        <p>
          1.2
          我们会根据具体产品形态、购买方式、任务状态、消耗情况、异常原因及适用法律规定，对退款申请进行审核处理。
        </p>
      </LegalSection>

      <LegalSection number="2" title="订阅服务退款规则">
        <p>
          2.1
          如您购买的是按月、按年或其他周期计费的订阅服务，您可以在下一个续费周期开始前取消自动续费。取消后，已支付订阅周期内的服务通常仍可使用至当前周期结束。
        </p>
        <p>
          2.2
          除适用法律另有强制规定外，已开始生效的订阅周期费用，原则上不因未使用、使用较少、主观不满意或中途停止使用而按比例退款。
        </p>
        <p>
          2.3
          如您认为存在误扣费、重复扣费或未经授权扣费，请在扣费发生后 7 日内联系{" "}
          <a
            href={SUPPORT_EMAIL_HREF}
            className="text-foreground underline-offset-4 hover:underline"
          >
            {SUPPORT_EMAIL}
          </a>{" "}
          申请核查。
        </p>
      </LegalSection>

      <LegalSection number="3" title="点数、额度包或按量购买退款规则">
        <p>
          3.1 如您购买的是点数、处理额度、任务包、一次性数字权益或其他按量消耗型商品：
        </p>
        <LegalClauseList
          items={[
            "未使用的点数或额度，在购买后 7 日内可申请退款，是否通过以实际审核结果为准；",
            "已使用、部分已使用、已消耗、已过期、活动赠送、补偿发放、不可转让的点数或额度，不支持退款；",
            "任务一旦提交并进入处理流程，相关点数或额度可能视为已开始履行，不再适用无条件退款。",
          ]}
        />
        <p>
          3.2
          如购买页面、活动页、套餐说明页另有特殊约定，以相关页面说明为准。
        </p>
      </LegalSection>

      <LegalSection number="4" title="处理失败与异常情况">
        <p>
          4.1
          若您提交的付费任务因经我们核实确认的系统技术故障、平台错误、服务异常而未能正常完成，且未向您交付可用结果，我们可根据实际情况选择以下一种或多种方式处理：
        </p>
        <LegalClauseList
          items={[
            "重新处理任务；",
            "返还相应点数或额度；",
            "提供部分退款；",
            "提供全额退款。",
          ]}
        />
        <p>4.2 以下情况通常不视为平台责任，不当然构成退款依据：</p>
        <LegalClauseList
          items={[
            "源视频或音频质量差、噪音大、语音不清、多人混杂导致识别效果不佳；",
            "翻译风格、措辞、字幕习惯、音色相似度、语速效果未完全符合您的主观预期；",
            "您上传了错误文件、错误版本或错误配置；",
            "输出结果仍需人工校对、剪辑或二次编辑；",
            "第三方平台限制、源内容本身问题、网络环境异常等非平台直接原因导致效果不理想。",
          ]}
        />
      </LegalSection>

      <LegalSection number="5" title="不支持退款的情形">
        <p>在适用法律允许的范围内，以下情形通常不支持退款：</p>
        <LegalClauseList
          items={[
            "已使用或已消耗的订阅权益、点数、额度、处理资源；",
            "因您自身原因导致的误操作、误购、重复提交任务；",
            "因您违反服务条款、平台规则或适用法律而导致账户被限制、冻结或终止；",
            "活动商品、特价商品、赠送权益、测试资格、邀请码权益等页面已注明“不支持退款”的情形；",
            "超出退款申请时限的请求；",
            "无法提供有效订单信息、付款凭证或必要说明，导致无法核实的请求。",
          ]}
        />
      </LegalSection>

      <LegalSection number="6" title="重复收费与支付异常">
        <p>
          6.1
          如出现重复扣费、订单金额异常、支付成功但权益未到账等情况，请尽快联系{" "}
          <a
            href={SUPPORT_EMAIL_HREF}
            className="text-foreground underline-offset-4 hover:underline"
          >
            {SUPPORT_EMAIL}
          </a>
          。
        </p>
        <p>
          6.2
          经核实属于平台或支付链路异常的，我们将为您处理补发权益、返还差额、恢复点数或退款。
        </p>
      </LegalSection>

      <LegalSection number="7" title="拒付与争议处理">
        <p>
          7.1
          如您对账单有疑问，建议您先通过{" "}
          <a
            href={SUPPORT_EMAIL_HREF}
            className="text-foreground underline-offset-4 hover:underline"
          >
            {SUPPORT_EMAIL}
          </a>{" "}
          联系我们，我们将尽力协助解决。
        </p>
        <p>
          7.2
          如您在未经沟通的情况下直接发起恶意拒付、欺诈性争议或明显滥用退款政策，我们有权暂停或限制您的账户、服务权限及后续购买资格。
        </p>
        <p>
          7.3
          对于已核实存在欺诈、盗刷、套现、滥用活动规则等行为的账户，我们有权拒绝退款并追究责任。
        </p>
      </LegalSection>

      <LegalSection number="8" title="退款申请方式">
        <p>
          如需申请退款，请通过邮箱{" "}
          <a
            href={SUPPORT_EMAIL_HREF}
            className="text-foreground underline-offset-4 hover:underline"
          >
            {SUPPORT_EMAIL}
          </a>{" "}
          提交申请。
        </p>
        <p>请在邮件中尽量提供以下信息：</p>
        <ul className="ml-5 list-disc space-y-1">
          <li>账户邮箱</li>
          <li>订单编号或交易编号</li>
          <li>购买时间</li>
          <li>付款金额</li>
          <li>退款原因</li>
          <li>相关截图、报错信息、任务链接或其他辅助材料</li>
        </ul>
      </LegalSection>

      <LegalSection number="9" title="审核与到账时间">
        <p>
          9.1 我们通常会在收到完整申请材料后的 3-7 个工作日内完成审核。
        </p>
        <p>
          9.2
          若退款申请通过，退款原路返回的到账时间取决于支付机构、银行或第三方支付渠道，通常需要额外 3-15 个工作日。
        </p>
        <p>9.3 实际到账时间以支付机构处理结果为准。</p>
      </LegalSection>

      <LegalSection number="10" title="特别说明">
        <p>
          10.1 本退款政策不排除或限制适用法律赋予消费者的强制性权利。
        </p>
        <p>
          10.2
          若您所在司法辖区对数字服务的撤回权、退款权或自动续费规则有特殊要求，我们将依法适用相关规则。
        </p>
      </LegalSection>

      <LegalSection number="11" title="联系我们">
        <p>
          如您对本退款政策有任何疑问，请通过页面底部的运营主体信息或
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
