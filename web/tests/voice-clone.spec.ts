import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'

// Exercises the actual create-agent -> record/upload -> clone -> preview flow described in
// IMPLEMENTATION_PLAN.md, against the real backend (ffmpeg decode + local TTS clone_voice + TTS
// preview synthesis). Gated like the other voice/notes e2e specs: it needs the real local speech
// stack, not just a mocked network layer.
test.use({ permissions: ['microphone'], launchOptions: { args: ['--autoplay-policy=no-user-gesture-required'] } })

function adminPassword(): string {
  const path = process.env.TEST_ENV_FILE ?? '../.env'
  const line = readFileSync(path, 'utf8').split('\n').find((value) => value.startsWith('ADMIN_PASSWORD='))
  return line?.slice('ADMIN_PASSWORD='.length).trim() ?? ''
}

/** Replaces getUserMedia with a fake microphone that plays `wavBase64` on loop through a
 * MediaStreamDestination — only the device boundary is replaced; capture, resampling, upload and
 * cloning all run for real. */
async function mockMicrophone(page: import('@playwright/test').Page, wavBase64: string) {
  await page.addInitScript(async (wav) => {
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new AudioContext()
      await context.resume()
      const bytes = Uint8Array.from(atob(wav), (c) => c.charCodeAt(0))
      const buffer = await context.decodeAudioData(bytes.buffer)
      const source = context.createBufferSource()
      source.buffer = buffer
      source.loop = true
      const destination = context.createMediaStreamDestination()
      source.connect(destination)
      source.start()
      ;(window as typeof window & { verificationTracks?: MediaStreamTrack[] }).verificationTracks = destination.stream.getTracks()
      return destination.stream
    }
  }, wavBase64)
}

async function deleteAgent(agentId: string) {
  execFileSync('uv', ['run', 'python', '-c',
    'import sys,uuid,shutil; from permetheus.config import Settings; from permetheus.db import make_engine,make_sessionmaker; ' +
    'from permetheus.voice import VoiceAgent; e=make_engine(Settings().database_url); s=make_sessionmaker(e)(); ' +
    'a=s.get(VoiceAgent,uuid.UUID(sys.argv[1])); s.delete(a) if a else None; s.commit(); s.close(); e.dispose(); ' +
    'shutil.rmtree(Settings().data_dir / "voices" / str(uuid.UUID(sys.argv[1])), ignore_errors=True)',
    agentId], { cwd: '..', stdio: 'pipe' })
}

test('records a sample inside agent creation, clones it, and previews the fresh voice', async ({ page }) => {
  test.skip(process.env.RUN_VOICE_E2E !== '1', 'Explicit opt-in runs the real local ffmpeg/TTS clone pipeline')
  test.setTimeout(180_000)
  const password = adminPassword()
  if (!password) throw new Error('Local operator password required')
  const name = `Voice clone verification ${Date.now()}`
  let agentId: string | undefined
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  try {
    await mockMicrophone(page, readFileSync('../data/voice-smoke.wav').toString('base64'))
    await page.goto('/voice-notes')
    await page.getByLabel('Password', { exact: true }).fill(password)
    await page.getByRole('button', { name: 'Sign in', exact: true }).click()

    await page.getByRole('button', { name: 'Create agent', exact: true }).click()
    await page.getByLabel('Name', { exact: true }).fill(name)
    await page.getByLabel('Introduction', { exact: false }).fill('Hello. I am an AI assistant used only for automated verification.')

    // Custom voice source is the default; capture a sample on the (default) Record tab.
    await page.getByRole('button', { name: 'Start recording', exact: true }).click()
    await page.waitForTimeout(4_000)
    await page.getByRole('button', { name: 'Stop', exact: true }).click()
    await expect(page.getByLabel('Recorded sample playback', { exact: true })).toBeVisible()

    const created = page.waitForResponse((response) => response.url().endsWith('/api/voice/agents') && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Save agent', exact: true }).click()
    agentId = (await (await created).json()).id
    expect(agentId).toBeTruthy()

    // Success is only reported once cloning actually succeeds (not right after agent creation).
    await expect(page.getByText('Cloned voice', { exact: true })).toBeVisible({ timeout: 120_000 })
    await expect(page.getByRole('button', { name: 'New conversation agent', exact: false })).toHaveCount(0)

    // The preview <audio> must be keyed to the clone that just happened, not a stale/bundled one.
    const previewAudio = page.getByLabel('Preview agent introduction', { exact: true })
    const src = await previewAudio.getAttribute('src')
    expect(src).toContain(`/api/voice/agents/${agentId}/preview`)
    expect(src).toMatch(/[?&]v=/)
    const preview = await page.request.get(src!)
    expect(preview.ok()).toBeTruthy()
    expect(preview.headers()['content-type']).toBe('audio/wav')
    expect((await preview.body()).byteLength).toBeGreaterThan(10_000)
    await page.reload()
    await page.getByLabel('Conversation agent', { exact: true }).selectOption(agentId!)
    await expect(page.getByText('Cloned voice', { exact: true })).toBeVisible()

    expect(errors).toEqual([])
  } finally {
    if (agentId) deleteAgent(agentId)
  }
})

test('cancellation and recorder startup failure release the sample microphone', async ({ page }) => {
  test.skip(process.env.RUN_VOICE_E2E !== '1', 'Uses the local microphone fixture')
  await mockMicrophone(page, readFileSync('../data/voice-smoke.wav').toString('base64'))
  expect((await page.request.post('/api/auth/login', { data: { password: adminPassword() } })).ok()).toBeTruthy()
  await page.goto('/voice-notes')
  await page.getByRole('button', { name: 'Create agent', exact: true }).click()
  await page.getByRole('button', { name: 'Start recording', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  expect(await page.evaluate(() => (window as typeof window & { verificationTracks?: MediaStreamTrack[] }).verificationTracks?.every(track => track.readyState === 'ended'))).toBeTruthy()
  await expect(page.getByRole('button', { name: 'Create agent', exact: true })).toBeVisible()
  await page.evaluate(() => { MediaRecorder.prototype.start = () => { throw new Error('Recorder startup failed') } })
  await page.getByRole('button', { name: 'Create agent', exact: true }).click()
  await page.getByRole('button', { name: 'Start recording', exact: true }).click()
  await expect(page.locator('.voice-capture [role="alert"]')).toBeVisible()
  expect(await page.evaluate(() => (window as typeof window & { verificationTracks?: MediaStreamTrack[] }).verificationTracks?.every(track => track.readyState === 'ended'))).toBeTruthy()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
})

test('keeps an invalid sample selected after a failed clone and retries the same agent without duplicating it', async ({ page }) => {
  test.skip(process.env.RUN_VOICE_E2E !== '1', 'Explicit opt-in runs the real local ffmpeg decode path')
  test.setTimeout(120_000)
  const password = adminPassword()
  if (!password) throw new Error('Local operator password required')
  const name = `Voice clone retry verification ${Date.now()}`
  let agentId: string | undefined
  const tempDir = mkdtempSync(join(tmpdir(), 'voice-clone-test-'))
  const badFile = join(tempDir, 'not-audio.wav')
  writeFileSync(badFile, 'this is not a real audio file, ffmpeg must reject it'.repeat(20))
  try {
    await page.goto('/voice-notes')
    await page.getByLabel('Password', { exact: true }).fill(password)
    await page.getByRole('button', { name: 'Sign in', exact: true }).click()

    await page.getByRole('button', { name: 'Create agent', exact: true }).click()
    await page.getByLabel('Name', { exact: true }).fill(name)
    await page.getByLabel('Introduction', { exact: false }).fill('Hello. I am an AI assistant used only for automated verification.')

    await page.getByRole('tab', { name: 'Upload file', exact: true }).click()
    await page.getByLabel('Choose a voice sample audio file', { exact: true }).setInputFiles(badFile)
    await expect(page.getByText('not-audio.wav')).toBeVisible()

    const created = page.waitForResponse((response) => response.url().endsWith('/api/voice/agents') && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Save agent', exact: true }).click()
    agentId = (await (await created).json()).id
    expect(agentId).toBeTruthy()

    // Useful, specific error — and the sample stays selected instead of being discarded.
    await expect(page.getByRole('alert').filter({ hasText: /not decodable audio/i })).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('not-audio.wav')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Retry cloning with the same sample', exact: true })).toBeVisible()

    // The agent from the failed attempt still exists exactly once, with the bundled (untouched) voice.
    const afterFailure = await (await page.request.get('/api/voice/agents')).json() as { id: string; name: string; voice: { kind: string } }[]
    const matches = afterFailure.filter((a) => a.name === name)
    expect(matches).toHaveLength(1)
    expect(matches[0].voice.kind).toBe('default')

    // Swap in a decodable sample and save again: it must PATCH the same agent, not create another.
    await page.getByRole('button', { name: 'Remove', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Save agent', exact: true })).toBeDisabled()
    const goodFile = join(tempDir, 'sample.wav')
    writeFileSync(goodFile, readFileSync('../data/voice-smoke.wav'))
    await page.getByLabel('Choose a voice sample audio file', { exact: true }).setInputFiles(goodFile)
    await page.getByRole('button', { name: 'Save agent', exact: true }).click()
    await expect(page.getByText('Cloned voice', { exact: true })).toBeVisible({ timeout: 120_000 })

    const afterRetry = await (await page.request.get('/api/voice/agents')).json() as { id: string; name: string; voice: { kind: string } }[]
    const retryMatches = afterRetry.filter((a) => a.name === name)
    expect(retryMatches).toHaveLength(1)
    expect(retryMatches[0].id).toBe(agentId)
    expect(retryMatches[0].voice.kind).toBe('cloned')
  } finally {
    rmSync(tempDir, { recursive: true, force: true })
    if (agentId) deleteAgent(agentId)
  }
})
