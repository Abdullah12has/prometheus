# Registry population API

Modules: `permetheus/registry_sources.py` (network adapters, no database) and
`permetheus/registry.py` (tables, bulk persistence, jobs, router).

All routes: `/api/registry/...`, session required, `X-CSRF-Token` on writes.
Existing `/api/discovery/*` routes (bounded PRH search) are unchanged.

## Sources — `GET /api/registry/sources`

```json
[{"id": "prh_bulk", "country": "FI", "label": "Finnish Trade Register (PRH open data)",
  "publisher": "PRH", "url": "https://avoindata.prh.fi/opendata-ytj-api/v3/all_companies",
  "license": "CC BY 4.0", "identifier": {"jurisdiction": "FI", "scheme": "business_id"},
  "coverage": "...", "limits": "..."}]
```

Ids: `prh_bulk` (FI, full daily bulk file, records with an end date skipped as inactive),
`zefix_lindas` (CH, LINDAS open linked data: active commercial-register entities),
`gleif_de` (DE, **only** active entities holding an LEI with German legal address — explicit
subset, not the German commercial register). `coverage`/`limits` strings are display-ready.

## Imports

`POST /api/registry/imports` body `{"source": "prh_bulk" | "zefix_lindas" | "gleif_de"}`
→ `202` new import, or `200` with the existing queued/running/paused/failed import for that source.
Resume a failed import through its resume endpoint.

`GET /api/registry/imports?source=&limit=50` → list, newest first.
`GET /api/registry/imports/{id}` → one.
`POST /api/registry/imports/{id}/pause` → only queued/running (else 409). Stops after the current
batch; checkpoint kept.
`POST /api/registry/imports/{id}/resume` → paused or failed (else 409). Continues from checkpoint.

`RegistryImportOut`:
```json
{"id": "uuid", "source": "zefix_lindas", "country": "CH",
 "status": "queued|running|paused|completed|failed",
 "job_id": "uuid|null", "job_state": "queued|running|succeeded|failed|cancelled|null",
 "source_total": 794210,          // total the source reports; null until known (PRH: set when file fully read)
 "processed": 12000, "created": 11950, "matched": 50, "skipped": 0,
 "progress": 0.015,               // 0..1 or null
 "snapshot": "2026-09-27",        // file date / golden-copy publish date / query date
 "checkpoint": {...},             // opaque, for display/debug only
 "errors": ["..."],               // most recent (bounded) errors, incl. rate limits / retries
 "exhausted": false,              // true once the source returned its last record
 "coverage": "...", "limits": "...",
 "started_at": "...", "finished_at": null, "updated_at": "..."}
```
`created` = new companies, `matched` = identifier already known (only empty fields filled),
`skipped` = excluded/invalid records (reason counts in `skip_reasons`: `{"inactive": n, "invalid_identifier": n}`).
The German CSV path counts all global file rows in `processed`/`source_total`, including excluded
countries. `created` and the country company count describe the imported German subset.

Import work runs on its own background loop (job kind `registry.import`, visible in `/api/jobs`),
so it never blocks enrichment or media jobs.

## Enrichment campaigns (low-concurrency background enrichment)

`POST /api/registry/enrichment` body `{"country": "FI|CH|DE", "max_in_flight": 2 (1..5), "max_companies": null|int}`
→ `202` new, or `200` existing running/paused campaign for that country.
`GET /api/registry/enrichment` → list.
`POST /api/registry/enrichment/{id}/pause` (running only) — stops feeding and cancels the campaign's
*queued* enrich jobs (running ones finish). `POST /api/registry/enrichment/{id}/resume` (paused only)
— requeues those cancelled jobs and continues from the keyset checkpoint.

`EnrichmentCampaignOut`:
```json
{"id": "uuid", "country": "CH", "status": "running|paused|completed",
 "max_in_flight": 2, "max_companies": null, "enqueued": 40,
 "jobs": {"queued": 2, "running": 1, "succeeded": 35, "failed": 2, "cancelled": 0},
 "exhausted": false, "last_error": null,
 "created_at": "...", "updated_at": "...", "finished_at": null}
```
The feeder keeps at most `max_in_flight` campaign jobs queued+running; it skips companies that
already have a completed research run or a queued/running enrichment job. Each job is a normal
`company.enrich` job (existing coverage ledger via `/api/companies/{id}/coverage`).

## Status — `GET /api/registry/status`

```json
{"countries": [
  {"country": "FI", "companies": 812345,
   "identifiers": {"business_id": 812345},
   "imports": [RegistryImportOut /* latest per source for this country */],
   "sources": ["prh_bulk"],
   "enrichment": {"campaign": EnrichmentCampaignOut | null,
                  "researched_companies": 120,
                  "jobs": {"queued": 2, "running": 1, "succeeded": 110, "failed": 9, "cancelled": 0}}}
 ],
 "registry_worker": true}
```

## Running

- With `WORKER_ENABLED=true` the app runs three registry loops (claiming only `registry.import`)
  and an enrichment feeder every 15 s. Two research workers process enrichment separately.
- Without the app worker: `python -m permetheus.registry` runs imports plus the feeder;
  `python -m permetheus.worker` runs the enrichment jobs.
- Import batches: PRH 1,000 records (streamed from `data/imports/prh/all_companies_YYYYMMDD.zip`,
  reusing a complete local file, resuming partial downloads with HTTP Range); Zefix 5,000 per
  SPARQL page (IRI keyset, 1 s pacing); GLEIF 10,000 global CSV rows per batch from an already-downloaded
  official archive under `data/imports/gleif`, or 200 per API cursor page (1.1 s pacing, 60 req/min).
  429/5xx/transport errors retry in-handler with Retry-After-aware backoff (max 6 tries, 300 s cap),
  then the job backs off (worker policy: 5 attempts, 10 s doubling to 600 s) and resumes from the
  checkpoint. An expired GLEIF cursor restarts from the first page (dedup keeps it safe).
- App shutdown immediately requeues jobs owned by that process, using the last committed checkpoint
  and fencing token. Late writes from the old thread are rejected. Retry budget is preserved.

## Stored data model

- Company: `country` FI/CH/DE, `registry_status` from PRH's trade-register status for FI (unknown
  when absent), "active" for CH/DE, `industry` (FI: TOL 2008 main line, EN),
  `description` (CH: registered purpose), `website` only when the registry states one (PRH). Existing
  non-empty company fields are never overwritten.
- Identifiers (unique jurisdiction+scheme+value): FI `business_id` `1234567-8`;
  CH `business_id` `CHE-123.456.789`; DE `lei` (20 chars, jurisdiction DE).
  Manual intake and search canonicalize Swiss UID punctuation, so `CHE116229879` matches
  `CHE-116.229.879`.
- Evidence per company per snapshot: `field="registry_record"`, `extraction_method="registry_import:<source>"`,
  `value` = normalized registry fields, `locator` = `{"import_id", "record_url"}`; one `Source` (kind
  `registry`) per import/source URL. A switch from API to CSV keeps earlier source attribution.
  Identical re-imports do not add rows.
