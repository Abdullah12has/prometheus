# Buyers directory and research

Add one Buyers destination beside Companies, using the same quiet workspace style. The list supports name/strategy search, country and firm-type filters, and clear research status. Each profile brings together sourced investment preferences, explicit exclusions, portfolio/deal history, sources and unanswered questions. Discovery controls stay behind one expandable section.

## Scope

Cover Denmark, Finland, Iceland, Norway, Sweden, Switzerland and Germany. Discover publicly listed private equity firms, family offices and family-owned investment holding companies that directly invest in businesses. Association membership is a lead, not proof of current buyer demand. Advisors, service providers and passive fund investors must not become qualified buyers merely because they appear in a directory. Private family offices without public disclosure cannot be exhaustively enumerated.

## Implementation

1. Inspect public association directories and official firm websites; document access, paging and coverage. Scrape allowed public pages with the existing DNS-safe, robots-aware acquisition functions. Use official-site starting points where no public directory exists. Retain discovery provenance and errors.
2. Add indexed buyer profiles linked to existing Company records, deduplicated by normalized website domain. Reuse Source, Evidence, durable Job processing and public-strategy mandates. Preserve source timestamps and distinguish unknown information from stated exclusions.
3. Run resumable directory discovery and separate profile-research jobs. Crawl relevant strategy, investment criteria, portfolio and news pages. Extract attributed facts and history; reject unsupported citations, guessed deal dates and claims about rejected sectors without explicit evidence. Portfolio membership alone does not prove a completed acquisition or its date.
4. Build list/detail pages with add, discover, pause/resume and research-again actions. A reviewed public strategy can be explicitly added to matching; scraping never confirms an active mandate or authorizes outreach.
5. Test deduplication, source validation, retry/fencing, filtering and browser flows. Run real regional discovery and research, verify examples from each country, document counts and inaccessible sources, then commit tested changes. Leave durable research running where a larger discovery queue remains.

## Verification criteria

Real firms and source links appear in the Buyers section; search finds indexed profile information; repeat discovery does not duplicate firms; profile claims and history link to fetched evidence; unavailable preferences remain unknown; all seven countries have a visible coverage/result state. Existing companies, voice and deal flows remain usable. Secrets and scraped page bodies stay in ignored local storage, and no external outreach is sent during development.


## Implemented and verified

- Added Buyers navigation, searchable list, regional/type filters, sourced profile, discovery progress, pause/resume, research retry and explicit public-strategy creation. Added the supplied logo to the sidebar, sign-in screen and browser tab.
- Connected the durable discovery and research workers to the real database and model gateway. Initial discovery produced 347 domain profiles; a researched Swiss supplement added 10 more. Public-source gaps and excluded candidates remain visible.
- Browser verification covers the real populated module, modal focus/keyboard behavior, responsive layout, cited evidence and logo rendering. Mocked UI tests separately exercise deterministic failure, pause/resume and action flows.
- Fixed shared crawl failures caused by HTTPS Host headers, www redirects and forced gzip responses. Added bounds and regressions, plus model-output limits, transaction/date validation, immediate evidence-review indexing and transient failure backoff.
- Delegated backend implementation to Claude Opus 5.5 and the bounded frontend task to Cursor's Sonnet 5 thinking model; reviewed and integrated both actual diffs. Native subagents independently reviewed contracts/data handling and researched primary sources. No outreach was dispatched.

Verification on 27 September 2026: 269 backend tests passed, one conditional test skipped; production build passed. Nine browser checks passed after integration (company, workspace, matches, assistant, buyers, registry research and real UI enrichment), plus two audio-capture checks. The six additional discovery/voice/clone/notes browser checks passed earlier in this implementation cycle. These public sources cannot establish an exhaustive list of undisclosed family offices or buyer-confirmed demand.

The first buyer research batch drained: 357 profiles, 296 successful research jobs and 61 failed jobs with their errors retained. Profile classification is separate: 263 profiled, 66 candidates and 28 excluded. Durable workers remain available for new work and explicit retries.

Company enrichment was also tested through the real UI on Vincit Oyj. Added persisted live stages, source counts and event history; fixed queued/running labels and background-refresh page flicker; prioritized manual jobs over bulk research. The real run collected seven sources and retained visible financial and owner-intent gaps. A repeat UI run also passed. Progress writes obey the existing worker lease and cancellation checks, and retries clear old progress.
