import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'

function password(): string {
  const values = Object.fromEntries(readFileSync(new URL('../../.env', import.meta.url), 'utf8').split('\n').filter(line => line.includes('=')).map(line => {
    const at = line.indexOf('='); return [line.slice(0, at), line.slice(at + 1)]
  }))
  if (!values.ADMIN_PASSWORD) throw new Error('Configure local ADMIN_PASSWORD before running browser checks')
  return values.ADMIN_PASSWORD
}

test('real login, company intake, edit, attributed intent and refresh', async ({ page }) => {
  const name = `Browser verification ${Date.now()}`
  let companyId: string | undefined
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  try {
    await page.goto('/')
    await page.getByLabel('Password', { exact: true }).fill(password())
    await page.getByRole('button', { name: 'Sign in', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()
    await page.getByRole('link', { name: 'Companies', exact: true }).click()
    await page.getByRole('button', { name: 'Add company', exact: true }).first().click()
    await page.getByLabel('Company name or website').fill(name)
    await page.getByRole('dialog').getByRole('button', { name: 'Add company', exact: true }).click()
    await expect(page.getByRole('heading', { name, exact: true })).toBeVisible()
    companyId = new URL(page.url()).pathname.split('/').pop()
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await page.getByLabel('Description', { exact: true }).fill('Temporary browser verification; deleted after the test.')
    await page.getByRole('button', { name: 'Save changes', exact: true }).click()
    await expect(page.getByText('Temporary browser verification; deleted after the test.', { exact: true })).toBeVisible()
    await page.getByText('Record a statement', { exact: true }).click()
    await page.getByLabel('Speaker name').fill('Test owner')
    await page.getByLabel('Authority', { exact: true }).selectOption('owner')
    await page.getByLabel('Stance', { exact: true }).selectOption('conditional')
    await page.getByLabel('When', { exact: true }).fill('2026-09-27T02:00')
    await page.getByLabel('Exact quote').fill('I would discuss a minority investment if I retain operational control.')
    await page.getByLabel('Confirm attributable owner statement').check()
    await page.getByRole('button', { name: 'Record statement', exact: true }).click()
    await expect(page.getByText('Intent statement recorded', { exact: true })).toBeVisible()
    await page.reload()
    await expect(page.getByRole('heading', { name, exact: true })).toBeVisible()
    await page.getByText('Record a statement', { exact: true }).click()
    await expect(page.getByText('I would discuss a minority investment if I retain operational control.')).toBeVisible()
    await page.screenshot({ path: '../data/screenshots/company.png', fullPage: true })
    await page.getByRole('link', { name: 'Research & explore futures' }).click()
    await expect(page.getByRole('heading', { name: 'Futures', exact: true })).toBeVisible()
    await expect(page.getByLabel('Company', { exact: true })).toHaveValue(companyId!)
    await page.getByRole('button', { name: 'Propose new version' }).click()
    await page.getByLabel('Owner keeps operating control', { exact: true }).check()
    await page.getByRole('button', { name: 'Non-negotiable', exact: true }).click()
    await page.getByRole('button', { name: 'Save proposed version' }).click()
    await page.getByRole('button', { name: 'Confirm this exact version' }).click()
    await page.getByLabel('Who is confirming').fill('Test owner')
    await page.getByLabel('Exact statement confirming these conditions').fill('I confirm I require operating control for any proposed transaction.')
    await page.getByRole('button', { name: 'Confirm version', exact: true }).click()
    await expect(page.getByText('Version 1 · in use for matching', { exact: true })).toBeVisible()
    await page.reload()
    await expect(page.getByText('Version 1 · in use for matching', { exact: true })).toBeVisible()
    expect(errors).toEqual([])
  } finally {
    const session = await (await page.request.get('/api/auth/me')).json()
    if (companyId) await page.request.delete(`/api/companies/${companyId}`, { headers: { 'X-CSRF-Token': session.csrf_token } })
  }
})
