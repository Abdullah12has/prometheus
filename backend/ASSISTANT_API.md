# Assistant API

The authenticated conversational endpoint is `POST /api/assistant` with
`{"message":"...", "conversation_id":"..."}`. It returns the conversation ID,
assistant reply, and an `actions` array containing API results or browser UI
actions. Browser action links point to their frontend section and navigation is
limited to `/companies`, `/buyers`, `/futures`, `/matches`, `/outreach`, and `/voice-notes`.
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
only the session cookie, CSRF token, and origin. The model can make at most eight
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

Company names, domains and business identifiers are resolved with company search before actions. Ambiguous matches require a name/country/website clarification. Tool results retain useful records when compacted, and persisted action results provide context for follow-up requests.

`GET /api/assistant/search?q=...` uses the configured public-search service and returns dated source snippets. `GET /api/assistant/read-source?url=...` reads a public page through the existing robots-aware, SSRF-safe crawler. Answers cite returned URLs; the UI renders citations and source cards as links. Search outages are explicit, and search snippets are distinguished from fetched page text. Source and research reads, registry/discovery controls, job cancellation/retry, buyer research, note corrections, contact edits and voice session reads are available through allowlisted tools. Uploads, microphone access and explicit human reviews remain browser actions.

Chat and live voice disable model reasoning. `LLM_CHAT_MODEL` optionally selects a separate fast model; otherwise they use `LLM_MODEL` (the current local setup uses Gemini 2.5 Flash). Research retains its own reasoning setting. Individual chat model calls time out after 40 seconds; voice replies after 45 seconds. Unavailable model configuration or service returns HTTP 503.
