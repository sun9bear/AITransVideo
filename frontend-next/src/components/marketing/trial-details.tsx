"use client"

import { useTranslations } from "next-intl"
import { Check, Lock, CreditCard, HelpCircle } from "lucide-react"
import { usePlans } from "./use-plans"

/**
 * Trial benefits panel used on `/trial`.
 *
 * Copy boundary — DESIGN.md §3.6 + T2 preflight:
 *
 * - While `trial.frozen === false`, this component shows qualitative trust cues
 *   and a neutral user-facing note about reviewing exact allowances after
 *   sign-up. It must NOT mention days, minutes, Studio inclusion, a specific
 *   tier name (e.g. "Plus"), or internal-sounding phrases like "仍在最终确认中".
 * - When the gateway flips trial.frozen to true and adds fields like `days` /
 *   `source_minutes` / `phone_required`, extend this file to render those
 *   fields from the API response. Never hardcode fallback numbers here.
 */
const QUALITATIVE_BENEFITS: Array<{
  icon: typeof Check
  titleKey: "benefit1Title" | "benefit2Title" | "benefit3Title" | "benefit4Title"
  bodyKey: "benefit1Body" | "benefit2Body" | "benefit3Body" | "benefit4Body"
}> = [
  { icon: Check, titleKey: "benefit1Title", bodyKey: "benefit1Body" },
  { icon: Lock, titleKey: "benefit2Title", bodyKey: "benefit2Body" },
  { icon: CreditCard, titleKey: "benefit3Title", bodyKey: "benefit3Body" },
  { icon: HelpCircle, titleKey: "benefit4Title", bodyKey: "benefit4Body" },
]

export function TrialDetails() {
  const t = useTranslations("marketing.trialDetails")
  const state = usePlans()
  const trial = state.status === "ready" ? state.data.trial : null
  const frozen = trial?.frozen === true

  return (
    <div className="space-y-8">
      <ul className="space-y-5" role="list">
        {QUALITATIVE_BENEFITS.map((b) => {
          const Icon = b.icon
          return (
            <li key={b.titleKey} className="flex gap-4">
              <div className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Icon className="h-4 w-4" aria-hidden="true" />
              </div>
              <div>
                <h3 className="text-base font-semibold text-foreground">{t(b.titleKey)}</h3>
                <p className="mt-1 zh-body text-muted-foreground text-[0.95rem]">{t(b.bodyKey)}</p>
              </div>
            </li>
          )
        })}
      </ul>

      {/* Trial allowance — renders frozen numeric facts from the API. */}
      {state.status === "ready" && frozen && trial && (
        <div className="rounded-lg border border-primary/20 bg-primary/5 p-4">
          <p className="text-sm font-medium text-foreground">{t("allowanceTitle")}</p>
          <ul className="mt-2 space-y-1 text-sm text-muted-foreground">
            {trial.days && <li>{t("allowanceDays", { days: trial.days })}</li>}
            {trial.source_minutes && (
              <li>{t("allowanceMinutes", { minutes: trial.source_minutes })}</li>
            )}
            {trial.includes_studio && <li>{t("allowanceStudio")}</li>}
            <li>{t("allowanceNoCharge")}</li>
          </ul>
        </div>
      )}
      {state.status === "ready" && !frozen && (
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="text-sm text-muted-foreground">{t("unfrozenNote")}</p>
        </div>
      )}
    </div>
  )
}
