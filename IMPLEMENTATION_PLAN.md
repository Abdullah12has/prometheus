# Interface, voice and discovery upgrade

## Outcomes

1. Replace the dated blue dashboard styling with the supplied reference's quiet, modern workspace style across every screen.
2. Create an agent and its cloned voice in one flow. Fix the actual upload, recording, cloning or preview failure, retain samples on recoverable errors, and make progress visible.
3. Discover and import Finnish, Swiss and German companies from available public sources, then continuously enrich them with attributable evidence. Show the limits of each source and actual progress.

## Design direction

Use a white content canvas (`#ffffff`), pale neutral sidebar (`#fafafa`), soft hover surface (`#f0f0f0`), subtle separators (`#e6e6e6`), charcoal text (`#1a1a1a`) and muted secondary text (`#555555`). Dark primary actions, compact icons, generous content spacing and restrained rounded surfaces replace blue panels and repeated card borders. Reuse the reference's typography where licensed assets are available, otherwise use its system sans fallback. Keep a readable 14–16px body and clear sentence-case headings.

Use a slim left navigation rail and a spacious main workspace. The assistant opens on demand instead of permanently taking a third of the screen. Tables, forms, dialogs and empty states share the same controls. Primary actions appear beside their page title; secondary settings use progressive disclosure. Preserve keyboard focus, labels, contrast, responsive layouts and reduced motion.

Voice creation: name → record or upload sample → preview sample → create and clone → listen to the result. Show a live timer, automatic recording limit, useful microphone errors and clear cloning progress. Remove repeated consent checkboxes and the separate authorization form. Keep a short own/authorized-voice notice and explicit user-triggered recording; do not silently invent consent records. Advanced instructions remain optional. Existing agents can replace their sample without losing their working voice on failure.

## Work sequence

### 1. Evidence and plan

- Audit the reference styles and all current routes.
- Reproduce cloning through the same upload and preview endpoints used by the browser.
- Verify registry endpoints, paging, available fields and source limits using primary documentation and small live requests.
- Preserve existing user data, credentials and prior research.

### 2. Interface pass

- Update shared tokens, shell, navigation, assistant, controls and all page layouts.
- Simplify dense forms without removing existing functionality.
- Add country selection and visible import/enrichment progress to company discovery.
- Inspect desktop and narrow layouts before acceptance.

### 3. Voice repair

- Fix the reproduced cause with a regression check.
- Integrate sample capture and cloning into agent creation, including failure/retry behavior.
- Verify uploaded audio, browser-recorded audio, saved clone reload, generated preview and live conversation.

### 4. Public company population

- Prefer PRH open data for Finland and Zefix open linked data for Switzerland.
- Verify Germany's accessible sources; use a clearly labelled public subset if unrestricted national bulk data is unavailable. Do not bypass access controls or present a subset as a complete register.
- Reuse the durable job and provenance model. Add country-specific identifiers, idempotent imports, checkpoints, backoff and operator pause/resume.
- Import records in batches with source attribution. Queue enrichment separately so discovery does not block behind model calls.
- Run real imports and enrichment, inspect results in all three countries, and record totals, failures and remaining coverage. Keep long imports resumable while the app remains responsive.

### 5. Review, test, fix, commit

- Review each isolated change before integration.
- Run backend regression tests, production build and browser workflow checks.
- Verify the actual local voice runtime and real discovery endpoints, then inspect stored evidence and deduplication.
- Scan Git-visible files for secrets, exclude downloaded datasets and recordings, and commit tested source changes without attribution trailers.
- Leave the app and authorized background import/enrichment work running; report exact completed counts and any ongoing work or external limits.

## Source evidence

- [PRH open data](https://www.prh.fi/en/companiesandorganisations/tietopalvelut/prhopendata.html): free daily company data and compressed download; excludes email and phone fields.
- [Swiss commercial register and Zefix](https://www.bj.admin.ch/de/handelsregister-zefix-und-regix): open linked-data subset contains current core data for active registered legal entities; private REST access is a separate service.
- Endpoint probes and German source coverage will be recorded with the implementation verification results.

## Acceptance

All existing routes remain usable in the new style. Creating an agent with a valid sample ends with a persisted cloned voice and playable generated audio. Failures preserve recoverable input and explain the next action. Country imports store genuine source-backed records without cross-country identifier collisions or duplicate re-imports. Background enrichment creates evidence rather than invented financial or selling-interest claims. Final reporting distinguishes verified results, ongoing bulk work and unavailable data.

## Verification — 27 September 2026

- Implementation committed as `961020e`; the plan was committed before implementation.
- `make check`: 240 backend tests passed, one conditional PostgreSQL concurrency test skipped because its disposable test database was not configured; production TypeScript/Vite build passed. Lint completed without errors (advisory React warnings remain).
- Ten end-to-end browser tests passed against the running local app: intake/edit/owner conditions, matching and an unsent draft, country discovery/search/pagination, every route, browser note transcription, record → clone → generated preview → reload, invalid-sample retry, microphone cancellation, and live ASR → model → speech playback. Recorder-start failure was then added to the microphone cleanup check and passed.
- All seven routes were checked at 1440px and 390px widths, with no horizontal page overflow. Agent creation was separately inspected at both widths. Screenshots remain in ignored local `data/screenshots/`.
- The voice tests replace only the microphone device boundary with a speech recording. Local audio capture/encoding, upload validation, voice model, database profile persistence and generated audio run for real. This verifies the pipeline; physical microphone permissions and perceived voice similarity still depend on the operator's device and sample.
- Live Swiss import pause/resume and application restart preserved the committed checkpoint; shutdown immediately requeued the process's import. A duplicate start returned the existing import. Regression tests also reject stale worker writes and preserve another worker's lease.
- German CSV fields were compared with live API records, including records where validation authority differs from registration authority. Resumed PRH/GLEIF archives are hash-checked before reading checkpointed rows. The German API prefix was replayed against the CSV without duplicate companies.
- Real country imports and enrichment run locally. See `DATA_SOURCES.md` for coverage and the Companies discovery panel for current counts. A completed research job represents bounded source attempts; missing financials and selling interest remain unknown.
- Git-visible files were checked against locally configured secret values and source-repository names. Credentials, source archives, recordings and database contents remain ignored. No live outreach was sent during verification.
