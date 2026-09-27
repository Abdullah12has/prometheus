import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Plus, Search, Building2 } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { Company, CompanyListResponse } from '../lib/types'
import { LoadingBlock, ErrorBlock, EmptyState } from '../components/StateViews'
import { DiscoveryPanel } from '../components/DiscoveryPanel'
import { CompanyIntakeDialog } from '../components/CompanyIntakeDialog'
import { SellerIntentBadge } from '../components/SellerIntentBadge'
import { Link, useRouter } from '../lib/router'

function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const handle = window.setTimeout(() => setDebounced(value), delayMs)
    return () => window.clearTimeout(handle)
  }, [value, delayMs])
  return debounced
}

export function CompaniesView() {
  const [companies, setCompanies] = useState<Company[] | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [country, setCountry] = useState('')
  const [offset, setOffset] = useState(0)
  const [total, setTotal] = useState(0)
  const requestId = useRef(0)
  const [showIntake, setShowIntake] = useState(false)
  const debouncedQuery = useDebounced(query, 250)
  const { navigate } = useRouter()

  const load = useCallback(() => {
    const id = ++requestId.current
    setStatus('loading')
    setError(null)
    const params = new URLSearchParams({ limit: '50', offset: String(offset) })
    if (debouncedQuery) params.set('q', debouncedQuery)
    if (country) params.set('country', country)
    api
      .get<CompanyListResponse>(`/api/companies?${params}`)
      .then((response) => {
        if (id !== requestId.current) return
        setCompanies(response.items)
        setTotal(response.total)
        setStatus('ready')
      })
      .catch((cause) => {
        if (id !== requestId.current) return
        setError(cause instanceof ApiError ? cause.message : 'Could not load companies.')
        setStatus('error')
      })
  }, [country, offset, debouncedQuery])

  useEffect(() => {
    load()
    return () => { requestId.current++ }
  }, [load])

  const isEmptySearch = useMemo(
    () => status === 'ready' && companies !== null && companies.length === 0 && debouncedQuery.length > 0,
    [status, companies, debouncedQuery],
  )
  const isEmptyOverall = useMemo(
    () => status === 'ready' && companies !== null && companies.length === 0 && debouncedQuery.length === 0,
    [status, companies, debouncedQuery],
  )

  return (
    <div className="page">
      <header className="page__header page__header--row">
        <div>
          <h1>Companies</h1>
          <p className="page__lede">Search the pipeline or add a new target.</p>
        </div>
        <button type="button" className="btn btn--primary" onClick={() => setShowIntake(true)}>
          <Plus size={16} aria-hidden="true" />
          Add company
        </button>
      </header>

      <div className="company-toolbar"><div className="search-field">
        <Search size={16} aria-hidden="true" />
        <input
          type="search"
          maxLength={200}
          placeholder="Search by name, website or business ID"
          value={query}
          onChange={(event) => { setQuery(event.target.value); setOffset(0) }}
          aria-label="Search companies"
        />
      </div>
      <select aria-label="Filter companies by country" value={country} onChange={(event) => { setCountry(event.target.value); setOffset(0) }}>
        <option value="">All countries</option><option value="FI">Finland</option><option value="CH">Switzerland</option><option value="DE">Germany</option>
      </select>
      <button type="button" className="btn btn--secondary" onClick={load} disabled={status === 'loading'}>Refresh</button>
      </div>

      <details className="panel"><summary>Discover and research companies</summary><DiscoveryPanel /></details>

      {status === 'loading' && <LoadingBlock label="Loading companies…" />}
      {status === 'error' && (
        <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={load} />
      )}

      {isEmptyOverall && (
        <EmptyState
          icon={<Building2 size={22} aria-hidden="true" />}
          title={country ? 'No companies in this country yet' : 'No companies yet'}
          description="Import public registry records above, or add a company by name or website."
          action={
            <button type="button" className="btn btn--primary" onClick={() => setShowIntake(true)}>
              Add company
            </button>
          }
        />
      )}

      {status === 'ready' && total > 0 && <nav className="company-pagination" aria-label="Company pages">
        <span className="muted small">{(offset + 1).toLocaleString()}–{Math.min(offset + 50, total).toLocaleString()} of {total.toLocaleString()} companies</span>
        <div><button type="button" className="btn btn--secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</button>
        <button type="button" className="btn btn--secondary" disabled={offset + 50 >= total} onClick={() => setOffset(offset + 50)}>Next</button></div>
      </nav>}

      {isEmptySearch && (
        <EmptyState
          icon={<Search size={22} aria-hidden="true" />}
          title="No matches"
          description={`Nothing matches "${debouncedQuery}". Try a different name or website.`}
        />
      )}

      {status === 'ready' && companies && companies.length > 0 && (
        <div className="company-table-wrap">
        <table className="company-table">
          <thead>
            <tr>
              <th scope="col">Company</th>
              <th scope="col">Country</th>
              <th scope="col" className="company-table__col--optional">Industry</th>
              <th scope="col">Seller intent</th>
            </tr>
          </thead>
          <tbody>
            {companies.map((company) => (
              <tr key={company.id} className="company-table__row">
                <td>
                  <Link to={`/companies/${company.id}`} className="company-table__name-link">
                    {company.name || 'Unnamed company'}
                  </Link>
                  {company.website && <div className="muted small">{company.website}</div>}
                </td>
                <td>{company.country ?? '—'}</td>
                <td className="company-table__col--optional">{company.industry ?? '—'}</td>
                <td>
                  <SellerIntentBadge intent={company.seller_intent ?? 'unknown'} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}

      {showIntake && (
        <CompanyIntakeDialog
          onClose={() => setShowIntake(false)}
          onCreated={(company) => {
            setShowIntake(false)
            navigate(`/companies/${company.id}`)
          }}
        />
      )}
    </div>
  )
}
