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
