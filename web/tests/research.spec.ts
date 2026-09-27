import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'

function password(): string {
  const envFile = process.env.TEST_ENV_FILE ?? new URL('../../.env', import.meta.url)
  const values = Object.fromEntries(readFileSync(envFile, 'utf8').split('\n').filter(line => line.includes('=')).map(line => {
    const at = line.indexOf('='); return [line.slice(0, at), line.slice(at + 1)]
  }))
  if (!values.ADMIN_PASSWORD) throw new Error('Configure local ADMIN_PASSWORD before running browser checks')
  return values.ADMIN_PASSWORD
}

type Company = { id: string; name: string }
type CompanyList = { items: Company[]; total: number }
type DiscoveryRun = { id: string; status: string; params: { name?: string | null; max_pages?: number; max_companies?: number }; companies_imported: number }

test('bounded registry discovery reuses an existing company and coverage shows missing financials', async ({ page }) => {
  test.skip(process.env.RUN_RESEARCH_E2E !== '1', 'Set RUN_RESEARCH_E2E=1 to run the bounded public-registry acceptance check.')
  test.setTimeout(180_000)

  const targetName = 'Reformo Networks'
  let discoveryRunId: string | undefined
  const pageErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))

  await page.goto('/')
  await page.getByLabel('Password', { exact: true }).fill(password())
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()

  const beforeResponse = await page.request.get(`/api/companies?q=${encodeURIComponent(targetName)}&limit=200`)
  expect(beforeResponse.ok()).toBeTruthy()
  const before = await beforeResponse.json() as CompanyList
  const existing = before.items.filter(company => company.name.trim().toLocaleLowerCase().startsWith(targetName.toLocaleLowerCase()))
  expect(existing, 'the named company must already exist; this test never creates it').toHaveLength(1)
  const companyId = existing[0].id
  const existingName = existing[0].name

  await page.getByRole('link', { name: 'Companies', exact: true }).click()
  await page.getByText('Discover and research companies', { exact: true }).click()
  await page.getByText('Advanced targeted search', { exact: true }).click()
  await page.getByLabel('Company name', { exact: true }).fill(targetName)
  await page.locator('.discovery-form__limits select').selectOption('1')
  await page.locator('.discovery-form__limits input[type="number"]').fill('1')
  const startedResponsePromise = page.waitForResponse(response =>
    response.url().endsWith('/api/discovery/runs') && response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: 'Start targeted search', exact: true }).click()
  const startedResponse = await startedResponsePromise
  expect(startedResponse.status()).toBe(202)
  const started = await startedResponse.json() as DiscoveryRun
  discoveryRunId = started.id
  expect(started.params).toMatchObject({ name: targetName, max_pages: 1, max_companies: 1 })

  await expect.poll(async () => {
    const response = await page.request.get(`/api/discovery/runs/${started.id}`)
    if (!response.ok()) return `http-${response.status()}`
    return (await response.json() as DiscoveryRun).status
  }, { timeout: 150_000, intervals: [1000, 2000, 3000] }).toBe('completed')

  const afterResponse = await page.request.get(`/api/companies?q=${encodeURIComponent(targetName)}&limit=200`)
  expect(afterResponse.ok()).toBeTruthy()
  const after = await afterResponse.json() as CompanyList
  const exactAfter = after.items.filter(company => company.name.trim().toLocaleLowerCase().startsWith(targetName.toLocaleLowerCase()))
  expect(exactAfter).toHaveLength(1)
  expect(exactAfter[0].id).toBe(companyId)
  expect(after.total).toBe(before.total)

  await page.goto(`/companies/${companyId}`)
  await expect(page.getByRole('heading', { name: existingName, exact: true })).toBeVisible()
  const required = page.getByRole('region', { name: 'Required information coverage' })
  await expect(required).toBeVisible({ timeout: 20_000 })
  for (const metric of ['Revenue', 'EBITDA', 'Employees']) {
    const row = required.locator('li').filter({ hasText: metric })
    await expect(row).toBeVisible()
    await expect(row.getByText('Missing', { exact: true })).toBeVisible()
  }
  expect(pageErrors).toEqual([])
  test.info().annotations.push({ type: 'discovery-run', description: `${discoveryRunId} (completed bounded registry search)` })
})
