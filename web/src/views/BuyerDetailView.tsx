// Buyer profile — identity, source-backed summary, stated preferences and
// exclusions, sector/geography, history (not exhaustive), sources, fact
// excerpts and open research gaps.
//
// Routes used (buyers module contract, exact):
//   GET  /api/buyers/{id}
//   POST /api/buyers/{id}/research
//   POST /api/buyers/{id}/mandate

import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, ExternalLink, RefreshCw, ShieldCheck, Building2 } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import {
  BUYER_KIND_LABELS, BUYER_RESEARCH_STATUS_LABELS, BUYER_STATUS_LABELS,
  type BuyerDetail, type BuyerMandateResponse, type BuyerResearchResponse,
} from '../lib/buyer-types'
import { LoadingBlock, ErrorBlock } from '../components/StateViews'
import { Link } from '../lib/router'
import { useToast } from '../lib/toast'
import './buyers.css'

function errorText(cause: unknown, fallback: string): string {
  if (cause instanceof ApiError) {
    return `${cause.message}${cause.detail ? ` — ${typeof cause.detail === 'string' ? cause.detail : JSON.stringify(cause.detail)}` : ''}`
  }
  return cause instanceof Error ? cause.message : fallback
}

const ACTIVE_RESEARCH = new Set(['queued', 'running'])

export function BuyerDetailView({ id }: { id: string }) {
  const [buyer, setBuyer] = useState<BuyerDetail | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [researching, setResearching] = useState(false)
  const [mandateBusy, setMandateBusy] = useState(false)
  const [mandateError, setMandateError] = useState<string | null>(null)
  const [mandateId, setMandateId] = useState<string | null>(null)
  const { push } = useToast()

  const load = useCallback((quiet = false) => {
    if (!quiet) setStatus('loading')
    setError(null)
    api.get<BuyerDetail>(`/api/buyers/${id}`)
      .then((result) => { result.facts = result.facts.filter(f => f.review_status !== 'rejected'); result.history = result.history.filter(h => h.review_status !== 'rejected'); setBuyer(result); setStatus('ready') })
      .catch((cause) => { setError(errorText(cause, 'Could not load this buyer.')); setStatus('error') })
  }, [id])
  useEffect(() => { load() }, [load])

  const activeResearch = buyer ? ACTIVE_RESEARCH.has(buyer.research_status) : false
  useEffect(() => {
    if (!activeResearch) return
    const timer = window.setInterval(() => load(true), 4000)
    return () => window.clearInterval(timer)
  }, [activeResearch, load])

  async function researchAgain() {
    if (!buyer) return
    setResearching(true)
    try {
      const result = await api.post<BuyerResearchResponse>(`/api/buyers/${buyer.id}/research`)
      push(result.state === 'running' ? 'Research already in progress' : 'Research queued', 'success')
      load()
    } catch (cause) {
      push(errorText(cause, 'Could not queue research.'), 'error')
    } finally {
      setResearching(false)
    }
  }

  async function createPublicStrategy() {
    if (!buyer) return
    setMandateBusy(true)
    setMandateError(null)
    try {
      const result = await api.post<BuyerMandateResponse>(`/api/buyers/${buyer.id}/mandate`)
      setMandateId(result.mandate_id)
      push('Public-strategy mandate created — not buyer-confirmed', 'success')
    } catch (cause) {
      setMandateError(errorText(cause, 'Could not create a public-strategy mandate. Sourced criteria may be missing.'))
    } finally {
      setMandateBusy(false)
    }
  }

  return (
    <div className="page buyer-detail">
      <Link to="/buyers" className="back-link"><ArrowLeft size={14} aria-hidden="true" /> Buyers</Link>

      {status === 'loading' && <LoadingBlock label="Loading buyer…" />}
      {status === 'error' && <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={() => load()} />}

      {status === 'ready' && buyer && (
        <>
          <header className="detail-header">
            <div>
              <h1>{buyer.name || 'Unnamed buyer'}</h1>
              <div className="detail-header__meta">
                {buyer.website && <a href={buyer.website} target="_blank" rel="noreferrer">{buyer.website}<ExternalLink size={12} aria-hidden="true" /></a>}
                {buyer.country && <span>Region: {buyer.country}</span>}
                <span>{BUYER_KIND_LABELS[buyer.kind]}</span>
                <span>{BUYER_STATUS_LABELS[buyer.status]}</span>
              </div>
              {buyer.company_id && (
                <p className="buyer-detail__company-link">
                  <Building2 size={13} aria-hidden="true" /> Linked company: <Link to={`/companies/${buyer.company_id}`}>Open company record</Link>
                </p>
              )}
            </div>
            <div className="buyer-detail__actions">
              <button type="button" className="btn btn--secondary" onClick={researchAgain} disabled={researching || activeResearch}>
                <RefreshCw size={14} aria-hidden="true" /> {activeResearch ? 'Researching…' : researching ? 'Queuing…' : 'Research again'}
              </button>
              <button type="button" className="btn btn--primary" onClick={createPublicStrategy} disabled={mandateBusy || buyer.status === 'excluded'}>
                <ShieldCheck size={14} aria-hidden="true" /> {mandateBusy ? 'Creating…' : 'Create public strategy'}
              </button>
            </div>
          </header>

          <div className="buyer-summary-row">
            <span className={`buyer-badge buyer-badge--research-${buyer.research_status}`}>{BUYER_RESEARCH_STATUS_LABELS[buyer.research_status]}</span>
            {buyer.last_researched_at && <span className="muted small">Last researched {new Date(buyer.last_researched_at).toLocaleString()}</span>}
          </div>

          {buyer.status === 'excluded' && <p className="field-error" role="status">Excluded from buyer qualification: {buyer.exclusion_reason || 'This firm does not match the buyer scope.'}</p>}

          {activeResearch && (
            <p className="state-block state-block--loading" role="status"><RefreshCw size={16} className="spin" aria-hidden="true" /> Research is running in the background. This page refreshes automatically.</p>
          )}
          {buyer.research_status === 'failed' && (
            <ErrorBlock message={buyer.research_error ?? buyer.latest_job?.error ?? 'The last research attempt failed.'} onRetry={researchAgain} />
          )}
          {mandateError && <p className="field-error" role="alert">{mandateError}</p>}
          {mandateId && (
            <p className="buyer-detail__mandate-note">
              Public-strategy mandate <code className="mono">{mandateId}</code> created. This is a hypothesis about strategy — not a buyer-confirmed mandate.{' '}
              <Link to={`/matches?mandate=${mandateId}`} className="btn btn--secondary">Open in Future simulations</Link>
            </p>
          )}

          <section className="panel">
            <h2>Source-backed summary</h2>
            {buyer.summary ? <p className="detail-description">{buyer.summary}</p> : <p className="muted">No public summary recorded yet.</p>}
          </section>

          <div className="buyer-detail__grid">
            <section className="panel">
              <h2>Stated preferences</h2>
              {buyer.preferences.length > 0 ? (
                <ul className="buyer-list">{buyer.preferences.map((item, index) => <li key={index}>{item}</li>)}</ul>
              ) : <p className="muted">Not publicly stated.</p>}
            </section>
            <section className="panel">
              <h2>Explicit exclusions</h2>
              {buyer.exclusions.length > 0 ? (
                <ul className="buyer-list">{buyer.exclusions.map((item, index) => <li key={index}>{item}</li>)}</ul>
              ) : <p className="muted">Not publicly stated.</p>}
            </section>
            <section className="panel">
              <h2>Sectors</h2>
              {buyer.sectors.length > 0 ? (
                <ul className="buyer-tag-list">{buyer.sectors.map((item) => <li key={item}>{item}</li>)}</ul>
              ) : <p className="muted">Not publicly stated.</p>}
            </section>
            <section className="panel">
              <h2>Geography</h2>
              {buyer.geographies.length > 0 ? (
                <ul className="buyer-tag-list">{buyer.geographies.map((item) => <li key={item}>{item}</li>)}</ul>
              ) : <p className="muted">Not publicly stated.</p>}
            </section>
          </div>

          <section className="panel">
            <h2>History &amp; portfolio</h2>
            <p className="muted small">Sourced instances only — not a complete list of every acquisition by this buyer.</p>
            {buyer.history.length > 0 ? (
              <div className="record-list">
                {buyer.history.map((item) => (
                  <article className="record-list__item" key={item.id}>
                    <strong>{item.target_name}</strong>
                    <span className="muted small">{item.status}{item.announced_on ? ` · ${(/^\d{4}$/.test(item.announced_on) ? item.announced_on : new Date(item.announced_on + 'T00:00:00').toLocaleDateString())}` : ''}</span>
                    {item.summary && <p>{item.summary}</p>}
                    {item.source_url && <a href={item.source_url} target="_blank" rel="noreferrer">Source <ExternalLink size={12} aria-hidden="true" /></a>}
                  </article>
                ))}
              </div>
            ) : <p className="muted">No sourced history recorded yet.</p>}
          </section>

          <section className="panel">
            <h2>Facts &amp; excerpts</h2>
            <p className="muted small">Extracted from public sources. Proposed facts still need review.</p>
            {buyer.facts.length > 0 ? (
              <div className="record-list">
                {buyer.facts.map((fact, index) => (
                  <article className="record-list__item" key={`${fact.field}-${index}`}>
                    <strong>{fact.field.replace(/[._]/g, ' ')}</strong>
                    {fact.value && <p>{fact.value}</p>}
                    {fact.excerpt && <p className="buyer-fact__excerpt">“{fact.excerpt}”</p>}
                    {fact.source_url && <a href={fact.source_url} target="_blank" rel="noreferrer">Open source <ExternalLink size={12} aria-hidden="true" /></a>}
                  </article>
                ))}
              </div>
            ) : <p className="muted">No fact excerpts recorded yet.</p>}
          </section>

          <section className="panel">
            <h2>Sources</h2>
            {buyer.sources.length > 0 ? (
              <ul className="buyer-list">
                {buyer.sources.map((source) => (
                  <li key={source.id}>
                    {source.url ? <a href={source.url} target="_blank" rel="noreferrer">{source.title ?? source.url}</a> : (source.title ?? 'Untitled source')}
                    {source.fetched_at && <span className="muted small"> · fetched {new Date(source.fetched_at).toLocaleDateString()}</span>}
                  </li>
                ))}
              </ul>
            ) : <p className="muted">No sources recorded yet.</p>}
          </section>

          <section className="panel">
            <h2>Open research gaps</h2>
            {buyer.gaps.length > 0 ? (
              <ul className="buyer-list">{buyer.gaps.map((item, index) => <li key={index}>{item}</li>)}</ul>
            ) : <p className="muted">No open gaps recorded.</p>}
          </section>
        </>
      )}
    </div>
  )
}
