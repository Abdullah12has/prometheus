import { test, expect, type APIRequestContext } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'

function password(): string {
  const envFile = process.env.TEST_ENV_FILE ?? new URL('../../.env', import.meta.url)
  const values = Object.fromEntries(readFileSync(envFile, 'utf8').split('\n').filter(line => line.includes('=')).map(line => {
    const at = line.indexOf('='); return [line.slice(0, at), line.slice(at + 1)]
  }))
  if (!values.ADMIN_PASSWORD) throw new Error('Configure local ADMIN_PASSWORD before running browser checks')
  return values.ADMIN_PASSWORD
}

async function create<T>(request: APIRequestContext, path: string, token: string, body: unknown): Promise<T> {
  const response = await request.post(path, { headers: { 'X-CSRF-Token': token }, data: body })
  const payload = await response.json()
  if (!response.ok()) throw new Error(`Setup request ${path} failed (${response.status()}): ${JSON.stringify(payload)}`)
  return payload as T
}

async function cancelCompanyEnrichment(request: APIRequestContext, companyId: string, token: string): Promise<void> {
  const response = await request.get(`/api/jobs?company_id=${companyId}&state=queued`)
  if (!response.ok()) throw new Error(`Could not inspect queued work for test company (${response.status()})`)
  const jobs = await response.json() as { id: string; kind: string }[]
  for (const job of jobs.filter(item => item.kind === 'company.enrich')) {
    const cancelled = await request.post(`/api/jobs/${job.id}/cancel`, { headers: { 'X-CSRF-Token': token } })
    if (!cancelled.ok()) throw new Error(`Could not cancel temporary company enrichment (${cancelled.status()})`)
  }
}

test('matches can create a source-backed proposal and an unapproved email draft without sending', async ({ page }) => {
  const suffix = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
  const sellerName = `Browser seller ${suffix}`
  const buyerName = `Browser buyer ${suffix}`
  const ids: { seller?: string; buyer?: string; contact?: string } = {}
  const cleanup: { sources: string[]; mandate?: string; draft?: string } = { sources: [] }
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))

  try {
    await page.goto('/')
    await page.getByLabel('Password', { exact: true }).fill(password())
    await page.getByRole('button', { name: 'Sign in', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()

    const sessionResponse = await page.request.get('/api/auth/me')
    expect(sessionResponse.ok()).toBeTruthy()
    const session = await sessionResponse.json() as { csrf_token: string }
    const source = await create<{ id: string }>(page.request, '/api/sources', session.csrf_token, {
      kind: 'registry', title: `Temporary browser workflow identity source ${suffix}`,
    })
    cleanup.sources.push(source.id)
    const seller = await create<{ company: { id: string } }>(page.request, '/api/companies', session.csrf_token, {
      name: sellerName, country: 'FI', industry: 'Software', allow_new: true,
    })
    ids.seller = seller.company.id
    await cancelCompanyEnrichment(page.request, ids.seller, session.csrf_token)
    const buyer = await create<{ company: { id: string } }>(page.request, '/api/companies', session.csrf_token, {
      name: buyerName, country: 'SE', industry: 'Investment', allow_new: true,
    })
    ids.buyer = buyer.company.id
    await cancelCompanyEnrichment(page.request, ids.buyer, session.csrf_token)
    const contact = await create<{ id: string }>(page.request, `/api/companies/${ids.buyer}/contacts`, session.csrf_token, {
      name: `Buyer contact ${suffix}`, title: 'Acquisitions', person_role: 'employee', contact_role: 'buyer',
      email: `browser-${suffix}@example.invalid`, source_id: source.id,
    })
    ids.contact = contact.id
    const verified = await page.request.post(`/api/contacts/${contact.id}/verify`, {
      headers: { 'X-CSRF-Token': session.csrf_token },
      data: { basis: 'Temporary test setup: synthetic contact identity used only in the local test database.', source_id: source.id },
    })
    expect(verified.ok()).toBeTruthy()
    const mandate = await create<{ id: string }>(page.request, '/api/mandates', session.csrf_token, {
      buyer_name: buyerName, buyer_company_id: ids.buyer, criteria: {}, evidence_level: 'buyer_confirmed',
      source_id: source.id, identity_verified: true, confirmed_by: `Buyer contact ${suffix}`,
      last_confirmed_at: new Date(Date.now() - 60_000).toISOString(),
      expires_at: new Date(Date.now() + 30 * 24 * 60 * 60 * 1000).toISOString(),
    })
    cleanup.mandate = mandate.id

    await page.getByRole('link', { name: 'Future simulations', exact: true }).click()
    await page.getByText('Advanced tools', { exact: true }).click()
    await page.getByRole('button', { name: 'Match runs', exact: true }).click()
    await page.getByLabel('Company', { exact: true }).selectOption(ids.seller)
    await page.getByRole('button', { name: 'Run match against active mandates', exact: true }).click()
    await expect(page.getByText('Match run completed', { exact: true })).toBeVisible()
    await expect(page.getByText('Results — this company')).toBeVisible()
    const opportunitiesResponse = await page.request.get(`/api/opportunities?company_id=${ids.seller}`)
    expect(opportunitiesResponse.ok()).toBeTruthy()
    const opportunities = await opportunitiesResponse.json() as { mandate_id: string; buyer_name: string; status: string }[]
    expect(opportunities).toContainEqual(expect.objectContaining({ mandate_id: mandate.id, buyer_name: buyerName, status: 'research_needed' }))

    await page.getByRole('button', { name: 'Opportunities', exact: true }).click()
    await page.getByLabel('Company (optional filter)').selectOption(ids.seller)
    const opportunity = page.locator('.match-card').filter({ hasText: buyerName }).first()
    await expect(opportunity).toBeVisible()
    await opportunity.getByRole('button', { name: 'Outcomes & drafts' }).click()
    await opportunity.getByRole('button', { name: 'Create draft brief' }).click()
    await page.getByLabel('Authorized by').fill(`Test owner ${suffix}`)
    await page.getByLabel('Company identity (name)').check()
    await page.getByLabel('Country', { exact: true }).uncheck()
    await page.getByLabel('Industry', { exact: true }).uncheck()
    const exactAuthorization = `Test owner ${suffix} explicitly authorized disclosure of the seller company name to ${buyerName}.`
    await page.getByLabel('Authorization statement (exact wording)').fill(exactAuthorization)
    await page.getByLabel('Authorization source description').fill(`Owner authorization recorded for browser test ${suffix}`)
    const proposalResponsePromise = page.waitForResponse(response =>
      response.url().includes(`/api/opportunities/`) && response.url().endsWith('/drafts') && response.request().method() === 'POST',
    )
    const ownerSourceResponsePromise = page.waitForResponse(response =>
      response.url().endsWith('/api/sources') && response.request().method() === 'POST',
    )
    await page.locator('form').filter({ has: page.getByLabel('Authorization statement (exact wording)') })
      .getByRole('button', { name: 'Create draft brief', exact: true }).click()
    const ownerSourceResponse = await ownerSourceResponsePromise
    if (ownerSourceResponse.ok()) {
      const ownerSource = await ownerSourceResponse.json() as { id: string }
      cleanup.sources.push(ownerSource.id)
    }
    const proposalResponse = await proposalResponsePromise
    expect(proposalResponse.status()).toBe(201)
    const proposal = await proposalResponse.json() as { authorization: { statement: string; scope: string[]; source_id: string }; payload: { company: { name: string } } }
    expect(proposal.authorization.statement).toBe(exactAuthorization)
    expect(proposal.authorization.scope).toEqual(['company_identity'])
    expect(proposal.authorization.source_id).toBeTruthy()
    expect(proposal.payload.company.name).toBe(sellerName)

    await expect(opportunity.getByRole('heading', { name: 'Authorized proposal' })).toBeVisible()
    await expect(opportunity.getByText(sellerName, { exact: true })).toBeVisible()
    await page.getByLabel('Verified buyer contact').selectOption(ids.contact!)
    const emailDraftResponsePromise = page.waitForResponse(response =>
      response.url().includes('/email-draft') && response.request().method() === 'POST',
    )
    await page.getByRole('button', { name: 'Create email draft', exact: true }).click()
    const emailDraftResponse = await emailDraftResponsePromise
    expect(emailDraftResponse.status()).toBe(201)
    const emailDraft = await emailDraftResponse.json() as { mail_draft_id: string; status: string; recipients: string[]; subject: string; body: string }
    expect(emailDraft.status).toBe('draft')
    expect(emailDraft.recipients).toEqual([`browser-${suffix}@example.invalid`])
    expect(emailDraft.subject).toContain('Confidential opportunity')
    cleanup.draft = emailDraft.mail_draft_id
    await expect(opportunity.getByRole('heading', { name: 'Email draft · awaiting review' })).toBeVisible()
    await expect(opportunity.getByText('Unapproved draft. Nothing has been sent.', { exact: true })).toBeVisible()
    await expect(opportunity.getByRole('link', { name: 'Open in Outreach' })).toHaveAttribute('href', '/outreach')
    expect(errors).toEqual([])

    // Verify the endpoint produced only a draft. The UI offers no approve/send action.
    expect(mandate.id).toBeTruthy()
  } finally {
    const sessionResponse = await page.request.get('/api/auth/me')
    if (sessionResponse.ok()) {
      const session = await sessionResponse.json() as { csrf_token: string }
      const leftovers: string[] = []
      for (const companyId of [ids.seller, ids.buyer]) {
        if (!companyId) continue
        const response = await page.request.delete(`/api/companies/${companyId}`, { headers: { 'X-CSRF-Token': session.csrf_token } })
        if (!response.ok()) leftovers.push(`company ${companyId} (delete failed with ${response.status()})`)
      }
      if (ids.contact) {
        const response = await page.request.delete(`/api/contacts/${ids.contact}`, { headers: { 'X-CSRF-Token': session.csrf_token } })
        if (!response.ok() && response.status() !== 404) leftovers.push(`contact ${ids.contact} (delete failed with ${response.status()})`)
      }
      execFileSync('uv', ['run', 'python', '-c', `
import sys,json,uuid
from permetheus.config import Settings
from permetheus.db import make_engine,make_sessionmaker
from permetheus.mail import OutreachDraft
from permetheus.deals import BuyerMandate
from permetheus.models import Source
payload=json.loads(sys.argv[1]);engine=make_engine(Settings().database_url)
with make_sessionmaker(engine)() as db:
 for cls,keys in [(OutreachDraft,[payload.get('draft')]),(BuyerMandate,[payload.get('mandate')]),(Source,payload['sources'])]:
  for key in filter(None,keys):
   row=db.get(cls,uuid.UUID(key))
   if row:db.delete(row)
  db.commit()
engine.dispose()
`, JSON.stringify(cleanup)], { cwd: '..', stdio: 'pipe' })
      expect(leftovers, 'Temporary companies should be deleted').toEqual([])
    }
  }
})
