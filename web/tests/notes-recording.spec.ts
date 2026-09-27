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

test('records, uploads, lists and deletes a browser microphone note', async ({ page }) => {
  test.skip(process.env.RUN_NOTES_E2E !== '1', 'Explicit opt-in runs the authenticated local notes flow')
  test.setTimeout(90_000)
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
        const destination = context.createMediaStreamDestination()
        source.connect(destination)
        source.start(context.currentTime + 3)
        source.onended = () => setTimeout(() => void context.close(), 3000)
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
    await page.getByRole('button', { name: 'Record', exact: true }).click()
    noteId = (await (await created).json()).id
    await expect(page.getByRole('button', { name: 'Stop & upload' })).toBeVisible()
    await page.waitForTimeout(10_000)
    await page.getByRole('button', { name: 'Stop & upload' }).click()
    await expect(page.getByText('Recording uploaded. Transcription is queued.')).toBeVisible({ timeout: 20_000 })
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
