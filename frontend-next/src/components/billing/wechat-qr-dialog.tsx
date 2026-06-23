"use client"

/**
 * In-page WeChat Native QR checkout dialog (plan 2026-05-22 T7).
 *
 * The gateway returns `display_mode: "qrcode"` + a `weixin://` code_url for
 * the wechatpay provider; this dialog renders it as an SVG QR (react-qr-code,
 * plan Q5) and polls the order until the webhook settles it. The webhook is
 * the settlement truth; polling (refresh=true → gateway query backstop) is
 * the UX confirmation channel.
 *
 * Closing the dialog is safe at any point: the pending-order stash written by
 * the checkout card keeps BillingStatusBanner polling in the background, so a
 * buyer who scans, closes, then pays still sees the page settle.
 *
 * Native QR codes can NOT be long-press-recognized inside WeChat, so the copy
 * tells mobile users to screenshot and pick the screenshot inside WeChat.
 */

import { useEffect, useRef, useState } from "react"
import QRCode from "react-qr-code"
import { CheckCircle2, Loader2 } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { getOrder, type PaymentOrderStatus } from "@/lib/billing/get-order"

const POLL_INTERVAL_MS = 3000
// Stop dialog-local polling after ~10 min (orders expire server-side at 30
// min; the banner stash keeps confirming at its own cadence after close).
const MAX_POLL_ATTEMPTS = 200

type WechatQrDialogProps = {
  orderId: string
  qrCodeUrl: string
  amountFen: number
  onClose: () => void
  onPaid: () => void
}

function formatYuan(fen: number): string {
  const yuan = fen / 100
  return yuan % 1 === 0 ? `¥${yuan.toFixed(0)}` : `¥${yuan.toFixed(2)}`
}

export function WechatQrDialog({
  orderId,
  qrCodeUrl,
  amountFen,
  onClose,
  onPaid,
}: WechatQrDialogProps) {
  const [status, setStatus] = useState<PaymentOrderStatus | "pending">("pending")
  const [pollExhausted, setPollExhausted] = useState(false)
  const onPaidRef = useRef(onPaid)

  useEffect(() => {
    onPaidRef.current = onPaid
  }, [onPaid])

  useEffect(() => {
    let cancelled = false

    async function poll() {
      for (let attempt = 0; attempt < MAX_POLL_ATTEMPTS && !cancelled; attempt += 1) {
        try {
          const order = await getOrder(orderId, { refresh: true })
          if (cancelled) return
          setStatus(order.status)
          if (order.status === "paid") {
            // Brief success beat so the user sees the confirmation, then
            // hand control back (parent refetches subscription + credits).
            await new Promise((r) => setTimeout(r, 1200))
            if (!cancelled) onPaidRef.current()
            return
          }
          if (
            order.status === "failed" ||
            order.status === "cancelled" ||
            order.status === "expired"
          ) {
            return // terminal non-paid: keep dialog open showing the state
          }
        } catch {
          // Transient poll error — keep going; the banner stash is the backstop.
        }
        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS))
      }
      if (!cancelled) setPollExhausted(true)
    }

    void poll()
    return () => {
      cancelled = true
    }
  }, [orderId])

  const terminalFailed =
    status === "failed" || status === "cancelled" || status === "expired"

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] w-[calc(100vw-2rem)] max-w-[420px] overflow-y-auto sm:max-w-[420px]">
        <DialogHeader>
          <DialogTitle>微信扫码支付</DialogTitle>
          <DialogDescription>
            应付金额 <span className="font-semibold tabular-nums">{formatYuan(amountFen)}</span>
          </DialogDescription>
        </DialogHeader>

        {status === "paid" ? (
          <div className="flex flex-col items-center gap-3 py-8">
            <CheckCircle2 className="h-10 w-10 text-primary" aria-hidden="true" />
            <p className="text-sm font-medium text-foreground">支付成功</p>
            <p className="text-xs text-muted-foreground">订阅与点数正在更新…</p>
          </div>
        ) : terminalFailed ? (
          <div className="flex flex-col items-center gap-3 py-8 text-center">
            <p className="text-sm font-medium text-foreground">
              {status === "failed" ? "支付失败" : "订单已关闭"}
            </p>
            <p className="text-xs leading-relaxed text-muted-foreground">
              本次订单未完成支付。关闭本窗口后可重新发起支付。
            </p>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-4 py-2">
            <div
              className="mx-auto flex aspect-square items-center justify-center rounded-lg bg-white p-3 shadow-sm"
              style={{ width: "min(72vw, 280px)" }}
            >
              <QRCode
                value={qrCodeUrl}
                size={256}
                style={{ height: "100%", width: "100%" }}
                viewBox="0 0 256 256"
                aria-label="微信支付二维码"
              />
            </div>
            <p className="px-2 text-center text-sm font-medium leading-relaxed text-foreground sm:hidden">
              手机端请截图后在微信中选择截图扫码。
            </p>
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              {pollExhausted ? (
                <span>确认超时。若你已完成支付,可关闭本窗口,到账后账单页会自动更新。</span>
              ) : (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                  等待支付确认…支付完成后本窗口会自动更新
                </>
              )}
            </div>
            <p className="px-2 text-center text-xs leading-relaxed text-muted-foreground">
              请打开手机微信,用「扫一扫」扫描上方二维码完成支付。
              二维码不支持长按识别;如你正在手机上浏览,可截图保存后在微信中选择截图扫码。
            </p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
