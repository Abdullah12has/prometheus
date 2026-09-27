import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'

test('every workspace screen loads without browser or API errors', async ({ page }) => {
  const env = readFileSync(new URL('../../.env', import.meta.url), 'utf8')
  const password = env.split('\n').find(line => line.startsWith('ADMIN_PASSWORD='))?.slice('ADMIN_PASSWORD='.length)
  if (!password) throw new Error('Local ADMIN_PASSWORD is required')
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('response', response => { if (response.url().includes('/api/') && response.status() >= 500) errors.push(`${response.status()} ${response.url()}`) })
  await page.goto('/')
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()
  for (const path of ['/', '/companies', '/futures', '/matches', '/outreach', '/voice-notes', '/settings']) {
    await page.goto(path)
    await expect(page.locator('main h1').first()).toBeVisible()
    await expect(page.locator('main')).not.toContainText('Could not reach the workspace API')
    await page.screenshot({ path: `../data/screenshots/workspace-${path.replaceAll('/', '') || 'overview'}.png`, fullPage: true })
  }
  expect(errors).toEqual([])
})
