// Voice notes UI: record or upload audio, track upload/transcription
// progress, review the raw ASR transcript alongside an editable correction,
// and review proposed summary facts/action items with their source quotes.
//
// This component owns no shared app files; it only talks to the notes API
// described in backend/permetheus/notes.py through web/src/lib/recordingStore.ts.
// See web/NOTES_UI.md for the full behavior write-up.

import {
  Component,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type ErrorInfo,
  type ReactNode,
} from 'react'
import {
  AlertCircle,
  AlertTriangle,
  Building2,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock,
  FileAudio,
  Loader2,
  Mic,
  Pencil,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Square,
  Trash2,
  Upload,
  X,
} from 'lucide-react'
import { api } from '../lib/api'
import type { Company } from '../lib/types'
import { useToast } from '../lib/toast'
import {
  createNote,
  deleteNote,
  describeApiError,
  describeMicError,
  finalizeNote,
  formatElapsed,
  getNote,
  guessMimeTypeFromFilename,
  isSupportedMimeType,
  listNotes,
  listSessions,
  noteAudioUrl,
  patchNote,
  pickMicMimeType,
  startMicRecording,
  SUPPORTED_MIME_TYPES,
  uploadAudioFile,
  uploadMissingSequences,
  deleteChunksForNote,
  deleteSession,
  type MicRecordingController,
  type Note,
  type RecordingSessionRecord,
} from '../lib/recordingStore'
import './notes.css'

const POLL_INTERVAL_MS = 2500
const POLLED_STATUSES = new Set(['queued', 'processing'])

// ---------------------------------------------------------------------------
// Error boundary — catches render-time failures without taking the app down.
// ---------------------------------------------------------------------------

class NotesErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('NotesPanel crashed', error, info)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="page">
          <div className="state-block state-block--error" role="alert">
            <AlertCircle size={20} aria-hidden="true" />
            <div className="state-block__body">
              <p>Something went wrong in the notes panel: {this.state.error.message}</p>
              <button type="button" className="btn btn--ghost" onClick={() => this.setState({ error: null })}>
                <RefreshCw size={14} aria-hidden="true" />
                Try again
              </button>
            </div>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

export function NotesPanel() {
  return (
    <NotesErrorBoundary>
      <NotesPanelInner />
    </NotesErrorBoundary>
  )
}

// ---------------------------------------------------------------------------
// Main panel.
// ---------------------------------------------------------------------------

type ComposerMode = 'idle' | 'recording' | 'uploading-file'

function NotesPanelInner() {
  const { push } = useToast()

  const [companies, setCompanies] = useState<Company[]>([])
  const [notes, setNotes] = useState<Note[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [listStatus, setListStatus] = useState<'loading' | 'ready' | 'error'>('loading')

  const [title, setTitle] = useState('')
  const [companyId, setCompanyId] = useState('')
  const [mode, setMode] = useState<ComposerMode>('idle')
  const [composerError, setComposerError] = useState<string | null>(null)
  const [elapsedMs, setElapsedMs] = useState(0)
  const [chunkStats, setChunkStats] = useState({ persisted: 0, uploaded: 0, failed: 0 })
  const [fileProgress, setFileProgress] = useState<{ done: number; total: number } | null>(null)

  const [sessions, setSessions] = useState<RecordingSessionRecord[]>([])
  const [selectedNoteId, setSelectedNoteId] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Note | null>(null)

  const controllerRef = useRef<MicRecordingController | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const pollTimerRef = useRef<number | undefined>(undefined)

  const loadNotes = useCallback(() => {
    setListStatus((prev) => (prev === 'ready' ? 'ready' : 'loading'))
    listNotes()
      .then((items) => {
        setNotes(items)
        setListStatus('ready')
        setListError(null)
      })
      .catch((cause) => {
        setListError(describeApiError(cause, 'Could not load notes.'))
        setListStatus('error')
      })
  }, [])

  const loadSessions = useCallback(() => {
    listSessions().then(setSessions).catch(() => setSessions([]))
  }, [])

  useEffect(() => {
    loadNotes()
    loadSessions()
    api
      .get<Company[] | { items: Company[] }>('/api/companies')
      .then((response) => setCompanies(Array.isArray(response) ? response : response.items))
      .catch(() => setCompanies([]))
  }, [loadNotes, loadSessions])

  // Poll the list while any note is actively processing on the server.
  useEffect(() => {
    const needsPoll = notes?.some((note) => POLLED_STATUSES.has(note.status)) ?? false
    if (!needsPoll) return
    pollTimerRef.current = window.setTimeout(() => loadNotes(), POLL_INTERVAL_MS)
    return () => {
      if (pollTimerRef.current !== undefined) window.clearTimeout(pollTimerRef.current)
    }
  }, [notes, loadNotes])

  // Release the microphone and persist partial audio if this panel unmounts
  // mid-recording (e.g. the user navigates to a different section).
  useEffect(() => {
    return () => {
      if (controllerRef.current) {
        controllerRef.current.abortForUnmount()
        push('Recording stopped because you left the notes panel. Recover it from the list below.', 'info')
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Warn (browser-native) before a full page unload while recording.
  useEffect(() => {
    function onBeforeUnload(event: BeforeUnloadEvent) {
      if (mode !== 'recording') return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [mode])

  async function handleStartRecording() {
    setComposerError(null)
    if (!title.trim()) {
      setComposerError('Give the recording a title before starting.')
      return
    }
    const mimeType = pickMicMimeType()
    if (!mimeType) {
      setComposerError('This browser cannot record audio in a format the server accepts.')
      return
    }
    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (cause) {
      const info = describeMicError(cause)
      setComposerError(`${info.title}: ${info.message}`)
      return
    }
    try {
      const note = await createNote(title.trim(), companyId || null)
      setChunkStats({ persisted: 0, uploaded: 0, failed: 0 })
      setElapsedMs(0)
      controllerRef.current = startMicRecording(
        stream,
        { id: note.id, title: title.trim(), companyId: companyId || null },
        mimeType,
        {
          onElapsedChange: setElapsedMs,
          onChunkPersisted: () => setChunkStats((s) => ({ ...s, persisted: s.persisted + 1 })),
          onChunkUploaded: () => setChunkStats((s) => ({ ...s, uploaded: s.uploaded + 1 })),
          onUploadError: (_seq, message) => {
            setChunkStats((s) => ({ ...s, failed: s.failed + 1 }))
            setComposerError(`Some audio failed to upload: ${message}`)
          },
          onStopped: (reason) => {
            if (reason === 'max-duration') push('Recording stopped automatically at the 4-hour limit.', 'info')
          },
        },
      )
      setMode('recording')
    } catch (cause) {
      stream.getTracks().forEach((track) => track.stop())
      setComposerError(describeApiError(cause, 'Could not start the recording.'))
    }
  }

  async function handleStopRecording() {
    const controller = controllerRef.current
    if (!controller) return
    setComposerError('Finishing upload…')
    try {
      const result = await controller.stop()
      controllerRef.current = null
      if (result.failedSequences.length > 0) {
        setMode('idle')
        setComposerError(
          `Recording captured (${result.chunkCount} chunks) but ${result.failedSequences.length} did not upload. Recover it from the list below to retry.`,
        )
        loadSessions()
        return
      }
      await finalizeNote(controller.noteId, result.chunkCount, controller.mimeType)
      await deleteChunksForNote(controller.noteId)
      await deleteSession(controller.noteId)
      setMode('idle')
      setComposerError(null)
      setTitle('')
      setCompanyId('')
      push('Recording uploaded. Transcription is queued.', 'success')
      loadNotes()
      loadSessions()
    } catch (cause) {
      setMode('idle')
      setComposerError(describeApiError(cause, 'Could not finish the recording.'))
      loadSessions()
    }
  }

  async function handleFilePicked(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setComposerError(null)
    if (!title.trim()) {
      setComposerError('Give the recording a title before uploading.')
      return
    }
    const mimeType = file.type || guessMimeTypeFromFilename(file.name) || ''
    if (!mimeType || !isSupportedMimeType(mimeType)) {
      setComposerError(
        `Unsupported audio type. Supported formats: ${SUPPORTED_MIME_TYPES.map((t) => t.split('/')[1]).join(', ')}.`,
      )
      return
    }
    setMode('uploading-file')
    setFileProgress({ done: 0, total: 1 })
    try {
      const note = await createNote(title.trim(), companyId || null)
      const result = await uploadAudioFile(note.id, file, mimeType, {
        onChunkUploaded: (sequence, chunkCount) => setFileProgress({ done: sequence + 1, total: chunkCount }),
        onUploadError: (_seq, message) => setComposerError(`Some parts failed to upload: ${message}`),
      })
      if (result.failedSequences.length > 0) {
        setComposerError(
          `Upload captured (${result.chunkCount} parts) but ${result.failedSequences.length} did not finish. Recover it from the list below to retry.`,
        )
        loadSessions()
      } else {
        await finalizeNote(note.id, result.chunkCount, mimeType)
        await deleteChunksForNote(note.id)
        await deleteSession(note.id)
        setTitle('')
        setCompanyId('')
        push('File uploaded. Transcription is queued.', 'success')
        loadNotes()
      }
    } catch (cause) {
      setComposerError(describeApiError(cause, 'Could not upload this file.'))
      loadSessions()
    } finally {
      setMode('idle')
      setFileProgress(null)
    }
  }

  async function handleRecoverSession(session: RecordingSessionRecord) {
    const total = session.expectedChunkCount ?? session.nextSequence
    if (total <= 0) {
      await deleteSession(session.noteId)
      await deleteChunksForNote(session.noteId)
      loadSessions()
      return
    }
    push('Recovering captured audio…', 'info')
    try {
      const failed = await uploadMissingSequences(session.noteId, total)
      if (failed.length > 0) {
        push(`${failed.length} chunk(s) still failing to upload. Try recovering again once you're back online.`, 'error')
        return
      }
      await finalizeNote(session.noteId, total, session.mimeType)
      if (session.kind === 'microphone') {
        await patchNote(session.noteId, {
          title: `${session.title} (recovered — recording was interrupted)`,
        })
      }
      await deleteChunksForNote(session.noteId)
      await deleteSession(session.noteId)
      push('Recovered. Transcription is queued.', 'success')
      loadNotes()
      loadSessions()
    } catch (cause) {
      push(describeApiError(cause, 'Could not recover this recording.'), 'error')
    }
  }

  async function handleDiscardSession(session: RecordingSessionRecord) {
    try {
      await deleteNote(session.noteId).catch(() => undefined)
      await deleteChunksForNote(session.noteId)
      await deleteSession(session.noteId)
      push('Discarded interrupted recording.', 'info')
      loadSessions()
      loadNotes()
    } catch (cause) {
      push(describeApiError(cause, 'Could not discard this recording.'), 'error')
    }
  }

  async function handleConfirmDelete() {
    if (!deleteTarget) return
    const target = deleteTarget
    setDeleteTarget(null)
    try {
      await deleteNote(target.id)
      if (selectedNoteId === target.id) setSelectedNoteId(null)
      push('Note deleted.', 'success')
      loadNotes()
    } catch (cause) {
      push(describeApiError(cause, 'Could not delete this note.'), 'error')
    }
  }

  const isBusy = mode !== 'idle'

  return (
    <div className="page notes-panel">
      <header className="page__header">
        <h1>Voice notes</h1>
        <p className="page__lede">
          Record a meeting or upload an existing audio file. Raw transcript and reviewed corrections stay
          visibly separate; summaries are proposals with source quotes, never automatic changes.
        </p>
      </header>

      {sessions.length > 0 && (
        <div className="notes-recovery" role="region" aria-label="Interrupted recordings">
          <strong className="notes-recovery__title">
            <AlertTriangle size={16} aria-hidden="true" /> Interrupted recordings found on this device
          </strong>
          <ul className="notes-recovery__list">
            {sessions.map((session) => (
              <li key={session.noteId} className="notes-recovery__item">
                <span>
                  <strong>{session.title || 'Untitled'}</strong>{' '}
                  <span className="muted small">
                    {session.kind === 'microphone' ? 'microphone recording' : 'file upload'} — captured{' '}
                    {session.expectedChunkCount ?? session.nextSequence} part(s)
                  </span>
                </span>
                <span className="notes-recovery__actions">
                  <button type="button" className="btn btn--secondary" onClick={() => handleRecoverSession(session)}>
                    <RotateCcw size={14} aria-hidden="true" />
                    Recover
                  </button>
                  <button type="button" className="btn btn--ghost" onClick={() => handleDiscardSession(session)}>
                    <Trash2 size={14} aria-hidden="true" />
                    Discard
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="panel notes-composer">
        <div className="field-row">
          <div className="field-col">
            <label htmlFor="note-title">Title</label>
            <input
              id="note-title"
              type="text"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="Weekly sync with Acme Oy"
              disabled={isBusy}
            />
          </div>
          <div className="field-col">
            <label htmlFor="note-company">
              <Building2 size={13} aria-hidden="true" /> Company (optional)
            </label>
            <select
              id="note-company"
              value={companyId}
              onChange={(event) => setCompanyId(event.target.value)}
              disabled={isBusy}
            >
              <option value="">No company</option>
              {companies.map((company) => (
                <option key={company.id} value={company.id}>
                  {company.name || 'Unnamed company'}
                </option>
              ))}
            </select>
          </div>
        </div>

        {mode === 'recording' ? (
          <div className="notes-composer__recording" role="status">
            <span className="notes-recording-dot" aria-hidden="true" />
            <Clock size={16} aria-hidden="true" />
            <span className="mono">{formatElapsed(elapsedMs)}</span>
            <span className="muted small">
              {chunkStats.persisted} saved locally · {chunkStats.uploaded} uploaded
              {chunkStats.failed > 0 ? ` · ${chunkStats.failed} retrying` : ''}
            </span>
            <button type="button" className="btn btn--primary" onClick={() => void handleStopRecording()}>
              <Square size={14} aria-hidden="true" />
              Stop &amp; upload
            </button>
          </div>
        ) : mode === 'uploading-file' ? (
          <div className="notes-composer__recording" role="status">
            <Loader2 className="spin" size={16} aria-hidden="true" />
            <span className="muted small">
              Uploading{fileProgress ? ` — part ${fileProgress.done}/${fileProgress.total}` : '…'}
            </span>
          </div>
        ) : (
          <div className="notes-composer__actions">
            <button type="button" className="btn btn--primary" onClick={() => void handleStartRecording()}>
              <Mic size={16} aria-hidden="true" />
              Record
            </button>
            <button type="button" className="btn btn--secondary" onClick={() => fileInputRef.current?.click()}>
              <Upload size={16} aria-hidden="true" />
              Upload file
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept="audio/*"
              className="sr-only"
              aria-label="Choose an audio file to upload"
              onChange={(event) => void handleFilePicked(event)}
            />
          </div>
        )}

        {composerError && (
          <p className="field-error" role="alert">
            {composerError}
          </p>
        )}
      </div>

      {listStatus === 'loading' && <div className="state-block state-block--loading" role="status">
        <Loader2 className="spin" size={20} aria-hidden="true" />
        <span>Loading notes…</span>
      </div>}
      {listStatus === 'error' && (
        <div className="state-block state-block--error" role="alert">
          <AlertCircle size={20} aria-hidden="true" />
          <div className="state-block__body">
            <p>{listError}</p>
            <button type="button" className="btn btn--ghost" onClick={loadNotes}>
              <RefreshCw size={14} aria-hidden="true" />
              Try again
            </button>
          </div>
        </div>
      )}
      {listStatus === 'ready' && notes && notes.length === 0 && (
        <div className="empty-state">
          <div className="empty-state__icon">
            <FileAudio size={28} aria-hidden="true" />
          </div>
          <h3>No notes yet</h3>
          <p>Record a conversation or upload an audio file to get started.</p>
        </div>
      )}

      {listStatus === 'ready' && notes && notes.length > 0 && (
        <ul className="notes-list">
          {notes.map((note) => (
            <NoteRow
              key={note.id}
              note={note}
              expanded={selectedNoteId === note.id}
              onToggle={() => setSelectedNoteId((current) => (current === note.id ? null : note.id))}
              onDeleteRequested={() => setDeleteTarget(note)}
              onChanged={loadNotes}
            />
          ))}
        </ul>
      )}

      {deleteTarget && (
        <div className="dialog-overlay" onMouseDown={(event) => event.target === event.currentTarget && setDeleteTarget(null)}>
          <div className="dialog" role="alertdialog" aria-modal="true" aria-labelledby="delete-note-title">
            <div className="dialog__header">
              <h2 id="delete-note-title">Delete note?</h2>
              <button type="button" className="icon-button" onClick={() => setDeleteTarget(null)} aria-label="Cancel">
                <X size={18} aria-hidden="true" />
              </button>
            </div>
            <div className="dialog__body">
              <p>
                <strong>{deleteTarget.title}</strong> and its audio, transcript, and summary will be permanently
                removed. This cannot be undone.
              </p>
              <div className="dialog__actions">
                <button type="button" className="btn btn--ghost" onClick={() => setDeleteTarget(null)}>
                  Cancel
                </button>
                <button type="button" className="btn btn--primary notes-danger-btn" onClick={() => void handleConfirmDelete()}>
                  <Trash2 size={14} aria-hidden="true" />
                  Delete
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Note list row + detail (transcript / summary / audio player).
// ---------------------------------------------------------------------------

const STATUS_LABELS: Record<Note['status'], string> = {
  uploading: 'Uploading',
  queued: 'Queued for transcription',
  processing: 'Transcribing',
  completed: 'Ready',
  failed: 'Failed',
}

function StatusBadge({ note }: { note: Note }) {
  const label = STATUS_LABELS[note.status]
  const className =
    note.status === 'completed'
      ? 'intent-badge intent-badge--interested'
      : note.status === 'failed'
        ? 'intent-badge intent-badge--not_interested'
        : note.status === 'processing' || note.status === 'queued'
          ? 'intent-badge intent-badge--conditional'
          : 'intent-badge intent-badge--unknown'
  return (
    <span className={className}>
      {(note.status === 'processing' || note.status === 'queued') && (
        <Loader2 className="spin notes-status-spinner" size={12} aria-hidden="true" />
      )}
      {label}
    </span>
  )
}

function NoteRow({
  note: initialNote,
  expanded,
  onToggle,
  onDeleteRequested,
  onChanged,
}: {
  note: Note
  expanded: boolean
  onToggle: () => void
  onDeleteRequested: () => void
  onChanged: () => void
}) {
  const [note, setNote] = useState(initialNote)
  const pollRef = useRef<number | undefined>(undefined)

  useEffect(() => {
    setNote(initialNote)
  }, [initialNote])

  useEffect(() => {
    if (!expanded || !POLLED_STATUSES.has(note.status)) return
    pollRef.current = window.setTimeout(() => {
      getNote(note.id)
        .then(setNote)
        .catch(() => undefined)
    }, POLL_INTERVAL_MS)
    return () => {
      if (pollRef.current !== undefined) window.clearTimeout(pollRef.current)
    }
  }, [expanded, note.status, note.id])

  return (
    <li className="notes-list__item">
      <button
        type="button"
        className="notes-list__header"
        onClick={onToggle}
        aria-expanded={expanded}
        aria-controls={`note-detail-${note.id}`}
      >
        {expanded ? <ChevronUp size={16} aria-hidden="true" /> : <ChevronDown size={16} aria-hidden="true" />}
        <span className="notes-list__title">{note.title}</span>
        <StatusBadge note={note} />
        <span className="muted small notes-list__date">{new Date(note.created_at).toLocaleString()}</span>
      </button>
      {expanded && (
        <div id={`note-detail-${note.id}`}>
          <NoteDetail
            note={note}
            onNoteUpdated={(next) => {
              setNote(next)
              onChanged()
            }}
            onDeleteRequested={onDeleteRequested}
          />
        </div>
      )}
    </li>
  )
}

function NoteDetail({
  note,
  onNoteUpdated,
  onDeleteRequested,
}: {
  note: Note
  onNoteUpdated: (note: Note) => void
  onDeleteRequested: () => void
}) {
  const { push } = useToast()
  const audioRef = useRef<HTMLAudioElement>(null)
  const [titleDraft, setTitleDraft] = useState(note.title)
  const [editingTitle, setEditingTitle] = useState(false)
  const [correctedDraft, setCorrectedDraft] = useState(note.transcript ?? note.transcript_raw ?? '')
  const [savingCorrection, setSavingCorrection] = useState(false)
  const [search, setSearch] = useState('')

  useEffect(() => {
    setTitleDraft(note.title)
  }, [note.title])
  useEffect(() => {
    setCorrectedDraft(note.transcript ?? note.transcript_raw ?? '')
  }, [note.id, note.transcript, note.transcript_raw])

  const filteredSegments = useMemo(() => {
    if (!search.trim()) return note.transcript_segments
    const needle = search.trim().toLowerCase()
    return note.transcript_segments.filter((segment) => segment.text.toLowerCase().includes(needle))
  }, [note.transcript_segments, search])

  function highlight(text: string): ReactNode {
    if (!search.trim()) return text
    const needle = search.trim().toLowerCase()
    const lower = text.toLowerCase()
    const parts: ReactNode[] = []
    let cursor = 0
    let index = lower.indexOf(needle, cursor)
    while (index !== -1) {
      parts.push(text.slice(cursor, index))
      parts.push(<mark key={index}>{text.slice(index, index + needle.length)}</mark>)
      cursor = index + needle.length
      index = lower.indexOf(needle, cursor)
    }
    parts.push(text.slice(cursor))
    return parts
  }

  function seekTo(startSec: number) {
    const audio = audioRef.current
    if (!audio) return
    audio.currentTime = startSec
    void audio.play()
  }

  async function saveTitle() {
    const trimmed = titleDraft.trim()
    if (!trimmed || trimmed === note.title) {
      setEditingTitle(false)
      return
    }
    try {
      const updated = await patchNote(note.id, { title: trimmed })
      onNoteUpdated(updated)
      setEditingTitle(false)
      push('Title updated.', 'success')
    } catch (cause) {
      push(describeApiError(cause, 'Could not rename this note.'), 'error')
    }
  }

  async function saveCorrection() {
    setSavingCorrection(true)
    try {
      const updated = await patchNote(note.id, { transcript_corrected: correctedDraft })
      onNoteUpdated(updated)
      push('Correction saved. Original ASR transcript is preserved.', 'success')
    } catch (cause) {
      push(describeApiError(cause, 'Could not save the correction.'), 'error')
    } finally {
      setSavingCorrection(false)
    }
  }

  const correctionChanged = correctedDraft !== (note.transcript ?? note.transcript_raw ?? '')

  return (
    <div className="notes-detail">
      <div className="notes-detail__row">
        {editingTitle ? (
          <div className="notes-detail__title-edit">
            <input
              value={titleDraft}
              onChange={(event) => setTitleDraft(event.target.value)}
              aria-label="Note title"
              autoFocus
            />
            <button type="button" className="btn btn--secondary" onClick={() => void saveTitle()}>
              <Save size={14} aria-hidden="true" />
              Save
            </button>
            <button type="button" className="btn btn--ghost" onClick={() => setEditingTitle(false)}>
              Cancel
            </button>
          </div>
        ) : (
          <button type="button" className="btn btn--ghost notes-detail__edit-title" onClick={() => setEditingTitle(true)}>
            <Pencil size={14} aria-hidden="true" />
            Rename
          </button>
        )}
        <button type="button" className="btn btn--ghost notes-danger-btn" onClick={onDeleteRequested}>
          <Trash2 size={14} aria-hidden="true" />
          Delete
        </button>
      </div>

      {note.status === 'failed' && note.processing_error && (
        <p className="field-error" role="alert">
          {note.processing_error}
        </p>
      )}

      {(note.status === 'queued' || note.status === 'processing') && (
        <div className="notes-detail__progress" role="status">
          <Loader2 className="spin" size={14} aria-hidden="true" />
          <span>{STATUS_LABELS[note.status]}{note.status === 'processing' ? ` — ${note.progress}%` : ''}</span>
        </div>
      )}

      {note.status !== 'uploading' && (
        <audio ref={audioRef} controls preload="none" src={noteAudioUrl(note.id)} className="notes-detail__audio">
          Your browser does not support the audio element.
        </audio>
      )}

      {note.transcript_segments.length > 0 && (
        <div className="notes-detail__transcript">
          <div className="search-field notes-detail__search">
            <Search size={16} aria-hidden="true" />
            <input
              type="search"
              placeholder="Search this transcript"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              aria-label="Search transcript"
            />
          </div>
          <ol className="notes-segments">
            {filteredSegments.map((segment, index) => (
              <li key={index}>
                <button type="button" className="notes-segments__seek" onClick={() => seekTo(segment.start_sec)}>
                  {formatElapsed(segment.start_sec * 1000)}
                </button>
                <span>{highlight(segment.text)}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {note.transcript_raw && (
        <div className="notes-detail__transcripts">
          <div className="notes-detail__transcript-col">
            <label>Raw transcript (ASR, unedited)</label>
            <div className="notes-detail__raw" aria-readonly="true">
              {note.transcript_raw}
            </div>
          </div>
          <div className="notes-detail__transcript-col">
            <label htmlFor={`corrected-${note.id}`}>Corrected transcript</label>
            <textarea
              id={`corrected-${note.id}`}
              value={correctedDraft}
              onChange={(event) => setCorrectedDraft(event.target.value)}
              rows={6}
            />
            <button
              type="button"
              className="btn btn--secondary"
              disabled={!correctionChanged || savingCorrection}
              onClick={() => void saveCorrection()}
            >
              <Save size={14} aria-hidden="true" />
              {savingCorrection ? 'Saving…' : 'Save correction'}
            </button>
          </div>
        </div>
      )}

      {note.summary_warning && <p className="muted small">{note.summary_warning}</p>}

      {note.summary && (
        <div className="notes-summary">
          <h3>Summary (proposed — review before acting on it)</h3>
          {note.summary.summary && <p>{note.summary.summary}</p>}
          {note.summary.facts.length > 0 && (
            <div>
              <strong className="small">Proposed facts</strong>
              <ul className="notes-summary__list">
                {note.summary.facts.map((fact, index) => (
                  <li key={index}>
                    <span>{fact.text}</span>
                    <blockquote>&ldquo;{fact.quote}&rdquo;</blockquote>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {note.summary.actions.length > 0 && (
            <div>
              <strong className="small">Proposed action items</strong>
              <ul className="notes-summary__list">
                {note.summary.actions.map((action, index) => (
                  <li key={index}>
                    <span>{action.text}</span>
                    <blockquote>&ldquo;{action.quote}&rdquo;</blockquote>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {note.summary.facts.length === 0 && note.summary.actions.length === 0 && (
            <p className="muted small">No facts or action items were proposed for this recording.</p>
          )}
        </div>
      )}

      {note.status === 'completed' && !note.summary && !note.summary_warning && (
        <p className="muted small">
          <CheckCircle2 size={13} aria-hidden="true" /> Transcribed. No summary was generated.
        </p>
      )}
    </div>
  )
}
