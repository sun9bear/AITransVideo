"use client"

/**
 * Admin presence + handoff routing config card.
 *
 * Plan 2026-05-08 follow-up §"管理员/运营/客服只要登录，就显示在线"
 * — admin can adjust:
 *   - heartbeat 频率（前端 ping 周期，默认 30s）
 *   - 在线阈值（后端判定 online 的窗口，默认 60s）
 *   - 离线兜底超时（用户转人工后多久无回复 → 切到 WeChat QR）
 *   - 离线提示文案（QR 卡片上方的 banner 文字）
 */

interface Values {
  support_admin_heartbeat_interval_seconds: number
  support_admin_online_threshold_seconds: number
  support_handoff_offline_fallback_minutes: number
  support_offline_message: string
}

export function PresenceConfigCard({
  values,
  onChange,
}: {
  values: Values
  onChange: (next: Partial<Values>) => void
}) {
  return (
    <section className="space-y-4 rounded-xl border border-border bg-card p-4">
      <h2 className="text-base font-semibold text-foreground">在线状态与离线兜底</h2>

      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="心跳频率（秒）" hint="前端 admin tab ping 后端的间隔；默认 30">
          <input
            type="number"
            min={5}
            max={600}
            value={values.support_admin_heartbeat_interval_seconds}
            onChange={(e) =>
              onChange({
                support_admin_heartbeat_interval_seconds: Math.max(
                  5,
                  Math.min(600, parseInt(e.target.value, 10) || 30),
                ),
              })
            }
            className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
          />
        </Field>
        <Field
          label="在线阈值（秒）"
          hint="心跳超过这个秒数 → 视为离线；建议 ≥ 心跳频率 × 2"
        >
          <input
            type="number"
            min={10}
            max={1200}
            value={values.support_admin_online_threshold_seconds}
            onChange={(e) =>
              onChange({
                support_admin_online_threshold_seconds: Math.max(
                  10,
                  Math.min(1200, parseInt(e.target.value, 10) || 60),
                ),
              })
            }
            className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
          />
        </Field>
        <Field
          label="离线兜底超时（分钟）"
          hint="用户转人工后等待多久无人回复 → 切到 WeChat QR；0 = 关闭"
        >
          <input
            type="number"
            min={0}
            max={120}
            value={values.support_handoff_offline_fallback_minutes}
            onChange={(e) =>
              onChange({
                support_handoff_offline_fallback_minutes: Math.max(
                  0,
                  Math.min(120, parseInt(e.target.value, 10) || 0),
                ),
              })
            }
            className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
          />
        </Field>
      </div>

      <Field label="离线提示文案（QR 卡片上方）">
        <textarea
          rows={2}
          value={values.support_offline_message}
          onChange={(e) =>
            onChange({ support_offline_message: e.target.value })
          }
          className="w-full resize-none rounded border border-border bg-background px-2 py-1 text-sm"
        />
      </Field>

      <p className="rounded border border-dashed border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
        改动后需刷新 admin 端浏览器或下次 admin 登录才会用新心跳频率；
        服务端在线判定立即生效。
      </p>
    </section>
  )
}

function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block text-foreground">{label}</span>
      {children}
      {hint ? (
        <span className="mt-1 block text-xs text-muted-foreground">{hint}</span>
      ) : null}
    </label>
  )
}
