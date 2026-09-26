import { Children, useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { ArrowLeft, ExternalLink, Pencil, X, Plus } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { CompanyDetail, CompanyDraft } from '../lib/types'
import { LoadingBlock, ErrorBlock } from '../components/StateViews'
import { SellerIntentBadge, SELLER_INTENT_OPTIONS } from '../components/SellerIntentBadge'
import { Link } from '../lib/router'
import { useToast } from '../lib/toast'

export function CompanyDetailView({ id }: { id: string }) {
  const [company, setCompany] = useState<CompanyDetail | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<CompanyDraft>({})
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const { push } = useToast()

  function load() {
    setStatus('loading')
    setError(null)
    api
      .get<CompanyDetail>(`/api/companies/${id}`)
      .then((result) => {
        setCompany(result)
        setStatus('ready')
      })
      .catch((cause) => {
        setError(cause instanceof ApiError ? cause.message : 'Could not load this company.')
        setStatus('error')
      })
  }

  useEffect(load, [id])

  function startEditing() {
    if (!company) return
    setDraft({
      name: company.name,
      website: company.website ?? '',
      country: company.country ?? '',
      industry: company.industry ?? '',
      description: company.description ?? '',
    })
    setFormError(null)
    setEditing(true)
  }

  async function handleSave(event: FormEvent) {
    event.preventDefault()
    if (!company) return
    setSaving(true)
    setFormError(null)
    try {
      const updated = await api.patch<CompanyDetail>(`/api/companies/${company.id}`, draft)
      setCompany((current) => current ? { ...current, ...updated } : updated)
      setEditing(false)
      push('Saved changes', 'success')
    } catch (cause) {
      const message = cause instanceof ApiError ? cause.message : 'Could not save changes.'
      setFormError(message)
      push(message, 'error')
    } finally {
      setSaving(false)
    }
  }

  async function handleIntent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    setSaving(true)
    setFormError(null)
    try {
      await api.post<CompanyDetail['intent_statements'][number]>(
        `/api/companies/${id}/intent-statements`,
        {
          speaker_name: form.get('speaker_name'),
          speaker_authority: form.get('speaker_authority'),
          stance: form.get('stance'),
          statement: form.get('statement'),
          stated_at: new Date(String(form.get('stated_at'))).toISOString(),
          confirmed: form.get('confirmed') === 'on',
        },
      )
      formElement.reset()
      load()
      push('Intent statement recorded', 'success')
    } catch (cause) {
      const message = cause instanceof ApiError ? cause.message : 'Could not record the statement.'
      setFormError(message)
      push(message, 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="page">
      <Link to="/companies" className="back-link">
        <ArrowLeft size={14} aria-hidden="true" />
        Companies
      </Link>

      {status === 'loading' && <LoadingBlock label="Loading company…" />}
      {status === 'error' && <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={load} />}

      {status === 'ready' && company && !editing && (
        <>
          <header className="detail-header">
            <div>
              <h1>{company.name || 'Unnamed company'}</h1>
              <div className="detail-header__meta">
                {company.website && (
                  <a href={company.website} target="_blank" rel="noreferrer">
                    {company.website}
                    <ExternalLink size={12} aria-hidden="true" />
                  </a>
                )}
                {company.country && <span>{company.country}</span>}
                {company.industry && <span>{company.industry}</span>}
              </div>
            </div>
            <button type="button" className="btn btn--secondary" onClick={startEditing}>
              <Pencil size={14} aria-hidden="true" />
              Edit
            </button>
          </header>

          <section className="panel">
            <div className="panel__row">
            <h2>Seller intent</h2>
              <SellerIntentBadge intent={company.seller_intent ?? 'unknown'} />
            </div>
          </section>

          <section className="panel">
            <h2>Overview</h2>
            {company.description ? (
              <p className="detail-description">{company.description}</p>
            ) : (
            <p className="muted">No description yet. Add one from Edit.</p>
            )}
          </section>

          <section className="panel">
            <h2>Record</h2>
            <dl className="detail-grid">
              <div>
                <dt>Business IDs</dt>
                <dd>{company.identifiers.length ? company.identifiers.map((item) => `${item.jurisdiction} ${item.value}`).join(', ') : '—'}</dd>
              </div>
              <div>
                <dt>Company ID</dt>
                <dd className="mono">{company.id}</dd>
              </div>
            </dl>
          </section>

          <section className="panel">
            <div className="panel__row"><h2>Confirm owner statement</h2><Plus size={16} aria-hidden="true" /></div>
            <p className="muted small">Seller intent changes only when an attributable statement is confirmed.</p>
            {formError && <p className="field-error" role="alert">{formError}</p>}
            <form className="stack-form" onSubmit={handleIntent}>
              <div className="field-row">
                <div className="field-col"><label htmlFor="speaker-name">Speaker name</label><input id="speaker-name" name="speaker_name" required /></div>
                <div className="field-col"><label htmlFor="speaker-authority">Authority</label>
                  <select id="speaker-authority" name="speaker_authority" defaultValue="owner">
                    <option value="owner">Owner</option>
                    <option value="authorized_representative">Authorized representative</option>
                    <option value="unverified">Unverified</option>
                  </select>
                </div>
              </div>
              <div className="field-row">
                <div className="field-col"><label htmlFor="statement-stance">Stance</label>
                  <select id="statement-stance" name="stance" defaultValue="interested">
                    {SELLER_INTENT_OPTIONS.filter((option) => option !== 'unknown').map((option) => <option key={option} value={option}>{option.replace('_', ' ')}</option>)}
                  </select>
                </div>
                <div className="field-col"><label htmlFor="stated-at">When</label><input id="stated-at" name="stated_at" type="datetime-local" required /></div>
              </div>
              <label htmlFor="statement">Exact quote</label>
              <textarea id="statement" name="statement" rows={3} required placeholder="Enter the exact words or message span." />
              <label className="checkbox-label"><input name="confirmed" type="checkbox" /> Confirm this statement is attributable</label>
              <button type="submit" className="btn btn--primary" disabled={saving}>{saving ? 'Recording…' : 'Record statement'}</button>
            </form>
            {company.intent_statements.length > 0 && <div className="record-list">{company.intent_statements.map((item) => <div className="record-list__item" key={item.id}><strong>{item.speaker_name}</strong><span className="muted small">{item.stance.replace('_', ' ')} · {item.confirmed ? 'confirmed' : 'unconfirmed'}</span><p>“{item.statement}”</p></div>)}</div>}
          </section>

          <DetailCollection title="Contacts">
            {company.contacts.map((contact) => <div className="record-list__item" key={contact.id}><strong>{contact.name}</strong><span className="muted small">{contact.title ?? contact.person_role}</span><p>{contact.email ?? contact.phone ?? 'No contact channel recorded'}</p></div>)}
          </DetailCollection>
          <DetailCollection title="Evidence">
            {company.evidence.map((item) => <div className="record-list__item" key={item.id}><strong>{item.field}</strong><span className="muted small">{item.review_status} · {item.source.title ?? item.source.kind}</span><p>{item.excerpt}</p></div>)}
          </DetailCollection>
          <DetailCollection title="Financials">
            {company.financials.map((item) => <div className="record-list__item" key={item.id}><strong>{item.metric}</strong><span className="muted small">{item.period_start} — {item.period_end} · {item.status}</span><p>{item.amount ?? 'Not disclosed'}{item.currency ? ` ${item.currency}` : ''}</p></div>)}
          </DetailCollection>
          <DetailCollection title="Research jobs">
            {company.jobs.map((job) => <div className="record-list__item" key={job.id}><strong>{job.kind}</strong><span className="muted small">{job.state} · {job.attempts} attempts</span><p>{job.last_error ?? 'No errors reported'}</p></div>)}
          </DetailCollection>
        </>
      )}

      {status === 'ready' && company && editing && (
        <form className="panel" onSubmit={handleSave}>
          <div className="panel__row">
            <h2>Edit company</h2>
            <button type="button" className="icon-button" onClick={() => setEditing(false)} aria-label="Cancel">
              <X size={16} aria-hidden="true" />
            </button>
          </div>
          {formError && <p className="field-error" role="alert">{formError}</p>}

          <label htmlFor="edit-name">Name</label>
          <input
            id="edit-name"
            value={draft.name ?? ''}
            onChange={(event) => setDraft((d) => ({ ...d, name: event.target.value }))}
          />

          <label htmlFor="edit-website">Website</label>
          <input
            id="edit-website"
            value={draft.website ?? ''}
            onChange={(event) => setDraft((d) => ({ ...d, website: event.target.value }))}
          />

          <div className="field-row">
            <div className="field-col">
              <label htmlFor="edit-country">Country</label>
              <input
                id="edit-country"
                value={draft.country ?? ''}
                onChange={(event) => setDraft((d) => ({ ...d, country: event.target.value }))}
              />
            </div>
          </div>

          <label htmlFor="edit-industry">Industry</label>
          <input
            id="edit-industry"
            value={draft.industry ?? ''}
            onChange={(event) => setDraft((d) => ({ ...d, industry: event.target.value }))}
          />

          <label htmlFor="edit-description">Description</label>
          <textarea
            id="edit-description"
            rows={4}
            value={draft.description ?? ''}
            onChange={(event) => setDraft((d) => ({ ...d, description: event.target.value }))}
          />

          <div className="dialog__actions">
            <button type="button" className="btn btn--ghost" onClick={() => setEditing(false)} disabled={saving}>
              Cancel
            </button>
            <button type="submit" className="btn btn--primary" disabled={saving}>
              {saving ? 'Saving…' : 'Save changes'}
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

function DetailCollection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="panel">
      <h2>{title}</h2>
      {Children.count(children) > 0 ? children : <p className="muted">Nothing recorded yet.</p>}
    </section>
  )
}
