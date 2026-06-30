"use client"

import { useState } from "react"
import { useTranslations } from "next-intl"
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

/** Translator scoped to the `appJianyingDraft` namespace（UI-06 part2 W1，同 part1 typed-key 模式）。 */
type JianyingDraftTranslator = ReturnType<typeof useTranslations<"appJianyingDraft">>

// Path templates rendered in the help block + copied on demand.
// String.raw keeps Windows backslashes literal without double-escaping.
const WINDOWS_PATH = String.raw`%LocalAppData%\JianyingPro\User Data\Projects\com.lveditor.draft`
const MAC_PATH = "~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"

// translator 线程化传入（模块级函数拿不到 hook，沿用 part1 首参 translator 约定）。
// label（"Windows" / "Mac"）是 content，作 ICU {os} 占位符透传不译。
async function copyToClipboard(tj: JianyingDraftTranslator, text: string, label: string) {
  try {
    await navigator.clipboard.writeText(text)
    toast.success(tj("copied", { os: label }))
  } catch {
    // navigator.clipboard rejects on: 非 HTTPS 上下文 / 权限被拒 / 老 webview
    toast.error(tj("copyFailed"))
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
  const tj = useTranslations("appJianyingDraft")
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
          {isFirstTime ? tj("titleFirst") : tj("title")}
        </DialogTitle>
      </DialogHeader>

      <div className="space-y-3 py-1">
        <Input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={tj("placeholder")}
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
              <span className="font-medium text-foreground/70">{tj("howToFindLabel")}</span>
              {tj("howToFindBody")}
            </p>
            <div className="space-y-1">
              <div className="flex items-center justify-between">
                <p className="font-medium text-foreground/70">{tj("windowsLabel")}</p>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-[10px]"
                  onClick={() => copyToClipboard(tj, WINDOWS_PATH, "Windows")}
                  aria-label={tj("copyAria", { os: "Windows" })}
                >
                  {tj("copy")}
                </Button>
              </div>
              <code className="block break-all font-mono text-[11px] text-foreground/60">
                {WINDOWS_PATH}
              </code>
            </div>
            <div className="space-y-1">
              <div className="flex items-center justify-between">
                <p className="font-medium text-foreground/70">{tj("macLabel")}</p>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-[10px]"
                  onClick={() => copyToClipboard(tj, MAC_PATH, "Mac")}
                  aria-label={tj("copyAria", { os: "Mac" })}
                >
                  {tj("copy")}
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
            {tj("editHint")}
          </p>
        )}
      </div>

      <DialogFooter>
        <Button
          variant="outline"
          onClick={() => onOpenChange(false)}
        >
          {tj("cancel")}
        </Button>
        <Button
          onClick={handleSubmit}
          disabled={!canSubmit}
        >
          {tj("submit")}
        </Button>
      </DialogFooter>
    </>
  )
}
