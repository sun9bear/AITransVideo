"use client"

// frontend-next/src/components/voice-clone/CosyVoiceCloneModal.tsx
//
// Phase 4.2 D.2 — CosyVoice 克隆 modal（前置上传 + target_model 选择 + consent
// flow + 付费 API 调用）。
//
// **关键安全约束（CLAUDE.md 付费 API 硬约束）**：
//   - 只有用户**显式**点击 "提交克隆" → ConsentModal 全 3 勾选 → 再次点击
//     "开始克隆" 之后，才发起 `submitCosyvoiceClone` 网络请求。
//   - 失败**不重试**。失败 banner + 重试按钮由用户主动点。
//   - clone-gate 拒绝（`can_access_clone === false`）时不展示提交按钮。
//
// **E 阶段集成预期**：父组件（VoiceSelectionPanel 等）通过 props 传入
//   `speakerId` / `speakerName` / 可选 `defaultSourceJobId`，并通过 `onSuccess`
//   接收 voice metadata。E 阶段的 VoiceSelectionPanel 还需要在显示这个 modal
//   前先 AND `provider.supportsClone`（runtime availability），本组件**不**
//   再次检查该字段（属于 wiring 层职责）。

import { useCallback, useEffect, useMemo, useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import {
  COSYVOICE_TARGET_MODELS,
  CosyvoiceCloneApiError,
  CosyvoiceCloneSuccess,
  CosyvoiceConsentPayload,
  CosyvoiceSampleMode,
  CosyvoiceTargetModel,
  DEFAULT_COSYVOICE_TARGET_MODEL,
  getCosyvoiceCloneGate,
  submitCosyvoiceClone,
  type CosyvoiceCloneGateResponse,
} from "@/lib/api/cosyvoiceClone"

import { CosyVoiceConsentModal } from "./CosyVoiceConsentModal"

interface CosyVoiceCloneModalProps {
  open: boolean
  onClose: () => void
  /** Required — identifies the speaker the new clone voice belongs to. */
  speakerId: string
  /** Display label; pre-fills the form (user can edit before submit). */
  speakerName: string
  /**
   * Optional — if the user wants to clone from existing transcript segments
   * of a specific job rather than upload a fresh file, parent passes the
   * job id here. The modal lets the user switch between "file" / "segments"
   * mode via a radio toggle when this is provided.
   *
   * NOT yet wired in D.2 — segments selector UI (picking which segment ids)
   * is deferred to E phase. For now, segments mode shows a placeholder.
   */
  defaultSourceJobId?: string
  /** Called with the backend's voice metadata after a successful clone. */
  onSuccess: (voice: CosyvoiceCloneSuccess) => void
}

type SubmitState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "error"; message: string; code: string | null }

const TARGET_MODEL_INFO: Record<
  CosyvoiceTargetModel,
  { label: string; description: string }
> = {
  "cosyvoice-v3.5-flash": {
    label: "Flash（推荐）",
    description: "DashScope cosyvoice-v3.5-flash · 国际端点延迟低 · ¥0.01/次",
  },
  "cosyvoice-v3.5-plus": {
    label: "Plus",
    description: "DashScope cosyvoice-v3.5-plus · 完整音色覆盖 · ¥0.01/次",
  },
}

const MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024 // 10 MB hard cap (matches plan)
const ACCEPTED_FILE_TYPES = ".wav,.mp3,.m4a"

export function CosyVoiceCloneModal({
  open,
  onClose,
  speakerId,
  speakerName,
  defaultSourceJobId,
  onSuccess,
}: CosyVoiceCloneModalProps) {
  // -------------------------------------------------------------------------
  // Local form state
  // -------------------------------------------------------------------------
  const [targetModel, setTargetModel] = useState<CosyvoiceTargetModel>(
    DEFAULT_COSYVOICE_TARGET_MODEL,
  )
  const [editableSpeakerName, setEditableSpeakerName] = useState(speakerName)
  const [sampleMode, setSampleMode] = useState<CosyvoiceSampleMode>(
    defaultSourceJobId ? "segments" : "file",
  )
  const [sampleFile, setSampleFile] = useState<File | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)
  const [submitState, setSubmitState] = useState<SubmitState>({ kind: "idle" })
  const [consentOpen, setConsentOpen] = useState(false)

  // -------------------------------------------------------------------------
  // clone-gate fetch (display-layer authorization visibility)
  // -------------------------------------------------------------------------
  const [gate, setGate] = useState<CosyvoiceCloneGateResponse | null>(null)
  const [gateLoading, setGateLoading] = useState(false)
  const [gateError, setGateError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setGateLoading(true)
    setGateError(null)
    getCosyvoiceCloneGate()
      .then((data) => {
        if (cancelled) return
        setGate(data)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const msg =
          err instanceof Error ? err.message : "无法读取克隆授权状态"
        setGateError(msg)
      })
      .finally(() => {
        if (!cancelled) setGateLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open])

  // Reset form when modal closes so reopening starts fresh
  useEffect(() => {
    if (!open) {
      setTargetModel(DEFAULT_COSYVOICE_TARGET_MODEL)
      setEditableSpeakerName(speakerName)
      setSampleMode(defaultSourceJobId ? "segments" : "file")
      setSampleFile(null)
      setFileError(null)
      setSubmitState({ kind: "idle" })
      setConsentOpen(false)
    }
  }, [open, speakerName, defaultSourceJobId])

  // -------------------------------------------------------------------------
  // File handling — 5 维校验的"客户端可早期 catch"部分（格式 + 大小）
  // -------------------------------------------------------------------------
  const handleFile = useCallback((file: File | null) => {
    setFileError(null)
    if (!file) {
      setSampleFile(null)
      return
    }
    const lowerName = file.name.toLowerCase()
    const okExt =
      lowerName.endsWith(".wav") ||
      lowerName.endsWith(".mp3") ||
      lowerName.endsWith(".m4a")
    if (!okExt) {
      setFileError("仅支持 WAV / MP3 / M4A 三种格式")
      setSampleFile(null)
      return
    }
    if (file.size > MAX_FILE_SIZE_BYTES) {
      setFileError(
        `文件超过 10MB 上限（当前 ${(file.size / 1024 / 1024).toFixed(2)}MB）`,
      )
      setSampleFile(null)
      return
    }
    setSampleFile(file)
  }, [])

  // -------------------------------------------------------------------------
  // Submit gate (前置校验 → 弹 ConsentModal → 用户再次确认才发 API)
  // -------------------------------------------------------------------------
  const canRequestConsent = useMemo(() => {
    if (!gate?.can_access_clone) return false
    if (editableSpeakerName.trim() === "") return false
    if (sampleMode === "file") return sampleFile !== null
    if (sampleMode === "segments") {
      // D.2: segments selector UI is stubbed; require a sourceJobId at minimum.
      return Boolean(defaultSourceJobId)
    }
    return false
  }, [gate, editableSpeakerName, sampleMode, sampleFile, defaultSourceJobId])

  const handleConsentConfirmed = useCallback(
    async (consent: CosyvoiceConsentPayload) => {
      setConsentOpen(false)
      setSubmitState({ kind: "loading" })
      try {
        const voice = await submitCosyvoiceClone({
          targetModel,
          speakerId,
          speakerName: editableSpeakerName.trim(),
          consent,
          sampleMode,
          sampleFile: sampleMode === "file" ? sampleFile ?? undefined : undefined,
          sourceJobId:
            sampleMode === "segments" ? defaultSourceJobId : undefined,
          // D.2 placeholder: parent provides actual segment IDs in E phase.
          // For D.2 standalone testing, segments mode is gated above on
          // sourceJobId presence; a non-empty array stub keeps the contract
          // intact (backend rejects empty array, which is the right thing).
          sourceSegmentIds:
            sampleMode === "segments" ? ["__d2_placeholder__"] : undefined,
        })
        toast.success("克隆成功，已加入个人音色库")
        setSubmitState({ kind: "idle" })
        onSuccess(voice)
      } catch (err: unknown) {
        let message = "克隆失败，请稍后重试"
        let code: string | null = null
        if (err instanceof CosyvoiceCloneApiError) {
          message = describeCloneError(err)
          code = err.code
        } else if (err instanceof Error) {
          message = err.message
        }
        toast.error(message)
        setSubmitState({ kind: "error", message, code })
      }
    },
    [
      targetModel,
      speakerId,
      editableSpeakerName,
      sampleMode,
      sampleFile,
      defaultSourceJobId,
      onSuccess,
    ],
  )

  const handleSubmitClick = () => {
    if (!canRequestConsent) return
    setConsentOpen(true)
  }

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------
  const isLoading = submitState.kind === "loading"

  return (
    <>
      <Dialog
        open={open}
        onOpenChange={(next) => {
          // Block dismissal while a paid request is in flight to prevent the
          // user thinking the request was cancelled (clone proceeds either way
          // on the backend once we've POSTed).
          if (!next && !isLoading) onClose()
        }}
      >
        <DialogContent className="max-w-xl sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>克隆「{speakerName}」的声音</DialogTitle>
            <DialogDescription>
              CosyVoice 克隆音色后会出现在你的个人音色库，可在后续任务中复用。
            </DialogDescription>
          </DialogHeader>

          {/* Gate state */}
          {gateLoading && (
            <p className="text-xs text-muted-foreground">
              正在读取克隆授权状态...
            </p>
          )}
          {gateError && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
              {gateError}
            </div>
          )}
          {gate && !gate.can_access_clone && (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300">
              当前账号未在 CosyVoice 克隆灰度名单中。如需开通请联系管理员。
              {gate.general_availability_enabled === false &&
                " 全员开放尚未开启。"}
            </div>
          )}

          {gate?.can_access_clone && (
            <div className="space-y-4 py-2">
              {/* Speaker name */}
              <div className="space-y-2">
                <Label htmlFor="cosyvoice-clone-speaker-name">音色名称</Label>
                <input
                  id="cosyvoice-clone-speaker-name"
                  type="text"
                  value={editableSpeakerName}
                  onChange={(e) => setEditableSpeakerName(e.target.value)}
                  placeholder="例如：旁白小李"
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
                  disabled={isLoading}
                />
              </div>

              {/* Target model */}
              <div className="space-y-2">
                <Label>目标模型</Label>
                <div className="space-y-2">
                  {COSYVOICE_TARGET_MODELS.map((model) => {
                    const info = TARGET_MODEL_INFO[model]
                    return (
                      <label
                        key={model}
                        data-target-model={model}
                        className="flex items-start gap-3 rounded-xl border border-border bg-muted/30 p-3 cursor-pointer hover:bg-muted/50 transition"
                      >
                        <input
                          type="radio"
                          name="cosyvoice-target-model"
                          checked={targetModel === model}
                          onChange={() => setTargetModel(model)}
                          className="mt-1 h-4 w-4"
                          disabled={isLoading}
                        />
                        <div>
                          <p className="text-sm font-medium text-foreground">
                            {info.label}
                          </p>
                          <p className="text-xs text-muted-foreground mt-0.5">
                            {info.description}
                          </p>
                        </div>
                      </label>
                    )
                  })}
                </div>
              </div>

              {/* Sample source */}
              <div className="space-y-2">
                <Label>样本来源</Label>
                <div className="space-y-2">
                  <label
                    data-sample-mode="file"
                    className="flex items-start gap-3 rounded-xl border border-border bg-muted/30 p-3 cursor-pointer hover:bg-muted/50 transition"
                  >
                    <input
                      type="radio"
                      name="cosyvoice-sample-mode"
                      checked={sampleMode === "file"}
                      onChange={() => setSampleMode("file")}
                      className="mt-1 h-4 w-4"
                      disabled={isLoading}
                    />
                    <div className="flex-1">
                      <p className="text-sm font-medium text-foreground">
                        上传音频文件
                      </p>
                      <p className="text-xs text-muted-foreground mt-0.5">
                        WAV (PCM 16-bit) / MP3 / M4A · 3-60 秒 · ≤10 MB · ≥16 kHz ·
                        本人清晰朗读，无背景音乐 / 多人声
                      </p>
                      {sampleMode === "file" && (
                        <div className="mt-2 space-y-1">
                          <input
                            type="file"
                            accept={ACCEPTED_FILE_TYPES}
                            onChange={(e) =>
                              handleFile(e.target.files?.[0] ?? null)
                            }
                            className="block text-xs"
                            disabled={isLoading}
                          />
                          {sampleFile && (
                            <p className="text-xs text-muted-foreground">
                              已选择：{sampleFile.name} (
                              {(sampleFile.size / 1024).toFixed(1)} KB)
                            </p>
                          )}
                          {fileError && (
                            <p className="text-xs text-destructive">
                              {fileError}
                            </p>
                          )}
                        </div>
                      )}
                    </div>
                  </label>

                  {defaultSourceJobId && (
                    <label
                      data-sample-mode="segments"
                      className="flex items-start gap-3 rounded-xl border border-border bg-muted/30 p-3 cursor-pointer hover:bg-muted/50 transition"
                    >
                      <input
                        type="radio"
                        name="cosyvoice-sample-mode"
                        checked={sampleMode === "segments"}
                        onChange={() => setSampleMode("segments")}
                        className="mt-1 h-4 w-4"
                        disabled={isLoading}
                      />
                      <div className="flex-1">
                        <p className="text-sm font-medium text-foreground">
                          从当前任务转写选段
                        </p>
                        <p className="text-xs text-muted-foreground mt-0.5">
                          基于任务 {defaultSourceJobId} 的 transcript
                          选择若干段拼接为样本。具体选段 UI 将在下一阶段接入。
                        </p>
                      </div>
                    </label>
                  )}
                </div>
              </div>

              {/* Submit error banner (failure path is user-driven retry; no auto-retry) */}
              {submitState.kind === "error" && (
                <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
                  {submitState.message}
                  {submitState.code && (
                    <span className="ml-2 font-mono">[{submitState.code}]</span>
                  )}
                </div>
              )}
            </div>
          )}

          <DialogFooter>
            <Button variant="outline" onClick={onClose} disabled={isLoading}>
              取消
            </Button>
            <Button
              onClick={handleSubmitClick}
              disabled={!canRequestConsent || isLoading}
            >
              {isLoading ? "克隆中..." : "提交克隆"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <CosyVoiceConsentModal
        open={consentOpen}
        onClose={() => setConsentOpen(false)}
        onConfirm={handleConsentConfirmed}
      />
    </>
  )
}

// ---------------------------------------------------------------------------
// Error code → user-facing Chinese message
// ---------------------------------------------------------------------------

function describeCloneError(err: CosyvoiceCloneApiError): string {
  // Try to read the FastAPI HTTPException `detail.message` if present —
  // backend already provides Chinese explanations for each known code.
  if (err.detail && typeof err.detail === "object" && "detail" in err.detail) {
    const inner = (err.detail as { detail?: unknown }).detail
    if (inner && typeof inner === "object" && "message" in inner) {
      const msg = (inner as { message?: unknown }).message
      if (typeof msg === "string" && msg.trim() !== "") return msg
    }
  }
  switch (err.code) {
    case "unauthenticated":
      return "请先登录"
    case "forbidden_not_in_allowlist":
      return "当前账号未在 CosyVoice 克隆灰度名单中"
    case "clone_feature_disabled":
      return "CosyVoice 克隆能力当前未启用"
    case "consent_required":
      return "需要确认授权条款后才能提交"
    case "consent_outdated":
      return "授权条款已升级，请重新阅读并勾选"
    case "invalid_target_model":
      return "目标模型不合法"
    case "quota_exceeded":
      return "已达个人克隆音色数量上限，请先删除部分音色"
    case "client_sample_source_mutex":
      return "样本来源（文件 / 段落）只能二选一"
    case "client_missing_sample_file":
      return "请选择音频文件"
    case "client_missing_source_segments":
      return "请选择至少一个段落作为样本"
    case "client_missing_source_job_id":
      return "缺少 source_job_id"
    case "client_consent_required":
      return "缺少授权确认"
    case "client_consent_outdated":
      return "授权版本不匹配，请刷新页面"
    default:
      return err.message || "克隆失败，请稍后重试"
  }
}
