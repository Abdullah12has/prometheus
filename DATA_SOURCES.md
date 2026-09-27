# Company discovery coverage

Permetheus imports public registry records, then researches company websites and other accessible public sources. Imported identities are source-backed; selling interest and financial claims still require their own evidence and review.

| Country | Source | Coverage | Refresh and access |
|---|---|---|---|
| Finland | [PRH / YTJ open data](https://avoindata.prh.fi/en/info/swagger-ui) | Finnish Trade Register registered and pending entities; includes historical records. Private traders, phone numbers and email addresses are excluded. | Daily. [Compressed JSON archive](https://avoindata.prh.fi/opendata-ytj-api/v3/all_companies) and [paged API schema](https://avoindata.prh.fi/opendata-ytj-api/v3/schema?lang=en). CC BY 4.0; source attribution retained. |
| Switzerland | [Zefix open linked data](https://register.ld.admin.ch/.well-known/dataset/foj-zefix) | Core identity and address information for active registered legal entities. | Daily. Public [LINDAS SPARQL endpoint](https://lindas.admin.ch/query); ordered keyset paging. |
| Germany | [GLEIF open data](https://www.gleif.org/en/lei-data/gleif-api) | Active entities whose legal address is in Germany and which have a Legal Entity Identifier. This is a subset of German entities, not a complete national register. | Daily Golden Copy; filtered API paging. |

Source probes on 27 September 2026 reported 826,354 Finnish records, 794,210 Swiss entities and 240,232 active German-address LEI records. These are provider counts at that time, not claims about the number imported or the number of operating companies. Daily updates can change them.

The [German register portal](https://www.handelsregister.de/rp_web/information/welcome.xhtml) restricts systematic retrieval for parallel registers. Permetheus does not use that portal for bulk population. A wider German dataset requires an appropriately licensed source; it must remain distinguishable from the LEI subset.

Imports use jurisdiction-specific identifiers and retained source links. Repeating a batch must not create duplicate companies. Checkpoints allow a stopped or failed import to resume. Live sources can change during a sweep, so a completed sweep describes the provider data encountered, not a perfect point-in-time national census.

Website research runs separately from imports and is bounded per company. Missing websites, blocked pages, unavailable financials and ambiguous identities are recorded as gaps. Registry availability never implies owner willingness to sell, verified buyer demand or a company valuation.

Downloaded archives, database contents, recordings and credentials remain in private local storage and are excluded from Git. The application reports imported counts and enrichment status independently.
