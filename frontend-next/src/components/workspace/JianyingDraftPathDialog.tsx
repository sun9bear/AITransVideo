"use client"

import { useState } from "react"
import { toast } from "sonner"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"

const LS_KEY = "avt:jianying_draft_root"

// Path templates rendered in the help block + copied on demand.
// String.raw keeps Windows backslashes literal without double-escaping.
const WINDOWS_PATH = String.raw`%LocalAppData%\JianyingPro\User Data\Projects\com.lveditor.draft`
const MAC_PATH = "~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"

async function copyToClipboard(text: string, label: string) {
  try {
    await navigator.clipboard.writeText(text)
    toast.success(`已复制 ${label} 路径`)
  } catch {
    // navigator.clipboard rejects on: 非 HTTPS 上下文 / 权限被拒 / 老 webview
    toast.error("复制失败，请手动选中复制")
  }
}

function readStoredPath(): string {
  if (typeof window === "undefined") return ""
  return window.localStorage.getItem(LS_KEY) ?? ""
}

interface JianyingDraftPathDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onConfirm: (userDraftRoot: string) => void
  /** Optional error message from backend (e.g. 400 invalid_user_draft_root). */
  errorMessage?: string
}

export function JianyingDraftPathDialog({
  open,
  onOpenChange,
  onConfirm,
  errorMessage,
}: JianyingDraftPathDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        {/*
         * Key on `open` forces a fresh mount each time the dialog opens,
         * so the inner state initialises from localStorage on every open
         * (same pattern as NewTranslationDialog). This avoids calling
         * setState inside an effect body.
         */}
        {open && (
          <DialogBody
            onOpenChange={onOpenChange}
            onConfirm={onConfirm}
            errorMessage={errorMessage}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

interface DialogBodyProps {
  onOpenChange: (open: boolean) => void
  onConfirm: (userDraftRoot: string) => void
  errorMessage?: string
}

function DialogBody({ onOpenChange, onConfirm, errorMessage }: DialogBodyProps) {
  // Read localStorage once on mount (fresh mount per open, see key above).
  const storedOnOpen = readStoredPath()
  const [value, setValue] = useState<string>(storedOnOpen)

  const isFirstTime = storedOnOpen === ""
  const trimmed = value.trim()
  const canSubmit = trimmed.length > 0

  function handleSubmit() {
    if (!canSubmit) return
    window.localStorage.setItem(LS_KEY, trimmed)
    onConfirm(trimmed)
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" && canSubmit) {
      e.preventDefault()
      handleSubmit()
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>
          {isFirstTime ? "请填写剪映草稿目录" : "剪映草稿目录"}
        </DialogTitle>
      </DialogHeader>

      <div className="space-y-3 py-1">
        <Input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="请输入剪映草稿目录的绝对路径"
          autoFocus
        />

        {/* Error message from backend */}
        {errorMessage && (
          <p className="text-xs text-red-500">{errorMessage}</p>
        )}

        {/* Help text — first time only */}
        {isFirstTime && (
          <div className="space-y-2 rounded-lg bg-muted/40 px-3 py-2.5 text-xs text-muted-foreground">
            <p>
              <span className="font-medium text-foreground/70">如何找：</span>
              打开剪映 → 设置 → 草稿位置，将该路径复制到上方输入框
            </p>
            <div className="space-y-1">
              <div className="flex items-center justify-between">
                <p className="font-medium text-foreground/70">Windows 默认路径：</p>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-[10px]"
                  onClick={() => copyToClipboard(WINDOWS_PATH, "Windows")}
                  aria-label="复制 Windows 路径"
                >
                  复制
                </Button>
              </div>
              <code className="block break-all font-mono text-[11px] text-foreground/60">
                {WINDOWS_PATH}
              </code>
            </div>
            <div className="space-y-1">
              <div className="flex items-center justify-between">
                <p className="font-medium text-foreground/70">Mac 默认路径：</p>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-[10px]"
                  onClick={() => copyToClipboard(MAC_PATH, "Mac")}
                  aria-label="复制 Mac 路径"
                >
                  复制
                </Button>
              </div>
              <code className="block break-all font-mono text-[11px] text-foreground/60">
                {MAC_PATH}
              </code>
            </div>
          </div>
        )}

        {/* Return visit hint */}
        {!isFirstTime && (
          <p className="text-xs text-muted-foreground">
            修改路径请直接编辑上方输入框
          </p>
        )}
      </div>

      <DialogFooter>
        <Button
          variant="outline"
          onClick={() => onOpenChange(false)}
        >
          取消
        </Button>
        <Button
          onClick={handleSubmit}
          disabled={!canSubmit}
        >
          开始生成
        </Button>
      </DialogFooter>
    </>
  )
}
