import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'

test.use({ permissions: ['microphone'], launchOptions: { args: ['--autoplay-policy=no-user-gesture-required'] } })

test('user speech interrupts the live agent and gets a brief response through local ASR and TTS', async ({ page }) => {
  test.skip(process.env.RUN_VOICE_E2E !== '1', 'Explicit opt-in runs the real local models and configured LLM')
  test.setTimeout(180_000)
  const secret = readFileSync('../.env', 'utf8').split('\n').find(line => line.startsWith('ADMIN_PASSWORD='))?.slice('ADMIN_PASSWORD='.length)
  if (!secret) throw new Error('Local operator password required')
  let agentId: string | undefined
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  let firstId = ''
  let firstAudio: () => void = () => {}, cleared: (id: string) => void = () => {}, played: () => void = () => {}
  const speaking = new Promise<void>(resolve => { firstAudio = resolve })
  const interrupted = new Promise<string>(resolve => { cleared = resolve })
  const replyPlayed = new Promise<void>(resolve => { played = resolve })
  const replies: string[] = []
  page.on('websocket', socket => {
    if (!socket.url().includes('/api/voice/')) return
    socket.on('framereceived', event => {
      const data = JSON.parse(event.payload.toString())
      if (data.type === 'audio' && !firstId) { firstId = data.utterance_id; firstAudio() }
      if (data.type === 'clear') cleared(data.utterance_id)
      if (data.type === 'agent_text' && firstId && data.utterance_id !== firstId) replies.push(data.text)
    })
    socket.on('framesent', event => {
      if (typeof event.payload !== 'string') return
      const data = JSON.parse(event.payload)
      if (data.type === 'playback_ack' && data.utterance_id !== firstId) played()
    })
  })
  // Chromium's fake microphone fails at OS capture on this machine. Replace only
  // the device boundary; the app's resampler, VAD, WebSocket and models stay real.
  await page.addInitScript(async (wav) => {
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new AudioContext()
      await context.resume()
      const bytes = Uint8Array.from(atob(wav), c => c.charCodeAt(0))
      const buffer = await context.decodeAudioData(bytes.buffer)
      const source = context.createBufferSource()
      source.buffer = buffer
      const destination = context.createMediaStreamDestination()
      source.connect(destination)
      window.addEventListener('verification-speech', () => source.start(), { once: true })
      source.onended = () => setTimeout(() => void context.close(), 3000)
      return destination.stream
    }
  }, readFileSync('../data/asr-smoke.wav').toString('base64'))
  try {
    await page.goto('/voice-notes')
    await page.getByLabel('Password', { exact: true }).fill(secret)
    await page.getByRole('button', { name: 'Sign in', exact: true }).click()
    await page.getByRole('button', { name: 'Create agent', exact: true }).click()
    await page.getByLabel('Name', { exact: true }).fill('Temporary browser voice verification')
    await page.getByLabel('Introduction', { exact: false }).fill('Hello. I am an AI assistant. What would make a deal work for you?')
    await page.getByText('Advanced settings (optional)', { exact: true }).click()
    // This test only verifies the live call path, not cloning, so skip the sample and use the
    // bundled voice.
    await page.getByLabel('Use the bundled voice', { exact: true }).check()
    const created = page.waitForResponse(response => response.url().endsWith('/api/voice/agents') && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Save agent', exact: true }).click()
    agentId = (await (await created).json()).id
    agentId = await page.getByLabel('Conversation agent', { exact: true }).inputValue()
    // Off by default: turning this on is what makes this personal test's transcript get saved.
    await page.getByLabel("Save transcript", { exact: true }).check()
    await page.getByRole('button', { name: 'Start browser conversation', exact: true }).click()
    await speaking
    await page.evaluate(() => window.dispatchEvent(new Event('verification-speech')))
    expect(await interrupted).toBe(firstId)
    const captions = page.getByLabel('Conversation captions')
    await expect(captions.locator('.is-user').filter({ hasText: /deal work for you/i }).first()).toBeVisible({ timeout: 120_000 })
    await expect.poll(async () => captions.locator('.is-agent').count(), { timeout: 60_000 }).toBeGreaterThan(1)
    await replyPlayed
    expect(replies.length).toBeLessThanOrEqual(2)
    expect(replies.join(' ').length).toBeLessThanOrEqual(240)
    await page.getByRole('button', { name: 'End conversation', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Start browser conversation', exact: true })).toBeEnabled()
    expect(errors).toEqual([])
  } finally {
    if (agentId) execFileSync('uv', ['run', 'python', '-c',
      'import sys,uuid; from permetheus.config import Settings; from permetheus.db import make_engine,make_sessionmaker; from permetheus.voice import VoiceAgent; e=make_engine(Settings().database_url); s=make_sessionmaker(e)(); a=s.get(VoiceAgent,uuid.UUID(sys.argv[1])); s.delete(a) if a else None; s.commit(); s.close(); e.dispose()', agentId], { cwd: '..', stdio: 'pipe' })
  }
})
