// Buyer directory list + discovery drawer + quick-add dialog.
//
// Routes used (backend/permetheus buyers module, exact — nothing guessed):
//   GET  /api/buyers?q=&country=&kind=&offset=0&limit=50
//   POST /api/buyers
//   GET  /api/buyers/discovery/sources
//   GET  /api/buyers/discovery/runs
//   POST /api/buyers/discovery/runs
//   POST /api/buyers/discovery/runs/{id}/pause
//   POST /api/buyers/discovery/runs/{id}/resume
//   GET  /api/buyers/summary

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Plus, Search, Compass, X, Pause, Play, RefreshCw, BriefcaseBusiness } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import {
  BUYER_COUNTRIES, BUYER_KIND_LABELS, BUYER_RESEARCH_STATUS_LABELS, BUYER_STATUS_LABELS,
  COUNTRY_LABELS, NORDIC_COUNTRIES, OTHER_COUNTRIES,
  type Buyer, type BuyerDraft, type BuyerKind, type BuyerListResponse,
  type BuyersSummary, type DiscoveryRun, type DiscoverySource,
} from '../lib/buyer-types'
import { LoadingBlock, ErrorBlock, EmptyState } from '../components/StateViews'
import { useToast } from '../lib/toast'
import { Link, useRouter } from '../lib/router'
import './buyers.css'

const PAGE_SIZE = 50
const KIND_OPTIONS: BuyerKind[] = ['private_equity', 'family_office', 'holding_company', 'unknown']

function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const handle = window.setTimeout(() => setDebounced(value), delayMs)
    return () => window.clearTimeout(handle)
  }, [value, delayMs])
  return debounced
}

function errorText(cause: unknown, fallback: string): string {
  if (cause instanceof ApiError) {
    return `${cause.message}${cause.detail ? ` — ${typeof cause.detail === 'string' ? cause.detail : JSON.stringify(cause.detail)}` : ''}`
  }
  return cause instanceof Error ? cause.message : fallback
}

function BuyerKindBadge({ kind }: { kind: BuyerKind }) {
  return <span className={`buyer-badge buyer-badge--kind-${kind}`}>{BUYER_KIND_LABELS[kind]}</span>
}

function BuyerResearchBadge({ status }: { status: Buyer['research_status'] }) {
  return <span className={`buyer-badge buyer-badge--research-${status}`}>{BUYER_RESEARCH_STATUS_LABELS[status]}</span>
}

function BuyerStatusBadge({ status }: { status: Buyer['status'] }) {
  return <span className={`buyer-badge buyer-badge--status-${status}`}>{BUYER_STATUS_LABELS[status]}</span>
}

function useNativeDialog() {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current
    const previous = document.activeElement
    dialog?.showModal()
    return () => { dialog?.close(); if (previous instanceof HTMLElement) previous.focus() }
  }, [])
  return ref
}

// ---------------------------------------------------------------- Add dialog

function AddBuyerDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (buyer: Buyer) => void }) {
  const [name, setName] = useState('')
  const [website, setWebsite] = useState('')
  const [country, setCountry] = useState('')
  const [kind, setKind] = useState<BuyerKind>('unknown')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const dialogRef = useNativeDialog()
  const { push } = useToast()

  useEffect(() => { inputRef.current?.focus() }, [])

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const trimmed = name.trim()
    if (!trimmed) { setError('Enter the buyer name.'); return }
    const draft: BuyerDraft = { name: trimmed, website: website.trim(), country }
    if (website.trim()) draft.website = website.trim()
    if (country) draft.country = country
    if (kind) draft.kind = kind
    setSubmitting(true)
    setError(null)
    try {
      const created = await api.post<Buyer>('/api/buyers', draft)
      push(`Added ${created.name} — research queued`, 'success')
      onCreated(created)
    } catch (cause) {
      setError(errorText(cause, 'Could not add this buyer.'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <dialog ref={dialogRef} onCancel={onClose} className="dialog-overlay buyer-modal" aria-labelledby="add-buyer-title" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="dialog">
        <div className="dialog__header">
          <h2 id="add-buyer-title">Add a buyer</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close"><X size={18} aria-hidden="true" /></button>
        </div>
        <form onSubmit={handleSubmit} className="dialog__body">
          <label htmlFor="buyer-name">Name</label>
          <input id="buyer-name" ref={inputRef} required value={name} onChange={(event) => setName(event.target.value)} disabled={submitting} placeholder="Nordic Capital" />

          <label htmlFor="buyer-website">Website</label>
          <input id="buyer-website" type="url" required value={website} onChange={(event) => setWebsite(event.target.value)} disabled={submitting} placeholder="https://…" />

          <div className="field-row">
            <div className="field-col">
              <label htmlFor="buyer-country">Region</label>
              <select id="buyer-country" required value={country} onChange={(event) => setCountry(event.target.value)} disabled={submitting}>
                <option value="">Choose region</option>
                <optgroup label="Nordics">
                  {NORDIC_COUNTRIES.map((code) => <option key={code} value={code}>{COUNTRY_LABELS[code]}</option>)}
                </optgroup>
                <optgroup label="Other">
                  {OTHER_COUNTRIES.map((code) => <option key={code} value={code}>{COUNTRY_LABELS[code]}</option>)}
                </optgroup>
              </select>
            </div>
            <div className="field-col">
              <label htmlFor="buyer-kind">Kind</label>
              <select id="buyer-kind" value={kind} onChange={(event) => setKind(event.target.value as BuyerKind)} disabled={submitting}>
                {KIND_OPTIONS.map((value) => <option key={value} value={value}>{BUYER_KIND_LABELS[value]}</option>)}
              </select>
            </div>
          </div>
          <p className="field-hint">Adding a buyer queues background research from public sources; nothing here is confirmed yet.</p>

          {error && <p className="field-error" role="alert">{error}</p>}

          <div className="dialog__actions">
            <button type="button" className="btn btn--ghost" onClick={onClose} disabled={submitting}>Cancel</button>
            <button type="submit" className="btn btn--primary" disabled={submitting}>{submitting ? 'Adding…' : 'Add buyer'}</button>
          </div>
        </form>
      </div>
    </dialog>
  )
}

// ------------------------------------------------------------- Discovery drawer

function progressPct(run: DiscoveryRun): number | null {
  if (!run.source_total) return null
  return Math.round(Math.min(1, Math.max(0, run.source_index / run.source_total)) * 100)
}

function DiscoveryDrawer({ onClose }: { onClose: () => void }) {
  const dialogRef = useNativeDialog()
  const [sources, setSources] = useState<DiscoverySource[] | null>(null)
  const [sourcesError, setSourcesError] = useState<string | null>(null)
  const [runs, setRuns] = useState<DiscoveryRun[] | null>(null)
  const [runsError, setRunsError] = useState<string | null>(null)
  const [summary, setSummary] = useState<BuyersSummary | null>(null)
  const [summaryError, setSummaryError] = useState<string | null>(null)
  const [selectedCountries, setSelectedCountries] = useState<Set<string>>(new Set())
  const [starting, setStarting] = useState(false)
  const [busyRunId, setBusyRunId] = useState<string | null>(null)
  const [startError, setStartError] = useState<string | null>(null)
  const { push } = useToast()

  const refreshRuns = useCallback(() => {
    api.get<DiscoveryRun[]>('/api/buyers/discovery/runs')
      .then((items) => { setRuns(items); setRunsError(null) })
      .catch((cause) => setRunsError(errorText(cause, 'Could not load discovery runs.')))
  }, [])
  const refreshSummary = useCallback(() => {
    api.get<BuyersSummary>('/api/buyers/summary')
      .then((value) => { setSummary(value); setSummaryError(null) })
      .catch((cause) => setSummaryError(errorText(cause, 'Could not load research totals.')))
  }, [])

  useEffect(() => {
    api.get<DiscoverySource[]>('/api/buyers/discovery/sources')
      .then((items) => { setSources(items); setSourcesError(null) })
      .catch((cause) => setSourcesError(errorText(cause, 'Could not load discovery sources.')))
    refreshRuns()
    refreshSummary()
  }, [refreshRuns, refreshSummary])

  const activeRun = useMemo(() => (runs ?? []).some((run) => ['queued', 'running'].includes(run.status)), [runs])
  useEffect(() => {
    if (!activeRun) return
    const timer = window.setInterval(() => { refreshRuns(); refreshSummary() }, 3000)
    return () => window.clearInterval(timer)
  }, [activeRun, refreshRuns, refreshSummary])


  function toggleCountry(code: string) {
    setSelectedCountries((current) => {
      const next = new Set(current)
      if (next.has(code)) next.delete(code); else next.add(code)
      return next
    })
  }

  async function runDiscovery() {
    if (selectedCountries.size === 0) { setStartError('Choose at least one country.'); return }
    setStarting(true)
    setStartError(null)
    try {
      await api.post('/api/buyers/discovery/runs', { countries: Array.from(selectedCountries) })
      push('Discovery run queued', 'success')
      refreshRuns()
    } catch (cause) {
      setStartError(errorText(cause, 'Could not start discovery.'))
    } finally {
      setStarting(false)
    }
  }

  async function pauseOrResume(run: DiscoveryRun, action: 'pause' | 'resume') {
    setBusyRunId(run.id)
    try {
      await api.post(`/api/buyers/discovery/runs/${run.id}/${action}`)
      refreshRuns()
    } catch (cause) {
      push(errorText(cause, `Could not ${action} this run.`), 'error')
    } finally {
      setBusyRunId(null)
    }
  }

  const sourcesByCountry = useMemo(() => {
    const grouped = new Map<string, DiscoverySource[]>()
    for (const source of sources ?? []) {
      const list = grouped.get(source.country) ?? []
      list.push(source)
      grouped.set(source.country, list)
    }
    return grouped
  }, [sources])

  return (
    <dialog ref={dialogRef} onCancel={onClose} className="drawer-overlay buyer-modal" aria-labelledby="discovery-drawer-title" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <aside className="drawer">
        <div className="drawer__header">
          <div><h2 id="discovery-drawer-title">Buyer discovery</h2><p className="page__lede">Find candidate buyers from public directories by country, then queue research.</p></div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close discovery drawer"><X size={18} aria-hidden="true" /></button>
        </div>
        <div className="drawer__body">
          <section className="panel">
            <h3>Choose region</h3>
            <fieldset className="buyer-country-picker">
              <legend className="sr-only">Countries to discover</legend>
              <div className="buyer-country-group">
                <span className="buyer-country-group__label">Nordics</span>
                {NORDIC_COUNTRIES.map((code) => (
                  <label key={code} className="checkbox-label buyer-country-option">
                    <input type="checkbox" checked={selectedCountries.has(code)} onChange={() => toggleCountry(code)} />
                    {COUNTRY_LABELS[code]}
                  </label>
                ))}
              </div>
              <div className="buyer-country-group">
                <span className="buyer-country-group__label">Other</span>
                {OTHER_COUNTRIES.map((code) => (
                  <label key={code} className="checkbox-label buyer-country-option">
                    <input type="checkbox" checked={selectedCountries.has(code)} onChange={() => toggleCountry(code)} />
                    {COUNTRY_LABELS[code]}
                  </label>
                ))}
              </div>
            </fieldset>
            {startError && <p className="field-error" role="alert">{startError}</p>}
            <button type="button" className="btn btn--primary" onClick={runDiscovery} disabled={starting}>
              <Compass size={14} aria-hidden="true" /> {starting ? 'Starting…' : 'Run discovery'}
            </button>
          </section>

          <section className="panel">
            <h3>Research totals</h3>
            {summaryError && <ErrorBlock message={summaryError} onRetry={refreshSummary} />}
            {!summaryError && !summary && <LoadingBlock label="Loading totals…" />}
            {summary && (
              <>
                <dl className="buyer-summary-grid">
                  <div><dt>Buyers tracked</dt><dd>{summary.total.toLocaleString()}</dd></div>
                  <div><dt>Queued</dt><dd>{summary.research_jobs.queued.toLocaleString()}</dd></div>
                  <div><dt>Running</dt><dd>{summary.research_jobs.running.toLocaleString()}</dd></div>
                  <div><dt>Researched</dt><dd>{summary.research_jobs.succeeded.toLocaleString()}</dd></div>
                  <div><dt>Failed</dt><dd>{summary.research_jobs.failed.toLocaleString()}</dd></div>
                </dl>
                <p className="muted small buyer-coverage-note">{summary.coverage}</p>
              </>
            )}
          </section>

          <section className="panel">
            <h3>Public sources by country</h3>
            {sourcesError && <ErrorBlock message={sourcesError} />}
            {!sourcesError && !sources && <LoadingBlock label="Loading sources…" />}
            {sources && sources.length === 0 && <p className="muted small">No public source list configured yet.</p>}
            {sources && sources.length > 0 && (
              <ul className="buyer-source-list">
                {BUYER_COUNTRIES.filter((code) => sourcesByCountry.has(code)).map((code) => (
                  <li key={code}>
                    <strong>{COUNTRY_LABELS[code]}</strong>
                    <ul>
                      {(sourcesByCountry.get(code) ?? []).map((source) => (
                        <li key={source.id}>
                          <a href={source.url} target="_blank" rel="noreferrer">{source.label}</a>
                          <span className="muted small"> — {source.coverage}</span>
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="panel">
            <div className="panel__row"><h3>Recent runs</h3><button type="button" className="btn btn--ghost" onClick={() => { refreshRuns(); refreshSummary() }}><RefreshCw size={14} aria-hidden="true" /> Refresh</button></div>
            {runsError && <ErrorBlock message={runsError} onRetry={refreshRuns} />}
            {!runsError && !runs && <LoadingBlock label="Loading runs…" />}
            {runs && runs.length === 0 && <p className="muted small">No discovery runs yet.</p>}
            {runs && runs.length > 0 && (
              <ul className="buyer-run-list">
                {runs.map((run) => {
                  const pct = progressPct(run)
                  const busy = busyRunId === run.id
                  return (
                    <li key={run.id} className="buyer-run">
                      <div className="buyer-run__head">
                        <span className={`buyer-run__status buyer-run__status--${run.status}`}>{run.status}</span>
                        <span className="muted small">{run.countries.join(', ')}</span>
                      </div>
                      <p className="muted small">
                        Source {Math.min(run.source_index, run.source_total)} of {run.source_total || '—'} · {run.found.toLocaleString()} found ·{' '}
                        {run.created.toLocaleString()} added · {run.matched.toLocaleString()} matched existing
                      </p>
                      {pct !== null && (
                        <div className="buyer-run__progress" role="progressbar" aria-label="Discovery run progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
                          <span style={{ width: `${pct}%` }} />
                        </div>
                      )}
                      {run.errors.length > 0 && (
                        <div className="buyer-run__errors">
                          {run.errors.slice(-3).map((message, index) => <p key={index}>{message}</p>)}
                        </div>
                      )}
                      <div className="buyer-run__actions">
                        {['queued', 'running'].includes(run.status) && (
                          <button type="button" className="btn btn--secondary" onClick={() => pauseOrResume(run, 'pause')} disabled={busy}><Pause size={13} aria-hidden="true" /> Pause</button>
                        )}
                        {(run.status === 'paused' || run.status === 'failed') && (
                          <button type="button" className="btn btn--secondary" onClick={() => pauseOrResume(run, 'resume')} disabled={busy}><Play size={13} aria-hidden="true" /> Resume</button>
                        )}
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        </div>
      </aside>
    </dialog>
  )
}

// -------------------------------------------------------------------- Main view

export function BuyersView() {
  const [buyers, setBuyers] = useState<Buyer[] | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [country, setCountry] = useState('')
  const [kind, setKind] = useState('')
  const [offset, setOffset] = useState(0)
  const [total, setTotal] = useState(0)
  const [showAdd, setShowAdd] = useState(false)
  const [showDiscovery, setShowDiscovery] = useState(false)
  const requestId = useRef(0)
  const debouncedQuery = useDebounced(query, 250)
  const { navigate } = useRouter()

  const load = useCallback(() => {
    const id = ++requestId.current
    setStatus('loading')
    setError(null)
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) })
    if (debouncedQuery) params.set('q', debouncedQuery)
    if (country) params.set('country', country)
    if (kind) params.set('kind', kind)
    api.get<BuyerListResponse>(`/api/buyers?${params}`)
      .then((response) => {
        if (id !== requestId.current) return
        setBuyers(response.items)
        setTotal(response.total)
        setStatus('ready')
      })
      .catch((cause) => {
        if (id !== requestId.current) return
        setError(errorText(cause, 'Could not load buyers.'))
        setStatus('error')
      })
  }, [country, kind, offset, debouncedQuery])

  useEffect(() => {
    load()
    return () => { requestId.current++ }
  }, [load])

  const isEmptySearch = useMemo(
    () => status === 'ready' && buyers !== null && buyers.length === 0 && (debouncedQuery.length > 0 || country || kind),
    [status, buyers, debouncedQuery, country, kind],
  )
  const isEmptyOverall = useMemo(
    () => status === 'ready' && buyers !== null && buyers.length === 0 && debouncedQuery.length === 0 && !country && !kind,
    [status, buyers, debouncedQuery, country, kind],
  )

  return (
    <div className="page">
      <header className="page__header page__header--row">
        <div>
          <h1>Buyers</h1>
          <p className="page__lede">Directory of candidate acquirers built from public sources — nothing here is buyer-confirmed by default.</p>
        </div>
        <div className="page__actions">
          <button type="button" className="btn btn--secondary" onClick={() => setShowDiscovery(true)}>
            <Compass size={16} aria-hidden="true" /> Discovery
          </button>
          <button type="button" className="btn btn--primary" onClick={() => setShowAdd(true)}>
            <Plus size={16} aria-hidden="true" /> Add buyer
          </button>
        </div>
      </header>

      <div className="company-toolbar">
        <div className="search-field">
          <Search size={16} aria-hidden="true" />
          <input
            type="search"
            maxLength={200}
            placeholder="Search by name, strategy or sector"
            value={query}
            onChange={(event) => { setQuery(event.target.value); setOffset(0) }}
            aria-label="Search buyers"
          />
        </div>
        <select aria-label="Filter buyers by country" value={country} onChange={(event) => { setCountry(event.target.value); setOffset(0) }}>
          <option value="">All countries</option>
          <optgroup label="Nordics">
            {NORDIC_COUNTRIES.map((code) => <option key={code} value={code}>{COUNTRY_LABELS[code]}</option>)}
          </optgroup>
          <optgroup label="Other">
            {OTHER_COUNTRIES.map((code) => <option key={code} value={code}>{COUNTRY_LABELS[code]}</option>)}
          </optgroup>
        </select>
        <select aria-label="Filter buyers by kind" value={kind} onChange={(event) => { setKind(event.target.value); setOffset(0) }}>
          <option value="">All kinds</option>
          {KIND_OPTIONS.map((value) => <option key={value} value={value}>{BUYER_KIND_LABELS[value]}</option>)}
        </select>
        <button type="button" className="btn btn--secondary" onClick={load} disabled={status === 'loading'}>Refresh</button>
      </div>

      {status === 'loading' && <LoadingBlock label="Loading buyers…" />}
      {status === 'error' && <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={load} />}

      {isEmptyOverall && (
        <EmptyState
          icon={<BriefcaseBusiness size={22} aria-hidden="true" />}
          title="No buyers yet"
          description="Run discovery against public sources, or add a buyer by name."
          action={<button type="button" className="btn btn--primary" onClick={() => setShowAdd(true)}>Add buyer</button>}
        />
      )}

      {status === 'ready' && total > 0 && (
        <nav className="company-pagination" aria-label="Buyer pages">
          <span className="muted small">{(offset + 1).toLocaleString()}–{Math.min(offset + PAGE_SIZE, total).toLocaleString()} of {total.toLocaleString()} buyers</span>
          <div>
            <button type="button" className="btn btn--secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
            <button type="button" className="btn btn--secondary" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button>
          </div>
        </nav>
      )}

      {isEmptySearch && (
        <EmptyState
          icon={<Search size={22} aria-hidden="true" />}
          title="No matches"
          description="Nothing matches these filters. Try a different search term or clear a filter."
        />
      )}

      {status === 'ready' && buyers && buyers.length > 0 && (
        <div className="company-table-wrap">
          <table className="company-table">
            <thead>
              <tr>
                <th scope="col">Buyer</th>
                <th scope="col">Region</th>
                <th scope="col">Kind</th>
                <th scope="col" className="company-table__col--optional">Status</th>
                <th scope="col">Research</th>
                <th scope="col" className="company-table__col--optional">Sources / history</th>
              </tr>
            </thead>
            <tbody>
              {buyers.map((buyer) => (
                <tr
                  key={buyer.id}
                  className="company-table__row buyer-table__row"
                  tabIndex={0}
                  onClick={() => navigate(`/buyers/${buyer.id}`)}
                  onKeyDown={(event) => { if (event.key === 'Enter') navigate(`/buyers/${buyer.id}`) }}
                >
                  <td>
                    <Link to={`/buyers/${buyer.id}`} className="company-table__name-link">
                      {buyer.name || 'Unnamed buyer'}
                    </Link>
                    {buyer.website && <div className="muted small">{buyer.website}</div>}
                  </td>
                  <td>{buyer.country ?? '—'}</td>
                  <td><BuyerKindBadge kind={buyer.kind} /></td>
                  <td className="company-table__col--optional"><BuyerStatusBadge status={buyer.status} /></td>
                  <td><BuyerResearchBadge status={buyer.research_status} /></td>
                  <td className="company-table__col--optional muted small">{buyer.source_count.toLocaleString()} sources · {buyer.history_count.toLocaleString()} history</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showAdd && (
        <AddBuyerDialog
          onClose={() => setShowAdd(false)}
          onCreated={(buyer) => { setShowAdd(false); navigate(`/buyers/${buyer.id}`) }}
        />
      )}
      {showDiscovery && <DiscoveryDrawer onClose={() => setShowDiscovery(false)} />}
    </div>
  )
}
