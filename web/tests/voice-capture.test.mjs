import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import assert from 'node:assert/strict'
import { test } from 'node:test'
function capture() {
  let Processor
  const events = []
  vm.runInNewContext(readFileSync(new URL('../public/voice-capture.worklet.js', import.meta.url), 'utf8'), {
    sampleRate: 48000, Int16Array,
    AudioWorkletProcessor: class { port = { postMessage: event => events.push(event) } },
    registerProcessor: (_, value) => { Processor = value },
  })
  const processor = new Processor()
  const feed = (amplitude, seconds) => {
    for (let i = 0; i < Math.ceil(seconds * 48000 / 128); i++) processor.process([[new Float32Array(128).fill(amplitude)]])
  }
  return { processor, events, feed }
}
test('brief noise does not interrupt; sustained speech preserves audio and ends once', () => {
  const { events, feed } = capture()
  feed(0, 0.3); feed(0.1, 0.01); feed(0, 1)
  assert.equal(events.length, 0)
  feed(0.08, 0.4); feed(0, 0.8)
  assert.equal(events.filter(e => e.type === 'speech_start').length, 1)
  assert.equal(events.filter(e => e.type === 'speech_end').length, 1)
  assert.ok(events.filter(e => e.type === 'pcm').reduce((n, e) => n + e.samples.length, 0) >= 6400)
})
test('continuous input has bounded turns instead of remaining stuck listening', () => {
  const { events, feed } = capture()
  feed(0, 0.3); feed(0.08, 26)
  assert.ok(events.some(e => e.type === 'speech_end'))
})
test('normal speech interrupts playback promptly without treating brief noise as speech', () => {
  const { processor, events, feed } = capture()
  processor.port.onmessage({ data: { playing: true } })
  feed(0.004, 1)
  feed(0.03, 0.02); feed(0.004, 0.3)
  assert.equal(events.length, 0)
  feed(0.024, 0.16)
  assert.equal(events.filter(e => e.type === 'speech_start').length, 1)
  feed(0.024, 0.4); feed(0, 0.8)
  assert.equal(events.filter(e => e.type === 'speech_end').length, 1)
  assert.ok(events.some(e => e.type === 'pcm'))
})
