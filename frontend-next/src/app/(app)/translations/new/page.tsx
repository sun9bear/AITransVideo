"use client"

import { useRouter } from "next/navigation"
import { TranslationForm } from "@/components/workspace/TranslationForm"

export default function NewTranslationPage() {
  const router = useRouter()
  return (
    <div className="mx-auto max-w-2xl space-y-6 py-8">
      <h1 className="text-2xl font-bold">新建翻译</h1>
      <TranslationForm
        mode="page"
        onCreated={(job) => router.push(`/workspace/${job.id}`)}
      />
    </div>
  )
}
