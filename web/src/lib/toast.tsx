import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { CheckCircle2, AlertTriangle, XCircle, X } from 'lucide-react'

type ToastVariant = 'success' | 'error' | 'info'

interface Toast {
  id: number
  variant: ToastVariant
  message: string
}

interface ToastState {
  push: (message: string, variant?: ToastVariant) => void
}

const ToastContext = createContext<ToastState | undefined>(undefined)

const ICONS: Record<ToastVariant, typeof CheckCircle2> = {
  success: CheckCircle2,
  error: XCircle,
  info: AlertTriangle,
}

const DURATION_MS = 5000

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const idRef = useRef(0)

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id))
  }, [])

  const push = useCallback(
    (message: string, variant: ToastVariant = 'info') => {
      const id = ++idRef.current
      setToasts((current) => [...current, { id, variant, message }])
      window.setTimeout(() => dismiss(id), DURATION_MS)
    },
    [dismiss],
  )

  const value = useMemo(() => ({ push }), [push])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map((toast) => {
          const Icon = ICONS[toast.variant]
          return (
            <div key={toast.id} className={`toast toast--${toast.variant}`}>
              <Icon size={18} aria-hidden="true" />
              <span>{toast.message}</span>
              <button
                type="button"
                className="toast__dismiss"
                aria-label="Dismiss notification"
                onClick={() => dismiss(toast.id)}
              >
                <X size={14} aria-hidden="true" />
              </button>
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastState {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used within a ToastProvider')
  return ctx
}
