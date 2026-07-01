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
import { useLocale, useTranslations } from "next-intl"
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
import { CosyVoiceSegmentPicker } from "./CosyVoiceSegmentPicker"

interface CosyVoiceCloneModalProps {
  open: boolean
  onClose: () => void
  /** Required — identifies the speaker the new clone voice belongs to. */
  speakerId: string
  /** Display label; pre-fills the form (user can edit before submit). */
  speakerName: string
  /**
   * Optional — job id whose transcript segments will be used as the sample
   * source when sampleMode === "segments". Without this, segments mode is
   * **disabled entirely** (only file upload is available).
   *
   * **E.2 语义（spec v2.2 §0 决策 1）**：传入此 prop 单独即启用 segments
   * 模式 —— modal 内嵌的 `CosyVoiceSegmentPicker` 会用 `(jobId, speakerId)`
   * 调 `getSpeakerAudioSegments` 自加载段全集。
   *
   * 调用点策略：
   *   - VoiceSelectionPanel（approve 流）传 `jobId` 启用 picker
   *   - VoiceModifyTab（editing 流）E.2 阶段**不传**此 prop 让 modal 自然
   *     回落 file-only（避免 baseline vs editing 段语义混淆，等 edit-aware
   *     endpoint 再开放）
   */
  defaultSourceJobId?: string
  /**
   * Optional — initial selection of integer segment ids. Strict `number[]`
   * matches backend `_parse_source_segments` (`type(x) is int`).
   *
   * **E.2 语义（spec v2.2 §0 决策 3）**：D.2 把此 prop 锁定为公开契约
   * （严格 `number[]`，拒绝 placeholder），E.2 保留接口但语义升级为
   * "外部注入的初始值" —— modal 在 open 时 `useEffect` 把它拷入内部
   * `selectedSegmentIds` state，picker 操作 mutate 的是 internal state，
   * 提交时**永远**读 internal state、不读此 prop。
   *
   * 当前 E.2 两个调用点（VoiceSelectionPanel / VoiceModifyTab）都**不**
   * 显式传入；prop 保留是为了不破坏 D.2 公开接口 + 允许 future external
   * preselect 场景（admin replay / E2E test 注入等）。
   *
   * D.2 锁定的 placeholder 拒绝守卫
   * (`test_d2_clone_modal_no_placeholder_segment_id`) + backend strict int
   * 校验仍生效，E.2 不削弱。
   */
  sourceSegmentIds?: number[]
  /** Called with the backend's voice metadata after a successful clone. */
  onSuccess: (voice: CosyvoiceCloneSuccess) => void
}

type SubmitState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "error"; message: string; code: string | null }

type CosyCloneTranslator = ReturnType<typeof useTranslations<"appCosyClone">>

function getTargetModelInfo(
  t: CosyCloneTranslator,
  model: CosyvoiceTargetModel,
): { label: string; description: string } {
  if (model === "cosyvoice-v3.5-plus") {
    return {
      label: t("targetModel.plus.label"),
      description: t("targetModel.plus.description"),
    }
  }
  return {
    label: t("targetModel.flash.label"),
    description: t("targetModel.flash.description"),
  }
}

const MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024 // 10 MB hard cap (matches plan)
const ACCEPTED_FILE_TYPES = ".wav,.mp3,.m4a"

export function CosyVoiceCloneModal({
  open,
  onClose,
  speakerId,
  speakerName,
  defaultSourceJobId,
  sourceSegmentIds,
  onSuccess,
}: CosyVoiceCloneModalProps) {
  const t = useTranslations("appCosyClone")
  const locale = useLocale()

  // -------------------------------------------------------------------------
  // segments-mode 可用性闸
  // -------------------------------------------------------------------------
  // E.2 spec v2.1 §0 决策 3：modal 接受 `sourceSegmentIds?: number[]` prop
  // 作为外部注入初始值，但**内部** picker 通过 `getSpeakerAudioSegments`
  // 加载段全集；E.1 时代"prop 非空才启用 segments mode"的条件改为只看
  // `defaultSourceJobId` 是否传入。
  //
  // VoiceModifyTab E.2 阶段不传 `defaultSourceJobId`（§0 决策 1），让 modal
  // 自然回落 file-only。VoiceSelectionPanel 传 `defaultSourceJobId={jobId}`
  // 启用 picker。
  //
  // 占位 id 守护：D.2 的 `sourceSegmentIds` placeholder 拒绝在 `cosyvoiceClone.ts`
  // 的 client-side mutex + backend strict `type(x) is int` 处仍生效；E.2 不削弱。
  const segmentsModeAvailable = Boolean(defaultSourceJobId)

  // -------------------------------------------------------------------------
  // Local form state
  // -------------------------------------------------------------------------
  const [targetModel, setTargetModel] = useState<CosyvoiceTargetModel>(
    DEFAULT_COSYVOICE_TARGET_MODEL,
  )
  const [editableSpeakerName, setEditableSpeakerName] = useState(speakerName)
  // **默认始终 "file"**（PR #14 Codex P2 二轮）。即使父组件传了
  // defaultSourceJobId 也先停在 file 模式 —— 让用户主动选 segments 模式才切，
  // 避免误以为已经选好"用任务转写片段"而点提交。
  const [sampleMode, setSampleMode] = useState<CosyvoiceSampleMode>("file")
  const [sampleFile, setSampleFile] = useState<File | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)
  // E.2 v2.2：picker 持有的内部状态。`selectedSegmentIds` 是提交时透传到
  // API client 的真实选段；`availableSegmentIds` 是 picker 加载完后回传的
  // 段全集（包成 Set 用于子集 assert）；`selectedDurationMs` 是 picker
  // 用 `endMs - startMs` 精确毫秒计算并回传的总时长，与后端
  // `MIN_DURATION_MS = 3_000` / `MAX_DURATION_MS = 60_000` **同单位**校验
  // （v2.2 / spec §4 E.2.4 L1 / Codex PR #16 P2 fix）。
  const [selectedSegmentIds, setSelectedSegmentIds] = useState<number[]>([])
  const [availableSegmentIds, setAvailableSegmentIds] = useState<Set<number>>(
    new Set(),
  )
  const [selectedDurationMs, setSelectedDurationMs] = useState(0)
  const [submitState, setSubmitState] = useState<SubmitState>({ kind: "idle" })
  const [consentOpen, setConsentOpen] = useState(false)

  // E.2 v2.1 §0 决策 3：打开 modal 时，把 prop sourceSegmentIds 作为外部注入
  // 的初始值拷入内部 state。此 prop 在 E.2 调用点都不传或传 undefined（picker
  // 自己加载），但契约保留为 D.2 公开接口的一部分。
  useEffect(() => {
    if (open && sourceSegmentIds && sourceSegmentIds.length > 0) {
      setSelectedSegmentIds([...sourceSegmentIds])
    }
  }, [open, sourceSegmentIds])

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
          err instanceof Error ? err.message : t("gateLoadFailed")
        setGateError(msg)
      })
      .finally(() => {
        if (!cancelled) setGateLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, t])

  // Reset form when modal closes so reopening starts fresh.
  // sampleMode always resets to "file" (the safe default per Codex P2 二轮);
  // user must explicitly switch to segments mode if they want it.
  useEffect(() => {
    if (!open) {
      setTargetModel(DEFAULT_COSYVOICE_TARGET_MODEL)
      setEditableSpeakerName(speakerName)
      setSampleMode("file")
      setSampleFile(null)
      setFileError(null)
      // E.2 v2.1：modal 关闭时重置 picker 相关 state（避免下次打开 / 切换
      // speaker 时残留旧选段——R3 / R6 互斥状态机防御）。
      setSelectedSegmentIds([])
      setAvailableSegmentIds(new Set())
      setSelectedDurationMs(0)
      setSubmitState({ kind: "idle" })
      setConsentOpen(false)
    }
  }, [open, speakerName])

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
      setFileError(t("fileErrorFormat"))
      setSampleFile(null)
      return
    }
    if (file.size > MAX_FILE_SIZE_BYTES) {
      setFileError(
        t("fileErrorTooLarge", { size: (file.size / 1024 / 1024).toFixed(2) }),
      )
      setSampleFile(null)
      return
    }
    setSampleFile(file)
  }, [t])

  // -------------------------------------------------------------------------
  // Submit gate (前置校验 → 弹 ConsentModal → 用户再次确认才发 API)
  // -------------------------------------------------------------------------
  const canRequestConsent = useMemo(() => {
    if (!gate?.can_access_clone) return false
    if (editableSpeakerName.trim() === "") return false
    if (sampleMode === "file") return sampleFile !== null
    if (sampleMode === "segments") {
      // E.2 v2.2 §0 决策 2 / Codex PR #16 P2 fix：客户端阈值用**毫秒**
      // 与后端 `MIN_DURATION_MS = 3_000` / `MAX_DURATION_MS = 60_000`
      // 完全同单位校验。v2.1 用 seconds 会被 `durationS` 一位小数 round
      // 漂移坑：真实 2.96s 显示成 3.0s → 前端放行 → 后端 ms 精度拒收。
      // v2.2 picker 用 `endMs - startMs` 聚合毫秒数，此处直接 ms 字面量比。
      // 子集 assert 在 handleSubmitClick 中做（spec §4 E.2.4 L1.5）。
      if (!segmentsModeAvailable) return false
      if (selectedSegmentIds.length === 0) return false
      if (selectedDurationMs < 3000) return false
      if (selectedDurationMs > 60000) return false
      return true
    }
    return false
  }, [
    gate,
    editableSpeakerName,
    sampleMode,
    sampleFile,
    segmentsModeAvailable,
    selectedSegmentIds,
    selectedDurationMs,
  ])

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
          // E.2 v2.1 §0 决策 3：提交永远读 modal 内部 `selectedSegmentIds`
          // 而不是 prop `sourceSegmentIds`（prop 已经在 open 时被拷入 internal
          // state，picker 操作 mutate 的也是 internal state）。
          // 守卫：D.2 strict `number[]` 类型 + 客户端 mutex（cosyvoiceClone.ts）
          // + 后端 `_parse_source_segments` strict int 三层兜底。
          sourceSegmentIds:
            sampleMode === "segments" ? selectedSegmentIds : undefined,
        })
        toast.success(t("cloneSuccessToast"))
        setSubmitState({ kind: "idle" })
        onSuccess(voice)
      } catch (err: unknown) {
        let message = t("cloneFailedDefault")
        let code: string | null = null
        if (err instanceof CosyvoiceCloneApiError) {
          message = describeCloneError(t, locale, err)
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
      selectedSegmentIds,
      onSuccess,
      t,
    ],
  )

  const handleSubmitClick = () => {
    if (!canRequestConsent) return
    // E.2 v2.1 §0 决策 4 / spec §4 E.2.4 L1.5：提交前子集 assert。
    // 防御 picker 状态因用户切换 speaker / 网络重排泄漏旧选段的场景。
    // 后端 A.2b 4 层 ownership 仍是最终防线；此处把状态错误提前到点击瞬间。
    if (sampleMode === "segments") {
      const allOwned = selectedSegmentIds.every((id) =>
        availableSegmentIds.has(id),
      )
      if (!allOwned) {
        setSubmitState({
          kind: "error",
          message: t("segmentsNotOwnedError"),
          code: "client_segments_not_subset",
        })
        return
      }
    }
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
        <DialogContent className="w-[calc(100vw-2rem)] max-w-xl overflow-x-hidden sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>{t("dialogTitle", { speakerName })}</DialogTitle>
            <DialogDescription>
              {t("dialogDescription")}
            </DialogDescription>
          </DialogHeader>

          {/* Gate state */}
          {gateLoading && (
            <p className="text-xs text-muted-foreground">
              {t("gateLoading")}
            </p>
          )}
          {gateError && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
              {gateError}
            </div>
          )}
          {gate && !gate.can_access_clone && (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300">
              {t("gateDenied")}
              {gate.general_availability_enabled === false &&
                t("gateGaDisabledSuffix")}
            </div>
          )}

          {gate?.can_access_clone && (
            <div className="min-w-0 space-y-4 py-2">
              {/* Speaker name */}
              <div className="space-y-2">
                <Label htmlFor="cosyvoice-clone-speaker-name">{t("speakerNameLabel")}</Label>
                <input
                  id="cosyvoice-clone-speaker-name"
                  type="text"
                  value={editableSpeakerName}
                  onChange={(e) => setEditableSpeakerName(e.target.value)}
                  placeholder={t("speakerNamePlaceholder")}
                  className="w-full min-w-0 rounded-md border border-border bg-background px-3 py-2 text-sm"
                  disabled={isLoading}
                />
              </div>

              {/* Target model */}
              <div className="space-y-2">
                <Label>{t("targetModelLabel")}</Label>
                <div className="space-y-2">
                  {COSYVOICE_TARGET_MODELS.map((model) => {
                    const info = getTargetModelInfo(t, model)
                    return (
                      <label
                        key={model}
                        data-target-model={model}
                        className="flex min-w-0 items-start gap-3 rounded-xl border border-border bg-muted/30 p-3 cursor-pointer hover:bg-muted/50 transition"
                      >
                        <input
                          type="radio"
                          name="cosyvoice-target-model"
                          checked={targetModel === model}
                          onChange={() => setTargetModel(model)}
                          className="mt-1 h-4 w-4"
                          disabled={isLoading}
                        />
                        <div className="min-w-0">
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
                <Label>{t("sampleSourceLabel")}</Label>
                <div className="space-y-2">
                  <label
                    data-sample-mode="file"
                    className="flex min-w-0 items-start gap-3 rounded-xl border border-border bg-muted/30 p-3 cursor-pointer hover:bg-muted/50 transition"
                  >
                    <input
                      type="radio"
                      name="cosyvoice-sample-mode"
                      checked={sampleMode === "file"}
                      onChange={() => {
                        setSampleMode("file")
                        // E.2 v2.1 §0 决策 5b：切到 file 必须清 segments 选段
                        // 状态（XOR 一致性，对侧不残留）。守卫 #8。
                        setSelectedSegmentIds([])
                        setSelectedDurationMs(0)
                      }}
                      className="mt-1 h-4 w-4"
                      disabled={isLoading}
                    />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-foreground">
                        {t("uploadFileTitle")}
                      </p>
                      <p className="text-xs text-muted-foreground mt-0.5">
                        {t("uploadFileHint")}
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
                              {t("selectedFile", {
                                name: sampleFile.name,
                                size: (sampleFile.size / 1024).toFixed(1),
                              })}
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

                  {/* segments 模式：E.2 v2.1 修改——只看 defaultSourceJobId
                      是否传入。Picker 内部加载段并通过 onAvailableSegmentIdsChange
                      回传段全集供子集 assert 使用。VoiceModifyTab 不传
                      defaultSourceJobId（§0 决策 1），所以 editing 路径
                      还是 file-only。 */}
                  {segmentsModeAvailable && (
                    <label
                      data-sample-mode="segments"
                      className="flex min-w-0 items-start gap-3 rounded-xl border border-border bg-muted/30 p-3 cursor-pointer hover:bg-muted/50 transition"
                    >
                      <input
                        type="radio"
                        name="cosyvoice-sample-mode"
                        checked={sampleMode === "segments"}
                        onChange={() => {
                          setSampleMode("segments")
                          // E.2 v2.1 §0 决策 5b：切到 segments 必须清 file
                          // 状态（XOR 一致性，对侧不残留）。守卫 #9。
                          setSampleFile(null)
                          setFileError(null)
                        }}
                        className="mt-1 h-4 w-4"
                        disabled={isLoading}
                      />
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-foreground">
                          {t("segmentsSourceTitle")}
                        </p>
                        <p className="text-xs text-muted-foreground mt-0.5">
                          {t("segmentsSourceHint")}
                        </p>
                        {sampleMode === "segments" && (
                          <div className="mt-2 min-w-0 overflow-hidden">
                            <CosyVoiceSegmentPicker
                              speakerId={speakerId}
                              jobId={defaultSourceJobId as string}
                              selectedSegmentIds={selectedSegmentIds}
                              onChange={setSelectedSegmentIds}
                              onAvailableSegmentIdsChange={(ids) =>
                                setAvailableSegmentIds(new Set(ids))
                              }
                              onSelectedDurationMsChange={setSelectedDurationMs}
                              disabled={isLoading}
                            />
                          </div>
                        )}
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
              {t("cancel")}
            </Button>
            <Button
              onClick={handleSubmitClick}
              disabled={!canRequestConsent || isLoading}
            >
              {isLoading ? t("cloning") : t("submitClone")}
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
// Error code → user-facing localized message
// ---------------------------------------------------------------------------

function describeCloneError(
  t: CosyCloneTranslator,
  locale: string,
  err: CosyvoiceCloneApiError,
): string {
  // Backend FastAPI HTTPException `detail.message` (Chinese) if present.
  const backendMessage: string | null = (() => {
    if (err.detail && typeof err.detail === "object" && "detail" in err.detail) {
      const inner = (err.detail as { detail?: unknown }).detail
      if (inner && typeof inner === "object" && "message" in inner) {
        const msg = (inner as { message?: unknown }).message
        if (typeof msg === "string" && msg.trim() !== "") return msg
      }
    }
    return null
  })()
  // zh: keep the backend's Chinese explanation verbatim so the default zh
  // experience is byte-identical to before uiloc (R1). en: prefer the localized
  // per-code translation so a KNOWN code never surfaces Chinese (uiloc W4a;
  // CodeX P2). Unknown/dynamic codes fall through to `backendMessage`.
  if (locale.startsWith("zh") && backendMessage) return backendMessage
  switch (err.code) {
    case "unauthenticated":
      return t("errorCode.unauthenticated")
    case "forbidden_not_in_allowlist":
      return t("errorCode.forbiddenNotInAllowlist")
    case "clone_feature_disabled":
      return t("errorCode.cloneFeatureDisabled")
    case "consent_required":
      return t("errorCode.consentRequired")
    case "consent_outdated":
      return t("errorCode.consentOutdated")
    case "invalid_target_model":
      return t("errorCode.invalidTargetModel")
    // Backend returns `voice_quota_exceeded` (gateway/cosyvoice_clone/api.py);
    // keep the legacy `quota_exceeded` alias too (@codex bot W4a review).
    case "voice_quota_exceeded":
    case "quota_exceeded":
      return t("errorCode.quotaExceeded")
    case "client_sample_source_mutex":
      return t("errorCode.clientSampleSourceMutex")
    case "client_missing_sample_file":
      return t("errorCode.clientMissingSampleFile")
    case "client_missing_source_segments":
      return t("errorCode.clientMissingSourceSegments")
    case "client_missing_source_job_id":
      return t("errorCode.clientMissingSourceJobId")
    case "client_consent_required":
      return t("errorCode.clientConsentRequired")
    case "client_consent_outdated":
      return t("errorCode.clientConsentOutdated")
    default:
      // Unknown / dynamic code — surface the backend's own explanation if
      // present, else a generic localized message.
      return backendMessage ?? (err.message || t("cloneFailedDefault"))
  }
}
