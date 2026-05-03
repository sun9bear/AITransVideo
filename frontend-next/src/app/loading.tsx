export default function Loading() {
  return (
    <main className="min-h-screen bg-background px-4 py-10 text-foreground">
      <div className="mx-auto w-full max-w-5xl">
        <div className="h-8 w-40 rounded-md bg-muted" />
        <div className="mt-6 grid gap-4 md:grid-cols-3">
          <div className="h-32 rounded-lg border border-border bg-card p-4">
            <div className="h-4 w-24 rounded bg-muted" />
            <div className="mt-5 h-8 w-32 rounded bg-muted" />
          </div>
          <div className="h-32 rounded-lg border border-border bg-card p-4">
            <div className="h-4 w-28 rounded bg-muted" />
            <div className="mt-5 h-8 w-24 rounded bg-muted" />
          </div>
          <div className="h-32 rounded-lg border border-border bg-card p-4">
            <div className="h-4 w-20 rounded bg-muted" />
            <div className="mt-5 h-8 w-28 rounded bg-muted" />
          </div>
        </div>
        <div className="mt-6 h-64 rounded-lg border border-border bg-card p-4">
          <div className="h-4 w-36 rounded bg-muted" />
          <div className="mt-6 space-y-3">
            <div className="h-3 w-full rounded bg-muted" />
            <div className="h-3 w-11/12 rounded bg-muted" />
            <div className="h-3 w-9/12 rounded bg-muted" />
          </div>
        </div>
      </div>
    </main>
  )
}
