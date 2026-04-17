"use client"

import { createContext, useContext, useEffect, useState, type ReactNode } from "react"

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
}

const SessionContext = createContext<SessionContextValue>({
  user: null,
  loading: true,
})

export function useSession() {
  return useContext(SessionContext)
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<SessionUser | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch("/auth/me", { credentials: "include" })
      .then((r) => {
        if (!r.ok) throw new Error("not authenticated")
        return r.json()
      })
      .then((d) => {
        if (d.user) setUser(d.user)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  return (
    <SessionContext value={{ user, loading }}>
      {children}
    </SessionContext>
  )
}
