// Types mirroring backend/permetheus/deals.py (Pydantic schemas) exactly.
// Keep in sync with that file — these are read-only contracts, not guesses.

export type Strength = 'hard' | 'soft' | 'unknown'

export type ConditionKind =
  | 'retained_ownership'
  | 'operating_control'
  | 'site_retention'
  | 'team_retention'
  | 'brand_retention'
  | 'timeline'
  | 'structure'
  | 'currency'
  | 'minimum_proceeds'

export type Structure = 'minority_investment' | 'majority_sale' | 'full_sale'

export type MandateStatus = 'active' | 'paused' | 'closed'

export type EvidenceLevel = 'public_strategy' | 'buyer_confirmed'

export type Financing = 'unknown' | 'buyer_stated' | 'evidenced'

export type MatchStatus = 'compatible' | 'research_needed' | 'excluded'

export type DealStatus = 'announced' | 'completed' | 'withdrawn'

export type Milestone =
  | 'reached'
  | 'replied'
  | 'qualified'
  | 'meeting'
  | 'nda'
  | 'engagement_proposed'
  | 'mandate_signed'
  | 'diligence'
  | 'offer'
  | 'closed'
  | 'lost'

export type BuyerResponse =
  | 'request_for_details'
  | 'interested'
  | 'rejected'
  | 'outside_mandate'
  | 'timing_budget_changed'
  | 'already_known'
  | 'opt_out'
  | 'unclear'

export type ReasonCategory =
  | 'outside_mandate'
  | 'valuation_gap'
  | 'structure_mismatch'
  | 'timing'
  | 'financing'
  | 'owner_withdrew'
  | 'buyer_withdrew'
  | 'competing_process'
  | 'other'

export type ReasonBasis = 'stated' | 'confirmed'

export type DisclosureField =
  | 'company_identity'
  | 'country'
  | 'industry'
  | 'revenue'
  | 'ebitda'
  | 'employees'
  | 'owner_conditions'

export type SpeakerAuthority = 'owner' | 'authorized_representative' | 'unverified'

export type SourceKind =
  | 'registry'
  | 'website'
  | 'search_result'
  | 'document'
  | 'email'
  | 'call'
  | 'note'
  | 'owner_reported'
  | 'manual'

export interface SourceIn {
  kind: SourceKind
  url?: string | null
  title?: string | null
  publisher?: string | null
}

export interface SourceOut {
  id: string
  kind: SourceKind
  url: string | null
  title: string | null
  publisher: string | null
  fetched_at: string | null
  published_at: string | null
  content_hash: string | null
  created_at: string
}

// ---------------------------------------------------------------- conditions & preferences

export interface Condition {
  kind: ConditionKind
  strength: Strength
  weight?: number
  min_pct?: string | null
  within_months?: number | null
  structures?: Structure[] | null
  currencies?: string[] | null
  amount?: string | null
  currency?: string | null
  sites?: string[] | null
  note?: string | null
  override?: boolean
}

export interface Confirmation {
  confirmed_at: string
  confirmed_by: string
  authority: SpeakerAuthority
  statement: string
  source_id: string | null
}

export interface PreferenceOut {
  id: string
  company_id: string
  version: number
  conditions: Condition[]
  conditions_hash: string
  stated_by: string | null
  note: string | null
  source_id: string | null
  confirmation: Confirmation | null
  created_at: string
}

export interface PreferenceList {
  company_id: string
  effective_profile_id: string | null
  items: PreferenceOut[]
}

export interface PreferenceIn {
  conditions: Condition[]
  stated_by?: string | null
  note?: string | null
  source_id?: string | null
}

export interface ConfirmIn {
  conditions_hash: string
  speaker_name: string
  speaker_authority: SpeakerAuthority
  statement: string
  source_id?: string | null
  confirmed_at: string
}

// ---------------------------------------------------------------- futures / scenarios

export interface MoneyFact {
  amount: string
  currency: string
}

export interface FactOverrides {
  country?: string | null
  industry?: string | null
  revenue?: MoneyFact | null
  ebitda?: MoneyFact | null
  employees?: number | null
}

export interface ScenarioIn {
  company_id: string
  name: string
  profile_id?: string | null
  conditions?: Condition[]
  remove_kinds?: ConditionKind[]
  facts?: FactOverrides | null
  mandate_ids?: string[] | null
}

export interface CheckLine {
  key: string
  origin: 'mandate' | 'owner'
  strength: 'hard' | 'soft' | 'unconfirmed'
  result: 'pass' | 'fail' | 'unknown'
  blocking: boolean
  detail: string
  citations: string[]
  question: string | null
}

export interface ExplanationLine {
  key: string
  strength: string
  detail: string
  citations: string[]
}

export interface Comparable {
  id: string
  target_name: string
  status: DealStatus
  announced_on: string | null
  structure: Structure | null
  source_url: string
}

export interface Explanation {
  summary: string
  supporting: ExplanationLine[]
  contrary: ExplanationLine[]
  unknown: ExplanationLine[]
  questions: string[]
  next_action: string
  mandate_evidence: EvidenceLevel
  financing: Financing | 'unknown (not inferred)'
  comparables?: Comparable[]
}

export interface ScenarioMandateResult {
  mandate_id: string
  mandate_version: number
  buyer_name: string
  baseline_status: MatchStatus
  status: MatchStatus
  fit_score: number | null
  coverage: number | null
  checks: CheckLine[]
  explanation: Explanation
}

export interface ScenarioOut {
  id: string
  company_id: string
  profile_id: string | null
  name: string
  snapshot: Record<string, unknown>
  snapshot_hash: string
  results: ScenarioMandateResult[]
  created_at: string
}

// ---------------------------------------------------------------- buyer mandates

export interface MandateCriteria {
  countries?: string[]
  industries?: string[]
  financial_currency?: string | null
  revenue_min?: string | null
  revenue_max?: string | null
  ebitda_min?: string | null
  ebitda_max?: string | null
  employees_min?: number | null
  employees_max?: number | null
  structures?: Structure[]
  max_rollover_pct?: string | null
  control_retention?: boolean | null
  site_commitment?: boolean | null
  team_commitment?: boolean | null
  brand_commitment?: boolean | null
  close_within_months?: number | null
  consideration_currency?: string | null
  max_consideration?: string | null
}

export interface MandateIn {
  buyer_name: string
  buyer_company_id?: string | null
  contact_name?: string | null
  advisor?: string | null
  status?: MandateStatus
  criteria: MandateCriteria
  evidence_level: EvidenceLevel
  source_id?: string | null
  identity_verified?: boolean
  confirmed_by?: string | null
  last_confirmed_at?: string | null
  financing_status?: Financing
  financing_source_id?: string | null
  expires_at: string
}

export interface MandateOut extends MandateIn {
  id: string
  version: number
  active: boolean
  created_at: string
  updated_at: string
}

export interface MandateVersionOut {
  version: number
  data: Record<string, unknown>
  changed_fields: string[]
  created_at: string
}

export interface MandateDetail extends MandateOut {
  versions: MandateVersionOut[]
}

// ---------------------------------------------------------------- match runs & opportunities

export interface MatchRunIn {
  company_id: string
  profile_id?: string | null
  mandate_ids?: string[] | null
}

export interface MatchResultOut {
  id: string
  mandate_id: string
  mandate_version: number
  status: MatchStatus
  fit_score: number | null
  coverage: number | null
  checks: CheckLine[]
  explanation: Explanation
}

export interface MatchRunOut {
  id: string
  company_id: string
  profile_id: string | null
  policy_version: string
  snapshot_hash: string
  counts: Record<string, number>
  created_at: string
}

export interface MatchRunDetail extends MatchRunOut {
  snapshot: Record<string, unknown>
  results: MatchResultOut[]
}

export interface OpportunityOut {
  id: string
  company_id: string
  mandate_id: string
  buyer_name: string
  status: MatchStatus
  latest_milestone: Milestone | null
  match_result_id: string | null
  fit_score: number | null
  coverage: number | null
  summary: string | null
  questions: string[]
  stale: boolean
  created_at: string
  updated_at: string
}

export interface OutcomeIn {
  milestone: Milestone
  occurred_at: string
  response?: BuyerResponse | null
  reason_category?: ReasonCategory | null
  reason_basis?: ReasonBasis | null
  evidence_excerpt?: string | null
  source_id?: string | null
  note?: string | null
}

export interface OutcomeOut {
  id: string
  opportunity_id: string
  milestone: Milestone
  response: BuyerResponse | null
  reason_category: ReasonCategory | null
  reason_basis: ReasonBasis | null
  evidence_excerpt: string | null
  source_id: string | null
  note: string | null
  occurred_at: string
  created_at: string
}

export interface DisclosureAuthorization {
  authorized_by: string
  authority: SpeakerAuthority
  scope: DisclosureField[]
  statement: string
  authorized_at: string
  source_id?: string | null
}

export interface DraftIn {
  authorization?: DisclosureAuthorization | null
}

export interface DraftOut {
  id: string
  opportunity_id: string
  mandate_id: string
  authorization: Record<string, unknown>
  payload: Record<string, unknown>
  content_hash: string
  status: string
  created_at: string
}

// ---------------------------------------------------------------- historical deals

export interface HistoricalDealIn {
  buyer_name: string
  buyer_company_id?: string | null
  target_name: string
  target_company_id?: string | null
  status: DealStatus
  announced_on?: string | null
  completed_on?: string | null
  withdrawn_on?: string | null
  sector?: string | null
  country?: string | null
  structure?: Structure | null
  stake_pct?: string | null
  value_amount?: string | null
  value_currency?: string | null
  source_url: string
  source_title?: string | null
  disclosure_rights: 'public' | 'licensed' | 'internal'
  as_of: string
}

export interface HistoricalDealOut extends HistoricalDealIn {
  id: string
  created_at: string
  updated_at: string
}

export interface DealPage {
  items: HistoricalDealOut[]
  total: number
}

export interface ImportResult {
  imported: number
  skipped_duplicate_rows: number[]
  ids: string[]
}

// ---------------------------------------------------------------- analytics & replay

export interface DealAnalytics {
  historical_deals: {
    total: number
    by_status: Record<string, number>
    value_disclosed: number
    value_undisclosed: number
  }
  opportunities: {
    total: number
    by_status: Record<string, number>
    by_latest_milestone: Record<string, number>
  }
  outcome_events: number
  failure_reasons: {
    lost_events: number
    supported: Record<string, number>
    without_supported_reason: number
  }
  notes: string[]
}

export interface ReplayIn {
  as_of: string
  company_id?: string | null
}

export interface ReplayResultItem {
  mandate_id: string
  saved_status: MatchStatus
  replayed_status: MatchStatus
  changed: boolean
  comparables_known: Comparable[]
  observed_milestones: { milestone: Milestone; occurred_at: string }[]
}

export interface ReplayRun {
  run_id: string
  company_id: string
  created_at: string
  saved_policy_version: string
  results: ReplayResultItem[]
}

export interface ReplayResponse {
  status: 'unavailable' | 'completed'
  as_of: string
  reasons?: string[]
  counts: Record<string, number>
  policy_version?: string
  runs?: ReplayRun[]
  notes?: string[]
}

// ---------------------------------------------------------------- display helpers (pure, no side effects)

export const CONDITION_LABELS: Record<ConditionKind, string> = {
  retained_ownership: 'Retained ownership',
  operating_control: 'Owner keeps operating control',
  site_retention: 'Site stays open',
  team_retention: 'Team retained',
  brand_retention: 'Brand retained',
  timeline: 'Timeline to close',
  structure: 'Deal structure',
  currency: 'Proceeds currency',
  minimum_proceeds: 'Minimum proceeds',
}

export const CONDITION_KINDS: ConditionKind[] = [
  'retained_ownership',
  'operating_control',
  'site_retention',
  'team_retention',
  'brand_retention',
  'timeline',
  'structure',
  'currency',
  'minimum_proceeds',
]

export const STRUCTURE_LABELS: Record<Structure, string> = {
  minority_investment: 'Minority investment',
  majority_sale: 'Majority sale',
  full_sale: 'Full sale',
}

export const MILESTONE_LABELS: Record<Milestone, string> = {
  reached: 'Reached',
  replied: 'Replied',
  qualified: 'Qualified',
  meeting: 'Meeting held',
  nda: 'NDA signed',
  engagement_proposed: 'Engagement proposed',
  mandate_signed: 'Mandate signed',
  diligence: 'Diligence',
  offer: 'Offer',
  closed: 'Closed',
  lost: 'Lost',
}

export const REASON_CATEGORY_LABELS: Record<ReasonCategory, string> = {
  outside_mandate: 'Outside mandate',
  valuation_gap: 'Valuation gap',
  structure_mismatch: 'Structure mismatch',
  timing: 'Timing',
  financing: 'Financing',
  owner_withdrew: 'Owner withdrew',
  buyer_withdrew: 'Buyer withdrew',
  competing_process: 'Competing process',
  other: 'Other',
}

export const BUYER_RESPONSE_LABELS: Record<BuyerResponse, string> = {
  request_for_details: 'Requested more details',
  interested: 'Interested',
  rejected: 'Rejected',
  outside_mandate: 'Outside mandate',
  timing_budget_changed: 'Timing/budget changed',
  already_known: 'Already known to buyer',
  opt_out: 'Opted out',
  unclear: 'Unclear',
}

export const DISCLOSURE_FIELD_LABELS: Record<DisclosureField, string> = {
  company_identity: 'Company identity (name)',
  country: 'Country',
  industry: 'Industry',
  revenue: 'Revenue',
  ebitda: 'EBITDA',
  employees: 'Employee count',
  owner_conditions: "Owner's conditions",
}

export const MATCH_STATUS_LABELS: Record<MatchStatus, string> = {
  compatible: 'Potentially compatible',
  research_needed: 'Research needed',
  excluded: 'Excluded',
}

export function toLocalDateTimeInput(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function fromLocalDateTimeInput(value: string): string | null {
  if (!value) return null
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return null
  return d.toISOString()
}

export function formatDate(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

export function formatDateTime(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
