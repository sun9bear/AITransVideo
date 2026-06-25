export default function WorkspaceLoading() {
  return (
    <section className="w-full px-4 py-6">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="h-5 w-28 rounded bg-muted" />
          <div className="mt-3 h-8 w-48 rounded bg-muted" />
        </div>
        <div className="h-9 w-28 rounded-md bg-muted" />
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="h-28 rounded-lg border border-border bg-card p-4">
          <div className="h-4 w-20 rounded bg-muted" />
          <div className="mt-5 h-7 w-24 rounded bg-muted" />
        </div>
        <div className="h-28 rounded-lg border border-border bg-card p-4">
          <div className="h-4 w-24 rounded bg-muted" />
          <div className="mt-5 h-7 w-20 rounded bg-muted" />
        </div>
        <div className="h-28 rounded-lg border border-border bg-card p-4">
          <div className="h-4 w-16 rounded bg-muted" />
          <div className="mt-5 h-7 w-28 rounded bg-muted" />
        </div>
      </div>
      <div className="mt-5 rounded-lg border border-border bg-card p-4">
        <div className="h-4 w-36 rounded bg-muted" />
        <div className="mt-5 space-y-3">
          <div className="h-14 rounded-md bg-muted" />
          <div className="h-14 rounded-md bg-muted" />
          <div className="h-14 rounded-md bg-muted" />
        </div>
      </div>
    </section>
  )
}
