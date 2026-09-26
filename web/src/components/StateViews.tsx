import { AlertCircle, Loader2, RefreshCw } from 'lucide-react'
import type { ReactNode } from 'react'

export function LoadingBlock({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="state-block state-block--loading" role="status">
      <Loader2 className="spin" size={20} aria-hidden="true" />
      <span>{label}</span>
    </div>
  )
}

export function ErrorBlock({
  message,
  onRetry,
}: {
  message: string
  onRetry?: () => void
}) {
  return (
    <div className="state-block state-block--error" role="alert">
      <AlertCircle size={20} aria-hidden="true" />
      <div className="state-block__body">
        <p>{message}</p>
        {onRetry && (
          <button type="button" className="btn btn--ghost" onClick={onRetry}>
            <RefreshCw size={14} aria-hidden="true" />
            Try again
          </button>
        )}
      </div>
    </div>
  )
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode
  title: string
  description: string
  action?: ReactNode
}) {
  return (
    <div className="empty-state">
      {icon && <div className="empty-state__icon">{icon}</div>}
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  )
}
