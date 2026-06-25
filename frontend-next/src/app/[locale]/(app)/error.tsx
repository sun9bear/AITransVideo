"use client"

import { useEffect } from "react"
import { Link } from "@/i18n/navigation"
import { AlertTriangle, FolderOpen, RefreshCw } from "lucide-react"

export default function WorkspaceError({
  error,
  reset,
}: {
  error: Error & { digest?: string }
  reset: () => void
}) {
  useEffect(() => {
    console.error("Unhandled workspace page error", error)
  }, [error])

  return (
    <section className="mx-auto flex min-h-[60vh] w-full max-w-3xl flex-col justify-center px-4 py-12">
      <div className="rounded-lg border border-border bg-card p-6 shadow-sm">
        <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-full bg-destructive/10 text-destructive">
          <AlertTriangle className="h-5 w-5" aria-hidden="true" />
        </div>
        <p className="text-sm font-medium text-muted-foreground">工作区异常</p>
        <h1 className="mt-2 text-2xl font-semibold text-foreground">当前页面加载失败</h1>
        <p className="mt-3 text-sm leading-6 text-muted-foreground">
          请重试一次；如果仍然失败，可以先回到项目列表。
        </p>
        <div className="mt-6 flex flex-wrap gap-3">
          <button
            type="button"
            onClick={reset}
            className="inline-flex h-9 items-center justify-center gap-2 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground transition hover:bg-primary/90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
          >
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            重试
          </button>
          <Link
            href="/projects"
            className="inline-flex h-9 items-center justify-center gap-2 rounded-md border border-border bg-background px-4 text-sm font-medium text-foreground transition hover:bg-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
          >
            <FolderOpen className="h-4 w-4" aria-hidden="true" />
            项目列表
          </Link>
        </div>
      </div>
    </section>
  )
}
