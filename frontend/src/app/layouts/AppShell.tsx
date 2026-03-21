import { NavLink, Outlet } from 'react-router-dom'

const navItems = [
  { label: '新建翻译', to: '/translations/new', end: true },
  { label: '当前任务', to: '/tasks/current', end: true },
  { label: '我的项目', to: '/projects', end: true },
  { label: '我的音色', to: '/voices', end: true },
  { label: '工作台', to: '/settings', end: true },
] as const

export function AppShell() {
  return (
    <div className="app-shell flex min-h-screen">
      {/* Sidebar */}
      <aside className="fixed inset-y-0 left-0 z-30 flex w-48 flex-col border-r border-ink-950/8 bg-white/60 backdrop-blur-xl">
        <div className="flex h-14 items-center px-5">
          <span className="text-base font-bold tracking-tight text-ink-950">
            AIVideoTrans
          </span>
        </div>

        <nav className="flex-1 space-y-1 px-3 py-3">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              end={item.end}
              className={({ isActive }) =>
                [
                  'block rounded-xl px-3 py-2.5 text-sm font-medium transition',
                  isActive
                    ? 'bg-ink-950 text-white shadow-md'
                    : 'text-ink-900/70 hover:bg-ink-950/5 hover:text-ink-950',
                ].join(' ')
              }
              to={item.to}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-ink-950/8 px-4 py-3">
          <p className="text-xs text-ink-900/45">单用户工作台</p>
        </div>
      </aside>

      {/* Main area */}
      <div className="flex min-h-screen flex-1 flex-col pl-48">
        {/* Top bar */}
        <header className="sticky top-0 z-20 flex h-14 items-center justify-between border-b border-ink-950/8 bg-white/70 px-6 backdrop-blur-xl">
          <div />
          <div className="flex items-center gap-4">
            <span className="rounded-full bg-ink-950/5 px-3 py-1.5 text-xs font-medium text-ink-900/60">
              审核出现时请优先处理
            </span>
          </div>
        </header>

        {/* Content */}
        <main className="flex-1 px-6 py-6 lg:px-10 lg:py-8">
          <div className="mx-auto max-w-6xl">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  )
}
