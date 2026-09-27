// UI contract checks; real-backend coverage lives in buyers-live.spec.ts.
import { test, expect, type Page } from '@playwright/test'

const NOW = new Date().toISOString()

function baseBuyer(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: 'buyer-1',
    company_id: null,
    name: 'Nordic Capital Partners',
    website: 'https://nordiccapitalpartners.example',
    country: 'SE',
    kind: 'private_equity',
    status: 'profiled',
    summary: 'Public materials describe acquisitions of profitable Nordic B2B software companies.',
    sectors: ['B2B software', 'Industrial technology'],
    geographies: ['Nordics'],
    preferences: ['Revenue above EUR 5m', 'Founder willing to stay 12+ months'],
    exclusions: ['Pre-revenue companies'],
    research_status: 'completed',
    last_researched_at: NOW,
    source_count: 3,
    history_count: 2,
    ...overrides,
  }
}

function buyerDetail(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    ...baseBuyer(overrides),
    facts: [
      {
        field: 'investment_thesis',
        value: 'Acquires profitable B2B software companies',
        source_id: 'source-1',
        source_url: 'https://nordiccapitalpartners.example/strategy',
        excerpt: 'We acquire profitable, founder-led B2B software companies across the Nordics.',
      },
    ],
    history: [
      {
        id: 'history-1',
        target_name: 'Example Target Oy',
        status: 'completed',
        announced_on: '2024-03-01',
        source_url: 'https://news.example/deal',
        summary: 'Acquired a majority stake in a Finnish software company.',
      },
    ],
    sources: [
      { id: 'source-1', title: 'Nordic Capital Partners — Strategy', url: 'https://nordiccapitalpartners.example/strategy', fetched_at: NOW },
    ],
    gaps: ['No stated minimum EBITDA found in public materials.'],
    research_error: null,
    latest_job: { id: 'job-1', state: 'succeeded', error: null },
    ...overrides,
  }
}

async function mockBuyersApi(page: Page) {
  const buyers = [
    baseBuyer(),
    baseBuyer({
      id: 'buyer-2',
      name: 'Alpine Family Holdings',
      website: 'https://alpinefamilyholdings.example',
      country: 'CH',
      kind: 'family_office',
      status: 'candidate',
      summary: null,
      sectors: [],
      geographies: [],
      preferences: [],
      exclusions: [],
      research_status: 'queued',
      last_researched_at: null,
      source_count: 0,
      history_count: 0,
    }),
  ]

  type MockRun = { id: string; status: string; countries: string[]; source_index: number; source_total: number; found: number; created: number; matched: number; errors: string[]; updated_at: string; job_id: string | null }
  let run: MockRun | null = null
  let pollCount = 0

  await page.route('**/api/auth/me', async (route) => {
    await route.fulfill({ json: { authenticated: true, csrf_token: 'test-csrf-token' } })
  })

  await page.route('**/api/buyers/discovery/sources', async (route) => {
    await route.fulfill({
      json: [
        { id: 'src-se', country: 'SE', label: 'Swedish Companies Registry (Bolagsverket)', url: 'https://bolagsverket.se', coverage: 'Full public register' },
        { id: 'src-ch', country: 'CH', label: 'Swiss Commercial Registry (Zefix)', url: 'https://zefix.ch', coverage: 'Partial — cantons published so far' },
      ],
    })
  })

  await page.route('**/api/buyers/summary', async (route) => {
    await route.fulfill({
      json: {
        total: buyers.length,
        by_country: { SE: 1, CH: 1 },
        by_kind: { private_equity: 1, family_office: 1 },
        research_jobs: { queued: 1, running: 0, succeeded: 1, failed: 0 },
        coverage: 'Partial public-source coverage: 2 of 7 countries have an active source configured.',
      },
    })
  })

  await page.route('**/api/buyers/discovery/runs/run-1/pause', async (route) => {
    if (run) run = { ...run, status: 'paused' }
    await route.fulfill({ json: run })
  })
  await page.route('**/api/buyers/discovery/runs/run-1/resume', async (route) => {
    if (run) run = { ...run, status: 'running' }
    await route.fulfill({ json: run })
  })

  await page.route('**/api/buyers/discovery/runs', async (route) => {
    const request = route.request()
    if (request.method() === 'POST') {
      const body = request.postDataJSON() as { countries: string[] }
      run = { id: 'run-1', status: 'running', countries: body.countries, source_index: 1, source_total: 2, found: 0, created: 0, matched: 0, errors: [], updated_at: NOW, job_id: 'job-run-1' }
      pollCount = 0
      await route.fulfill({ status: 201, json: run })
      return
    }
    if (!run) { await route.fulfill({ json: [] }); return }
    pollCount += 1
    // Second poll after the run starts surfaces progress and a non-fatal source error.
    if (pollCount === 2 && run.status === 'running') {
      run = { ...run, source_index: 2, found: 4, created: 1, matched: 1, errors: ['CH source timed out on page 3'] }
    }
    await route.fulfill({ json: [run] })
  })

  await page.route('**/api/buyers/buyer-1/research', async (route) => {
    await route.fulfill({ json: { job_id: 'job-research-1', state: 'queued' } })
  })
  await page.route('**/api/buyers/buyer-1/mandate', async (route) => {
    await route.fulfill({ status: 201, json: { mandate_id: 'mandate-abc-123' } })
  })

  async function registerDetailRoute(buyer: ReturnType<typeof baseBuyer>) {
    await page.route(`**/api/buyers/${buyer.id}`, async (route) => {
      await route.fulfill({ json: buyerDetail(buyer) })
    })
  }
  await registerDetailRoute(buyers[0])
  await registerDetailRoute(buyers[1])

  await page.route('**/api/buyers*', async (route) => {
    const request = route.request()
    if (request.method() === 'POST') {
      const body = request.postDataJSON() as { name: string; website?: string; country?: string; kind?: string }
      const created = baseBuyer({
        id: 'buyer-new',
        name: body.name,
        website: body.website ?? null,
        country: body.country ?? null,
        kind: body.kind ?? 'unknown',
        status: 'candidate',
        summary: null,
        sectors: [],
        geographies: [],
        preferences: [],
        exclusions: [],
        research_status: 'queued',
        last_researched_at: null,
        source_count: 0,
        history_count: 0,
      })
      buyers.push(created)
      await registerDetailRoute(created)
      await route.fulfill({ status: 201, json: created })
      return
    }
    const url = new URL(request.url())
    const q = (url.searchParams.get('q') ?? '').toLowerCase()
    const country = url.searchParams.get('country') ?? ''
    const kind = url.searchParams.get('kind') ?? ''
    const filtered = buyers.filter((buyer) => {
      const matchesQuery = !q
        || buyer.name.toLowerCase().includes(q)
        || (buyer.summary ?? '').toLowerCase().includes(q)
        || buyer.sectors.some((sector) => sector.toLowerCase().includes(q))
      const matchesCountry = !country || buyer.country === country
      const matchesKind = !kind || buyer.kind === kind
      return matchesQuery && matchesCountry && matchesKind
    })
    await route.fulfill({ json: { items: filtered, total: filtered.length } })
  })
}

test('buyers list search/filter, discovery drawer and profile use only the mocked network contract', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('response', (response) => {
    if (response.url().includes('/api/') && response.status() >= 500) errors.push(`${response.status()} ${response.url()}`)
  })

  await mockBuyersApi(page)
  await page.goto('/buyers')

  await expect(page.getByRole('heading', { name: 'Buyers', exact: true })).toBeVisible()
  await expect(page.locator('.company-table tbody tr')).toHaveCount(2)
  await expect(page.getByRole('link', { name: 'Nordic Capital Partners', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Alpine Family Holdings', exact: true })).toBeVisible()

  // Clicking anywhere in a row (not just the name link) opens its profile.
  await page.locator('.buyer-table__row', { hasText: 'Nordic Capital Partners' }).locator('td').nth(1).click()
  await expect(page).toHaveURL(/\/buyers\/buyer-1$/)
  await expect(page.getByRole('heading', { name: 'Nordic Capital Partners', exact: true })).toBeVisible()
  await page.goBack()
  await expect(page.getByRole('heading', { name: 'Buyers', exact: true })).toBeVisible()

  // Search filters the list and the country/kind filters combine with it.
  await page.getByLabel('Search buyers').fill('nordic')
  await expect(page.locator('.company-table tbody tr')).toHaveCount(1)
  await expect(page.getByRole('link', { name: 'Nordic Capital Partners', exact: true })).toBeVisible()

  await page.getByLabel('Search buyers').fill('')
  await page.getByLabel('Filter buyers by country').selectOption('CH')
  await expect(page.locator('.company-table tbody tr')).toHaveCount(1)
  await expect(page.getByRole('link', { name: 'Alpine Family Holdings', exact: true })).toBeVisible()

  await page.getByLabel('Filter buyers by kind').selectOption('private_equity')
  await expect(page.locator('.company-table tbody tr')).toHaveCount(0)
  await expect(page.getByText('No matches', { exact: true })).toBeVisible()

  // Filters persist across a manual refresh (guarded reload, not reset).
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect(page.getByLabel('Filter buyers by country')).toHaveValue('CH')
  await expect(page.getByLabel('Filter buyers by kind')).toHaveValue('private_equity')

  await page.getByLabel('Filter buyers by kind').selectOption('')
  await page.getByLabel('Filter buyers by country').selectOption('')

  // Add-buyer dialog queues research for a new candidate.
  await page.getByRole('button', { name: 'Add buyer', exact: true }).click()
  await expect(page.getByLabel('Name', { exact: true })).toBeFocused()
  await page.getByLabel('Name', { exact: true }).fill('Test Holding AB')
  await page.getByLabel('Website', { exact: true }).fill('https://holding.example')
  await page.getByLabel('Region', { exact: true }).selectOption('SE')
  await page.getByLabel('Kind', { exact: true }).selectOption('holding_company')
  await page.getByRole('dialog').getByRole('button', { name: 'Add buyer', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Test Holding AB', exact: true })).toBeVisible()

  // Detail profile: source-backed summary, preferences, exclusions, history label, sources, gaps.
  await expect(page.locator('.detail-header__meta')).toContainText('SE')
  await expect(page.locator('.buyer-summary-row .buyer-badge--research-queued')).toHaveText('Queued')

  await page.goto('/buyers/buyer-1')
  await expect(page.getByRole('heading', { name: 'Nordic Capital Partners', exact: true })).toBeVisible()
  await expect(page.getByText('We acquire profitable, founder-led B2B software companies across the Nordics.')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Stated preferences' })).toBeVisible()
  await expect(page.getByText('Revenue above EUR 5m', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Explicit exclusions' })).toBeVisible()
  await expect(page.getByText('Pre-revenue companies', { exact: true })).toBeVisible()
  await expect(page.getByText('Sourced instances only — not a complete list of every acquisition by this buyer.')).toBeVisible()
  await expect(page.getByText('Example Target Oy', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Open research gaps' })).toBeVisible()
  await expect(page.getByText('No stated minimum EBITDA found in public materials.')).toBeVisible()

  // Research again re-queues without claiming a status the mock never returned.
  await page.getByRole('button', { name: 'Research again', exact: true }).click()
  await expect(page.getByText('Research queued', { exact: true })).toBeVisible()

  // Create public strategy links onward to Matches and never claims verification.
  await page.getByRole('button', { name: 'Create public strategy', exact: true }).click()
  await expect(page.getByText('mandate-abc-123', { exact: true })).toBeVisible()
  await expect(page.getByText('not a buyer-confirmed mandate', { exact: false })).toBeVisible()
  const matchesLink = page.getByRole('link', { name: 'Open in Matches', exact: true })
  await expect(matchesLink).toHaveAttribute('href', '/matches?mandate=mandate-abc-123')

  // A candidate with no public data yet shows explicit "Not publicly stated" language.
  await page.goto('/buyers/buyer-2')
  await expect(page.getByRole('heading', { name: 'Alpine Family Holdings', exact: true })).toBeVisible()
  await expect(page.getByText('No public summary recorded yet.', { exact: true })).toBeVisible()
  await expect(page.locator('.panel').filter({ hasText: 'Stated preferences' }).getByText('Not publicly stated.', { exact: true })).toBeVisible()
  await expect(page.locator('.panel').filter({ hasText: 'Explicit exclusions' }).getByText('Not publicly stated.', { exact: true })).toBeVisible()

  // Discovery drawer: choose region, run discovery, then observe progress/errors and pause/resume.
  await page.goto('/buyers')
  await page.getByRole('button', { name: 'Discovery', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Buyer discovery', exact: true })).toBeVisible()
  await expect(page.getByText('No discovery runs yet.', { exact: true })).toBeVisible()
  await page.getByRole('checkbox', { name: 'Sweden' }).check()
  await page.getByRole('checkbox', { name: 'Switzerland' }).check()
  await page.getByRole('button', { name: 'Run discovery', exact: true }).click()
  await expect(page.locator('.buyer-run__status', { hasText: 'running' })).toBeVisible()

  // Background polling advances the run: progress, counts and a non-fatal source error surface.
  await expect(page.getByText('CH source timed out on page 3', { exact: true })).toBeVisible({ timeout: 10_000 })
  await expect(page.getByText('4 found', { exact: false })).toBeVisible()
  await expect(page.getByText('1 added', { exact: false })).toBeVisible()

  await expect(page.getByText('Partial public-source coverage', { exact: false })).toBeVisible()
  await expect(page.getByText('Swedish Companies Registry (Bolagsverket)', { exact: true })).toBeVisible()
  await expect(page.locator('.buyer-summary-grid').getByText('Queued', { exact: true })).toBeVisible()
  await expect(page.locator('.buyer-summary-grid').getByText('Researched', { exact: true })).toBeVisible()

  // Pause and resume the active run.
  await page.getByRole('button', { name: 'Pause', exact: true }).click()
  await expect(page.locator('.buyer-run__status', { hasText: 'paused' })).toBeVisible()
  await page.getByRole('button', { name: 'Resume', exact: true }).click()
  await expect(page.locator('.buyer-run__status', { hasText: 'running' })).toBeVisible()

  // Escape closes the drawer and returns focus to the trigger.
  await page.keyboard.press('Escape')
  await expect(page.getByRole('heading', { name: 'Buyer discovery' })).toHaveCount(0)

  // No horizontal overflow on a mobile viewport for either the list or a profile.
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy()
  await page.goto('/buyers/buyer-1')
  await expect(page.getByRole('heading', { name: 'Nordic Capital Partners', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy()

  expect(errors).toEqual([])
})
