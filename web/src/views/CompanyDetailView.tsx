import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { ArrowLeft, ExternalLink, Pencil, X } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { CompanyDetail, CompanyDraft, Contact, Evidence, Financial } from '../lib/types'
import { LoadingBlock, ErrorBlock } from '../components/StateViews'
import { SellerIntentBadge, SELLER_INTENT_OPTIONS } from '../components/SellerIntentBadge'
import { Link } from '../lib/router'
import { useToast } from '../lib/toast'
import { DocumentsPanel } from '../components/DocumentsPanel'
import './company.css'

type Tab = 'Overview' | 'Financials' | 'People' | 'Evidence' | 'Activity'
const tabs: Tab[] = ['Overview', 'Financials', 'People', 'Evidence', 'Activity']
const message = (error: unknown, fallback: string) => error instanceof ApiError
  ? `${error.message}${error.detail ? ` — ${typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail)}` : ''}`
  : error instanceof Error ? error.message : fallback
const readForm = (form: HTMLFormElement) => Object.fromEntries(new FormData(form).entries())

export function CompanyDetailView({ id }: { id: string }) {
  const [company, setCompany] = useState<CompanyDetail | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<CompanyDraft>({})
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('Overview')
  const { push } = useToast()

  const load = useCallback(() => {
    setStatus('loading'); setError(null)
    api.get<CompanyDetail>(`/api/companies/${id}`).then((result) => { setCompany(result); setStatus('ready') })
      .catch((cause) => { setError(message(cause, 'Could not load this company.')); setStatus('error') })
  }, [id])
  useEffect(() => { load() }, [load])

  function startEditing() {
    if (!company) return
    setDraft({ name: company.name, website: company.website ?? '', country: company.country ?? '', industry: company.industry ?? '', description: company.description ?? '' })
    setFormError(null); setEditing(true)
  }
  async function handleSave(event: FormEvent) {
    event.preventDefault(); if (!company) return
    setSaving(true); setFormError(null)
    try { const updated = await api.patch<CompanyDetail>(`/api/companies/${company.id}`, draft); setCompany((current) => current ? { ...current, ...updated } : updated); setEditing(false); push('Saved changes', 'success') }
    catch (cause) { const text = message(cause, 'Could not save changes.'); setFormError(text); push(text, 'error') }
    finally { setSaving(false) }
  }

  async function submit(event: FormEvent<HTMLFormElement>, action: (data: Record<string, FormDataEntryValue>) => Promise<unknown>, success: string) {
    event.preventDefault()
    const formElement = event.currentTarget
    const values = readForm(formElement)
    setSaving(true); setFormError(null)
    try { await action(values); formElement.reset(); load(); push(success, 'success') }
    catch (cause) { const text = message(cause, 'The request could not be completed.'); setFormError(text); push(text, 'error') }
    finally { setSaving(false) }
  }

  async function addSource(data: Record<string, FormDataEntryValue>) {
    return api.post<{ id: string }>('/api/sources', { kind: data.source_kind, url: data.source_url || null, title: data.source_title || null, publisher: data.publisher || null })
  }
  async function submitObservation(event: FormEvent<HTMLFormElement>, kind: 'evidence' | 'financial') {
    event.preventDefault()
    const formElement = event.currentTarget
    const data = readForm(formElement)
    setSaving(true); setFormError(null)
    try {
      const source = await addSource(data)
      if (kind === 'evidence') await api.post(`/api/companies/${id}/evidence`, { source_id: source.id, field: data.field, value: data.value || null, excerpt: data.excerpt, extraction_method: 'manual' })
      else await api.post(`/api/companies/${id}/financials`, { source_id: source.id, metric: data.metric, amount: data.amount === '' ? null : data.amount, currency: data.currency || null, period_start: data.period_start, period_end: data.period_end, scope: data.scope, status: data.financial_status })
      formElement.reset(); load(); push(kind === 'evidence' ? 'Evidence added' : 'Financial observation added', 'success')
    } catch (cause) { const text = message(cause, 'The observation could not be saved.'); setFormError(text); push(text, 'error') }
    finally { setSaving(false) }
  }

  async function review(kind: 'evidence' | 'financial', item: Evidence | Financial, decision: 'accepted' | 'rejected') {
    const reason = window.prompt(`Why are you marking this ${decision}?`)
    if (!reason || reason.trim().length < 3) return
    try { await api.post(`/api/${kind === 'evidence' ? 'evidence' : 'financials'}/${item.id}/review`, { status: decision, reason: reason.trim() }); load() }
    catch (cause) { push(message(cause, 'Could not update the review.'), 'error') }
  }

  return <div className="page company-detail">
    <Link to="/companies" className="back-link"><ArrowLeft size={14} aria-hidden="true" /> Companies</Link>
    {status === 'loading' && <LoadingBlock label="Loading company…" />}
    {status === 'error' && <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={load} />}
    {status === 'ready' && company && <>
      <header className="detail-header"><div><h1>{company.name || 'Unnamed company'}</h1><div className="detail-header__meta">
        {company.website && <a href={company.website} target="_blank" rel="noreferrer">{company.website}<ExternalLink size={12} aria-hidden="true" /></a>}{company.country && <span>{company.country}</span>}{company.industry && <span>{company.industry}</span>}
      </div></div><button type="button" className="btn btn--secondary" onClick={startEditing}><Pencil size={14} aria-hidden="true" /> Edit</button></header>
      {!editing ? <>
        <div className="company-summary"><span className="company-summary__status">{company.status === 'confirmed' ? 'Identity confirmed' : 'Identity provisional'}</span><SellerIntentBadge intent={company.seller_intent ?? 'unknown'} /><Link to={`/futures?company=${id}`} className="btn btn--secondary">Research &amp; explore futures</Link></div>
        <nav className="company-tabs" aria-label="Company sections">{tabs.map((item) => <button type="button" key={item} className={tab === item ? 'is-active' : ''} aria-current={tab === item ? 'page' : undefined} onClick={() => setTab(item)}>{item}</button>)}</nav>
        {formError && <p className="field-error" role="alert">{formError}</p>}
        {tab === 'Overview' && <div className="company-panels">
          <section className="panel"><h2>Company overview</h2>{company.description ? <p className="detail-description">{company.description}</p> : <p className="muted">No description recorded.</p>}<dl className="detail-grid"><div><dt>Business IDs</dt><dd>{company.identifiers.length ? company.identifiers.map((item) => `${item.jurisdiction} ${item.value}`).join(', ') : 'Not recorded'}</dd></div><div><dt>Registry status</dt><dd>{company.registry_status ?? 'Not recorded'}</dd></div></dl>
            {company.status !== 'confirmed' && <form className="identity-form" onSubmit={(e) => submit(e, async (data) => { if (data.confirm_identity !== 'on') throw new Error('Confirm the legal identity checkbox first'); await api.patch(`/api/companies/${id}`, { status: 'confirmed', confirmation_basis: data.basis }) }, 'Company identity confirmed')}><label className="checkbox-label"><input name="confirm_identity" type="checkbox" required /> I verified this is the correct legal entity</label><label htmlFor="identity-basis">Evidence basis</label><textarea id="identity-basis" name="basis" required minLength={5} placeholder="Registry entry, jurisdiction, or other basis" /><button className="btn btn--secondary" disabled={saving}>Confirm identity</button></form>}
          </section>
          <section className="panel"><h2>Research jobs</h2>{company.jobs.length ? company.jobs.map((job) => <div className="record-list__item" key={job.id}><strong>{job.kind === 'company.enrich' ? 'Company enrichment' : job.kind.replace(/[._]/g, ' ')}</strong><span className="muted small">{job.state} · {job.attempts} attempts</span>{job.last_error && <p>{job.last_error}</p>}</div>) : <p className="muted">No research activity yet.</p>}</section>
          <section className="panel"><h2>Confirm owner statement</h2><p className="muted small">Seller intent changes only when an attributable statement is confirmed.</p><details><summary>Record a statement</summary><form className="stack-form" onSubmit={(e) => submit(e, (d) => api.post(`/api/companies/${id}/intent-statements`, { speaker_name: d.speaker_name, speaker_authority: d.speaker_authority, stance: d.stance, statement: d.statement, stated_at: new Date(String(d.stated_at)).toISOString(), confirmed: d.confirmed === 'on' }), 'Intent statement recorded')}>
            <label htmlFor="speaker-name">Speaker name</label><input id="speaker-name" name="speaker_name" required /><label htmlFor="speaker-authority">Authority</label><select id="speaker-authority" name="speaker_authority"><option value="owner">Owner</option><option value="authorized_representative">Authorized representative</option><option value="unverified">Unverified</option></select><label htmlFor="statement-stance">Stance</label><select id="statement-stance" name="stance">{SELLER_INTENT_OPTIONS.filter((x) => x !== 'unknown').map((x) => <option key={x} value={x}>{x.replace('_', ' ')}</option>)}</select><label htmlFor="stated-at">When</label><input id="stated-at" name="stated_at" type="datetime-local" required /><label htmlFor="statement">Exact quote</label><textarea id="statement" name="statement" rows={3} required /><label className="checkbox-label"><input name="confirmed" type="checkbox" /> Confirm attributable owner statement</label><button className="btn btn--primary" disabled={saving}>{saving ? 'Recording…' : 'Record statement'}</button></form></details>
            {company.intent_statements.map((item) => <div className="record-list__item" key={item.id}><strong>{item.speaker_name}</strong><span className="muted small">{item.stance.replace('_', ' ')} · {item.confirmed ? 'confirmed' : 'unconfirmed'}</span><p>“{item.statement}”</p></div>)}
          </section></div>}
        {tab === 'Financials' && <><DocumentsPanel companyId={id} onUpdated={load} /><section className="panel"><h2>Financial observations</h2>{company.financials.map((item) => <article className="record-list__item" key={item.id}><strong>{item.metric.replace('_', ' ')}</strong><span className="muted small">{item.period_start} – {item.period_end} · {item.scope} · {item.status} · {item.review_status}</span><p>{item.amount === null ? item.status === 'not_disclosed' ? 'Not disclosed' : 'Not applicable' : `${item.amount}${item.currency ? ` ${item.currency}` : ''}`}{item.source.url && <> · <a href={item.source.url} target="_blank" rel="noreferrer">{item.source.title ?? item.source.url}</a></>}</p>{item.review_status === 'proposed' && <ReviewButtons onReview={(d) => review('financial', item, d)} />}</article>)}{!company.financials.length && <p className="muted">No financials recorded. Missing data is not zero.</p>}<details><summary>Add financial observation</summary><FinancialForm saving={saving} onSubmit={(e) => submitObservation(e, 'financial')} /></details></section></>}
        {tab === 'People' && <section className="panel"><h2>People</h2>{company.contacts.map((person) => <ContactCard key={person.id} person={person} saving={saving} onAction={async (action, data) => { try { await action(data); load() } catch (cause) { push(message(cause, 'Contact update failed.'), 'error') } }} />)}{!company.contacts.length && <p className="muted">No people recorded.</p>}<details><summary>Add person</summary><ContactForm saving={saving} onSubmit={(e) => submit(e, (d) => api.post(`/api/companies/${id}/contacts`, d), 'Contact added')} /></details></section>}
        {tab === 'Evidence' && <section className="panel"><h2>Evidence</h2>{company.evidence.map((item) => <article className="record-list__item" key={item.id}><strong>{item.field.replace(/[._]/g, ' ')}</strong><span className="muted small">{item.review_status} · {item.source.title ?? item.source.kind}</span><p>{item.excerpt}</p>{item.source.url && <a href={item.source.url} target="_blank" rel="noreferrer">Open source <ExternalLink size={12} /></a>}{item.review_status === 'proposed' && <ReviewButtons onReview={(d) => review('evidence', item, d)} />}</article>)}{!company.evidence.length && <p className="muted">No evidence recorded.</p>}<details><summary>Add evidence</summary><EvidenceForm saving={saving} onSubmit={(e) => submitObservation(e, 'evidence')} /></details></section>}
        {tab === 'Activity' && <section className="panel"><h2>Activity</h2>{company.activities.map((event) => <div className="record-list__item" key={event.id}><strong>{event.summary}</strong><span className="muted small">{event.actor} · {new Date(event.created_at).toLocaleString()}</span></div>)}{!company.activities.length && <p className="muted">No activity recorded.</p>}</section>}
      </> : <form className="panel" onSubmit={handleSave}><div className="panel__row"><h2>Edit company</h2><button type="button" className="icon-button" onClick={() => setEditing(false)} aria-label="Cancel"><X size={16} /></button></div>{formError && <p className="field-error" role="alert">{formError}</p>}<label htmlFor="edit-name">Name</label><input id="edit-name" value={draft.name ?? ''} onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))} /><label htmlFor="edit-website">Website</label><input id="edit-website" value={draft.website ?? ''} onChange={(e) => setDraft((d) => ({ ...d, website: e.target.value }))} /><label htmlFor="edit-country">Country</label><input id="edit-country" value={draft.country ?? ''} onChange={(e) => setDraft((d) => ({ ...d, country: e.target.value }))} /><label htmlFor="edit-industry">Industry</label><input id="edit-industry" value={draft.industry ?? ''} onChange={(e) => setDraft((d) => ({ ...d, industry: e.target.value }))} /><label htmlFor="edit-description">Description</label><textarea id="edit-description" rows={4} value={draft.description ?? ''} onChange={(e) => setDraft((d) => ({ ...d, description: e.target.value }))} /><div className="dialog__actions"><button type="button" className="btn btn--ghost" onClick={() => setEditing(false)}>Cancel</button><button className="btn btn--primary" disabled={saving}>{saving ? 'Saving…' : 'Save changes'}</button></div></form>}
    </>}
  </div>
}

function ReviewButtons({ onReview }: { onReview: (decision: 'accepted' | 'rejected') => void }) { return <div className="review-actions"><button type="button" className="btn btn--secondary" onClick={() => onReview('accepted')}>Accept</button><button type="button" className="btn btn--ghost" onClick={() => onReview('rejected')}>Reject</button></div> }
function ContactCard({ person, saving, onAction }: { person: Contact; saving: boolean; onAction: (action: (data: Record<string, FormDataEntryValue>) => Promise<unknown>, data: Record<string, FormDataEntryValue>) => void }) {
  return <article className="record-list__item"><strong>{person.name}</strong><span className="muted small">{person.title ?? person.person_role.replace('_', ' ')} · {person.verification}</span><p>{person.email ?? person.phone ?? 'No contact channel recorded'}</p><details><summary>Edit / verify</summary><form className="stack-form" onSubmit={(e) => { e.preventDefault(); const form = e.currentTarget; const d = readForm(form); onAction((v) => api.patch(`/api/contacts/${person.id}`, v), d) }}><label>Name</label><input name="name" defaultValue={person.name} required /><label>Title</label><input name="title" defaultValue={person.title ?? ''} /><label>Email</label><input name="email" type="email" defaultValue={person.email ?? ''} /><label>Phone</label><input name="phone" defaultValue={person.phone ?? ''} /><button className="btn btn--secondary" disabled={saving}>Save contact</button></form>{person.verification !== 'verified' && <form className="stack-form" onSubmit={(e) => { e.preventDefault(); const form = e.currentTarget; onAction((v) => api.post(`/api/contacts/${person.id}/verify`, v), readForm(form)) }}><label>Verification basis</label><input name="basis" required minLength={5} /><button className="btn btn--secondary" disabled={saving}>Mark verified</button></form>}</details></article>
}
function ContactForm({ saving, onSubmit }: { saving: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) { return <form className="stack-form" onSubmit={onSubmit}><label>Name</label><input name="name" required /><label>Title</label><input name="title" /><label>Role</label><select name="person_role"><option>owner</option><option>founder</option><option>director</option><option>executive</option><option>representative</option><option>employee</option><option>other</option></select><label>Contact type</label><select name="contact_role"><option value="seller">Seller</option><option value="buyer">Buyer</option></select><label>Email</label><input name="email" type="email" /><label>Phone</label><input name="phone" /><button className="btn btn--primary" disabled={saving}>Add person</button></form> }
function SourceFields() { return <><label>Source type</label><select name="source_kind"><option value="manual">Manual note</option><option value="registry">Registry</option><option value="website">Website</option><option value="document">Document</option><option value="owner_reported">Owner reported</option></select><label>Source title</label><input name="source_title" required /><label>Source URL</label><input name="source_url" type="url" placeholder="https://…" /><label>Publisher</label><input name="publisher" /></> }
function EvidenceForm({ saving, onSubmit }: { saving: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) { return <form className="stack-form" onSubmit={onSubmit}><label>Field</label><input name="field" placeholder="website, employees" pattern="[a-z][a-z0-9_.]{0,63}" required /><label>Observed value</label><input name="value" /><label>Exact supporting excerpt</label><textarea name="excerpt" required /><SourceFields /><button className="btn btn--primary" disabled={saving}>Add evidence</button></form> }
function FinancialForm({ saving, onSubmit }: { saving: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) { return <form className="stack-form" onSubmit={onSubmit}><label>Metric</label><select name="metric">{['revenue','ebitda','ebit','net_income','cash','debt','equity','employees'].map((x) => <option key={x}>{x}</option>)}</select><label>Amount (leave blank only when not disclosed or not applicable)</label><input name="amount" type="number" step="any" /><label>Currency</label><input name="currency" maxLength={3} placeholder="EUR" /><label>Period start</label><input type="date" name="period_start" required /><label>Period end</label><input type="date" name="period_end" required /><label>Scope</label><select name="scope"><option value="entity">Entity</option><option value="consolidated">Consolidated</option></select><label>Basis</label><select name="financial_status"><option value="reported">Reported</option><option value="derived">Derived</option><option value="estimated">Estimated</option><option value="not_disclosed">Not disclosed</option><option value="not_applicable">Not applicable</option></select><SourceFields /><button className="btn btn--primary" disabled={saving}>Add observation</button></form> }
