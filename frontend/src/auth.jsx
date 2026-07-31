import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { api } from './api.js'

// One shared identity fetch for the whole app. Every page needs to know who's
// signed in and whether they hold master privileges, and hitting /api/me from
// each one would be wasteful.
const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      setUser(await api.me())
    } catch {
      // A failed identity check just means "logged out" as far as the UI cares.
      setUser({ authenticated: false, loginEnabled: false })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const logout = useCallback(async () => {
    await api.logout().catch(() => {})
    await refresh()
  }, [refresh])

  return (
    <AuthContext.Provider value={{ user, loading, refresh, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>')
  return ctx
}
