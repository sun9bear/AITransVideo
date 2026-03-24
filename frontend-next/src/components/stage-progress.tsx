import type { StageProgressItem } from "@/types/jobs"

const stateStyles = {
  complete: "bg-primary text-white",
  current: "bg-cyan-500 text-white ring-2 ring-cyan-500/30",
  error: "bg-red-500 text-white",
  upcoming: "bg-white/10 text-white/40",
} as const

const connectorStyles = {
  complete: "bg-primary/60",
  current: "bg-cyan-500/40",
  error: "bg-red-400/60",
  upcoming: "bg-white/10",
} as const

const labelStyles = {
  complete: "text-white/60",
  current: "text-cyan-400 font-semibold",
  error: "text-red-400 font-semibold",
  upcoming: "text-white/30",
} as const

export function StageProgress({ items }: { items: readonly StageProgressItem[] }) {
  return (
    <div className="flex items-center gap-0 overflow-x-auto py-2">
      {items.map((item, index) => (
        <div key={item.key} className="flex items-center">
          <div className="flex flex-col items-center gap-1 min-w-[60px]">
            <div
              className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${stateStyles[item.state]}`}
              title={item.description}
            >
              {item.state === "complete" ? "✓" : index + 1}
            </div>
            <span className={`text-[10px] leading-tight text-center whitespace-nowrap ${labelStyles[item.state]}`}>
              {item.label}
            </span>
          </div>
          {index < items.length - 1 ? (
            <div className={`h-[2px] w-6 shrink-0 ${connectorStyles[item.state]}`} />
          ) : null}
        </div>
      ))}
    </div>
  )
}
