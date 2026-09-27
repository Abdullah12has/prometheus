// Shared buyer-directory domain types. These mirror the buyers module
// contract exactly (routes, field names and enums) — keep both in sync.
// Countries covered: Nordics (DK, FI, IS, NO, SE) plus CH, DE.

export type BuyerKind = 'private_equity' | 'family_office' | 'holding_company' | 'unknown'
export type BuyerStatus = 'candidate' | 'profiled' | 'excluded'
export type BuyerResearchStatus = 'pending' | 'queued' | 'running' | 'completed' | 'failed'

export const BUYER_COUNTRIES = ['DK', 'FI', 'IS', 'NO', 'SE', 'CH', 'DE'] as const
export type BuyerCountry = (typeof BUYER_COUNTRIES)[number]

export const NORDIC_COUNTRIES: BuyerCountry[] = ['DK', 'FI', 'IS', 'NO', 'SE']
export const OTHER_COUNTRIES: BuyerCountry[] = ['CH', 'DE']

export const COUNTRY_LABELS: Record<BuyerCountry, string> = {
  DK: 'Denmark',
  FI: 'Finland',
  IS: 'Iceland',
  NO: 'Norway',
  SE: 'Sweden',
  CH: 'Switzerland',
  DE: 'Germany',
}

export const BUYER_KIND_LABELS: Record<BuyerKind, string> = {
  private_equity: 'Private equity',
  family_office: 'Family office',
  holding_company: 'Holding company',
  unknown: 'Unknown',
}

export const BUYER_STATUS_LABELS: Record<BuyerStatus, string> = {
  candidate: 'Candidate',
  profiled: 'Profiled',
  excluded: 'Excluded',
}

export const BUYER_RESEARCH_STATUS_LABELS: Record<BuyerResearchStatus, string> = {
  pending: 'Pending',
  queued: 'Queued',
  running: 'Researching',
  completed: 'Researched',
  failed: 'Research failed',
}

export interface Buyer {
  id: string
  company_id: string | null
  name: string
  website: string | null
  country: string | null
  kind: BuyerKind
  status: BuyerStatus
  exclusion_reason?: string | null
  summary: string | null
  sectors: string[]
  geographies: string[]
  preferences: string[]
  exclusions: string[]
  research_status: BuyerResearchStatus
  last_researched_at: string | null
  source_count: number
  history_count: number
}

export interface BuyerListResponse {
  items: Buyer[]
  total: number
}

export interface BuyerFact {
  review_status?: string
  field: string
  value: string | null
  source_id: string | null
  source_url: string | null
  excerpt: string | null
}

export interface BuyerHistoryItem {
  review_status?: string
  id: string
  target_name: string
  status: string
  announced_on: string | null
  source_url: string | null
  summary: string | null
}

export interface BuyerSource {
  id: string
  title: string | null
  url: string | null
  fetched_at: string | null
}

export interface BuyerJobRef {
  id: string
  state: string
  error: string | null
}

export interface BuyerDetail extends Buyer {
  facts: BuyerFact[]
  history: BuyerHistoryItem[]
  sources: BuyerSource[]
  gaps: string[]
  research_error: string | null
  latest_job: BuyerJobRef | null
}

export interface BuyerDraft {
  name: string
  website: string
  country: string
  kind?: BuyerKind
}

export interface BuyerResearchResponse {
  job_id: string
  state: string
}

export interface BuyerMandateResponse {
  mandate_id: string
}

export interface DiscoverySource {
  id: string
  country: string
  label: string
  url: string
  coverage: string
}

export type DiscoveryRunStatus = 'queued' | 'running' | 'paused' | 'completed' | 'failed'

export interface DiscoveryRun {
  id: string
  status: DiscoveryRunStatus
  countries: string[]
  source_index: number
  source_total: number
  found: number
  created: number
  matched: number
  errors: string[]
  updated_at: string
  job_id: string | null
}

export interface BuyersSummary {
  total: number
  by_country: Record<string, number>
  by_kind: Record<string, number>
  research_jobs: { queued: number; running: number; succeeded: number; failed: number }
  coverage: string
}
