import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { Sparkles, X, ArrowUp, Plus, MessageSquare, ArrowUpRight } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import { useRouter } from '../lib/router'
import './assistant.css'

type Action = { kind: string; tool?: string; status?: number; link?: string; path?: string; result?: unknown }
type ChatMessage = { id: string; role: 'user' | 'assistant'; content: string; actions: Action[]; created_at?: string }
type Conversation = { id: string; title: string; updated_at: string }
type ConversationDetail = { id: string; title: string; messages: ChatMessage[] }
const allowedLinks = new Set(['/companies', '/futures', '/matches', '/outreach', '/voice-notes', '/voice-notes?tab=notes'])
const errorText = (cause: unknown) => cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : 'The assistant request failed.'
function friendlyName(name?: string) { return (name || 'Workspace update').replace(/^(get|post|patch)_/i, '').replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()) }
function actionsFrom(error: ApiError): Action[] { const value = error.detail; return value && typeof value === 'object' && 'actions' in value && Array.isArray(value.actions) ? value.actions as Action[] : [] }
function conversationFrom(error: ApiError): string | null { const value = error.detail; return value && typeof value === 'object' && 'conversation_id' in value && typeof value.conversation_id === 'string' ? value.conversation_id : null }
function resultSummary(result: unknown) {
  if (!result || typeof result !== 'object') return typeof result === 'string' ? result : ''
  const value = result as Record<string, unknown>
  if (typeof value.error === 'string') return value.error
  if (typeof value.message === 'string') return value.message
  const name = value.name ?? value.title ?? value.summary
  return typeof name === 'string' ? name : ''
}
function needsHumanReview(result: unknown) { return !!result && typeof result === 'object' && 'review_status' in result && result.review_status === 'proposed' }

export function AssistantPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [draft, setDraft] = useState('')
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [loadingHistory, setLoadingHistory] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [failureActions, setFailureActions] = useState<Action[]>([])
  const bodyRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const requestRef = useRef(false)
  const { navigate } = useRouter()

  const loadConversations = useCallback(async () => {
    try { setConversations(await api.get<Conversation[]>('/api/assistant/conversations')); setHistoryError(null) }
    catch (cause) { setHistoryError(errorText(cause)) }
  }, [])
  const loadConversation = useCallback(async (id: string) => {
    setLoadingHistory(true); setHistoryError(null); setError(null); setFailureActions([])
    try {
      const detail = await api.get<ConversationDetail>(`/api/assistant/conversations/${id}`)
      setConversationId(detail.id); setMessages(detail.messages)
    } catch (cause) { setHistoryError(errorText(cause)) }
    finally { setLoadingHistory(false) }
  }, [])
  useEffect(() => { void loadConversations() }, [loadConversations])
  useEffect(() => { if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight }, [messages, sending, failureActions, open])
  useEffect(() => {
    if (!open) return
    const previousFocus = document.activeElement
    inputRef.current?.focus()
    const onKeyDown = (event: globalThis.KeyboardEvent) => { if (event.key === 'Escape' && !document.querySelector('dialog[open]')) onClose() }
    window.addEventListener('keydown', onKeyDown)
    return () => { window.removeEventListener('keydown', onKeyDown); if (previousFocus instanceof HTMLElement) previousFocus.focus() }
  }, [open, onClose])

  function newConversation() {
    setConversationId(null); setMessages([]); setDraft(''); setError(null); setFailureActions([]); setHistoryError(null)
  }
  async function reloadAfterFailure(id: string | null, errorActions: Action[]) {
    if (id) {
      try { const detail = await api.get<ConversationDetail>(`/api/assistant/conversations/${id}`); setConversationId(detail.id); setMessages(detail.messages); setFailureActions([]) }
      catch { setFailureActions(errorActions) }
    } else setFailureActions(errorActions)
    await loadConversations()
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault()
    const text = draft.trim()
    if (!text || sending || requestRef.current) return
    requestRef.current = true; setSending(true); setError(null); setFailureActions([])
    setMessages((items) => [...items, { id: `pending-${Date.now()}`, role: 'user', content: text, actions: [] }])
    try {
      const response = await api.post<{ conversation_id: string; reply: string; actions: Action[] }>('/api/assistant', { message: text, ...(conversationId ? { conversation_id: conversationId } : {}) })
      setConversationId(response.conversation_id)
      setDraft('')
      await Promise.all([loadConversation(response.conversation_id), loadConversations()])
    } catch (cause) {
      const errActions = cause instanceof ApiError ? actionsFrom(cause) : []
      const id = cause instanceof ApiError ? conversationFrom(cause) ?? conversationId : conversationId
      setError(errorText(cause))
      if (id) setConversationId(id)
      await reloadAfterFailure(id, errActions)
    } finally { requestRef.current = false; setSending(false) }
  }

  function onComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit() }
  }
  function openAction(action: Action) {
    const path = action.path ?? action.link
    if (typeof path === 'string' && allowedLinks.has(path)) { navigate(path); onClose() }
  }

  return <aside className="assistant-panel" id="assistant-panel" aria-label="Assistant" hidden={!open}>
    <div className="assistant-panel__header"><div className="assistant-panel__title"><Sparkles size={16} aria-hidden="true" /><span>Assistant</span></div><div className="assistant-panel__header-actions"><button type="button" className="icon-button" onClick={newConversation} aria-label="New conversation" title="New conversation"><Plus size={16} aria-hidden="true" /></button><button type="button" className="icon-button" onClick={onClose} aria-label="Collapse assistant panel" title="Close (Esc)"><X size={16} aria-hidden="true" /></button></div></div>
    <details className="assistant-panel__history"><summary><MessageSquare size={14} aria-hidden="true" /> Conversations{conversations.length ? ` (${conversations.length})` : ''}</summary><div className="assistant-panel__history-list">{historyError && <p className="assistant-panel__muted" role="alert">{historyError}</p>}{!historyError && conversations.length === 0 && <p className="assistant-panel__muted">No saved conversations yet.</p>}{conversations.map((item) => <button type="button" key={item.id} className={item.id === conversationId ? 'is-current' : ''} onClick={() => void loadConversation(item.id)}>{item.title || 'New conversation'}<time>{new Date(item.updated_at).toLocaleDateString()}</time></button>)}</div></details>
    <div className="assistant-panel__body" ref={bodyRef} aria-live="polite">
      {loadingHistory && <p className="assistant-panel__muted">Loading conversation…</p>}
      {historyError && !loadingHistory && <p className="assistant-panel__error" role="alert">{historyError}</p>}
      {!loadingHistory && messages.length === 0 && <div className="assistant-panel__empty"><p>Ask about companies, mandates, matches, or notes. The assistant can prepare reviewable actions and drafts.</p></div>}
      {messages.map((message) => <article className={`assistant-panel__message is-${message.role}`} key={message.id}><p className="assistant-panel__role">{message.role === 'user' ? 'You' : 'Assistant'}</p><p className="assistant-panel__text">{message.content}</p>{message.actions?.map((action, index) => <ActionCard key={`${message.id}-${index}`} action={action} onOpen={() => openAction(action)} />)}</article>)}
      {sending && <p className="assistant-panel__muted" role="status">Working…</p>}
      {error && <div className="assistant-panel__error" role="alert"><p>{error}</p><p>Completed actions are shown below when available. You can retry your message.</p></div>}
      {failureActions.map((action, index) => <ActionCard key={`failed-${index}`} action={action} onOpen={() => openAction(action)} />)}
    </div>
    <form className="assistant-panel__composer" onSubmit={sendMessage}><label htmlFor="assistant-input" className="sr-only">Message the assistant</label><div className="assistant-panel__input"><textarea id="assistant-input" ref={inputRef} rows={2} placeholder="Ask the assistant…" value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={onComposerKeyDown} maxLength={4000} disabled={sending} /><button type="submit" className="assistant-panel__send" disabled={sending || !draft.trim()} aria-label={sending ? 'Sending…' : 'Send'} title="Send"><ArrowUp size={16} aria-hidden="true" /></button></div><span className="assistant-panel__hint">Enter to send · Shift+Enter for a new line</span></form>
  </aside>
}

function ActionCard({ action, onOpen }: { action: Action; onOpen: () => void }) {
  const isBrowserAction = ['navigate', 'open_recorder', 'open_voice'].includes(action.kind)
  const target = action.path ?? action.link
  const canOpen = typeof target === 'string' && allowedLinks.has(target)
  const failed = action.kind === 'error' || (typeof action.status === 'number' && action.status >= 400)
  const summary = resultSummary(action.result)
  const review = needsHumanReview(action.result)
  return <div className={`assistant-panel__action ${failed ? 'is-failed' : ''}`}><div><strong>{isBrowserAction ? action.kind === 'open_recorder' ? 'Open notes recorder' : action.kind === 'open_voice' ? 'Open voice screen' : 'Open workspace section' : friendlyName(action.tool)}</strong><span>{failed ? 'Needs attention' : review ? 'Proposal · human review required' : isBrowserAction ? 'Ready to open' : 'Action completed'}</span>{summary && <p>{summary}</p>}</div>{canOpen && <button type="button" onClick={onOpen}>Open <ArrowUpRight size={13} /></button>}</div>
}
