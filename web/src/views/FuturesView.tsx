// Owner journey: pick a company, capture the conditions that would let the
// owner consider a sale, save a proposed profile, get an explicit owner
// confirmation of the *exact* version, then explore alternatives ("futures")
// that adjust a condition without touching the confirmed original.
//
// Routes used (backend/permetheus/deals.py, exact — nothing guessed):
//   GET  /api/companies
//   GET  /api/companies/{company_id}/preferences
//   POST /api/companies/{company_id}/preferences
//   POST /api/preferences/{profile_id}/confirm
//   POST /api/scenarios
//   GET  /api/scenarios?company_id=...
//   POST /api/sources                 (only when the user attaches a source)

import { useEffect, useState, type FormEvent } from 'react'
import { Compass, Plus, ShieldCheck, FlaskConical, ChevronDown, ChevronRight } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { Company } from '../lib/types'
import { CompanyPicker } from '../components/CompanyPicker'
import { LoadingBlock, ErrorBlock, EmptyState } from '../components/StateViews'
import { useToast } from '../lib/toast'
import {
  CONDITION_KINDS,
  CONDITION_LABELS,
  STRUCTURE_LABELS,
  formatDateTime,
  type Condition,
  type ConditionKind,
  type FactOverrides,
  type PreferenceList,
  type PreferenceOut,
  type ScenarioOut,
  type SourceIn,
  type SourceOut,
  type SpeakerAuthority,
  type Structure,
} from '../lib/dealsTypes'
import './deals.css'

const STRUCTURES: Structure[] = ['minority_investment', 'majority_sale', 'full_sale']
const CCY_OPTIONS = ['EUR', 'USD', 'GBP', 'SEK', 'NOK', 'DKK']

type ConditionDraft = Partial<Condition> & { enabled: boolean }

function emptyDrafts(): Record<ConditionKind, ConditionDraft> {
  const out = {} as Record<ConditionKind, ConditionDraft>
  for (const kind of CONDITION_KINDS) out[kind] = { enabled: false, kind, strength: 'unknown', weight: 1 }
  return out
}

/** Builds the exact Condition[] payload the backend accepts, stripping UI-only fields. */
function serializeConditions(drafts: Record<ConditionKind, ConditionDraft>): Condition[] {
  const out: Condition[] = []
  for (const kind of CONDITION_KINDS) {
    const d = drafts[kind]
    if (!d.enabled) continue
    const c: Condition = { kind, strength: d.strength ?? 'unknown' }
    if (d.strength === 'soft') c.weight = d.weight ?? 1
    if (kind === 'retained_ownership') c.min_pct = d.min_pct
    if (kind === 'timeline') c.within_months = d.within_months
    if (kind === 'structure') c.structures = d.structures
    if (kind === 'currency') c.currencies = d.currencies
    if (kind === 'minimum_proceeds') {
      c.amount = d.amount
      c.currency = d.currency
    }
    if (kind === 'site_retention' && d.sites?.length) c.sites = d.sites
    if (d.note) c.note = d.note
    out.push(c)
  }
  return out
}

function draftValid(drafts: Record<ConditionKind, ConditionDraft>): string | null {
  for (const kind of CONDITION_KINDS) {
    const d = drafts[kind]
    if (!d.enabled) continue
    if (kind === 'retained_ownership' && d.min_pct == null) return 'Retained ownership needs a minimum percentage.'
    if (kind === 'timeline' && !d.within_months) return 'Timeline needs a number of months.'
    if (kind === 'structure' && !d.structures?.length) return 'Structure needs at least one accepted deal structure.'
    if (kind === 'currency' && !d.currencies?.length) return 'Currency needs at least one accepted currency.'
    if (kind === 'minimum_proceeds' && (!d.amount || !d.currency)) return 'Minimum proceeds needs an amount and currency.'
  }
  if (!CONDITION_KINDS.some((k) => drafts[k].enabled)) return 'Select at least one condition.'
  return null
}

/** Shared row renderer for both the owner-preference form and the scenario override form. */
function ConditionRow({
  kind,
  draft,
  onChange,
}: {
  kind: ConditionKind
  draft: ConditionDraft
  onChange: (next: ConditionDraft) => void
}) {
  return (
    <div className="condition-row">
      <div className="condition-row__head">
        <label>
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(e) => onChange({ ...draft, enabled: e.target.checked })}
          />
          {CONDITION_LABELS[kind]}
        </label>
        {draft.enabled && (
          <div className="condition-row__strength" role="radiogroup" aria-label={`${CONDITION_LABELS[kind]} strength`}>
            {(['hard', 'soft', 'unknown'] as const).map((s) => (
              <button
                key={s}
                type="button"
                className={`strength-btn ${draft.strength === s ? `strength-btn--active-${s}` : ''}`}
                onClick={() => onChange({ ...draft, strength: s })}
              >
                {s === 'hard' ? 'Non-negotiable' : s === 'soft' ? 'Preference' : 'Not yet said'}
              </button>
            ))}
          </div>
        )}
      </div>

      {draft.enabled && (
        <div className="condition-row__fields">
          {draft.strength === 'soft' && (
            <label>
              Weight (1–5)
              <input
                type="number"
                min={1}
                max={5}
                value={draft.weight ?? 1}
                onChange={(e) => onChange({ ...draft, weight: Number(e.target.value) })}
              />
            </label>
          )}

          {kind === 'retained_ownership' && (
            <label>
              Minimum equity owner keeps (%)
              <input
                type="number"
                min={0}
                max={100}
                step="0.01"
                value={draft.min_pct ?? ''}
                onChange={(e) => onChange({ ...draft, min_pct: e.target.value })}
              />
            </label>
          )}

          {kind === 'timeline' && (
            <label>
              Must close within (months)
              <input
                type="number"
                min={1}
                max={240}
                value={draft.within_months ?? ''}
                onChange={(e) => onChange({ ...draft, within_months: Number(e.target.value) })}
              />
            </label>
          )}

          {kind === 'structure' && (
            <div className="multi-check">
              {STRUCTURES.map((s) => (
                <label key={s}>
                  <input
                    type="checkbox"
                    checked={draft.structures?.includes(s) ?? false}
                    onChange={(e) => {
                      const cur = new Set(draft.structures ?? [])
                      if (e.target.checked) cur.add(s)
                      else cur.delete(s)
                      onChange({ ...draft, structures: Array.from(cur) })
                    }}
                  />
                  {STRUCTURE_LABELS[s]}
                </label>
              ))}
            </div>
          )}

          {kind === 'currency' && (
            <div className="multi-check">
              {CCY_OPTIONS.map((c) => (
                <label key={c}>
                  <input
                    type="checkbox"
                    checked={draft.currencies?.includes(c) ?? false}
                    onChange={(e) => {
                      const cur = new Set(draft.currencies ?? [])
                      if (e.target.checked) cur.add(c)
                      else cur.delete(c)
                      onChange({ ...draft, currencies: Array.from(cur) })
                    }}
                  />
                  {c}
                </label>
              ))}
            </div>
          )}

          {kind === 'minimum_proceeds' && (
            <>
              <label>
                Minimum proceeds amount
                <input
                  type="number"
                  min={0}
                  step="0.01"
                  value={draft.amount ?? ''}
                  onChange={(e) => onChange({ ...draft, amount: e.target.value })}
                />
              </label>
              <label>
                Currency
                <select value={draft.currency ?? ''} onChange={(e) => onChange({ ...draft, currency: e.target.value })}>
                  <option value="">Select…</option>
                  {CCY_OPTIONS.map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
              </label>
            </>
          )}

          {kind === 'site_retention' && (
            <label>
              Sites to keep open (comma separated, optional)
              <input
                value={(draft.sites ?? []).join(', ')}
                onChange={(e) =>
                  onChange({ ...draft, sites: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })
                }
              />
            </label>
          )}

          <label>
            Note (optional)
            <input value={draft.note ?? ''} onChange={(e) => onChange({ ...draft, note: e.target.value })} />
          </label>
        </div>
      )}
    </div>
  )
}

/** Optional inline "attach a source" fieldset. Creates the Source record on submit and returns its id. */
function SourceFieldset({
  want,
  setWant,
  kind,
  setKind,
  url,
  setUrl,
  title,
  setTitle,
}: {
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
        Attach a source for this
      </label>
      {want && (
        <div className="condition-row__fields">
          <label>
            Source type
            <select value={kind} onChange={(e) => setKind(e.target.value as SourceIn['kind'])}>
              <option value="owner_reported">Owner reported</option>
              <option value="call">Call</option>
              <option value="email">Email</option>
              <option value="note">Note</option>
              <option value="document">Document</option>
              <option value="website">Website</option>
              <option value="manual">Manual entry</option>
            </select>
          </label>
          <label>
            URL (optional)
            <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" />
          </label>
          <label>
            Title / description
            <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Call with owner, 3 Feb" />
          </label>
        </div>
      )}
    </div>
  )
}

async function maybeCreateSource(
  want: boolean,
  kind: SourceIn['kind'],
  url: string,
  title: string,
): Promise<string | undefined> {
  if (!want) return undefined
  if (!url && !title) throw new Error('A source needs a URL or a title.')
  const source = await api.post<SourceOut>('/api/sources', {
    kind,
    url: url || undefined,
    title: title || undefined,
  } satisfies SourceIn)
  return source.id
}

export function FuturesView() {
  const [companyId, setCompanyId] = useState<string>(() => new URLSearchParams(window.location.search).get('company') ?? '')
  const [selectedCompany, setSelectedCompany] = useState<Company | null>(null)

  const [preferences, setPreferences] = useState<PreferenceList | null>(null)
  const [prefStatus, setPrefStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [prefError, setPrefError] = useState<string | null>(null)

  const [scenarios, setScenarios] = useState<ScenarioOut[] | null>(null)
  const [scenarioListStatus, setScenarioListStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')

  function loadPreferences(id: string) {
    if (!id) return
    setPrefStatus('loading')
    setPrefError(null)
    api
      .get<PreferenceList>(`/api/companies/${id}/preferences`)
      .then((result) => {
        setPreferences(result)
        setPrefStatus('ready')
      })
      .catch((cause) => {
        setPrefError(cause instanceof ApiError ? cause.message : 'Could not load owner conditions.')
        setPrefStatus('error')
      })
  }

  function loadScenarios(id: string) {
    if (!id) return
    setScenarioListStatus('loading')
    api
      .get<ScenarioOut[]>(`/api/scenarios?company_id=${id}`)
      .then((result) => {
        setScenarios(result)
        setScenarioListStatus('ready')
      })
      .catch(() => setScenarioListStatus('error'))
  }

  useEffect(() => {
    if (companyId) {
      loadPreferences(companyId)
      loadScenarios(companyId)
    } else {
      setPreferences(null)
      setScenarios(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [companyId])

  return (
    <div className="page">
      <header className="page__header">
        <h1>Futures</h1>
        <p className="page__lede">
          Capture the conditions an owner would need before considering a sale, get an explicit confirmation
          of that exact version, then explore alternatives without changing the confirmed original.
        </p>
      </header>

      <div className="company-picker">
        <label htmlFor="futures-company">Company</label>
        <CompanyPicker id="futures-company" value={companyId} onChange={(value) => setCompanyId(String(value))}
          onResolved={(items) => setSelectedCompany(items[0] ?? null)} />
      </div>

      {!companyId && (
        <EmptyState
          icon={<Compass size={28} aria-hidden="true" />}
          title="Select a company"
          description="Owner conditions, confirmation and futures scenarios are all per-company."
        />
      )}

      {companyId && (
        <>
          <OwnerPreferencesPanel
            companyId={companyId}
            companyName={selectedCompany?.name ?? ''}
            preferences={preferences}
            status={prefStatus}
            error={prefError}
            onReload={() => loadPreferences(companyId)}
          />

          <ScenariosPanel
            companyId={companyId}
            effectiveProfile={preferences?.items.find((p) => p.id === preferences.effective_profile_id) ?? null}
            scenarios={scenarios}
            status={scenarioListStatus}
            onReload={() => loadScenarios(companyId)}
          />
        </>
      )}
    </div>
  )
}

function OwnerPreferencesPanel({
  companyId,
  companyName,
  preferences,
  status,
  error,
  onReload,
}: {
  companyId: string
  companyName: string
  preferences: PreferenceList | null
  status: 'idle' | 'loading' | 'ready' | 'error'
  error: string | null
  onReload: () => void
}) {
  const { push } = useToast()
  const [showForm, setShowForm] = useState(false)
  const [drafts, setDrafts] = useState<Record<ConditionKind, ConditionDraft>>(emptyDrafts)
  const [statedBy, setStatedBy] = useState('')
  const [note, setNote] = useState('')
  const [wantSource, setWantSource] = useState(false)
  const [sourceKind, setSourceKind] = useState<SourceIn['kind']>('owner_reported')
  const [sourceUrl, setSourceUrl] = useState('')
  const [sourceTitle, setSourceTitle] = useState('')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const [confirmTarget, setConfirmTarget] = useState<PreferenceOut | null>(null)
  const [speakerName, setSpeakerName] = useState('')
  const [speakerAuthority, setSpeakerAuthority] = useState<SpeakerAuthority>('owner')
  const [statement, setStatement] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [confirmError, setConfirmError] = useState<string | null>(null)

  async function handlePropose(event: FormEvent) {
    event.preventDefault()
    const invalid = draftValid(drafts)
    if (invalid) {
      setFormError(invalid)
      return
    }
    setSaving(true)
    setFormError(null)
    try {
      const source_id = await maybeCreateSource(wantSource, sourceKind, sourceUrl, sourceTitle)
      await api.post<PreferenceOut>(`/api/companies/${companyId}/preferences`, {
        conditions: serializeConditions(drafts),
        stated_by: statedBy || undefined,
        note: note || undefined,
        source_id,
      })
      setDrafts(emptyDrafts())
      setStatedBy('')
      setNote('')
      setWantSource(false)
      setSourceUrl('')
      setSourceTitle('')
      setShowForm(false)
      onReload()
      push('Proposed profile saved. It still needs owner confirmation.', 'success')
    } catch (cause) {
      const message = cause instanceof ApiError ? cause.message : 'Could not save these conditions.'
      setFormError(message)
      push(message, 'error')
    } finally {
      setSaving(false)
    }
  }

  async function handleConfirm(event: FormEvent) {
    event.preventDefault()
    if (!confirmTarget) return
    if (speakerAuthority === 'unverified') {
      setConfirmError('Only an owner or an authorized representative can confirm preferences.')
      return
    }
    setConfirming(true)
    setConfirmError(null)
    try {
      await api.post<PreferenceOut>(`/api/preferences/${confirmTarget.id}/confirm`, {
        conditions_hash: confirmTarget.conditions_hash,
        speaker_name: speakerName,
        speaker_authority: speakerAuthority,
        statement,
        confirmed_at: new Date().toISOString(),
      })
      setConfirmTarget(null)
      setSpeakerName('')
      setStatement('')
      onReload()
      push(`Version ${confirmTarget.version} confirmed`, 'success')
    } catch (cause) {
      const message = cause instanceof ApiError ? cause.message : 'Could not confirm this version.'
      setConfirmError(message)
      push(message, 'error')
    } finally {
      setConfirming(false)
    }
  }

  return (
    <section className="panel deals-section">
      <div className="panel__row">
        <h2>Owner conditions{companyName ? ` — ${companyName}` : ''}</h2>
        <button type="button" className="btn btn--secondary" onClick={() => setShowForm((v) => !v)}>
          <Plus size={14} aria-hidden="true" />
          Propose new version
        </button>
      </div>

      <p className="form-note">
        A <strong>proposed</strong> version is a private hypothesis — what an advisor believes the owner would
        require. It only becomes usable for matching once the owner (or an authorized representative) explicitly
        confirms that <em>exact</em> version below.
      </p>

      {status === 'loading' && <LoadingBlock label="Loading owner conditions…" />}
      {status === 'error' && <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={onReload} />}

      {showForm && (
        <form className="stack-form" onSubmit={handlePropose}>
          {formError && <p className="field-error" role="alert">{formError}</p>}
          <div className="condition-grid">
            {CONDITION_KINDS.map((kind) => (
              <ConditionRow
                key={kind}
                kind={kind}
                draft={drafts[kind]}
                onChange={(next) => setDrafts((d) => ({ ...d, [kind]: next }))}
              />
            ))}
          </div>
          <div className="field-row">
            <div className="field-col">
              <label htmlFor="stated-by">Stated by (optional)</label>
              <input id="stated-by" value={statedBy} onChange={(e) => setStatedBy(e.target.value)} placeholder="e.g. Owner's advisor" />
            </div>
          </div>
          <label htmlFor="pref-note">Note (optional, private context)</label>
          <textarea id="pref-note" rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
          <SourceFieldset
            want={wantSource}
            setWant={setWantSource}
            kind={sourceKind}
            setKind={setSourceKind}
            url={sourceUrl}
            setUrl={setSourceUrl}
            title={sourceTitle}
            setTitle={setSourceTitle}
          />
          <div className="dialog__actions">
            <button type="button" className="btn btn--ghost" onClick={() => setShowForm(false)} disabled={saving}>Cancel</button>
            <button type="submit" className="btn btn--primary" disabled={saving}>
              {saving ? 'Saving…' : 'Save proposed version'}
            </button>
          </div>
        </form>
      )}

      {status === 'ready' && preferences && preferences.items.length === 0 && (
        <EmptyState title="No conditions recorded yet" description="Propose the first version above." />
      )}

      {status === 'ready' && preferences && preferences.items.length > 0 && (
        <div className="list-scroll">
          {preferences.items.map((p) => (
            <div key={p.id} className={`profile-card ${p.id === preferences.effective_profile_id ? 'profile-card--effective' : ''}`}>
              <div className="profile-card__head">
                <strong>Version {p.version}{p.id === preferences.effective_profile_id ? ' · in use for matching' : ''}</strong>
                {p.confirmation ? (
                  <span className="confirmed-tag">
                    Confirmed by {p.confirmation.confirmed_by} ({p.confirmation.authority.replace('_', ' ')}) · {formatDateTime(p.confirmation.confirmed_at)}
                  </span>
                ) : (
                  <span className="hypothetical-tag">Proposed — not yet confirmed by the owner</span>
                )}
              </div>
              <div className="profile-card__conditions">
                {p.conditions.map((c) => (
                  <span key={c.kind} className={`badge badge--${c.strength === 'unknown' ? 'unknown' : c.strength}`}>
                    {CONDITION_LABELS[c.kind]}
                  </span>
                ))}
              </div>
              {p.confirmation ? (
                <p className="confirm-note">“{p.confirmation.statement}”</p>
              ) : (
                <div className="dialog__actions">
                  <button
                    type="button"
                    className="btn btn--secondary"
                    onClick={() => {
                      setConfirmTarget(p)
                      setConfirmError(null)
                    }}
                  >
                    <ShieldCheck size={14} aria-hidden="true" />
                    Confirm this exact version
                  </button>
                </div>
              )}
              {confirmTarget?.id === p.id && (
                <form className="stack-form" onSubmit={handleConfirm}>
                  <p className="form-note">
                    This confirms version {p.version} exactly (hash <code className="mono">{p.conditions_hash.slice(0, 10)}…</code>).
                    If conditions change, propose a new version instead — this one is never edited in place.
                  </p>
                  {confirmError && <p className="field-error" role="alert">{confirmError}</p>}
                  <div className="field-row">
                    <div className="field-col">
                      <label htmlFor="speaker-name">Who is confirming</label>
                      <input id="speaker-name" required value={speakerName} onChange={(e) => setSpeakerName(e.target.value)} />
                    </div>
                    <div className="field-col">
                      <label htmlFor="speaker-authority">Their authority</label>
                      <select id="speaker-authority" value={speakerAuthority} onChange={(e) => setSpeakerAuthority(e.target.value as SpeakerAuthority)}>
                        <option value="owner">Owner</option>
                        <option value="authorized_representative">Authorized representative</option>
                        <option value="unverified">Unverified (cannot confirm)</option>
                      </select>
                    </div>
                  </div>
                  <label htmlFor="confirm-statement">Exact statement confirming these conditions</label>
                  <textarea id="confirm-statement" rows={2} required value={statement} onChange={(e) => setStatement(e.target.value)} />
                  <div className="dialog__actions">
                    <button type="button" className="btn btn--ghost" onClick={() => setConfirmTarget(null)} disabled={confirming}>Cancel</button>
                    <button type="submit" className="btn btn--primary" disabled={confirming}>
                      {confirming ? 'Confirming…' : 'Confirm version'}
                    </button>
                  </div>
                </form>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function ScenariosPanel({
  companyId,
  effectiveProfile,
  scenarios,
  status,
  onReload,
}: {
  companyId: string
  effectiveProfile: PreferenceOut | null
  scenarios: ScenarioOut[] | null
  status: 'idle' | 'loading' | 'ready' | 'error'
  onReload: () => void
}) {
  const { push } = useToast()
  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [overrideDrafts, setOverrideDrafts] = useState<Record<ConditionKind, ConditionDraft>>(emptyDrafts)
  const [removeKinds, setRemoveKinds] = useState<Set<ConditionKind>>(new Set())
  const [facts, setFacts] = useState<FactOverrides>({})
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const existingKinds = effectiveProfile?.conditions.map((c) => c.kind) ?? []

  async function handleCreate(event: FormEvent) {
    event.preventDefault()
    if (!name.trim()) {
      setFormError('Give this scenario a name, e.g. "Accept 60% rollover".')
      return
    }
    setSaving(true)
    setFormError(null)
    try {
      const overrides = serializeConditions(overrideDrafts).map((c) => ({ ...c, override: undefined }))
      await api.post(`/api/scenarios`, {
        company_id: companyId,
        name: name.trim(),
        conditions: overrides,
        remove_kinds: Array.from(removeKinds),
        facts: Object.keys(facts).length ? facts : undefined,
      })
      setName('')
      setOverrideDrafts(emptyDrafts())
      setRemoveKinds(new Set())
      setFacts({})
      setShowForm(false)
      onReload()
      push('Scenario computed', 'success')
    } catch (cause) {
      const message = cause instanceof ApiError ? cause.message : 'Could not compute this scenario.'
      setFormError(message)
      push(message, 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="panel deals-section">
      <div className="panel__row">
        <h2>Futures — alternatives &amp; scenarios</h2>
        <button type="button" className="btn btn--secondary" onClick={() => setShowForm((v) => !v)} disabled={!effectiveProfile}>
          <FlaskConical size={14} aria-hidden="true" />
          New scenario
        </button>
      </div>
      <p className="form-note">
        A scenario adjusts one or more conditions <em>hypothetically</em> to see what would change — it never
        edits the confirmed original. Company facts can also be overridden here as explicit hypotheticals
        (e.g. "if revenue were €5M"); those are always labelled and never confused with confirmed data.
      </p>

      {!effectiveProfile && (
        <p className="muted small">Confirm an owner-conditions version above before running scenarios.</p>
      )}

      {showForm && effectiveProfile && (
        <form className="stack-form" onSubmit={handleCreate}>
          {formError && <p className="field-error" role="alert">{formError}</p>}
          <label htmlFor="scenario-name">Scenario name</label>
          <input id="scenario-name" required value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Accept a lower rollover" />

          <p className="form-note">Override or add conditions for this scenario only:</p>
          <div className="condition-grid">
            {CONDITION_KINDS.map((kind) => (
              <ConditionRow
                key={kind}
                kind={kind}
                draft={overrideDrafts[kind]}
                onChange={(next) => setOverrideDrafts((d) => ({ ...d, [kind]: next }))}
              />
            ))}
          </div>

          {existingKinds.length > 0 && (
            <>
              <p className="form-note">Or remove a confirmed condition for this scenario only:</p>
              <div className="multi-check">
                {existingKinds.map((kind) => (
                  <label key={kind}>
                    <input
                      type="checkbox"
                      checked={removeKinds.has(kind)}
                      onChange={(e) => {
                        const next = new Set(removeKinds)
                        if (e.target.checked) next.add(kind)
                        else next.delete(kind)
                        setRemoveKinds(next)
                      }}
                    />
                    {CONDITION_LABELS[kind]}
                  </label>
                ))}
              </div>
            </>
          )}

          <p className="form-note">Optional hypothetical company facts (clearly separate from confirmed evidence):</p>
          <div className="condition-row__fields">
            <label>
              Country
              <input value={facts.country ?? ''} onChange={(e) => setFacts((f) => ({ ...f, country: e.target.value.toUpperCase() || undefined }))} placeholder="e.g. FI" maxLength={2} />
            </label>
            <label>
              Industry
              <input value={facts.industry ?? ''} onChange={(e) => setFacts((f) => ({ ...f, industry: e.target.value || undefined }))} />
            </label>
            <label>
              Employees
              <input type="number" min={0} value={facts.employees ?? ''} onChange={(e) => setFacts((f) => ({ ...f, employees: e.target.value ? Number(e.target.value) : undefined }))} />
            </label>
          </div>

          <div className="dialog__actions">
            <button type="button" className="btn btn--ghost" onClick={() => setShowForm(false)} disabled={saving}>Cancel</button>
            <button type="submit" className="btn btn--primary" disabled={saving}>{saving ? 'Computing…' : 'Compute scenario'}</button>
          </div>
        </form>
      )}

      {status === 'loading' && <LoadingBlock label="Loading scenarios…" />}
      {status === 'error' && <ErrorBlock message="Could not load scenarios." onRetry={onReload} />}
      {status === 'ready' && (scenarios ?? []).length === 0 && (
        <p className="muted small">No scenarios computed yet.</p>
      )}
      {status === 'ready' && scenarios && scenarios.length > 0 && (
        <div className="list-scroll">
          {scenarios.map((s) => {
            const isOpen = expanded.has(s.id)
            return (
              <div key={s.id} className="match-card">
                <button
                  type="button"
                  className="details-toggle"
                  onClick={() => {
                    const next = new Set(expanded)
                    if (isOpen) next.delete(s.id)
                    else next.add(s.id)
                    setExpanded(next)
                  }}
                >
                  {isOpen ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
                  {' '}{s.name} · computed {formatDateTime(s.created_at)}
                </button>
                {isOpen && (
                  <div className="check-list">
                    {s.results.map((r) => (
                      <div key={r.mandate_id} className="check-line">
                        <div className="check-line__detail">
                          <strong>{r.buyer_name}</strong>: baseline{' '}
                          <span className={`status-pill status-pill--${r.baseline_status}`}>{r.baseline_status.replace('_', ' ')}</span>
                          {' → '}
                          <span className={`status-pill status-pill--${r.status}`}>{r.status.replace('_', ' ')}</span>
                          <div className="muted small">{r.explanation.summary}</div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
