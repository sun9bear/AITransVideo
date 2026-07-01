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
import { useTranslations } from "next-intl"

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
 * Checkbox legal text lives in the localized catalog
 * (messages/{zh,en}/appCosyConsent.json → `checkbox.<id>.{title,detail}`); the
 * zh values are **byte-identical** to the source per
 * docs/legal/2026-05-25-cosyvoice-clone-authorization-v1.md (uiloc R1).
 *
 * **DO NOT change the zh legal wording** independently of:
 *   1. The legal doc (any change must go through 修订历史 + bump `v1` → `v2`)
 *   2. `CONSENT_MODAL_VERSION` constant in lib/api/cosyvoiceClone.ts
 *   3. Backend `gateway/cosyvoice_clone/api.py::CONSENT_MODAL_VERSION`
 *
 * The `modal_version` sentinel (rendered below) — NOT the surface text — is what
 * legally identifies which version the user signed; editing the zh text without
 * bumping it would silently let users sign a NEW version under the OLD
 * identifier. The en translation is a faithful courtesy rendering shown under
 * the same zh-anchored `modal_version` (see PR for the flagged legal note).
 */
const CHECKBOX_CONFIG: Record<
  CheckboxId,
  {
    titleKey: Parameters<ReturnType<typeof useTranslations<"appCosyConsent">>>[0]
    detailKey: Parameters<ReturnType<typeof useTranslations<"appCosyConsent">>>[0]
  }
> = {
  source: {
    titleKey: "checkbox.source.title",
    detailKey: "checkbox.source.detail",
  },
  data_flow: {
    titleKey: "checkbox.data_flow.title",
    detailKey: "checkbox.data_flow.detail",
  },
  consequences: {
    titleKey: "checkbox.consequences.title",
    detailKey: "checkbox.consequences.detail",
  },
}

export function CosyVoiceConsentModal({
  open,
  onClose,
  onConfirm,
}: CosyVoiceConsentModalProps) {
  const t = useTranslations("appCosyConsent")
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
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription>
            {t("description")}
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
                  <p className="font-medium text-foreground">{t(cfg.titleKey)}</p>
                  <p className="mt-1 text-xs text-muted-foreground whitespace-pre-line">
                    {t(cfg.detailKey)}
                  </p>
                </div>
              </label>
            )
          })}
        </div>

        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300">
          <strong>{t("paidApiLabel")}</strong>
          {t("paidApiBody")}
        </div>

        <DialogFooter>
          {/* Cancel MUST go through resetAndClose to wipe checkbox state.
              Direct `onClick={onClose}` is forbidden by static guard
              `test_d2_consent_modal_cancel_button_uses_reset_path`. */}
          <Button variant="outline" onClick={resetAndClose}>
            {t("cancel")}
          </Button>
          <Button
            disabled={!allChecked}
            onClick={handleConfirm}
            title={allChecked ? undefined : t("confirmAllTip")}
          >
            {t("startClone")}
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
