class VoiceCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this.ratio = sampleRate / 16000
    this.nextPosition = 0
    this.inputIndex = 0
    this.previous = 0
    this.frame = []
    this.speech = false
    this.quietFrames = 0
    this.loudFrames = 0
    this.turnFrames = 0
    this.preRoll = []
    this.noiseFloor = 0.003
    this.playing = false
    this.port.onmessage = ({ data }) => { this.playing = data.playing === true }
  }

  process(inputs) {
    const input = inputs[0]?.[0]
    if (!input) return true
    let energy = 0
    for (let i = 0; i < input.length; i++) energy += input[i] * input[i]
    const rms = Math.sqrt(energy / input.length)
    // ponytail: energy VAD with hysteresis; use a local speech classifier if sustained non-speech still triggers.
    const threshold = Math.max(0.015, this.noiseFloor * 3)
    const loud = rms > (this.speech ? Math.max(0.008, this.noiseFloor * 1.8) : threshold)
    if (!this.speech) {
      // Echo cancellation runs at capture. Don't learn residual agent audio as room noise.
      if (!loud && !this.playing) this.noiseFloor = this.noiseFloor * 0.98 + rms * 0.02
      this.loudFrames = loud ? this.loudFrames + input.length : 0
      if (this.loudFrames >= sampleRate * 0.12) {
        this.speech = true
        this.frame.push(...this.preRoll)
        this.preRoll = []
        this.turnFrames = 0
        this.port.postMessage({ type: 'speech_start' })
      }
    }
    if (this.speech) {
      this.turnFrames += input.length
      this.quietFrames = loud ? 0 : this.quietFrames + input.length
    }

    for (let i = 0; i < input.length; i++) {
      const value = input[i]
      while (this.nextPosition <= this.inputIndex) {
        const fraction = this.inputIndex === 0 ? 1 : this.nextPosition - (this.inputIndex - 1)
        const sample = this.inputIndex === 0 ? value : this.previous + (value - this.previous) * fraction
        const pcm = Math.max(-32768, Math.min(32767, Math.round(sample * 32767)))
        if (this.speech) this.frame.push(pcm)
        else this.preRoll.push(pcm)
        this.nextPosition += this.ratio
      }
      this.previous = value
      this.inputIndex++
    }
    if (this.preRoll.length > 4000) this.preRoll.splice(0, this.preRoll.length - 4000)
    while (this.frame.length >= 3200) this.postFrame(3200)
    if (this.speech && (this.quietFrames >= sampleRate * 0.5 || this.turnFrames >= sampleRate * 25)) {
      if (this.frame.length) this.postFrame(this.frame.length)
      this.speech = false
      this.quietFrames = 0
      this.loudFrames = 0
      this.turnFrames = 0
      this.port.postMessage({ type: 'speech_end' })
    }
    return true
  }

  postFrame(length) {
    const samples = Int16Array.from(this.frame.splice(0, length))
    this.port.postMessage({ type: 'pcm', samples }, [samples.buffer])
  }
}

registerProcessor('voice-capture', VoiceCaptureProcessor)
