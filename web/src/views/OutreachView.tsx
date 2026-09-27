import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import {
  Mail, Unplug, Plug, RefreshCw, Loader2, CheckCircle2, XCircle, Info, Eye,
  Pause, Play, Plus, Trash2, ShieldAlert, Send, X, Ban,
} from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { Company, CompanyDetail, Contact } from '../lib/types'
import { CompanyPicker } from '../components/CompanyPicker'
import type {
  ClassifyInput, Conversation, ConversationDetail, DraftCreateInput, Enrollment, GmailConnectResponse,
  GmailDisconnectResponse, GmailStatus, GmailSyncResponse, MailMessage, OutreachControls, OutreachDraft,
  ReconcileResponse, ReplyIntent, Sequence, SequenceInput, SequenceStep, SpeakerAuthority,
  StepApproval, StepKind, Suppression, SuppressionInput, TemplateApproval,
} from '../lib/mailTypes'
import { LoadingBlock, ErrorBlock, EmptyState } from '../components/StateViews'
import { Link } from '../lib/router'
import { useToast } from '../lib/toast'
import './outreach.css'

type Tab = 'Inbox' | 'Drafts' | 'Sequences' | 'Controls'
const TABS: Tab[] = ['Inbox', 'Drafts', 'Sequences', 'Controls']

const REQUIRED_SCOPE_NOTES: Record<string, string> = {
  'https://www.googleapis.com/auth/gmail.send': 'Send messages you have approved.',
  'https://www.googleapis.com/auth/gmail.readonly':
    'Read replies. Google grants this at the whole-mailbox level, but Permetheus only reads and stores messages that belong to threads it started — nothing else in your inbox is fetched, browsed, or stored.',
}

function errorMessage(cause: unknown, fallback: string): string {
  if (cause instanceof ApiError) {
    const detail = cause.detail
    const extra = detail ? (typeof detail === 'string' ? detail : JSON.stringify(detail)) : ''
    return `${cause.message}${extra ? ` — ${extra}` : ''}`
  }
  if (cause instanceof Error) return cause.message
  return fallback
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function readForm(form: HTMLFormElement): Record<string, string> {
  return Object.fromEntries(new FormData(form).entries()) as Record<string, string>
}

function statusLabel(value: string): string {
  return value.replace(/_/g, ' ')
}

// ---------------------------------------------------------------- shared data hooks (companies + contacts)

interface Directory {
  companies: Company[]
  companiesStatus: 'loading' | 'ready' | 'error'
  companyName: (id: string) => string
  contactsFor: (companyId: string) => Contact[] | undefined
  ensureContacts: (companyId: string) => void
  ensureCompany: (companyId: string) => void
  rememberCompanies: (companies: Company[]) => void
  contactLabel: (companyId: string, contactId: string | null) => string
  reloadCompanies: () => void
}

function useDirectory(): Directory {
  const [companies, setCompanies] = useState<Company[]>([])
  const [companiesStatus, setCompaniesStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [contactsByCompany, setContactsByCompany] = useState<Record<string, Contact[]>>({})
  const pending = useRef<Set<string>>(new Set())
  const pendingCompanies = useRef<Set<string>>(new Set())
  const knownCompanyIds = useRef<Set<string>>(new Set())

  const reloadCompanies = useCallback(() => {
    setCompaniesStatus('loading')
    api
      .get<{ items: Company[]; total: number } | Company[]>('/api/companies')
      .then((response) => {
        const items = Array.isArray(response) ? response : response.items
        knownCompanyIds.current = new Set(items.map((company) => company.id))
        setCompanies(items)
        setCompaniesStatus('ready')
      })
      .catch(() => setCompaniesStatus('error'))
  }, [])

  useEffect(reloadCompanies, [reloadCompanies])

  const ensureContacts = useCallback((companyId: string) => {
    if (!companyId || contactsByCompany[companyId] || pending.current.has(companyId)) return
    pending.current.add(companyId)
    api
      .get<CompanyDetail>(`/api/companies/${companyId}`)
      .then((detail) => {
        setContactsByCompany((current) => ({ ...current, [companyId]: detail.contacts }))
        knownCompanyIds.current.add(detail.id)
        setCompanies((current) => current.some((company) => company.id === detail.id) ? current : [...current, detail])
      })
      .catch(() => setContactsByCompany((current) => ({ ...current, [companyId]: [] })))
      .finally(() => pending.current.delete(companyId))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contactsByCompany])

  const ensureCompany = useCallback((companyId: string) => {
    if (!companyId || knownCompanyIds.current.has(companyId) || pendingCompanies.current.has(companyId)) return
    pendingCompanies.current.add(companyId)
    api.get<CompanyDetail>(`/api/companies/${encodeURIComponent(companyId)}`)
      .then((detail) => {
        knownCompanyIds.current.add(detail.id)
        setCompanies((current) => current.some((company) => company.id === detail.id) ? current : [...current, detail])
      })
      .catch(() => undefined)
      .finally(() => pendingCompanies.current.delete(companyId))
  }, [])

  const rememberCompanies = useCallback((items: Company[]) => {
    if (items.length) setCompanies((current) => {
      const byId = new Map(current.map((company) => [company.id, company]))
      items.forEach((company) => { knownCompanyIds.current.add(company.id); byId.set(company.id, company) })
      return Array.from(byId.values())
    })
  }, [])

  const companyName = useCallback(
    (id: string) => companies.find((c) => c.id === id)?.name || 'Unknown company',
    [companies],
  )

  const contactsFor = useCallback((companyId: string) => contactsByCompany[companyId], [contactsByCompany])

  const contactLabel = useCallback(
    (companyId: string, contactId: string | null) => {
      if (!contactId) return 'No contact'
      const list = contactsByCompany[companyId]
      if (list === undefined) { ensureContacts(companyId); return 'Loading contact…' }
      return list.find((c) => c.id === contactId)?.name || 'Unknown contact'
    },
    [contactsByCompany, ensureContacts],
  )

  return { companies, companiesStatus, companyName, contactsFor, ensureContacts, ensureCompany, rememberCompanies, contactLabel, reloadCompanies }
}

/** Inline link to the company detail page plus the resolved contact name — shown next to verification errors. */
function LinkedTarget({ dir, companyId, contactId }: { dir: Directory; companyId: string; contactId: string | null }) {
  useEffect(() => { dir.ensureContacts(companyId) }, [dir, companyId])
  return (
    <p className="linked-target">
      <span>Company: <Link to={`/companies/${companyId}`}>{dir.companyName(companyId)}</Link></span>
      <span>Contact: {dir.contactLabel(companyId, contactId)}</span>
    </p>
  )
}

// ================================================================== Gmail connection card

function GmailConnectionCard({ status, onChange }: { status: GmailStatus | null; onChange: () => void }) {
  const [busy, setBusy] = useState<'connect' | 'disconnect' | 'sync' | null>(null)
  const [syncResult, setSyncResult] = useState<GmailSyncResponse | null>(null)
  const { push } = useToast()

  async function connect() {
    setBusy('connect')
    try {
      const { authorization_url } = await api.post<GmailConnectResponse>('/api/gmail/connect')
      window.location.href = authorization_url
    } catch (cause) {
      push(errorMessage(cause, 'Could not start the Gmail connection.'), 'error')
      setBusy(null)
    }
  }

  async function disconnect() {
    if (!window.confirm('Disconnect Gmail? Sending and reading replies will stop until you reconnect.')) return
    setBusy('disconnect')
    try {
      const result = await api.post<GmailDisconnectResponse>('/api/gmail/disconnect')
      push(result.revoked ? 'Gmail disconnected and access revoked at Google.' : 'Gmail disconnected locally. Revoke access in your Google account settings too.', 'success')
      onChange()
    } catch (cause) { push(errorMessage(cause, 'Could not disconnect Gmail.'), 'error') }
    finally { setBusy(null) }
  }

  async function sync() {
    setBusy('sync')
    try {
      const result = await api.post<GmailSyncResponse>('/api/gmail/sync')
      setSyncResult(result)
      push(`Sync complete: ${result.ingested} new message(s) across ${result.tracked_threads} tracked thread(s)${result.resynced ? ' (full resync)' : ''}.`, 'success')
      onChange()
    } catch (cause) { push(errorMessage(cause, 'Sync failed.'), 'error') }
    finally { setBusy(null) }
  }

  if (!status) return <div className="panel gmail-card"><LoadingBlock label="Checking Gmail connection…" /></div>

  const pillClass = !status.configured ? 'unconfigured' : status.needs_reauth ? 'reauth' : status.connected ? 'connected' : 'disconnected'
  const pillText = !status.configured ? 'Not configured' : status.needs_reauth ? 'Needs reconnect' : status.connected ? 'Connected' : 'Not connected'

  return (
    <section className="panel gmail-card">
      <div className="gmail-card__row">
        <div className="gmail-card__identity">
          <Mail size={18} aria-hidden="true" />
          <div>
            <strong>Gmail</strong>
            <div className="muted small">{status.email ?? 'No mailbox connected'}</div>
          </div>
          <span className={`gmail-status-pill gmail-status-pill--${pillClass}`}>
            {status.needs_reauth ? <ShieldAlert size={13} aria-hidden="true" /> : status.connected ? <CheckCircle2 size={13} aria-hidden="true" /> : <XCircle size={13} aria-hidden="true" />}
            {pillText}
          </span>
        </div>
        <div className="gmail-card__actions">
          {status.configured && !status.connected && (
            <button type="button" className="btn btn--primary" disabled={busy !== null} onClick={connect}>
              <Plug size={14} aria-hidden="true" /> {busy === 'connect' ? 'Redirecting…' : 'Connect Gmail'}
            </button>
          )}
          {status.configured && status.needs_reauth && (
            <button type="button" className="btn btn--primary" disabled={busy !== null} onClick={connect}>
              <Plug size={14} aria-hidden="true" /> Reconnect Gmail
            </button>
          )}
          {status.connected && (
            <>
              <button type="button" className="btn btn--secondary" disabled={busy !== null} onClick={sync}>
                {busy === 'sync' ? <Loader2 size={14} className="spin" aria-hidden="true" /> : <RefreshCw size={14} aria-hidden="true" />} Sync now
              </button>
              <button type="button" className="btn btn--ghost" disabled={busy !== null} onClick={disconnect}>
                <Unplug size={14} aria-hidden="true" /> Disconnect
              </button>
            </>
          )}
        </div>
      </div>

      <div className="gmail-card__meta">
        <span>Last sync: {formatDateTime(status.last_sync_at)}</span>
        <span>Sync cursor: {status.has_sync_cursor ? 'present' : 'none yet'}</span>
        {status.last_error && <span className="muted">Last error: {status.last_error}</span>}
        {syncResult && <span>Last manual sync: {syncResult.ingested} ingested, {syncResult.tracked_threads} tracked thread(s){syncResult.resynced ? ', full resync' : ''}.</span>}
      </div>

      <div className="gmail-disclosure">
        <Info size={15} aria-hidden="true" />
        <p>
          Connecting asks Google for permission to send email as you and to read your Gmail. The read permission is
          granted by Google at the whole-mailbox level, but Permetheus only ever reads, stores and syncs messages inside
          threads that it itself started (cold outreach or sequence steps). Nothing else in your inbox is fetched or
          stored. Tokens stay encrypted on the server and are never shown here or sent to any AI model.
          {status.scopes.length > 0 && (
            <> Currently granted: {status.scopes.map((s) => REQUIRED_SCOPE_NOTES[s] ? s.split('/').pop() : s).join(', ')}.</>
          )}
        </p>
      </div>

      {!status.configured && (
        <div className="gmail-setup">
          <p className="muted small"><strong>Gmail is not set up on this backend yet.</strong> This UI never asks for a client secret or token — those are configured on the server. Ask whoever runs the backend to:</p>
          <ol>
            <li>Create a Google Cloud project and enable the <strong>Gmail API</strong>.</li>
            <li>Create an OAuth <strong>Web application</strong> client and register the exact redirect URI the backend expects (e.g. <code>https://your-domain/oauth/google/callback</code>).</li>
            <li>Set the backend environment values <code>GOOGLE_CLIENT_ID</code>, <code>GOOGLE_CLIENT_SECRET</code>, <code>GOOGLE_REDIRECT_URI</code> and a <code>SESSION_SECRET</code> of 32+ characters.</li>
            <li>Add the two scopes above on the OAuth consent screen and add development mailboxes as test users if the app is External.</li>
            <li>Restart the backend, then reload this page and click Connect Gmail.</li>
          </ol>
        </div>
      )}
      {status.configured && status.needs_reauth && (
        <p className="muted small">Stored Gmail tokens can no longer be refreshed (revoked, expired, or the session secret changed). Reconnect above; nothing sends or reads until you do.</p>
      )}
    </section>
  )
}

// ================================================================== Inbox tab

function IntentPill({ intent }: { intent: ReplyIntent }) {
  return <span className={`status-pill status-pill--${intent === 'optout' ? 'opted_out' : intent}`}>{statusLabel(intent)}</span>
}

function ClassifyForm({ message, onClassify }: { message: MailMessage; onClassify: (input: ClassifyInput) => Promise<void> }) {
  const [intent, setIntent] = useState<ReplyIntent>(message.proposed_intent ?? 'unclear')
  const [authority, setAuthority] = useState<SpeakerAuthority>('unverified')
  const [busy, setBusy] = useState(false)
  return (
    <div className="message-bubble__confirm">
      <select value={intent} onChange={(e) => setIntent(e.target.value as ReplyIntent)} aria-label="Confirm intent">
        <option value="interested">Interested</option>
        <option value="no">Not interested</option>
        <option value="optout">Opt-out</option>
        <option value="unclear">Unclear</option>
      </select>
      <select value={authority} onChange={(e) => setAuthority(e.target.value as SpeakerAuthority)} aria-label="Speaker authority">
        <option value="unverified">Speaker: unverified</option>
        <option value="owner">Speaker: owner</option>
        <option value="authorized_representative">Speaker: authorized representative</option>
      </select>
      <button
        type="button"
        className="btn btn--secondary"
        disabled={busy}
        onClick={async () => { setBusy(true); try { await onClassify({ message_id: message.id, intent, speaker_authority: authority }) } finally { setBusy(false) } }}
      >
        {busy ? 'Confirming…' : 'Confirm'}
      </button>
    </div>
  )
}

function MessageBubble({ message, onClassify }: { message: MailMessage; onClassify: (input: ClassifyInput) => Promise<void> }) {
  return (
    <div className={`message-bubble message-bubble--${message.direction}`}>
      <div className="message-bubble__head">
        <span><strong>{message.direction === 'inbound' ? message.sender : 'You'}</strong> → {message.recipients.join(', ') || '—'}</span>
        <span>{formatDateTime(message.sent_at)}</span>
      </div>
      <div className="message-bubble__body">{message.body_text || '(empty body)'}</div>
      {message.direction === 'inbound' && (
        <div className="message-bubble__intent">
          {message.confirmed_intent ? (
            <>Confirmed: <IntentPill intent={message.confirmed_intent} /> {message.confirmed_at && <span className="muted small">on {formatDateTime(message.confirmed_at)}</span>}</>
          ) : (
            <>
              {message.proposed_intent && <span className="muted small">Proposed: <IntentPill intent={message.proposed_intent} /> ({message.proposed_reason})</span>}
              <ClassifyForm message={message} onClassify={onClassify} />
            </>
          )}
        </div>
      )}
    </div>
  )
}

function InboxTab({ dir }: { dir: Directory }) {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<ConversationDetail | null>(null)
  const [detailStatus, setDetailStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [followupBusy, setFollowupBusy] = useState(false)
  const { push } = useToast()

  const loadList = useCallback(() => {
    setStatus('loading'); setError(null)
    api.get<Conversation[]>('/api/outreach/conversations')
      .then((items) => { setConversations(items); items.forEach((item) => dir.ensureCompany(item.company_id)); setStatus('ready') })
      .catch((cause) => { setError(errorMessage(cause, 'Could not load conversations.')); setStatus('error') })
  }, [dir.ensureCompany])
  useEffect(loadList, [loadList])

  const loadDetail = useCallback((id: string) => {
    setDetailStatus('loading')
    api.get<ConversationDetail>(`/api/outreach/conversations/${id}`)
      .then((result) => { setDetail(result); setDetailStatus('ready'); dir.ensureContacts(result.company_id) })
      .catch((cause) => { push(errorMessage(cause, 'Could not load this thread.'), 'error'); setDetailStatus('error') })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [push])

  function select(id: string) { setSelectedId(id); loadDetail(id) }

  async function classify(input: ClassifyInput) {
    if (!detail) return
    try {
      await api.post(`/api/outreach/conversations/${detail.id}/classify`, input)
      push('Intent confirmed.', 'success')
      loadDetail(detail.id); loadList()
    } catch (cause) { push(errorMessage(cause, 'Could not confirm this reply.'), 'error') }
  }

  async function createFollowup() {
    if (!detail) return
    setFollowupBusy(true)
    try {
      await api.post(`/api/outreach/conversations/${detail.id}/followup`)
      push('Follow-up draft created. It still needs preview, approval and send.', 'success')
      loadDetail(detail.id)
    } catch (cause) { push(errorMessage(cause, 'Could not create a follow-up.'), 'error') }
    finally { setFollowupBusy(false) }
  }

  const latestInbound = detail?.messages.filter((m) => m.direction === 'inbound').slice(-1)[0] ?? null
  const canFollowup = latestInbound?.confirmed_intent === 'interested'

  if (status === 'loading') return <LoadingBlock label="Loading conversations…" />
  if (status === 'error') return <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={loadList} />
  if (conversations.length === 0) return <EmptyState icon={<Mail size={26} aria-hidden="true" />} title="No tracked threads yet" description="Threads appear once a cold draft is sent. Only threads Permetheus started are read." />

  return (
    <div className="outreach-split">
      <div className="conversation-list" role="list">
        {conversations.map((conv) => (
          <button
            key={conv.id}
            type="button"
            role="listitem"
            className={`conversation-list__item ${selectedId === conv.id ? 'is-active' : ''}`}
            onClick={() => select(conv.id)}
          >
            <strong>{conv.subject}</strong>
            <span className="muted">{dir.companyName(conv.company_id)}</span>
            <span className={`status-pill status-pill--${conv.status}`}>{statusLabel(conv.status)}</span>
            {conv.latest_reply && <span className="muted small">{conv.latest_reply.body_text.slice(0, 80) || '(empty reply)'}</span>}
          </button>
        ))}
      </div>

      <div>
        {detailStatus === 'idle' && <EmptyState title="Select a thread" description="Pick a conversation on the left to see the full reply history." />}
        {detailStatus === 'loading' && <LoadingBlock label="Loading thread…" />}
        {detailStatus === 'ready' && detail && (
          <div className="panel">
            <div className="panel__row">
              <div>
                <h2>{detail.subject}</h2>
                <p className="muted small">{dir.companyName(detail.company_id)} · <span className={`status-pill status-pill--${detail.status}`}>{statusLabel(detail.status)}</span></p>
              </div>
              <button type="button" className="btn btn--secondary" disabled={!canFollowup || followupBusy} onClick={createFollowup} title={canFollowup ? undefined : 'Confirm an interested reply first'}>
                {followupBusy ? 'Creating…' : 'Generate follow-up (missing fields)'}
              </button>
            </div>
            {!canFollowup && <p className="muted small">A follow-up asking for missing fields can only be generated after a reply in this thread is confirmed “interested”.</p>}

            <div className="thread">
              {detail.messages.map((m) => <MessageBubble key={m.id} message={m} onClassify={classify} />)}
              {detail.messages.length === 0 && <p className="muted">No messages synced yet for this thread.</p>}
            </div>

            {detail.drafts.length > 0 && (
              <div>
                <h2>Drafts in this thread</h2>
                <div className="draft-list">
                  {detail.drafts.map((d) => (
                    <DraftCard key={d.id} draft={d} dir={dir} onReload={() => { loadDetail(detail.id); loadList() }} />
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
        {detailStatus === 'error' && <ErrorBlock message="Could not load this thread." onRetry={() => selectedId && loadDetail(selectedId)} />}
      </div>
    </div>
  )
}

// ================================================================== Drafts tab

function DraftPreview({ draft }: { draft: OutreachDraft }) {
  return (
    <div className="draft-preview">
      <div className="draft-preview__field">To</div>
      <p>{draft.recipients.join(', ')}</p>
      <div className="draft-preview__field">Subject</div>
      <p>{draft.subject}</p>
      <div className="draft-preview__field">Body</div>
      <div className="draft-preview__body">{draft.body}</div>
      <p className="draft-preview__hash">version {draft.version} · content hash {draft.content_hash}</p>
    </div>
  )
}

function DraftEditForm({ draft, onSave, onCancel, saving }: {
  draft: OutreachDraft
  onSave: (patch: { recipients: string[]; subject: string; body: string }) => Promise<void>
  onCancel: () => void
  saving: boolean
}) {
  const [recipients, setRecipients] = useState(draft.recipients.join(', '))
  const [subject, setSubject] = useState(draft.subject)
  const [body, setBody] = useState(draft.body)
  return (
    <form
      className="stack-form"
      onSubmit={async (e) => {
        e.preventDefault()
        await onSave({ recipients: recipients.split(',').map((r) => r.trim()).filter(Boolean), subject, body })
      }}
    >
      <label>Recipients (comma separated)</label>
      <input value={recipients} onChange={(e) => setRecipients(e.target.value)} required />
      <label>Subject</label>
      <input value={subject} onChange={(e) => setSubject(e.target.value)} required />
      <label>Body</label>
      <textarea rows={6} value={body} onChange={(e) => setBody(e.target.value)} required />
      <div className="dialog__actions">
        <button type="button" className="btn btn--ghost" onClick={onCancel}>Cancel</button>
        <button className="btn btn--primary" disabled={saving}>{saving ? 'Saving…' : 'Save edit (voids approval)'}</button>
      </div>
    </form>
  )
}

function DraftCard({ draft, dir, onReload }: { draft: OutreachDraft; dir: Directory; onReload: () => void }) {
  const [previewOpen, setPreviewOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState<'approve' | 'send' | 'save' | null>(null)
  const [localError, setLocalError] = useState<string | null>(null)
  const { push } = useToast()

  const canEdit = draft.status === 'draft' || draft.status === 'approved' || draft.status === 'failed'
  const canApprove = (draft.status === 'draft' || draft.status === 'approved') && previewOpen
  const canSend = draft.status === 'approved'

  async function approve() {
    setBusy('approve'); setLocalError(null)
    try {
      await api.post(`/api/outreach/drafts/${draft.id}/approve`, { version: draft.version, content_hash: draft.content_hash })
      push('Draft approved. It still needs a separate Send.', 'success')
      onReload()
    } catch (cause) { setLocalError(errorMessage(cause, 'Could not approve this draft.')) }
    finally { setBusy(null) }
  }

  async function send() {
    setBusy('send'); setLocalError(null)
    try {
      await api.post(`/api/outreach/drafts/${draft.id}/send`, { version: draft.version, content_hash: draft.content_hash })
      push('Message sent.', 'success')
      onReload()
    } catch (cause) { setLocalError(errorMessage(cause, 'Send was blocked.')) }
    finally { setBusy(null) }
  }

  async function save(patch: { recipients: string[]; subject: string; body: string }) {
    setBusy('save'); setLocalError(null)
    try {
      await api.patch(`/api/outreach/drafts/${draft.id}`, patch)
      push('Draft updated. Preview and approve again before sending.', 'success')
      setEditing(false); setPreviewOpen(false)
      onReload()
    } catch (cause) { setLocalError(errorMessage(cause, 'Could not save the edit.')) }
    finally { setBusy(null) }
  }

  return (
    <article className="draft-card">
      <div className="draft-card__head">
        <div className="draft-card__title">
          <strong>{draft.subject}</strong>
          <span className="muted small">{dir.companyName(draft.company_id)} · {dir.contactLabel(draft.company_id, draft.contact_id)} · {draft.kind.replace('_', ' ')}</span>
        </div>
        <span className={`status-pill status-pill--${draft.status}`}>{statusLabel(draft.status)}</span>
      </div>

      <div className="draft-card__meta">
        <span>To: {draft.recipients.join(', ')}</span>
        <span>Version {draft.version}</span>
        {draft.approval && <span>Approved until {formatDateTime(draft.approval.expires_at)}</span>}
        {draft.dispatch && <span>Last attempt: {statusLabel(draft.dispatch.state)}{draft.dispatch.error ? ` — ${draft.dispatch.error}` : ''}</span>}
      </div>

      {editing ? (
        <DraftEditForm draft={draft} saving={busy === 'save'} onSave={save} onCancel={() => setEditing(false)} />
      ) : (
        <>
          {previewOpen && <DraftPreview draft={draft} />}
          <div className="draft-card__actions">
            <button type="button" className="btn btn--secondary" onClick={() => setPreviewOpen((v) => !v)}>
              <Eye size={14} aria-hidden="true" /> {previewOpen ? 'Hide preview' : 'Preview exact message'}
            </button>
            {canEdit && (
              <button type="button" className="btn btn--secondary" onClick={() => setEditing(true)}>Edit</button>
            )}
            <button type="button" className="btn btn--secondary" disabled={!canApprove || busy !== null} title={previewOpen ? undefined : 'Preview the exact message first'} onClick={approve}>
              <CheckCircle2 size={14} aria-hidden="true" /> {busy === 'approve' ? 'Approving…' : 'Approve'}
            </button>
            <button type="button" className="btn btn--primary" disabled={!canSend || busy !== null} title={canSend ? undefined : 'Only approved drafts can be sent'} onClick={send}>
              <Send size={14} aria-hidden="true" /> {busy === 'send' ? 'Sending…' : 'Send'}
            </button>
          </div>
        </>
      )}

      {localError && (
        <div>
          <p className="field-error" role="alert"><XCircle size={13} aria-hidden="true" /> {localError}</p>
          <LinkedTarget dir={dir} companyId={draft.company_id} contactId={draft.contact_id} />
        </div>
      )}
    </article>
  )
}

function NewDraftForm({ dir, onCreated }: { dir: Directory; onCreated: () => void }) {
  const [companyId, setCompanyId] = useState('')
  const [contactId, setContactId] = useState('')
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const { push } = useToast()

  useEffect(() => {
    if (!companyId) { setConversations([]); return }
    dir.ensureContacts(companyId)
    api.get<Conversation[]>(`/api/outreach/conversations?company_id=${companyId}`).then(setConversations).catch(() => setConversations([]))
  }, [companyId, dir])

  const contacts = companyId ? dir.contactsFor(companyId) : undefined

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = readForm(event.currentTarget)
    if (!companyId || !contactId) { setFormError('Choose a company and a contact.'); return }
    const contact = contacts?.find((c) => c.id === contactId)
    const recipients = (data.recipients || contact?.email || '').split(',').map((r) => r.trim()).filter(Boolean)
    if (recipients.length === 0) { setFormError('At least one recipient email is required.'); return }
    const input: DraftCreateInput = {
      company_id: companyId,
      contact_id: contactId,
      conversation_id: data.conversation_id || null,
      recipients,
      subject: data.subject,
      body: data.body,
      disclosure: data.disclosure_note ? { note: data.disclosure_note } : {},
    }
    setSaving(true); setFormError(null)
    try {
      await api.post('/api/outreach/drafts', input)
      event.currentTarget.reset(); setCompanyId(''); setContactId('')
      push('Draft created. It still needs preview, approval and send.', 'success')
      onCreated()
    } catch (cause) { setFormError(errorMessage(cause, 'Could not create the draft.')) }
    finally { setSaving(false) }
  }

  return (
    <form className="stack-form panel" onSubmit={submit}>
      <h2>New draft</h2>
      {formError && <p className="field-error" role="alert">{formError}</p>}
      <label htmlFor="draft-company">Company</label>
      <CompanyPicker id="draft-company" value={companyId} onChange={(value) => { const id = String(value); setCompanyId(id); setContactId('') }}
        required onResolved={dir.rememberCompanies} />
      <label>Contact</label>
      <select value={contactId} onChange={(e) => setContactId(e.target.value)} required disabled={!companyId}>
        <option value="">{contacts === undefined ? (companyId ? 'Loading contacts…' : 'Choose a company first') : contacts.length === 0 ? 'No contacts recorded' : 'Select a contact…'}</option>
        {contacts?.map((c) => <option key={c.id} value={c.id}>{c.name}{c.verification !== 'verified' ? ' (unverified)' : ''}{c.email ? ` — ${c.email}` : ''}</option>)}
      </select>
      <label>Existing conversation (optional, for an in-thread reply)</label>
      <select name="conversation_id" defaultValue="" disabled={!companyId}>
        <option value="">New cold outreach (no thread yet)</option>
        {conversations.map((c) => <option key={c.id} value={c.id}>{c.subject} — {statusLabel(c.status)}</option>)}
      </select>
      <label>Recipients override (comma separated, defaults to the contact's email)</label>
      <input name="recipients" placeholder="leave blank to use the contact's email" />
      <label>Subject</label>
      <input name="subject" required maxLength={300} />
      <label>Body</label>
      <textarea name="body" rows={6} required />
      <label>Disclosure note (optional — e.g. buyer identity, AI assistance used)</label>
      <input name="disclosure_note" placeholder="e.g. Drafted with AI assistance, reviewed by a human before sending" />
      <button className="btn btn--primary" disabled={saving}>{saving ? 'Creating…' : 'Create draft'}</button>
    </form>
  )
}

function BatchReview({ drafts, onDone }: { drafts: OutreachDraft[]; onDone: () => void }) {
  const [reviewed, setReviewed] = useState<string[]>([])
  const [phase, setPhase] = useState<'review' | 'sending' | 'done'>('review')
  const [results, setResults] = useState<{ id: string; message: string }[]>([])

  async function approveAndSend() {
    if (phase !== 'review' || drafts.length === 0 || !drafts.every((draft) => reviewed.includes(draft.id))) return
    setPhase('sending')
    for (const draft of drafts) {
      const preview = { version: draft.version, content_hash: draft.content_hash }
      let message: string
      try {
        await api.post(`/api/outreach/drafts/${draft.id}/approve`, preview)
        await api.post(`/api/outreach/drafts/${draft.id}/send`, preview)
        message = 'Sent'
      } catch (cause) {
        message = errorMessage(cause, 'Could not complete this message. Check its status before trying again.')
      }
      setResults((current) => [...current, { id: draft.id, message }])
    }
    setPhase('done')
  }

  return (
    <section className="panel" aria-label="Review email batch">
      <h2>Review {drafts.length} emails</h2>
      <p>Review each recipient, subject and body. Approve and send authorizes only these exact versions. Changed or blocked messages will not send. Each email has its own result; a failure does not undo emails already sent.</p>
      {drafts.map((draft) => (
        <article className="draft-card" key={draft.id}>
          <DraftPreview draft={draft} />
          <label>
            <input type="checkbox" checked={reviewed.includes(draft.id)} disabled={phase !== 'review'}
              onChange={(event) => setReviewed((current) => event.target.checked ? [...current, draft.id] : current.filter((id) => id !== draft.id))} />
            I reviewed this email to {draft.recipients.join(', ')}: {draft.subject}
          </label>
          {results.filter((result) => result.id === draft.id).map((result) => <p role="status" key={result.id}>{result.message}</p>)}
        </article>
      ))}
      <div className="draft-card__actions">
        <button type="button" className="btn btn--secondary" disabled={phase === 'sending'} onClick={onDone}>{phase === 'done' ? 'Back to drafts' : 'Cancel'}</button>
        <button type="button" className="btn btn--primary" disabled={phase !== 'review' || reviewed.length !== drafts.length} onClick={approveAndSend}>
          {phase === 'sending' ? `Sending (${results.length}/${drafts.length})…` : phase === 'done' ? 'Batch finished' : `Approve and send ${drafts.length} emails`}
        </button>
      </div>
    </section>
  )
}

function DraftsTab({ dir }: { dir: Directory }) {
  const [drafts, setDrafts] = useState<OutreachDraft[]>([])
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState('')
  const [companyFilter, setCompanyFilter] = useState('')
  const [showNew, setShowNew] = useState(false)
  const [selected, setSelected] = useState<string[]>([])
  const [batch, setBatch] = useState<OutreachDraft[]>([])

  const load = useCallback(() => {
    setStatus('loading'); setError(null); setSelected([])
    const params = new URLSearchParams()
    if (statusFilter) params.set('status', statusFilter)
    if (companyFilter) params.set('company_id', companyFilter)
    const query = params.toString()
    api.get<OutreachDraft[]>(`/api/outreach/drafts${query ? `?${query}` : ''}`)
      .then((items) => { setDrafts(items); items.forEach((item) => dir.ensureCompany(item.company_id)); setStatus('ready') })
      .catch((cause) => { setError(errorMessage(cause, 'Could not load drafts.')); setStatus('error') })
  }, [statusFilter, companyFilter, dir.ensureCompany])
  useEffect(load, [load])

  if (batch.length > 0) return <BatchReview drafts={batch} onDone={() => { setBatch([]); load() }} />

  return (
    <div className="page" style={{ maxWidth: 'none', padding: 0, gap: 16 }}>
      <div className="panel__row">
        <div className="gmail-card__actions">
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} aria-label="Filter by status">
            <option value="">All statuses</option>
            {['draft', 'approved', 'sending', 'sent', 'delivery_unknown', 'failed'].map((s) => <option key={s} value={s}>{statusLabel(s)}</option>)}
          </select>
          <CompanyPicker id="draft-company-filter" value={companyFilter} onChange={(value) => setCompanyFilter(String(value))}
            emptyLabel="All companies" searchLabel="Search companies to filter drafts" selectLabel="Filter by company"
            onResolved={dir.rememberCompanies} />
        </div>
        <button type="button" className="btn btn--primary" onClick={() => setShowNew((v) => !v)}>
          <Plus size={14} aria-hidden="true" /> {showNew ? 'Close' : 'New draft'}
        </button>
      </div>

      {showNew && <NewDraftForm dir={dir} onCreated={() => { setShowNew(false); load() }} />}

      {status === 'ready' && drafts.some((draft) => draft.status === 'draft' || draft.status === 'approved') && (
        <div className="draft-card__actions">
          <button type="button" className="btn btn--secondary" onClick={() => setSelected(drafts.filter((draft) => draft.status === 'draft' || draft.status === 'approved').map((draft) => draft.id))}>Select all reviewable emails</button>
          <button type="button" className="btn btn--primary" disabled={selected.length === 0} onClick={() => setBatch(drafts.filter((draft) => selected.includes(draft.id)))}>Review selected ({selected.length})</button>
        </div>
      )}

      {status === 'loading' && <LoadingBlock label="Loading drafts…" />}
      {status === 'error' && <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={load} />}
      {status === 'ready' && drafts.length === 0 && <EmptyState title="No drafts" description="Create a draft above, or generate one from a thread reply in Inbox." />}
      {status === 'ready' && drafts.length > 0 && (
        <div className="draft-list">{drafts.map((d) => (
          <div key={d.id}>
            {(d.status === 'draft' || d.status === 'approved') && <label>
              <input type="checkbox" checked={selected.includes(d.id)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, d.id] : current.filter((id) => id !== d.id))} />
              Select {d.subject}
            </label>}
            <DraftCard draft={d} dir={dir} onReload={load} />
          </div>
        ))}</div>
      )}
    </div>
  )
}

// ================================================================== Sequences tab

function emptyStep(index: number): SequenceStep {
  return index === 0
    ? { kind: 'ask_interest', delay_hours: 0, subject: '', body: '', approval: 'per_draft' }
    : { kind: 'ask_interest', delay_hours: 72, subject: '', body: '', approval: 'per_draft' }
}

function StepCard({ step, index, onChange, onRemove }: { step: SequenceStep; index: number; onChange: (step: SequenceStep) => void; onRemove: () => void }) {
  const locked = index === 0
  return (
    <div className="step-card">
      <div className="step-card__head">
        <strong>Step {index}{locked ? ' (cold outreach — locked to ask_interest / per_draft)' : ''}</strong>
        {!locked && <button type="button" className="icon-button" onClick={onRemove} aria-label={`Remove step ${index}`}><Trash2 size={14} aria-hidden="true" /></button>}
      </div>
      <div className="step-grid">
        <label>Kind
          <select
            value={step.kind}
            disabled={locked}
            onChange={(e) => {
              const kind = e.target.value as StepKind
              onChange(kind === 'request_missing_fields' ? { kind, delay_hours: step.delay_hours || 24, approval: 'per_draft' } : { ...step, kind, approval: step.approval })
            }}
          >
            <option value="ask_interest">Ask interest (custom message)</option>
            <option value="request_missing_fields">Request missing fields (auto-generated)</option>
          </select>
        </label>
        <label>Delay after previous step (hours)
          <input type="number" min={locked ? 0 : 1} max={2160} value={step.delay_hours} disabled={locked}
            onChange={(e) => onChange({ ...step, delay_hours: Number(e.target.value) })} />
        </label>
        {step.kind === 'ask_interest' && (
          <label>Approval
            <select value={step.approval} disabled={locked} onChange={(e) => onChange({ ...step, approval: e.target.value as StepApproval })}>
              <option value="per_draft">Per draft (always previewed)</option>
              <option value="template">Template (can be preauthorized for a scope + expiry)</option>
            </select>
          </label>
        )}
      </div>
      {step.kind === 'ask_interest' && (
        <>
          {locked && <label>Subject<input value={step.subject ?? ''} onChange={(e) => onChange({ ...step, subject: e.target.value })} required /></label>}
          {!locked && <p className="muted small">Later steps reply in-thread as “Re: &lt;original subject&gt;” automatically.</p>}
          <label>Body — only <code>{'{company_name}'}</code> and <code>{'{contact_name}'}</code> placeholders are allowed
            <textarea value={step.body ?? ''} onChange={(e) => onChange({ ...step, body: e.target.value })} required />
          </label>
        </>
      )}
      {step.kind === 'request_missing_fields' && <p className="muted small">This step is generated automatically from what's missing and is always reviewed per draft before sending — no subject/body to author here.</p>}
    </div>
  )
}

function SequenceEditor({ sequence, onSave, onCancel, saving }: {
  sequence: Sequence | null
  onSave: (input: SequenceInput) => Promise<void>
  onCancel: () => void
  saving: boolean
}) {
  const [name, setName] = useState(sequence?.name ?? '')
  const [steps, setSteps] = useState<SequenceStep[]>(sequence?.steps ?? [emptyStep(0)])
  const [formError, setFormError] = useState<string | null>(null)

  return (
    <form
      className="panel stack-form"
      onSubmit={async (e) => {
        e.preventDefault()
        if (!name.trim()) { setFormError('Name the sequence.'); return }
        setFormError(null)
        try { await onSave({ name, steps }) } catch (cause) { setFormError(errorMessage(cause, 'Could not save the sequence.')) }
      }}
    >
      <div className="panel__row"><h2>{sequence ? `Edit ${sequence.name}` : 'New sequence'}</h2><button type="button" className="icon-button" onClick={onCancel} aria-label="Cancel"><X size={16} /></button></div>
      {formError && <p className="field-error" role="alert">{formError}</p>}
      <label>Name</label>
      <input value={name} onChange={(e) => setName(e.target.value)} required />
      <div className="step-list">
        {steps.map((step, i) => (
          <StepCard
            key={i}
            step={step}
            index={i}
            onChange={(next) => setSteps((current) => current.map((s, idx) => (idx === i ? next : s)))}
            onRemove={() => setSteps((current) => current.filter((_, idx) => idx !== i))}
          />
        ))}
      </div>
      <button type="button" className="btn btn--secondary" onClick={() => setSteps((current) => [...current, emptyStep(current.length)])} disabled={steps.length >= 20}>
        <Plus size={14} aria-hidden="true" /> Add follow-up step
      </button>
      <div className="dialog__actions">
        <button type="button" className="btn btn--ghost" onClick={onCancel}>Cancel</button>
        <button className="btn btn--primary" disabled={saving}>{saving ? 'Saving…' : sequence ? 'Save (new version)' : 'Create sequence'}</button>
      </div>
    </form>
  )
}

function TemplateApprovalForm({ sequence, stepIndex, dir, onCreated }: { sequence: Sequence; stepIndex: number; dir: Directory; onCreated: (ta: TemplateApproval) => void }) {
  const [companyIds, setCompanyIds] = useState<string[]>([])
  const [hours, setHours] = useState(168)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  return (
    <form
      className="stack-form"
      onSubmit={async (e) => {
        e.preventDefault()
        if (companyIds.length === 0) { setError('Select at least one company.'); return }
        setSaving(true); setError(null)
        try {
          const ta = await api.post<TemplateApproval>(`/api/outreach/sequences/${sequence.id}/template-approvals`, { step_index: stepIndex, company_ids: companyIds, expires_in_hours: hours })
          onCreated(ta)
        } catch (cause) { setError(errorMessage(cause, 'Could not authorize the template.')) }
        finally { setSaving(false) }
      }}
    >
      {error && <p className="field-error" role="alert">{error}</p>}
      <label htmlFor="template-approval-companies">Companies this preauthorization covers</label>
      <CompanyPicker id="template-approval-companies" value={companyIds} multiple
        onChange={(value) => setCompanyIds(Array.isArray(value) ? value : [])} onResolved={dir.rememberCompanies} />
      <label>Expires in (hours, max 720)</label>
      <input type="number" min={1} max={720} value={hours} onChange={(e) => setHours(Number(e.target.value))} />
      <button className="btn btn--secondary" disabled={saving}>{saving ? 'Authorizing…' : `Authorize template for step ${stepIndex}`}</button>
    </form>
  )
}

function SequenceCard({ sequence, dir, onReload }: { sequence: Sequence; dir: Directory; onReload: () => void }) {
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [enrollments, setEnrollments] = useState<Enrollment[]>([])
  const [showEnrollments, setShowEnrollments] = useState(false)
  const [showEnroll, setShowEnroll] = useState(false)
  const [templateStep, setTemplateStep] = useState<number | null>(null)
  const [lastApproval, setLastApproval] = useState<TemplateApproval | null>(null)
  const { push } = useToast()

  function loadEnrollments() {
    api.get<Enrollment[]>(`/api/outreach/enrollments?sequence_id=${sequence.id}`).then((items) => {
      setEnrollments(items)
      items.forEach((item) => dir.ensureCompany(item.company_id))
    }).catch(() => setEnrollments([]))
  }

  async function togglePause() {
    try {
      await api.post(`/api/outreach/sequences/${sequence.id}/${sequence.paused ? 'resume' : 'pause'}`)
      push(sequence.paused ? 'Sequence resumed.' : 'Sequence paused.', 'success')
      onReload()
    } catch (cause) { push(errorMessage(cause, 'Could not update the sequence.'), 'error') }
  }

  async function save(input: SequenceInput) {
    setSaving(true)
    try {
      await api.patch(`/api/outreach/sequences/${sequence.id}`, input)
      push('Sequence saved as a new version. Running enrollments keep their original steps.', 'success')
      setEditing(false); onReload()
    } finally { setSaving(false) }
  }

  async function enrollmentAction(enrollment: Enrollment, action: 'stop' | 'resume') {
    try {
      await api.post(`/api/outreach/enrollments/${enrollment.id}/${action}`)
      loadEnrollments()
    } catch (cause) { push(errorMessage(cause, `Could not ${action} this enrollment.`), 'error') }
  }

  if (editing) return <SequenceEditor sequence={sequence} saving={saving} onSave={save} onCancel={() => setEditing(false)} />

  const templatableSteps = sequence.steps.map((s, i) => ({ s, i })).filter(({ s, i }) => i > 0 && s.approval === 'template')

  return (
    <div className="panel">
      <div className="panel__row">
        <div>
          <h2>{sequence.name}</h2>
          <p className="muted small">v{sequence.version} · {sequence.steps.length} step(s) · <span className={`status-pill status-pill--${sequence.paused ? 'stopped' : 'active'}`}>{sequence.paused ? 'paused' : 'active'}</span></p>
        </div>
        <div className="gmail-card__actions">
          <button type="button" className="btn btn--secondary" onClick={togglePause}>
            {sequence.paused ? <Play size={14} aria-hidden="true" /> : <Pause size={14} aria-hidden="true" />} {sequence.paused ? 'Resume' : 'Pause'}
          </button>
          <button type="button" className="btn btn--secondary" onClick={() => setEditing(true)}>Edit</button>
        </div>
      </div>

      <div className="step-list">
        {sequence.steps.map((step, i) => (
          <div key={i} className="step-card">
            <div className="step-card__head"><strong>Step {i}: {step.kind === 'ask_interest' ? (step.subject || '(no subject)') : 'Request missing fields'}</strong><span className="muted small">{step.approval} · +{step.delay_hours}h</span></div>
            {step.kind === 'ask_interest' && <p className="muted small" style={{ whiteSpace: 'pre-wrap' }}>{step.body}</p>}
          </div>
        ))}
      </div>

      {templatableSteps.length > 0 && (
        <div>
          <p className="muted small">Template-approval steps only cover in-thread follow-ups (never the first cold message), and only for the exact scope + expiry you set.</p>
          <div className="gmail-card__actions">
            {templatableSteps.map(({ i }) => (
              <button key={i} type="button" className="btn btn--ghost" onClick={() => setTemplateStep(templateStep === i ? null : i)}>Authorize step {i}</button>
            ))}
          </div>
          {templateStep !== null && (
            <TemplateApprovalForm sequence={sequence} stepIndex={templateStep} dir={dir} onCreated={(ta) => { setLastApproval(ta); setTemplateStep(null); push('Template authorized for the selected companies.', 'success') }} />
          )}
          {lastApproval && (
            <p className="muted small">
              Approval <code className="mono">{lastApproval.id}</code> for step {lastApproval.step_index}, {lastApproval.company_ids.length} company(ies), expires {formatDateTime(lastApproval.expires_at)}.{' '}
              <button type="button" className="btn btn--ghost" onClick={async () => { await api.delete(`/api/outreach/template-approvals/${lastApproval.id}`); setLastApproval(null); push('Template approval revoked.', 'success') }}>Revoke</button>
            </p>
          )}
        </div>
      )}

      <div className="gmail-card__actions">
        <button type="button" className="btn btn--secondary" onClick={() => setShowEnroll((v) => !v)}>{showEnroll ? 'Close' : 'Enroll a contact'}</button>
        <button type="button" className="btn btn--ghost" onClick={() => { setShowEnrollments((v) => !v); if (!showEnrollments) loadEnrollments() }}>{showEnrollments ? 'Hide' : 'Show'} enrollments</button>
      </div>

      {showEnroll && <EnrollForm sequence={sequence} dir={dir} onEnrolled={() => { setShowEnroll(false); loadEnrollments(); setShowEnrollments(true) }} />}

      {showEnrollments && (
        <table className="enrollment-table">
          <thead><tr><th>Company</th><th>Contact</th><th>Step</th><th>State</th><th>Next run</th><th></th></tr></thead>
          <tbody>
            {enrollments.map((e) => (
              <tr key={e.id}>
                <td><Link to={`/companies/${e.company_id}`}>{dir.companyName(e.company_id)}</Link></td>
                <td>{dir.contactLabel(e.company_id, e.contact_id)}</td>
                <td>{e.step_index + 1} / {e.steps}</td>
                <td><span className={`status-pill status-pill--${e.state}`}>{statusLabel(e.state)}</span></td>
                <td>{formatDateTime(e.next_run_at)}</td>
                <td>
                  {(e.state === 'active' || e.state === 'awaiting_review') && <button type="button" className="btn btn--ghost" onClick={() => enrollmentAction(e, 'stop')}>Stop</button>}
                  {e.state === 'awaiting_review' && <button type="button" className="btn btn--ghost" onClick={() => enrollmentAction(e, 'resume')}>Resume</button>}
                </td>
              </tr>
            ))}
            {enrollments.length === 0 && <tr><td colSpan={6} className="muted">No enrollments yet.</td></tr>}
          </tbody>
        </table>
      )}
    </div>
  )
}

function EnrollForm({ sequence, dir, onEnrolled }: { sequence: Sequence; dir: Directory; onEnrolled: () => void }) {
  const [companyId, setCompanyId] = useState('')
  const [contactId, setContactId] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { push } = useToast()
  const contacts = companyId ? dir.contactsFor(companyId) : undefined
  useEffect(() => { if (companyId) dir.ensureContacts(companyId) }, [companyId, dir])

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!companyId || !contactId) { setError('Choose a verified contact.'); return }
    setSaving(true); setError(null)
    try {
      await api.post(`/api/outreach/sequences/${sequence.id}/enrollments`, { company_id: companyId, contact_id: contactId })
      push('Contact enrolled.', 'success')
      onEnrolled()
    } catch (cause) { setError(errorMessage(cause, 'Could not enroll this contact.')) }
    finally { setSaving(false) }
  }

  return (
    <form className="stack-form" onSubmit={submit}>
      {error && <p className="field-error" role="alert">{error}</p>}
      <label htmlFor="enroll-company">Company</label>
      <CompanyPicker id="enroll-company" value={companyId} onChange={(value) => { const id = String(value); setCompanyId(id); setContactId('') }}
        required onResolved={dir.rememberCompanies} />
      <label>Contact (verified only)</label>
      <select value={contactId} onChange={(e) => setContactId(e.target.value)} required disabled={!companyId}>
        <option value="">{contacts === undefined ? 'Loading…' : 'Select a contact…'}</option>
        {contacts?.filter((c) => c.verification === 'verified' && c.email).map((c) => <option key={c.id} value={c.id}>{c.name} — {c.email}</option>)}
      </select>
      {contacts && contacts.filter((c) => c.verification === 'verified' && c.email).length === 0 && <p className="muted small">No verified contacts with an email for this company yet.</p>}
      <button className="btn btn--primary" disabled={saving}>{saving ? 'Enrolling…' : 'Enroll'}</button>
    </form>
  )
}

function SequencesTab({ dir }: { dir: Directory }) {
  const [sequences, setSequences] = useState<Sequence[]>([])
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [saving, setSaving] = useState(false)
  const { push } = useToast()

  const load = useCallback(() => {
    setStatus('loading'); setError(null)
    api.get<Sequence[]>('/api/outreach/sequences')
      .then((items) => { setSequences(items); setStatus('ready') })
      .catch((cause) => { setError(errorMessage(cause, 'Could not load sequences.')); setStatus('error') })
  }, [])
  useEffect(load, [load])

  async function create(input: SequenceInput) {
    setSaving(true)
    try {
      await api.post('/api/outreach/sequences', input)
      push('Sequence created.', 'success')
      setCreating(false); load()
    } finally { setSaving(false) }
  }

  return (
    <div>
      <div className="panel__row">
        <p className="muted small">Sequences are a linear list of steps — no loops. Step 0 is always cold outreach with a per-draft preview.</p>
        <button type="button" className="btn btn--primary" onClick={() => setCreating((v) => !v)}><Plus size={14} aria-hidden="true" /> {creating ? 'Close' : 'New sequence'}</button>
      </div>
      {creating && <SequenceEditor sequence={null} saving={saving} onSave={create} onCancel={() => setCreating(false)} />}
      {status === 'loading' && <LoadingBlock label="Loading sequences…" />}
      {status === 'error' && <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={load} />}
      {status === 'ready' && sequences.length === 0 && !creating && <EmptyState title="No sequences yet" description="Create a linear sequence of steps above." />}
      {status === 'ready' && sequences.map((s) => <SequenceCard key={s.id} sequence={s} dir={dir} onReload={load} />)}
    </div>
  )
}

// ================================================================== Controls tab

function SuppressionForm({ dir, onAdd }: { dir: Directory; onAdd: (input: SuppressionInput) => Promise<void> }) {
  const [kind, setKind] = useState<SuppressionInput['kind']>('email')
  const [value, setValue] = useState('')
  const [reason, setReason] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => { setValue(kind === 'channel' ? 'email' : '') }, [kind])

  return (
    <form
      className="stack-form"
      onSubmit={async (e) => {
        e.preventDefault()
        setSaving(true)
        try { await onAdd({ kind, value, reason }); setValue(kind === 'channel' ? 'email' : ''); setReason('') }
        finally { setSaving(false) }
      }}
    >
      <div className="field-row">
        <div className="field-col">
          <label>Kind</label>
          <select value={kind} onChange={(e) => setKind(e.target.value as SuppressionInput['kind'])}>
            <option value="email">Email address</option>
            <option value="company">Company</option>
            <option value="channel">Channel (email)</option>
          </select>
        </div>
        <div className="field-col">
          <label>Value</label>
          {kind === 'company' ? (
            <CompanyPicker id="suppression-company" value={value} onChange={(next) => setValue(String(next))}
              required onResolved={dir.rememberCompanies} />
          ) : kind === 'channel' ? (
            <input value="email" readOnly disabled />
          ) : (
            <input type="email" value={value} onChange={(e) => setValue(e.target.value)} required placeholder="name@company.com" />
          )}
        </div>
      </div>
      <label>Reason</label>
      <input value={reason} onChange={(e) => setReason(e.target.value)} required minLength={1} maxLength={300} placeholder="Why this is being suppressed" />
      <button className="btn btn--secondary" style={{ alignSelf: 'flex-start' }} disabled={saving}><Plus size={14} aria-hidden="true" /> {saving ? 'Adding…' : 'Add suppression'}</button>
    </form>
  )
}

function ControlsTab({ dir }: { dir: Directory }) {
  const [controls, setControls] = useState<OutreachControls | null>(null)
  const [suppressions, setSuppressions] = useState<Suppression[]>([])
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [busy, setBusy] = useState(false)
  const [reconcileResult, setReconcileResult] = useState<ReconcileResponse | null>(null)
  const { push } = useToast()

  const load = useCallback(() => {
    setStatus('loading')
    Promise.all([api.get<OutreachControls>('/api/outreach/controls'), api.get<Suppression[]>('/api/outreach/suppressions')])
      .then(([c, s]) => { setControls(c); setSuppressions(s); s.filter((item) => item.kind === 'company').forEach((item) => dir.ensureCompany(item.value)); setStatus('ready') })
      .catch(() => setStatus('error'))
  }, [dir.ensureCompany])
  useEffect(load, [load])

  async function toggleStop() {
    if (!controls) return
    const next = !controls.stopped
    if (next && !window.confirm('Stop all outbound email and sequence sends immediately?')) return
    setBusy(true)
    try {
      const result = await api.post<OutreachControls>('/api/outreach/stop', { stopped: next })
      setControls(result)
      push(result.stopped ? 'All outbound stopped.' : 'Outbound resumed.', 'success')
    } catch (cause) { push(errorMessage(cause, 'Could not change the stop switch.'), 'error') }
    finally { setBusy(false) }
  }

  async function addSuppression(input: SuppressionInput) {
    try {
      await api.post<SuppressionInput & { suppressed: boolean }>('/api/outreach/suppressions', input)
      push('Suppression added.', 'success')
      load()
    } catch (cause) { push(errorMessage(cause, 'Could not add the suppression.'), 'error') }
  }

  async function removeSuppression(s: Suppression) {
    if (!window.confirm(`Remove the suppression for "${s.value}"?`)) return
    try { await api.delete(`/api/outreach/suppressions/${s.id}`); load() }
    catch (cause) { push(errorMessage(cause, 'Could not remove this suppression.'), 'error') }
  }

  async function reconcile() {
    setBusy(true)
    try {
      const result = await api.post<ReconcileResponse>('/api/outreach/reconcile')
      setReconcileResult(result)
      push(`Reconciled ${result.reconciled.length} uncertain send(s).`, 'success')
    } catch (cause) { push(errorMessage(cause, 'Reconcile failed.'), 'error') }
    finally { setBusy(false) }
  }

  if (status === 'loading') return <LoadingBlock label="Loading controls…" />
  if (status === 'error' || !controls) return <ErrorBlock message="Could not load outreach controls." onRetry={load} />

  return (
    <div>
      <div className={`stop-banner ${controls.stopped ? 'stop-banner--stopped' : 'stop-banner--running'}`}>
        <div>
          <strong>{controls.stopped ? 'All outbound is stopped' : 'Outbound is running'}</strong>
          <p className="muted small">{controls.stopped ? 'No send or sequence step will go out until this is turned off.' : 'Sends and sequence steps proceed through their normal approval gates.'}</p>
        </div>
        <button type="button" className={`btn ${controls.stopped ? 'btn--primary' : 'btn--secondary'}`} disabled={busy} onClick={toggleStop}>
          <Ban size={14} aria-hidden="true" /> {controls.stopped ? 'Resume outbound' : 'Stop all outbound'}
        </button>
      </div>

      <section className="panel">
        <h2>Reconcile uncertain deliveries</h2>
        <p className="muted small">Searches Sent mail for the deterministic Message-ID of any send that timed out or crashed mid-flight. Never resends.</p>
        <button type="button" className="btn btn--secondary" disabled={busy} onClick={reconcile}><RefreshCw size={14} aria-hidden="true" /> Reconcile now</button>
        {reconcileResult && (
          <ul className="record-list">
            {reconcileResult.reconciled.map((r) => (
              <li key={r.dispatch_id} className="record-list__item">
                <span className={`status-pill status-pill--${r.state}`}>{statusLabel(r.state)}</span> dispatch {r.dispatch_id}{r.error ? ` — ${r.error}` : ''}
              </li>
            ))}
            {reconcileResult.reconciled.length === 0 && <li className="muted">Nothing needed reconciling.</li>}
          </ul>
        )}
      </section>

      <section className="panel">
        <h2>Suppression list</h2>
        <p className="muted small">Opt-outs are added automatically and cannot be removed here. Manual suppressions can be added and removed.</p>
        <SuppressionForm dir={dir} onAdd={addSuppression} />
        <table className="suppression-table">
          <thead><tr><th>Kind</th><th>Value</th><th>Reason</th><th>Source</th><th></th></tr></thead>
          <tbody>
            {suppressions.map((s) => (
              <tr key={s.id}>
                <td>{s.kind}</td>
                <td>{s.kind === 'company' ? dir.companyName(s.value) : s.value}</td>
                <td>{s.reason}</td>
                <td>{s.source}</td>
                <td>{s.source !== 'optout' && <button type="button" className="icon-button" aria-label="Remove suppression" onClick={() => removeSuppression(s)}><Trash2 size={14} aria-hidden="true" /></button>}</td>
              </tr>
            ))}
            {suppressions.length === 0 && <tr><td colSpan={5} className="muted">No suppressions recorded.</td></tr>}
          </tbody>
        </table>
      </section>
    </div>
  )
}

// ================================================================== root view

export function OutreachView() {
  const [gmailStatus, setGmailStatus] = useState<GmailStatus | null>(null)
  const [tab, setTab] = useState<Tab>('Inbox')
  const dir = useDirectory()
  const { push } = useToast()

  const loadGmailStatus = useCallback(() => {
    api.get<GmailStatus>('/api/gmail/status').then(setGmailStatus).catch(() => setGmailStatus(null))
  }, [])
  useEffect(loadGmailStatus, [loadGmailStatus])

  // Handle the redirect back from Google's OAuth screen (?gmail=connected|error&reason=...).
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const result = params.get('gmail')
    if (!result) return
    if (result === 'connected') push('Gmail connected.', 'success')
    else push(`Gmail connection failed${params.get('reason') ? `: ${params.get('reason')}` : ''}.`, 'error')
    window.history.replaceState({}, '', window.location.pathname)
    loadGmailStatus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="page outreach">
      <header className="page__header">
        <h1>Outreach</h1>
        <p className="page__lede">Connect Gmail, review replies, and approve exact data requests individually or in a batch before sending.</p>
      </header>

      <GmailConnectionCard status={gmailStatus} onChange={loadGmailStatus} />

      <nav className="view-tabs" aria-label="Outreach sections">
        {TABS.map((item) => (
          <button type="button" key={item} className={tab === item ? 'is-active' : ''} aria-current={tab === item ? 'page' : undefined} onClick={() => setTab(item)}>{item}</button>
        ))}
      </nav>

      {dir.companiesStatus === 'error' && <ErrorBlock message="Could not load companies for the dropdowns." onRetry={dir.reloadCompanies} />}

      {tab === 'Inbox' && <InboxTab dir={dir} />}
      {tab === 'Drafts' && <DraftsTab dir={dir} />}
      {tab === 'Sequences' && <SequencesTab dir={dir} />}
      {tab === 'Controls' && <ControlsTab dir={dir} />}
    </div>
  )
}
