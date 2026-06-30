"use client"

import { useTranslations } from "next-intl"
import { useRouter } from "@/i18n/navigation"
import { TranslationForm } from "@/components/workspace/TranslationForm"

export default function NewTranslationPage() {
  const t = useTranslations("appTranslationForm")
  const router = useRouter()
  return (
    <div className="mx-auto w-full min-w-0 max-w-2xl space-y-4 py-4 sm:space-y-6 sm:py-8">
      <h1 className="break-keep text-2xl font-bold leading-tight">{t("pageTitle")}</h1>
      <TranslationForm
        mode="page"
        onCreated={(job) => router.push(`/workspace/${job.id}`)}
      />
    </div>
  )
}
