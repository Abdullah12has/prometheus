import { useEffect, useState, type ReactNode } from 'react'
import {
  Building2,
  TrendingUp,
  AlertCircle,
  ShieldCheck,
  Users,
  Activity,
  Plus,
  Compass,
  GitCompareArrows,
  Send,
  Mic,
  ChevronRight,
} from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { Company, CompanyListResponse, DashboardSummary, SellerIntent } from '../lib/types'
import { LoadingBlock, ErrorBlock, EmptyState } from '../components/StateViews'
import { SellerIntentBadge } from '../components/SellerIntentBadge'
import { Link } from '../lib/router'

const INTENT_LABELS: [SellerIntent, string][] = [
  ['interested', 'Interested'],
  ['conditional', 'Conditional'],
  ['not_now', 'Not now'],
  ['not_interested', 'Not interested'],
  ['unknown', 'Unknown'],
]

const SHORTCUTS = [
  { to: '/futures', icon: Compass, title: 'Owner conditions', text: 'Record and confirm what an owner needs before a sale.' },
  { to: '/matches', icon: GitCompareArrows, title: 'Match mandates', text: 'Run buyer mandates against confirmed conditions.' },
  { to: '/outreach', icon: Send, title: 'Review outreach', text: 'Approve drafts and triage replies by hand.' },
  { to: '/voice-notes', icon: Mic, title: 'Capture a meeting', text: 'Record notes or hold a browser conversation.' },
]

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

  const intentTotal = data ? INTENT_LABELS.reduce((sum, [key]) => sum + (data.companies_by_seller_intent?.[key] ?? 0), 0) : 0
  const jobs = data ? Object.entries(data.jobs_by_state ?? {}).filter(([, count]) => count > 0) : []

  return (
    <div className="page">
      <header className="page__header page__header--row">
        <div>
          <h1>Overview</h1>
          <p className="page__lede">Where your origination pipeline stands right now.</p>
        </div>
        <div className="page__actions">
          <Link to="/companies" className="btn btn--primary">
            <Plus size={16} aria-hidden="true" />
            Add a company
          </Link>
        </div>
      </header>

      {status === 'loading' && <LoadingBlock label="Loading overview…" />}
      {status === 'error' && <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={load} />}

      {status === 'ready' && data && (
        <>
          <div className="stat-grid">
            <StatCard
              icon={<Building2 size={16} aria-hidden="true" />}
              label="Companies tracked"
              value={data.companies}
            />
            <StatCard
              icon={<AlertCircle size={16} aria-hidden="true" />}
              label="Needing review"
              value={data.companies_by_status.provisional}
            />
            <StatCard
              icon={<ShieldCheck size={16} aria-hidden="true" />}
              label="Identity confirmed"
              value={data.companies_by_status.confirmed}
            />
            <StatCard
              icon={<TrendingUp size={16} aria-hidden="true" />}
              label="Seller intent confirmed"
              value={data.companies_by_seller_intent?.interested}
            />
            <StatCard
              icon={<Users size={16} aria-hidden="true" />}
              label="People recorded"
              value={data.contacts}
            />
            <StatCard
              icon={<Activity size={16} aria-hidden="true" />}
              label="Activity, last 7 days"
              value={data.activities_last_7_days}
            />
          </div>

          <div className="overview-grid">
            <section className="panel" aria-labelledby="recent-title">
              <div className="section-title">
                <h2 id="recent-title">Recently added</h2>
                {recentCompanies.length > 0 && <Link to="/companies">View all</Link>}
              </div>
              {recentCompanies.length > 0 ? (
                <div className="company-table-wrap">
                  <table className="company-table">
                    <thead>
                      <tr>
                        <th scope="col">Company</th>
                        <th scope="col" className="company-table__col--optional">Country</th>
                        <th scope="col">Seller intent</th>
                      </tr>
                    </thead>
                    <tbody>
                      {recentCompanies.map((company) => (
                        <tr key={company.id} className="company-table__row">
                          <td>
                            <Link to={`/companies/${company.id}`} className="company-table__name-link">
                              {company.name || 'Unnamed company'}
                            </Link>
                            {company.industry && <div className="muted small">{company.industry}</div>}
                          </td>
                          <td className="company-table__col--optional">{company.country ?? 'Country unknown'}</td>
                          <td><SellerIntentBadge intent={company.seller_intent ?? 'unknown'} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState
                  icon={<Building2 size={22} aria-hidden="true" />}
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

            <div className="overview-side">
              <section className="panel" aria-labelledby="intent-title">
                <h2 id="intent-title">Seller intent</h2>
                {intentTotal > 0 ? (
                  <ul className="bar-list">
                    {INTENT_LABELS.map(([key, label]) => {
                      const count = data.companies_by_seller_intent?.[key] ?? 0
                      return (
                        <li key={key}>
                          <span>{label}</span>
                          <span className="bar-list__value">{count}</span>
                          <span className="bar-list__track" aria-hidden="true">
                            <span className="bar-list__fill" style={{ width: `${(count / intentTotal) * 100}%` }} />
                          </span>
                        </li>
                      )
                    })}
                  </ul>
                ) : (
                  <p className="muted">No seller intent recorded yet.</p>
                )}
                {jobs.length > 0 && (
                  <p className="muted small">
                    Background research: {jobs.map(([state, count]) => `${count} ${state.replace(/_/g, ' ')}`).join(' · ')}
                  </p>
                )}
              </section>

              <section className="panel" aria-labelledby="shortcut-title">
                <h2 id="shortcut-title">Continue working</h2>
                <ul className="shortcut-list">
                  {SHORTCUTS.map(({ to, icon: Icon, title, text }) => (
                    <li key={to}>
                      <Link to={to}>
                        <Icon size={16} aria-hidden="true" />
                        <div>
                          <strong>{title}</strong>
                          <span>{text}</span>
                        </div>
                        <ChevronRight size={16} aria-hidden="true" />
                      </Link>
                    </li>
                  ))}
                </ul>
              </section>
            </div>
          </div>
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
