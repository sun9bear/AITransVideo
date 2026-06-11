"use client"

import * as React from "react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

export interface ConfirmDialogOptions {
  /** 弹窗正文；`\n` 换行通过 whitespace-pre-line 保留 */
  description: string
  title?: string
  confirmLabel?: string
  cancelLabel?: string
  /** 删除 / 清理等不可逆操作时确认按钮用 destructive 样式 */
  destructive?: boolean
}

export interface ConfirmDialogProps extends ConfirmDialogOptions {
  open: boolean
  onConfirm: () => void
  onCancel: () => void
}

export function ConfirmDialog({
  open,
  title = "请确认",
  description,
  confirmLabel = "确定",
  cancelLabel = "取消",
  destructive = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const confirmButtonRef = React.useRef<HTMLButtonElement>(null)
  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) onCancel()
      }}
    >
      {/* initialFocus 指向确认按钮：打开后 Enter 即确认；focus trap 与
          Escape 关闭（= 取消）由 base-ui modal Dialog 自带。 */}
      <DialogContent initialFocus={confirmButtonRef} showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription className="whitespace-pre-line">
            {description}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            {cancelLabel}
          </Button>
          <Button
            ref={confirmButtonRef}
            variant={destructive ? "destructive" : "default"}
            onClick={onConfirm}
          >
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * Promise 版 window.confirm 替代：
 *
 *   const { confirm, confirmDialog } = useConfirmDialog()
 *   // JSX 里渲染 {confirmDialog}，然后：
 *   if (!(await confirm({ description: "确定要…吗？" }))) return
 */
export function useConfirmDialog() {
  const [open, setOpen] = React.useState(false)
  const [options, setOptions] = React.useState<ConfirmDialogOptions | null>(null)
  const resolveRef = React.useRef<((confirmed: boolean) => void) | null>(null)

  // 宿主组件卸载（或走 early-return 分支不再渲染 confirmDialog）时，
  // 未决的 confirm 按取消结算，避免调用方 await 永久悬挂
  React.useEffect(() => {
    return () => {
      resolveRef.current?.(false)
      resolveRef.current = null
    }
  }, [])

  const confirm = React.useCallback((nextOptions: ConfirmDialogOptions) => {
    return new Promise<boolean>((resolve) => {
      // 同一时刻只有一个确认框；未决的旧 Promise 按取消结算，避免悬挂
      resolveRef.current?.(false)
      resolveRef.current = resolve
      setOptions(nextOptions)
      setOpen(true)
    })
  }, [])

  const settle = React.useCallback((confirmed: boolean) => {
    resolveRef.current?.(confirmed)
    resolveRef.current = null
    // 保留 options，退出动画期间文案不闪空
    setOpen(false)
  }, [])

  const confirmDialog = (
    <ConfirmDialog
      open={open}
      description={options?.description ?? ""}
      title={options?.title}
      confirmLabel={options?.confirmLabel}
      cancelLabel={options?.cancelLabel}
      destructive={options?.destructive}
      onConfirm={() => settle(true)}
      onCancel={() => settle(false)}
    />
  )

  return { confirm, confirmDialog }
}
