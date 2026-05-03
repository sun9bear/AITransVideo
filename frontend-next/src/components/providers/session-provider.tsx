"use client"

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react"

export type SessionUser = {
  id: string
  display_name: string
  email: string
  phone_number?: string
  role?: string
  plan_code?: string
  created_at?: string
}

type SessionContextValue = {
  user: SessionUser | null
  loading: boolean
  error: string | null
  refresh: () => Promise<void>
}

const SessionContext = createContext<SessionContextValue>({
  user: null,
  loading: true,
  error: null,
  refresh: async () => {},
})

export function useSession() {
  return useContext(SessionContext)
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<SessionUser | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)

    try {
      const response = await fetch("/auth/me", { credentials: "include" })
      if (response.status === 401) {
        setUser(null)
        return
      }
      if (!response.ok) {
        throw new Error(`/auth/me failed with HTTP ${response.status}`)
      }

      const data = await response.json()
      setUser(data.user ?? null)
    } catch (err) {
      console.error("Failed to load session", err)
      setUser(null)
      setError("登录状态加载失败，请重试。")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return (
    <SessionContext value={{ user, loading, error, refresh }}>
      {children}
    </SessionContext>
  )
}
