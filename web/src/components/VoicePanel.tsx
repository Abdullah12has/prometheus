import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { api, ApiError } from '../lib/api'
import { VoiceAudio, type VoiceAudioEvent } from '../lib/voice-audio'
import './voice.css'

type Agent = { id: string; name: string; introduction: string; instructions: string; max_duration_seconds: number; voice: { kind: string; label: string; authorization: { voice_owner_name: string; basis: string } | null } }
type Capabilities = { browser: { available: boolean; label: string; speech_runtime: boolean; language_model: boolean }; phone: { available: false; label: string; reason: string } }
type TranscriptLine = { role: 'user' | 'agent'; text: string }
type ServerEvent = { type: string; session_id?: string; websocket_path?: string; channel_label?: string; utterance_id?: string; seq?: number; sample_rate?: number; pcm?: string; text?: string; committed?: string; tentative?: string; turn_id?: string | null; reason?: string; message?: string; fatal?: boolean }
const voiceError = (cause: unknown) => cause instanceof ApiError ? `${cause.message}${cause.detail ? ` (${JSON.stringify(cause.detail)})` : ''}` : cause instanceof Error ? cause.message : 'The request could not be completed.'

export function VoicePanel() {
  const [agents, setAgents] = useState<Agent[]>([])
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [selected, setSelected] = useState('')
  const [editing, setEditing] = useState(false)
  const [agentName, setAgentName] = useState('')
  const [introduction, setIntroduction] = useState('Hello, I am an AI assistant calling from the team. Is now a good time to talk?')
  const [instructions, setInstructions] = useState('')
  const [duration, setDuration] = useState(300)
  const [sample, setSample] = useState<File | null>(null)
  const [owner, setOwner] = useState('')
  const [basis, setBasis] = useState<'self' | 'written_permission'>('self')
  const [authorized, setAuthorized] = useState(false)
  const [recording, setRecording] = useState(false)
  const [sampleSeconds, setSampleSeconds] = useState(0)
  const [consent, setConsent] = useState(false)
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
  const disposedRef = useRef(false)

  const refresh = useCallback(async () => {
    try {
      const [caps, data] = await Promise.all([api.get<Capabilities>('/api/voice/capabilities'), api.get<Agent[]>('/api/voice/agents')])
      setCapabilities(caps); setAgents(data); setSelected((current) => current || data[0]?.id || '')
    } catch (cause) { setError(voiceError(cause)) }
  }, [])
  useEffect(() => { void refresh() }, [refresh])
  const agent = agents.find((item) => item.id === selected)

  useEffect(() => {
    disposedRef.current = false
    return () => {
    disposedRef.current = true
    if (socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: 'stop' }))
    socketRef.current?.close()
    void audioRef.current?.stop()
    if (mediaRecorderRef.current?.state === 'recording') mediaRecorderRef.current.stop()
    sampleStreamRef.current?.getTracks().forEach((track) => track.stop())
    }
  }, [])

  function beginEdit(item?: Agent) {
    setSelected(item?.id ?? '')
    setAgentName(item?.name ?? '')
    setIntroduction(item?.introduction ?? 'Hello, I am an AI assistant calling from the team. Is now a good time to talk?')
    setInstructions(item?.instructions ?? '')
    setDuration(item?.max_duration_seconds ?? 300)
    setEditing(true); setError(null)
  }

  async function saveAgent(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(null)
    try {
      const values = { name: agentName, introduction, instructions, max_duration_seconds: duration }
      const result = selected ? await api.patch<Agent>(`/api/voice/agents/${selected}`, values) : await api.post<Agent>('/api/voice/agents', values)
      setAgents((list) => selected ? list.map((item) => item.id === result.id ? result : item) : [...list, result])
      setSelected(result.id); setEditing(false)
    } catch (cause) { setError(voiceError(cause)) }
    finally { setBusy(false) }
  }

  async function startSampleRecording() {
    setError(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      sampleStreamRef.current = stream
      const recorder = new MediaRecorder(stream)
      sampleChunksRef.current = []
      recordingStartedRef.current = Date.now()
      recorder.ondataavailable = (event) => { if (event.data.size) sampleChunksRef.current.push(event.data) }
      recorder.onstop = () => {
        const seconds = (Date.now() - recordingStartedRef.current) / 1000
        setSampleSeconds(seconds)
        setSample(new File(sampleChunksRef.current, `voice-sample.${recorder.mimeType.includes('ogg') ? 'ogg' : 'webm'}`, { type: recorder.mimeType }))
        stream.getTracks().forEach((track) => track.stop())
        sampleStreamRef.current = null
        setRecording(false)
        if (seconds < 3 || seconds > 30) setError('Voice samples must be between 3 and 30 seconds. Record another sample.')
      }
      recorder.start(); mediaRecorderRef.current = recorder; setRecording(true)
      window.setTimeout(() => { if (recorder.state === 'recording') recorder.stop() }, 29_500)
    } catch (cause) { setError(voiceError(cause)) }
  }

  async function uploadSample(event: FormEvent) {
    event.preventDefault()
    if (!agent || !sample || !authorized) return
    if (sampleSeconds > 0 && (sampleSeconds < 3 || sampleSeconds > 30)) { setError('Voice samples must be between 3 and 30 seconds.'); return }
    const data = new FormData()
    data.set('sample', sample); data.set('voice_owner_name', owner); data.set('owner_authorization', 'true'); data.set('authorization_basis', basis)
    setBusy(true); setError(null)
    try { const updated = await api.upload<Agent>(`/api/voice/agents/${agent.id}/voice-sample`, data); setAgents((list) => list.map((item) => item.id === updated.id ? updated : item)); setSample(null); setAuthorized(false) }
    catch (cause) { setError(voiceError(cause)) }
    finally { setBusy(false) }
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
    if (!agent || !consent) return
    setError(null); setTranscript([]); setCallState('connecting')
    try {
      const audio = new VoiceAudio()
      audioRef.current = audio
      await audio.unlock()
      const ticket = await api.post<{ session_id: string; ticket: string; websocket_path: string }>('/api/voice/browser-sessions', { agent_id: agent.id, recording_consent: true })
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
        if (!disposedRef.current) { setCallState('idle'); if (event.code !== 1000 && event.code !== 1005) setError((current) => current ?? `Conversation disconnected (${event.code}).`) }
      }
    } catch (cause) { setError(voiceError(cause)); await stopCall(false) }
  }

  return <section className="voice-panel" aria-labelledby="voice-panel-title">
    <header className="voice-panel__header"><div><h2 id="voice-panel-title">Browser conversation</h2><p>Local speech recognition and voice. Transcript saved only with participant consent.</p></div><span className={`voice-panel__availability ${capabilities?.browser.available ? 'is-available' : ''}`}>{capabilities ? capabilities.browser.available ? 'Available' : 'Unavailable' : 'Checking…'}</span></header>
    <p className="voice-panel__phone"><strong>Phone calling unavailable.</strong> Telephone calls are not enabled.</p>
    {error && <p className="voice-panel__error" role="alert">{error}</p>}
    {editing ? <form className="voice-panel__form" onSubmit={saveAgent}><h3>{selected ? 'Edit conversation agent' : 'New conversation agent'}</h3><label>Name<input value={agentName} onChange={(e) => setAgentName(e.target.value)} required maxLength={120} /></label><label>Introduction <span>Must clearly disclose that the speaker is AI.</span><textarea value={introduction} onChange={(e) => setIntroduction(e.target.value)} required maxLength={600} /></label><label>Additional instructions<textarea value={instructions} onChange={(e) => setInstructions(e.target.value)} maxLength={4000} /></label><label>Maximum conversation length (seconds)<input type="number" min={30} max={900} value={duration} onChange={(e) => setDuration(Number(e.target.value))} /></label><div className="voice-panel__actions"><button className="voice-button voice-button--primary" disabled={busy}>{busy ? 'Saving…' : 'Save agent'}</button><button type="button" className="voice-button" onClick={() => setEditing(false)}>Cancel</button></div></form> : <>
      <div className="voice-panel__agent-row"><label htmlFor="voice-agent">Conversation agent</label><select id="voice-agent" value={selected} onChange={(e) => setSelected(e.target.value)}><option value="">Choose an agent</option>{agents.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select><button className="voice-button" type="button" onClick={() => beginEdit()}>Create agent</button>{agent && <button className="voice-button" type="button" onClick={() => beginEdit(agent)}>Edit</button>}</div>
      {agent && <><div className="voice-panel__voice"><div><strong>{agent.voice.label}</strong>{agent.voice.authorization && <p>Authorized for {agent.voice.authorization.voice_owner_name} · {agent.voice.authorization.basis.replace('_', ' ')}</p>}</div>{agent.voice.kind === 'cloned' && <button className="voice-button" type="button" onClick={() => void switchDefault()} disabled={busy}>Use bundled voice</button>}<audio controls preload="none" src={`/api/voice/agents/${agent.id}/preview`} aria-label="Preview agent introduction" /></div>
        <details className="voice-panel__details"><summary>Voice sample and owner authorization</summary><form onSubmit={uploadSample}><label>Record a 3–30 second voice sample</label><div className="voice-panel__actions"><button type="button" className="voice-button" disabled={recording || busy} onClick={() => void startSampleRecording()}>{recording ? 'Recording…' : 'Start recording'}</button><button type="button" className="voice-button" disabled={!recording} onClick={() => mediaRecorderRef.current?.stop()}>Stop recording</button></div><label>Or choose an audio file<input type="file" accept="audio/*" onChange={(e) => { const file = e.target.files?.[0] ?? null; setSample(file); setSampleSeconds(0) }} /></label>{sample && <p className="voice-panel__hint">Selected: {sample.name} {sampleSeconds > 0 ? `· ${sampleSeconds.toFixed(1)} seconds` : '· Duration checked during upload'}</p>}<label>Voice owner name<input value={owner} onChange={(e) => setOwner(e.target.value)} required maxLength={200} /></label><label>Authorization basis<select value={basis} onChange={(e) => setBasis(e.target.value as 'self' | 'written_permission')}><option value="self">I am the voice owner</option><option value="written_permission">Written permission from the voice owner</option></select></label><label className="voice-panel__check"><input type="checkbox" checked={authorized} onChange={(e) => setAuthorized(e.target.checked)} required /> I confirm the voice owner authorized this sample to create a voice clone.</label><button className="voice-button voice-button--primary" disabled={busy || !sample || !authorized}>{busy ? 'Uploading…' : 'Create authorized voice'}</button></form></details>
        <div className="voice-panel__start"><label className="voice-panel__check"><input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} /> The participant has agreed to this browser conversation and transcript recording.</label><button className="voice-button voice-button--primary" type="button" disabled={!consent || !capabilities?.browser.available || callState !== 'idle'} onClick={() => void startCall()}>{callState === 'connecting' ? 'Connecting…' : 'Start browser conversation'}</button>{callState !== 'idle' && <button className="voice-button" type="button" onClick={() => void stopCall()}>End conversation</button>}</div>
      </>}
    </>}
    {(transcript.length > 0 || partial.committed || partial.tentative) && <div className="voice-panel__transcript" aria-live="polite" aria-label="Conversation captions">{transcript.map((line, index) => <p key={`${index}-${line.role}`} className={`voice-panel__line is-${line.role}`}><strong>{line.role === 'user' ? 'You' : agent?.name ?? 'AI'}:</strong> {line.text}</p>)}{(partial.committed || partial.tentative) && <p className="voice-panel__line is-user"><strong>You:</strong> {partial.committed}<span className="voice-panel__tentative">{partial.tentative}</span></p>}</div>}
  </section>
}
