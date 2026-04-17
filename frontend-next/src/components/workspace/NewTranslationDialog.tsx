"use client"

import { useCallback, useEffect, useRef, useState } from "react"
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
  return (
    <Dialog open={open} onOpenChange={(value) => onOpenChange(value)}>
      <DialogContent className="max-w-lg max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>新建翻译任务</DialogTitle>
        </DialogHeader>
        {/* Key on `open` forces fresh mount each time dialog opens,
            resetting showSuccess without needing setState in useEffect */}
        {open && (
          <DialogBody
            onOpenChange={onOpenChange}
            onJobCreated={onJobCreated}
            initialSourceUrl={initialSourceUrl}
          />
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
        <CheckCircle className="h-12 w-12 text-emerald-400" />
        <p className="text-base font-semibold text-foreground">任务已创建</p>
      </div>
    )
  }

  return <TranslationForm mode="dialog" onCreated={handleCreated} initialSourceUrl={initialSourceUrl} />
}
