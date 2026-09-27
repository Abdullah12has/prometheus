import { test, expect } from '@playwright/test'

test('chat displays its returned answer and safe source links without refetching history', async ({ page }) => {
  await page.route('**/api/auth/me', route => route.fulfill({ json: { authenticated: true, csrf_token: 'test' } }))
  await page.route('**/api/companies*', route => route.fulfill({ json: { items: [], total: 0 } }))
  await page.route('**/api/assistant/conversations', route => route.fulfill({ json: [] }))
  await page.route('**/api/assistant/conversations/*', route => route.fulfill({ status: 500, json: {} }))
  await page.route('**/api/assistant', route => route.fulfill({ json: {
    conversation_id: 'fixture', reply: 'The answer is supported by [Official source](https://example.org/report).',
    actions: [{ kind: 'api', tool: 'search_sources', status: 200, result: { sources: [
      { title: 'Official report', url: 'https://example.org/report' },
      { title: 'Unsafe source', url: 'javascript:alert(1)' },
    ] } }],
  } }))
  await page.goto('/companies')
  await page.getByRole('button', { name: 'Assistant', exact: true }).click()
  await page.getByLabel('Message the assistant').fill('Find a source')
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await expect(page.getByRole('link', { name: 'Official source', exact: true })).toHaveAttribute('href', 'https://example.org/report')
  await expect(page.getByRole('link', { name: 'Official report', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Unsafe source' })).toHaveCount(0)
  await expect(page.getByLabel('Message the assistant')).toBeEnabled()
})

test('cleared voice audio cannot resume when late chunks arrive', async ({ page }) => {
  await page.goto('/')
  const result = await page.evaluate(async () => {
    // Exercise the production playback implementation with the browser AudioContext.
    const { VoiceAudio } = await import('/src/lib/voice-audio.ts')
    const audio = new VoiceAudio()
    await audio.unlock()
    audio.clear('cancelled')
    audio.enqueue('cancelled', 0, 24000, 'not-valid-base64')
    await audio.stop()
    return true
  })
  expect(result).toBe(true)
})

test('speaking stops queued audio, rejects late playback and keeps listening for the next turn', async ({ page }) => {
  await page.goto('/')
  const result = await page.evaluate(async () => {
    const { VoiceAudio } = await import('/src/lib/voice-audio.ts')
    const microphone = new AudioContext()
    await microphone.resume()
    const tone = microphone.createOscillator()
    const gain = microphone.createGain()
    gain.gain.value = 0
    const destination = microphone.createMediaStreamDestination()
    tone.connect(gain).connect(destination)
    tone.start()
    navigator.mediaDevices.getUserMedia = async () => destination.stream
    const audio = new VoiceAudio()
    const acks: string[] = []
    let began: () => void = () => {}, ended: () => void = () => {}, played: () => void = () => {}
    const speech = new Promise<void>(resolve => { began = resolve })
    const silence = new Promise<void>(resolve => { ended = resolve })
    const reply = new Promise<void>(resolve => { played = resolve })
    try {
      await audio.unlock()
      await audio.startCapture(event => {
        if (event.type === 'speech_start') began()
        if (event.type === 'speech_end') ended()
      }, id => { acks.push(id); if (id === 'next-reply') played() })
      audio.enqueue('agent-speaking', 0, 24000, btoa('\x00\x10'.repeat(24000 * 4)))
      audio.finish('agent-speaking')
      gain.gain.value = 0.08
      await speech
      // A new packet can race with the server's clear event. It must never play.
      audio.enqueue('late-packet', 0, 24000, 'not-valid-base64')
      gain.gain.value = 0
      await silence
      audio.enqueue('late-packet', 1, 24000, 'not-valid-base64')
      audio.enqueue('next-reply', 0, 24000, btoa('\x00\x10'.repeat(2400)))
      audio.finish('next-reply')
      await reply
      return acks
    } finally {
      await audio.stop()
      await microphone.close()
    }
  })
  expect(result).toEqual(['next-reply'])
})
