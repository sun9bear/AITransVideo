"use client"

import { useState } from "react"
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
        setError("已存在同名说话人，请改一个名字")
      } else {
        setError("创建失败，请重试")
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
          <DialogTitle>新增说话人</DialogTitle>
          <DialogDescription>
            为 S2 漏检的说话人新建一个条目。创建后请到段落下拉里把属于这个说话人的段都改归属，
            后台会自动跑一次音色画像推断（约 5-15 秒）。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 py-2">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="例：桑达尔·皮查伊"
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
              {localConflict ? "已有同名说话人" : error}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={onClose}
            disabled={submitting}
          >
            取消
          </Button>
          <Button onClick={handleSubmit} disabled={!canSubmit}>
            {submitting ? "创建中..." : "创建"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
