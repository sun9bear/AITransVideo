"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslations } from "next-intl"
import { CheckCircle } from "lucide-react"

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { TranslationForm } from "@/components/workspace/TranslationForm"

interface NewTranslationDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Called after a job is created, so parent can refresh job list */
  onJobCreated?: () => void
  /** Pre-fill source URL for "recreate" from failed job */
  initialSourceUrl?: string
}

export function NewTranslationDialog({
  open,
  onOpenChange,
  onJobCreated,
  initialSourceUrl,
}: NewTranslationDialogProps) {
  const t = useTranslations("appTranslationForm")
  return (
    <Dialog open={open} onOpenChange={(value) => onOpenChange(value)}>
      {/* 宽度覆盖必须成对写 max-w-* + sm:max-w-*（CLAUDE.md Dialog 宽度约定）：
          裸 max-w-lg 会被 tailwind-merge 吃掉基类的移动端留白 max-w-[calc(100%-2rem)]，
          却压不过带断点前缀的 sm:max-w-sm —— 曾导致桌面端弹窗实际只有 384px。
          w-[calc(100vw-2rem)] 补回移动端留白；dvh 规避移动端动态工具栏遮挡；
          flex + 内层滚动让标题与关闭按钮常驻可见（不随长表单滚出视野）。 */}
      <DialogContent className="flex max-h-[85dvh] w-[calc(100vw-2rem)] max-w-lg flex-col overflow-hidden sm:max-w-xl md:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t("dialog.title")}</DialogTitle>
        </DialogHeader>
        {/* Key on `open` forces fresh mount each time dialog opens,
            resetting showSuccess without needing setState in useEffect */}
        {open && (
          <div className="-mx-4 min-h-0 overflow-y-auto px-4 pb-1">
            <DialogBody
              onOpenChange={onOpenChange}
              onJobCreated={onJobCreated}
              initialSourceUrl={initialSourceUrl}
            />
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function DialogBody({
  onOpenChange,
  onJobCreated,
  initialSourceUrl,
}: {
  onOpenChange: (open: boolean) => void
  onJobCreated?: () => void
  initialSourceUrl?: string
}) {
  const t = useTranslations("appTranslationForm")
  const [showSuccess, setShowSuccess] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [])

  const handleCreated = useCallback(() => {
    setShowSuccess(true)
    onJobCreated?.()
    timerRef.current = setTimeout(() => {
      timerRef.current = null
      onOpenChange(false)
    }, 1500)
  }, [onJobCreated, onOpenChange])

  if (showSuccess) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-10">
        <CheckCircle className="h-12 w-12" style={{ color: "var(--bamboo)" }} />
        <p className="text-base font-semibold text-foreground">{t("dialog.created")}</p>
      </div>
    )
  }

  return <TranslationForm mode="dialog" onCreated={handleCreated} initialSourceUrl={initialSourceUrl} />
}
