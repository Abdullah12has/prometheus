// Mirrors backend/permetheus/mail.py and backend/MAIL_API.md exactly.
// Keep both in sync — this file intentionally has no fields the backend does not return.

export type DraftStatus = 'draft' | 'approved' | 'sending' | 'sent' | 'delivery_unknown' | 'failed'

export type ReplyIntent = 'interested' | 'no' | 'optout' | 'unclear'

export type SpeakerAuthority = 'owner' | 'authorized_representative' | 'unverified'

export type StepKind = 'ask_interest' | 'request_missing_fields'

export type StepApproval = 'per_draft' | 'template'

export type SuppressionKind = 'email' | 'company' | 'channel'

// ---------------------------------------------------------------- Gmail

export interface GmailStatus {
  configured: boolean
  connected: boolean
  email: string | null
  scopes: string[]
  required_scopes: string[]
  needs_reauth: boolean
  last_sync_at: string | null
  last_error: string | null
  has_sync_cursor: boolean
}

export interface GmailConnectResponse {
  authorization_url: string
  expires_in: number
}

export interface GmailDisconnectResponse {
  connected: false
  revoked: boolean
}

export interface GmailSyncResponse {
  ingested: number
  resynced: boolean
  tracked_threads: number
}

// ---------------------------------------------------------------- drafts

export interface DraftApproval {
  version: number
  content_hash: string
  approved_at: string
  expires_at: string
  template_approval_id: string | null
}

export interface DraftDispatch {
  id: string
  state: DraftStatus
  rfc_message_id: string
  gmail_message_id: string | null
  gmail_thread_id: string | null
  error: string | null
  claimed_at: string
  finished_at: string | null
}

export interface OutreachDraft {
  id: string
  company_id: string
  contact_id: string
  conversation_id: string | null
  enrollment_id: string | null
  kind: 'manual' | 'sequence' | 'missing_fields'
  recipients: string[]
  subject: string
  body: string
  disclosure: Record<string, unknown>
  version: number
  status: DraftStatus
  content_hash: string
  approval: DraftApproval | null
  dispatch: DraftDispatch | null
  created_at: string
  updated_at: string
}

export interface DraftCreateInput {
  company_id: string
  contact_id: string
  conversation_id?: string | null
  recipients: string[]
  subject: string
  body: string
  disclosure?: Record<string, unknown>
}

export interface DraftPatchInput {
  recipients?: string[]
  subject?: string
  body?: string
  disclosure?: Record<string, unknown>
}

export interface DraftApproveInput {
  version: number
  content_hash: string
  expires_in_minutes?: number
}

// ---------------------------------------------------------------- conversations

export interface MailMessage {
  id: string
  direction: 'inbound' | 'outbound'
  sender: string
  recipients: string[]
  subject: string
  body_text: string
  sent_at: string
  rfc_message_id: string | null
  proposed_intent: ReplyIntent | null
  proposed_reason: string | null
  confirmed_intent: ReplyIntent | null
  confirmed_at: string | null
}

export interface Conversation {
  id: string
  company_id: string
  contact_id: string | null
  gmail_thread_id: string
  subject: string
  status: string
  last_inbound_at: string | null
  updated_at: string
  latest_reply: MailMessage | null
}

export interface ConversationDetail extends Conversation {
  messages: MailMessage[]
  drafts: OutreachDraft[]
}

export interface ClassifyInput {
  message_id: string
  intent: ReplyIntent
  speaker_authority?: SpeakerAuthority
}

// ---------------------------------------------------------------- suppression and stop switch

export interface Suppression {
  id: string
  kind: SuppressionKind
  value: string
  reason: string
  source: 'manual' | 'optout'
  created_at: string
}

export interface SuppressionInput {
  kind: SuppressionKind
  value: string
  reason: string
}

export interface OutreachControls {
  stopped: boolean
}

// ---------------------------------------------------------------- sequences

export interface SequenceStep {
  kind: StepKind
  delay_hours: number
  subject?: string | null
  body?: string | null
  approval: StepApproval
}

export interface Sequence {
  id: string
  name: string
  steps: SequenceStep[]
  version: number
  paused: boolean
  updated_at: string
}

export interface SequenceInput {
  name: string
  steps: SequenceStep[]
}

export interface TemplateApproval {
  id: string
  sequence_version: number
  step_index: number
  template_hash: string
  company_ids: string[]
  expires_at: string
}

export interface TemplateApprovalInput {
  step_index: number
  company_ids: string[]
  expires_in_hours?: number
}

export interface Enrollment {
  id: string
  sequence_id: string
  sequence_version: number
  company_id: string
  contact_id: string
  conversation_id: string | null
  step_index: number
  steps: number
  state: 'active' | 'awaiting_review' | 'stopped' | 'done'
  next_run_at: string
  pending_draft_id: string | null
}

export interface EnrollInput {
  company_id: string
  contact_id: string
}

export interface RunStepResult {
  enrollment_id: string
  step_index: number
  kind: StepKind
  result: 'sent' | 'draft_awaiting_review' | 'awaiting_review' | 'blocked'
  draft_id?: string
  reason?: string
  error?: string
}

export interface RunDueResponse {
  stopped: boolean
  results: RunStepResult[]
}

export interface ReconcileResult {
  dispatch_id: string
  draft_id?: string
  state: DraftStatus
  error?: string
}

export interface ReconcileResponse {
  reconciled: ReconcileResult[]
}
