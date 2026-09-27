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
  }

  process(inputs) {
    const input = inputs[0]?.[0]
    if (!input) return true
    let energy = 0
    for (let i = 0; i < input.length; i++) energy += input[i] * input[i]
    const loud = Math.sqrt(energy / input.length) > 0.015
    if (loud) {
      this.quietFrames = 0
      if (!this.speech) {
        this.speech = true
        this.port.postMessage({ type: 'speech_start' })
      }
    } else if (this.speech) {
      this.quietFrames += input.length
    }

    for (let i = 0; i < input.length; i++) {
      const value = input[i]
      while (this.nextPosition <= this.inputIndex) {
        const fraction = this.inputIndex === 0 ? 1 : this.nextPosition - (this.inputIndex - 1)
        const sample = this.inputIndex === 0 ? value : this.previous + (value - this.previous) * fraction
        if (this.speech) this.frame.push(Math.max(-32768, Math.min(32767, Math.round(sample * 32767))))
        this.nextPosition += this.ratio
      }
      this.previous = value
      this.inputIndex++
    }
    while (this.frame.length >= 3200) this.postFrame(3200)
    if (this.speech && this.quietFrames >= sampleRate * 0.7) {
      if (this.frame.length) this.postFrame(this.frame.length)
      this.speech = false
      this.quietFrames = 0
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
