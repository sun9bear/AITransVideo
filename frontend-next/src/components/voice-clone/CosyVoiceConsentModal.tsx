"use client"

// frontend-next/src/components/voice-clone/CosyVoiceConsentModal.tsx
//
// Phase 4.2 D.2 — CosyVoice clone 授权 modal v1
// (`modal_version = "2026-05-25-v1"`).
//
// Surfaces the three independent legally-required checkboxes from
// docs/legal/2026-05-25-cosyvoice-clone-authorization-v1.md. On confirm,
// returns a `CosyvoiceConsentPayload` ready to be POSTed by
// `submitCosyvoiceClone`. The component does **not** call any API — it is a
// pure UI gate. The 付费 API call only happens after the parent CloneModal
// receives the payload, validates upload, and explicitly clicks 提交.
//
// Three independent checkboxes (not a single "I agree to everything"):
// the 三轮 review with Codex landed this design specifically to satisfy
// "声纹生物特征" 单独同意 expectation per 信安技术 §个人信息安全规范.

import { useState } from "react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  CONSENT_MODAL_VERSION,
  type CosyvoiceConsentPayload,
} from "@/lib/api/cosyvoiceClone"

interface CosyVoiceConsentModalProps {
  /** Controls dialog visibility. Parent owns the state. */
  open: boolean
  /** Called when user closes / cancels the dialog (does NOT trigger clone). */
  onClose: () => void
  /**
   * Called when **all three** checkboxes are ticked and user clicks
   * "开始克隆". Receives a fresh `CosyvoiceConsentPayload` (frontend
   * captures the current UTC timestamp for `confirmed_at`).
   *
   * The parent should then proceed to fire `submitCosyvoiceClone` — this
   * modal does NOT make the network call directly. Keeping consent capture
   * separate from the paid API call is a CLAUDE.md hard constraint:
   * automatic transitions from consent to paid call would lose the
   * "user explicitly clicked submit twice" safety belt.
   */
  onConfirm: (payload: CosyvoiceConsentPayload) => void
}

/**
 * Checkbox identifiers — used both for React state keys and (via the
 * `data-checkbox-id` attribute) by static guard tests to verify the three
 * required checkboxes exist verbatim.
 */
const CHECKBOX_IDS = ["source", "data_flow", "consequences"] as const
type CheckboxId = (typeof CHECKBOX_IDS)[number]

/**
 * The full legal text per docs/legal/2026-05-25-cosyvoice-clone-authorization-v1.md.
 *
 * **DO NOT EDIT** independently of:
 *   1. The legal doc (any change must go through 修订历史 + bump `v1` → `v2`)
 *   2. `CONSENT_MODAL_VERSION` constant in lib/api/cosyvoiceClone.ts
 *   3. Backend `gateway/cosyvoice_clone/api.py::CONSENT_MODAL_VERSION`
 *
 * Editing text here without bumping `modal_version` will silently let users
 * sign a NEW version under the OLD identifier — that breaks legal traceability.
 */
const CHECKBOX_CONFIG: Record<
  CheckboxId,
  { title: string; detail: string }
> = {
  source: {
    title:
      "我确认：本次提供的声音样本是我本人的声音，或者我已获得声音所有人的书面授权用于声音克隆和后续 TTS 合成。",
    detail:
      "我理解：未经声音所有人明确同意而克隆他人声音（含名人、公众人物、家人、朋友）属于侵权行为，本平台有权随时停用该音色并保留追究法律责任的权利。",
  },
  data_flow: {
    title:
      "我同意：声音样本将上传至中国境内的阿里云语音合成服务进行处理，用于生成与我声音相似的合成音色。",
    detail:
      "样本数据：用途仅用于本次克隆音色生成与后续 TTS 合成；处理位置为中国境内的阿里云服务器（2026-05 实测以「华北 2 - 北京」为主，本平台不固定单一 region）；中转节点位于中国境内武汉；克隆完成后 24 小时内删除原始样本，克隆出的 voice_id 保留在个人音色库由用户自主管理；本平台不会主动将样本用于训练通用 AI 模型，也不会主动与无关第三方共享或用于商业广告投放；阿里云作为第三方处理方，其对样本数据的具体处理规则以阿里云服务条款 / 数据处理协议为准。",
  },
  consequences: {
    title:
      "我了解并同意：违规后果与退出权 —— 平台收到投诉时音色将被停用、调查；冒用他人声音可能导致账号封禁。",
    detail:
      "如平台收到声音所有人投诉或第三方举报，我的克隆音色将被立即停用、调查；调查期间相关功能可能暂停。如确认存在冒用他人声音的行为，本平台有权封禁我的账号，并依法配合相关部门调查；由此产生的法律责任由我本人承担。我可随时在「个人音色库」页面删除已克隆的音色；删除后音色不可用于新的 TTS 合成，已合成的历史视频不受影响。",
  },
}

export function CosyVoiceConsentModal({
  open,
  onClose,
  onConfirm,
}: CosyVoiceConsentModalProps) {
  const [checked, setChecked] = useState<Record<CheckboxId, boolean>>({
    source: false,
    data_flow: false,
    consequences: false,
  })

  const allChecked = CHECKBOX_IDS.every((id) => checked[id])

  /**
   * **Single dismissal entry point** (PR #14 Codex P2 二轮 fix —
   * discussion_rXXXXXXXXX). Reset checkbox state + notify parent.
   *
   * ALL dismissal paths must funnel through this:
   *   - Cancel button (`onClick={resetAndClose}`)
   *   - Esc key / overlay click / X icon (`handleOpenChange(false)`)
   *
   * 之前 Cancel button 直接 ``onClick={onClose}`` 绕过 reset，导致用户勾选
   * 三项后取消，重开时仍显示已勾选 —— 削弱"每次显式确认"安全带。
   * 静态守卫 ``test_d2_consent_modal_cancel_button_uses_reset_path``
   * 锁住此契约。
   */
  const resetAndClose = () => {
    setChecked({ source: false, data_flow: false, consequences: false })
    onClose()
  }

  const handleConfirm = () => {
    if (!allChecked) return // defensive — button disabled state already covers
    const payload: CosyvoiceConsentPayload = {
      voice_clone_confirmed: "true",
      // Frozen literal — frontend MUST send the value the backend expects.
      modal_version: CONSENT_MODAL_VERSION,
      confirmed_at: new Date().toISOString(),
    }
    // Reset state for next open (the parent owns `open` and may reopen).
    setChecked({ source: false, data_flow: false, consequences: false })
    onConfirm(payload)
  }

  const handleOpenChange = (next: boolean) => {
    if (!next) {
      // User dismissed (Esc / overlay click / close button). Reset state and
      // notify parent — do NOT call onConfirm.
      resetAndClose()
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-2xl sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>声音克隆授权确认</DialogTitle>
          <DialogDescription>
            为了创建您专属的克隆音色，我们需要您本人的声音样本。在继续之前，
            请确认以下三项内容并勾选。任一未勾选时，「开始克隆」按钮将保持禁用。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 max-h-[60vh] overflow-y-auto py-2">
          {CHECKBOX_IDS.map((id) => {
            const cfg = CHECKBOX_CONFIG[id]
            return (
              <label
                key={id}
                data-checkbox-id={id}
                className="flex items-start gap-3 rounded-xl border border-border bg-muted/30 p-3 cursor-pointer hover:bg-muted/50 transition"
              >
                <input
                  type="checkbox"
                  checked={checked[id]}
                  onChange={(e) =>
                    setChecked((prev) => ({
                      ...prev,
                      [id]: e.target.checked,
                    }))
                  }
                  className="mt-1 h-4 w-4 rounded border-border"
                />
                <div className="text-sm">
                  <p className="font-medium text-foreground">{cfg.title}</p>
                  <p className="mt-1 text-xs text-muted-foreground whitespace-pre-line">
                    {cfg.detail}
                  </p>
                </div>
              </label>
            )
          })}
        </div>

        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300">
          <strong>付费 API 提示：</strong>
          点击「开始克隆」后将向 DashScope CosyVoice 发起一次音色克隆请求
          （每次约 ¥0.01 + ¥0.005 试听）。失败不重试 — 重试需您主动再次提交。
        </div>

        <DialogFooter>
          {/* Cancel MUST go through resetAndClose to wipe checkbox state.
              Direct `onClick={onClose}` is forbidden by static guard
              `test_d2_consent_modal_cancel_button_uses_reset_path`. */}
          <Button variant="outline" onClick={resetAndClose}>
            取消
          </Button>
          <Button
            disabled={!allChecked}
            onClick={handleConfirm}
            title={
              allChecked ? undefined : "请先确认上述全部三项条款"
            }
          >
            开始克隆
          </Button>
        </DialogFooter>

        {/* Modal version sentinel — invisible. Static guards grep this to
            confirm UI <-> contract pinning without depending on file path. */}
        <span
          data-cosyvoice-consent-modal-version={CONSENT_MODAL_VERSION}
          aria-hidden="true"
          className="sr-only"
        >
          {CONSENT_MODAL_VERSION}
        </span>
      </DialogContent>
    </Dialog>
  )
}
