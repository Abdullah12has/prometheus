export type VoiceAudioEvent =
  | { type: 'pcm'; samples: Int16Array }
  | { type: 'speech_start' }
  | { type: 'speech_end' }

export class VoiceAudio {
  private context: AudioContext | null = null
  private captureGeneration = 0
  private stream: MediaStream | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private processor: AudioWorkletNode | null = null
  private mute: GainNode | null = null
  private currentUtterance: string | null = null
  private buffers = new Map<number, AudioBuffer>()
  private sources = new Set<AudioBufferSourceNode>()
  private expectedSeq = 0
  private nextTime = 0
  private done = false
  private onAck: ((id: string) => void) | null = null

  async unlock() {
    this.context = new AudioContext()
    await this.context.resume()
  }

  async startCapture(onEvent: (event: VoiceAudioEvent) => void, onAck: (id: string) => void) {
    this.context ??= new AudioContext()
    await this.context.audioWorklet.addModule('/voice-capture.worklet.js')
    const generation = ++this.captureGeneration
    const pending = navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
    let expired = false
    let timer: ReturnType<typeof setTimeout>
    pending.then((stream) => { if (expired || generation !== this.captureGeneration) stream.getTracks().forEach(track => track.stop()) }, () => {})
    try {
      this.stream = await Promise.race([pending, new Promise<never>((_, reject) => {
        timer = setTimeout(() => { expired = true; reject(new Error('Microphone did not start. Check browser microphone permissions and try again.')) }, 20_000)
      })])
    } finally { clearTimeout(timer!) }
    if (generation !== this.captureGeneration || !this.context) throw new Error('Microphone capture was cancelled')
    this.source = this.context.createMediaStreamSource(this.stream)
    this.processor = new AudioWorkletNode(this.context, 'voice-capture', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] })
    this.mute = this.context.createGain()
    this.mute.gain.value = 0
    this.processor.port.onmessage = (event: MessageEvent<VoiceAudioEvent>) => onEvent(event.data)
    this.onAck = onAck
    this.source.connect(this.processor).connect(this.mute).connect(this.context.destination)
    await this.context.resume()
  }

  enqueue(id: string, seq: number, rate: number, base64: string) {
    const context = this.context
    if (!context) return
    if (id !== this.currentUtterance) {
      this.flush()
      this.currentUtterance = id
      this.expectedSeq = 0
      this.nextTime = context.currentTime
      this.done = false
    }
    const raw = Uint8Array.from(atob(base64), (character) => character.charCodeAt(0))
    const view = new DataView(raw.buffer, raw.byteOffset, raw.byteLength)
    const audio = context.createBuffer(1, Math.floor(raw.byteLength / 2), rate)
    const channel = audio.getChannelData(0)
    for (let i = 0; i < channel.length; i++) channel[i] = view.getInt16(i * 2, true) / 32768
    this.buffers.set(seq, audio)
    this.scheduleAvailable()
  }

  finish(id: string) {
    if (id !== this.currentUtterance) return
    this.done = true
    this.scheduleAvailable()
    this.ackIfPlayed()
  }

  clear(id: string) {
    if (id !== this.currentUtterance) return
    this.flush()
    this.currentUtterance = null
  }

  private scheduleAvailable() {
    const context = this.context
    if (!context) return
    while (this.buffers.has(this.expectedSeq)) {
      const buffer = this.buffers.get(this.expectedSeq)!
      this.buffers.delete(this.expectedSeq++)
      const source = context.createBufferSource()
      source.buffer = buffer
      source.connect(context.destination)
      source.onended = () => {
        this.sources.delete(source)
        this.ackIfPlayed()
      }
      const when = Math.max(this.nextTime, context.currentTime + 0.02)
      source.start(when)
      this.nextTime = when + buffer.duration
      this.sources.add(source)
    }
  }

  private ackIfPlayed() {
    if (this.done && this.buffers.size === 0 && this.sources.size === 0 && this.currentUtterance) {
      this.onAck?.(this.currentUtterance)
      this.currentUtterance = null
      this.done = false
    }
  }

  private flush() {
    this.buffers.clear()
    for (const source of this.sources) {
      source.onended = null
      try { source.stop() } catch { /* already ended */ }
      source.disconnect()
    }
    this.sources.clear()
    this.done = false
  }

  async stop() {
    this.captureGeneration++
    this.flush()
    this.currentUtterance = null
    this.processor?.port.close()
    this.processor?.disconnect()
    this.source?.disconnect()
    this.mute?.disconnect()
    this.stream?.getTracks().forEach((track) => track.stop())
    if (this.context && this.context.state !== 'closed') await this.context.close()
    this.context = null
    this.stream = null
    this.processor = null
  }
}
