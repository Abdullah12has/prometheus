import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { api, ApiError } from './api'
import type { SessionUser } from './types'

type AuthStatus = 'checking' | 'signed-out' | 'signed-in'

interface AuthState {
  status: AuthStatus
  user: SessionUser | null
  error: string | null
  login: (password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthState | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('checking')
  const [user, setUser] = useState<SessionUser | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .get<SessionUser>('/api/auth/me')
      .then((session) => {
        if (cancelled) return
        setUser(session)
        setStatus(session.authenticated ? 'signed-in' : 'signed-out')
      })
      .catch(() => {
        if (cancelled) return
        setStatus('signed-out')
      })
    return () => {
      cancelled = true
    }
  }, [])

  const login = useCallback(async (password: string) => {
    setError(null)
    try {
      await api.post('/api/auth/login', { password })
      const session = await api.get<SessionUser>('/api/auth/me')
      setUser(session)
      setStatus(session.authenticated ? 'signed-in' : 'signed-out')
    } catch (cause) {
      const message =
        cause instanceof ApiError
          ? cause.status === 401 || cause.status === 403
            ? 'That password is incorrect.'
            : cause.message
          : 'Sign-in failed.'
      setError(message)
      throw cause
    }
  }, [])

  const logout = useCallback(async () => {
    try {
      await api.post('/api/auth/logout')
    } finally {
      setUser(null)
      setStatus('signed-out')
    }
  }, [])

  const value = useMemo(
    () => ({ status, user, error, login, logout }),
    [status, user, error, login, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
