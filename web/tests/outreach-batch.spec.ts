import { test, expect } from '@playwright/test'
import type { OutreachDraft } from '../src/lib/mailTypes'

test('batch sends only reviewed versions and reports partial failures without retrying', async ({ page }) => {
  const drafts: OutreachDraft[] = ['First', 'Second', 'Third'].map((name, index) => ({
    id: `draft-${index}`, company_id: 'company-1', contact_id: 'contact-1', conversation_id: null,
    enrollment_id: null, kind: 'missing_fields', recipients: [`owner${index}@example.com`],
    subject: `${name} data request`, body: `Please share ${name.toLowerCase()} company details.`,
    disclosure: {}, version: 1, status: 'draft', content_hash: String(index).repeat(64),
    approval: null, dispatch: null, created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  }))
  const calls: { path: string; body: unknown }[] = []
  // Intercept every API request: this test cannot reach a real mailbox or alter workspace data.
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/auth/me') return route.fulfill({ json: { authenticated: true, csrf_token: 'test-csrf' } })
    if (path === '/api/companies') return route.fulfill({ json: [{ id: 'company-1', name: 'Test Company' }] })
    if (path === '/api/gmail/status') return route.fulfill({ json: { configured: true, connected: true, email: 'sender@example.com', scopes: [], required_scopes: [], needs_reauth: false } })
    if (path === '/api/outreach/drafts') return route.fulfill({ json: drafts })
    if (request.method() === 'POST') {
      calls.push({ path, body: request.postDataJSON() })
      if (path === '/api/outreach/drafts/draft-1/approve') return route.fulfill({ status: 409, json: { error: { code: 'stale_preview', message: 'Draft changed after review' } } })
      if (path === '/api/outreach/drafts/draft-2/send') return route.fulfill({ status: 502, json: { error: { code: 'delivery_unknown', message: 'Delivery unknown; reconcile before retry' } } })
      return route.fulfill({ json: {} })
    }
    return route.fulfill({ json: [] })
  })
  await page.goto('/outreach')
  await page.getByRole('button', { name: 'Drafts', exact: true }).click()
  await page.getByRole('button', { name: 'Select all reviewable emails' }).click()
  await page.getByRole('button', { name: 'Review selected (3)' }).click()
  const review = page.getByRole('region', { name: 'Review email batch' })
  const send = review.getByRole('button', { name: 'Approve and send 3 emails' })
  await expect(send).toBeDisabled()
  for (const draft of drafts) {
    await expect(review.getByText(draft.body, { exact: true })).toBeVisible()
    await review.getByRole('checkbox', { name: `I reviewed this email to ${draft.recipients.join(', ')}: ${draft.subject}` }).check()
  }
  expect(calls).toEqual([])
  await expect(send).toBeEnabled()
  await send.click()
  await expect(review.getByRole('button', { name: 'Batch finished' })).toBeDisabled()
  await expect(review.getByText('Sent', { exact: true })).toBeVisible()
  await expect(review.getByText('Draft changed after review', { exact: true })).toBeVisible()
  await expect(review.getByText('Delivery unknown; reconcile before retry', { exact: true })).toBeVisible()
  expect(calls).toEqual([
    { path: '/api/outreach/drafts/draft-0/approve', body: { version: 1, content_hash: '0'.repeat(64) } },
    { path: '/api/outreach/drafts/draft-0/send', body: { version: 1, content_hash: '0'.repeat(64) } },
    { path: '/api/outreach/drafts/draft-1/approve', body: { version: 1, content_hash: '1'.repeat(64) } },
    { path: '/api/outreach/drafts/draft-2/approve', body: { version: 1, content_hash: '2'.repeat(64) } },
    { path: '/api/outreach/drafts/draft-2/send', body: { version: 1, content_hash: '2'.repeat(64) } },
  ])
  await review.getByRole('button', { name: 'Back to drafts' }).click()
  await page.getByRole('button', { name: 'Select all reviewable emails' }).click()
  await page.getByRole('button', { name: 'Review selected (3)' }).click()
  await expect(review.getByRole('button', { name: 'Approve and send 3 emails' })).toBeDisabled()
  await review.getByRole('button', { name: 'Cancel', exact: true }).click()
  expect(calls).toHaveLength(5)
})
