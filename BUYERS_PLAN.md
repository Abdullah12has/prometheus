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
