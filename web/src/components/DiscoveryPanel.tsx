import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, getCsrfToken } from '../lib/api'
import { useRouter } from '../lib/router'
import './research.css'

type DiscoveryRun = { id: string; job_id: string | null; status: string; params: { name?: string | null; business_id?: string | null; registration_start?: string | null; registration_end?: string | null; max_pages?: number; max_companies?: number }; current_page: number; total_results: number | null; companies_seen: number; companies_imported: number; errors: string[]; started_at: string; finished_at: string | null; updated_at: string }
type Job = { id: string; kind: string; state: string; company_id: string | null; last_error: string | null; updated_at: string }
type Schedule = { enabled: boolean; hour_utc: number; window_days: number; last_run_date: string | null }
const humanize = (value: string) => value.replace(/_/g, ' ').replace(/: /g, ' — ')
const errorText = (cause: unknown) => cause instanceof ApiError ? `${cause.message}${cause.detail ? ` — ${typeof cause.detail === 'string' ? cause.detail : JSON.stringify(cause.detail)}` : ''}` : cause instanceof Error ? cause.message : 'Discovery could not be loaded.'

async function putJson<T>(path: string, body: unknown): Promise<T> {
  const headers = new Headers({ Accept: 'application/json', 'Content-Type': 'application/json' })
  const csrf = getCsrfToken()
  if (csrf) headers.set('X-CSRF-Token', csrf)
  let response: Response
  try { response = await fetch(path, { method: 'PUT', credentials: 'include', headers, body: JSON.stringify(body) }) }
  catch (cause) { throw new ApiError('Could not reach the workspace API. Check that the backend is running.', 0, 'network_error', cause) }
  const payload = await response.json().catch(() => undefined) as { error?: { message?: string; code?: string; details?: unknown } } | undefined
  if (!response.ok) throw new ApiError(payload?.error?.message ?? `Request failed with status ${response.status}`, response.status, payload?.error?.code, payload?.error?.details)
  return payload as T
}

export function DiscoveryPanel() {
  const [name, setName] = useState('')
  const [businessId, setBusinessId] = useState('')
  const [registrationStart, setRegistrationStart] = useState('')
  const [registrationEnd, setRegistrationEnd] = useState('')
  const [maxPages, setMaxPages] = useState(3)
  const [maxCompanies, setMaxCompanies] = useState(100)
  const [runs, setRuns] = useState<DiscoveryRun[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [schedule, setSchedule] = useState<Schedule>({ enabled: false, hour_utc: 3, window_days: 7, last_run_date: null })
  const [scheduleDraft, setScheduleDraft] = useState({ enabled: false, hour_utc: 3, window_days: 7 })
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const scheduleInitialized = useRef(false)
  const { navigate } = useRouter()

  const refresh = useCallback(async () => {
    try {
      const [nextRuns, nextJobs, nextSchedule] = await Promise.all([
        api.get<DiscoveryRun[]>('/api/discovery/runs?limit=20'),
        api.get<Job[]>('/api/jobs?limit=200'),
        api.get<Schedule>('/api/discovery/schedule'),
      ])
      setRuns(nextRuns); setJobs(nextJobs); setSchedule(nextSchedule)
      if (!scheduleInitialized.current) {
        setScheduleDraft({ enabled: nextSchedule.enabled, hour_utc: nextSchedule.hour_utc, window_days: nextSchedule.window_days })
        scheduleInitialized.current = true
      }
      setError(null)
    } catch (cause) { setError(errorText(cause)) }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { void refresh() }, [refresh])

  const jobFor = (run: DiscoveryRun) => jobs.find((job) => job.id === run.job_id)
  const active = runs.some((run) => {
    const job = jobFor(run)
    return run.status === 'running' && (!job || ['queued', 'running'].includes(job.state))
  })
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => { void refresh() }, 3000)
    return () => window.clearInterval(timer)
  }, [active, refresh])

  async function startRun(event: React.FormEvent) {
    event.preventDefault()
    if (!name.trim() && !businessId.trim() && !registrationStart && !registrationEnd) { setError('Enter a company name or Finnish business ID, or choose a registration date window.'); return }
    if (registrationStart && registrationEnd && registrationStart > registrationEnd) { setError('The start date must be before the end date.'); return }
    setBusy(true); setError(null)
    try {
      await api.post('/api/discovery/runs', { name: name.trim() || null, business_id: businessId.trim() || null, registration_start: registrationStart || null, registration_end: registrationEnd || null, max_pages: maxPages, max_companies: maxCompanies })
      await refresh()
    } catch (cause) { setError(errorText(cause)) }
    finally { setBusy(false) }
  }
  async function jobAction(id: string, action: 'cancel' | 'retry') {
    setBusy(true); setError(null)
    try { await api.post(`/api/jobs/${id}/${action}`); await refresh() }
    catch (cause) { setError(errorText(cause)) }
    finally { setBusy(false) }
  }
  async function saveSchedule(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError(null)
    try { const saved = await putJson<Schedule>('/api/discovery/schedule', scheduleDraft); setSchedule(saved); setScheduleDraft({ enabled: saved.enabled, hour_utc: saved.hour_utc, window_days: saved.window_days }); scheduleInitialized.current = true }
    catch (cause) { setError(errorText(cause)) }
    finally { setBusy(false) }
  }

  return <section className="research-panel discovery-panel" aria-labelledby="discovery-title">
    <header className="research-panel__header"><div><h2 id="discovery-title">Registry discovery</h2><p>Search Finnish registry records in a bounded window.</p></div></header>
    {error && <p className="research-error" role="alert">{error}</p>}
    <form className="discovery-form" onSubmit={startRun}><h3>Start a run</h3><label>Company name<input value={name} onChange={(e) => setName(e.target.value)} maxLength={300} placeholder="Optional with business ID or dates" /></label><label>Finnish business ID<input value={businessId} onChange={(e) => setBusinessId(e.target.value)} maxLength={64} placeholder="1234567-8" autoComplete="off" /></label><div className="discovery-form__dates"><label>Registered from<input type="date" value={registrationStart} onChange={(e) => setRegistrationStart(e.target.value)} /></label><label>Registered through<input type="date" value={registrationEnd} onChange={(e) => setRegistrationEnd(e.target.value)} /></label></div><p className="research-muted">Search by company name, Finnish business ID, registration dates, or a combination.</p><div className="discovery-form__limits"><label>Maximum pages<select value={maxPages} onChange={(e) => setMaxPages(Number(e.target.value))}>{[1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>{value}</option>)}</select></label><label>Maximum companies<input type="number" min={1} max={100} required value={maxCompanies} onChange={(e) => setMaxCompanies(Number(e.target.value))} /></label></div><button className="research-button research-button--primary" disabled={busy}>{busy ? 'Starting…' : 'Start discovery'}</button></form>
    <details className="discovery-schedule"><summary>Daily discovery settings</summary><form onSubmit={saveSchedule}><label className="discovery-schedule__toggle"><input type="checkbox" checked={scheduleDraft.enabled} onChange={(e) => setScheduleDraft((current) => ({ ...current, enabled: e.target.checked }))} /> Enable daily discovery</label><label>Run at (UTC hour)<select value={scheduleDraft.hour_utc} onChange={(e) => setScheduleDraft((current) => ({ ...current, hour_utc: Number(e.target.value) }))}>{Array.from({ length: 24 }, (_, hour) => <option key={hour} value={hour}>{String(hour).padStart(2, '0')}:00 UTC</option>)}</select></label><label>Registration window (days)<input type="number" min={1} max={30} value={scheduleDraft.window_days} onChange={(e) => setScheduleDraft((current) => ({ ...current, window_days: Number(e.target.value) }))} /></label><p className="research-muted">The schedule queues a bounded search once each day after the selected UTC hour.</p><button className="research-button" disabled={busy}>{busy ? 'Saving…' : 'Save daily settings'}</button>{schedule.last_run_date && <p className="research-muted">Last scheduled run: {schedule.last_run_date}</p>}</form></details>
    <div className="discovery-runs"><h3>Recent runs</h3>{loading ? <p className="research-muted">Loading runs…</p> : runs.length === 0 ? <p className="research-muted">No discovery runs yet.</p> : runs.map((run) => {
      const job = jobFor(run)
      const status = run.status === 'running' && job && !['queued', 'running'].includes(job.state) ? job.state : run.status
      const max = Number(run.params.max_pages ?? 5)
      const retry = job && ['failed', 'cancelled'].includes(job.state)
      return <article className="discovery-run" key={run.id}><div className="discovery-run__title"><strong>{humanize(status)}</strong><time>{new Date(run.started_at).toLocaleString()}</time></div><p>Page {Math.min(run.current_page, max)} of {max} · {run.companies_seen} records reviewed · {run.companies_imported} added{run.total_results !== null && ` · ${run.total_results} results reported`}</p>{run.params.name && <p className="research-muted">Search: {run.params.name}</p>}{run.params.business_id && <p className="research-muted">Business ID: {run.params.business_id}</p>}{run.errors.map((item, index) => <p className="research-error" key={index}>{humanize(item)}</p>)}<div className="discovery-run__actions">{job && ['queued', 'running'].includes(job.state) && <button className="research-button" onClick={() => void jobAction(job.id, 'cancel')} disabled={busy}>Cancel</button>}{job && retry && <button className="research-button" onClick={() => void jobAction(job.id, 'retry')} disabled={busy}>Retry</button>}{status === 'completed' && run.companies_imported > 0 && <button className="research-button" onClick={() => navigate('/companies')}>View companies</button>}</div></article>
    })}</div>
  </section>
}
