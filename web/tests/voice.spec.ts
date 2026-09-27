import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'

test.use({ permissions: ['microphone'], launchOptions: { args: ['--autoplay-policy=no-user-gesture-required'] } })

test('browser audio processing, local ASR, model response and audio playback', async ({ page }) => {
  test.skip(process.env.RUN_VOICE_E2E !== '1', 'Explicit opt-in runs the real local models and configured LLM')
  test.setTimeout(180_000)
  const secret = readFileSync('../.env', 'utf8').split('\n').find(line => line.startsWith('ADMIN_PASSWORD='))?.slice('ADMIN_PASSWORD='.length)
  if (!secret) throw new Error('Local operator password required')
  let agentId: string | undefined
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
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
      source.start(context.currentTime + 3)
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
    await page.getByLabel('Additional instructions').fill('Reply in one short sentence.')
    const created = page.waitForResponse(response => response.url().endsWith('/api/voice/agents') && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Save agent', exact: true }).click()
    agentId = (await (await created).json()).id
    agentId = await page.getByLabel('Conversation agent', { exact: true }).inputValue()
    await page.getByLabel('The participant has agreed to this browser conversation and transcript recording.').check()
    await page.getByRole('button', { name: 'Start browser conversation', exact: true }).click()
    const captions = page.getByLabel('Conversation captions')
    await expect(captions.locator('.is-user').filter({ hasText: /deal work for you/i }).first()).toBeVisible({ timeout: 120_000 })
    await expect.poll(async () => captions.locator('.is-agent').count(), { timeout: 60_000 }).toBeGreaterThan(1)
    await page.getByRole('button', { name: 'End conversation', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Start browser conversation', exact: true })).toBeEnabled()
    expect(errors).toEqual([])
  } finally {
    if (agentId) execFileSync('uv', ['run', 'python', '-c',
      'import sys,uuid; from permetheus.config import Settings; from permetheus.db import make_engine,make_sessionmaker; from permetheus.voice import VoiceAgent; e=make_engine(Settings().database_url); s=make_sessionmaker(e)(); a=s.get(VoiceAgent,uuid.UUID(sys.argv[1])); s.delete(a) if a else None; s.commit(); s.close(); e.dispose()', agentId], { cwd: '..', stdio: 'pipe' })
  }
})
