import { useEffect, useState, type ReactNode } from 'react'
import { Building2, TrendingUp, AlertCircle } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { Company, CompanyListResponse, DashboardSummary } from '../lib/types'
import { LoadingBlock, ErrorBlock, EmptyState } from '../components/StateViews'
import { Link } from '../lib/router'

export function OverviewView() {
  const [data, setData] = useState<DashboardSummary | null>(null)
  const [recentCompanies, setRecentCompanies] = useState<Company[]>([])
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  function load() {
    setStatus('loading')
    setError(null)
    Promise.all([
      api.get<DashboardSummary>('/api/dashboard'),
      api.get<CompanyListResponse>('/api/companies?limit=5&offset=0'),
    ]).then(([summary, companies]) => {
        setData(summary)
        setRecentCompanies(companies.items)
        setStatus('ready')
      })
      .catch((cause) => {
        setError(
          cause instanceof ApiError
            ? cause.message
            : 'Could not load the overview.',
        )
        setStatus('error')
      })
  }

  useEffect(load, [])

  return (
    <div className="page">
      <header className="page__header">
        <h1>Overview</h1>
        <p className="page__lede">Where your origination pipeline stands right now.</p>
      </header>

      {status === 'loading' && <LoadingBlock label="Loading overview…" />}
      {status === 'error' && <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={load} />}

      {status === 'ready' && data && (
        <>
          <div className="stat-grid">
            <StatCard
              icon={<Building2 size={18} aria-hidden="true" />}
              label="Companies tracked"
              value={data.companies}
            />
            <StatCard
              icon={<AlertCircle size={18} aria-hidden="true" />}
              label="Needing review"
              value={data.companies_by_status.provisional}
            />
            <StatCard
              icon={<TrendingUp size={18} aria-hidden="true" />}
              label="Seller intent confirmed"
              value={data.companies_by_seller_intent?.interested}
            />
          </div>

          <section className="panel">
            <h2>Recently added</h2>
            {recentCompanies.length > 0 ? (
              <ul className="company-mini-list">
                {recentCompanies.map((company) => (
                  <li key={company.id}>
                    <Link to={`/companies/${company.id}`}>{company.name}</Link>
                    <span className="muted">{company.country ?? 'Country unknown'}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState
                title="No companies yet"
                description="Add your first company to start building the pipeline."
                action={
                  <Link to="/companies" className="btn btn--primary">
                    Add a company
                  </Link>
                }
              />
            )}
          </section>
        </>
      )}
    </div>
  )
}

function StatCard({
  icon,
  label,
  value,
}: {
  icon: ReactNode
  label: string
  value?: number
}) {
  return (
    <div className="stat-card">
      <div className="stat-card__icon">{icon}</div>
      <div>
        <div className="stat-card__value">{value ?? '—'}</div>
        <div className="stat-card__label">{label}</div>
      </div>
    </div>
  )
}
