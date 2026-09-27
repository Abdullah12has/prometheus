# Buyers API

Directory of private equity, family office and holding company buyers in DK FI IS NO SE CH DE.
All routes need the session cookie; writes also need `X-CSRF-Token`. Code: `permetheus/buyers.py`.

## Rules

- A buyer wraps one company, reused by exact normalized domain (`www.` stripped). The buyer domain is unique:
  `POST /api/buyers` answers 409 `duplicate_buyer`, and discovery counts a repeat as `matched`.
- Facts are proposed `Evidence` rows (`buyer_sector`, `buyer_geography`, `buyer_preference`,
  `buyer_investment_size`, `buyer_exclusion`, `buyer_summary`) with an exact excerpt from a page fetched
  from the buyer's own domain. Review them with the existing `POST /api/evidence/{id}/review`. Rejected facts
  drop out of profile aggregates and search immediately after review.
- The model's output is untrusted. An item is dropped, and the reason goes into `failures`, when its
  `source_url` is not a page in that batch, its quote is not verbatim in that page, an exclusion quote has
  no explicit negation, a history target is missing from its quote or is the buyer itself, or a date is
  invalid, in the future, or not in the quote.
- History `status` is `portfolio`, `acquisition` or `exit`. A portfolio listing is never turned into a dated
  acquisition. `announced_on` is `"YYYY"` or `"YYYY-MM-DD"`, exactly as precise as the source. Rows recorded
  in `historical_deals` for the same company appear with `origin: "deal_history"`.
- An advisor, LP-only investor, service provider or pure VC firm, when a quote shows it, is set to
  `status: excluded` with `exclusion_reason`. Its sources are kept but no buyer facts are stored.
- `PATCH /api/buyers/{id}` writes operator values into `reviewed`. Research never overwrites them. Sending
  `null` for a field returns it to the sourced value.
- Nothing here confirms an identity or a mandate, and nothing here sends outreach.

## Routes

| Method | Path | Result |
|---|---|---|
| GET | `/api/buyers?q=&country=&kind=&status=&offset=0&limit=50` | `{items: Buyer[], total}`. Every `q` term must appear in name/domain/sectors/geographies/preferences/exclusions (literal, case-insensitive). |
| POST | `/api/buyers` `{name, website, country, kind}` | 201 Buyer; research queued |
| GET | `/api/buyers/summary` | `{total, by_country, by_kind, by_status, research_jobs: {queued, running, succeeded, failed}, coverage}` |
| GET | `/api/buyers/discovery/sources` | `[{id, country, label, url, coverage}]`; `[]` until `buyer_sources.py` exists |
| GET | `/api/buyers/discovery/runs` | Run[] |
| POST | `/api/buyers/discovery/runs` `{countries?: [...]}` | 202 new Run, or 200 with the open (queued/running/paused) run. 503 if the catalog is absent, 422 if no source covers the countries |
| POST | `/api/buyers/discovery/runs/{id}/pause` \| `/resume` | Run; 409 when the state does not allow it |
| GET | `/api/buyers/{id}` | BuyerDetail |
| PATCH | `/api/buyers/{id}` | BuyerDetail (operator review; `status: excluded` needs `exclusion_reason`) |
| POST | `/api/buyers/{id}/research` | 202 `{job_id, state}`; returns the active job unchanged while it is queued or running |
| POST | `/api/buyers/{id}/mandate` | 201 `{mandate_id}` (200 if already created). Creates a `public_strategy` mandate with `identity_verified: false` from sourced sector/country facts; 422 `no_sourced_criteria`, 409 for excluded buyers |

Buyer: `id, company_id, name, website, domain, country, kind, status, exclusion_reason, summary, sectors[],
geographies[], preferences[], exclusions[], reviewed_fields[], research_status, last_researched_at,
source_count, history_count`.

BuyerDetail adds `facts[{id, field, value, source_id, source_url, excerpt, review_status}]`,
`history[{id, target_name, status, announced_on, source_url, summary, excerpt, origin, review_status}]`,
`sources[{id, title, url, fetched_at}]`, `failures[]`, `discovered_via[]`, `gaps[]`, `research_error`,
`latest_job{id, state, error}|null`, `mandates[]`.

Run: `id, status (queued|running|paused|completed|failed), countries, source_index, source_total, found,
created, matched, errors[], job_id, job_state, created_at, updated_at, finished_at`.

Contract differences (all additive): extra fields listed above; `status` filter on the list; `PATCH`
review route; history `status` values are `portfolio|acquisition|exit` (deal-history rows keep
`announced|completed|withdrawn`).

## Jobs

`buyer.discover` and `buyer.research` run on `background.buyer_loop` (two lanes), separate from seller
enrichment and registry imports. They use `worker.claim_job`, `research._fence` and `worker._release` with the
same retry/backoff budget.

- Discovery calls `buyer_sources.discover(source_id, beat)` for each source in turn. Each source's
  candidates, its listing `Source` row, the run counters and `source_index` are committed in one fenced
  transaction. A resume therefore starts at the next uncommitted source. A source that raises is recorded
  in `errors` and skipped. Adapters must let exceptions raised by `beat()` propagate. Every new buyer gets
  a queued research job.
- Research crawls the home page (3 pages). It then fetches up to 6 more same-domain links, ranked by path
  words (strategy, criteria, invest, portfolio, acquisitions, holdings, companies, then news/press/about),
  each through `acquisition.research_website` with robots and public-DNS checks. At most 12,000 characters
  are kept per page, stored as an artifact and a `Source`. Model calls are batched at 30,000 characters,
  with at most 20 facts/history items requested per call to stay within the output budget. Results commit in one fenced transaction, together with the job's terminal state.
- Transient network/model failures retain any successful evidence and retry with the shared bounded backoff. Unsupported pages, robots restrictions and an unconfigured model remain visible gaps. Partial extraction failure is never reported as a fully successful run.
- When the app stops (`registry.STOP`), a job is requeued without using up an attempt. Research then
  restarts from scratch, and discovery restarts from its checkpoint.
