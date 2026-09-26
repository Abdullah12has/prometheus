import { useEffect, useMemo, useState } from 'react'
import { Plus, Search, Building2 } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { Company, CompanyListResponse } from '../lib/types'
import { LoadingBlock, ErrorBlock, EmptyState } from '../components/StateViews'
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
  const [showIntake, setShowIntake] = useState(false)
  const debouncedQuery = useDebounced(query, 250)
  const { navigate } = useRouter()

  function load(search: string) {
    setStatus('loading')
    setError(null)
    const path = search ? `/api/companies?q=${encodeURIComponent(search)}` : '/api/companies'
    api
      .get<CompanyListResponse | Company[]>(path)
      .then((response) => {
        const list = Array.isArray(response) ? response : response.items
        setCompanies(list)
        setStatus('ready')
      })
      .catch((cause) => {
        setError(cause instanceof ApiError ? cause.message : 'Could not load companies.')
        setStatus('error')
      })
  }

  useEffect(() => {
    load(debouncedQuery)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedQuery])

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

      <div className="search-field">
        <Search size={16} aria-hidden="true" />
        <input
          type="search"
          placeholder="Search by name, website or business ID"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label="Search companies"
        />
      </div>

      {status === 'loading' && <LoadingBlock label="Loading companies…" />}
      {status === 'error' && (
        <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={() => load(debouncedQuery)} />
      )}

      {isEmptyOverall && (
        <EmptyState
          icon={<Building2 size={28} aria-hidden="true" />}
          title="No companies yet"
          description="Add a company by name or website to start the pipeline."
          action={
            <button type="button" className="btn btn--primary" onClick={() => setShowIntake(true)}>
              Add company
            </button>
          }
        />
      )}

      {isEmptySearch && (
        <EmptyState
          title="No matches"
          description={`Nothing matches "${debouncedQuery}". Try a different name or website.`}
        />
      )}

      {status === 'ready' && companies && companies.length > 0 && (
        <table className="company-table">
          <thead>
            <tr>
              <th scope="col">Company</th>
              <th scope="col">Country</th>
              <th scope="col">Industry</th>
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
                <td>{company.industry ?? '—'}</td>
                <td>
                  <SellerIntentBadge intent={company.seller_intent ?? 'unknown'} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
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
