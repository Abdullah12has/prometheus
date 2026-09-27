# Gmail and outreach API

Module: `backend/permetheus/mail.py`. It declares its own tables on the shared `Base` and exposes two routers:

- `mail.router` (prefix `/api`, every route requires the operator session and `X-CSRF-Token` for writes)
- `mail.oauth_router` (prefix `/oauth/google`, the browser callback; bound by state + cookie instead of the session)

## Application integration

```python
from . import mail
app.include_router(mail.router)
app.include_router(mail.oauth_router)
```

Configured settings (missing OAuth credentials report "not configured"):

| Setting | Purpose |
|---|---|
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | OAuth web client |
| `GOOGLE_REDIRECT_URI` | Exact registered callback, e.g. `http://localhost:4311/oauth/google/callback` |
| `SESSION_SECRET` | ≥ 32 characters. HKDF-SHA256 derives the Fernet key that encrypts tokens and PKCE verifiers at rest |

Dependency: `cryptography`. After login the browser is redirected to the first `WEB_ORIGINS` entry + `/outreach`, e.g. `http://localhost:4310/outreach?gmail=connected` or `?gmail=error&reason=…`.

Rotating `SESSION_SECRET` makes stored tokens unreadable. Status then reports `gmail_reauth_required` and the operator reconnects.

## Google Cloud setup

1. Create a Google Cloud project and enable the **Gmail API**.
2. OAuth consent screen: choose **Internal** only if every mailbox belongs to a Google Workspace organization you control. Otherwise choose **External** and add each development mailbox as a test user.
3. Create an OAuth client of type **Web application** and register the exact redirect URI above.
4. Scopes requested, and nothing else:
   - `https://www.googleapis.com/auth/gmail.send`: send approved messages.
   - `https://www.googleapis.com/auth/gmail.readonly`: read replies in tracked threads and search Sent mail to reconcile uncertain sends. This is a *restricted* scope. It technically grants whole-mailbox read access, so show that disclosure in Settings. An external production app needs Google verification and possibly a security assessment.
   - No draft, modify or delete scope: drafts live in Permetheus until sent.
5. Flow: authorization code with PKCE (S256), `access_type=offline`, `prompt=consent` (so a refresh token is issued). The callback rejects the grant if either scope was not granted or no refresh token came back.
6. **Testing-mode limit:** External apps in *Testing* get refresh tokens that expire after about 7 days. The next refresh then fails with `invalid_grant`, the account is marked `needs_reauth`, and status shows it. That is fine for a labelled development build but is not production readiness.

Tokens never reach the browser or a model. `GET /api/gmail/status` returns no token material.

## Endpoints

### Gmail connection

| Method | Path | Notes |
|---|---|---|
| POST | `/api/gmail/connect` | Returns `{authorization_url, expires_in}`. Stores a hashed, 10-minute, one-time state and sets an HttpOnly `SameSite=Lax` cookie (path `/oauth/google`) that binds the state to this browser |
| GET | `/oauth/google/callback` | Validates state hash + cookie + expiry, consumes it atomically, exchanges the code with the PKCE verifier, fetches the profile, and stores encrypted tokens and the address. A new mailbox starts its `historyId` cursor from the profile. **Reconnecting the same mailbox keeps the existing cursor**, so replies that arrived while it was disconnected are still read. Always 303-redirects to `/outreach` |
| GET | `/api/gmail/status` | `configured, connected, email, scopes, required_scopes, needs_reauth, last_sync_at, last_error, has_sync_cursor` |
| POST | `/api/gmail/disconnect` | Revokes the refresh token at Google (best effort, reported as `revoked`) and erases the tokens. The row stays with its address and cursor (`needs_reauth: true`), so retained threads stay tied to that mailbox |
| POST | `/api/gmail/sync` | Incremental `history.list` from the stored cursor, following `nextPageToken` (up to 50 pages). **Only threads Permetheus started** are fetched: each thread ID is looked up when its message is processed, and other entries are dropped before any body is requested. A 404 (expired cursor) or too much history triggers a resync of tracked threads, newest first (`resynced: true`), never "no reply". If more than 500 threads are tracked, the resync reads the newest 500, returns `complete: false`, sets `last_error` and **does not advance the cursor**. Provider message IDs are deduplicated. Each stored reply is committed on its own. Refreshes the access token when needed. A 429 comes back as `gmail_rate_limited` |

**One mailbox per workspace.** Conversations, dispatches and Message-IDs are not tagged with a mailbox, so connecting a *different* address is refused (`?gmail=error&reason=mailbox_mismatch`, and the new grant is revoked) while any conversation or dispatch exists. With no retained history, the old mailbox's token is revoked and its row replaced. No schema change or migration is needed for this.

The application scheduler syncs replies, reconciles uncertain deliveries, then runs due approved sequence steps every 60 seconds.

### Drafts, approval, send

| Method | Path | Notes |
|---|---|---|
| POST | `/api/outreach/drafts` | `company_id, contact_id, conversation_id?, recipients[], subject, body, disclosure{}`. Recipients are normalized and deduplicated. The subject must be one line |
| GET | `/api/outreach/drafts?status=&company_id=` | List. Each draft includes `content_hash`, `approval` and the latest `dispatch` |
| GET | `/api/outreach/drafts/{id}` | Detail |
| PATCH | `/api/outreach/drafts/{id}` | Any edit bumps `version` and voids the approval. Refused for `sending`, `sent` and `delivery_unknown` (`409 draft_locked`). The write is conditional on the version and status that were read, so an edit racing a send claim or another edit gets `409 draft_conflict` and changes nothing |
| POST | `/api/outreach/drafts/{id}/approve` | Body `{version, content_hash, expires_in_minutes=1440}`. Must match exactly what the operator previewed, otherwise `409 stale_preview`. Also conditional on version + status |
| POST | `/api/outreach/drafts/{id}/send` | See send gate below |
| POST | `/api/outreach/reconcile` | Resolves `delivery_unknown` attempts and stale `sending` attempts (> 5 min) by searching `rfc822msgid:` with `includeSpamTrash=true`. Found: marked `sent`, and the thread's messages are ingested once (sync skipped them while the thread was untracked). **Not found stays `delivery_unknown` indefinitely** and the draft stays locked: a miss is not proof it wasn't sent (index lag, deleted mail). **Never resends.** To contact the person again, create a new draft |

**Send gate**, rechecked at send time:

1. Gmail connected, token refreshable.
2. Draft `approved`, approval hash = current content hash, approved version = current version, not expired. A template approval must also not be revoked or expired.
3. No suppression: global stop switch, `channel=email`, the company, or any recipient address.
4. Company `status=confirmed`. The contact and every recipient are verified contacts of that company.
5. For in-thread drafts: the conversation is not `opted_out` or `declined`, and no reply was ingested after the approval (`conversation_changed`).
6. Template-approved drafts additionally need a conversation in `awaiting_reply` with no stored inbound message. Once the contact has answered, even after "interested" or a manual resume re-activates the enrollment, the next template step becomes `blocked` and needs per-draft review.
7. For in-thread drafts, Gmail is asked for the thread (`threads.get?format=minimal`). Any non-draft message ID not stored locally (a reply that has not been synced yet) gives `409 conversation_changed`.

Then the draft is **claimed**: a conditional `UPDATE … WHERE status='approved' AND version=… AND approved_hash=…` plus a ledger row that is unique per `(draft_id, draft_version)`, committed *before* Gmail is called. **Checks 2–6 run again after the claim commit.** A stop, suppression or reply committed before the claim still blocks. The attempt is then recorded `failed` with `Blocked at send time: <code>`, and nothing reaches Gmail. The MIME message carries a deterministic `Message-ID` `<permetheus.{draft}.v{version}@{sender-domain}>`. In-thread sends set `threadId`, `In-Reply-To` and `References`.

A send is finished only by `UPDATE outreach_dispatches SET state='sent' WHERE id=… AND state IN ('sending','delivery_unknown')`. A second finisher (parallel reconciles, or a reconcile racing the send) changes nothing. The conversation is taken from the `threadId` Gmail returns: if Gmail started a new thread (e.g. the subject changed), that thread becomes a tracked conversation. Message rows are written under a row lock on the conversation (`SELECT … FOR UPDATE`, a no-op on SQLite).

| Provider outcome | Result |
|---|---|
| 2xx with id + threadId | `sent`; conversation + outbound message recorded |
| 4xx (incl. 429), connect error / connect timeout | `failed` (Google never accepted it), `502 send_failed` |
| 5xx, read timeout, dropped connection, 2xx without ids | `delivery_unknown`, `502 delivery_unknown`. Locked until a reconcile finds it |

### Conversations and replies

| Method | Path | Notes |
|---|---|---|
| GET | `/api/outreach/conversations?company_id=&status=` | With `latest_reply` |
| GET | `/api/outreach/conversations/{id}` | Messages and drafts in the thread |
| POST | `/api/outreach/conversations/{id}/classify` | `{message_id, intent: interested\|no\|optout\|unclear, speaker_authority}`. Human confirmation, once per message (`409 already_classified`). `speaker_authority` other than `unverified` requires the sender to be a verified contact of the company (`409 speaker_unverified`) |
| POST | `/api/outreach/conversations/{id}/followup` | Creates an unapproved in-thread draft asking for missing fields. Requires the latest reply to be confirmed `interested` |

Ingestion stores only the new reply text (quoted history and forwarded blocks are stripped). It proposes an intent with keyword rules in English and Finnish. Opt-out is checked first, and automatic replies are marked `unclear`.

- A proposed **opt-out** immediately suppresses the sender *and* the conversation's contact, closes the conversation and stops its sequences. A confirmed opt-out does the same.
- Any other reply sets the conversation to `needs_review` and pauses active sequences for it. A conversation that is already `opted_out` or `declined` stays closed (e.g. a later auto-reply).
- Proposals never change seller intent. `classify` records a confirmed `IntentStatement` with an email `Source`. The statement is credited to the contact's name only if the contact's address sent it; otherwise it is credited to the sender address. `Company.seller_intent` changes only when the operator marks a verified sender as `owner` or `authorized_representative`.
- The follow-up only asks questions. It skips financial metrics that already have accepted observations and never writes figures into the text. Answers come back as replies for review ("owner-reported").

No mail content is sent to a language model by this module.

### Suppression and stop switch

| Method | Path | Notes |
|---|---|---|
| GET | `/api/outreach/suppressions` | |
| POST | `/api/outreach/suppressions` | `{kind: email\|company\|channel, value, reason}` (`channel` value: `email`) |
| DELETE | `/api/outreach/suppressions/{id}` | Opt-outs are protected |
| GET | `/api/outreach/controls` | `{stopped}` |
| POST | `/api/outreach/stop` | `{stopped: true\|false}`. Blocks every send and sequence run |

### Sequences

A step is `{kind: ask_interest|request_missing_fields, delay_hours, subject?, body?, approval: per_draft|template}`. Validation:

- Step 0 is `ask_interest` with `per_draft` approval (cold outreach always gets an exact preview).
- Later steps need `delay_hours > 0` and reply in-thread with `Re: <subject>`.
- Templates may only use `{company_name}` and `{contact_name}`, with no format specs.
- `request_missing_fields` is generated and always reviewed per draft.
- Steps are a linear list, so loops can't be expressed.

| Method | Path | Notes |
|---|---|---|
| GET/POST | `/api/outreach/sequences` | |
| PATCH | `/api/outreach/sequences/{id}` | Full replacement, bumps `version`. Running enrollments keep their snapshot |
| POST | `/api/outreach/sequences/{id}/pause`, `/resume` | |
| POST | `/api/outreach/sequences/{id}/template-approvals` | `{step_index ≥ 1, company_ids[], expires_in_hours ≤ 720}`. Bound to sequence version + step hash + company scope |
| DELETE | `/api/outreach/template-approvals/{id}` | Revoke |
| POST | `/api/outreach/sequences/{id}/enrollments` | `{company_id, contact_id}`. Verified contact, one running sequence per contact |
| GET | `/api/outreach/enrollments?sequence_id=` | |
| POST | `/api/outreach/enrollments/{id}/stop`, `/resume` | |
| POST | `/api/outreach/run-due` | Advances due enrollments (`next_run_at` is stored, so waits survive restarts). Each enrollment is claimed first (`UPDATE … SET pending_draft_id=… WHERE pending_draft_id IS NULL AND state='active' AND step_index=…`). An overlapping run gets `result: skipped` for it, so a step is drafted and sent once. A template-approved in-thread step is sent through the same gate. Anything else becomes a draft awaiting review. The caller schedules this |

## Limitations

- Provider behaviour is covered only by stubbed HTTP (`httpx.MockTransport`). No real Google account, token lifetime, rate limit or send has been exercised. Section 8's two-account acceptance run is still to do.
- **Unverified assumption:** reconciliation relies on Gmail keeping our `Message-ID` header on `messages.send`, so `rfc822msgid:` finds it. Confirm with one real send + search before relying on it. If Gmail rewrites it, uncertain sends stay `delivery_unknown` (safe but stuck).
- Concurrency guards (conditional updates, `FOR UPDATE`) are tested sequentially on SQLite. `test_postgres_concurrent_run_due_claims_once` runs two real threads against PostgreSQL when `PERMETHEUS_TEST_POSTGRES_URL` points at a disposable database (it drops all tables afterwards; needs a PostgreSQL driver).
- An uncertain send has no manual "not sent" override. It stays locked, and the operator creates a new draft.
- Drafts get no disclosure policy check here. The root is expected to call `deals.validate_deal_disclosure(db, disclosure)` for buyer-brief drafts in create/patch.
- Reply classification uses keyword rules. The operator confirms every intent that matters.
- The HTML-to-text fallback is a simple tag strip. Attachments are not stored.
- Out-of-office return dates, bounce parsing and push notifications (Pub/Sub) are not implemented.
- Secure document upload routes for requested fields are not part of this module.
