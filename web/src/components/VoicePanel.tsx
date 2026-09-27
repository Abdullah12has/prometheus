// Voice agents: create a conversation agent, capture or upload a voice sample, clone it, and run
// a local browser conversation. Sample capture (record/upload) lives inside the create/replace
// flow — there is no separate hidden authorization form. Consent is a short in-product notice plus
// the explicit action of recording/uploading and saving; the backend contract mirrors that (see
// backend/permetheus/voice.py, `consent_action=create_own_voice`). Nothing here fabricates a
// checked consent value.

import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import { CheckCircle2, Loader2, Mic, RotateCcw, ShieldCheck, Square, Trash2, Upload } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { Company, CompanyListResponse } from '../lib/types'
import { useToast } from '../lib/toast'
import { CompanyPicker } from './CompanyPicker'
import { describeMicError, formatElapsed, pickMicMimeType } from '../lib/recordingStore'
import { VoiceAudio, type VoiceAudioEvent } from '../lib/voice-audio'
import './voice.css'

type Agent = {
  id: string
  name: string
  introduction: string
  instructions: string
  max_duration_seconds: number
  updated_at: string
  voice: {
    kind: string
    label: string
    authorization: { voice_owner_name: string | null; basis: string; authorized_at?: string } | null
  }
}
type Capabilities = { browser: { available: boolean; label: string; speech_runtime: boolean; language_model: boolean }; phone: { available: false; label: string; reason: string } }
type TranscriptLine = { role: 'user' | 'agent'; text: string }
type SavedSession = { id: string; agent_id: string; company_id: string | null; status: string; created_at: string; ended_at: string | null; end_reason: string | null; transcript?: TranscriptLine[]; proposed_outcome?: { summary?: string; stated_interest?: string; follow_ups?: string[]; note?: string } | null }
type ServerEvent = { type: string; session_id?: string; websocket_path?: string; channel_label?: string; utterance_id?: string; seq?: number; sample_rate?: number; pcm?: string; text?: string; committed?: string; tentative?: string; turn_id?: string | null; reason?: string; message?: string; fatal?: boolean }
type SampleTab = 'record' | 'upload'
type ClonePhase = 'idle' | 'preparing' | 'cloning' | 'ready' | 'error'
type VoiceSource = 'custom' | 'bundled'

const voiceError = (cause: unknown) => cause instanceof ApiError ? `${cause.message}${cause.detail ? ` (${JSON.stringify(cause.detail)})` : ''}` : cause instanceof Error ? cause.message : 'The request could not be completed.'
const micErrorMessage = (cause: unknown) => { const info = describeMicError(cause); return `${info.title}: ${info.message}` }

const DEFAULT_INTRODUCTION = 'Hello, I am an AI assistant calling from the team. Is now a good time to talk?'
const MIN_SAMPLE_SECONDS = 3
const MAX_SAMPLE_SECONDS = 30
const SAMPLE_SCRIPT = 'The quick brown fox jumps over the lazy dog while autumn leaves drift across the quiet garden path, and a distant train whistle echoes through the valley.'

/** Reads duration client-side (best effort) so upload feedback is as fast as recording feedback. */
function probeDuration(file: File): Promise<number | null> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file)
    const audio = new Audio()
    const timer = window.setTimeout(() => done(null), 5000)
    const done = (value: number | null) => { window.clearTimeout(timer); audio.onloadedmetadata = null; audio.onerror = null; URL.revokeObjectURL(url); resolve(value) }
    audio.preload = 'metadata'
    audio.onloadedmetadata = () => done(Number.isFinite(audio.duration) ? audio.duration : null)
    audio.onerror = () => done(null)
    audio.src = url
  })
}

function VoiceSampleCapture({ tab, onTab, sample, sampleUrl, sampleSeconds, recording, elapsedMs, micError, onStartRecording, onStopRecording, onFile, onClear, disabled }: {
  tab: SampleTab
  onTab: (tab: SampleTab) => void
  sample: File | null
  sampleUrl: string | null
  sampleSeconds: number
  recording: boolean
  elapsedMs: number
  micError: string | null
  onStartRecording: () => void
  onStopRecording: () => void
  onFile: (event: ChangeEvent<HTMLInputElement>) => void
  onClear: () => void
  disabled: boolean
}) {
  const fileInputRef = useRef<HTMLInputElement>(null)
  return <div className="voice-capture">
    <div className="voice-tabs" role="tablist" aria-label="Sample source">
      <button type="button" role="tab" aria-selected={tab === 'record'} className={tab === 'record' ? 'is-active' : ''} onClick={() => onTab('record')} disabled={disabled || recording}>Record</button>
      <button type="button" role="tab" aria-selected={tab === 'upload'} className={tab === 'upload' ? 'is-active' : ''} onClick={() => onTab('upload')} disabled={disabled || recording}>Upload file</button>
    </div>
    <p className="voice-script"><strong>Read this aloud{tab === 'record' ? ' while recording' : ''}:</strong> &ldquo;{SAMPLE_SCRIPT}&rdquo;</p>
    {tab === 'record'
      ? recording
        ? <div className="voice-record__active" role="status">
            <span className="voice-record__dot" aria-hidden="true" />
            <Mic size={16} aria-hidden="true" />
            <span className="mono">{formatElapsed(elapsedMs)}</span>
            <span className="muted small">of up to {MAX_SAMPLE_SECONDS}s</span>
            <button type="button" className="btn btn--secondary" onClick={onStopRecording}><Square size={14} aria-hidden="true" />Stop</button>
          </div>
        : <button type="button" className="btn btn--secondary" onClick={onStartRecording} disabled={disabled || recording}><Mic size={16} aria-hidden="true" />Start recording</button>
      : <div className="voice-upload">
          <button type="button" className="btn btn--secondary" onClick={() => fileInputRef.current?.click()} disabled={disabled || recording}><Upload size={16} aria-hidden="true" />Choose an audio file</button>
          <input ref={fileInputRef} type="file" accept="audio/*" className="sr-only" aria-label="Choose a voice sample audio file" onChange={onFile} />
        </div>}
    {micError && <p className="field-error" role="alert">{micError}</p>}
    {sampleUrl && <div className="voice-sample-preview">
      <audio controls preload="metadata" src={sampleUrl} aria-label="Recorded sample playback" />
      <span className="muted small">{sample?.name ?? 'Recorded sample'}{sampleSeconds > 0 ? ` · ${sampleSeconds.toFixed(1)}s` : ''}</span>
      <button type="button" className="btn btn--ghost" onClick={onClear} disabled={disabled || recording}><Trash2 size={14} aria-hidden="true" />Remove</button>
    </div>}
  </div>
}

export function VoicePanel() {
  const { push } = useToast()
  const [agents, setAgents] = useState<Agent[]>([])
  const [companies, setCompanies] = useState<Company[]>([])
  const [companyId, setCompanyId] = useState('')
  const [sessions, setSessions] = useState<SavedSession[]>([])
  const [expandedSession, setExpandedSession] = useState<SavedSession | null>(null)
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [selected, setSelected] = useState('')
  const [editing, setEditing] = useState(false)
  const [creating, setCreating] = useState(false)
  const [agentName, setAgentName] = useState('')
  const [introduction, setIntroduction] = useState(DEFAULT_INTRODUCTION)
  const [instructions, setInstructions] = useState('')
  const [duration, setDuration] = useState(300)

  // Voice sample capture, shared by "new agent" creation and "replace sample" on an existing one.
  const [voiceSource, setVoiceSource] = useState<VoiceSource>('custom')
  const [sampleTab, setSampleTab] = useState<SampleTab>('record')
  const [sample, setSample] = useState<File | null>(null)
  const [sampleUrl, setSampleUrl] = useState<string | null>(null)
  const [sampleSeconds, setSampleSeconds] = useState(0)
  const [recording, setRecording] = useState(false)
  const [recordElapsedMs, setRecordElapsedMs] = useState(0)
  const [micError, setMicError] = useState<string | null>(null)
  const [clonePhase, setClonePhase] = useState<ClonePhase>('idle')
  const [pendingAgentId, setPendingAgentId] = useState<string | null>(null)
  const [replacingSample, setReplacingSample] = useState(false)

  // Off by default: no consent is assumed. Turning this on before starting a personal browser
  // test is what makes the transcript get saved for review.
  const [saveTranscript, setSaveTranscript] = useState(false)
  const [callState, setCallState] = useState<'idle' | 'connecting' | 'live'>('idle')
  const [partial, setPartial] = useState({ committed: '', tentative: '' })
  const [transcript, setTranscript] = useState<TranscriptLine[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const socketRef = useRef<WebSocket | null>(null)
  const audioRef = useRef<VoiceAudio | null>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const sampleStreamRef = useRef<MediaStream | null>(null)
  const sampleChunksRef = useRef<Blob[]>([])
  const recordingStartedRef = useRef(0)
  const elapsedTimerRef = useRef<number | undefined>(undefined)
  const autoStopTimerRef = useRef<number | undefined>(undefined)
  const sampleUrlRef = useRef<string | null>(null)
  const disposedRef = useRef(false)
  const captureRequestRef = useRef(0)

  useEffect(() => { sampleUrlRef.current = sampleUrl }, [sampleUrl])

  const refresh = useCallback(async () => {
    try {
      const [caps, data] = await Promise.all([api.get<Capabilities>('/api/voice/capabilities'), api.get<Agent[]>('/api/voice/agents')])
      setCapabilities(caps); setAgents(data); setSelected((current) => current || data[0]?.id || '')
      const [companyResult, historyResult] = await Promise.allSettled([
        api.get<CompanyListResponse | Company[]>('/api/companies'), api.get<SavedSession[]>('/api/voice/sessions'),
      ])
      if (companyResult.status === 'fulfilled') setCompanies(Array.isArray(companyResult.value) ? companyResult.value : companyResult.value.items)
      if (historyResult.status === 'fulfilled') setSessions(historyResult.value)
    } catch (cause) { setError(voiceError(cause)) }
  }, [])
  useEffect(() => { void refresh() }, [refresh])
  const agent = agents.find((item) => item.id === selected)
  const rememberCompanies = useCallback((items: Company[]) => {
    setCompanies((current) => {
      const added = items.filter((item) => !current.some((existing) => existing.id === item.id))
      return added.length ? [...current, ...added] : current
    })
  }, [])

  useEffect(() => {
    disposedRef.current = false
    return () => {
      disposedRef.current = true
      if (socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: 'stop' }))
      socketRef.current?.close()
      void audioRef.current?.stop()
      captureRequestRef.current++
      if (mediaRecorderRef.current) { mediaRecorderRef.current.onstop = null; if (mediaRecorderRef.current.state === 'recording') mediaRecorderRef.current.stop() }
      sampleStreamRef.current?.getTracks().forEach((track) => track.stop())
      if (elapsedTimerRef.current !== undefined) window.clearInterval(elapsedTimerRef.current)
      if (autoStopTimerRef.current !== undefined) window.clearTimeout(autoStopTimerRef.current)
      if (sampleUrlRef.current) URL.revokeObjectURL(sampleUrlRef.current)
    }
  }, [])

  function resetSampleState() {
    setSample(null)
    setSampleUrl((prev) => { if (prev) URL.revokeObjectURL(prev); return null })
    setSampleSeconds(0)
    setMicError(null)
  }

  function applySample(file: File, seconds: number) {
    setSample(file)
    setSampleUrl((prev) => { if (prev) URL.revokeObjectURL(prev); return URL.createObjectURL(file) })
    setSampleSeconds(seconds)
    setMicError(null)
  }

  function clearSample() { resetSampleState() }

  function teardownSampleRecording() {
    sampleStreamRef.current?.getTracks().forEach((track) => track.stop())
    sampleStreamRef.current = null
    if (elapsedTimerRef.current !== undefined) window.clearInterval(elapsedTimerRef.current)
    if (autoStopTimerRef.current !== undefined) window.clearTimeout(autoStopTimerRef.current)
    elapsedTimerRef.current = undefined; autoStopTimerRef.current = undefined
    setRecording(false)
  }

  async function startSampleRecording() {
    setMicError(null)
    const request = ++captureRequestRef.current
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (disposedRef.current || request !== captureRequestRef.current) { stream.getTracks().forEach((track) => track.stop()); return }
      sampleStreamRef.current = stream
      const mimeType = pickMicMimeType()
      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream)
      sampleChunksRef.current = []
      recordingStartedRef.current = Date.now()
      recorder.ondataavailable = (event) => { if (event.data.size) sampleChunksRef.current.push(event.data) }
      recorder.onerror = () => { recorder.onstop = null; setMicError('The microphone stopped unexpectedly. Try recording again.'); teardownSampleRecording() }
      recorder.onstop = () => {
        const seconds = (Date.now() - recordingStartedRef.current) / 1000
        teardownSampleRecording()
        if (seconds < MIN_SAMPLE_SECONDS) { setMicError(`That recording was only ${seconds.toFixed(1)}s. Record at least ${MIN_SAMPLE_SECONDS} seconds.`); return }
        const ext = recorder.mimeType.includes('ogg') ? 'ogg' : recorder.mimeType.includes('mp4') ? 'm4a' : 'webm'
        applySample(new File(sampleChunksRef.current, `voice-sample.${ext}`, { type: recorder.mimeType }), seconds)
      }
      recorder.start(); mediaRecorderRef.current = recorder
      setRecording(true); setRecordElapsedMs(0)
      elapsedTimerRef.current = window.setInterval(() => setRecordElapsedMs(Date.now() - recordingStartedRef.current), 200)
      // Automatic, visible stop at the sample cap: the timer above shows this happening, so
      // reaching the limit is never a silent rejection.
      autoStopTimerRef.current = window.setTimeout(() => { if (recorder.state === 'recording') recorder.stop() }, MAX_SAMPLE_SECONDS * 1000 - 500)
    } catch (cause) {
      if (request !== captureRequestRef.current || disposedRef.current) return
      teardownSampleRecording()
      setMicError(micErrorMessage(cause))
    }
  }

  function stopSampleRecording() {
    if (mediaRecorderRef.current?.state === 'recording') mediaRecorderRef.current.stop()
  }

  async function handleSampleFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0] ?? null
    event.target.value = ''
    if (!file) return
    setMicError(null)
    if (file.size > 10 * 1024 * 1024) { setMicError("Choose an audio sample smaller than 10 MB."); return }
    const seconds = await probeDuration(file)
    if (seconds != null && (seconds < MIN_SAMPLE_SECONDS || seconds > MAX_SAMPLE_SECONDS)) {
      setMicError(`This file is ${seconds.toFixed(1)}s. Voice samples must be between ${MIN_SAMPLE_SECONDS} and ${MAX_SAMPLE_SECONDS} seconds.`)
      return
    }
    applySample(file, seconds ?? 0)
  }

  /** Uploads the captured/uploaded sample and clones it. Never clears `sample` on failure, so a
   * retry reuses the same file without asking the person to record again. */
  async function cloneSample(agentId: string) {
    if (!sample) return
    setClonePhase('cloning'); setError(null)
    const data = new FormData()
    data.set('sample', sample)
    data.set('consent_action', 'create_own_voice')
    try {
      const updated = await api.upload<Agent>(`/api/voice/agents/${agentId}/voice-sample`, data)
      setAgents((list) => list.map((item) => (item.id === updated.id ? updated : item)))
      setClonePhase('ready')
      push('Voice cloned. Preview it below.', 'success')
      closeForm()
    } catch (cause) {
      setClonePhase('error')
      setError(voiceError(cause))
    }
  }

  function retryClone() { if (pendingAgentId) void cloneSample(pendingAgentId) }

  function closeForm() {
    cancelSampleRecording()
    setEditing(false); setReplacingSample(false)
    resetSampleState(); setClonePhase('idle'); setPendingAgentId(null)
  }

  function beginEdit(item?: Agent) {
    setCreating(!item)
    setSelected(item?.id ?? '')
    setPendingAgentId(item?.id ?? null)
    setAgentName(item?.name ?? '')
    setIntroduction(item?.introduction ?? DEFAULT_INTRODUCTION)
    setInstructions(item?.instructions ?? '')
    setDuration(item?.max_duration_seconds ?? 300)
    setVoiceSource('custom'); setSampleTab('record')
    resetSampleState(); setClonePhase('idle'); setReplacingSample(false)
    setEditing(true); setError(null)
  }

  const hasClonedVoice = agent?.voice.kind === 'cloned'
  const requiresSample = creating && voiceSource === 'custom' && !sample

  /** Create/update the agent, then — only if a sample was captured — clone it. Success (and the
   * "ready" state) is only reported once cloning actually succeeds; a create-only save (bundled
   * voice, or editing text fields on an agent whose voice is untouched) reports success right away. */
  async function handleSave(event: FormEvent) {
    event.preventDefault()
    if (recording || requiresSample) return
    setError(null); setClonePhase('preparing')
    try {
      const values = { name: agentName, introduction, instructions, max_duration_seconds: duration }
      const targetId = pendingAgentId || selected
      const result = targetId
        ? await api.patch<Agent>(`/api/voice/agents/${targetId}`, values)
        : await api.post<Agent>('/api/voice/agents', values)
      setAgents((list) => (list.some((item) => item.id === result.id) ? list.map((item) => (item.id === result.id ? result : item)) : [...list, result]))
      setSelected(result.id)
      setPendingAgentId(result.id)
      if (voiceSource === 'custom' && sample) {
        await cloneSample(result.id)
      } else {
        setClonePhase('ready')
        push('Agent saved.', 'success')
        closeForm()
      }
    } catch (cause) {
      setClonePhase('error')
      setError(voiceError(cause))
    }
  }

  function openReplaceSample() {
    if (!agent) return
    setPendingAgentId(agent.id)
    setSampleTab('record'); resetSampleState(); setClonePhase('idle')
    setReplacingSample(true)
  }

  function closeReplaceSample() {
    cancelSampleRecording()
    setReplacingSample(false)
    resetSampleState(); setClonePhase('idle'); setPendingAgentId(null)
  }

  function cancelSampleRecording() {
    captureRequestRef.current++
    if (mediaRecorderRef.current) {
      mediaRecorderRef.current.onstop = null
      if (mediaRecorderRef.current.state === 'recording') mediaRecorderRef.current.stop()
    }
    teardownSampleRecording()
  }

  async function saveReplacementSample() {
    if (!agent || !sample) return
    setPendingAgentId(agent.id)
    await cloneSample(agent.id)
  }

  async function switchDefault() {
    if (!agent) return
    setBusy(true); setError(null)
    try { const updated = await api.patch<Agent>(`/api/voice/agents/${agent.id}`, { voice: 'default' }); setAgents((list) => list.map((item) => item.id === updated.id ? updated : item)) }
    catch (cause) { setError(voiceError(cause)) }
    finally { setBusy(false) }
  }

  async function stopCall(sendStop = true) {
    if (sendStop && socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: 'stop' }))
    socketRef.current?.close(); socketRef.current = null
    await audioRef.current?.stop(); audioRef.current = null
    setCallState('idle'); setPartial({ committed: '', tentative: '' })
  }

  async function startCall() {
    if (!agent) return
    setError(null); setTranscript([]); setCallState('connecting')
    try {
      const audio = new VoiceAudio()
      audioRef.current = audio
      await audio.unlock()
      // `saveTranscript` is the real, current value of the on-screen setting — never hardcoded —
      // so the server only ever persists a transcript when this session actually asked for it.
      const ticket = await api.post<{ session_id: string; ticket: string; websocket_path: string }>('/api/voice/browser-sessions', { agent_id: agent.id, company_id: companyId || null, recording_consent: saveTranscript })
      const wsUrl = new URL(ticket.websocket_path, window.location.href)
      wsUrl.protocol = wsUrl.protocol === 'https:' ? 'wss:' : 'ws:'
      const socket = new WebSocket(wsUrl)
      socketRef.current = socket
      socket.onopen = () => { socket.send(JSON.stringify({ type: 'auth', ticket: ticket.ticket })) }
      socket.onmessage = (event) => {
        const data = JSON.parse(String(event.data)) as ServerEvent
        if (data.type === 'ready') {
          void audio.startCapture((capture: VoiceAudioEvent) => {
            if (socket.readyState !== WebSocket.OPEN) return
            if (capture.type === 'speech_start') socket.send(JSON.stringify({ type: 'interrupt' }))
            else if (capture.type === 'speech_end') socket.send(JSON.stringify({ type: 'end_turn' }))
            else if (capture.type === 'pcm') socket.send(new Int16Array(capture.samples).buffer)
          }, (utteranceId) => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'playback_ack', utterance_id: utteranceId })) }).then(() => setCallState('live')).catch((cause) => { setError(voiceError(cause)); void stopCall() })
        } else if (data.type === 'partial') {
          setPartial({ committed: data.committed ?? data.text ?? '', tentative: data.tentative ?? '' })
        } else if (data.type === 'final') {
          if (data.text) setTranscript((lines) => [...lines, { role: 'user', text: data.text! }])
          setPartial({ committed: '', tentative: '' })
        } else if (data.type === 'agent_text') {
          setTranscript((lines) => [...lines, { role: 'agent', text: data.text ?? '' }])
        } else if (data.type === 'audio' && data.utterance_id && data.pcm) {
          audioRef.current?.enqueue(data.utterance_id, data.seq ?? 0, data.sample_rate ?? 24000, data.pcm)
        } else if (data.type === 'agent_done' && data.utterance_id) audioRef.current?.finish(data.utterance_id)
        else if (data.type === 'clear' && data.utterance_id) audioRef.current?.clear(data.utterance_id)
        else if (data.type === 'error') { setError(data.message ?? 'Voice conversation error.'); if (data.fatal) void stopCall(false) }
        else if (data.type === 'ended') void stopCall(false)
      }
      socket.onerror = () => { setError('The browser conversation connection failed. Check that the voice service is available.'); void stopCall(false) }
      socket.onclose = (event) => {
        void audio.stop()
        if (audioRef.current === audio) audioRef.current = null
        if (!disposedRef.current) { setCallState('idle'); if (event.code !== 1000 && event.code !== 1005) setError((current) => current ?? `Conversation disconnected (${event.code}).`); void api.get<SavedSession[]>('/api/voice/sessions').then(setSessions).catch(() => {}) }
      }
    } catch (cause) { setError(voiceError(cause)); await stopCall(false) }
  }

  return <section className="voice-panel" aria-labelledby="voice-panel-title">
    <header className="voice-panel__header"><div><h2 id="voice-panel-title">Browser conversation</h2><p>Speak in your browser using local speech recognition and voice.</p></div><span className={`voice-panel__availability ${capabilities?.browser.available ? 'is-available' : ''}`}>{capabilities ? capabilities.browser.available ? 'Available' : 'Unavailable' : 'Checking…'}</span></header>
    <p className="voice-panel__phone">Telephone calling can be connected later.</p>
    {error && <p className="field-error" role="alert">{error}</p>}
    {editing ? <form className="panel voice-form" onSubmit={(event) => void handleSave(event)}>
        <h3>{creating ? 'New conversation agent' : 'Edit conversation agent'}</h3>
        <div className="field-col">
          <label htmlFor="voice-name">Name</label>
          <input id="voice-name" value={agentName} onChange={(e) => setAgentName(e.target.value)} required maxLength={120} />
        </div>
        <div className="field-col">
          <label htmlFor="voice-intro">Introduction <span className="muted small">Must clearly disclose that the speaker is AI.</span></label>
          <textarea id="voice-intro" value={introduction} onChange={(e) => setIntroduction(e.target.value)} required maxLength={600} />
        </div>
        <details className="voice-advanced">
          <summary>Advanced settings (optional)</summary>
          <div className="field-col">
            <label htmlFor="voice-instructions">Additional instructions</label>
            <textarea id="voice-instructions" value={instructions} onChange={(e) => setInstructions(e.target.value)} maxLength={4000} />
          </div>
          <div className="field-col">
            <label htmlFor="voice-duration">Maximum conversation length (seconds)</label>
            <input id="voice-duration" type="number" min={30} max={900} value={duration} onChange={(e) => setDuration(Number(e.target.value))} />
          </div>
        </details>

        {!hasClonedVoice && <div className="voice-source" role="radiogroup" aria-label="Voice source">
          <label><input type="radio" name="voice-source" checked={voiceSource === 'custom'} onChange={() => setVoiceSource('custom')} /> Record or upload a custom voice (recommended)</label>
          <label><input type="radio" name="voice-source" checked={voiceSource === 'bundled'} onChange={() => setVoiceSource('bundled')} /> Use the bundled voice</label>
        </div>}
        {hasClonedVoice && <p className="muted small">This agent already has a custom cloned voice. Record or upload a new sample below to replace it, or leave it as is.</p>}

        {(hasClonedVoice || voiceSource === 'custom') && <>
          <p className="voice-notice"><ShieldCheck size={14} aria-hidden="true" /> Only record or upload a voice you own, or one you have explicit permission to clone.</p>
          <VoiceSampleCapture tab={sampleTab} onTab={setSampleTab} sample={sample} sampleUrl={sampleUrl} sampleSeconds={sampleSeconds}
            recording={recording} elapsedMs={recordElapsedMs} micError={micError}
            onStartRecording={() => void startSampleRecording()} onStopRecording={stopSampleRecording}
            onFile={(e) => void handleSampleFile(e)} onClear={clearSample}
            disabled={clonePhase === 'preparing' || clonePhase === 'cloning'} />
        </>}

        {requiresSample && <p className="muted small">Record or upload a sample, or switch to the bundled voice.</p>}
        {clonePhase === 'cloning' && <p className="voice-progress" role="status"><Loader2 className="spin" size={14} aria-hidden="true" /> Cloning voice… this can take a little while the first time.</p>}
        {clonePhase === 'error' && pendingAgentId && sample && <button type="button" className="btn btn--secondary" onClick={retryClone}><RotateCcw size={14} aria-hidden="true" />Retry cloning with the same sample</button>}

        <div className="voice-panel__actions">
          <button className="btn btn--primary" disabled={recording || requiresSample || clonePhase === 'preparing' || clonePhase === 'cloning'}>
            {clonePhase === 'preparing' ? 'Saving…' : clonePhase === 'cloning' ? 'Cloning voice…' : 'Save agent'}
          </button>
          <button type="button" className="btn btn--ghost" onClick={closeForm} disabled={clonePhase === 'preparing' || clonePhase === 'cloning'}>Cancel</button>
        </div>
      </form> : <>
      <div className="voice-panel__agent-row"><label htmlFor="voice-agent">Conversation agent</label><select id="voice-agent" value={selected} onChange={(e) => setSelected(e.target.value)}><option value="">Choose an agent</option>{agents.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select><button className="btn btn--secondary" type="button" onClick={() => beginEdit()} disabled={callState !== 'idle'}>Create agent</button>{agent && <button className="btn btn--secondary" type="button" onClick={() => beginEdit(agent)} disabled={callState !== 'idle'}>Edit</button>}</div>
      {agent && <>
        <div className="panel voice-panel__voice">
          <div className="voice-panel__voice-row">
            <div><strong>{agent.voice.label}</strong>{agent.voice.kind === 'cloned' && agent.voice.authorization?.authorized_at && <p className="muted small">Cloned {new Date(agent.voice.authorization.authorized_at).toLocaleDateString()}</p>}</div>
            <audio key={agent.updated_at} controls preload="none" src={`/api/voice/agents/${agent.id}/preview?v=${encodeURIComponent(agent.updated_at)}`} aria-label="Preview agent introduction" />
            <div className="voice-panel__actions">
              {agent.voice.kind === 'cloned' && <button className="btn btn--secondary" type="button" onClick={() => void switchDefault()} disabled={busy}>Use bundled voice</button>}
              <button className="btn btn--secondary" type="button" onClick={openReplaceSample} disabled={callState !== 'idle'}>{agent.voice.kind === 'cloned' ? 'Replace sample' : 'Record or upload a voice'}</button>
            </div>
          </div>
          {replacingSample && <div className="voice-replace-panel">
            <p className="voice-notice"><ShieldCheck size={14} aria-hidden="true" /> Only record or upload a voice you own, or one you have explicit permission to clone.</p>
            <VoiceSampleCapture tab={sampleTab} onTab={setSampleTab} sample={sample} sampleUrl={sampleUrl} sampleSeconds={sampleSeconds}
              recording={recording} elapsedMs={recordElapsedMs} micError={micError}
              onStartRecording={() => void startSampleRecording()} onStopRecording={stopSampleRecording}
              onFile={(e) => void handleSampleFile(e)} onClear={clearSample}
              disabled={clonePhase === 'cloning'} />
            {clonePhase === 'cloning' && <p className="voice-progress" role="status"><Loader2 className="spin" size={14} aria-hidden="true" /> Cloning voice…</p>}
            {clonePhase === 'ready' && <p className="voice-progress voice-progress--ready" role="status"><CheckCircle2 size={14} aria-hidden="true" /> Voice ready.</p>}
            <div className="voice-panel__actions">
              <button type="button" className="btn btn--primary" disabled={recording || !sample || clonePhase === 'cloning'} onClick={() => void saveReplacementSample()}>{clonePhase === 'cloning' ? 'Cloning…' : 'Save voice'}</button>
              {clonePhase === 'error' && <button type="button" className="btn btn--secondary" onClick={retryClone}><RotateCcw size={14} aria-hidden="true" />Retry with the same sample</button>}
              <button type="button" className="btn btn--ghost" onClick={closeReplaceSample} disabled={clonePhase === 'cloning'}>Cancel</button>
            </div>
          </div>}
        </div>
        <div className="panel voice-panel__start">
          <div className="field-col"><label htmlFor="voice-company">Associate with a company (optional)</label><CompanyPicker id="voice-company" value={companyId} onChange={(value) => setCompanyId(String(value))} emptyLabel="No company" onResolved={rememberCompanies} /></div>
          <label className="checkbox-label"><input type="checkbox" checked={saveTranscript} onChange={(e) => setSaveTranscript(e.target.checked)} /> Save transcript</label>
          <p className="muted small">Try the agent here using your microphone.</p>
          <div className="voice-panel__actions">
            <button className="btn btn--primary" type="button" disabled={!capabilities?.browser.available || callState !== 'idle'} onClick={() => void startCall()}>{callState === 'connecting' ? 'Connecting…' : 'Start browser conversation'}</button>
            {callState !== 'idle' && <button className="btn btn--secondary" type="button" onClick={() => void stopCall()}>End conversation</button>}
          </div>
        </div>
      </>}
    </>}
    {(transcript.length > 0 || partial.committed || partial.tentative) && <div className="voice-panel__transcript" aria-live="polite" aria-label="Conversation captions">{transcript.map((line, index) => <p key={`${index}-${line.role}`} className={`voice-panel__line is-${line.role}`}><strong>{line.role === 'user' ? 'You' : agent?.name ?? 'AI'}:</strong> {line.text}</p>)}{(partial.committed || partial.tentative) && <p className="voice-panel__line is-user"><strong>You:</strong> {partial.committed}<span className="voice-panel__tentative">{partial.tentative}</span></p>}</div>}
    <section className="voice-panel__history" aria-labelledby="voice-history-title"><header><h3 id="voice-history-title">Previous conversations</h3><button type="button" className="btn btn--secondary" onClick={() => void api.get<SavedSession[]>('/api/voice/sessions').then(setSessions).catch((cause) => setError(voiceError(cause)))}>Refresh</button></header>{sessions.length === 0 ? <p className="voice-panel__hint">Saved conversations will appear here.</p> : <ul>{sessions.map((session) => {
      const savedAgent = agents.find((item) => item.id === session.agent_id)
      const company = companies.find((item) => item.id === session.company_id)
      const expanded = expandedSession?.id === session.id
      return <li key={session.id}><button type="button" className="voice-panel__history-item" aria-expanded={expanded} onClick={async () => {
        if (expanded) { setExpandedSession(null); return }
        try { setExpandedSession(await api.get<SavedSession>(`/api/voice/sessions/${session.id}`)) }
        catch (cause) { setError(voiceError(cause)) }
      }}><span><strong>{company?.name ?? savedAgent?.name ?? 'Conversation'}</strong><small>{new Date(session.created_at).toLocaleString()} · {session.status}{session.end_reason ? ` · ${session.end_reason.replaceAll('_', ' ')}` : ''}</small></span><span>{expanded ? 'Hide' : 'View'}</span></button>{expanded && expandedSession && <div className="voice-panel__history-detail">{expandedSession.proposed_outcome && <><p><strong>Proposed summary:</strong> {expandedSession.proposed_outcome.summary || 'No summary available.'}</p><p><strong>Stated interest (proposed):</strong> {expandedSession.proposed_outcome.stated_interest || 'unknown'}</p>{expandedSession.proposed_outcome.follow_ups?.length ? <p><strong>Possible follow-ups:</strong> {expandedSession.proposed_outcome.follow_ups.join(' · ')}</p> : null}<p className="voice-panel__hint">{expandedSession.proposed_outcome.note}</p></>}{expandedSession.transcript?.length ? expandedSession.transcript.map((line, index) => <p key={`${index}-${line.role}`}><strong>{line.role === 'user' ? 'Participant' : savedAgent?.name ?? 'Agent'}:</strong> {line.text}</p>) : <p className="voice-panel__hint">No transcript was saved for this conversation.</p>}</div>}</li>
    })}</ul>}</section>
  </section>
}
