# Permetheus

Mergero's local M&A origination workspace: discover companies, research sourced facts, explore owner conditions, compare buyer mandates, prepare outreach, and capture conversations.

## Run locally

Prerequisites: macOS Apple Silicon, uv, Node.js/npm, Rust/Cargo, Docker Desktop and ffmpeg. The native speech build uses Metal.

```sh
make setup
make dev
```

Open [localhost:4310](http://localhost:4310). Sign in using `ADMIN_PASSWORD` from the local `.env`. Setup creates missing local credentials without printing them. Existing settings are preserved. Keep `.env` private and excluded from Git.

`make dev` starts PostgreSQL and SearXNG containers, the API on 4311, and the web app on 4310. With `WORKER_ENABLED=true`, the API processes durable research, document and recording jobs and refreshes changed matches and checks approved outreach sequences every minute. Run a single API process for the personal workspace.

See [local setup and recovery](LOCAL_SETUP.md) for the pinned speech model, backups, and restore. `make doctor` reports configuration presence without printing secrets. Ctrl-C stops the application processes; `make stop` stops the service containers.

## Main workflows

- **Companies:** paste a name or website, or discover records from the Finnish register. Inspect research coverage, sources, contacts and financials. Scraped facts are proposals; review them before relying on them for matching. Upload text PDF/iXBRL statements under Financials. Scanned PDFs need manual review.
- **Futures:** record owner conditions, confirm an exact version, and compare hypothetical scenarios without changing the confirmed conditions.
- **Matches:** record sourced buyer mandates, run comparisons, inspect reasons and missing evidence, record outcomes, and replay historical snapshots. An authorized brief can become an unapproved email draft.
- **Outreach:** connect Gmail, review drafts and recipients, approve exact content, then send. Manage follow-up sequences, opt-outs and the global stop control here. No email is sent simply by researching or matching a company.
- **Voice & notes:** create an agent, upload or record an authorized voice sample, preview it, and run a browser conversation. The Notes tab records locally, resumes pending uploads, transcribes and proposes a summary. Failed transcriptions can be retried without losing corrections. Audio and transcripts stay in local storage; text sent to the configured model leaves the machine.
- **Assistant:** describe actions in plain English. It uses the same application APIs to research, prepare drafts and run comparisons; confirmations and sending remain explicit UI actions.

## Connections

The language model uses an OpenAI-compatible gateway configured with `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`. `LLM_REASONING_EFFORT=none` was verified with the configured Gemini 2.5 Flash gateway for quick voice responses. Change or remove this value if another model does not support it.

For Gmail, create a Google OAuth web application, enable Gmail API, add your personal account as a test user, and configure `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. Register the exact `GOOGLE_REDIRECT_URI` from `.env` (default `http://localhost:4311/oauth/google/callback`). Open the app using **localhost**, then connect from Outreach. The browser and callback must share a hostname. Requested permissions are send and read-only; only tracked outreach threads are imported. Tokens are encrypted using `SESSION_SECRET`, which must be retained with backups. Google authorization is still an operator step.

Paid telephone calling is deferred. Leave `TWILIO_FROM_NUMBER` and `PUBLIC_BASE_URL` blank. Browser conversations and recording work without a carrier account.

## Verify

```sh
make check
cd web
npx playwright test tests/company.spec.ts tests/workspace.spec.ts tests/matches.spec.ts
RUN_VOICE_E2E=1 RUN_NOTES_E2E=1 npx playwright test tests/voice.spec.ts tests/notes-recording.spec.ts
# Optional live registry check; requires the imported Reformo Networks Oy demo record:
RUN_RESEARCH_E2E=1 npx playwright test tests/research.spec.ts
```

Browser tests require the running application and its local `.env`. Voice tests additionally need the local speech fixture `data/asr-smoke.wav`, speech models, and a working model gateway. They use a recorded source at the microphone boundary; the application audio processing, ASR, model, synthesis, uploads and database paths are real. Physical microphone permission/capture needs a manual check on the demonstration browser.

Research is bounded and reports inaccessible sources and missing information. Public strategy is not a verified buyer mandate; historical announcements are not proof of present buying interest. Matching scores are explainable constraint rankings, not probabilities or valuations. Live Gmail delivery requires Google authorization and has not been tested against a real mailbox.

Technical module contracts are in `backend/*_API.md`. Private planning/research remain in the ignored `docs/` and `research/` directories on this machine.
