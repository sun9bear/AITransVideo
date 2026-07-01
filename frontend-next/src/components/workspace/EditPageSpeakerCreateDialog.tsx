"use client"

import { useState } from "react"
import { useTranslations } from "next-intl"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  createEditingSpeaker,
  DisplayNameConflict,
  type EditingSpeaker,
} from "@/lib/api/editing"

interface EditPageSpeakerCreateDialogProps {
  jobId: string
  open: boolean
  /**
   * 当前已存在的 display_names（baseline + editing），用于客户端去重提示。
   * 比较前都 trim 一次（与后端语义对齐）。
   */
  existingNames: Set<string>
  onClose: () => void
  /** 创建成功回调，父组件用于刷新 speakers 列表。 */
  onCreated: (speaker: EditingSpeaker) => void
}

export function EditPageSpeakerCreateDialog({
  jobId,
  open,
  existingNames,
  onClose,
  onCreated,
}: EditPageSpeakerCreateDialogProps) {
  const t = useTranslations("appSpeakerCreate")
  const [name, setName] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const trimmed = name.trim()
  const localConflict = trimmed.length > 0 && existingNames.has(trimmed)
  const canSubmit = trimmed.length > 0 && !localConflict && !submitting

  const handleSubmit = async () => {
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      const sp = await createEditingSpeaker(jobId, trimmed)
      onCreated(sp)
      setName("")
      onClose()
    } catch (e: unknown) {
      if (e instanceof DisplayNameConflict) {
        setError(t("nameConflictServer"))
      } else {
        setError(t("createFailed"))
      }
    } finally {
      setSubmitting(false)
    }
  }

  const handleOpenChange = (next: boolean) => {
    if (!next && !submitting) {
      setName("")
      setError(null)
      onClose()
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription>
            {t("description")}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 py-2">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("placeholder")}
            maxLength={40}
            autoFocus
            onKeyDown={(e) => {
              if (e.key === "Enter" && canSubmit) {
                e.preventDefault()
                void handleSubmit()
              }
            }}
            aria-invalid={localConflict || error !== null}
            aria-describedby={
              localConflict || error
                ? "speaker-create-dialog-error"
                : undefined
            }
          />
          {(localConflict || error) && (
            <p
              id="speaker-create-dialog-error"
              className="text-sm text-red-500"
              role="alert"
            >
              {localConflict ? t("nameConflictLocal") : error}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={onClose}
            disabled={submitting}
          >
            {t("cancel")}
          </Button>
          <Button onClick={handleSubmit} disabled={!canSubmit}>
            {submitting ? t("creating") : t("create")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
