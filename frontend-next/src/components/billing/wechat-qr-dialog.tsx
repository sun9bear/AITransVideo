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
 * Native QR codes can NOT be long-press-recognized inside WeChat. WeChat also
 * blocks paying from album-scanned screenshots, so mobile users need a second
 * scanning screen.
 */

import { useEffect, useRef, useState } from "react"
import { useTranslations } from "next-intl"
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
  const t = useTranslations("appBilling")
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
      <DialogContent
        className="h-fit w-[calc(100vw-2rem)] max-w-[420px] overflow-y-auto p-5 sm:p-4"
        style={{
          inset: "1rem",
          margin: "auto",
          maxHeight: "calc(100dvh - 2rem)",
          translate: "0 0",
        }}
      >
        <DialogHeader>
          <DialogTitle>{t("wechat.title")}</DialogTitle>
          <DialogDescription>
            {t("wechat.amountDue")} <span className="font-semibold tabular-nums">{formatYuan(amountFen)}</span>
          </DialogDescription>
        </DialogHeader>

        {status === "paid" ? (
          <div className="flex flex-col items-center gap-3 py-8">
            <CheckCircle2 className="h-10 w-10 text-primary" aria-hidden="true" />
            <p className="text-sm font-medium text-foreground">{t("wechat.paidTitle")}</p>
            <p className="text-xs text-muted-foreground">{t("wechat.paidBody")}</p>
          </div>
        ) : terminalFailed ? (
          <div className="flex flex-col items-center gap-3 py-8 text-center">
            <p className="text-sm font-medium text-foreground">
              {status === "failed" ? t("wechat.failedTitle") : t("wechat.closedTitle")}
            </p>
            <p className="text-xs leading-relaxed text-muted-foreground">
              {t("wechat.failedBody")}
            </p>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-4 py-2">
            <div
              className="mx-auto flex aspect-square max-w-full items-center justify-center rounded-lg bg-white p-3 shadow-sm"
              style={{ width: "min(70vw, 280px)" }}
            >
              <QRCode
                value={qrCodeUrl}
                size={256}
                style={{ height: "100%", width: "100%" }}
                viewBox="0 0 256 256"
                aria-label={t("wechat.qrAriaLabel")}
              />
            </div>
            <p className="px-2 text-center text-sm font-medium leading-relaxed text-foreground sm:hidden">
              {t("wechat.screenshotHint")}
            </p>
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              {pollExhausted ? (
                <span>{t("wechat.pollExhausted")}</span>
              ) : (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                  {t("wechat.waiting")}
                </>
              )}
            </div>
            <p className="px-2 text-center text-xs leading-relaxed text-muted-foreground">
              {t("wechat.scanHint")}
            </p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
