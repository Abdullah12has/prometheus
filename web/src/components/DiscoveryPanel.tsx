import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, getCsrfToken } from '../lib/api'
import { useRouter } from '../lib/router'
import './research.css'

type DiscoveryRun = { id: string; job_id: string | null; status: string; params: { name?: string | null; business_id?: string | null; registration_start?: string | null; registration_end?: string | null; max_pages?: number; max_companies?: number }; current_page: number; total_results: number | null; companies_seen: number; companies_imported: number; errors: string[]; started_at: string; finished_at: string | null; updated_at: string }
type Job = { id: string; kind: string; state: string; company_id: string | null; last_error: string | null; updated_at: string }
type Schedule = { enabled: boolean; hour_utc: number; window_days: number; last_run_date: string | null }
type RegistrySource = { id: string; country: Country; label: string; publisher: string; url: string; license: string; identifier: { jurisdiction: string; scheme: string }; coverage: string; limits: string }
type Country = 'FI' | 'CH' | 'DE'
type JobCounts = { queued: number; running: number; succeeded: number; failed: number; cancelled: number }
type RegistryImport = { id: string; source: string; country: Country; status: string; source_total: number | null; processed: number; created: number; matched: number; skipped: number; progress: number | null; checkpoint: { source?: string }; snapshot: string | null; errors: string[]; exhausted: boolean; coverage: string; limits: string; updated_at: string; started_at: string; finished_at: string | null }
type Campaign = { id: string; country: Country; status: 'running' | 'paused' | 'completed'; max_in_flight: number; max_companies: number | null; enqueued: number; jobs: JobCounts; exhausted: boolean; last_error: string | null; created_at: string; updated_at: string; finished_at: string | null }
type RegistryCountry = { country: Country; companies: number; identifiers: Record<string, number>; imports: RegistryImport[]; sources: string[]; enrichment: { campaign: Campaign | null; researched_companies: number; jobs: JobCounts } }
type RegistryStatus = { countries: RegistryCountry[]; registry_worker: boolean }

const COUNTRIES: Country[] = ['FI', 'CH', 'DE']
const COUNTRY_NAMES: Record<Country, string> = { FI: 'Finland', CH: 'Switzerland', DE: 'Germany' }
const SOURCE_IDS: Record<Country, string> = { FI: 'prh_bulk', CH: 'zefix_lindas', DE: 'gleif_de' }
const humanize = (value: string) => value.replace(/_/g, ' ').replace(/: /g, ' — ')
const count = (value: number) => value.toLocaleString()
const dateTime = (value: string | null | undefined) => value ? new Date(value).toLocaleString() : '—'
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
  const [registry, setRegistry] = useState<RegistryStatus | null>(null)
  const [sources, setSources] = useState<RegistrySource[]>([])
  const [registryLoading, setRegistryLoading] = useState(true)
  const [registryError, setRegistryError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
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
  const [advancedLoading, setAdvancedLoading] = useState(false)
  const [advancedError, setAdvancedError] = useState<string | null>(null)
  const scheduleInitialized = useRef(false)
  const { navigate } = useRouter()

  const refreshRegistry = useCallback(async () => {
    try {
      const [nextRegistry, nextSources] = await Promise.all([
        api.get<RegistryStatus>('/api/registry/status'),
        api.get<RegistrySource[]>('/api/registry/sources'),
      ])
      setRegistry(nextRegistry)
      setSources(nextSources)
      setRegistryError(null)
    } catch (cause) { setRegistryError(errorText(cause)) }
    finally { setRegistryLoading(false) }
  }, [])

  const refreshAdvanced = useCallback(async () => {
    setAdvancedLoading(true)
    try {
      const [nextRuns, nextJobs, nextSchedule] = await Promise.all([
        api.get<DiscoveryRun[]>('/api/discovery/runs?limit=20'),
        api.get<Job[]>('/api/jobs?limit=200'),
        api.get<Schedule>('/api/discovery/schedule'),
      ])
      setRuns(nextRuns)
      setJobs(nextJobs)
      setSchedule(nextSchedule)
      if (!scheduleInitialized.current) {
        setScheduleDraft({ enabled: nextSchedule.enabled, hour_utc: nextSchedule.hour_utc, window_days: nextSchedule.window_days })
        scheduleInitialized.current = true
      }
      setAdvancedError(null)
    } catch (cause) { setAdvancedError(errorText(cause)) }
    finally { setAdvancedLoading(false) }
  }, [])

  useEffect(() => { void refreshRegistry() }, [refreshRegistry])

  const countries = registry?.countries ?? []
  const registryActive = countries.some((item) => item.imports.some((run) => ['queued', 'running'].includes(run.status))
    || item.enrichment.campaign?.status === 'running'
    || item.enrichment.jobs.queued > 0 || item.enrichment.jobs.running > 0)
  useEffect(() => {
    if (!registryActive) return
    const timer = window.setInterval(() => { void refreshRegistry() }, 4000)
    return () => window.clearInterval(timer)
  }, [registryActive, refreshRegistry])

  const jobFor = (run: DiscoveryRun) => jobs.find((job) => job.id === run.job_id)
  const legacyActive = runs.some((run) => {
    const job = jobFor(run)
    return run.status === 'running' && (!job || ['queued', 'running'].includes(job.state))
  })
  useEffect(() => {
    if (!legacyActive) return
    const timer = window.setInterval(() => { void refreshAdvanced() }, 3000)
    return () => window.clearInterval(timer)
  }, [legacyActive, refreshAdvanced])

  async function runCountryAction(country: Country, action: 'import' | 'pause-import' | 'resume-import' | 'start-research' | 'pause-research' | 'resume-research') {
    const item = countries.find((entry) => entry.country === country)
    const currentImport = item?.imports.find((entry) => entry.source === SOURCE_IDS[country])
    const campaign = item?.enrichment.campaign
    setBusy(`${country}:${action}`)
    setRegistryError(null)
    try {
      if (action === 'import') await api.post('/api/registry/imports', { source: SOURCE_IDS[country] })
      if (action === 'pause-import' && currentImport) await api.post(`/api/registry/imports/${currentImport.id}/pause`)
      if (action === 'resume-import' && currentImport) await api.post(`/api/registry/imports/${currentImport.id}/resume`)
      if (action === 'start-research') await api.post('/api/registry/enrichment', { country, max_in_flight: 2, max_companies: null })
      if (action === 'pause-research' && campaign) await api.post(`/api/registry/enrichment/${campaign.id}/pause`)
      if (action === 'resume-research' && campaign) await api.post(`/api/registry/enrichment/${campaign.id}/resume`)
      await refreshRegistry()
    } catch (cause) { setRegistryError(errorText(cause)) }
    finally { setBusy(null) }
  }

  async function startRun(event: React.FormEvent) {
    event.preventDefault()
    if (!name.trim() && !businessId.trim() && !registrationStart && !registrationEnd) { setAdvancedError('Enter a company name or Finnish business ID, or choose a registration date window.'); return }
    if (registrationStart && registrationEnd && registrationStart > registrationEnd) { setAdvancedError('The start date must be before the end date.'); return }
    setBusy('advanced'); setAdvancedError(null)
    try {
      await api.post('/api/discovery/runs', { name: name.trim() || null, business_id: businessId.trim() || null, registration_start: registrationStart || null, registration_end: registrationEnd || null, max_pages: maxPages, max_companies: maxCompanies })
      await refreshAdvanced()
    } catch (cause) { setAdvancedError(errorText(cause)) }
    finally { setBusy(null) }
  }
  async function jobAction(id: string, action: 'cancel' | 'retry') {
    setBusy('advanced'); setAdvancedError(null)
    try { await api.post(`/api/jobs/${id}/${action}`); await refreshAdvanced() }
    catch (cause) { setAdvancedError(errorText(cause)) }
    finally { setBusy(null) }
  }
  async function saveSchedule(event: React.FormEvent) {
    event.preventDefault(); setBusy('schedule'); setAdvancedError(null)
    try { const saved = await putJson<Schedule>('/api/discovery/schedule', scheduleDraft); setSchedule(saved); setScheduleDraft({ enabled: saved.enabled, hour_utc: saved.hour_utc, window_days: saved.window_days }); scheduleInitialized.current = true }
    catch (cause) { setAdvancedError(errorText(cause)) }
    finally { setBusy(null) }
  }

  return <section className="research-panel discovery-panel" aria-labelledby="discovery-title">
    <header className="research-panel__header"><div><h2 id="discovery-title">Company registry</h2><p>Import registry records, then research companies in small background batches.</p></div><button type="button" className="research-button" onClick={() => void refreshRegistry()} disabled={registryLoading}>Refresh status</button></header>
    {registryError && <p className="research-error" role="alert">{registryError}</p>}
    {registry?.registry_worker === false && <p className="research-note">Registry work is queued, but the background worker is not available yet.</p>}
    {registryLoading && !registry ? <p className="research-muted">Loading country status…</p> : <div className="registry-countries">
      {COUNTRIES.map((country) => {
        const item = countries.find((entry) => entry.country === country)
        const sourceId = SOURCE_IDS[country]
        const source = sources.find((entry) => entry.id === sourceId)
        const imported = item?.imports.find((entry) => entry.source === sourceId)
        const campaign = item?.enrichment.campaign ?? null
        const importRunning = imported && ['queued', 'running'].includes(imported.status)
        const importResumable = imported && ['paused', 'failed'].includes(imported.status)
        const importing = busy?.startsWith(`${country}:`) === true
        const researching = busy?.startsWith(`${country}:`) === true
        const progress = imported?.progress
        const campaignJobs = campaign?.jobs ?? item?.enrichment.jobs
        return <article className="registry-country" key={country}>
          <div className="registry-country__heading"><div><h3>{COUNTRY_NAMES[country]}</h3><p>{country}</p></div><strong className="registry-country__count">{count(item?.companies ?? 0)}<span>companies</span></strong></div>
          {source ? <div className="registry-source"><strong>{source.label}</strong><p>{source.coverage}</p><p className="research-muted">{source.limits}</p></div> : <p className="research-muted">Source details are loading.</p>}
          <button type="button" className="research-button research-button--primary" onClick={() => void runCountryAction(country, importResumable ? 'resume-import' : 'import')} disabled={Boolean(importRunning) || importing || !source}>
            {importing ? 'Updating import…' : importRunning ? 'Import in progress' : importResumable ? 'Resume import' : imported?.status === 'completed' ? 'Refresh registry' : 'Import companies'}
          </button>
          {imported ? <div className="registry-operation">
            <div className="registry-operation__heading"><strong>{humanize(imported.status)}</strong><span>{imported.snapshot ? `Source date ${imported.snapshot}` : `Updated ${dateTime(imported.updated_at)}`}</span></div>
            <p>{count(imported.processed)}{imported.source_total != null ? ` of ${count(imported.source_total)}` : ''} {imported.checkpoint.source === 'gleif_csv' ? 'global records checked' : 'records processed'}{imported.source_total == null ? ' · total not reported yet' : ''}</p>
            {progress != null && <div className="registry-progress" role="progressbar" aria-label={`${COUNTRY_NAMES[country]} import progress`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(Math.min(1, Math.max(0, progress)) * 100)}><span style={{ width: `${Math.min(100, Math.max(0, progress * 100))}%` }} /></div>}
            <p className="research-muted">{count(imported.created)} added · {count(imported.matched)} matched · {count(imported.skipped)} skipped</p>
            {imported.errors.slice(-2).map((message, index) => <p className="research-error registry-operation__error" key={`${index}-${message}`}>{message}</p>)}
            <div className="registry-operation__actions">
              {importRunning && <button type="button" className="research-button" onClick={() => void runCountryAction(country, 'pause-import')} disabled={importing}>Pause import</button>}
              <time>Updated {dateTime(imported.updated_at)}</time>
            </div>
          </div> : <p className="research-muted">No import has run yet.</p>}
          <div className="registry-research">
            <div className="registry-research__heading"><div><strong>Company research</strong><span>Background research queue</span></div><span className={`registry-status registry-status--${campaign?.status ?? 'idle'}`}>{campaign ? humanize(campaign.status) : 'Not started'}</span></div>
            <p className="research-muted">{count(campaign ? campaignJobs?.succeeded ?? 0 : item?.enrichment.researched_companies ?? 0)} researched · {count(campaignJobs?.failed ?? 0)} failed</p>
            {campaign?.last_error && <p className="research-error registry-operation__error">{campaign.last_error}</p>}
            <div className="registry-operation__actions">
              {campaign?.status === 'running' && <button type="button" className="research-button" onClick={() => void runCountryAction(country, 'pause-research')} disabled={researching}>Pause campaign</button>}
              {campaign?.status === 'paused' && <button type="button" className="research-button" onClick={() => void runCountryAction(country, 'resume-research')} disabled={researching}>Resume campaign</button>}
              {(!campaign || campaign.status === 'completed') && <button type="button" className="research-button" onClick={() => void runCountryAction(country, 'start-research')} disabled={researching}>Start research campaign</button>}
            </div>
          </div>
        </article>
      })}
    </div>}
    <details className="discovery-advanced" onToggle={(event) => { if (event.currentTarget.open) void refreshAdvanced() }}>
      <summary>Advanced targeted search</summary>
      <div className="discovery-advanced__body">
        <p className="research-muted">Run a bounded Finnish Trade Register search by name, business ID, or registration date.</p>
        {advancedError && <p className="research-error" role="alert">{advancedError}</p>}
        <form className="discovery-form" onSubmit={startRun}><h3>Targeted Finnish search</h3><label>Company name<input value={name} onChange={(e) => setName(e.target.value)} maxLength={300} placeholder="Optional with business ID or dates" /></label><label>Finnish business ID<input value={businessId} onChange={(e) => setBusinessId(e.target.value)} maxLength={64} placeholder="1234567-8" autoComplete="off" /></label><div className="discovery-form__dates"><label>Registered from<input type="date" value={registrationStart} onChange={(e) => setRegistrationStart(e.target.value)} /></label><label>Registered through<input type="date" value={registrationEnd} onChange={(e) => setRegistrationEnd(e.target.value)} /></label></div><p className="research-muted">Search by company name, Finnish business ID, registration dates, or a combination.</p><div className="discovery-form__limits"><label>Maximum pages<select value={maxPages} onChange={(e) => setMaxPages(Number(e.target.value))}>{[1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>{value}</option>)}</select></label><label>Maximum companies<input type="number" min={1} max={100} required value={maxCompanies} onChange={(e) => setMaxCompanies(Number(e.target.value))} /></label></div><button className="research-button research-button--primary" disabled={busy === 'advanced'}>{busy === 'advanced' ? 'Starting…' : 'Start targeted search'}</button></form>
        <details className="discovery-schedule"><summary>Daily discovery settings</summary><form onSubmit={saveSchedule}><label className="discovery-schedule__toggle"><input type="checkbox" checked={scheduleDraft.enabled} onChange={(e) => setScheduleDraft((current) => ({ ...current, enabled: e.target.checked }))} /> Enable daily discovery</label><label>Run at (UTC hour)<select value={scheduleDraft.hour_utc} onChange={(e) => setScheduleDraft((current) => ({ ...current, hour_utc: Number(e.target.value) }))}>{Array.from({ length: 24 }, (_, hour) => <option key={hour} value={hour}>{String(hour).padStart(2, '0')}:00 UTC</option>)}</select></label><label>Registration window (days)<input type="number" min={1} max={30} value={scheduleDraft.window_days} onChange={(e) => setScheduleDraft((current) => ({ ...current, window_days: Number(e.target.value) }))} /></label><p className="research-muted">The schedule queues a bounded search once each day after the selected UTC hour.</p><button className="research-button" disabled={busy === 'schedule'}>{busy === 'schedule' ? 'Saving…' : 'Save daily settings'}</button>{schedule.last_run_date && <p className="research-muted">Last scheduled run: {schedule.last_run_date}</p>}</form></details>
        <div className="discovery-runs"><div className="registry-operation__heading"><h3>Recent targeted searches</h3><button type="button" className="research-button" onClick={() => void refreshAdvanced()} disabled={advancedLoading}>Refresh</button></div>{advancedLoading ? <p className="research-muted">Loading searches…</p> : runs.length === 0 ? <p className="research-muted">No targeted searches yet.</p> : runs.map((run) => {
          const job = jobFor(run)
          const status = run.status === 'running' && job && !['queued', 'running'].includes(job.state) ? job.state : run.status
          const max = Number(run.params.max_pages ?? 5)
          const retry = job && ['failed', 'cancelled'].includes(job.state)
          return <article className="discovery-run" key={run.id}><div className="discovery-run__title"><strong>{humanize(status)}</strong><time>{dateTime(run.started_at)}</time></div><p>Page {Math.min(run.current_page, max)} of {max} · {count(run.companies_seen)} records reviewed · {count(run.companies_imported)} added{run.total_results !== null && ` · ${count(run.total_results)} results reported`}</p>{run.params.name && <p className="research-muted">Search: {run.params.name}</p>}{run.params.business_id && <p className="research-muted">Business ID: {run.params.business_id}</p>}{run.errors.map((message, index) => <p className="research-error" key={index}>{humanize(message)}</p>)}<div className="discovery-run__actions">{job && ['queued', 'running'].includes(job.state) && <button className="research-button" onClick={() => void jobAction(job.id, 'cancel')} disabled={busy === 'advanced'}>Cancel</button>}{job && retry && <button className="research-button" onClick={() => void jobAction(job.id, 'retry')} disabled={busy === 'advanced'}>Retry</button>}{status === 'completed' && run.companies_imported > 0 && <button className="research-button" onClick={() => navigate('/companies')}>View companies</button>}</div></article>
        })}</div>
      </div>
    </details>
  </section>
}
