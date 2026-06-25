import type { Metadata } from "next"
import { Link } from "@/i18n/navigation"
import { COMPANY_ADDRESS, COMPANY_NAME, PAYMENT_CHANNEL_NOTE, SUPPORT_EMAIL } from "@/components/marketing/company-info"
import { LegalPage, LegalSection } from "@/components/marketing/legal-page"
import { BreadcrumbJsonLd } from "@/components/seo/breadcrumb-json-ld"
import { absoluteUrl } from "@/lib/seo/site"

const PAGE_DESCRIPTION =
  "联系 AITrans.Video：客服支持、账单退款、隐私请求、版权投诉、商务合作等各类联系方式。"

export const metadata: Metadata = {
  // Short title — root layout template adds " · 爱译视频 AITrans.Video".
  title: "联系我们",
  description: PAGE_DESCRIPTION,
  alternates: { canonical: "/contact" },
  openGraph: {
    title: "联系我们 · 爱译视频",
    description: PAGE_DESCRIPTION,
    url: absoluteUrl("/contact"),
    type: "website",
  },
}

const UPDATED_AT = "2026-04-22"

function Mail({ address, label }: { address: string; label?: string }) {
  return (
    <a href={`mailto:${address}`} className="text-foreground underline-offset-4 hover:underline">
      {label ?? address}
    </a>
  )
}

export default function ContactPage() {
  return (
    <>
      <BreadcrumbJsonLd
        items={[
          { name: "首页", path: "/" },
          { name: "联系我们", path: "/contact" },
        ]}
      />
      <LegalPage
      eyebrow="联系我们"
      title="联系我们"
      titleEn="Contact Us"
      updatedAt={UPDATED_AT}
      intro={
        <>
          <p>
            感谢您关注 AITrans.Video。如果您在使用过程中遇到技术问题、付款问题、退款申请、隐私请求、版权投诉或商务合作需求，
            欢迎通过以下方式与我们联系。
          </p>
          <p className="mt-4">
            目前所有类别的来信均统一发送至 <Mail address={SUPPORT_EMAIL} />
            ，并在邮件标题中注明对应类别，例如“账单/退款申请”“隐私请求”“商务合作”，我们会按类别分流处理。
          </p>
        </>
      }
    >
      <LegalSection number="1" title="客服支持">
        <p>如您遇到账户登录、任务失败、字幕异常、配音异常、文件导出、功能使用等问题，请联系：</p>
        <p>
          <span className="text-foreground">客服邮箱：</span>
          <Mail address={SUPPORT_EMAIL} />
        </p>
        <p>建议您在来信中提供以下信息，以便我们更快处理：</p>
        <ul className="ml-5 list-disc space-y-1">
          <li>您的账户邮箱</li>
          <li>问题发生时间</li>
          <li>相关任务名称或订单编号</li>
          <li>具体问题描述</li>
          <li>截图、报错信息或相关链接</li>
        </ul>
      </LegalSection>

      <LegalSection number="2" title="账单与退款">
        <p>如您遇到以下问题：</p>
        <ul className="ml-5 list-disc space-y-1">
          <li>付款失败</li>
          <li>重复扣费</li>
          <li>套餐未到账</li>
          <li>退款申请</li>
          <li>发票或账单问题</li>
        </ul>
        <p>
          请发送邮件至 <Mail address={SUPPORT_EMAIL} />
          ，并在邮件标题中注明“账单 / 退款申请”。具体退款规则请参考{" "}
          <Link href="/refund" className="text-foreground underline-offset-4 hover:underline">
            《退款政策》
          </Link>
          。
        </p>
      </LegalSection>

      <LegalSection number="3" title="隐私与数据请求">
        <p>如您需要：</p>
        <ul className="ml-5 list-disc space-y-1">
          <li>查询个人信息处理情况</li>
          <li>申请删除账户或数据</li>
          <li>提出隐私投诉</li>
          <li>提交数据权利请求</li>
        </ul>
        <p>
          请发送邮件至 <Mail address={SUPPORT_EMAIL} />
          ，并在邮件标题中注明“隐私请求”。具体隐私规则请参考{" "}
          <Link href="/privacy" className="text-foreground underline-offset-4 hover:underline">
            《隐私政策》
          </Link>
          。
        </p>
      </LegalSection>

      <LegalSection number="4" title="知识产权与侵权投诉">
        <p>
          如您认为本平台上的相关内容侵犯了您的著作权、商标权、肖像权、声音权益或其他合法权利，请发送投诉材料至{" "}
          <Mail address={SUPPORT_EMAIL} />
          ，并在邮件标题中注明“侵权投诉”。
        </p>
        <p>请尽量提供以下内容：</p>
        <ul className="ml-5 list-disc space-y-1">
          <li>权利人姓名 / 名称</li>
          <li>联系方式</li>
          <li>权利证明材料</li>
          <li>涉嫌侵权内容的具体链接或说明</li>
          <li>投诉理由及声明</li>
        </ul>
        <p>我们将在收到完整材料后依法依规处理。</p>
      </LegalSection>

      <LegalSection number="5" title="商务合作">
        <p>如您有以下合作需求：</p>
        <ul className="ml-5 list-disc space-y-1">
          <li>API 接入</li>
          <li>企业合作</li>
          <li>渠道合作</li>
          <li>定制化服务</li>
          <li>品牌合作</li>
        </ul>
        <p>
          请发送邮件至 <Mail address={SUPPORT_EMAIL} />
          ，并在邮件标题中注明“商务合作”。
        </p>
      </LegalSection>

      <LegalSection number="6" title="响应时间">
        <p>我们通常会在 2-5 个工作日内回复大多数咨询。</p>
        <p>如遇节假日、高峰期、复杂技术问题或需要进一步核实的情况，处理时间可能有所延长。</p>
      </LegalSection>

      <LegalSection number="7" title="运营主体信息">
        <p>
          <span className="text-foreground">主体名称：</span>
          {COMPANY_NAME}
        </p>
        <p>
          <span className="text-foreground">联系邮箱：</span>
          <Mail address={SUPPORT_EMAIL} />
        </p>
        {COMPANY_ADDRESS ? (
          <p>
            <span className="text-foreground">联系地址：</span>
            {COMPANY_ADDRESS}
          </p>
        ) : null}
        <p>{PAYMENT_CHANNEL_NOTE}</p>
      </LegalSection>
    </LegalPage>
    </>
  )
}
