# Company discovery coverage

Permetheus imports public registry records, then researches company websites and other accessible public sources. Imported identities are source-backed; selling interest and financial claims still require their own evidence and review.

| Country | Source | Coverage | Refresh and access |
|---|---|---|---|
| Finland | [PRH / YTJ open data](https://avoindata.prh.fi/en/info/swagger-ui) | Finnish Trade Register registered and pending entities; includes historical records. Private traders, phone numbers and email addresses are excluded. | Daily. [Compressed JSON archive](https://avoindata.prh.fi/opendata-ytj-api/v3/all_companies) and [paged API schema](https://avoindata.prh.fi/opendata-ytj-api/v3/schema?lang=en). CC BY 4.0; source attribution retained. |
| Switzerland | [Zefix open linked data](https://register.ld.admin.ch/.well-known/dataset/foj-zefix) | Core identity and address information for active registered legal entities. | Daily. Public [LINDAS SPARQL endpoint](https://lindas.admin.ch/query); ordered keyset paging. |
| Germany | [GLEIF open data](https://www.gleif.org/en/lei-data/gleif-api) | Active entities whose legal address is in Germany and which have a Legal Entity Identifier. This is a subset of German entities, not a complete national register. | Daily Golden Copy CSV, streamed from a local archive when available; filtered API paging otherwise. |

Source probes on 27 September 2026 reported 826,354 records in the Finnish paged API, 794,210 Swiss entities and 240,232 active German-address LEI records. The Finnish daily bulk file is a different export: the downloaded snapshot contained 464,431 records, from which 462,855 companies were added after skipping 1,174 ended records and 402 records without a usable name. Provider counts are not claims about the number of operating companies. Daily updates can change them.

The German CSV checkpoint counts rows in the global archive (3,443,927 in the downloaded snapshot); only matching German active entities are imported. The country company count is shown separately. Replaying an earlier API prefix against the CSV matches existing identifiers instead of duplicating companies.

The [German register portal](https://www.handelsregister.de/rp_web/information/welcome.xhtml) restricts systematic retrieval for parallel registers. Permetheus does not use that portal for bulk population. A wider German dataset requires an appropriately licensed source; it must remain distinguishable from the LEI subset.

Imports use jurisdiction-specific identifiers and retained source links. Repeating a batch must not create duplicate companies. Checkpoints allow a stopped or failed import to resume. Live sources can change during a sweep, so a completed sweep describes the provider data encountered, not a perfect point-in-time national census.

Website research runs separately from imports and is bounded per company. Missing websites, blocked pages, unavailable financials and ambiguous identities are recorded as gaps. Registry availability never implies owner willingness to sell, verified buyer demand or a company valuation.

Public search providers can return quotas or CAPTCHA challenges. Search requests are paced across workers and back off when every provider is unavailable. Website crawling continues where a source URL is known; an unavailable search provider remains a visible research gap. A finished research job means its bounded attempts finished, not that all information about a company was found.

Downloaded archives, database contents, recordings and credentials remain in private local storage and are excluded from Git. The application reports imported counts and enrichment status independently.


## Buyer discovery

The Buyers section covers public direct-investor candidates in Denmark, Finland, Iceland, Norway, Sweden, Switzerland and Germany. Its Region field records the discovery source's market, not a verified headquarters address. International firms can be listed in several association directories; normalized domains deduplicate them and their source memberships are retained.

| Public source | Discovery scope |
|---|---|
| [SVCA](https://www.svca.se/ordinarie-medlemmar/) | Swedish growth-capital and buyout categories; VC and LP-only categories excluded at discovery. |
| [NVCA](https://www.nvca.no/medlem-medlemmer/vare-medlemmer) | Norwegian family-office, growth/buyout and other direct-investor categories. |
| [FVCA](https://paaomasijoittajat.fi/en/members/member-directory/?type=general_partner) | Finnish public member cards and their public detail popups; candidates require website qualification. |
| [Aktive Ejere](https://aktiveejere.dk/alle-medlemmer/) | Danish capital funds and private investment/family-office categories. Its directory currently reports incomplete member display. |
| [BVK](https://www.bvkap.de/der-bvk/mitglieder?gruppe=2) | Paginated ordinary investment members, including international firms active in Germany. Service/associate categories are excluded; ordinary members still require qualification. |
| Official investor websites | Documented starting points fill public-directory gaps, especially Swiss and Icelandic firms and family-owned investors. The versioned source catalog includes each primary URL and its discovery rationale. |

SECA member data is not imported because of its stated usage restriction. Private family offices that do not disclose their existence or portfolio cannot be exhaustively enumerated. These sources are a public coverage set, not a census or a buyer-confirmed mandate.

The first real sweep on 27 September 2026 completed all 12 configured sources and added 347 domain-deduplicated profiles. Two BVK detail pages returned HTTP 500 and were recorded in the run's errors. Website research runs separately and may exclude pure VC, advisors, fund-only investors or service providers. A follow-up Swiss source sweep added 10 more profiles, bringing the directory to 357. Read current counts in Buyers → Discovery; candidate totals include firms awaiting qualification.

Research fetches a bounded selection of the investor's own strategy, criteria, portfolio and news pages. Facts carry exact source excerpts; unstated dislikes remain unknown. Portfolio entries, acquisitions and exits are distinct. A year-only date stays a year. Excerpts and history remain proposed until reviewed; a public-strategy mandate is created only by an explicit operator action and remains unverified.

The crawler checks public DNS and robots policies, handles www aliases after checking the destination policy, bounds compressed and expanded response bytes, and supplies canonical HTTPS Host headers. Scraped bodies remain in ignored local object storage; only source definitions and code are committed.
