import { useEffect, useRef, useState } from 'react'
import { ArrowRight, GitBranch, Play, ExternalLink, FileSearch, ScanLine, Check, Clock3, CircleHelp } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import { Link, useRouter } from '../lib/router'
import { CompanyPicker } from './CompanyPicker'
import type { CheckLine, MatchStatus } from '../lib/dealsTypes'
import { STRUCTURE_LABELS } from '../lib/dealsTypes'
import './future-simulations.css'

type Source = { ref: string; title: string; url: string; value: string; excerpt: string; field: string; review_status: string; retrieved_at: string | null }
type Branch = { id: string; label: string; hypothetical: boolean; status: MatchStatus; checks: CheckLine[]; questions: string[] }
type Node = { question: string; state: string; yes: string; no: string; unknown: string }
type Result = { buyer_id: string; buyer_name: string; reason: string; interpretation: string; status: MatchStatus; sources: Source[]; questions: string[]; scenarios: Branch[]; decision_tree: Node[] }
type RunSummary = { id: string; company_id: string; company_name: string; created_at: string; lead_count: number }
type Run = Omit<RunSummary, 'lead_count'> & { buyers_evaluated: number; buyers_not_evaluated: number; rejected_recommendations: number; results: Result[]; limitations: string[]; scope: string }
type Options = { companies: { id: string; name: string; country: string | null }[]; scenarios: { id: string; label: string }[] }
const errorMessage = (error: unknown) => error instanceof ApiError ? error.message : 'Could not load simulations. Please retry.'
const statusText = (status: string) => status === 'excluded' ? 'Conflicting requirement' : status === 'compatible' ? 'Potentially compatible' : 'Needs confirmation'
const explanationLabels: Record<string, string> = { ...STRUCTURE_LABELS, max_rollover_pct: 'how much equity the owner can retain', control_retention: 'whether the owner can keep operating control', site_commitment: 'whether the site will stay open', team_commitment: 'whether the team will be kept', brand_commitment: 'whether the brand will be kept', close_within_months: 'the time needed to close', consideration_currency: 'the payment currency', max_consideration: 'the maximum purchase price' }
const readable = (text: string) => text.replace(/mandate is silent on/g, 'buyer preferences do not specify').replace(/\b[a-z]+(?:_[a-z]+)+\b/g, key => explanationLabels[key] ?? key.replaceAll('_', ' '))

export function FutureSimulations() {
  const { search, navigate } = useRouter()
  const [companyId, setCompanyId] = useState(new URLSearchParams(search).get('company') ?? '')
  const [options, setOptions] = useState<Options | null>(null)
  const [history, setHistory] = useState<RunSummary[]>([])
  const [run, setRun] = useState<Run | null>(null)
  const [busy, setBusy] = useState<'loading' | 'running' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const generation = useRef(0)

  useEffect(() => {
    let cancelled = false
    Promise.all([api.get<Options>('/api/simulations/options'), api.get<RunSummary[]>('/api/simulations')])
      .then(([nextOptions, saved]) => { if (!cancelled) { setOptions(nextOptions); setHistory(saved) } })
      .catch(cause => { if (!cancelled) setError(errorMessage(cause)) })
    return () => { cancelled = true; generation.current++ }
  }, [])

  async function openRun(id: string) {
    const current = ++generation.current
    setBusy('loading'); setError(null)
    try {
      const result = await api.get<Run>(`/api/simulations/${encodeURIComponent(id)}`)
      if (generation.current === current) { setRun(result); setCompanyId(result.company_id) }
    } catch (cause) { if (generation.current === current) setError(errorMessage(cause)) }
    finally { if (generation.current === current) setBusy(null) }
  }

  useEffect(() => {
    const id = new URLSearchParams(search).get('run')
    if (id) void openRun(id)
  }, [search])

  function selectCompany(id: string) {
    generation.current++; setCompanyId(id); setRun(null); setError(null)
  }

  async function explore() {
    if (!companyId || busy) return
    const current = ++generation.current
    setBusy('running'); setError(null); setRun(null)
    try {
      const result = await api.post<Run>('/api/simulations', { company_id: companyId })
      if (generation.current !== current) return
      setRun(result)
      setHistory(items => [{ id: result.id, company_id: result.company_id, company_name: result.company_name, created_at: result.created_at, lead_count: result.results.length }, ...items].slice(0, 20))
    } catch (cause) { if (generation.current === current) setError(errorMessage(cause)) }
    finally { if (generation.current === current) setBusy(null) }
  }

  return <section className="future-sim" aria-label="Explore future simulations">
    <div className="future-sim__intro">
      <div><h2>Explore the paths to a deal.</h2><p>Connect company research to potential buyers. See the evidence, test owner choices, and find the questions that move a deal forward.</p></div>
      <div className="future-sim__intro-mark" aria-hidden="true"><GitBranch size={36} /></div>
    </div>
    <div className="future-sim__start">
      <div><label htmlFor="simulation-company">Company to explore</label><CompanyPicker id="simulation-company" value={companyId} onChange={value => selectCompany(String(value))} disabled={Boolean(busy)} /></div>
      <button className="btn btn--primary" type="button" onClick={() => void explore()} disabled={Boolean(busy) || !companyId}><Play size={16} aria-hidden="true" />{busy === 'running' ? 'Building your simulation…' : busy === 'loading' ? 'Opening saved run…' : 'Explore futures'}</button>
    </div>
    {!run && !busy && options && <div className="future-sim__suggested"><span>Recently researched</span>{options.companies.map(company => <button type="button" key={company.id} onClick={() => selectCompany(company.id)} aria-pressed={company.id === companyId}>{company.name} <small>{company.country}</small></button>)}</div>}
    <SimulationFlow busy={busy} run={run} failed={Boolean(error)} />
    {error && <div role="alert" className="future-sim__error"><p>{error}</p><div><button className="btn btn--secondary" onClick={() => void explore()} disabled={!companyId || Boolean(busy)}>Try again</button> <Link to={companyId ? `/companies/${companyId}` : '/companies'}>Review company research</Link> · <Link to="/buyers">Review buyers</Link></div></div>}
    {run && !busy && <>
      <header className="future-sim__results-header"><div><span className="future-sim__eyebrow">Saved {new Date(run.created_at).toLocaleString()}</span><h2>Possible futures for {run.company_name}</h2><p>{run.results.length} buyer leads · {run.buyers_evaluated} sourced profiles compared · {run.buyers_not_evaluated} not evaluated</p></div><button className="btn btn--secondary" onClick={() => navigate(`/matches?run=${run.id}`)}>Open saved run</button></header>
      <p className="future-sim__notice">Research leads, not confirmed buyer interest. Unreviewed evidence stays unreviewed; each owner choice below is hypothetical.</p>
      {!run.results.length && <div className="future-sim__empty"><h3>No sufficiently supported buyer lead found</h3><p>Nothing has been forced into a match. Add company detail or research more buyers, then explore again.</p><Link to="/buyers">Research buyers <ArrowRight size={14} /></Link></div>}
      {run.rejected_recommendations > 0 && <p className="muted small">{run.rejected_recommendations} model suggestions were discarded because their evidence references could not be validated.</p>}
      <div className="future-sim__results">{run.results.map((result, index) => <BuyerFuture key={`${run.id}-${result.buyer_id}`} result={result} index={index} />)}</div>
      <details className="future-sim__method"><summary>What this simulation can and cannot tell you</summary><p>{run.scope}</p><ul>{run.limitations.map(item => <li key={item}>{item}</li>)}</ul></details>
    </>}
    {history.length > 0 && <details className="future-sim__history" open={!run}><summary>Saved simulations ({history.length})</summary><div>{history.map(item => <button type="button" key={item.id} disabled={Boolean(busy)} onClick={() => void openRun(item.id)}><strong>{item.company_name}</strong><span>{item.lead_count} leads · {new Date(item.created_at).toLocaleString()}</span><ArrowRight size={14} /></button>)}</div></details>}
  </section>
}

function SimulationFlow({ busy, run, failed }: { busy: 'loading' | 'running' | null; run: Run | null; failed: boolean }) {
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    setElapsed(0)
    if (busy !== 'running') return
    const started = Date.now()
    const timer = window.setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000)
    return () => window.clearInterval(timer)
  }, [busy])
  const completed = Boolean(run && !busy)
  const sources = new Set(run?.results.flatMap(result => result.sources.map(source => source.ref))).size
  return <section className={`future-sim__canvas${busy === 'running' ? ' is-running' : ''}`} aria-label="Simulation workflow">
    <header><span><GitBranch size={16} aria-hidden="true" /> How the simulation works</span><span className={`future-sim__run-state${completed ? ' is-complete' : ''}`} role="status">{completed ? <Check size={14} aria-hidden="true" /> : <Clock3 size={14} aria-hidden="true" />}{busy === 'running' ? 'Simulation running' : busy === 'loading' ? 'Loading saved results' : failed ? 'Run needs attention' : completed ? 'Results saved' : 'Ready to explore'}</span></header>
    <ol className="future-sim__flow">
      <li><span className="future-sim__node-icon"><FileSearch size={21} /></span><span className="future-sim__eyebrow">Company evidence</span><h3>Start with what’s known</h3><p>Read the company’s saved research and the sources behind it.</p><span className="future-sim__node-foot">{completed ? `${sources} distinct records cited in leads` : 'Published facts & source records'}</span></li>
      <li><span className="future-sim__node-icon"><ScanLine size={21} /></span><span className="future-sim__eyebrow">Buyer fit</span><h3>Find the relevant buyers</h3><p>Compare buyer strategies and validate the evidence behind each lead.</p><span className="future-sim__node-foot">{completed ? `${run?.buyers_evaluated} profiles evaluated` : 'Sector, geography & deal preferences'}</span></li>
      <li><span className="future-sim__node-icon"><GitBranch size={21} /></span><span className="future-sim__eyebrow">Owner choices</span><h3>Follow the possible paths</h3><p>Test how a sale, retained stake or team protection changes the fit.</p><span className="future-sim__node-foot">{completed ? `${run?.results.length} buyer leads to review` : 'Three hypothetical scenarios per lead'}</span></li>
    </ol>
    <footer><span>{busy === 'running' ? 'Comparing research and building decision branches. Results appear when the run finishes.' : completed ? 'Open a lead below to follow its evidence and explore each owner choice.' : 'Each path shows supporting evidence, conflicts and questions still to resolve.'}</span>{busy === 'running' ? <span className="future-sim__elapsed" aria-label="Elapsed time">{elapsed}s elapsed</span> : <span>Owner choices are hypothetical</span>}</footer>
  </section>
}

function BuyerFuture({ result, index }: { result: Result; index: number }) {
  const [branchId, setBranchId] = useState(result.scenarios[0]?.id ?? '')
  const branch = result.scenarios.find(item => item.id === branchId)
  return <article className="future-sim__buyer">
    <header><div className="future-sim__buyer-title"><span className="future-sim__lead-mark" aria-hidden="true"><GitBranch size={22} /></span><div><span className="future-sim__eyebrow">Research lead {index + 1}</span><h3><Link to={`/buyers/${result.buyer_id}`}>{result.buyer_name}</Link></h3></div></div><span className={`status-pill status-pill--${result.status}`}>{statusText(result.status)}</span></header>
    <p className="future-sim__reason">{result.reason}</p><p className="muted small">{result.interpretation}</p>
    <details className="future-sim__evidence" open={index === 0}><summary>Why this lead: {result.sources.length} cited records</summary><div>{result.sources.map(source => <blockquote key={source.ref}><span>{source.field.startsWith('buyer_') ? 'Buyer evidence' : 'Seller evidence'} · {source.review_status}</span><p>{source.excerpt || source.value}</p><a href={source.url} target="_blank" rel="noopener noreferrer">{source.title} <ExternalLink size={12} /></a>{source.retrieved_at && <small>Retrieved {new Date(source.retrieved_at).toLocaleDateString()}</small>}</blockquote>)}</div></details>
    <h4>What would the owner prefer?</h4><p className="future-sim__scenario-hint">Select a path to see what changes and what needs a conversation.</p>
    <div className="future-sim__branches" role="group" aria-label={`Owner scenarios for ${result.buyer_name}`}>{result.scenarios.map(item => <button type="button" key={item.id} onClick={() => setBranchId(item.id)} aria-pressed={item.id === branchId}><GitBranch size={15} /><strong>{item.label}</strong><span>{statusText(item.status)}</span></button>)}</div>
    {branch && <div className="future-sim__branch-detail" aria-live="polite"><strong>{branch.label} — hypothetical</strong><ul>{branch.checks.filter(check => check.origin === 'owner').map(check => <li key={check.key}><span>{check.result === 'pass' ? <Check size={15} aria-hidden="true" /> : <CircleHelp size={15} aria-hidden="true" />}{check.result === 'pass' ? 'Supported by mandate' : check.result === 'fail' ? 'Conflicts with mandate' : 'Ask the buyer'}</span>: {readable(check.detail)}</li>)}</ul><p><ArrowRight size={15} aria-hidden="true" /> Next: {readable(branch.questions[0] ?? 'Advisor reviews the evidence with both parties.')}</p></div>}
    <details className="future-sim__tree"><summary><GitBranch size={15} /> View the full decision tree</summary><ol>{result.decision_tree.map(node => <li key={node.question}><strong>{node.question}</strong><span className="muted small">Current state: {statusText(node.state)}</span><div><p><b>Yes</b>{readable(node.yes)}</p><p><b>No</b>{readable(node.no)}</p><p><b>Unknown</b>{readable(node.unknown)}</p></div></li>)}</ol></details>
    <details><summary>Questions to resolve before approaching this buyer</summary><ul>{result.questions.map(question => <li key={question}>{readable(question)}</li>)}</ul></details>
  </article>
}
