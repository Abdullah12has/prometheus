// Buyer mandates, match runs, opportunities/outcomes, historical deals,
// analytics and as-of replay — all against the deals API.
//
// Routes used (backend/permetheus/deals.py, exact — nothing guessed):
//   GET   /api/mandates                       POST /api/mandates
//   GET   /api/mandates/{id}                   PATCH /api/mandates/{id}
//   POST  /api/match-runs
//   GET   /api/match-runs                       GET /api/match-runs/{id}
//   GET   /api/opportunities
//   POST  /api/opportunities/{id}/outcomes      GET /api/opportunities/{id}/outcomes
//   POST  /api/opportunities/{id}/drafts
//   GET   /api/historical-deals                 POST /api/historical-deals
//   POST  /api/historical-deals/import          PATCH /api/historical-deals/{id}
//   GET   /api/deals/analytics
//   POST  /api/simulations/replay
//   POST  /api/sources                          (only when attaching a source)
//   GET   /api/companies                        (company picker)

import { useEffect, useMemo, useState, type FormEvent } from 'react'
import {
  GitCompareArrows, Plus, Play, Pause, Upload, RefreshCw, ChevronDown, ChevronRight, ShieldCheck,
} from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { CompanyDetail, Contact } from '../lib/types'
import { CompanyPicker } from '../components/CompanyPicker'
import { LoadingBlock, ErrorBlock, EmptyState } from '../components/StateViews'
import { useToast } from '../lib/toast'
import { Link, useRouter } from '../lib/router'
import {
  BUYER_RESPONSE_LABELS, DISCLOSURE_FIELD_LABELS, MATCH_STATUS_LABELS, MILESTONE_LABELS,
  REASON_CATEGORY_LABELS, STRUCTURE_LABELS, formatDate, formatDateTime,
  type BuyerResponse, type DealAnalytics, type DealPage, type DisclosureField,
  type DraftOut, type Financing, type HistoricalDealIn, type HistoricalDealOut, type ImportResult, type MandateCriteria,
  type MandateDetail, type MandateIn, type MandateOut, type MatchResultOut, type MatchRunDetail, type MatchRunOut,
  type Milestone, type OpportunityOut, type OutcomeOut, type ReasonBasis, type ReasonCategory,
  type ReplayResponse, type SourceIn, type SourceOut, type SpeakerAuthority, type Structure,
} from '../lib/dealsTypes'
import './deals.css'

const STRUCTURES: Structure[] = ['minority_investment', 'majority_sale', 'full_sale']
const CCY_OPTIONS = ['EUR', 'USD', 'GBP', 'SEK', 'NOK', 'DKK']
const TABS = [
  { id: 'mandates', label: 'Buyer mandates' },
  { id: 'matches', label: 'Match runs' },
  { id: 'opportunities', label: 'Opportunities' },
  { id: 'graph', label: 'Graph' },
  { id: 'history', label: 'Historical deals' },
  { id: 'analytics', label: 'Analytics' },
  { id: 'replay', label: 'Simulate (as-of)' },
] as const
type TabId = typeof TABS[number]['id']

function StatusPill({ status }: { status: 'compatible' | 'research_needed' | 'excluded' }) {
  return <span className={`status-pill status-pill--${status}`}>{MATCH_STATUS_LABELS[status]}</span>
}

async function maybeCreateSource(want: boolean, kind: SourceIn['kind'], url: string, title: string): Promise<string | undefined> {
  if (!want) return undefined
  if (!url && !title) throw new Error('A source needs a URL or a title.')
  const source = await api.post<SourceOut>('/api/sources', { kind, url: url || undefined, title: title || undefined })
  return source.id
}

function SourcePicker({
  label, want, setWant, kind, setKind, url, setUrl, title, setTitle,
}: {
  label: string
  want: boolean
  setWant: (v: boolean) => void
  kind: SourceIn['kind']
  setKind: (v: SourceIn['kind']) => void
  url: string
  setUrl: (v: string) => void
  title: string
  setTitle: (v: string) => void
}) {
  return (
    <div className="condition-row">
      <label className="checkbox-label">
        <input type="checkbox" checked={want} onChange={(e) => setWant(e.target.checked)} />
        {label}
      </label>
      {want && (
        <div className="condition-row__fields">
          <label>
            Source type
            <select value={kind} onChange={(e) => setKind(e.target.value as SourceIn['kind'])}>
              <option value="website">Website</option>
              <option value="document">Document</option>
              <option value="email">Email</option>
              <option value="call">Call</option>
              <option value="note">Note</option>
              <option value="registry">Registry</option>
              <option value="manual">Manual entry</option>
            </select>
          </label>
          <label>
            URL (optional)
            <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" />
          </label>
          <label>
            Title / description
            <input value={title} onChange={(e) => setTitle(e.target.value)} />
          </label>
        </div>
      )}
    </div>
  )
}

export function MatchesView() {
  const [tab, setTab] = useState<TabId>('mandates')

  return (
    <div className="page">
      <header className="page__header">
        <h1>Matches</h1>
        <p className="page__lede">
          Buyer mandates, match runs against owner-confirmed conditions, resulting opportunities and their
          recorded outcomes. Results are ranked as potentially compatible, needing research, or excluded —
          never a probability, offer or guarantee.
        </p>
      </header>

      <nav className="view-tabs" aria-label="Matches sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={tab === t.id ? 'is-active' : undefined}
            aria-current={tab === t.id ? 'page' : undefined}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {tab === 'mandates' && <MandatesTab />}
      {tab === 'matches' && <MatchesTab />}
      {tab === 'opportunities' && <OpportunitiesTab />}
      {tab === 'graph' && <GraphTab />}
      {tab === 'history' && <HistoryTab />}
      {tab === 'analytics' && <AnalyticsTab />}
      {tab === 'replay' && <ReplayTab />}
    </div>
  )
}

// ================================================================== Mandates

function emptyCriteria(): MandateCriteria {
  return {}
}

function MandateForm({ onSaved }: { onSaved: () => void }) {
  const { push } = useToast()
  const [buyerName, setBuyerName] = useState('')
  const [buyerCompanyId, setBuyerCompanyId] = useState('')
  const [identityVerified, setIdentityVerified] = useState(false)
  const [contactName, setContactName] = useState('')
  const [advisor, setAdvisor] = useState('')
  const [evidenceLevel, setEvidenceLevel] = useState<'public_strategy' | 'buyer_confirmed'>('public_strategy')
  const [confirmedBy, setConfirmedBy] = useState('')
  const [lastConfirmedAt, setLastConfirmedAt] = useState('')
  const [expiresAt, setExpiresAt] = useState('')
  const [financingStatus, setFinancingStatus] = useState<Financing>('unknown')
  const [criteria, setCriteria] = useState<MandateCriteria>(emptyCriteria)
  const [countries, setCountries] = useState('')
  const [industries, setIndustries] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [advanced, setAdvanced] = useState(false)

  const [wantMandateSource, setWantMandateSource] = useState(false)
  const [mandateSourceKind, setMandateSourceKind] = useState<SourceIn['kind']>('website')
  const [mandateSourceUrl, setMandateSourceUrl] = useState('')
  const [mandateSourceTitle, setMandateSourceTitle] = useState('')

  const [wantFinancingSource, setWantFinancingSource] = useState(false)
  const [financingSourceKind, setFinancingSourceKind] = useState<SourceIn['kind']>('document')
  const [financingSourceUrl, setFinancingSourceUrl] = useState('')
  const [financingSourceTitle, setFinancingSourceTitle] = useState('')

  function triState(value: boolean | null | undefined, onChange: (v: boolean | null) => void, label: string) {
    return (
      <label>
        {label}
        <select
          value={value === true ? 'yes' : value === false ? 'no' : 'unset'}
          onChange={(e) => onChange(e.target.value === 'unset' ? null : e.target.value === 'yes')}
        >
          <option value="unset">Not stated</option>
          <option value="yes">Yes</option>
          <option value="no">No</option>
        </select>
      </label>
    )
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    if (evidenceLevel === 'buyer_confirmed' && (!confirmedBy || !lastConfirmedAt)) {
      setError('A buyer-confirmed mandate needs who confirmed it and when.')
      return
    }
    if (evidenceLevel === 'public_strategy' && !wantMandateSource) {
      setError('A public-strategy mandate needs a source (e.g. the page stating their acquisition strategy).')
      return
    }
    if (identityVerified && (!buyerCompanyId || !wantMandateSource)) {
      setError('Identity verification needs an existing buyer company and a source that supports its legal identity.')
      return
    }
    if (financingStatus === 'evidenced' && !wantFinancingSource) {
      setError('Evidenced financing needs a source.')
      return
    }
    setSaving(true)
    try {
      const source_id = await maybeCreateSource(wantMandateSource, mandateSourceKind, mandateSourceUrl, mandateSourceTitle)
      const financing_source_id = await maybeCreateSource(wantFinancingSource, financingSourceKind, financingSourceUrl, financingSourceTitle)
      const body: MandateIn = {
        buyer_name: buyerName,
        buyer_company_id: buyerCompanyId || undefined,
        identity_verified: identityVerified,
        contact_name: contactName || undefined,
        advisor: advisor || undefined,
        criteria: {
          ...criteria,
          countries: countries ? countries.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean) : undefined,
          industries: industries ? industries.split(',').map((s) => s.trim()).filter(Boolean) : undefined,
        },
        evidence_level: evidenceLevel,
        source_id,
        confirmed_by: evidenceLevel === 'buyer_confirmed' ? confirmedBy : undefined,
        last_confirmed_at: evidenceLevel === 'buyer_confirmed' ? new Date(lastConfirmedAt).toISOString() : undefined,
        financing_status: financingStatus,
        financing_source_id,
        expires_at: new Date(expiresAt).toISOString(),
      }
      await api.post<MandateOut>('/api/mandates', body)
      push('Buyer mandate recorded', 'success')
      onSaved()
    } catch (cause) {
      setError(explainApiError(cause, 'Could not save this mandate.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="stack-form" onSubmit={handleSubmit}>
      {error && <p className="field-error" role="alert">{error}</p>}
      <div className="field-row">
        <div className="field-col">
          <label htmlFor="buyer-name">Buyer name</label>
          <input id="buyer-name" required value={buyerName} onChange={(e) => setBuyerName(e.target.value)} />
        </div>
        <div className="field-col">
          <label htmlFor="expires-at">Mandate expires</label>
          <input id="expires-at" type="datetime-local" required value={expiresAt} onChange={(e) => setExpiresAt(e.target.value)} />
        </div>
      </div>

      <div className="condition-row">
        <div className="condition-row__head">
          <label>Evidence of this mandate</label>
        </div>
        <div className="condition-row__fields">
          <label>
            Type
            <select value={evidenceLevel} onChange={(e) => setEvidenceLevel(e.target.value as typeof evidenceLevel)}>
              <option value="public_strategy">Public strategy (e.g. website says they acquire)</option>
              <option value="buyer_confirmed">Buyer confirmed (an accountable contact confirmed this brief)</option>
            </select>
          </label>
        </div>
        <p className="form-note">
          {evidenceLevel === 'public_strategy'
            ? 'Weaker signal — treat as a hypothesis about buyer strategy, not verified demand.'
            : 'Stronger signal — a named, accountable buyer contact confirmed this mandate directly.'}
        </p>
        {evidenceLevel === 'buyer_confirmed' && (
          <div className="condition-row__fields">
            <label>
              Confirmed by (buyer contact)
              <input required value={confirmedBy} onChange={(e) => setConfirmedBy(e.target.value)} />
            </label>
            <label>
              Confirmed on
              <input type="datetime-local" required value={lastConfirmedAt} onChange={(e) => setLastConfirmedAt(e.target.value)} />
            </label>
          </div>
        )}
        {(evidenceLevel === 'public_strategy' || identityVerified) && (
          <SourcePicker
            label={identityVerified && evidenceLevel === 'public_strategy' ? 'Source for public strategy and buyer legal identity (required)' : identityVerified ? 'Source for buyer legal identity (required)' : 'Source for this public strategy (required)'}
            want={wantMandateSource} setWant={setWantMandateSource}
            kind={mandateSourceKind} setKind={setMandateSourceKind}
            url={mandateSourceUrl} setUrl={setMandateSourceUrl}
            title={mandateSourceTitle} setTitle={setMandateSourceTitle}
          />
        )}
        <div className="condition-row__fields">
          <label htmlFor="mandate-buyer-company">Existing buyer company (for verified buyer contacts)</label>
          <CompanyPicker id="mandate-buyer-company" value={buyerCompanyId} onChange={(value) => setBuyerCompanyId(String(value))} emptyLabel="No linked company" />
        </div>
        <label className="checkbox-label identity-verify">
          <input type="checkbox" checked={identityVerified} onChange={(e) => { setIdentityVerified(e.target.checked); if (e.target.checked) setWantMandateSource(true) }} />
          I have checked the source and confirm this is the buyer’s legal identity
        </label>
        <p className="form-note">This confirmation is an explicit operator decision. A company name or public acquisition strategy alone does not verify legal identity.</p>
      </div>

      <div className="condition-row">
        <div className="condition-row__head"><label>Criteria — geography &amp; size</label></div>
        <div className="condition-row__fields">
          <label>Countries (comma-separated 2-letter codes, empty = any)
            <input value={countries} onChange={(e) => setCountries(e.target.value)} placeholder="FI, SE" />
          </label>
          <label>Industries (comma-separated, empty = any)
            <input value={industries} onChange={(e) => setIndustries(e.target.value)} placeholder="SaaS, Manufacturing" />
          </label>
        </div>
        <div className="condition-row__fields">
          <label>Financial currency
            <select value={criteria.financial_currency ?? ''} onChange={(e) => setCriteria((c) => ({ ...c, financial_currency: e.target.value || undefined }))}>
              <option value="">Not set</option>
              {CCY_OPTIONS.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
          <label>Revenue min
            <input type="number" min={0} value={criteria.revenue_min ?? ''} onChange={(e) => setCriteria((c) => ({ ...c, revenue_min: e.target.value || undefined }))} />
          </label>
          <label>Revenue max
            <input type="number" min={0} value={criteria.revenue_max ?? ''} onChange={(e) => setCriteria((c) => ({ ...c, revenue_max: e.target.value || undefined }))} />
          </label>
        </div>
      </div>

      <button type="button" className="details-toggle" onClick={() => setAdvanced((v) => !v)}>
        {advanced ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />} More criteria (EBITDA, employees, structure, financing…)
      </button>

      {advanced && (
        <>
          <div className="condition-row">
            <div className="condition-row__fields">
              <label>EBITDA min
                <input type="number" value={criteria.ebitda_min ?? ''} onChange={(e) => setCriteria((c) => ({ ...c, ebitda_min: e.target.value || undefined }))} />
              </label>
              <label>EBITDA max
                <input type="number" value={criteria.ebitda_max ?? ''} onChange={(e) => setCriteria((c) => ({ ...c, ebitda_max: e.target.value || undefined }))} />
              </label>
              <label>Employees min
                <input type="number" min={0} value={criteria.employees_min ?? ''} onChange={(e) => setCriteria((c) => ({ ...c, employees_min: e.target.value ? Number(e.target.value) : undefined }))} />
              </label>
              <label>Employees max
                <input type="number" min={0} value={criteria.employees_max ?? ''} onChange={(e) => setCriteria((c) => ({ ...c, employees_max: e.target.value ? Number(e.target.value) : undefined }))} />
              </label>
            </div>
            <div className="multi-check">
              {STRUCTURES.map((s) => (
                <label key={s}>
                  <input
                    type="checkbox"
                    checked={criteria.structures?.includes(s) ?? false}
                    onChange={(e) => {
                      const cur = new Set(criteria.structures ?? [])
                      if (e.target.checked) cur.add(s); else cur.delete(s)
                      setCriteria((c) => ({ ...c, structures: Array.from(cur) }))
                    }}
                  />
                  {STRUCTURE_LABELS[s]}
                </label>
              ))}
            </div>
            <div className="condition-row__fields">
              <label>Max rollover the buyer accepts (%)
                <input type="number" min={0} max={100} value={criteria.max_rollover_pct ?? ''} onChange={(e) => setCriteria((c) => ({ ...c, max_rollover_pct: e.target.value || undefined }))} />
              </label>
              <label>Must close within (months)
                <input type="number" min={1} max={240} value={criteria.close_within_months ?? ''} onChange={(e) => setCriteria((c) => ({ ...c, close_within_months: e.target.value ? Number(e.target.value) : undefined }))} />
              </label>
              <label>Consideration currency
                <select value={criteria.consideration_currency ?? ''} onChange={(e) => setCriteria((c) => ({ ...c, consideration_currency: e.target.value || undefined }))}>
                  <option value="">Not set</option>
                  {CCY_OPTIONS.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </label>
              <label>Max consideration
                <input type="number" min={0} value={criteria.max_consideration ?? ''} onChange={(e) => setCriteria((c) => ({ ...c, max_consideration: e.target.value || undefined }))} />
              </label>
            </div>
            <div className="condition-row__fields">
              {triState(criteria.control_retention, (v) => setCriteria((c) => ({ ...c, control_retention: v })), 'Accepts owner keeping control')}
              {triState(criteria.site_commitment, (v) => setCriteria((c) => ({ ...c, site_commitment: v })), 'Commits to site retention')}
              {triState(criteria.team_commitment, (v) => setCriteria((c) => ({ ...c, team_commitment: v })), 'Commits to team retention')}
              {triState(criteria.brand_commitment, (v) => setCriteria((c) => ({ ...c, brand_commitment: v })), 'Commits to brand retention')}
            </div>
          </div>

          <div className="condition-row">
            <div className="condition-row__fields">
              <label>Financing status
                <select value={financingStatus} onChange={(e) => setFinancingStatus(e.target.value as Financing)}>
                  <option value="unknown">Unknown (not inferred)</option>
                  <option value="buyer_stated">Buyer stated</option>
                  <option value="evidenced">Evidenced</option>
                </select>
              </label>
              <label>
                Contact name
                <input value={contactName} onChange={(e) => setContactName(e.target.value)} />
              </label>
              <label>
                Advisor
                <input value={advisor} onChange={(e) => setAdvisor(e.target.value)} />
              </label>
            </div>
            {financingStatus === 'evidenced' && (
              <SourcePicker
                label="Source for evidenced financing (required)"
                want={wantFinancingSource} setWant={setWantFinancingSource}
                kind={financingSourceKind} setKind={setFinancingSourceKind}
                url={financingSourceUrl} setUrl={setFinancingSourceUrl}
                title={financingSourceTitle} setTitle={setFinancingSourceTitle}
              />
            )}
          </div>
        </>
      )}

      <div className="dialog__actions">
        <button type="submit" className="btn btn--primary" disabled={saving}>{saving ? 'Saving…' : 'Save mandate'}</button>
      </div>
    </form>
  )
}

function MandatesTab() {
  const { search } = useRouter()
  const requestedMandate = new URLSearchParams(search).get('mandate')
  const [mandates, setMandates] = useState<MandateOut[] | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [activeOnly, setActiveOnly] = useState(true)
  const [detail, setDetail] = useState<MandateDetail | null>(null)

  function load() {
    setStatus('loading')
    setError(null)
    api
      .get<MandateOut[]>(`/api/mandates${activeOnly ? '?active_only=true' : ''}`)
      .then((r) => { setMandates(r); setStatus('ready') })
      .catch((cause) => {
        setError(cause instanceof ApiError ? cause.message : 'Could not load buyer mandates.')
        setStatus('error')
      })
  }
  useEffect(load, [activeOnly])
  useEffect(() => { if (requestedMandate) openDetail(requestedMandate) }, [requestedMandate])

  function openDetail(id: string) {
    api.get<MandateDetail>(`/api/mandates/${id}`).then(setDetail).catch(() => setDetail(null))
  }

  return (
    <section className="deals-section">
      <div className="panel__row">
        <div className="pill-toggle">
          <button type="button" className={activeOnly ? 'active' : ''} onClick={() => setActiveOnly(true)}>Active only</button>
          <button type="button" className={!activeOnly ? 'active' : ''} onClick={() => setActiveOnly(false)}>All</button>
        </div>
        <button type="button" className="btn btn--primary" onClick={() => setShowForm((v) => !v)}>
          <Plus size={14} aria-hidden="true" /> New mandate
        </button>
      </div>

      {showForm && (
        <div className="panel">
          <MandateForm onSaved={() => { setShowForm(false); load() }} />
        </div>
      )}

      {status === 'loading' && <LoadingBlock label="Loading buyer mandates…" />}
      {status === 'error' && <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={load} />}
      {status === 'ready' && (mandates ?? []).length === 0 && (
        <EmptyState title="No buyer mandates yet" description="Record a buyer's criteria to start matching." />
      )}

      {status === 'ready' && mandates && mandates.length > 0 && (
        <div className="list-scroll">
          {mandates.map((m) => (
            <div key={m.id} className="match-card">
              <div className="match-card__head">
                <div>
                  <strong>{m.buyer_name}</strong>{' '}
                  <span className={`badge ${m.evidence_level === 'buyer_confirmed' ? 'badge--pass' : 'badge--unknown'}`}>
                    {m.evidence_level === 'buyer_confirmed' ? 'Verified demand' : 'Public strategy only'}
                  </span>{' '}
                  {!m.active && <span className="badge badge--neutral">Inactive / expired</span>}
                </div>
                <span className="muted small">v{m.version} · expires {formatDate(m.expires_at)}</span>
              </div>
              <div className="muted small">
                {m.criteria.countries?.length ? `Countries: ${m.criteria.countries.join(', ')}` : 'No geography criterion'}
                {m.criteria.industries?.length ? ` · Industries: ${m.criteria.industries.join(', ')}` : ''}
              </div>
              <button type="button" className="details-toggle" onClick={() => openDetail(m.id)}>
                View version history
              </button>
              {detail?.id === m.id && (
                <div className="check-list">
                  {detail.versions.map((v) => (
                    <div key={v.version} className="check-line">
                      <div className="check-line__detail">
                        v{v.version} · changed: {v.changed_fields.join(', ') || 'initial'} · {formatDateTime(v.created_at)}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

// ================================================================== Match runs

function CheckLines({ items }: { items: MatchResultOut['checks'] }) {
  return (
    <div className="check-list">
      {items.map((c, i) => (
        <div key={`${c.key}-${i}`} className="check-line">
          <span className={`badge badge--${c.result}`}>{c.result}</span>
          <div className="check-line__detail">
            <strong>{c.key.replace(/_/g, ' ')}</strong> ({c.origin === 'mandate' ? 'mandate criterion' : 'owner condition'}, {c.strength}) — {c.detail}
            {c.blocking && <span className="badge badge--fail" style={{ marginLeft: 6 }}>blocking</span>}
            {c.citations.length > 0 && <div className="check-line__citations">Source: {c.citations.join(' · ')}</div>}
          </div>
        </div>
      ))}
    </div>
  )
}

function MatchResultCard({ r }: { r: MatchResultOut }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="match-card">
      <div className="match-card__head">
        <div>
          <StatusPill status={r.status} />
          <div className="muted small" style={{ marginTop: 4 }}>{r.explanation.summary}</div>
        </div>
        <div className="match-card__scores">
          <span>Fit: <strong>{r.fit_score != null ? `${Math.round(r.fit_score * 100)}%` : '—'}</strong></span>
          <span>Coverage: <strong>{r.coverage != null ? `${Math.round(r.coverage * 100)}%` : '—'}</strong></span>
        </div>
      </div>
      {r.explanation.questions.length > 0 && (
        <ul className="questions-list">
          {r.explanation.questions.map((q, i) => <li key={i}>{q}</li>)}
        </ul>
      )}
      <p className="muted small">Next: {r.explanation.next_action}</p>
      <button type="button" className="details-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />} {open ? 'Hide' : 'Show'} all checks &amp; reasons
      </button>
      {open && (
        <>
          <CheckLines items={r.checks} />
          {r.explanation.comparables && r.explanation.comparables.length > 0 && (
            <div className="form-note">
              <strong>Prior deals by this buyer (context only, not evidence of current demand):</strong>
              <ul className="questions-list">
                {r.explanation.comparables.map((c) => (
                  <li key={c.id}>{c.target_name} — {c.status} {c.announced_on ? `(${formatDate(c.announced_on)})` : ''} — <a href={c.source_url} target="_blank" rel="noreferrer">source</a></li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function MatchesTab() {
  const { push } = useToast()
  const [companyId, setCompanyId] = useState('')
  const [running, setRunning] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)
  const [detail, setDetail] = useState<MatchRunDetail | null>(null)
  const [runs, setRuns] = useState<MatchRunOut[] | null>(null)
  const [runsStatus, setRunsStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')

  function loadRuns(id: string) {
    if (!id) return
    setRunsStatus('loading')
    api.get<MatchRunOut[]>(`/api/match-runs?company_id=${id}`)
      .then((r) => { setRuns(r); setRunsStatus('ready') })
      .catch(() => setRunsStatus('error'))
  }
  useEffect(() => { if (companyId) loadRuns(companyId); else { setRuns(null); setDetail(null) } }, [companyId])

  async function runMatch() {
    if (!companyId) return
    setRunning(true)
    setRunError(null)
    try {
      const result = await api.post<MatchRunDetail>('/api/match-runs', { company_id: companyId })
      setDetail(result)
      loadRuns(companyId)
      push('Match run completed', 'success')
    } catch (cause) {
      setRunError(cause instanceof ApiError ? cause.message : 'Could not run matching.')
    } finally {
      setRunning(false)
    }
  }

  async function openRun(id: string) {
    try {
      const result = await api.get<MatchRunDetail>(`/api/match-runs/${id}`)
      setDetail(result)
    } catch {
      setRunError('Could not load that match run.')
    }
  }

  return (
    <section className="deals-section">
      <div className="company-picker">
        <label htmlFor="matches-company">Company</label>
        <CompanyPicker id="matches-company" value={companyId} onChange={(value) => setCompanyId(String(value))} />
      </div>

      {companyId && (
        <div className="dialog__actions" style={{ justifyContent: 'flex-start' }}>
          <button type="button" className="btn btn--primary" onClick={runMatch} disabled={running}>
            <GitCompareArrows size={14} aria-hidden="true" /> {running ? 'Running…' : 'Run match against active mandates'}
          </button>
        </div>
      )}
      {runError && <ErrorBlock message={runError} onRetry={runMatch} />}

      {companyId && runsStatus === 'ready' && runs && runs.length > 0 && (
        <div className="two-col">
          <div className="deals-section">
            <h3 style={{ fontSize: 13, margin: 0 }}>Previous runs</h3>
            <div className="list-scroll" style={{ maxHeight: 220 }}>
              {runs.map((r) => (
                <button key={r.id} type="button" className="candidate-list__item" onClick={() => openRun(r.id)}>
                  <span>{formatDateTime(r.created_at)}</span>
                  <span className="muted small">{Object.entries(r.counts).map(([k, v]) => `${k.replace('_', ' ')}: ${v}`).join(' · ')}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {detail && (
        <div className="deals-section">
          <h3 style={{ fontSize: 13, margin: 0 }}>
            Results — {detail.company_id === companyId ? 'this company' : detail.company_id}
            {' · '}<span className="muted small">policy {detail.policy_version} · {formatDateTime(detail.created_at)}</span>
          </h3>
          {!detail.profile_id && (
            <p className="form-note">
              No confirmed owner-conditions version was used — every result will show research needed until one exists.
            </p>
          )}
          {detail.results.length === 0 && <p className="muted small">No active buyer mandates to compare against.</p>}
          <div className="list-scroll">
            {detail.results.map((r) => <MatchResultCard key={r.id} r={r} />)}
          </div>
        </div>
      )}
    </section>
  )
}

// ================================================================== Opportunities

function OutcomeForm({ opportunityId, onSaved }: { opportunityId: string; onSaved: () => void }) {
  const { push } = useToast()
  const [milestone, setMilestone] = useState<Milestone>('reached')
  const [occurredAt, setOccurredAt] = useState('')
  const [response, setResponse] = useState<BuyerResponse | ''>('')
  const [reasonCategory, setReasonCategory] = useState<ReasonCategory | ''>('')
  const [reasonBasis, setReasonBasis] = useState<ReasonBasis>('stated')
  const [evidenceExcerpt, setEvidenceExcerpt] = useState('')
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [wantSource, setWantSource] = useState(false)
  const [sourceKind, setSourceKind] = useState<SourceIn['kind']>('email')
  const [sourceUrl, setSourceUrl] = useState('')
  const [sourceTitle, setSourceTitle] = useState('')

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    if (reasonCategory && milestone !== 'lost') {
      setError('A reason is only recorded when the milestone is "Lost".')
      return
    }
    if (reasonCategory && !evidenceExcerpt && !wantSource) {
      setError('A reason needs an evidence excerpt or a source — no reply is not a reason.')
      return
    }
    setSaving(true)
    try {
      const source_id = await maybeCreateSource(wantSource, sourceKind, sourceUrl, sourceTitle)
      await api.post<OutcomeOut>(`/api/opportunities/${opportunityId}/outcomes`, {
        milestone,
        occurred_at: new Date(occurredAt).toISOString(),
        response: response || undefined,
        reason_category: reasonCategory || undefined,
        reason_basis: reasonCategory ? reasonBasis : undefined,
        evidence_excerpt: evidenceExcerpt || undefined,
        source_id,
        note: note || undefined,
      })
      push('Outcome recorded', 'success')
      onSaved()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : 'Could not record this outcome.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="stack-form" onSubmit={handleSubmit}>
      {error && <p className="field-error" role="alert">{error}</p>}
      <div className="field-row">
        <div className="field-col">
          <label htmlFor="milestone">Milestone reached</label>
          <select id="milestone" value={milestone} onChange={(e) => setMilestone(e.target.value as Milestone)}>
            {Object.entries(MILESTONE_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
        <div className="field-col">
          <label htmlFor="occurred-at">When</label>
          <input id="occurred-at" type="datetime-local" required value={occurredAt} onChange={(e) => setOccurredAt(e.target.value)} />
        </div>
      </div>
      <label htmlFor="response">Buyer response (optional)</label>
      <select id="response" value={response} onChange={(e) => setResponse(e.target.value as BuyerResponse | '')}>
        <option value="">Not recorded</option>
        {Object.entries(BUYER_RESPONSE_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
      </select>

      {milestone === 'lost' && (
        <div className="condition-row">
          <div className="condition-row__fields">
            <label>Reason category
              <select value={reasonCategory} onChange={(e) => setReasonCategory(e.target.value as ReasonCategory | '')}>
                <option value="">None recorded</option>
                {Object.entries(REASON_CATEGORY_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </label>
            {reasonCategory && (
              <label>Basis
                <select value={reasonBasis} onChange={(e) => setReasonBasis(e.target.value as ReasonBasis)}>
                  <option value="stated">Stated by a party</option>
                  <option value="confirmed">Confirmed later</option>
                </select>
              </label>
            )}
          </div>
          {reasonCategory && (
            <>
              <label>Evidence excerpt (exact quote, optional if a source is attached)
                <textarea rows={2} value={evidenceExcerpt} onChange={(e) => setEvidenceExcerpt(e.target.value)} />
              </label>
              <SourcePicker
                label="Attach a source for this reason"
                want={wantSource} setWant={setWantSource}
                kind={sourceKind} setKind={setSourceKind}
                url={sourceUrl} setUrl={setSourceUrl}
                title={sourceTitle} setTitle={setSourceTitle}
              />
            </>
          )}
        </div>
      )}
      <label htmlFor="outcome-note">Note (optional)</label>
      <textarea id="outcome-note" rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
      <div className="dialog__actions">
        <button type="submit" className="btn btn--primary" disabled={saving}>{saving ? 'Recording…' : 'Record outcome'}</button>
      </div>
    </form>
  )
}

function explainApiError(cause: unknown, fallback: string): string {
  if (!(cause instanceof ApiError)) return cause instanceof Error ? cause.message : fallback
  const details = cause.detail
  const reasons = details && typeof details === 'object' && 'reasons' in details && Array.isArray((details as { reasons?: unknown[] }).reasons)
    ? (details as { reasons: unknown[] }).reasons.map(String)
    : []
  return [cause.message, ...reasons].join(' — ')
}

interface EmailDraftOut {
  mail_draft_id: string
  status: string
  recipients: string[]
  subject: string
  body: string
  disclosure: Record<string, unknown>
}

function DraftForm({ opportunityId, onDrafted }: { opportunityId: string; onDrafted: (proposal: DraftOut) => void }) {
  const { push } = useToast()
  const [authorizedBy, setAuthorizedBy] = useState('')
  const [authority, setAuthority] = useState<SpeakerAuthority>('owner')
  const [scope, setScope] = useState<Set<DisclosureField>>(new Set(['industry', 'country']))
  const [statement, setStatement] = useState('')
  const [sourceUrl, setSourceUrl] = useState('')
  const [sourceTitle, setSourceTitle] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    if (scope.size === 0) {
      setError('Select at least one field the owner authorized to disclose.')
      return
    }
    setSaving(true)
    setError(null)
    try {
      if (!authorizedBy.trim() || !statement.trim()) throw new Error('Enter who authorized disclosure and their exact authorization statement.')
      const source_id = await maybeCreateSource(true, 'owner_reported', sourceUrl, sourceTitle)
      const draft = await api.post<DraftOut>(`/api/opportunities/${opportunityId}/drafts`, {
        authorization: {
          authorized_by: authorizedBy,
          authority,
          scope: Array.from(scope),
          statement,
          authorized_at: new Date().toISOString(),
          source_id,
        },
      })
      push('Draft brief created (not sent)', 'success')
      onDrafted(draft)
    } catch (cause) {
      setError(explainApiError(cause, 'Could not create this draft.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="stack-form" onSubmit={handleSubmit}>
      {error && <p className="field-error" role="alert">{error}</p>}
      <p className="form-note">
        This creates a draft brief scoped to exactly what the owner authorizes — nothing is sent to the buyer.
      </p>
      <div className="field-row">
        <div className="field-col">
          <label htmlFor="auth-by">Authorized by</label>
          <input id="auth-by" required value={authorizedBy} onChange={(e) => setAuthorizedBy(e.target.value)} />
        </div>
        <div className="field-col">
          <label htmlFor="auth-authority">Authority</label>
          <select id="auth-authority" value={authority} onChange={(e) => setAuthority(e.target.value as SpeakerAuthority)}>
            <option value="owner">Owner</option>
            <option value="authorized_representative">Authorized representative</option>
          </select>
        </div>
      </div>
      <p className="form-note">Disclosure scope — only these fields may appear in the draft:</p>
      <div className="multi-check">
        {Object.entries(DISCLOSURE_FIELD_LABELS).map(([k, v]) => (
          <label key={k}>
            <input
              type="checkbox"
              checked={scope.has(k as DisclosureField)}
              onChange={(e) => {
                const next = new Set(scope)
                if (e.target.checked) next.add(k as DisclosureField); else next.delete(k as DisclosureField)
                setScope(next)
              }}
            />
            {v}
          </label>
        ))}
      </div>
      <label htmlFor="auth-statement">Authorization statement (exact wording)</label>
      <textarea id="auth-statement" rows={2} required value={statement} onChange={(e) => setStatement(e.target.value)} />
      <div className="field-row">
        <div className="field-col"><label htmlFor="auth-source-url">Authorization source URL (optional)</label><input id="auth-source-url" type="url" value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} placeholder="https://…" /></div>
        <div className="field-col"><label htmlFor="auth-source-title">Authorization source description</label><input id="auth-source-title" required value={sourceTitle} onChange={(e) => setSourceTitle(e.target.value)} placeholder="Owner authorization recorded in a call" /></div>
      </div>
      <p className="form-note">This source records the authorization basis. The source entry does not itself prove consent; use only the exact permission you received.</p>
      <div className="dialog__actions">
        <button type="submit" className="btn btn--primary" disabled={saving}>{saving ? 'Creating…' : 'Create draft brief'}</button>
      </div>
    </form>
  )
}

function EmailDraftForm({ opportunityId, proposal, mandateId }: { opportunityId: string; proposal: DraftOut; mandateId: string }) {
  const [contacts, setContacts] = useState<Contact[]>([])
  const [selectedContact, setSelectedContact] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [mailDraft, setMailDraft] = useState<EmailDraftOut | null>(null)

  useEffect(() => {
    let active = true
    setLoading(true)
    api.get<MandateDetail>(`/api/mandates/${mandateId}`)
      .then((mandate) => {
        if (!mandate.buyer_company_id) throw new Error('Link this mandate to an existing buyer company before choosing a recipient.')
        return api.get<CompanyDetail>(`/api/companies/${mandate.buyer_company_id}`)
      })
      .then((company) => {
        if (!active) return
        const eligible = company.contacts.filter((contact) => contact.contact_role === 'buyer' && contact.verification === 'verified' && Boolean(contact.email))
        setContacts(eligible)
        setSelectedContact(eligible[0]?.id ?? '')
      })
      .catch((cause) => { if (active) setError(explainApiError(cause, 'Could not load verified buyer contacts.')) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [mandateId])

  async function createEmailDraft(event: FormEvent) {
    event.preventDefault()
    if (!selectedContact) return
    setSaving(true)
    setError(null)
    try {
      const result = await api.post<EmailDraftOut>(`/api/opportunities/${opportunityId}/email-draft`, {
        proposal_id: proposal.id,
        contact_id: selectedContact,
      })
      setMailDraft(result)
    } catch (cause) {
      setError(explainApiError(cause, 'Could not create the email draft.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="proposal-review">
      <h4>Authorized proposal</h4>
      <p className="form-note">Proposal saved as a draft. It contains only accepted facts in the scope authorized above.</p>
      <dl className="proposal-review__facts">
        {Object.entries((proposal.payload.company ?? {}) as Record<string, unknown>).map(([key, value]) => (
          <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd></div>
        ))}
      </dl>
      <p className="form-note">{String(proposal.payload.disclaimer ?? '')}</p>
      {!mailDraft ? (
        <form className="stack-form" onSubmit={createEmailDraft}>
          <h4>Create an unapproved email draft</h4>
          {loading && <p className="muted small">Loading buyer contacts…</p>}
          {error && <p className="field-error" role="alert">{error}</p>}
          {!loading && !error && contacts.length === 0 && <p className="form-note">No verified buyer contact with an email is available for this mandate.</p>}
          {!loading && contacts.length > 0 && <label className="field-col">Verified buyer contact
            <select value={selectedContact} onChange={(e) => setSelectedContact(e.target.value)}>
              {contacts.map((contact) => <option key={contact.id} value={contact.id}>{contact.name} · {contact.email}</option>)}
            </select>
          </label>}
          <p className="form-note">Creating a draft does not approve or send it. Review is still required in Outreach.</p>
          <div className="dialog__actions"><button className="btn btn--primary" type="submit" disabled={saving || loading || !selectedContact}>{saving ? 'Creating…' : 'Create email draft'}</button></div>
        </form>
      ) : (
        <section className="mail-draft-preview" aria-label="Unapproved email draft">
          <h4>Email draft · awaiting review</h4>
          <p><strong>To:</strong> {mailDraft.recipients.join(', ')}</p>
          <p><strong>Subject:</strong> {mailDraft.subject}</p>
          <pre>{mailDraft.body}</pre>
          <p className="form-note">Unapproved draft. Nothing has been sent.</p>
          <Link to="/outreach" className="btn btn--secondary">Open in Outreach</Link>
        </section>
      )}
    </div>
  )
}

function OpportunitiesTab() {
  const [companyId, setCompanyId] = useState('')
  const [opportunities, setOpportunities] = useState<OpportunityOut[] | null>(null)
  const [status, setStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [outcomes, setOutcomes] = useState<OutcomeOut[] | null>(null)
  const [showOutcomeForm, setShowOutcomeForm] = useState(false)
  const [showDraftForm, setShowDraftForm] = useState(false)
  const [proposal, setProposal] = useState<DraftOut | null>(null)

  function load() {
    setStatus('loading')
    setError(null)
    api.get<OpportunityOut[]>(`/api/opportunities${companyId ? `?company_id=${companyId}` : ''}`)
      .then((r) => { setOpportunities(r); setStatus('ready') })
      .catch((cause) => {
        setError(cause instanceof ApiError ? cause.message : 'Could not load opportunities.')
        setStatus('error')
      })
  }
  useEffect(load, [companyId])

  function loadOutcomes(id: string) {
    api.get<OutcomeOut[]>(`/api/opportunities/${id}/outcomes`).then(setOutcomes).catch(() => setOutcomes([]))
  }

  return (
    <section className="deals-section">
      <div className="company-picker">
        <label htmlFor="opp-company">Company (optional filter)</label>
        <CompanyPicker id="opp-company" value={companyId} onChange={(value) => setCompanyId(String(value))} emptyLabel="All companies" />
      </div>

      {status === 'loading' && <LoadingBlock label="Loading opportunities…" />}
      {status === 'error' && <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={load} />}
      {status === 'ready' && (opportunities ?? []).length === 0 && (
        <EmptyState title="No opportunities yet" description="Opportunities appear once a match run finds a non-excluded result." />
      )}

      {status === 'ready' && opportunities && opportunities.length > 0 && (
        <div className="list-scroll">
          {opportunities.map((o) => (
            <div key={o.id} className="match-card">
              <div className="match-card__head">
                <div>
                  <strong>{o.buyer_name}</strong> <StatusPill status={o.status} />
                  {o.stale && <span className="badge badge--unknown" style={{ marginLeft: 6 }}>Stale — rerun matching</span>}
                  <div className="muted small">{o.summary}</div>
                </div>
                <span className="muted small">
                  {o.latest_milestone ? MILESTONE_LABELS[o.latest_milestone] : 'No milestone yet'}
                </span>
              </div>
              <div className="dialog__actions" style={{ justifyContent: 'flex-start' }}>
                <button type="button" className="btn btn--secondary" onClick={() => {
                  setSelected(o.id); loadOutcomes(o.id); setShowOutcomeForm(false); setShowDraftForm(false); setProposal(null)
                }}>
                  {selected === o.id ? 'Hide' : 'Outcomes & drafts'}
                </button>
              </div>

              {selected === o.id && (
                <div className="deals-section">
                  <div className="dialog__actions" style={{ justifyContent: 'flex-start' }}>
                    <button type="button" className="btn btn--ghost" onClick={() => setShowOutcomeForm((v) => !v)}>
                      <Plus size={14} aria-hidden="true" /> Record outcome
                    </button>
                    <button
                      type="button"
                      className="btn btn--ghost"
                      onClick={() => setShowDraftForm((v) => !v)}
                      disabled={o.status === 'excluded'}
                    >
                      <ShieldCheck size={14} aria-hidden="true" /> Create draft brief
                    </button>
                  </div>
                  {showOutcomeForm && <OutcomeForm opportunityId={o.id} onSaved={() => { setShowOutcomeForm(false); loadOutcomes(o.id); load() }} />}
                  {showDraftForm && <DraftForm opportunityId={o.id} onDrafted={(created) => { setProposal(created); setShowDraftForm(false) }} />}
                  {proposal && selected === o.id && <EmailDraftForm key={proposal.id} opportunityId={o.id} mandateId={o.mandate_id} proposal={proposal} />}
                  {outcomes && outcomes.length > 0 && (
                    <div className="check-list">
                      {outcomes.map((e) => (
                        <div key={e.id} className="check-line">
                          <div className="check-line__detail">
                            <strong>{MILESTONE_LABELS[e.milestone]}</strong> · {formatDateTime(e.occurred_at)}
                            {e.response && ` · ${BUYER_RESPONSE_LABELS[e.response]}`}
                            {e.reason_category && (
                              <div className="muted small">
                                Reason: {REASON_CATEGORY_LABELS[e.reason_category]} ({e.reason_basis}) — {e.evidence_excerpt ?? 'source attached'}
                              </div>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

// ================================================================== Graph

function hashAngle(id: string, count: number, index: number): number {
  let h = 0
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0
  const jitter = (h % 20) - 10 // deterministic ±10deg jitter, purely from the id
  return (360 / Math.max(count, 1)) * index + jitter
}

function GraphTab() {
  const [companyId, setCompanyId] = useState('')
  const [companyName, setCompanyName] = useState('Company')
  const [detail, setDetail] = useState<MatchRunDetail | null>(null)
  const [status, setStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null)
  const [playing, setPlaying] = useState(false)
  const reducedMotion = useMemo(
    () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches,
    [],
  )

  function load() {
    if (!companyId) return
    setStatus('loading')
    api.get<MatchRunOut[]>(`/api/match-runs?company_id=${companyId}&limit=1`)
      .then((runs) => {
        if (!runs.length) { setDetail(null); setStatus('ready'); return }
        return api.get<MatchRunDetail>(`/api/match-runs/${runs[0].id}`).then((d) => { setDetail(d); setStatus('ready') })
      })
      .catch(() => setStatus('error'))
  }
  useEffect(() => { setDetail(null); setSelectedEdge(null); if (companyId) load() }, [companyId])

  useEffect(() => {
    if (!playing || reducedMotion || !detail || detail.results.length === 0) return
    const ids = detail.results.map((r) => r.mandate_id)
    let i = ids.indexOf(selectedEdge ?? '') // continue from current
    const interval = window.setInterval(() => {
      i = (i + 1) % ids.length
      setSelectedEdge(ids[i])
    }, 2200)
    return () => window.clearInterval(interval)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, reducedMotion, detail])

  const colorFor = { compatible: 'var(--color-teal)', research_needed: 'var(--color-amber)', excluded: 'var(--color-red)' } as const

  const buyerNames = useMemo(() => {
    const snapshotMandates = (detail?.snapshot?.mandates as { id: string; buyer_name: string }[] | undefined) ?? []
    return new Map(snapshotMandates.map((m) => [m.id, m.buyer_name]))
  }, [detail])
  const nameFor = (mandateId: string) => buyerNames.get(mandateId) ?? 'Buyer'

  const cx = 260, cy = 220, r = 150
  const nodes = (detail?.results ?? []).map((res, i) => {
    const angle = (hashAngle(res.mandate_id, detail!.results.length, i) * Math.PI) / 180
    return { res, x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) }
  })
  const selected = detail?.results.find((r) => r.mandate_id === selectedEdge) ?? null

  return (
    <section className="deals-section">
      <div className="company-picker">
        <label htmlFor="graph-company">Company</label>
        <CompanyPicker id="graph-company" value={companyId} onChange={(value) => setCompanyId(String(value))}
          onResolved={(items) => setCompanyName(items[0]?.name ?? 'Company')} />
      </div>

      {status === 'loading' && <LoadingBlock label="Loading latest match run…" />}
      {status === 'error' && <ErrorBlock message="Could not load a match run for this company." onRetry={load} />}
      {status === 'ready' && !detail && companyId && (
        <EmptyState title="No match run yet" description="Run matching for this company from the Match runs tab first." />
      )}

      {detail && detail.results.length > 0 && (
        <div className="deals-graph">
          <div className="deals-graph__toolbar">
            <span className="muted small">Deterministic layout of {companyName} and its buyer mandates. Edge color = match status.</span>
            {!reducedMotion && (
              <button type="button" className="btn btn--ghost" onClick={() => setPlaying((v) => !v)}>
                {playing ? <Pause size={14} aria-hidden="true" /> : <Play size={14} aria-hidden="true" />}
                {playing ? 'Pause replay' : 'Step through matches'}
              </button>
            )}
          </div>
          <svg viewBox="0 0 520 440" width="100%" height="360" role="group" aria-label={`Match graph for ${companyName}`}>
            {nodes.map(({ res, x, y }) => (
              <line
                key={res.mandate_id}
                className={`deals-graph__edge ${playing && selectedEdge === res.mandate_id ? 'deals-graph__edge--playing' : ''} ${selectedEdge && selectedEdge !== res.mandate_id ? 'deals-graph__edge--dim' : ''}`}
                x1={cx} y1={cy} x2={x} y2={y}
                stroke={colorFor[res.status]}
                strokeWidth={selectedEdge === res.mandate_id ? 3 : 1.5}
              />
            ))}
            <circle cx={cx} cy={cy} r={26} fill="var(--color-cobalt)" />
            <text x={cx} y={cy + 4} textAnchor="middle" fontSize={11} fill="#fff">{companyName.slice(0, 10)}</text>
            {nodes.map(({ res, x, y }) => (
              <g
                key={res.mandate_id}
                style={{ cursor: 'pointer' }}
                role="button" tabIndex={0} aria-label={`Inspect match with ${nameFor(res.mandate_id)}`}
                onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setPlaying(false); setSelectedEdge(res.mandate_id) } }}
                onClick={() => { setPlaying(false); setSelectedEdge(res.mandate_id) }}
              >
                <circle cx={x} cy={y} r={18} fill={colorFor[res.status]} opacity={0.85} />
                <text x={x} y={y + 4} textAnchor="middle" fontSize={10} fill="#fff">{nameFor(res.mandate_id).slice(0, 8)}</text>
              </g>
            ))}
          </svg>
          <div className="deals-graph__legend">
            <span><i className="deals-graph__swatch" style={{ background: colorFor.compatible }} /> Potentially compatible</span>
            <span><i className="deals-graph__swatch" style={{ background: colorFor.research_needed }} /> Research needed</span>
            <span><i className="deals-graph__swatch" style={{ background: colorFor.excluded }} /> Excluded</span>
          </div>
          {selected && (
            <div className="deals-graph__evidence">
              <strong>{nameFor(selected.mandate_id)}</strong> — {selected.explanation.summary}
              <CheckLines items={selected.checks} />
            </div>
          )}
        </div>
      )}
    </section>
  )
}

// ================================================================== Historical deals

function DealForm({ onSaved }: { onSaved: () => void }) {
  const { push } = useToast()
  const [form, setForm] = useState<Partial<HistoricalDealIn>>({ status: 'announced', disclosure_rights: 'public' })
  const [asOf, setAsOf] = useState('')
  const [announcedOn, setAnnouncedOn] = useState('')
  const [completedOn, setCompletedOn] = useState('')
  const [withdrawnOn, setWithdrawnOn] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    try {
      const body: HistoricalDealIn = {
        buyer_name: form.buyer_name ?? '',
        target_name: form.target_name ?? '',
        status: (form.status as HistoricalDealIn['status']) ?? 'announced',
        announced_on: announcedOn || undefined,
        completed_on: form.status === 'completed' ? completedOn || undefined : undefined,
        withdrawn_on: form.status === 'withdrawn' ? withdrawnOn || undefined : undefined,
        sector: form.sector || undefined,
        country: form.country || undefined,
        structure: form.structure || undefined,
        stake_pct: form.stake_pct || undefined,
        value_amount: form.value_amount || undefined,
        value_currency: form.value_currency || undefined,
        source_url: form.source_url ?? '',
        source_title: form.source_title || undefined,
        disclosure_rights: (form.disclosure_rights as HistoricalDealIn['disclosure_rights']) ?? 'public',
        as_of: new Date(asOf).toISOString(),
      }
      await api.post<HistoricalDealOut>('/api/historical-deals', body)
      push('Historical deal recorded', 'success')
      onSaved()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not save this deal.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="stack-form" onSubmit={handleSubmit}>
      {error && <p className="field-error" role="alert">{error}</p>}
      <div className="field-row">
        <div className="field-col">
          <label htmlFor="deal-buyer">Buyer name</label>
          <input id="deal-buyer" required value={form.buyer_name ?? ''} onChange={(e) => setForm((f) => ({ ...f, buyer_name: e.target.value }))} />
        </div>
        <div className="field-col">
          <label htmlFor="deal-target">Target name</label>
          <input id="deal-target" required value={form.target_name ?? ''} onChange={(e) => setForm((f) => ({ ...f, target_name: e.target.value }))} />
        </div>
      </div>
      <div className="field-row">
        <div className="field-col">
          <label htmlFor="deal-status">Status</label>
          <select id="deal-status" value={form.status ?? 'announced'} onChange={(e) => setForm((f) => ({ ...f, status: e.target.value as HistoricalDealIn['status'] }))}>
            <option value="announced">Announced</option>
            <option value="completed">Completed</option>
            <option value="withdrawn">Withdrawn</option>
          </select>
        </div>
        <div className="field-col">
          <label htmlFor="deal-announced">Announced on</label>
          <input id="deal-announced" type="date" value={announcedOn} onChange={(e) => setAnnouncedOn(e.target.value)} />
        </div>
      </div>
      {form.status === 'completed' && (
        <label htmlFor="deal-completed">Completed on<input id="deal-completed" type="date" required value={completedOn} onChange={(e) => setCompletedOn(e.target.value)} /></label>
      )}
      {form.status === 'withdrawn' && (
        <label htmlFor="deal-withdrawn">Withdrawn on<input id="deal-withdrawn" type="date" required value={withdrawnOn} onChange={(e) => setWithdrawnOn(e.target.value)} /></label>
      )}
      <div className="field-row">
        <div className="field-col">
          <label htmlFor="deal-sector">Sector</label>
          <input id="deal-sector" value={form.sector ?? ''} onChange={(e) => setForm((f) => ({ ...f, sector: e.target.value }))} />
        </div>
        <div className="field-col">
          <label htmlFor="deal-country">Country</label>
          <input id="deal-country" maxLength={2} value={form.country ?? ''} onChange={(e) => setForm((f) => ({ ...f, country: e.target.value.toUpperCase() }))} />
        </div>
      </div>
      <div className="field-row">
        <div className="field-col">
          <label htmlFor="deal-structure">Structure</label>
          <select id="deal-structure" value={form.structure ?? ''} onChange={(e) => setForm((f) => ({ ...f, structure: (e.target.value || undefined) as Structure | undefined }))}>
            <option value="">Not disclosed</option>
            {STRUCTURES.map((s) => <option key={s} value={s}>{STRUCTURE_LABELS[s]}</option>)}
          </select>
        </div>
        <div className="field-col">
          <label htmlFor="deal-stake">Stake %</label>
          <input id="deal-stake" type="number" min={0} max={100} value={form.stake_pct ?? ''} onChange={(e) => setForm((f) => ({ ...f, stake_pct: e.target.value }))} />
        </div>
      </div>
      <div className="field-row">
        <div className="field-col">
          <label htmlFor="deal-value">Value (leave blank if undisclosed)</label>
          <input id="deal-value" type="number" min={0} value={form.value_amount ?? ''} onChange={(e) => setForm((f) => ({ ...f, value_amount: e.target.value }))} />
        </div>
        <div className="field-col">
          <label htmlFor="deal-currency">Currency</label>
          <select id="deal-currency" value={form.value_currency ?? ''} onChange={(e) => setForm((f) => ({ ...f, value_currency: e.target.value || undefined }))}>
            <option value="">—</option>
            {CCY_OPTIONS.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
      </div>
      <div className="field-row">
        <div className="field-col">
          <label htmlFor="deal-source-url">Source URL</label>
          <input id="deal-source-url" type="url" required value={form.source_url ?? ''} onChange={(e) => setForm((f) => ({ ...f, source_url: e.target.value }))} placeholder="https://…" />
        </div>
        <div className="field-col">
          <label htmlFor="deal-source-title">Source title</label>
          <input id="deal-source-title" value={form.source_title ?? ''} onChange={(e) => setForm((f) => ({ ...f, source_title: e.target.value }))} />
        </div>
      </div>
      <div className="field-row">
        <div className="field-col">
          <label htmlFor="deal-rights">Disclosure rights</label>
          <select id="deal-rights" value={form.disclosure_rights ?? 'public'} onChange={(e) => setForm((f) => ({ ...f, disclosure_rights: e.target.value as HistoricalDealIn['disclosure_rights'] }))}>
            <option value="public">Public</option>
            <option value="licensed">Licensed</option>
            <option value="internal">Internal</option>
          </select>
        </div>
        <div className="field-col">
          <label htmlFor="deal-as-of">Known as of</label>
          <input id="deal-as-of" type="datetime-local" required value={asOf} onChange={(e) => setAsOf(e.target.value)} />
        </div>
      </div>
      <div className="dialog__actions">
        <button type="submit" className="btn btn--primary" disabled={saving}>{saving ? 'Saving…' : 'Save deal'}</button>
      </div>
    </form>
  )
}

function CsvImport({ onDone }: { onDone: () => void }) {
  const { push } = useToast()
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<ImportResult | null>(null)
  const [rowErrors, setRowErrors] = useState<{ row: number; errors: { message: string }[] }[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function handleFile(file: File) {
    setBusy(true)
    setError(null)
    setResult(null)
    setRowErrors(null)
    try {
      const text = await file.text()
      const blob = new Blob([text], { type: 'text/csv' })
      const res = await api.upload<ImportResult>('/api/historical-deals/import', blob)
      setResult(res)
      push(`Imported ${res.imported} deal(s)`, 'success')
      onDone()
    } catch (cause) {
      if (cause instanceof ApiError && Array.isArray(cause.detail)) {
        setRowErrors(cause.detail as { row: number; errors: { message: string }[] }[])
        setError(cause.message)
      } else {
        setError(cause instanceof ApiError ? cause.message : 'Could not import this CSV.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="csv-drop">
      <Upload size={20} aria-hidden="true" />
      <p>Import source-backed historical deals from a CSV matching the historical-deal fields.</p>
      <input
        type="file"
        accept=".csv,text/csv"
        disabled={busy}
        onChange={(e) => { const f = e.target.files?.[0]; if (f) void handleFile(f) }}
      />
      {busy && <LoadingBlock label="Importing…" />}
      {error && <p className="field-error" role="alert">{error}</p>}
      {rowErrors && (
        <div className="csv-errors">
          {rowErrors.map((r) => (
            <div key={r.row}>Row {r.row}: {r.errors.map((e) => e.message).join('; ')}</div>
          ))}
        </div>
      )}
      {result && (
        <p className="muted small">
          Imported {result.imported}. Skipped {result.skipped_duplicate_rows.length} exact duplicate row(s).
        </p>
      )}
    </div>
  )
}

function HistoryTab() {
  const [deals, setDeals] = useState<DealPage | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const [buyerFilter, setBuyerFilter] = useState('')

  function load() {
    setStatus('loading')
    setError(null)
    const params = new URLSearchParams()
    if (buyerFilter) params.set('buyer', buyerFilter)
    api.get<DealPage>(`/api/historical-deals?${params.toString()}`)
      .then((r) => { setDeals(r); setStatus('ready') })
      .catch((cause) => {
        setError(cause instanceof ApiError ? cause.message : 'Could not load historical deals.')
        setStatus('error')
      })
  }
  useEffect(load, [buyerFilter])

  return (
    <section className="deals-section">
      <div className="panel__row">
        <div className="search-field" style={{ maxWidth: 280 }}>
          <input placeholder="Filter by buyer" value={buyerFilter} onChange={(e) => setBuyerFilter(e.target.value)} />
        </div>
        <div className="dialog__actions">
          <button type="button" className="btn btn--secondary" onClick={() => setShowImport((v) => !v)}>
            <Upload size={14} aria-hidden="true" /> Import CSV
          </button>
          <button type="button" className="btn btn--primary" onClick={() => setShowForm((v) => !v)}>
            <Plus size={14} aria-hidden="true" /> Add deal
          </button>
        </div>
      </div>

      {showImport && <div className="panel"><CsvImport onDone={load} /></div>}
      {showForm && <div className="panel"><DealForm onSaved={() => { setShowForm(false); load() }} /></div>}

      {status === 'loading' && <LoadingBlock label="Loading historical deals…" />}
      {status === 'error' && <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={load} />}
      {status === 'ready' && deals && deals.items.length === 0 && (
        <EmptyState title="No historical deals recorded" description="Add a source-backed deal or import a CSV." />
      )}
      {status === 'ready' && deals && deals.items.length > 0 && (
        <div className="list-scroll">
          <p className="muted small">{deals.total} total</p>
          {deals.items.map((d) => (
            <div key={d.id} className="record-list__item">
              <strong>{d.buyer_name} → {d.target_name}</strong>
              <span className="muted small">
                {d.status} · {formatDate(d.announced_on)} · {d.value_amount ? `${d.value_amount} ${d.value_currency}` : 'value undisclosed'}
              </span>
              <p><a href={d.source_url} target="_blank" rel="noreferrer">{d.source_title ?? d.source_url}</a></p>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

// ================================================================== Analytics

function AnalyticsTab() {
  const [data, setData] = useState<DealAnalytics | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')

  function load() {
    setStatus('loading')
    api.get<DealAnalytics>('/api/deals/analytics').then((r) => { setData(r); setStatus('ready') }).catch(() => setStatus('error'))
  }
  useEffect(load, [])

  if (status === 'loading') return <LoadingBlock label="Loading analytics…" />
  if (status === 'error' || !data) return <ErrorBlock message="Could not load analytics." onRetry={load} />

  return (
    <section className="deals-section">
      <div className="analytics-grid">
        <div className="analytics-card">
          <h3>Historical deals</h3>
          <dl>
            <div className="count-row"><span>Total</span><strong>{data.historical_deals.total}</strong></div>
            {Object.entries(data.historical_deals.by_status).map(([k, v]) => (
              <div className="count-row" key={k}><span>{k}</span><strong>{v}</strong></div>
            ))}
            <div className="count-row"><span>Value disclosed</span><strong>{data.historical_deals.value_disclosed}</strong></div>
            <div className="count-row"><span>Value undisclosed</span><strong>{data.historical_deals.value_undisclosed}</strong></div>
          </dl>
        </div>
        <div className="analytics-card">
          <h3>Opportunities</h3>
          <dl>
            <div className="count-row"><span>Total</span><strong>{data.opportunities.total}</strong></div>
            {Object.entries(data.opportunities.by_status).map(([k, v]) => (
              <div className="count-row" key={k}><span>{k.replace('_', ' ')}</span><strong>{v}</strong></div>
            ))}
          </dl>
        </div>
        <div className="analytics-card">
          <h3>Outcomes</h3>
          <dl>
            <div className="count-row"><span>Recorded events</span><strong>{data.outcome_events}</strong></div>
            <div className="count-row"><span>Lost with reason</span><strong>{Object.values(data.failure_reasons.supported).reduce((a, b) => a + b, 0)}</strong></div>
            <div className="count-row"><span>Lost without a stated/confirmed reason</span><strong>{data.failure_reasons.without_supported_reason}</strong></div>
          </dl>
        </div>
      </div>
      <div className="form-note">
        {data.notes.map((n, i) => <p key={i}>{n}</p>)}
      </div>
    </section>
  )
}

// ================================================================== Replay

function ReplayTab() {
  const [asOf, setAsOf] = useState('')
  const [companyId, setCompanyId] = useState('')
  const [result, setResult] = useState<ReplayResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run(event: FormEvent) {
    event.preventDefault()
    if (!asOf) return
    setBusy(true)
    setError(null)
    try {
      const r = await api.post<ReplayResponse>('/api/simulations/replay', {
        as_of: new Date(asOf).toISOString(),
        company_id: companyId || undefined,
      })
      setResult(r)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not run this simulation.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="deals-section">
      <p className="form-note">
        Replays saved match-run snapshots against the current policy, as they would have looked at a past date.
        Only uses saved snapshots and recorded outcomes/deals known by that date — never synthetic history.
      </p>
      <form className="field-row" onSubmit={run}>
        <div className="field-col">
          <label htmlFor="replay-as-of">As of</label>
          <input id="replay-as-of" type="datetime-local" required value={asOf} onChange={(e) => setAsOf(e.target.value)} />
        </div>
        <div className="field-col">
          <label htmlFor="replay-company">Company (optional)</label>
          <CompanyPicker id="replay-company" value={companyId} onChange={(value) => setCompanyId(String(value))} emptyLabel="All companies" />
        </div>
        <div className="field-col" style={{ alignSelf: 'flex-end' }}>
          <button type="submit" className="btn btn--primary" disabled={busy}>
            <RefreshCw size={14} aria-hidden="true" /> {busy ? 'Simulating…' : 'Run simulation'}
          </button>
        </div>
      </form>
      {error && <ErrorBlock message={error} />}
      {result && result.status === 'unavailable' && (
        <EmptyState title="Simulation unavailable" description={(result.reasons ?? []).join(' ') || 'Not enough data to replay this date.'} />
      )}
      {result && result.status === 'completed' && (
        <div className="deals-section">
          <p className="muted small">
            {Object.entries(result.counts).map(([k, v]) => `${k.replace('_', ' ')}: ${v}`).join(' · ')}
          </p>
          {(result.runs ?? []).map((run) => (
            <div key={run.run_id} className="match-card">
              <strong>Run at {formatDateTime(run.created_at)}</strong>
              <div className="check-list">
                {run.results.map((item) => (
                  <div key={item.mandate_id} className="check-line">
                    <div className="check-line__detail">
                      <StatusPill status={item.saved_status} /> → <StatusPill status={item.replayed_status} />
                      {item.changed && <span className="badge badge--unknown" style={{ marginLeft: 6 }}>Changed under current policy</span>}
                      {item.observed_milestones.length > 0 && (
                        <div className="muted small">
                          Observed since: {item.observed_milestones.map((m) => `${MILESTONE_LABELS[m.milestone]} (${formatDate(m.occurred_at)})`).join(', ')}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
          {(result.notes ?? []).map((n, i) => <p key={i} className="form-note">{n}</p>)}
        </div>
      )}
    </section>
  )
}
