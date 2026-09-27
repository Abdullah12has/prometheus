import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, LoaderCircle, Clock3, AlertCircle } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import './research.css'

type ResearchRun = { id: string; company_id: string; job_id: string | null; status: string; checked: string[]; missing: string[]; blocked: string[]; errors: string[]; recommendations: string[]; started_at: string; finished_at: string | null }
type ResearchCoverage = ResearchRun & { required: Record<string, 'accepted' | 'proposed_unreviewed' | 'missing' | 'confirmed' | 'unconfirmed'>; scope_note: string }
type Progress = { phase: string; detail: string; updated_at: string; source_count: number; current?: number; total?: number; events: { phase: string; detail: string; at: string }[] }
type Job = { progress?: Progress | null; id: string; kind: string; state: string; company_id: string | null; attempts: number; last_error: string | null; created_at: string; updated_at: string }
const humanize = (value: string) => value.replace(/_/g, ' ').replace(/: /g, ' — ')
const errorText = (cause: unknown) => cause instanceof ApiError ? `${cause.message}${cause.detail ? ` — ${typeof cause.detail === 'string' ? cause.detail : JSON.stringify(cause.detail)}` : ''}` : cause instanceof Error ? cause.message : 'Research could not be loaded.'

export function ResearchPanel({ companyId, onUpdated }: { companyId: string; onUpdated?: () => void }) {
  const [runs, setRuns] = useState<ResearchRun[]>([])
  const [coverage, setCoverage] = useState<ResearchCoverage | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const wasActive = useRef(false)
  const requestId = useRef(0)
  const activeJobs = jobs.filter((job) => job.kind === 'company.enrich' && job.company_id === companyId && ['queued', 'running'].includes(job.state))
  const active = activeJobs.length > 0

  const refresh = useCallback(async () => {
    const request = ++requestId.current
    try {
      const [nextRuns, nextJobs] = await Promise.all([
        api.get<ResearchRun[]>(`/api/research/runs?company_id=${encodeURIComponent(companyId)}&limit=10`),
        api.get<Job[]>(`/api/jobs?company_id=${encodeURIComponent(companyId)}&limit=50`),
      ])
      let nextCoverage: ResearchCoverage | null = null
      // Coverage returns 404 until the first research run exists; don't turn that normal state into an error.
      if (nextRuns.length > 0) {
        try { nextCoverage = await api.get<ResearchCoverage>(`/api/companies/${encodeURIComponent(companyId)}/coverage`) }
        catch (cause) { if (!(cause instanceof ApiError && cause.status === 404)) throw cause }
      }
      if (request !== requestId.current) return
      setRuns(nextRuns); setJobs(nextJobs.filter((job) => job.kind === 'company.enrich')); setCoverage(nextCoverage); setError(null)
      const nowActive = nextJobs.some((job) => job.kind === 'company.enrich' && job.company_id === companyId && ['queued', 'running'].includes(job.state))
      if (wasActive.current && !nowActive) onUpdated?.()
      wasActive.current = nowActive
    } catch (cause) { if (request === requestId.current) setError(errorText(cause)) }
    finally { if (request === requestId.current) setLoading(false) }
  }, [companyId, onUpdated])

  useEffect(() => { setLoading(true); void refresh(); return () => { requestId.current++ } }, [refresh])
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => { void refresh() }, 1000)
    return () => window.clearInterval(timer)
  }, [active, refresh])

  async function queueResearch() {
    setBusy(true); setError(null)
    try { await api.post(`/api/companies/${companyId}/enrichments`); onUpdated?.(); await refresh() }
    catch (cause) { setError(errorText(cause)) }
    finally { setBusy(false) }
  }
  async function jobAction(jobId: string, action: 'cancel' | 'retry') {
    setBusy(true); setError(null)
    try { await api.post(`/api/jobs/${jobId}/${action}`); await refresh() }
    catch (cause) { setError(errorText(cause)) }
    finally { setBusy(false) }
  }

  const latest = runs[0]
  const displayedJob = activeJobs[0] ?? jobs[0]
  const canRetry = jobs.find((job) => job.kind === 'company.enrich' && ['failed', 'cancelled'].includes(job.state))
  return <section className="panel research-panel" aria-labelledby="research-panel-title">
    <header className="research-panel__header"><div><h2 id="research-panel-title">Research coverage</h2><p>Shows what this run checked and what remains unknown.</p></div><button type="button" className="research-button research-button--primary" onClick={() => void queueResearch()} disabled={busy || active}>{active ? (activeJobs[0]?.state === 'queued' ? 'Research queued…' : 'Research running…') : busy ? 'Starting…' : 'Refresh research'}</button></header>
    {error && <p className="research-error" role="alert">{error}</p>}
    {loading ? <p className="research-muted">Loading research…</p> : <>
      {displayedJob && <ResearchActivity job={displayedJob} />}
      {activeJobs.map((job) => <div className="research-job" key={job.id}><span>{job.state === 'queued' ? 'Starts when a research worker is free. Interactive jobs take priority over bulk research.' : 'You can leave this page; research continues in the background.'}</span><button className="research-button" onClick={() => void jobAction(job.id, 'cancel')} disabled={busy}>Cancel research</button>{job.last_error && <p>{job.last_error}</p>}</div>)}
      {latest ? <><div className="research-run-status"><strong>{active && latest.job_id !== displayedJob?.id ? 'Previous run' : humanize(latest.status)}</strong><time>{new Date(latest.started_at).toLocaleString()}</time></div><p className="research-disclaimer">Coverage reflects sources checked in this run; it does not establish that all available information was found.</p>
        {coverage && <RequiredCoverage items={coverage.required} scopeNote={coverage.scope_note} />}
        <details className="research-details" open={Boolean(latest.blocked.length || latest.errors.length)}><summary>Source checks and research gaps</summary><CoverageList title="Checked" items={latest.checked} empty="No sources checked yet." /><CoverageList title="Missing" items={latest.missing} empty="No missing items were reported by this run." /><CoverageList title="Blocked" items={latest.blocked} empty="No blocked items were reported by this run." /><CoverageList title="Errors" items={latest.errors} empty="No errors reported." isError /><CoverageList title="Suggested next steps" items={latest.recommendations} empty="No next steps suggested." /></details>
      </> : !active && <p className="research-muted">No research run yet. Start one to check the registry and available company sources.</p>}
      {!active && !canRetry && jobs.find((job) => job.kind === 'company.enrich' && job.state === 'failed')?.last_error && <p className="research-error">{jobs.find((job) => job.kind === 'company.enrich' && job.state === 'failed')?.last_error}</p>}
      {canRetry && !active && <div className="research-job"><span>Previous research {canRetry.state}</span><button type="button" className="research-button" onClick={() => void jobAction(canRetry.id, 'retry')} disabled={busy}>Retry</button>{canRetry.last_error && <p>{canRetry.last_error}</p>}</div>}
    </>}
  </section>
}

const STAGES = [
  ['registry', 'Company identity'], ['website', 'Company website'], ['search', 'Web sources'],
  ['extraction', 'Extract insights'], ['saving', 'Save evidence'],
] as const

function ResearchActivity({ job }: { job: Job }) {
  const progress = job.progress
  const running = job.state === 'running'
  const queued = job.state === 'queued'
  const complete = job.state === 'succeeded'
  const failed = job.state === 'failed' || job.state === 'cancelled'
  const stage = STAGES.findIndex(([phase]) => phase === progress?.phase)
  const title = queued ? 'Queued for enrichment' : complete ? 'Research complete' : failed ? `Research ${job.state}` : 'Enriching this company'
  const Icon = queued ? Clock3 : running ? LoaderCircle : failed ? AlertCircle : Check
  return <section className={`enrichment-activity ${running ? 'is-running' : ''}`} aria-label="Enrichment activity">
    <div className="enrichment-activity__heading"><Icon size={18} className={running ? 'enrichment-spinner' : ''} aria-hidden="true" /><strong>{title}</strong>{progress && <span>{progress.source_count} {progress.source_count === 1 ? 'source' : 'sources'} collected</span>}</div>
    <p className="enrichment-activity__current" role="status" aria-live="polite">{queued ? (job.last_error ? 'A temporary issue occurred. Waiting to retry.' : 'Waiting for the next available research worker.') : progress?.detail ?? (running ? 'Preparing the company and its available sources…' : complete ? 'Open the evidence and coverage below to review the results.' : job.last_error ?? 'This run has stopped.')}</p>
    <ol className="enrichment-stages" aria-label="Enrichment stages">{STAGES.map(([phase, label], index) => {
      const done = complete || (!queued && stage > index)
      const current = running && progress?.phase === phase
      return <li key={phase} className={done ? 'is-done' : current ? 'is-current' : ''} aria-current={current ? 'step' : undefined}><span aria-hidden="true">{done ? <Check size={12} /> : index + 1}</span>{label}</li>
    })}</ol>
    {running && progress?.current != null && progress.total != null && <p className="research-muted">{progress.phase === 'extraction' ? 'Batch' : 'Source'} {progress.current} of {progress.total}</p>}
    {Boolean(progress?.events.length) && <ol className="enrichment-events" aria-label="Run activity">{progress!.events.slice().reverse().map((event, index) => <li key={`${event.at}-${index}`}><time dateTime={event.at}>{new Date(event.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time><span>{event.detail}</span></li>)}</ol>}
  </section>
}

function CoverageList({ title, items, empty, isError = false }: { title: string; items: string[]; empty: string; isError?: boolean }) {
  return <section className={`research-coverage ${isError && items.length ? 'is-error' : ''}`}><h3>{title} <span>{items.length}</span></h3>{items.length ? <ul>{items.map((item, index) => <li key={`${item}-${index}`}>{humanize(item)}</li>)}</ul> : <p>{empty}</p>}</section>
}

function RequiredCoverage({ items, scopeNote }: { items: ResearchCoverage['required']; scopeNote: string }) {
  const labels: Record<string, string> = {
    'financial.revenue': 'Revenue',
    'financial.ebitda': 'EBITDA',
    'financial.employees': 'Employees',
    owner_intent: 'Owner intent',
  }
  const statusLabels: Record<string, string> = {
    accepted: 'Accepted',
    proposed_unreviewed: 'Proposed · needs review',
    missing: 'Missing',
    confirmed: 'Confirmed',
    unconfirmed: 'Unconfirmed',
  }
  return <section className="research-coverage research-coverage--required" aria-label="Required information coverage">
    <h3>Required information</h3>
    <ul>{Object.entries(items).map(([key, status]) => <li key={key}>
      <span>{labels[key] ?? humanize(key)}</span>
      <strong className={`coverage-status coverage-status--${status}`}>{statusLabels[status] ?? humanize(status)}</strong>
    </li>)}</ul>
    <p>{scopeNote}</p>
  </section>
}
