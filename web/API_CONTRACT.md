# API contract (frontend expectations)

The `web/` app is built against the live backend contract below. Update this
file in the same change as any frontend request/response handling it describes.

All requests are same-origin (`/api/...`) and sent with `credentials:
"include"`. All responses are JSON, including errors.

## Conventions

- IDs are UUID strings.
- Timestamps are ISO 8601 strings.
- Error responses use:
  ```json
  { "error": { "code": "machine_code", "message": "human-readable message", "details": {} } }
  ```
  `details` is optional and may contain validation entries or intake candidates.
- Endpoints marked **(built)** are called by the current frontend slice.
  Endpoints marked **(planned)** are referenced by placeholder views only
  and are not called yet.

## Auth **(built)**

### `POST /api/auth/login`
Request: `{ "password": "string" }`
Response: `{ "authenticated": true, "csrf_token": "...", "expires_at": "..." }`.
The frontend stores `csrf_token` and sends it as `X-CSRF-Token` on every write.
Sets the session cookie.

### `GET /api/auth/me`
Response:
```json
{ "authenticated": true, "csrf_token": "...", "expires_at": "..." }
```
`role` is optional/unused today. `authenticated: false` (or a `401`) sends
the user to the login screen.

### `POST /api/auth/logout`
Clears the session cookie. Any `2xx` response is treated as success.

## Companies **(built)**

### `GET /api/companies`
Query params: `q` (optional, free-text search across name/website/business ID).
Response: `{ "items": [...], "total": 42 }`. Each item is a `Company`.

### `POST /api/companies`
Request (`CompanyDraft`, all fields optional except that at least one of
`name`/`website` should be present):
```json
{
  "name": "Acme Oy",
  "website": "https://acme.fi",
  "country": "Finland",
  "industry": "Manufacturing"
}
```
The intake dialog sends only filled fields. Response is an `IntakeOut` wrapper
with `resolution`, `company`, and `possible_duplicates`. An
`ambiguous_company` 409 puts candidate companies in `error.details.candidates`;
the operator can explicitly resend with `allow_new: true`.

### `GET /api/companies/{id}`
Response: a `Company`. `404` with an error body if not found.

### `PATCH /api/companies/{id}`
Request: any subset of `Company` fields (`CompanyDraft`). Response: the
updated `Company`.

### `Company` shape
```json
{
  "id": "uuid",
  "name": "Acme Oy",
  "website": "https://acme.fi",
  "country": "Finland",
  "industry": "Manufacturing",
  "description": "Free-text sourced summary.",
  "domain": "acme.fi",
  "registry_status": null,
  "status": "provisional",
  "identifiers": [{ "scheme": "business_id", "jurisdiction": "FI", "value": "1234567-8" }],
  "seller_intent": "unknown",
  "created_at": "2026-09-01T12:00:00Z",
  "updated_at": "2026-09-20T09:30:00Z"
}
```
`seller_intent` is one of: `"unknown" | "interested" | "conditional" |
"not_now" | "not_interested"`. Nullable fields (`website`, `country`, `industry`,
`description`) may be `null`. Company create/PATCH never accepts seller intent;
confirmed intent is recorded through the separate intent-statements endpoint.

## Dashboard **(built)**

### `GET /api/dashboard`
Response:
```json
{
  "companies": 42,
  "companies_by_status": { "provisional": 5 },
  "companies_by_seller_intent": { "interested": 3, "unknown": 30 },
  "contacts": 12,
  "jobs_by_state": { "queued": 2 },
  "activities_last_7_days": 8
}
```

## Settings status **(built)**

### `GET /api/settings/status`
Response:
```json
{
  "database": "ok",
  "auth": { "configured": true },
  "outbound_dispatch": { "email": "disabled", "phone": "disabled" },
  "connectors": [{ "id": "gmail", "label": "Gmail", "configured": false,
    "implemented": false, "missing": ["GOOGLE_CLIENT_ID"], "note": "..." }]
}
```
`status` is one of `"ok" | "warning" | "error" | "unconfigured"`.

## Not built yet — referenced by placeholder views only

These views ship with honest empty states and are not wired to any
endpoint. They exist so the shell and navigation are complete; connect them
once the corresponding backend work lands.

| View | Expected endpoint (placeholder only) |
|---|---|
| Outreach | `GET /api/outreach` — conversation threads, sequence step, next action |
| Futures | `GET /api/futures` — owner-confirmed conditions per company |
| Matches | `GET /api/matches` — ranked buyer-mandate/company pairs |
| Voice & notes | `GET /api/recordings` — recordings, transcripts, reviewed summaries |
| Assistant panel | `POST /api/assistant/turns` — plan/execute allowed tools, return object links |

No shape is assumed for these yet — define it when the backend work starts
and update this file first.
