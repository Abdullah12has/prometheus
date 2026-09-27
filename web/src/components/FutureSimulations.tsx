import { useEffect, useRef, useState } from 'react'
import { ArrowRight, GitBranch, Play, ExternalLink } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import { Link, useRouter } from '../lib/router'
import { CompanyPicker } from './CompanyPicker'
import type { CheckLine, MatchStatus } from '../lib/dealsTypes'
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

export function FutureSimulations() {
  const { search, navigate } = useRouter()
  const [companyId, setCompanyId] = useState(new URLSearchParams(search).get('company') ?? '')
  const [options, setOptions] = useState<Options | null>(null)
  const [history, setHistory] = useState<RunSummary[]>([])
  const [run, setRun] = useState<Run | null>(null)
  const [busy, setBusy] = useState(false)
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
    setBusy(true); setError(null)
    try {
      const result = await api.get<Run>(`/api/simulations/${encodeURIComponent(id)}`)
      if (generation.current === current) { setRun(result); setCompanyId(result.company_id) }
    } catch (cause) { if (generation.current === current) setError(errorMessage(cause)) }
    finally { if (generation.current === current) setBusy(false) }
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
    setBusy(true); setError(null); setRun(null)
    try {
      const result = await api.post<Run>('/api/simulations', { company_id: companyId })
      if (generation.current !== current) return
      setRun(result)
      setHistory(items => [{ id: result.id, company_id: result.company_id, company_name: result.company_name, created_at: result.created_at, lead_count: result.results.length }, ...items].slice(0, 20))
    } catch (cause) { if (generation.current === current) setError(errorMessage(cause)) }
    finally { if (generation.current === current) setBusy(false) }
  }

  return <section className="future-sim" aria-label="Explore future simulations">
    <div className="future-sim__intro">
      <div><span className="future-sim__eyebrow">Start with a company. We build the paths.</span><h2>Who could be the right buyer?</h2><p>Compare researched buyers, see why they may fit, and explore three possible owner decisions. No mandate setup required.</p></div>
      <ol className="future-sim__steps"><li>Choose a seller</li><li>Explore futures</li><li>Review buyers &amp; next steps</li></ol>
    </div>
    <div className="future-sim__start">
      <div><label htmlFor="simulation-company">Company to explore</label><CompanyPicker id="simulation-company" value={companyId} onChange={value => selectCompany(String(value))} disabled={busy} /></div>
      <button className="btn btn--primary" type="button" onClick={() => void explore()} disabled={busy || !companyId}><Play size={16} />{busy ? 'Building your simulation…' : 'Explore futures'}</button>
    </div>
    {!run && !busy && options && <div className="future-sim__suggested"><span>Recently researched</span>{options.companies.map(company => <button type="button" key={company.id} onClick={() => selectCompany(company.id)} aria-pressed={company.id === companyId}>{company.name} <small>{company.country}</small></button>)}</div>}
    {!run && <p className="muted small">Automatically explores: sell the whole company · retain a 25% stake · keep the team. These are hypothetical choices, never assumed owner wishes.</p>}
    {busy && <div className="future-sim__progress" role="status"><GitBranch size={20} /><div><strong>Comparing source-backed buyer research</strong><p>Checking fit, attaching evidence and building decision branches. This may take up to a minute.</p></div></div>}
    {error && <div role="alert" className="future-sim__error"><p>{error}</p><div><button className="btn btn--secondary" onClick={() => void explore()} disabled={!companyId || busy}>Try again</button> <Link to={companyId ? `/companies/${companyId}` : '/companies'}>Review company research</Link> · <Link to="/buyers">Review buyers</Link></div></div>}
    {run && !busy && <>
      <header className="future-sim__results-header"><div><span className="future-sim__eyebrow">Saved {new Date(run.created_at).toLocaleString()}</span><h2>Possible futures for {run.company_name}</h2><p>{run.results.length} buyer leads · {run.buyers_evaluated} sourced profiles compared · {run.buyers_not_evaluated} not evaluated</p></div><button className="btn btn--secondary" onClick={() => navigate(`/matches?run=${run.id}`)}>Open saved run</button></header>
      <p className="future-sim__notice">Research leads, not confirmed buyer interest. Unreviewed evidence stays unreviewed; each owner choice below is hypothetical.</p>
      {!run.results.length && <div className="future-sim__empty"><h3>No sufficiently supported buyer lead found</h3><p>Nothing has been forced into a match. Add company detail or research more buyers, then explore again.</p><Link to="/buyers">Research buyers <ArrowRight size={14} /></Link></div>}
      {run.rejected_recommendations > 0 && <p className="muted small">{run.rejected_recommendations} model suggestions were discarded because their evidence references could not be validated.</p>}
      <div className="future-sim__results">{run.results.map((result, index) => <BuyerFuture key={`${run.id}-${result.buyer_id}`} result={result} index={index} />)}</div>
      <details className="future-sim__method"><summary>What this simulation can and cannot tell you</summary><p>{run.scope}</p><ul>{run.limitations.map(item => <li key={item}>{item}</li>)}</ul></details>
    </>}
    {history.length > 0 && <details className="future-sim__history" open={!run}><summary>Saved simulations ({history.length})</summary><div>{history.map(item => <button type="button" key={item.id} disabled={busy} onClick={() => void openRun(item.id)}><strong>{item.company_name}</strong><span>{item.lead_count} leads · {new Date(item.created_at).toLocaleString()}</span><ArrowRight size={14} /></button>)}</div></details>}
  </section>
}

function BuyerFuture({ result, index }: { result: Result; index: number }) {
  const [branchId, setBranchId] = useState(result.scenarios[0]?.id ?? '')
  const branch = result.scenarios.find(item => item.id === branchId)
  return <article className="future-sim__buyer">
    <header><div><span className="future-sim__eyebrow">Research lead {index + 1}</span><h3><Link to={`/buyers/${result.buyer_id}`}>{result.buyer_name}</Link></h3></div><span className={`status-pill status-pill--${result.status}`}>{statusText(result.status)}</span></header>
    <p className="future-sim__reason">{result.reason}</p><p className="muted small">{result.interpretation}</p>
    <details className="future-sim__evidence" open={index === 0}><summary>Why this lead: {result.sources.length} cited records</summary><div>{result.sources.map(source => <blockquote key={source.ref}><span>{source.field.startsWith('buyer_') ? 'Buyer evidence' : 'Seller evidence'} · {source.review_status}</span><p>{source.excerpt || source.value}</p><a href={source.url} target="_blank" rel="noopener noreferrer">{source.title} <ExternalLink size={12} /></a>{source.retrieved_at && <small>Retrieved {new Date(source.retrieved_at).toLocaleDateString()}</small>}</blockquote>)}</div></details>
    <h4>What would the owner prefer?</h4>
    <div className="future-sim__branches" role="group" aria-label={`Owner scenarios for ${result.buyer_name}`}>{result.scenarios.map(item => <button type="button" key={item.id} onClick={() => setBranchId(item.id)} aria-pressed={item.id === branchId}><GitBranch size={15} /><strong>{item.label}</strong><span>{statusText(item.status)}</span></button>)}</div>
    {branch && <div className="future-sim__branch-detail"><strong>{branch.label} — hypothetical</strong><ul>{branch.checks.filter(check => check.origin === 'owner').map(check => <li key={check.key}><span>{check.result === 'pass' ? 'Supported by mandate' : check.result === 'fail' ? 'Conflicts with mandate' : 'Ask the buyer'}</span>: {check.detail}</li>)}</ul><p>Next: {branch.questions[0] ?? 'Advisor reviews the evidence with both parties.'}</p></div>}
    <details className="future-sim__tree"><summary><GitBranch size={15} /> View the full decision tree</summary><ol>{result.decision_tree.map(node => <li key={node.question}><strong>{node.question}</strong><span className="muted small">Current state: {statusText(node.state)}</span><div><p><b>Yes</b>{node.yes}</p><p><b>No</b>{node.no}</p><p><b>Unknown</b>{node.unknown}</p></div></li>)}</ol></details>
    <details><summary>Questions to resolve before approaching this buyer</summary><ul>{result.questions.map(question => <li key={question}>{question}</li>)}</ul></details>
  </article>
}
