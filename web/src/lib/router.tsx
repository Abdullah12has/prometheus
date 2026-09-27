// Minimal history-backed router. Deliberately dependency-free: the app has a
// handful of top-level destinations and doesn't need a routing library.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

interface RouterState {
  path: string
  search: string
  navigate: (path: string) => void
}

const RouterContext = createContext<RouterState | undefined>(undefined)

function normalize(path: string): string {
  if (!path.startsWith('/')) return `/${path}`
  return path
}

export function RouterProvider({ children }: { children: ReactNode }) {
  const [path, setPath] = useState(() => normalize(window.location.pathname + window.location.search))

  useEffect(() => {
    const onPopState = () => setPath(normalize(window.location.pathname + window.location.search))
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const navigate = useCallback((next: string) => {
    const normalized = normalize(next)
    if (normalized === window.location.pathname + window.location.search) return
    window.history.pushState({}, '', normalized)
    setPath(normalized)
  }, [])

  const value = useMemo(() => {
    const url = new URL(path, window.location.origin)
    return { path: url.pathname, search: url.search, navigate }
  }, [path, navigate])

  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>
}

export function useRouter(): RouterState {
  const ctx = useContext(RouterContext)
  if (!ctx) throw new Error('useRouter must be used within a RouterProvider')
  return ctx
}

/** Splits the current path into ['companies', ':id'] style segments. */
export function useSegments(): string[] {
  const { path } = useRouter()
  return useMemo(() => path.split('/').filter(Boolean), [path])
}

export function Link({
  to,
  children,
  className,
  onNavigate,
  ariaCurrent,
}: {
  to: string
  children: ReactNode
  className?: string
  onNavigate?: () => void
  ariaCurrent?: 'page'
}) {
  const { navigate } = useRouter()
  return (
    <a
      href={to}
      className={className}
      aria-current={ariaCurrent}
      onClick={(event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return
        event.preventDefault()
        navigate(to)
        onNavigate?.()
      }}
    >
      {children}
    </a>
  )
}
