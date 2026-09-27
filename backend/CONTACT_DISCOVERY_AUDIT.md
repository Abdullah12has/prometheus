# Contact discovery audit — 27 September 2026

Public email candidates were found for **22 of 30 companies**, up from **9 of 30** with the previous collection path. Phone candidates increased from **9 to 19 companies**. The local database now contains **177 sourced, unverified contact records** for this sample: 86 email records and 91 phone records. These are company/channel pairs; local and international forms of a phone number can coexist.

## How this was measured

- Sample: 10 companies each from Finland, Switzerland and Germany; five with a website on file and five without, selected by deterministic `md5(id::text)` ordering within each group.
- This is a diagnostic sample, not a country-wide success estimate. The known-website Swiss and German groups largely come from existing buyer seeds and have a stronger web presence than typical registry imports.
- The baseline used frozen acquisition/research modules from `ca1ebd9`. Both versions used the same manifest, real public websites, local SearXNG, bounded production gathering and contact validation. No model was needed to discover the contacts. A separate UI run exercised full enrichment with the configured model.
- Sources and search responses can change between runs. The corrected collection pass found emails for 18/30. Retesting the five German companies without websites after fixing their query brought this to 20/30. Retrying two companies affected by search-engine timeouts brought it to 22/30. The final figure therefore includes retries, not a single uninterrupted pass.
- Success means at least one published, source-attributed candidate. It does not mean SMTP deliverability, current ownership, decision-maker authority or seller interest. Some company pages also list service providers, billing addresses or advisers; candidates remain unverified until their relationship and purpose are reviewed. No outreach was sent.

## Results by country

| Country | Companies | Email before | Email after, including retries | Phone before | Phone after |
|---|---:|---:|---:|---:|---:|
| FI | 10 | 3 | 8 | 3 | 8 |
| CH | 10 | 4 | 7 | 4 | 6 |
| DE | 10 | 2 | 7 | 2 | 5 |

## What was broken and what changed

1. Search crawls collected contact candidates but the enrichment handler discarded them; persistence also considered only website sources. Search-discovered company contacts now survive into the database with their source.
2. There was no contact-specific query. A contact query now precedes financial/workforce research, and discovered company sites can contribute contact subpages. Already-fetched URLs do not spend the search budget again. Exact-name German queries now use “Kontakt”; an English country term plus a multilingual OR clause hid otherwise available results. Embedded quotation marks in legal names are sanitized.
3. Encoded links, organization JSON-LD, PDFs and common email obfuscation were missed. The parser now reads those formats, removes explicitly hidden obfuscation spans and rejects reserved/example email values. A directory’s hidden “null” span had split an otherwise published email address.
4. Company association was too weak for similarly named businesses. Unknown domains require exact legal-name evidence plus a matching company-like host. Finnish directory adapters read only identified company profile sections and exclude shared footers. Domain-matched email snippets retain a visible snippet label.
5. Numeric tables, business IDs, invoice routing IDs and example numbers could become phone contacts. Filters now reject those shapes/context and normalize explicit telephone links, including international numbers containing an optional `(0)`.
6. Source provenance could be lost behind snippets or a truncated text excerpt. Fetched-page candidates take precedence; snippet-only candidates retain their own source. Artifacts retain labelled parsed contact attributes. Repeat discovery fills an absent source without changing an existing verification decision.

For example, the German foundation’s [published contact page](https://www.ludwigskirche.de/stiftung-ludwigskirche/ansprechpartner) was discoverable with its exact name and “Kontakt”. C2Point’s UI enrichment recovered both email and phone through a public directory profile, despite having no website on file.

## Remaining gaps

| Company | Observed limitation in the checked sources |
|---|---|
| Ohjelmatoimisto Ja - He Oy | No website on file; six retained sources did not yield an attributable email/phone. One directory domain failed DNS resolution. |
| Sähkö Amber Oy | No website on file; eight retained sources yielded no eligible contact candidates. |
| Restaurant Des Sportifs Les Bugnenets Sàrl | No website on file; several directory requests returned 403, 405 or 429. Three retained sources yielded no contacts. |
| Digitalbüro Puma | No website on file; directory access was partly blocked and a search provider degraded. Four retained sources yielded no eligible contacts. |
| Geissbühler GmbH | No website on file; directory access included 403 responses and a TLS certificate failure. No contact could be safely associated from the retained sources. |
| Friedrichs & Junker GbR | No website on file and no relevant search results retained. One search engine also reported a suspended timeout. |
| MS "Katharina Sibum" GmbH & Co. KG | No website on file; retained registry/directory pages did not publish eligible contacts. One directory returned 403. |
| MIW - Unternehmensberatung GmbH | No website on file; retained pages did not yield contacts. Other directories returned 403 or redirected to different hosts. |

These are gaps in the checked sources, not proof that no contact exists. Actual page denials, robots exclusions, TLS failures and unrelated-host redirects remain visible; the crawler does not bypass them or invent email permutations. Previously saved contacts survive later provider failures. HQ Equita and BGG both recovered a domain-matched public email on retry, though their original websites redirected to other hosts.

## Company-by-company results

Counts below are unique candidate values within each company/run. They can include generic company channels, individual contacts, billing channels and third parties mentioned on the company site.

| Country | Company | Website initially known | Emails before → after | Phones before → after |
|---|---|---|---:|---:|
| FI | Asunto Oy Joensuun Kaisla | Yes | 0 → 1 | 0 → 1 |
| FI | W Pop Oy | Yes | 8 → 8 | 37 → 7 |
| FI | Milena Training Oy | Yes | 6 → 6 | 13 → 4 |
| FI | Re-Po Rahoitus Oy | Yes | 4 → 2 | 7 → 5 |
| FI | Rensa Ab Oy | Yes | 0 → 1 | 0 → 1 |
| FI | Ohjelmatoimisto Ja - He Oy | No | 0 → 0 | 0 → 0 |
| FI | C2Point Oy | No | 0 → 1 | 0 → 1 |
| FI | Lievestuoreen Tarjous-Sähkö Oy | No | 0 → 1 | 0 → 1 |
| FI | Kurikan Vesihuolto Oy | No | 0 → 3 | 0 → 13 |
| FI | Sähkö Amber Oy | No | 0 → 0 | 0 → 0 |
| CH | CC Trust Group AG | Yes | 1 → 1 | 2 → 2 |
| CH | EGS Beteiligungen AG | Yes | 7 → 3 | 26 → 1 |
| CH | Jacobs Capital | Yes | 0 → 2 | 0 → 4 |
| CH | Capital Transmission | Yes | 6 → 6 | 11 → 12 |
| CH | Capvis | Yes | 2 → 3 | 4 → 3 |
| CH | Restaurant Des Sportifs Les Bugnenets Sàrl | No | 0 → 0 | 0 → 0 |
| CH | Garage Lutz | No | 0 → 4 | 0 → 3 |
| CH | Digitalbüro Puma | No | 0 → 0 | 0 → 0 |
| CH | A Table in a Box SNC | No | 0 → 2 | 0 → 0 |
| CH | Geissbühler GmbH | No | 0 → 0 | 0 → 0 |
| DE | HAUCK AUFHÄUSER LAMPE PRIVATBANK AG | Yes | 0 → 7 | 0 → 11 |
| DE | IBB Beteiligungsgesellschaft mbH | Yes | 14 → 14 | 2 → 1 |
| DE | HQ Equita GmbH | Yes | 0 → 1 | 0 → 0 |
| DE | Borromin Capital Management GmbH | Yes | 15 → 15 | 19 → 15 |
| DE | BGG Bayerische Garantiegesellschaft mbH für mittelständische Beteiligungen | Yes | 0 → 1 | 0 → 0 |
| DE | Friedrichs & Junker GbR | No | 0 → 0 | 0 → 0 |
| DE | Stiftung Ludwigskirche | No | 0 → 3 | 0 → 5 |
| DE | SUITS U GmbH | No | 0 → 1 | 0 → 1 |
| DE | MS "Katharina Sibum" GmbH & Co. KG | No | 0 → 0 | 0 → 0 |
| DE | MIW - Unternehmensberatung GmbH | No | 0 → 0 | 0 → 0 |

## Verification and reproduction

- Backend: `uv run pytest backend/tests -q` — 308 passed, one skipped.
- Web: `cd web && npm run build` — passed; existing bundle-size warning remains.
- UI: signed in, opened C2Point, started Refresh research, observed the completed run and its contact-discovery activity, then verified the email, phone and source links under People. The run collected ten sources; provider/access gaps remained visible.
- Tests cover search-only contact persistence, wrong-company rejection, directory footer exclusion, snippet/page provenance, deduplication, source-less contact enrichment, hidden spans, encoded/structured/PDF contacts and false phone patterns.
- Review fixes were applied and the final full backend suite passed. No credentials or raw audit datasets are committed.

```sh
uv run python scripts/contact_audit.py --manifest data/contact-audit/sample.json --output data/contact-audit/recheck.json
```

The command is read-only with respect to database contacts unless `--save-contacts` is supplied. Source text artifacts are stored locally in either mode. The saved manifest, frozen baseline modules, per-run JSON and logs remain under ignored `data/contact-audit/`. `combined-final.json` uses the corrected German subset and timeout retries to assemble the 30 final observations. `--baseline-dir data/contact-audit` reproduces the frozen baseline; baseline runs cannot save contacts.

UI screenshot: `data/screenshots/contact-discovery.png` (local, ignored by Git).
