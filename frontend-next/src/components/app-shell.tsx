"use client"

import { Link } from "@/i18n/navigation"
import { usePathname } from "@/i18n/navigation"
import { useLocale } from "next-intl"
import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import { useSession } from "@/components/providers/session-provider"
import { clearPostAuthSessionHint } from "@/lib/auth/post-auth-redirect"
import {
  clearAnonConvertReady,
  maybeClaimAnonPreviewAfterLogin,
  scopeAnonConvertReadyToUser,
} from "@/lib/api/claim"
import { WORKSPACE_THEME_STORAGE_KEY } from "@/lib/theme"
import { Button } from "@/components/ui/button"
import { BrandMark, BrandLockup } from "@/components/marketing/brand-mark"
import { SupportWidget } from "@/components/support/SupportWidget"
import { AdminPresenceSwitcher } from "@/components/support/AdminPresenceSwitcher"
import { LocaleSwitcher } from "@/components/i18n/LocaleSwitcher"
import {
  NotificationBell,
  useNotificationUnreadCount,
} from "@/components/notifications/NotificationBell"
import { NotificationPopupModal } from "@/components/notifications/NotificationPopupModal"
import {
  Video,
  Mic2,
  BarChart3,
  Bell,
  User,
  Users,
  Headphones,
  HelpCircle,
  ChevronLeft,
  ChevronRight,
  LogOut,
  Menu,
  X,
  Sun,
  Moon,
  Settings2,
  AudioLines,
  Wallet,
  MessageSquareText,
  Activity,
  Calculator,
  ClipboardList,
  TrendingUp,
  Globe2,
  Search,
  CreditCard,
  AlertTriangle,
  ShieldAlert,
  RefreshCw,
  HardDrive,
  Cloud,
  FileSearch,
} from "lucide-react"

type NavItem = {
  label: string
  href: string
  icon: typeof Video
}

type NavGroup = {
  label: string
  items: NavItem[]
  adminOnly?: boolean
}

const navGroups: NavGroup[] = [
  {
    label: "工作流",
    items: [
      { label: "视频翻译", href: "/projects", icon: Video },
      { label: "我的音色", href: "/voices", icon: Mic2 },
    ],
  },
  {
    label: "资源",
    items: [
      { label: "用量统计", href: "/usage", icon: BarChart3 },
    ],
  },
  {
    label: "账户",
    items: [
      { label: "通知", href: "/notifications", icon: Bell },
      { label: "账单管理", href: "/settings/billing", icon: CreditCard },
      { label: "账户设置", href: "/settings", icon: User },
      { label: "帮助中心", href: "/help", icon: HelpCircle },
    ],
  },
  {
    label: "管理",
    adminOnly: true,
    items: [
      { label: "任务管理", href: "/admin/jobs", icon: ClipboardList },
      { label: "用户管理", href: "/admin/users", icon: Users },
      { label: "音色管理", href: "/admin/voices", icon: AudioLines },
      { label: "CosyVoice", href: "/admin/cosyvoice", icon: AudioLines },
      { label: "模型管理", href: "/admin/prompts", icon: MessageSquareText },
      { label: "审校监控", href: "/admin/s2-monitor", icon: Activity },
      { label: "智能版监控", href: "/admin/smart-analytics", icon: BarChart3 },
      { label: "报告分析", href: "/admin/report-analysis", icon: FileSearch },
      { label: "访问监控", href: "/admin/traffic", icon: Globe2 },
      { label: "转化监控", href: "/admin/conversions", icon: TrendingUp },
      { label: "安全监控", href: "/admin/security", icon: ShieldAlert },
      { label: "发现优化", href: "/admin/discovery", icon: Search },
      { label: "成本管理", href: "/admin/costs", icon: Calculator },
      { label: "磁盘管理", href: "/admin/disk", icon: HardDrive },
      { label: "网盘备份", href: "/admin/pan/dashboard", icon: Cloud },
      { label: "定价管理", href: "/admin/pricing", icon: Wallet },
      { label: "点数校准", href: "/admin/credits-monitor", icon: TrendingUp },
      { label: "客服管理", href: "/admin/support", icon: Headphones },
      { label: "系统公告", href: "/admin/support/announcements", icon: MessageSquareText },
      { label: "系统设置", href: "/admin/settings", icon: Settings2 },
    ],
  },
]

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const locale = useLocale()
  const { user, error: sessionError, refresh: refreshSession } = useSession()
  // Live unread count drives the sidebar "通知" badge. The hook polls
  // every 30s when the tab is visible and skips while hidden, so the
  // sidebar always reflects whatever the backend currently reports.
  const unreadNotifications = useNotificationUnreadCount(user !== null)
  const [collapsed, setCollapsed] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)
  const claimRetryUserRef = useRef<string | null>(null)
  // darkMode is hydrated from localStorage on first client render so the
  // user's preference persists across reloads. Default is light for the
  // workspace because most mobile browser chrome and auth surfaces are light.
  const [darkMode, setDarkMode] = useState(false)

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      try {
        const stored = window.localStorage.getItem(WORKSPACE_THEME_STORAGE_KEY)
        if (stored === "dark") setDarkMode(true)
        else if (stored === "light") setDarkMode(false)
      } catch {
        // localStorage not available (privacy mode etc.) — keep default.
      }
    })
    return () => window.cancelAnimationFrame(frame)
  }, [])

  useEffect(() => {
    // Toggle the legacy .dark class on html so any shadcn `dark:` utility
    // still resolves correctly. The data-theme attribute drives the actual
    // surface tokens inside the (app) scope.
    document.documentElement.classList.toggle("dark", darkMode)
    // Also stamp the theme on html itself so Radix/base-ui portals (Dialog,
    // Toast, Tooltip, DropdownMenu — all of which render to document.body
    // via Portal) pick up the right ink token family. Without this, the
    // (app) wrapper's data-theme is on a non-ancestor of the portal root,
    // so portal content falls through to the default .dark steel-blue tokens.
    // Clean up on unmount so navigating to (marketing) / (auth) doesn't
    // leave a stale data-theme on html — those route groups define their
    // own scope on their layout divs.
    document.documentElement.setAttribute("data-theme", darkMode ? "ink-dark" : "ink")
    try {
      window.localStorage.setItem(WORKSPACE_THEME_STORAGE_KEY, darkMode ? "dark" : "light")
    } catch {
      // ignore — see above
    }
    return () => {
      document.documentElement.removeAttribute("data-theme")
    }
  }, [darkMode])

  useEffect(() => {
    if (!user?.id) {
      claimRetryUserRef.current = null
      return
    }
    scopeAnonConvertReadyToUser(user.id)
    if (claimRetryUserRef.current === user.id) {
      return
    }
    claimRetryUserRef.current = user.id
    void maybeClaimAnonPreviewAfterLogin(user.id)
  }, [user?.id])

  // Close mobile sidebar on route change
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setMobileOpen(false))
    return () => window.cancelAnimationFrame(frame)
  }, [pathname])

  useEffect(() => {
    if (!mobileOpen) return

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = "hidden"

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMobileOpen(false)
      }
    }

    window.addEventListener("keydown", handleKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener("keydown", handleKeyDown)
    }
  }, [mobileOpen])

  const handleLogout = async () => {
    await fetch("/auth/logout", { method: "POST", credentials: "include" })
    clearPostAuthSessionHint()
    clearAnonConvertReady()
    toast.success("已登出")
    // Hard reload to clear in-memory session state, but keep the visitor's UI
    // locale (UI-04 Step 5.6): /en/workspace logs out to /en/auth/login, zh
    // stays bare per localePrefix:"as-needed". locale is read, not a new lang
    // source (R5); the hard-navigation semantics are unchanged.
    window.location.href =
      locale && locale !== "zh" ? `/${locale}/auth/login` : "/auth/login"
  }

  const renderSidebarContent = (isCollapsed: boolean, onNavigate?: () => void) => (
    <>
      {/* Brand mark — uses the marketing SealStamp asset so the workspace
          and the marketing surface share one visual identity. Was previously
          a violet→cyan gradient with "AI" text; that block predated the
          ink-aesthetic redesign and read as out-of-system. */}
      <div className="flex h-14 items-center justify-between px-3 border-b border-border">
        {!isCollapsed && (
          <Link href="/" className="flex items-center gap-2 truncate" onClick={onNavigate}>
            <BrandLockup />
          </Link>
        )}
        {isCollapsed && (
          <Link href="/" className="mx-auto" aria-label="AITrans.Video 首页" onClick={onNavigate}>
            <BrandMark size={28} />
          </Link>
        )}
      </div>

      {/* Nav groups */}
      <nav className="flex-1 overflow-y-auto py-3 px-2 space-y-4">
        {navGroups.filter((group) => {
          if (group.adminOnly) {
            return user?.role === 'admin'
          }
          return true
        }).map((group) => (
          <div key={group.label}>
            {!isCollapsed && (
              <p className="px-3 mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-sidebar-foreground/40">
                {group.label}
              </p>
            )}
            <div className="space-y-0.5">
              {group.items.map((item) => {
                const Icon = item.icon
                // /admin/support has a sub-route /admin/support/announcements;
                // make /admin/support match only on exact path so the two
                // sidebar entries don't both highlight at once. Other items
                // keep prefix matching (e.g. /workspace/{id} → /projects).
                const isActive =
                  pathname === item.href ||
                  (item.href === "/admin/support"
                    ? false
                    : item.href !== "/" && pathname.startsWith(item.href)) ||
                  (item.href === "/projects" && pathname.startsWith("/workspace/"))
                // Unread badge for the "通知" item — drives the user
                // back to the notification center when something new
                // lands. Only shows if count > 0.
                const showNotifBadge =
                  item.href === "/notifications" && unreadNotifications > 0

                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition-all ${
                      isActive
                        ? "bg-primary/15 text-primary font-medium"
                        : "text-muted-foreground hover:bg-accent hover:text-foreground"
                    }`}
                    title={isCollapsed ? item.label : undefined}
                    onClick={onNavigate}
                  >
                    <Icon className={`h-4 w-4 shrink-0 ${isActive ? "text-primary" : ""}`} />
                    {!isCollapsed && (
                      <span className="flex flex-1 items-center justify-between truncate">
                        <span className="truncate">{item.label}</span>
                        {showNotifBadge ? (
                          <span
                            aria-hidden
                            className="ml-2 inline-flex min-w-[18px] items-center justify-center rounded-full bg-[color:var(--cinnabar,#C73E3A)] px-1 text-[10px] font-semibold leading-4 text-white"
                          >
                            {unreadNotifications > 99 ? "99+" : unreadNotifications}
                          </span>
                        ) : null}
                      </span>
                    )}
                    {isCollapsed && showNotifBadge ? (
                      <span
                        aria-hidden
                        className="absolute right-1 top-1 inline-flex min-w-[14px] items-center justify-center rounded-full bg-[color:var(--cinnabar,#C73E3A)] px-1 text-[9px] font-semibold leading-3 text-white"
                      >
                        {unreadNotifications > 9 ? "9+" : unreadNotifications}
                      </span>
                    ) : null}
                  </Link>
                )
              })}
            </div>
          </div>
        ))}
      </nav>

      {/* Bottom: theme toggle + user */}
      <div className="border-t border-border p-2 space-y-2">
        {/* Theme toggle */}
        <Button
          variant="ghost"
          size={isCollapsed ? "icon" : "sm"}
          className={`${isCollapsed ? "w-full" : "w-full justify-start gap-2"} text-muted-foreground hover:text-foreground`}
          onClick={() => setDarkMode(!darkMode)}
          aria-label={darkMode ? "切换到浅色模式" : "切换到深色模式"}
        >
          {darkMode ? <Sun className="h-4 w-4 shrink-0" aria-hidden="true" /> : <Moon className="h-4 w-4 shrink-0" aria-hidden="true" />}
          {!isCollapsed && <span className="text-xs">{darkMode ? "浅色模式" : "深色模式"}</span>}
        </Button>

        {/* User info */}
        {user && !isCollapsed && (
          <div className="px-2 space-y-1.5">
            <p className="text-xs font-medium text-foreground truncate">{user.display_name}</p>
            <p className="text-[10px] text-muted-foreground truncate">{user.email}</p>
            <Button
              variant="ghost"
              size="sm"
              className="w-full justify-start gap-2 text-xs text-muted-foreground hover:text-destructive"
              onClick={handleLogout}
            >
              <LogOut className="h-3 w-3" />
              登出
            </Button>
          </div>
        )}
        {user && isCollapsed && (
          <Button
            variant="ghost"
            size="icon"
            className="w-full text-muted-foreground hover:text-destructive"
            onClick={handleLogout}
            title="登出"
          >
            <LogOut className="h-4 w-4" />
          </Button>
        )}
      </div>
    </>
  )

  return (
    // Theme scope: ink-dark for darkMode, ink (light paper) for light mode.
    // Both inherit the marketing brand language; the toggle simply swaps
    // luminance + grain. data-theme attribute drives the [data-theme=...]
    // token blocks in globals.css.
    <div
      data-theme={darkMode ? "ink-dark" : "ink"}
      className="flex min-h-screen overflow-x-hidden bg-background text-foreground"
    >
      {mobileOpen ? (
        <div
          className="fixed inset-0 z-40 bg-black/45 backdrop-blur-sm lg:hidden"
          onClick={() => setMobileOpen(false)}
        />
      ) : null}
      {/* Sidebar — desktop */}
      <aside
        className={`hidden lg:flex sticky top-0 h-screen flex-col border-r border-border bg-sidebar transition-[width] duration-200 ${
          collapsed ? "w-16" : "w-56"
        }`}
      >
        {renderSidebarContent(collapsed)}
        {/* Collapse toggle */}
        <button
          className="absolute -right-3 top-20 z-10 flex h-6 w-6 items-center justify-center rounded-full border border-border bg-card text-muted-foreground hover:text-foreground shadow-sm"
          onClick={() => setCollapsed(!collapsed)}
          aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"}
        >
          {collapsed ? <ChevronRight className="h-3 w-3" aria-hidden="true" /> : <ChevronLeft className="h-3 w-3" aria-hidden="true" />}
        </button>
      </aside>

      {/* Sidebar — mobile */}
      {mobileOpen ? (
        <aside className="fixed inset-y-0 left-0 z-50 flex w-56 max-w-[72dvw] flex-col border-r border-border bg-sidebar shadow-xl lg:hidden">
          <button
            type="button"
            className="absolute right-2 top-3 z-10 flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground hover:text-foreground"
            onClick={() => setMobileOpen(false)}
            aria-label="关闭侧边栏"
          >
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
          {renderSidebarContent(false, () => setMobileOpen(false))}
        </aside>
      ) : null}

      {/* Main content */}
      <div className="flex min-w-0 flex-1 flex-col overflow-x-hidden">
        {/* Top bar */}
        <header className="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-border bg-background/80 backdrop-blur-md px-3 sm:px-4 lg:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 shrink-0 text-muted-foreground lg:hidden"
              onClick={() => setMobileOpen(true)}
              aria-label="打开菜单"
            >
              <Menu className="h-5 w-5" aria-hidden="true" />
            </Button>
            {/* Breadcrumb-style page indicator */}
            <span className="truncate text-sm font-medium text-foreground font-heading">
              {pathname.startsWith("/workspace/") ? "工作区" : navGroups.flatMap(g => g.items).find(i => pathname.startsWith(i.href))?.label || "AITrans.Video"}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <LocaleSwitcher />
            {/* Notification bell — visible to all logged-in users. Polls
                unread count every 30s, flashes tab title when new
                notification arrives in background tab. */}
            <NotificationBell isAuthenticated={user !== null} />
            {/* Admin-only: presence status switcher (online / paused / offline).
                Drives heartbeat to /api/admin/support/heartbeat. */}
            <AdminPresenceSwitcher isAdmin={user?.role === "admin"} />
            {user && (
              <span className="hidden sm:block text-xs text-muted-foreground">
                {user.display_name}
              </span>
            )}
            <div
              className="h-7 w-7 rounded-full flex items-center justify-center"
              style={{
                backgroundColor: "color-mix(in oklab, var(--cinnabar) 14%, transparent)",
                color: "var(--cinnabar)",
                border: "1px solid color-mix(in oklab, var(--cinnabar) 35%, transparent)",
              }}
            >
              <span className="text-[10px] font-semibold">
                {user?.display_name?.[0]?.toUpperCase() || "U"}
              </span>
            </div>
          </div>
        </header>

        {sessionError ? (
          <div className="border-b border-destructive/25 bg-destructive/10 px-4 py-3 text-sm text-destructive lg:px-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <AlertTriangle className="h-4 w-4" aria-hidden="true" />
                <span>{sessionError}</span>
              </div>
              <button
                type="button"
                onClick={() => void refreshSession()}
                className="inline-flex h-8 items-center justify-center gap-2 rounded-md border border-destructive/30 bg-background/60 px-3 text-xs font-medium text-destructive transition hover:bg-background focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-destructive"
              >
                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                重试
              </button>
            </div>
          </div>
        ) : null}

        {/* Page content */}
        <main id="main-content" className="min-w-0 flex-1 overflow-x-hidden p-3 sm:p-4 lg:p-6">
          {children}
        </main>
      </div>
      {/* Floating support widget (plan 2026-05-08 §8.2) */}
      <SupportWidget />
      {/* Modal for popup-flagged system announcements. Mounts at the
          AppShell level so it shows on every authenticated route. */}
      <NotificationPopupModal isAuthenticated={user !== null} />
    </div>
  )
}
