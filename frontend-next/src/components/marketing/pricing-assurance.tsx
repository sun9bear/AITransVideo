import Link from "next/link"
import {
  ArrowRight,
  CircleCheckBig,
  CreditCard,
  FolderOutput,
  ShieldCheck,
} from "lucide-react"
import { DIGITAL_DELIVERABLES, PAYMENT_CHANNEL_NOTE } from "./company-info"

const RULES = [
  "购买后权益自动发放至当前登录账户，无需人工开通。",
  "试用结束不会自动扣费；是否升级由用户主动决定。",
  "付款方式以结算页展示为准，支付成功后才会确认开通。",
]

export function PricingAssurance() {
  return (
    <section className="mt-12 rounded-3xl border border-border bg-muted/40 p-6 sm:p-8">
      <div className="grid gap-8 lg:grid-cols-[1.15fr_0.85fr]">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wider text-primary">
            购买与开通说明
          </p>
          <h2 className="mt-2 text-2xl font-semibold tracking-tight text-foreground sm:text-3xl">
            购买的是账户内视频处理能力和数字化交付结果
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            AIVideoTrans 为在线数字化服务。用户完成支付后，可在账户内创建翻译任务、查看历史项目、下载交付结果并继续人工复核。
            不同套餐解锁的是处理时长、并发能力、工作台模式和下载权限。
          </p>

          <div className="mt-6 grid gap-3 sm:grid-cols-2">
            {DIGITAL_DELIVERABLES.map((item) => (
              <div
                key={item}
                className="flex items-center gap-3 rounded-2xl border border-border bg-card px-4 py-3 text-sm text-foreground"
              >
                <CircleCheckBig className="h-4 w-4 shrink-0 text-primary" />
                <span>{item}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="space-y-4">
          <div className="rounded-2xl border border-border bg-card p-5">
            <div className="flex items-center gap-3">
              <span className="rounded-full bg-primary/10 p-2 text-primary">
                <CreditCard className="h-4 w-4" />
              </span>
              <h3 className="text-base font-semibold text-foreground">结算规则</h3>
            </div>
            <p className="mt-3 text-sm leading-6 text-muted-foreground">
              {PAYMENT_CHANNEL_NOTE}
            </p>
          </div>

          <div className="rounded-2xl border border-border bg-card p-5">
            <div className="flex items-center gap-3">
              <span className="rounded-full bg-primary/10 p-2 text-primary">
                <FolderOutput className="h-4 w-4" />
              </span>
              <h3 className="text-base font-semibold text-foreground">开通后得到什么</h3>
            </div>
            <ul className="mt-3 space-y-2 text-sm leading-6 text-muted-foreground">
              {RULES.map((rule) => (
                <li key={rule} className="flex items-start gap-2">
                  <CircleCheckBig className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                  <span>{rule}</span>
                </li>
              ))}
            </ul>
          </div>

          <div className="rounded-2xl border border-border bg-card p-5">
            <div className="flex items-center gap-3">
              <span className="rounded-full bg-primary/10 p-2 text-primary">
                <ShieldCheck className="h-4 w-4" />
              </span>
              <h3 className="text-base font-semibold text-foreground">购买前可查看</h3>
            </div>
            <div className="mt-3 flex flex-col gap-3 text-sm">
              <Link
                href="/trial"
                className="inline-flex items-center justify-between rounded-xl border border-border px-4 py-3 text-muted-foreground transition-colors hover:text-foreground"
              >
                <span>免费试用说明</span>
                <ArrowRight className="h-4 w-4" />
              </Link>
              <Link
                href="/refund"
                className="inline-flex items-center justify-between rounded-xl border border-border px-4 py-3 text-muted-foreground transition-colors hover:text-foreground"
              >
                <span>退款政策</span>
                <ArrowRight className="h-4 w-4" />
              </Link>
              <Link
                href="/contact"
                className="inline-flex items-center justify-between rounded-xl border border-border px-4 py-3 text-muted-foreground transition-colors hover:text-foreground"
              >
                <span>联系客服</span>
                <ArrowRight className="h-4 w-4" />
              </Link>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
