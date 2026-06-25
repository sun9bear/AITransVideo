"use client"

import { useState } from "react"
import { Link } from "@/i18n/navigation"
import { toast } from "sonner"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { getJobDisplayTitle } from "@/features/jobs/presentation"
import {
  createSmartPreviewJob,
  mapSmartPreviewCreateError,
} from "@/lib/api/smartPreviewClone"
import type { CreateTranslationJobInput } from "@/types/jobs"

/**
 * 智能版 3 分钟预览的预扣确认弹窗（P3e-4c 切片 3）.
 *
 * 这是付费克隆（MiniMax）的**用户显式触发面**——满足 CLAUDE.md 付费 API 硬约束的
 * 白名单豁免「✅ 用户在前端显式点击按钮触发」。勾选 consent + 点「确认并预扣」才发起
 * ``createSmartPreviewJob``（preview_mode + auto_voice_clone=true）。余额已知且不足 →
 * 预先挡住并引导充值（服务端仍是最终裁判，402 由 mapSmartPreviewCreateError 兜底）。
 */
interface SmartPreviewConfirmDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 共享 job 配置（source/speakers/转录/语向）；smart/preview/克隆具体项由 createSmartPreviewJob 强制。null = 尚未就绪。 */
  jobInput: CreateTranslationJobInput | null
  /** 当前可用点数（credits.total_available）；null = 读取失败/加载中（不本地硬挡，交服务端判定）。 */
  availableCredits: number | null
  /** Gateway pricing API returned ``smart_preview_clone_cost_credits``. */
  cloneCostCredits: number | null
  cloneCostLoadFailed: boolean
  /** 创建成功回调（与主表单一致：容器决定跳转/关闭）。 */
  onCreated: (job: { id: string; title: string }) => void
}

export function SmartPreviewConfirmDialog({
  open,
  onOpenChange,
  jobInput,
  availableCredits,
  cloneCostCredits,
  cloneCostLoadFailed,
  onCreated,
}: SmartPreviewConfirmDialogProps) {
  const [consented, setConsented] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [showRecharge, setShowRecharge] = useState(false)

  const normalizedCloneCost =
    typeof cloneCostCredits === "number" &&
    Number.isFinite(cloneCostCredits) &&
    cloneCostCredits >= 0
      ? Math.trunc(cloneCostCredits)
      : null
  const cloneCostReady = normalizedCloneCost !== null
  const cloneCostLabel = cloneCostReady
    ? `${normalizedCloneCost} 点`
    : cloneCostLoadFailed
      ? "暂时无法读取"
      : "读取中…"

  // 余额已知且不足 gateway 返回的预览克隆费用 → 本地预挡（服务端仍会最终判定）。
  const insufficientKnown =
    normalizedCloneCost !== null &&
    availableCredits !== null &&
    availableCredits < normalizedCloneCost

  function handleOpenChange(next: boolean) {
    if (submitting) return // 提交中不允许关闭（避免悬挂的预扣请求被用户误判为取消）。
    if (!next) {
      setConsented(false)
      setErrorMessage(null)
      setShowRecharge(false)
    }
    onOpenChange(next)
  }

  async function handleConfirm() {
    if (!jobInput || !consented || submitting || insufficientKnown || !cloneCostReady) return
    setSubmitting(true)
    setErrorMessage(null)
    setShowRecharge(false)
    try {
      const job = await createSmartPreviewJob(jobInput)
      toast.success(`预览任务已创建：${getJobDisplayTitle(job)}`)
      setConsented(false)
      onOpenChange(false)
      onCreated({ id: job.id, title: getJobDisplayTitle(job) })
    } catch (error) {
      const mapped = mapSmartPreviewCreateError(error, { cloneCostCredits })
      setErrorMessage(mapped.message)
      setShowRecharge(mapped.reason === "insufficient_credits")
    } finally {
      setSubmitting(false)
    }
  }

  const confirmDisabled =
    !jobInput || !consented || submitting || insufficientKnown || !cloneCostReady

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>试用智能版 · 3 分钟预览</DialogTitle>
          <DialogDescription>
            克隆主说话人音色，生成前 3 分钟带水印的在线预览。满意后可转完整成片（按分钟正常扣点，复用不再重复扣克隆费）。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-sm">
          <div className="flex items-center justify-between rounded-lg border border-border bg-muted/30 px-3 py-2">
            <span className="text-muted-foreground">本次预扣（克隆主说话人）</span>
            <span className="font-semibold text-foreground">{cloneCostLabel}</span>
          </div>
          <div className="flex items-center justify-between px-1 text-xs">
            <span className="text-muted-foreground">当前可用</span>
            <span
              className={
                insufficientKnown
                  ? "font-medium text-[color:var(--cinnabar)]"
                  : "text-foreground"
              }
            >
              {availableCredits === null ? "读取中…" : `${availableCredits} 点`}
            </span>
          </div>

          {insufficientKnown ? (
            <p className="rounded-lg border border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/5 px-3 py-2 text-xs leading-relaxed text-[color:var(--cinnabar)]">
              余额不足 {cloneCostLabel}。
              <Link href="/settings/billing" className="ml-1 underline underline-offset-2">
                去充值
              </Link>
            </p>
          ) : null}

          {!cloneCostReady ? (
            <p className="rounded-lg border border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/5 px-3 py-2 text-xs leading-relaxed text-[color:var(--cinnabar)]">
              {cloneCostLoadFailed ? "扣点信息暂不可用，请稍后重试。" : "扣点信息读取中，请稍候。"}
            </p>
          ) : null}

          <label className="flex cursor-pointer items-start gap-2.5">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 shrink-0 accent-[color:var(--primary)]"
              checked={consented}
              disabled={submitting}
              onChange={(e) => setConsented(e.target.checked)}
            />
            <span className="text-xs leading-relaxed text-muted-foreground">
              我已了解本次预览将克隆主说话人音色并预扣 {cloneCostLabel}，且我拥有该视频的声音使用授权。
            </span>
          </label>

          {errorMessage ? (
            <p className="rounded-lg border border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/5 px-3 py-2 text-xs leading-relaxed text-[color:var(--cinnabar)]">
              {errorMessage}
              {showRecharge ? (
                <Link href="/settings/billing" className="ml-1 underline underline-offset-2">
                  去充值
                </Link>
              ) : null}
            </p>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => handleOpenChange(false)} disabled={submitting}>
            取消
          </Button>
          <Button onClick={handleConfirm} disabled={confirmDisabled}>
            {submitting ? "创建中…" : `确认并预扣 ${cloneCostLabel}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
