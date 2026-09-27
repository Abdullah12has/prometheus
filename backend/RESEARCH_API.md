# Research API

Registry discovery and company enrichment. Modules: `permetheus/acquisition.py`
(network, no database), `permetheus/research.py` (handlers and router),
`permetheus/research_models.py` (tables), `permetheus/worker.py` (job runner).

The integrating app mounts the router with `app.include_router(research.router)`.
All routes require a session, plus the CSRF header on writes.

## Routes

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/companies/{id}/enrichments` | Queue `company.enrich`. Returns the existing queued or running job (200) instead of adding a duplicate. |
| GET | `/api/companies/{id}/coverage` | Latest research run plus `required` and `scope_note` (see below). |
| GET | `/api/research/runs?company_id=` | Run ledgers, one per enrichment attempt. |
| POST | `/api/discovery/runs` | Queue a bounded PRH import. |
| GET | `/api/discovery/runs`, `/api/discovery/runs/{id}` | Discovery progress and checkpoint. |
| GET/PUT | `/api/discovery/schedule` | Daily scheduled discovery. Disabled by default. |
| POST | `/api/jobs/{id}/cancel` | Cancel a queued or running job of any kind. |
| POST | `/api/jobs/{id}/retry` | Requeue the same row of a failed or cancelled job of any kind. |

### Discovery input

`{name?, business_id?, registration_start?, registration_end?, max_pages<=5, max_companies<=100}`.
`business_id` is a checksum-validated Finnish ID, normalized to `1234567-8`. Dates
must be real calendar dates (`2023-02-29` is rejected), with end on or after
start. All fields are passed to the PRH v3 `/companies` search.

A scheduled run imports only the trailing `window_days` of new registrations,
with the same limits (5 pages, 100 companies). It runs at most once per day.

### Coverage

`required` covers `financial.revenue`, `financial.ebitda`, `financial.employees`:
`accepted` (human reviewed), `proposed_unreviewed` (machine extraction) or `missing`.
`owner_intent` is `confirmed` only when a confirmed owner or representative
statement exists, and `unconfirmed` otherwise. The run's `missing` list repeats
these as `required_missing: …` / `required_unconfirmed: …`. `scope_note` says that
coverage lists only the sources that were checked. It never claims the whole
internet was searched.

## Enrichment

1. Registry: exact business ID, or a single exact legal-name match. Ambiguous
   matches are reported as `blocked`. A business ID that already belongs to
   another company is reported as `registry_business_id_in_use` and is not merged.
2. Website: a robots-aware, SSRF-safe crawl of the website on file (or the
   registry website), up to 5 pages.
3. Web search: always runs, even when a website is known. It queries the
   SearXNG instance at `SEARXNG_URL` (`settings.searxng_url`). If none is
   configured, the ledger records `web_search_not_configured`. Up to 3 results
   that mention the target are each fetched through `research_website(url, max_pages=1)`.
   They are kept as `search_result` sources with stored artifacts. Search
   results never change the company's identity fields.
4. Extraction: sources are packed into model inputs of at most 35,000 characters
   each, so nothing hits the model client's 40k truncation. The prompt names the
   exact target (name, business ID, domain), tells the model to ignore
   instructions inside sources, and asks for financials of the target only.
   A proposal is kept only if its quote is a verbatim substring of a fetched
   source and that source identifies the target. Identification means one of:
   registry, own site, or the text contains the business ID, domain or full
   normalized name.
5. Every financial is stored as a `FinancialObservation`, plus an `Evidence` row
   `financial.<metric>` holding the exact quote on the same source. Everything
   starts as `proposed`.

Writes are idempotent. A Source is reused for the same URL and artifact digest,
and Evidence and financials are skipped if identical ones already exist. Nothing
is ever deleted, so reviewed rows survive re-runs and retries.

## Jobs, leases and fencing

- `worker.claim_job(db, kinds=HANDLED_KINDS)`: the default claims only
  `company.enrich` and `discovery.run`. The root runtime passes its own kinds
  for `note.transcribe` / `document.extract`. `process_one` runs research kinds only.
- Fencing token = `Job.attempts` after the claim. It never resets. A manual
  retry records `payload.retry_base`, and the retry budget is `attempts - retry_base`
  (max 5, with backoff of 10 s doubling to a 600 s cap). An expired lease on the
  final attempt fails the job instead of being reclaimed again.
- Handlers commit their run row or checkpoint before any network I/O. They hold
  no write transaction while fetching, and they renew the lease between every
  page, search result and model batch. Results and the job's terminal state
  commit together in one fenced transaction. A stale attempt (reclaimed or
  retried) gets `LeaseLost` and writes nothing. A crash after that commit cannot
  duplicate facts, because the job is already `succeeded`.
- Cancel: the job becomes `cancelled`. For `company.enrich`, the run of the
  current attempt (`ResearchRun.attempt == Job.attempts`) is marked `cancelled`.
  Older reclaimed attempts are left alone, and they are closed as
  `attempt_superseded` when a newer attempt starts. For `discovery.run`, the
  DiscoveryRun is marked `cancelled`. The worker notices at its next lease
  renewal, stops without fetching further pages, and keeps the cancelled status
  with its ledger.
- Retry: the same job row is requeued, never duplicated. Run markers are touched
  only for `discovery.run`, where an unfinished run is set back to `running` and
  resumes from its page checkpoint.

## Crawler safety

`fetch_public_url` pins validated public addresses and bounds size, time and
redirects. By default it follows redirects to other hosts. `research_website` passes
`allow_cross_host_redirects=False`, because robots.txt was checked only for the
original host. A cross-host hop fails with `cross_host_redirect_blocked`. A
same-host redirect into a robots-disallowed path is discarded and listed in
`robots_disallowed`. The `before_fetch` callback runs before each page fetch, and
any exception it raises stops the crawl.

## Known gaps

- Table creation is `create_all` only. The new `research_runs.attempt` column
  needs a manual `ALTER TABLE` on an existing database.
- The target-identity check is textual. A short company name can collide with
  another, which is why every claim stays `proposed` until reviewed.
- Postgres `SKIP LOCKED` claiming has not been exercised by the SQLite test suite.
