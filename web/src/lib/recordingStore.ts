// Notes recording store: durable IndexedDB-backed audio capture + upload
// pipeline for the local voice-notes backend (see backend/permetheus/notes.py
// for the exact server contract this file talks to).
//
// Persist each 3-second capture in IndexedDB until server finalization succeeds.
// File uploads use the same recovery protocol with bounded byte slices.

import { api, ApiError } from './api'

// ---------------------------------------------------------------------------
// Types mirroring backend/permetheus/notes.py exactly.
// ---------------------------------------------------------------------------

export type NoteStatus = 'uploading' | 'queued' | 'processing' | 'completed' | 'failed'

export interface TranscriptSegment {
  start_sec: number
  end_sec: number
  text: string
}

export interface SummaryFact {
  text: string
  quote: string
  status: string
}

export interface NoteSummary {
  summary: string
  facts: SummaryFact[]
  actions: SummaryFact[]
}

export interface NoteChunkInfo {
  sequence: number
  sha256: string
  bytes: number
}

export interface Note {
  id: string
  title: string
  company_id: string | null
  status: NoteStatus
  job_id?: string | null
  mime_type: string | null
  chunk_count: number | null
  received_sequences: number[] | null
  audio_sha256: string | null
  duration_seconds: number | null
  transcript_raw: string | null
  /** Effective transcript: `transcript_corrected` if set, else `transcript_raw`. */
  transcript: string | null
  transcript_segments: TranscriptSegment[]
  summary: NoteSummary | null
  progress: number
  processing_error: string | null
  summary_warning: string | null
  processing_error_seen?: boolean
  created_at: string
  updated_at: string
  chunks?: NoteChunkInfo[]
}

interface ChunkUploadAck {
  sequence: number
  sha256: string
  idempotent: boolean
}

// ---------------------------------------------------------------------------
// Constants shared with the backend contract.
// ---------------------------------------------------------------------------

export const MAX_CHUNK_BYTES = 8 * 1024 * 1024
/** Safety margin under the server's 8 MiB hard cap. */
export const UPLOAD_SLICE_BYTES = 7 * 1024 * 1024
export const MAX_DURATION_MS = 4 * 60 * 60 * 1000
const MAX_DURATION_SECONDS = MAX_DURATION_MS / 1000
/** Three-second uploads across the four-hour recording limit. */
export const MAX_SEQUENCES = Math.floor(MAX_DURATION_SECONDS / 3) + 2

const SUB_CHUNK_MS = 3000
/** ~30s of audio per uploaded chunk, matching the backend's chunk budget. */
const SUB_CHUNKS_PER_GROUP = 1

/** Base mime types the backend accepts (see MIME_EXTENSIONS in notes.py). */
export const SUPPORTED_MIME_TYPES = [
  'audio/webm',
  'audio/ogg',
  'audio/wav',
  'audio/x-wav',
  'audio/mpeg',
  'audio/mp4',
  'audio/aac',
  'audio/flac',
] as const

const EXTENSION_MIME_HINTS: Record<string, string> = {
  webm: 'audio/webm',
  ogg: 'audio/ogg',
  oga: 'audio/ogg',
  wav: 'audio/wav',
  mp3: 'audio/mpeg',
  m4a: 'audio/mp4',
  mp4: 'audio/mp4',
  aac: 'audio/aac',
  flac: 'audio/flac',
}

/** Candidate MediaRecorder mime types in preference order. */
const MIC_MIME_CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/ogg',
  'audio/mp4',
]

// ---------------------------------------------------------------------------
// Pure helpers (no DOM/IndexedDB access — easy to reason about / unit test).
// ---------------------------------------------------------------------------

/** Strips codec parameters, e.g. "audio/webm;codecs=opus" -> "audio/webm". */
export function baseMimeType(mimeType: string): string {
  return mimeType.split(';', 1)[0].trim().toLowerCase()
}

export function isSupportedMimeType(mimeType: string): boolean {
  return (SUPPORTED_MIME_TYPES as readonly string[]).includes(baseMimeType(mimeType))
}

/** Best-effort mime type guess for files whose `File.type` is empty. */
export function guessMimeTypeFromFilename(filename: string): string | null {
  const dot = filename.lastIndexOf('.')
  if (dot < 0) return null
  const ext = filename.slice(dot + 1).toLowerCase()
  return EXTENSION_MIME_HINTS[ext] ?? null
}

/** Picks the first MediaRecorder-supported candidate, or null if none work. */
export function pickMicMimeType(
  isTypeSupported: (candidate: string) => boolean = (t) =>
    typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(t),
): string | null {
  for (const candidate of MIC_MIME_CANDIDATES) {
    if (isTypeSupported(candidate)) return candidate
  }
  return null
}

/** Formats elapsed milliseconds as "mm:ss" or "h:mm:ss" for real, measured time. */
export function formatElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${pad(minutes)}:${pad(seconds)}`
}

/** Splits [0, chunkCount) into the sequences not present in `uploaded`. */
export function computeMissingSequences(chunkCount: number, uploaded: ReadonlySet<number>): number[] {
  const missing: number[] = []
  for (let sequence = 0; sequence < chunkCount; sequence += 1) {
    if (!uploaded.has(sequence)) missing.push(sequence)
  }
  return missing
}

export interface MicErrorInfo {
  title: string
  message: string
}

/** Turns getUserMedia/MediaRecorder failures into actionable, human copy. */
export function describeMicError(error: unknown): MicErrorInfo {
  const name = error instanceof DOMException ? error.name : ''
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return {
        title: 'Microphone access denied',
        message:
          'This browser blocked microphone access. Allow the microphone permission for this site in your browser settings, then try again.',
      }
    case 'NotFoundError':
    case 'OverconstrainedError':
      return {
        title: 'No microphone found',
        message: 'No microphone was found on this device. Connect a microphone and try again.',
      }
    case 'NotReadableError':
      return {
        title: 'Microphone unavailable',
        message: 'The microphone is already in use by another application or tab. Close it and try again.',
      }
    case 'AbortError':
      return {
        title: 'Recording interrupted',
        message: 'The recording was interrupted by the browser or operating system.',
      }
    default:
      return {
        title: 'Microphone unavailable',
        message:
          error instanceof Error && error.message
            ? error.message
            : 'Could not access the microphone in this browser.',
      }
  }
}

/** Turns API failures into human copy, distinguishing permission/network errors. */
export function describeApiError(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    if (error.status === 0) return 'Could not reach the server. Check your connection and try again.'
    if (error.status === 401 || error.status === 403) {
      return 'Your session needs to be refreshed. Reload the page and try again.'
    }
    return error.message || fallback
  }
  return fallback
}

// ---------------------------------------------------------------------------
// Thin API wrappers over the shared fetch client.
// ---------------------------------------------------------------------------

export async function createNote(title: string, companyId: string | null): Promise<Note> {
  return api.post<Note>('/api/notes', companyId ? { title, company_id: companyId } : { title })
}

export async function listNotes(limit = 50): Promise<Note[]> {
  return api.get<Note[]>(`/api/notes?limit=${limit}`)
}

export async function getNote(noteId: string): Promise<Note> {
  return api.get<Note>(`/api/notes/${noteId}`)
}

export async function uploadNoteChunk(noteId: string, sequence: number, blob: Blob): Promise<ChunkUploadAck> {
  return api.upload<ChunkUploadAck>(`/api/notes/${noteId}/chunks/${sequence}`, blob, 'PUT')
}

export async function finalizeNote(noteId: string, chunkCount: number, mimeType: string): Promise<Note> {
  return api.post<Note>(`/api/notes/${noteId}/finalize`, {
    chunk_count: chunkCount,
    mime_type: baseMimeType(mimeType),
  })
}

export async function patchNote(
  noteId: string,
  patch: { title?: string; transcript_corrected?: string },
): Promise<Note> {
  return api.patch<Note>(`/api/notes/${noteId}`, patch)
}

export async function deleteNote(noteId: string): Promise<void> {
  await api.delete<void>(`/api/notes/${noteId}`)
}

export function noteAudioUrl(noteId: string): string {
  return `/api/notes/${noteId}/audio`
}

// ---------------------------------------------------------------------------
// IndexedDB durable storage: captured chunks + recovery session metadata.
// ---------------------------------------------------------------------------

const DB_NAME = 'permetheus-notes'
const DB_VERSION = 1
const STORE_CHUNKS = 'chunks'
const STORE_SESSIONS = 'sessions'

export interface StoredChunk {
  noteId: string
  sequence: number
  blob: Blob
  mimeType: string
  byteSize: number
  uploaded: boolean
}

export type SessionKind = 'microphone' | 'file'
export type SessionStatus = 'active' | 'interrupted' | 'finalizing' | 'error'

export interface RecordingSessionRecord {
  noteId: string
  kind: SessionKind
  title: string
  companyId: string | null
  mimeType: string
  /** Next upload sequence to assign. Also the count of chunks produced so far. */
  nextSequence: number
  /** Known only for file uploads; null while a microphone recording is open-ended. */
  expectedChunkCount: number | null
  status: SessionStatus
  startedAt: number
  updatedAt: number
  lastError: string | null
}

let dbPromise: Promise<IDBDatabase> | null = null

function openDb(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise
  dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION)
    request.onupgradeneeded = () => {
      const db = request.result
      if (!db.objectStoreNames.contains(STORE_CHUNKS)) {
        const store = db.createObjectStore(STORE_CHUNKS, { keyPath: ['noteId', 'sequence'] })
        store.createIndex('byNote', 'noteId', { unique: false })
      }
      if (!db.objectStoreNames.contains(STORE_SESSIONS)) {
        db.createObjectStore(STORE_SESSIONS, { keyPath: 'noteId' })
      }
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error ?? new Error('Could not open local recording storage'))
  })
  return dbPromise
}

function runTx<T>(
  storeName: string,
  mode: IDBTransactionMode,
  run: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  return openDb().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const tx = db.transaction(storeName, mode)
        const store = tx.objectStore(storeName)
        const request = run(store)
        request.onerror = () => reject(request.error ?? new Error('Local storage request failed'))
        tx.onerror = () => reject(tx.error ?? new Error('Local storage transaction failed'))
        tx.oncomplete = () => resolve(request.result)
      }),
  )
}

export async function putChunk(chunk: StoredChunk): Promise<void> {
  await runTx(STORE_CHUNKS, 'readwrite', (store) => store.put(chunk))
}

export async function markChunkUploaded(noteId: string, sequence: number): Promise<void> {
  const db = await openDb()
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(STORE_CHUNKS, 'readwrite')
    const store = tx.objectStore(STORE_CHUNKS)
    const getRequest = store.get([noteId, sequence])
    getRequest.onsuccess = () => {
      const existing = getRequest.result as StoredChunk | undefined
      if (existing) store.put({ ...existing, uploaded: true })
    }
    tx.onerror = () => reject(tx.error ?? new Error('Local storage transaction failed'))
    tx.oncomplete = () => resolve()
  })
}

export async function listChunksForNote(noteId: string): Promise<StoredChunk[]> {
  const db = await openDb()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_CHUNKS, 'readonly')
    const index = tx.objectStore(STORE_CHUNKS).index('byNote')
    const results: StoredChunk[] = []
    const request = index.openCursor(IDBKeyRange.only(noteId))
    request.onsuccess = () => {
      const cursor = request.result
      if (cursor) {
        results.push(cursor.value as StoredChunk)
        cursor.continue()
      }
    }
    request.onerror = () => reject(request.error ?? new Error('Local storage request failed'))
    tx.oncomplete = () => resolve(results.sort((a, b) => a.sequence - b.sequence))
  })
}

export async function deleteChunksForNote(noteId: string): Promise<void> {
  const db = await openDb()
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(STORE_CHUNKS, 'readwrite')
    const store = tx.objectStore(STORE_CHUNKS)
    const index = store.index('byNote')
    const request = index.openCursor(IDBKeyRange.only(noteId))
    request.onsuccess = () => {
      const cursor = request.result
      if (cursor) {
        store.delete(cursor.primaryKey)
        cursor.continue()
      }
    }
    request.onerror = () => reject(request.error ?? new Error('Local storage request failed'))
    tx.oncomplete = () => resolve()
  })
}

export async function putSession(session: RecordingSessionRecord): Promise<void> {
  await runTx(STORE_SESSIONS, 'readwrite', (store) => store.put(session))
}

export async function getSession(noteId: string): Promise<RecordingSessionRecord | undefined> {
  return runTx(STORE_SESSIONS, 'readonly', (store) => store.get(noteId))
}

export async function deleteSession(noteId: string): Promise<void> {
  await runTx(STORE_SESSIONS, 'readwrite', (store) => store.delete(noteId))
}

export async function listSessions(): Promise<RecordingSessionRecord[]> {
  const db = await openDb()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_SESSIONS, 'readonly')
    const store = tx.objectStore(STORE_SESSIONS)
    const results: RecordingSessionRecord[] = []
    const request = store.openCursor()
    request.onsuccess = () => {
      const cursor = request.result
      if (cursor) {
        results.push(cursor.value as RecordingSessionRecord)
        cursor.continue()
      }
    }
    request.onerror = () => reject(request.error ?? new Error('Local storage request failed'))
    tx.oncomplete = () => resolve(results.sort((a, b) => b.updatedAt - a.updatedAt))
  })
}

// ---------------------------------------------------------------------------
// Upload primitive shared by microphone recording and file upload.
// ---------------------------------------------------------------------------

const RETRY_DELAYS_MS = [500, 1500, 4000]

async function uploadChunkWithRetry(noteId: string, sequence: number, blob: Blob): Promise<string | null> {
  let lastError: unknown = null
  for (let attempt = 0; attempt <= RETRY_DELAYS_MS.length; attempt += 1) {
    try {
      const ack = await uploadNoteChunk(noteId, sequence, blob)
      await markChunkUploaded(noteId, sequence)
      return ack.sha256
    } catch (error) {
      lastError = error
      if (attempt < RETRY_DELAYS_MS.length) {
        await new Promise((resolve) => window.setTimeout(resolve, RETRY_DELAYS_MS[attempt]))
      }
    }
  }
  throw lastError instanceof Error ? lastError : new Error('Chunk upload failed')
}

/**
 * Uploads whichever sequences in [0, chunkCount) are missing from
 * `uploaded`, serially, using each sequence's blob from IndexedDB.
 * Returns the sequences that still failed after retries.
 */
export async function uploadMissingSequences(
  noteId: string,
  chunkCount: number,
  onProgress?: (sequence: number) => void,
): Promise<number[]> {
  const stored = await listChunksForNote(noteId)
  const bySequence = new Map(stored.map((chunk) => [chunk.sequence, chunk]))
  const failed: number[] = []
  for (let sequence = 0; sequence < chunkCount; sequence += 1) {
    const chunk = bySequence.get(sequence)
    if (!chunk) {
      failed.push(sequence)
      continue
    }
    if (chunk.uploaded) {
      onProgress?.(sequence)
      continue
    }
    try {
      await uploadChunkWithRetry(noteId, sequence, chunk.blob)
      onProgress?.(sequence)
    } catch {
      failed.push(sequence)
    }
  }
  return failed
}

// ---------------------------------------------------------------------------
// Microphone recording controller.
// ---------------------------------------------------------------------------

export type RecordingStopReason = 'user' | 'unmount' | 'max-duration' | 'error'

export interface MicRecordingCallbacks {
  onElapsedChange?: (ms: number) => void
  onChunkPersisted?: (sequence: number) => void
  onChunkUploaded?: (sequence: number) => void
  onUploadError?: (sequence: number, message: string) => void
  onStopped?: (reason: RecordingStopReason) => void
}

export interface StopResult {
  chunkCount: number
  failedSequences: number[]
}

export interface MicRecordingController {
  noteId: string
  mimeType: string
  /** Gracefully finishes capture, uploads, and returns whether finalize is safe. */
  stop: () => Promise<StopResult>
  /**
   * Best-effort synchronous-ish teardown for component unmount: stops the
   * recorder, releases the microphone immediately, and persists whatever
   * was captured so it can be recovered later. Does not finalize.
   */
  abortForUnmount: () => void
  getElapsedMs: () => number
}

/**
 * Starts microphone capture for an already-created note. `stream` must come
 * from a user gesture (e.g. a click handler calling getUserMedia).
 */
export function startMicRecording(
  stream: MediaStream,
  note: { id: string; title: string; companyId: string | null },
  mimeType: string,
  callbacks: MicRecordingCallbacks = {},
): MicRecordingController {
  const noteId = note.id
  const recorder = new MediaRecorder(stream, { mimeType })
  const startedAt = Date.now()

  let subBuffer: Blob[] = []
  let subBufferBytes = 0
  let nextSequence = 0
  let uploadChain: Promise<void> = Promise.resolve()
  const failedSequences = new Set<number>()
  let stopping = false
  let stopped = false
  let elapsedTimer: number | undefined
  let maxDurationTimer: number | undefined
  let resolveStopEvent: (() => void) | null = null
  const stopEventPromise = new Promise<void>((resolve) => {
    resolveStopEvent = resolve
  })

  function releaseTracks() {
    stream.getTracks().forEach((track) => track.stop())
  }

  async function persistSession(status: SessionStatus) {
    await putSession({
      noteId,
      kind: 'microphone',
      title: note.title,
      companyId: note.companyId,
      mimeType,
      nextSequence,
      expectedChunkCount: null,
      status,
      startedAt,
      updatedAt: Date.now(),
      lastError: null,
    })
  }

  function enqueueGroup(blob: Blob) {
    const sequence = nextSequence
    nextSequence += 1
    uploadChain = uploadChain.then(async () => {
      await putChunk({ noteId, sequence, blob, mimeType, byteSize: blob.size, uploaded: false })
      callbacks.onChunkPersisted?.(sequence)
      await persistSession(stopping ? 'interrupted' : 'active')
      try {
        await uploadChunkWithRetry(noteId, sequence, blob)
        failedSequences.delete(sequence)
        callbacks.onChunkUploaded?.(sequence)
      } catch (error) {
        failedSequences.add(sequence)
        callbacks.onUploadError?.(
          sequence,
          error instanceof Error ? error.message : 'Could not upload this chunk yet.',
        )
      }
    })
  }

  function flushGroupIfNeeded(force: boolean) {
    if (subBuffer.length === 0) return
    const groupIsFull = subBuffer.length >= SUB_CHUNKS_PER_GROUP || subBufferBytes >= UPLOAD_SLICE_BYTES
    if (!force && !groupIsFull) return
    const blob = new Blob(subBuffer, { type: mimeType })
    subBuffer = []
    subBufferBytes = 0
    enqueueGroup(blob)
  }

  recorder.addEventListener('dataavailable', (event: BlobEvent) => {
    if (event.data.size === 0) return
    subBuffer.push(event.data)
    subBufferBytes += event.data.size
    flushGroupIfNeeded(false)
  })

  recorder.addEventListener('stop', () => {
    flushGroupIfNeeded(true)
    resolveStopEvent?.()
  })

  recorder.addEventListener('error', () => {
    // MediaRecorder failed (e.g. the device disappeared). Release the
    // microphone and preserve whatever was already captured; do not try to
    // keep recording or finalize automatically.
    teardown('error')
  })

  recorder.start(SUB_CHUNK_MS)
  void persistSession('active')

  elapsedTimer = window.setInterval(() => {
    callbacks.onElapsedChange?.(Date.now() - startedAt)
  }, 1000)

  maxDurationTimer = window.setTimeout(() => {
    void stopInternal('max-duration')
  }, MAX_DURATION_MS)

  function clearTimers() {
    if (elapsedTimer !== undefined) window.clearInterval(elapsedTimer)
    if (maxDurationTimer !== undefined) window.clearTimeout(maxDurationTimer)
    elapsedTimer = undefined
    maxDurationTimer = undefined
  }

  async function stopInternal(reason: RecordingStopReason): Promise<StopResult> {
    if (stopped) return { chunkCount: nextSequence, failedSequences: [...failedSequences] }
    stopping = true
    clearTimers()
    if (recorder.state !== 'inactive') recorder.stop()
    await stopEventPromise
    await uploadChain
    stopped = true
    releaseTracks()
    await persistSession(failedSequences.size > 0 ? 'interrupted' : 'finalizing')
    callbacks.onStopped?.(reason)
    return { chunkCount: nextSequence, failedSequences: [...failedSequences] }
  }

  /**
   * Synchronous best-effort teardown used for unmount and recorder errors:
   * stop trying to record, release the microphone immediately, and persist
   * an "interrupted" session for later recovery. Never finalizes.
   */
  function teardown(reason: 'unmount' | 'error') {
    if (stopped) return
    stopping = true
    stopped = true
    clearTimers()
    if (recorder.state !== 'inactive') {
      try {
        recorder.stop()
      } catch {
        // Recorder may already be inactive; releasing tracks below is what matters.
      }
    }
    releaseTracks()
    void persistSession('interrupted')
    callbacks.onStopped?.(reason)
  }

  return {
    noteId,
    mimeType,
    stop: () => stopInternal('user'),
    abortForUnmount: () => teardown('unmount'),
    getElapsedMs: () => Date.now() - startedAt,
  }
}

// ---------------------------------------------------------------------------
// File upload controller (pre-recorded audio, sliced at a byte boundary).
// ---------------------------------------------------------------------------

export interface FileUploadCallbacks {
  onChunkUploaded?: (sequence: number, chunkCount: number) => void
  onUploadError?: (sequence: number, message: string) => void
}

export interface FileUploadResult {
  chunkCount: number
  failedSequences: number[]
}

/**
 * Slices `file` into <=8 MiB pieces, persists each to IndexedDB immediately,
 * and uploads them serially using the same chunk protocol as microphone
 * recording. Never holds the whole file in memory — only one slice at a
 * time is read via `File.slice`, which is a zero-copy view until read.
 */
export async function uploadAudioFile(
  noteId: string,
  file: File,
  mimeType: string,
  callbacks: FileUploadCallbacks = {},
): Promise<FileUploadResult> {
  const chunkCount = Math.max(1, Math.ceil(file.size / UPLOAD_SLICE_BYTES))
  await putSession({
    noteId,
    kind: 'file',
    title: file.name,
    companyId: null,
    mimeType,
    nextSequence: 0,
    expectedChunkCount: chunkCount,
    status: 'active',
    startedAt: Date.now(),
    updatedAt: Date.now(),
    lastError: null,
  })

  const failedSequences: number[] = []
  for (let sequence = 0; sequence < chunkCount; sequence += 1) {
    const start = sequence * UPLOAD_SLICE_BYTES
    const end = Math.min(file.size, start + UPLOAD_SLICE_BYTES)
    const blob = file.slice(start, end, mimeType)
    await putChunk({ noteId, sequence, blob, mimeType, byteSize: blob.size, uploaded: false })
    try {
      await uploadChunkWithRetry(noteId, sequence, blob)
      callbacks.onChunkUploaded?.(sequence, chunkCount)
    } catch (error) {
      failedSequences.push(sequence)
      callbacks.onUploadError?.(sequence, error instanceof Error ? error.message : 'Chunk upload failed')
    }
    await putSession({
      noteId,
      kind: 'file',
      title: file.name,
      companyId: null,
      mimeType,
      nextSequence: sequence + 1,
      expectedChunkCount: chunkCount,
      status: failedSequences.length > 0 ? 'interrupted' : 'active',
      startedAt: Date.now(),
      updatedAt: Date.now(),
      lastError: null,
    })
  }
  return { chunkCount, failedSequences }
}
