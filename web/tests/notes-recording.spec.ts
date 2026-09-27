import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const audioFixture = resolve(process.env.NOTE_TEST_AUDIO ?? '../data/asr-smoke.wav')

test.use({ permissions: ['microphone'], launchOptions: { args: ['--autoplay-policy=no-user-gesture-required'] } })

function adminPassword(): string {
  const path = process.env.TEST_ENV_FILE ?? resolve('../.env')
  const line = readFileSync(path, 'utf8').split('\n').find((value) => value.startsWith('ADMIN_PASSWORD='))
  return line?.slice('ADMIN_PASSWORD='.length).trim() ?? ''
}

test('keeps recording across workspace tabs and navigation, then transcribes the complete note', async ({ page }) => {
  test.skip(process.env.RUN_NOTES_E2E !== '1', 'Explicit opt-in runs the authenticated local notes flow')
  test.setTimeout(150_000)
  const password = adminPassword()
  if (!password) throw new Error('Local operator password required')

  let noteId: string | undefined
  let csrfToken = ''
  try {
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
        source.start(context.currentTime + 3)
        return destination.stream
      }
    }, readFileSync(audioFixture).toString('base64'))
    await page.goto('/voice-notes?tab=notes')
    const login = page.waitForResponse((response) => response.url().endsWith('/api/auth/login'))
    await page.getByLabel('Password', { exact: true }).fill(password)
    await page.getByRole('button', { name: 'Sign in', exact: true }).click()
    const loginBody = await (await login).json() as { csrf_token?: string }
    csrfToken = loginBody.csrf_token ?? ''
    const title = `Browser recording verification ${Date.now()}`
    await page.getByLabel('Title', { exact: true }).fill(title)
    const created = page.waitForResponse((response) => response.url().endsWith('/api/notes') && response.request().method() === 'POST')
    const firstChunk = page.waitForResponse((response) => /\/api\/notes\/[^/]+\/chunks\/0$/.test(response.url()) && response.ok())
    await page.getByRole('button', { name: 'Record', exact: true }).click()
    noteId = (await (await created).json()).id
    await expect(page.getByRole('button', { name: 'Stop & upload' })).toBeVisible()
    await firstChunk
    await page.getByRole('tab', { name: 'Voice agents', exact: true }).click()
    // Sequence 1 can be the final flush from an aborted recorder. Sequence 2
    // proves capture actually continues while the Notes tab is no longer open.
    await page.waitForResponse((response) => response.url().endsWith(`/api/notes/${noteId}/chunks/2`) && response.ok(), { timeout: 12_000 })
    const recording = page.getByRole('region', { name: 'Active note recording' })
    await expect(recording).toContainText(title)
    await page.getByRole('link', { name: 'Companies', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Companies', exact: true })).toBeVisible()
    await page.waitForResponse((response) => response.url().endsWith(`/api/notes/${noteId}/chunks/3`) && response.ok(), { timeout: 8_000 })
    await page.goBack()
    await page.getByRole('tab', { name: 'Notes', exact: true }).click()
    await expect(page.getByLabel('Title', { exact: true })).toHaveValue(title)
    await expect(page.getByRole('button', { name: 'Stop & upload' })).toBeVisible()
    await expect(page.getByRole('region', { name: 'Interrupted recordings' }).filter({ hasText: title })).toHaveCount(0)
    await page.getByRole('link', { name: 'Companies', exact: true }).click()
    await page.screenshot({ path: '../data/screenshots/notes-recording-navigation.png' })
    await recording.getByRole('button', { name: 'Stop & upload' }).click()
    await expect(page.getByText('Recording uploaded. Transcription is queued.')).toBeVisible({ timeout: 20_000 })
    await expect(recording).toHaveCount(0)
    await page.getByRole('link', { name: 'Voice & notes', exact: true }).click()
    await page.getByRole('tab', { name: 'Notes', exact: true }).click()
    const row = page.locator('.notes-list__header').filter({ hasText: title })
    await expect(row).toBeVisible()
    await row.click()
    await expect(row).toContainText('Ready', { timeout: 120_000 })
    await expect(page.locator('.notes-detail__raw')).toHaveText(/\S+/, { timeout: 10_000 })
  } finally {
    if (noteId && csrfToken) {
      const status = await page.evaluate(async ({ id, token }) => {
        const response = await fetch(`/api/notes/${id}`, { method: 'DELETE', credentials: 'include', headers: { 'X-CSRF-Token': token } })
        return response.status
      }, { id: noteId, token: csrfToken })
      expect([204, 404]).toContain(status)
    }
  }
})
