import { NavLink, Outlet } from 'react-router-dom'

const primaryNavItems = [
  {
    label: '新建翻译',
    description: '提交 YouTube 视频任务',
    to: '/translations/new',
    end: true,
  },
  {
    label: '当前任务',
    description: '查看当前任务进展',
    to: '/tasks/current',
    end: true,
  },
  {
    label: '项目详情',
    description: '查看当前或最近任务结果',
    to: '/projects/latest',
    end: true,
  },
] as const

const secondaryNavItems = [
  {
    label: '我的项目',
    description: '回看历史任务',
    to: '/projects',
    end: true,
  },
  {
    label: '我的音色',
    description: '查看已保存的音色',
    to: '/voices',
    end: true,
  },
  {
    label: '工作台',
    description: '快速入门与配置说明',
    to: '/settings',
    end: true,
  },
] as const

export function AppShell() {
  return (
    <div className="app-shell">
      <div className="mx-auto flex min-h-screen max-w-7xl flex-col px-4 py-6 sm:px-6 lg:px-8">
        <header className="surface-card flex flex-col gap-4 p-6 lg:flex-row lg:items-end lg:justify-between">
          <div className="max-w-2xl space-y-2">
            <p className="eyebrow">AIVideoTrans</p>
            <div className="space-y-2">
              <h1 className="section-title">AIVideoTrans Web Console</h1>
              <p className="muted-copy">
                围绕“新建翻译、跟进任务、处理审核、查看结果”这条单用户主链路来操作。
              </p>
            </div>
          </div>
          <div className="rounded-3xl border border-ink-950/10 bg-sand-50/80 px-5 py-4 text-sm text-ink-900/70">
            <div>单用户工作台</div>
            <div>审核出现时请优先处理</div>
          </div>
        </header>

        <div className="mt-6 grid flex-1 gap-6 lg:grid-cols-[260px_minmax(0,1fr)]">
          <aside className="space-y-4">
            <nav className="surface-card p-3">
              <p className="eyebrow px-3 pb-2 pt-1">主要入口</p>
              <div className="space-y-2">
                {primaryNavItems.map((item) => (
                  <NavLink
                    key={item.to}
                    end={item.end}
                    className={({ isActive }) =>
                      [
                        'block rounded-2xl px-4 py-3 transition',
                        isActive
                          ? 'bg-ink-950 text-white shadow-lg'
                          : 'bg-transparent text-ink-950 hover:bg-sand-100/90',
                      ].join(' ')
                    }
                    to={item.to}
                  >
                    <div className="text-sm font-semibold">{item.label}</div>
                    <div className="mt-1 text-xs opacity-75">{item.description}</div>
                  </NavLink>
                ))}
              </div>
            </nav>

            <nav className="surface-card p-3">
              <p className="eyebrow px-3 pb-2 pt-1">补充入口</p>
              <div className="space-y-2">
                {secondaryNavItems.map((item) => (
                  <NavLink
                    key={item.to}
                    end={item.end}
                    className={({ isActive }) =>
                      [
                        'block rounded-2xl px-4 py-3 transition',
                        isActive
                          ? 'bg-ink-950 text-white shadow-lg'
                          : 'bg-transparent text-ink-950 hover:bg-sand-100/90',
                      ].join(' ')
                    }
                    to={item.to}
                  >
                    <div className="text-sm font-semibold">{item.label}</div>
                    <div className="mt-1 text-xs opacity-75">{item.description}</div>
                  </NavLink>
                ))}
              </div>
            </nav>
          </aside>

          <main className="space-y-6">
            <Outlet />
          </main>
        </div>
      </div>
    </div>
  )
}
