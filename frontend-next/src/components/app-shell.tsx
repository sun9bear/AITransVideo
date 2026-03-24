"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import { useEffect, useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import {
  PlusCircle,
  ListChecks,
  FolderOpen,
  Mic2,
  BarChart3,
  Bell,
  User,
  HelpCircle,
  ChevronLeft,
  ChevronRight,
  LogOut,
  Menu,
  X,
  Sun,
  Moon,
} from "lucide-react"

const navGroups = [
  {
    label: "工作流",
    items: [
      { label: "新建翻译", href: "/translations/new", icon: PlusCircle },
      { label: "当前任务", href: "/tasks/current", icon: ListChecks },
      { label: "我的项目", href: "/projects", icon: FolderOpen },
    ],
  },
  {
    label: "资源",
    items: [
      { label: "我的音色", href: "/voices", icon: Mic2 },
      { label: "用量统计", href: "/usage", icon: BarChart3 },
    ],
  },
  {
    label: "账户",
    items: [
      { label: "通知", href: "/notifications", icon: Bell },
      { label: "账户设置", href: "/account", icon: User },
      { label: "帮助中心", href: "/help", icon: HelpCircle },
    ],
  },
]

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const [collapsed, setCollapsed] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)
  const [darkMode, setDarkMode] = useState(true)
  const [user, setUser] = useState<{ display_name: string; email: string } | null>(null)

  useEffect(() => {
    fetch("/auth/me", { credentials: "include" })
      .then((r) => r.json())
      .then((d) => { if (d.user) setUser(d.user) })
      .catch(() => {})
  }, [])

  useEffect(() => {
    document.documentElement.classList.toggle("dark", darkMode)
  }, [darkMode])

  // Close mobile sidebar on route change
  useEffect(() => {
    setMobileOpen(false)
  }, [pathname])

  const handleLogout = async () => {
    await fetch("/auth/logout", { method: "POST", credentials: "include" })
    toast.success("已登出")
    window.location.href = "/auth/login"
  }

  // Auth pages: clean layout without sidebar/header
  if (pathname.startsWith("/auth/")) {
    return <>{children}</>
  }

  const sidebarContent = (
    <>
      {/* Logo */}
      <div className="flex h-14 items-center justify-between px-3 border-b border-border">
        {!collapsed && (
          <Link href="/" className="flex items-center gap-2 truncate">
            <div className="h-7 w-7 rounded-lg bg-gradient-to-br from-violet-500 to-cyan-500 flex items-center justify-center">
              <span className="text-xs font-bold text-white">AI</span>
            </div>
            <span className="font-heading text-sm font-semibold text-foreground">AIVideoTrans</span>
          </Link>
        )}
        {collapsed && (
          <div className="mx-auto h-7 w-7 rounded-lg bg-gradient-to-br from-violet-500 to-cyan-500 flex items-center justify-center">
            <span className="text-xs font-bold text-white">AI</span>
          </div>
        )}
      </div>

      {/* Nav groups */}
      <nav className="flex-1 overflow-y-auto py-3 px-2 space-y-4">
        {navGroups.map((group) => (
          <div key={group.label}>
            {!collapsed && (
              <p className="px-3 mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60">
                {group.label}
              </p>
            )}
            <div className="space-y-0.5">
              {group.items.map((item) => {
                const Icon = item.icon
                const isActive =
                  pathname === item.href ||
                  (item.href !== "/" && pathname.startsWith(item.href)) ||
                  (item.href === "/tasks/current" && pathname.startsWith("/workspace/"))

                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition-all ${
                      isActive
                        ? "bg-primary/15 text-primary font-medium"
                        : "text-muted-foreground hover:bg-accent hover:text-foreground"
                    }`}
                    title={collapsed ? item.label : undefined}
                  >
                    <Icon className={`h-4 w-4 shrink-0 ${isActive ? "text-primary" : ""}`} />
                    {!collapsed && <span className="truncate">{item.label}</span>}
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
          size={collapsed ? "icon" : "sm"}
          className={`${collapsed ? "w-full" : "w-full justify-start gap-2"} text-muted-foreground hover:text-foreground`}
          onClick={() => setDarkMode(!darkMode)}
        >
          {darkMode ? <Sun className="h-4 w-4 shrink-0" /> : <Moon className="h-4 w-4 shrink-0" />}
          {!collapsed && <span className="text-xs">{darkMode ? "浅色模式" : "深色模式"}</span>}
        </Button>

        {/* User info */}
        {user && !collapsed && (
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
        {user && collapsed && (
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
    <div className="flex min-h-screen bg-background">
      {/* Mobile overlay */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm lg:hidden"
          onClick={() => setMobileOpen(false)}
        />
      )}

      {/* Sidebar — desktop */}
      <aside
        className={`hidden lg:flex sticky top-0 h-screen flex-col border-r border-border bg-sidebar transition-all duration-200 ${
          collapsed ? "w-16" : "w-56"
        }`}
      >
        {sidebarContent}
        {/* Collapse toggle */}
        <button
          className="absolute -right-3 top-20 z-10 flex h-6 w-6 items-center justify-center rounded-full border border-border bg-card text-muted-foreground hover:text-foreground shadow-sm"
          onClick={() => setCollapsed(!collapsed)}
        >
          {collapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronLeft className="h-3 w-3" />}
        </button>
      </aside>

      {/* Sidebar — mobile */}
      <aside
        className={`fixed inset-y-0 left-0 z-50 flex w-64 flex-col border-r border-border bg-sidebar transition-transform duration-200 lg:hidden ${
          mobileOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <button
          className="absolute right-2 top-3 z-10 flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground hover:text-foreground"
          onClick={() => setMobileOpen(false)}
        >
          <X className="h-5 w-5" />
        </button>
        {sidebarContent}
      </aside>

      {/* Main content */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Top bar */}
        <header className="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-border bg-background/80 backdrop-blur-md px-4 lg:px-6">
          <div className="flex items-center gap-3">
            {/* Mobile hamburger */}
            <Button
              variant="ghost"
              size="icon"
              className="lg:hidden h-8 w-8 text-muted-foreground"
              onClick={() => setMobileOpen(true)}
            >
              <Menu className="h-5 w-5" />
            </Button>
            {/* Breadcrumb-style page indicator */}
            <span className="text-sm font-medium text-foreground font-heading">
              {pathname.startsWith("/workspace/") ? "工作区" : navGroups.flatMap(g => g.items).find(i => pathname.startsWith(i.href))?.label || "AIVideoTrans"}
            </span>
          </div>
          <div className="flex items-center gap-3">
            {user && (
              <span className="hidden sm:block text-xs text-muted-foreground">
                {user.display_name}
              </span>
            )}
            <div className="h-7 w-7 rounded-full bg-gradient-to-br from-violet-500 to-cyan-500 flex items-center justify-center">
              <span className="text-[10px] font-bold text-white">
                {user?.display_name?.[0]?.toUpperCase() || "U"}
              </span>
            </div>
          </div>
        </header>

        {/* Page content */}
        <main className="flex-1 p-4 lg:p-6">
          {children}
        </main>
      </div>
    </div>
  )
}
