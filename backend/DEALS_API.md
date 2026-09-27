# Deals API

Module: `permetheus.deals` (`router`, prefix `/api`). Every route needs the session cookie. Unsafe methods also need `X-CSRF-Token`. Errors use the core envelope: `{"error": {"code", "message", "details?"}}`. Request models reject unknown fields. Money values are JSON strings (Decimal), and currencies are never converted.

## Integration (root)

- `from . import deals`, then `app.include_router(deals.router)`. Importing the module registers its tables on `models.Base`, so `init_db` creates them.
- `deals` imports `mail.OutreachDraft` at module level. `mail` must therefore import `deals` lazily, inside the function (see the send gate below).
- Tables: `buyer_mandates`, `buyer_mandate_versions`, `owner_preference_profiles`, `futures_scenarios`, `match_runs`, `match_results`, `deal_opportunities`, `opportunity_outcomes`, `historical_deals`, `proposal_drafts`.
- `backend/tests/test_deals.py` has a `permetheus.__path__` shim for testing against a separate core checkout. It does nothing once `deals.py` lives in the package, and it can be removed then. The test fixture includes `deals.router` and `mail.router` only if the app has not already registered them.

### Mail send gate (root must add)

In `mail._check_sendable(db, draft)`, before the claim, add:

```python
from .deals import validate_deal_disclosure  # lazy: deals imports mail
validate_deal_disclosure(db, draft.disclosure, draft)
```

- It does nothing for drafts that are not deal briefs.
- For `kind == "deal_brief"`, or a disclosure with `type == "deal_brief"`, it raises `ApiError(409, "deal_disclosure_invalid", …, {"reasons": [...]})` when any of these holds:
  - the proposal is gone or its content hash no longer verifies
  - the opportunity is excluded or stale
  - a newer match result replaced the one the proposal was bound to
  - the payload holds fields outside the authorized scope
  - the contact is no longer a verified buyer-role contact of the mandate's `buyer_company_id`
  - the subject or body was edited after drafting (`message_edited`)
  - the contact or recipients changed (`recipient_changed`)
  - the disclosure was stripped or altered
- Passing `draft` is what catches edited text and stripped metadata. Without it, only the disclosure itself is checked.
- `mail.patch_draft` lets an operator edit the body and re-approve. Such a deal brief is then blocked at send time. To change the content, redraft the proposal.

## Shared types

- `Strength`: `hard` (non-negotiable) | `soft` (preference, the only kind that is scored) | `unknown` (the owner has not said whether it is negotiable).
- `Structure`: `minority_investment` | `majority_sale` | `full_sale`.
- `Check`: `{key, origin: "mandate"|"owner", strength: "hard"|"soft"|"unconfirmed", result: "pass"|"fail"|"unknown", blocking: bool, detail, citations: [str], question: str|null}`.
  - Citation forms: `mandate:<id>@v<n>.criteria.<field>`, `preference:<id>@v<n>.conditions.<kind>`, `company:<id>.country`, `evidence:<id>`, `financial:<id>`, a source URL, `scenario:override.<kind>`, `scenario:hypothetical.<fact>`.
- `Explanation`: `{summary, supporting: [Line], contrary: [Line], unknown: [Line], questions: [str], next_action, mandate_evidence, financing, comparables?: [ComparableDeal]}`.
  - `Line = {key, strength, detail, citations}`.
- `MatchStatus`:
  - `compatible` means potentially compatible. It is never a confirmed match, interest or offer.
  - `research_needed` means no hard condition failed but something blocking is still unresolved.
  - `excluded` means at least one hard condition failed.

### Evaluation rules (deterministic, `policy_version = "constraints-v2"`)

1. Mandate criteria are always hard.
   - geography: `countries` vs `company.country`
   - industry: `industries` vs the latest `industry` evidence, matched case-insensitively
   - `revenue`, `ebitda`, `employees` ranges vs the latest `reported` or `derived` financial observation with an amount
   - Only `accepted` evidence and financials (reviewed via `POST /api/evidence/{id}/review` and `POST /api/financials/{id}/review`) can pass or fail a check.
     - If nothing is accepted, the latest `proposed` item is cited with `review_status: "proposed"`, but its check is `unknown` and it asks for a review. It is never a decision fact.
     - Rejected and estimated values are ignored.
   - A missing fact → `unknown`. A different currency → `unknown` (no FX conversion).
2. Owner condition vs mandate field:

   | Condition | Mandate field | Passes when |
   |---|---|---|
   | `retained_ownership` | `max_rollover_pct` | `max_rollover_pct` ≥ `min_pct` |
   | `operating_control` | `control_retention` | the field is `true` |
   | `site_retention` | `site_commitment` | the field is `true` |
   | `team_retention` | `team_commitment` | the field is `true` |
   | `brand_retention` | `brand_commitment` | the field is `true` |
   | `timeline` | `close_within_months` | `close_within_months` ≤ `within_months` |
   | `structure` | `structures` | the lists share at least one structure |
   | `currency` | `consideration_currency` | the currency is in the owner's list |
   | `minimum_proceeds` | `max_consideration` | `max_consideration` ≥ `amount`, same currency only |

   If the mandate is silent on the field, the result is `unknown`.
3. A result is `excluded` if any hard check fails.
4. Otherwise it is `research_needed` if any of these holds:
   - a hard check is unknown
   - a check with unconfirmed strength does not pass (this asks the owner a question instead of excluding)
   - the mandate is `public_strategy`
   - the buyer's identity is not verified: `identity_verified` is false or the mandate has no `source_id` (blocker `buyer_identity`)
   - there is no owner profile
   - the profile is unconfirmed
5. Otherwise it is `compatible`.
6. `fit_score` = weight of passed soft checks / total soft weight. It is `null` when excluded or when there are no soft conditions.
   - `coverage` = weight of known soft checks / total soft weight.
   - Unknown adds nothing to the score. A score never changes the status.

## Mandates

`MandateIn`:

```
buyer_name: str(1..300)              buyer_company_id: uuid|null
contact_name: str|null               advisor: str|null
status: "active"|"paused"|"closed" = "active"
criteria: MandateCriteria
evidence_level: "public_strategy"|"buyer_confirmed"
source_id: uuid|null                 identity_verified: bool = false
confirmed_by: str|null               last_confirmed_at: datetime(tz)|null  (not future)
financing_status: "unknown"|"buyer_stated"|"evidenced" = "unknown"
financing_source_id: uuid|null
expires_at: datetime(tz)             (must be future on create or when changed)
```

Rules:
- `buyer_confirmed` needs `confirmed_by` and `last_confirmed_at`.
- `public_strategy` needs `source_id` and must not carry a confirmation.
- `identity_verified: true` needs the `source_id` that evidences the buyer's identity. Without both, no result for this mandate can be `compatible`.
- `financing_source_id` is required exactly when `financing_status` is `evidenced`. Financing is never inferred.

`MandateCriteria` (every field optional; an empty list means "not stated"):

```
countries: [ISO2 upper]   industries: [str]
financial_currency: ISO4217 (required with any revenue/ebitda bound)
revenue_min/max: decimal≥0   ebitda_min/max: decimal   employees_min/max: int≥0   (min ≤ max)
structures: [Structure]
max_rollover_pct: 0..100 (max equity the seller keeps; 0 = buyer requires 100%)
control_retention | site_commitment | team_commitment | brand_commitment: bool|null
close_within_months: 1..240
consideration_currency: ISO4217 (required with max_consideration)   max_consideration: decimal≥0
```

`MandateOut` = `MandateIn` + `{id, version, active, created_at, updated_at}`. `active` means `status == active` and `expires_at > now`.

| Method | Path | Body / query | Response |
|---|---|---|---|
| GET | `/api/mandates` | `?active_only=bool&status=` | `[MandateOut]` |
| POST | `/api/mandates` | `MandateIn` | 201 `MandateOut` |
| GET | `/api/mandates/{id}` | | `MandateOut + versions: [{version, data, changed_fields, created_at}]`, newest first |
| PATCH | `/api/mandates/{id}` | partial `MandateIn` | `MandateOut` |

PATCH does a shallow merge; `criteria` is merged key by key. The merged record is then revalidated in full. A change bumps `version` and appends a version row. A no-op PATCH keeps the version. Errors: 422 `validation_error` / `unknown_reference` / `expired`.

## Owner preferences

`Condition`: `{kind, strength, weight: 1..5 = 1, note?}` plus exactly the parameters for its kind. Parameters for any other kind are rejected.

| kind | parameters |
|---|---|
| `retained_ownership` | `min_pct` (0..100) |
| `operating_control`, `team_retention`, `brand_retention` | none |
| `site_retention` | optional `sites: [str]` |
| `timeline` | `within_months` (1..240) |
| `structure` | `structures: [Structure]` |
| `currency` | `currencies: [ISO4217]` |
| `minimum_proceeds` | `amount`, `currency` |

Each kind may appear at most once per version. Versions are immutable. Only a confirmation can be added to a version.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/companies/{id}/preferences` | | `{company_id, effective_profile_id, items: [PreferenceOut]}` |
| POST | `/api/companies/{id}/preferences` | `{conditions: [Condition], stated_by?, note?, source_id?}` | 201 `PreferenceOut` (next version) |
| POST | `/api/preferences/{profile_id}/confirm` | `{conditions_hash, speaker_name, speaker_authority, statement, source_id?, confirmed_at}` | `PreferenceOut` |

- `items` is newest first. `effective_profile_id` is the highest confirmed version.
- `PreferenceOut`: `{id, company_id, version, conditions, conditions_hash (sha256), stated_by, note, source_id, confirmation: {confirmed_at, confirmed_by, authority, statement, source_id}|null, created_at}`.
- Confirm errors:
  - 422: `speaker_authority` is `unverified`, or `confirmed_at` is in the future.
  - 409 `version_mismatch`: the hash differs.
  - 409 `already_confirmed`.
  - 409 `superseded`: a newer version is already confirmed.

## Futures scenarios (immutable)

POST `/api/scenarios`:

```
{company_id, name,
 profile_id?,            # default: latest version, confirmed or not
 conditions?: [Condition],   # replace or add by kind
 remove_kinds?: [kind],      # must not overlap conditions
 facts?: {country?, industry?, revenue?: {amount, currency}, ebitda?: {...}, employees?},  # hypothetical
 mandate_ids?: [uuid]}   # default: all active; inactive → 409 inactive_mandates
```

→ 201 `{id, company_id, profile_id, name, snapshot, snapshot_hash, results, created_at}`

- `snapshot` = `{taken_at, policy_version, company, profile, overrides, effective_conditions, facts, mandates}`. Hypothetical facts carry `hypothetical: true`.
- `results[]` = `{mandate_id, mandate_version, buyer_name, baseline_status, status, fit_score, coverage, checks, explanation}`.
  - `baseline_status` is the result for the unmodified profile and the real facts.
- Profiles and facts are never written by a scenario. There is no update or delete route.

GET `/api/scenarios?company_id=&limit=` → `[ScenarioOut]`, newest first.

## Match runs and opportunities

POST `/api/match-runs`: `{company_id, profile_id?, mandate_ids?}` → 201 `MatchRunDetail`

- `profile_id` defaults to the highest confirmed version. With no confirmed version, the run uses no profile and every result is `research_needed` at best.
- Errors: 409 `no_active_mandates`, 409 `inactive_mandates`, 422 `profile_company_mismatch`.
- The run persists a snapshot (facts, profile, mandate versions) with `snapshot_hash`, one `MatchResult` per mandate, and upserted opportunities:
  - One opportunity per company and mandate.
  - An excluded result never creates an opportunity.
  - An existing opportunity whose new result is excluded is set to `excluded` and keeps its outcome history.
- `explanation.comparables`: up to 5 historical deals by the same buyer (by name or `buyer_company_id`) known at run time: `{id, target_name, status, announced_on, structure, source_url}`. They are context only, not evidence of current demand.

`MatchRunOut` = `{id, company_id, profile_id, policy_version, snapshot_hash, counts: {compatible, research_needed, excluded}, created_at}`.

`MatchRunDetail` = `MatchRunOut + {snapshot, results: [{id, mandate_id, mandate_version, status, fit_score, coverage, checks, explanation}]}`. Results are sorted compatible → research_needed → excluded, then by score and coverage.

| Method | Path | Response |
|---|---|---|
| GET | `/api/match-runs?company_id=&limit=` | `[MatchRunOut]` |
| GET | `/api/match-runs/{id}` | `MatchRunDetail` |
| GET | `/api/opportunities?company_id=&mandate_id=&status=&limit=` | `[OpportunityOut]` |

`OpportunityOut` = `{id, company_id, mandate_id, buyer_name, status, latest_milestone, match_result_id, fit_score, coverage, summary, questions, stale, stale_reasons, created_at, updated_at}`.

`stale` is true when `stale_reasons` is non-empty. The reasons compare the result's saved snapshot with the current state:

- `mandate_version`: the mandate changed.
- `mandate_inactive`: the mandate is paused, closed or expired.
- `company_facts`: the company name, or the current facts, differ from the snapshot. That includes a review decision on evidence or financials, new data and a country change.
- `owner_profile`: the effective (highest confirmed) owner profile differs from the snapshot's profile id or `conditions_hash`. A new unconfirmed version is not effective.
- `no_match_result`.

Rerun matching to clear it.

### Outcomes

POST `/api/opportunities/{id}/outcomes` → 201 `OutcomeOut`

```
{milestone: reached|replied|qualified|meeting|nda|engagement_proposed|mandate_signed|diligence|offer|closed|lost,
 occurred_at (tz, not future),
 response?: request_for_details|interested|rejected|outside_mandate|timing_budget_changed|already_known|opt_out|unclear,
 reason_category?: outside_mandate|valuation_gap|structure_mismatch|timing|financing|owner_withdrew|buyer_withdrew|competing_process|other,
 reason_basis?: stated|confirmed,
 evidence_excerpt?, source_id?, note?}
```

- A reason needs `milestone = lost`, a `reason_basis`, and an `evidence_excerpt` or `source_id`. No reply is never recorded as a reason.
- `latest_milestone` is the event with the latest `occurred_at`.
- GET `/api/opportunities/{id}/outcomes` → `[OutcomeOut]` in `occurred_at` order.
- `OutcomeOut` = the request fields + `{id, opportunity_id, created_at}`.

### Buyer-specific proposal (no sending)

POST `/api/opportunities/{id}/drafts`

- Body: `{authorization: {authorized_by, authority: owner|authorized_representative, scope: [company_identity|country|industry|revenue|ebitda|employees|owner_conditions], statement, authorized_at, source_id?}}`
- Response 201: `{id, opportunity_id, mandate_id, authorization, payload, content_hash, status: "draft", created_at}`.
- `payload` = `{recipient, basis, match_status, company, disclaimer}`.
  - `basis` binds the exact snapshot: `{company_id, company_name, facts_hash, profile_id, profile_version, conditions_hash, mandate_id, mandate_version, match_result_id, snapshot_hash}`.
  - `content_hash` = sha256 over `{payload, authorization}`.
- `company` comes from the match-run snapshot and holds only the authorized scope:
  - Only accepted facts are included. A proposed fact is left out even when it is in scope.
  - Without `company_identity`, the company name reads "Undisclosed company".
  - `owner_conditions` are included only from a confirmed profile, without notes or unknown-strength conditions.
- Errors:
  - 409 `disclosure_authorization_required`: no authorization in the body.
  - 409 `opportunity_excluded`.
  - 409 `opportunity_stale`, with `details.reasons` as above.
  - 422: the authority is unverified.

### Email draft from a proposal (no approval, no sending)

POST `/api/opportunities/{id}/email-draft`: `{proposal_id, contact_id}` → 201

```
{mail_draft_id, status: "draft", recipients: [email], subject, body, disclosure}
```

- Creates an unapproved `mail.OutreachDraft` with `kind="deal_brief"`, `company_id` set to the buyer company and `recipients=[contact.email]`.
- Approval and sending use the existing mail routes and gate. This route never approves or sends.
- `subject` and `body` are plain text built only from the proposal's authorized `company` payload and its disclaimer. The contact name is used in the greeting.
- `disclosure` = `{type: "deal_brief", proposal_id, proposal_content_hash, opportunity_id, contact_id, basis, scope, authorized_by, authority, authorized_at, message_sha256}`. `message_sha256` covers the subject and body.
- Errors:
  - 422 `proposal_mismatch`: the proposal belongs to another opportunity.
  - 409 `disclosure_blocked`, with `details.reasons` drawn from:
    - `proposal_modified`
    - `opportunity_excluded`
    - `match_result_superseded`
    - any stale reason
    - `out_of_scope:<field>`
    - `mandate_without_buyer_company`
    - `contact_missing`, `contact_not_at_buyer_company`, `contact_not_buyer_role`, `contact_unverified`, `contact_without_email`

`validate_deal_disclosure(db, disclosure, draft=None)` runs the same checks at send time (see "Mail send gate").

## Historical deals

`HistoricalDealIn`:

```
buyer_name, target_name: str               buyer_company_id?, target_company_id?: uuid (must exist)
status: announced|completed|withdrawn
announced_on?, completed_on?, withdrawn_on?: date
sector?: str   country?: ISO2   structure?: Structure   stake_pct?: 0..100
value_amount?: decimal≥0  value_currency?: ISO4217   (both or neither; null = undisclosed, never zero)
source_url: http(s) URL (required)   source_title?
disclosure_rights: public|licensed|internal
as_of: datetime(tz), when the information became available (not future)
```

Rules:
- `completed_on` is required exactly when `status` is `completed`. `withdrawn_on` is required exactly when `status` is `withdrawn`.
- An end date must not be before `announced_on`.
- No event date may be after `as_of`.

| Method | Path | Body / query | Response |
|---|---|---|---|
| GET | `/api/historical-deals` | `?status=&buyer=&known_as_of=&limit=&offset=` | `{items: [HistoricalDealOut], total}` |
| POST | `/api/historical-deals` | `HistoricalDealIn` | 201 `HistoricalDealOut` |
| PATCH | `/api/historical-deals/{id}` | partial, fully revalidated | `HistoricalDealOut` |
| DELETE | `/api/historical-deals/{id}` | | `{id, deleted: true}` |
| POST | `/api/historical-deals/import` | raw CSV, `Content-Type: text/csv` | 201 `{imported, skipped_duplicate_rows: [line], ids}` |

- `HistoricalDealOut` = `HistoricalDealIn` + `{id, created_at, updated_at}`.
- CSV import:
  - The header must be a subset of the `HistoricalDealIn` field names and include every required field.
  - Empty cells mean null.
  - Limits: 1 MB (413 `payload_too_large`) and 2000 rows (413 `too_many_rows`). A wrong media type returns 415.
  - The import is all or nothing. Invalid rows return 422 `invalid_csv_rows` with `details: [{row, errors}]` (first 50 rows), and nothing is written.
  - Exact duplicates (same source_url, buyer and target ignoring case, and status), whether in the file or already stored, are skipped and reported.
  - Other errors: `invalid_csv_header`, `empty_csv`, `invalid_csv`.

## Analytics

GET `/api/deals/analytics` returns counts only:

```
{historical_deals: {total, by_status, value_disclosed, value_undisclosed},
 opportunities: {total, by_status, by_latest_milestone},
 outcome_events,
 failure_reasons: {lost_events, supported: {category: n}, without_supported_reason},
 notes: [str]}
```

It reports no rates, probabilities or causal claims.

## Simulation replay (read-only)

POST `/api/simulations/replay`: `{as_of (tz, not future), company_id?}`

- The replay re-evaluates saved match snapshots taken at or before `as_of` with the current policy.
- Comparables are limited to historical deals whose `as_of` is at or before the replay `as_of`.
- Outcomes recorded after each run are attached as labels, not inputs.
- If no records apply: `{status: "unavailable", as_of, reasons: [str], counts}`. This happens when there are no snapshots, or when no historical deals were known at `as_of` and no replayed result has outcomes.
- Otherwise: `{status: "completed", as_of, policy_version, counts: {runs, results, changed, labeled, historical_deals_known}, runs: [{run_id, company_id, created_at, saved_policy_version, results: [{mandate_id, saved_status, replayed_status, changed, comparables_known, observed_milestones}]}], notes}`.

## Limitations

- The send-time check works only after root wires `validate_deal_disclosure` into `mail._check_sendable`. Until then a deal-brief draft passes only mail's own gate.
- Any rerun of matching replaces the opportunity's result. Older proposals then fail with `match_result_superseded`, even if nothing material changed, and need a new proposal.
- Industry matching is exact after case folding. There is no taxonomy or synonym handling.
- Company country comes from the company record (operator-entered, cited as `company:<id>.country`), not from a source. The `Company.industry` column is not used; only industry evidence counts.
- Replay re-evaluates old snapshots under v2. Proposed facts and unverified buyer identities saved by v1 now come out `research_needed`.
- There is no mandate delete endpoint. `buyer_company_id` inside mandate JSON is checked when written but has no DB foreign key.
- Scenarios do not override mandate assumptions (only owner conditions and hypothetical company facts).
- Opportunities are not recomputed automatically when facts change. `stale` reports it instead.
- The opportunity list recomputes company facts per row for staleness, which is fine up to a few hundred rows.
- Schema bootstrap uses `create_all`; there are no migrations. The tables were verified on SQLite only (the Postgres JSONB variant comes from core's `JsonType`).
