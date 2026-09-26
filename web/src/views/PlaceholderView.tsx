import type { ReactNode } from 'react'
import { EmptyState } from '../components/StateViews'

/**
 * Shared shell for views whose backend endpoints don't exist yet. Each of
 * these names the API this view expects (see API_CONTRACT.md) rather than
 * showing populated or scripted data.
 */
export function PlaceholderView({
  title,
  lede,
  icon,
  emptyTitle,
  emptyDescription,
  expectedApi,
}: {
  title: string
  lede: string
  icon: ReactNode
  emptyTitle: string
  emptyDescription: string
  expectedApi: string
}) {
  return (
    <div className="page">
      <header className="page__header">
        <h1>{title}</h1>
        <p className="page__lede">{lede}</p>
      </header>

      <EmptyState icon={icon} title={emptyTitle} description={emptyDescription} />

      <p className="placeholder-note">
        Expects <code>{expectedApi}</code> from the backend. Not built yet — see{' '}
        <code>web/API_CONTRACT.md</code>.
      </p>
    </div>
  )
}
