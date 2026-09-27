import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'

test('company enrichment starts from the UI and displays real progress and saved results', async ({ page }) => {
  test.skip(process.env.RUN_ENRICHMENT_E2E !== '1', 'Uses the real imported company, websites and configured model')
  test.setTimeout(360_000)
  const password = readFileSync(new URL('../../.env', import.meta.url), 'utf8').split('\n')
    .find(line => line.startsWith('ADMIN_PASSWORD='))?.slice('ADMIN_PASSWORD='.length)
  if (!password) throw new Error('Local ADMIN_PASSWORD is required')
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()
  const targetName = process.env.ENRICHMENT_COMPANY ?? 'Vincit Oyj'
  const companies = await (await page.request.get(`/api/companies?q=${encodeURIComponent(targetName)}&limit=20`)).json()
  const company = companies.items.find((item: { name: string }) => item.name === targetName)
  expect(company, 'requires the real Finnish register import').toBeTruthy()
  await page.goto(`/companies/${company.id}`)
  await expect(page.getByRole('button', { name: 'Refresh research', exact: true })).toBeEnabled({ timeout: 90_000 })
  const queued = page.waitForResponse(response => response.url().endsWith(`/companies/${company.id}/enrichments`) && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Refresh research', exact: true }).click()
  const response = await queued
  expect(response.ok()).toBeTruthy()
  const jobId = (await response.json()).id
  const activity = page.getByRole('region', { name: 'Enrichment activity', exact: true })
  await expect(activity).toBeVisible()
  let capturedRunning = false
  let finalJob: { state: string; progress?: { phase: string; source_count: number; events: { phase: string; detail: string }[] } } | undefined
  await expect.poll(async () => {
    const jobs = await (await page.request.get(`/api/jobs?company_id=${company.id}`)).json()
    finalJob = jobs.find((job: { id: string }) => job.id === jobId)
    if (!capturedRunning && finalJob?.state === 'running' && await activity.locator('[aria-current="step"]').count()) {
      await activity.scrollIntoViewIfNeeded()
      await page.screenshot({ path: '../data/screenshots/enrichment-live.png', fullPage: true })
      capturedRunning = true
    }
    return finalJob?.state
  }, { timeout: 300_000, intervals: [250, 500, 1000] }).toBe('succeeded')
  expect(finalJob?.progress?.phase).toBe('completed')
  expect(finalJob?.progress?.source_count).toBeGreaterThan(0)
  const phases = finalJob!.progress!.events.map(event => event.phase)
  expect(phases).toEqual(expect.arrayContaining(['registry', 'website', 'search', 'extraction', 'saving', 'completed']))
  await expect(activity.getByText('Research complete', { exact: true })).toBeVisible({ timeout: 10_000 })
  await expect(activity.getByRole('list', { name: 'Run activity' })).toBeVisible()
  await expect(page.getByRole('heading', { name: company.name, exact: true })).toBeVisible()
  const detail = await (await page.request.get(`/api/companies/${company.id}`)).json()
  expect(detail.evidence.length).toBeGreaterThan(0)
  if (process.env.REQUIRE_WORKFORCE === '1') {
    expect(detail.evidence.some((item: { field: string }) => item.field === 'employee_count_text')).toBeTruthy()
    await expect(page.getByRole('region', { name: 'Workforce information', exact: true })).toBeVisible()
  }
  if (process.env.REQUIRE_FINANCIAL_SUMMARY === '1') {
    expect(detail.evidence.some((item: { field: string }) => item.field === 'financial_summary_text')).toBeTruthy()
    await page.getByRole('button', { name: 'Financials', exact: true }).click()
    const summaries = page.getByRole('region', { name: 'Public financial summaries', exact: true })
    await expect(summaries).toBeVisible()
    await expect(summaries.getByRole('link').first()).toBeVisible()
    const rejectedToggle = page.getByLabel('Show rejected observations', { exact: true })
    if (await rejectedToggle.count()) {
      await expect(page.locator('.record-list__item').filter({ hasText: /· rejected/ })).toHaveCount(0)
      await rejectedToggle.check()
      await expect(page.locator('.record-list__item').filter({ hasText: /· rejected/ }).first()).toBeVisible()
      await rejectedToggle.uncheck()
    }
    await page.screenshot({ path: '../data/screenshots/enrichment-financials.png', fullPage: true })
    await page.getByRole('button', { name: 'Overview', exact: true }).click()
  }
  await activity.scrollIntoViewIfNeeded()
  await page.screenshot({ path: '../data/screenshots/enrichment-complete.png', fullPage: true })
  await page.reload()
  await expect(page.getByRole('region', { name: 'Enrichment activity', exact: true }).getByText('Research complete', { exact: true })).toBeVisible()
  expect(errors).toEqual([])
  test.info().annotations.push({ type: 'real-enrichment-job', description: `${jobId}; active UI captured: ${capturedRunning}` })
})
