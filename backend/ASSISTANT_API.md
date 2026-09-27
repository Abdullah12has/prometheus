# Assistant API

The authenticated conversational endpoint is `POST /api/assistant` with
`{"message":"...", "conversation_id":"..."}`. It returns the conversation ID,
assistant reply, and an `actions` array containing API results or browser UI
actions. Browser action links point to their frontend section and navigation is
limited to `/companies`, `/futures`, `/matches`, `/outreach`, and `/voice-notes`.
Omit `conversation_id` to start a thread. `GET
/api/assistant/conversations` lists the workspace's threads and `GET
/api/assistant/conversations/{id}` returns their persisted messages and actions.
All assistant routes require an authenticated session. History remains
available after signing out and back in.

The assistant uses `app.state.llm.complete` and exposes tools only when their
exact method and route are in the allowlist in `assistant.py` and that route is
registered in the current app's OpenAPI schema. Tool argument schemas come from
the registered route's OpenAPI request and parameter schemas; the domain API
performs final validation. Calls use an in-process ASGI transport and forward
only the session cookie, CSRF token, and origin. The model can make at most four
tool rounds and eight tool calls per message.

Current tools cover company creation, lookup and reversible edits; contacts;
workspace summaries; buyer mandates, proposed preferences,
scenarios, match runs and opportunity outcomes; outreach draft create/read/edit
and follow-up drafts; conversation and control reads; sequence create/read/edit
and pause; enrollment reads/stops; historical-deal create/read, analytics and
replay; and voice agent creation/editing and note capture routes when those
routers are registered. Sequence steps must require per-draft approval. Draft
disclosure metadata is reserved for explicit human review. Model calls cannot
confirm preferences, seller intent, buyer identity, or mandate evidence.
Browser actions can open the recorder or voice screens. Sending or approving
email/phone outreach, classifying replies, resuming sequences/enrollments,
template approvals, deletion, credential access, arbitrary routes, SQL, and
model-generated code are not exposed. Opportunity proposal-draft creation is
omitted because its API accepts disclosure authorization.

Research routes, job cancellation, email sequence pause, and dedicated email
draft routes are not present in the inspected registered or parallel modules,
so they are omitted until their exact API definitions are available. Opportunity
draft creation remains a reviewable draft; it never dispatches external contact.
Unavailable model configuration or service returns HTTP 503.
