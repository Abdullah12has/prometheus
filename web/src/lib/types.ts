// Shared domain types. These mirror web/API_CONTRACT.md — keep both in sync.

export type SellerIntent =
  | 'unknown'
  | 'interested'
  | 'conditional'
  | 'not_now'
  | 'not_interested'

export interface Company {
  id: string
  name: string
  industry: string | null
  description: string | null
  country: string | null
  website: string | null
  domain: string | null
  registry_status: string | null
  status: 'provisional' | 'confirmed'
  seller_intent: SellerIntent
  identifiers: CompanyIdentifier[]
  last_verified_at: string | null
  created_at: string
  updated_at: string
}

export interface CompanyIdentifier {
  id: string
  scheme: string
  jurisdiction: string
  value: string
}

export type CompanyDraft = Partial<
  Pick<Company, 'name' | 'website' | 'country' | 'industry' | 'description' | 'registry_status'>
> & { business_id?: string; allow_new?: boolean }

export interface CompanyListResponse {
  items: Company[]
  total: number
}

export interface DashboardSummary {
  companies: number
  companies_by_status: Record<string, number>
  companies_by_seller_intent: Partial<Record<SellerIntent, number>>
  contacts: number
  jobs_by_state: Record<string, number>
  activities_last_7_days: number
}

export interface Connector {
  id: string
  label: string
  configured: boolean
  implemented: boolean
  missing: string[]
  note: string
}

export interface SettingsStatus {
  database: 'ok' | 'unavailable'
  auth: { configured: boolean }
  outbound_dispatch: { email: string; phone: string }
  connectors: Connector[]
}

export interface SessionUser {
  authenticated: boolean
  csrf_token?: string | null
  expires_at?: string | null
}

export interface Source {
  id: string
  kind: string
  url: string | null
  title: string | null
  publisher: string | null
  fetched_at: string | null
  published_at: string | null
  content_hash: string | null
  created_at: string
}

export interface Evidence {
  id: string
  field: string
  value: unknown
  excerpt: string
  locator: Record<string, unknown>
  extraction_method: string
  review_status: string
  source: Source
  created_at: string
}

export interface Financial {
  id: string
  metric: string
  amount: string | number | null
  currency: string | null
  period_start: string
  period_end: string
  scope: string
  status: string
  formula: string | null
  review_status: string
  source: Source
  created_at: string
}

export interface Contact {
  id: string
  company_id: string
  name: string
  title: string | null
  person_role: string
  contact_role: string
  email: string | null
  phone: string | null
  verification: string
  source_id: string | null
  source?: Source | null
  created_at: string
}

export interface IntentStatement {
  id: string
  speaker_name: string
  speaker_authority: 'owner' | 'authorized_representative' | 'unverified'
  stance: SellerIntent
  statement: string
  stated_at: string
  source_id: string | null
  confirmed: boolean
  created_at: string
}

export interface Job {
  id: string
  kind: string
  state: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  company_id: string | null
  attempts: number
  available_at: string
  last_error: string | null
  created_at: string
  updated_at: string
}

export interface Activity {
  id: string
  company_id: string | null
  kind: string
  summary: string
  actor: string
  payload: Record<string, unknown>
  created_at: string
}

export interface CompanyDetail extends Company {
  contacts: Contact[]
  evidence: Evidence[]
  financials: Financial[]
  intent_statements: IntentStatement[]
  jobs: Job[]
  activities: Activity[]
}

export interface IntakeResponse {
  resolution: 'created' | 'matched_business_id' | 'matched_website'
  company: Company
  possible_duplicates: Company[]
}
