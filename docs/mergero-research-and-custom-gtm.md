# Mergero Research and Customized GTM Engineering Proposal

**Research date:** 26 September 2026  
**Status:** Working brief for the marketing hackathon  
**Purpose:** Document what is known about Mergero, distinguish established capabilities from gaps, and define a solution that improves business-owner acquisition and outreach without duplicating MGX.

## 1. Executive summary

Mergero is a technology-enabled M&A advisory firm operating across the Nordics and DACH. Its existing MGX Deal Engine already structures mandates, matches acquisition criteria with opportunities, tracks owner intent and readiness, and gives verified buyers a private deal-flow workflow.

The hackathon opportunity is therefore **not** to build another M&A marketplace, matching engine, or internal data-processing layer. Based on the challenge brief and direct Q&A with Mergero, the operational backlog is upstream:

1. Finding the complete universe of eligible owner-led businesses.
2. Acquiring reliable owner contacts and financial information.
3. Reaching owners through the appropriate regional channel.
4. Personalizing outreach at a scale that the current manual process cannot support.
5. Capturing responses and follow-up timing so each contact improves future outreach.

The proposed product is a **customized GTM engineering system for M&A origination**. It converts an active buyer mandate into a continuously refreshed prospect universe and a compliant, human-supervised multichannel outreach operation. It should feed qualified conversations and structured owner-intent data into MGX rather than replace MGX.

## 2. Evidence standards

Statements in this document use four evidence categories:

- **Public fact:** Confirmed by Mergero or another named public source.
- **Challenge brief:** Confirmed by the Mergero hackathon brief supplied to the team.
- **Mergero Q&A:** Reported by the team after direct discussion with a Mergero representative.
- **Working hypothesis:** A design assumption that still requires validation with Mergero.

This distinction matters because MGX's internal data model, integrations, vendor licenses and outreach tooling are not publicly visible.

## 3. What is known about Mergero

### 3.1 Positioning and business model

- **Public fact:** Mergero describes itself as an institutional M&A advisory firm combining human advisory execution with proprietary origination and matching technology.
- **Public fact:** Its clients include private equity funds, corporations and business owners.
- **Public fact:** Its services include sell-side advisory, customized deal sourcing, subscription access to MGX and full buy-side advisory.
- **Public fact:** Mergero emphasizes discretion, human judgment, systematic access and faster execution rather than positioning itself as a public marketplace.
- **Public fact:** The firm was founded in Finland in 2019. Public company profiles describe a privately held organization with 11–50 employees, while Mergero's site identifies a compact leadership, advisory, analyst and engineering team.

### 3.2 Geography and company profile

- **Public fact:** Mergero lists offices in Helsinki, Frankfurt, Zürich and Lugano and markets pan-European coverage.
- **Mergero Q&A:** Current practical origination focus is Switzerland, DACH and the Nordics.
- **Mergero Q&A:** In the Nordics, the relevant target segment is generally companies with approximately 5–20 employees.
- **Mergero Q&A:** In DACH, the relevant target segment is generally companies with approximately 20–40 employees.
- **Mergero Q&A:** The target universe is industry-agnostic. A company becomes relevant when it matches real buyer demand.
- **Public fact:** Mergero's published transaction list is diverse across industrial services, construction, software, logistics, health, consumer and other sectors.
- **Public observation:** All 46 selected transactions currently visible across Mergero's six public deal-list pages are labelled Nordics. This does not prove an absence of DACH activity because Mergero states that the list is selective, but it supports treating scalable DACH origination as an important expansion problem.

### 3.3 Scale and traction

Mergero's current website reports:

- More than 2,000 verified buyers.
- More than €52 billion of aggregate buyer appetite.
- More than €500 million of deals closed.
- Eighteen transactions in the first half of 2026 and approximately 50% growth ahead, according to the hackathon brief.
- Hundreds of owner conversations per month, according to the MGX product page.

These numbers are company-reported and have not been independently audited for this brief.

### 3.4 What MGX already does

MGX is Mergero's private deal-flow infrastructure for verified professional acquirers. Publicly documented capabilities include:

- Silent, anonymized off-market mandates.
- Structured sell-side mandates.
- Acquisition-criteria-based matching.
- Saved searches, alerts, favorites and shortlists.
- A matched deal feed and weekly new-deal flow.
- Structured business descriptions, sector and geography.
- Revenue and EBITDA range indicators.
- Owner intent and readiness assessments.
- Mandate history, next steps and Deal Advisor support.
- A Soft Launch process for testing buyer appetite before a full sale process.

Mergero's technology partner Empirica states that MGX combines information created during M&A work, uses AI to process large amounts of information and surfaces potentially relevant companies and buyers. Human professionals retain responsibility for assessment, interaction, negotiation and closing.

### 3.5 What the challenge is actually asking for

- **Challenge brief:** Mergero already has substantial company and buyer data, proprietary technology and MGX.
- **Challenge brief:** Origination is still relationship-driven and limits international scaling.
- **Mergero Q&A:** Internal processing and automation are not the main bottlenecks.
- **Mergero Q&A:** Collecting target-company data and reaching business owners remain largely manual and form the operational backlog.
- **Mergero Q&A:** The minimum useful company profile consists of contact information, financial information and the owner's current willingness to consider a transaction. Other attributes are useful but secondary.

## 4. What not to build

The solution should not be presented primarily as:

- A new M&A marketplace.
- A replacement for MGX.
- A generic AI buyer–company matching engine.
- Another CRM for internal deal execution.
- A static list of private companies.
- A generic message-writing chatbot.
- An autonomous mass-messaging bot.
- A system whose novelty depends on scraping LinkedIn.

These approaches either duplicate Mergero's current capabilities, reproduce mature third-party products, or introduce legal and reputational risk without solving the core backlog.

## 5. Proposed solution: Mergero GTM Engine

### 5.1 Product statement

> Mergero GTM Engine continuously builds the reachable universe of owner-led businesses in the Nordics and DACH, enriches each company with financial and owner-contact data, and turns active buyer demand into personalized, region-appropriate outreach. Every response updates the owner's intent and next action, producing qualified conversations for MGX.

### 5.2 Why this improves the current system

MGX operates on qualified mandates and owner conversations. The GTM Engine improves the stage before qualification:

```text
Active buyer mandate
        ↓
Eligible company universe
        ↓
Verified financial and contact data
        ↓
Buyer-specific prioritization
        ↓
Personalized regional outreach
        ↓
Owner response and intent captured
        ↓
Qualified opportunity passed to MGX
```

The defensible asset is not the purchased company record. It is Mergero's growing, time-stamped knowledge of who the real owner is, which channel reaches them, what proposition generated a response and when they may be willing to transact.

## 6. Core workflow

### 6.1 Acquisition campaign input

A Mergero adviser enters or imports an active buyer mandate containing:

- Geography.
- Employee, revenue and profitability ranges.
- Required and excluded business characteristics.
- Buyer identity visibility rules.
- Strategic rationale and value proposition.
- Preferred languages.

The system can use MGX criteria when available, but the prototype must also accept a standalone structured or natural-language mandate because live MGX API access is not confirmed.

### 6.2 Build and refresh the prospect universe

The system retrieves eligible companies from licensed data providers and official registries, resolves duplicates using national business identifiers, and records source and freshness for every material field.

Priority data:

1. Legal identity and registration number.
2. Country, location and employee count.
3. Revenue, EBITDA or operating profit, margin and reporting period.
4. Owner and decision-maker identity.
5. Verified business email, phone number and LinkedIn profile URL.
6. Previous Mergero contact, opt-out and current owner-intent state.

Additional web research, industry descriptions, news and trigger events are optional enrichment and should not block outreach when the minimum profile is complete.

### 6.3 Prioritize outreach

Ranking should combine:

- Hard buyer-mandate fit.
- Financial fit.
- Contact confidence.
- Data freshness.
- Previous interaction and suppression status.
- Current owner intent and follow-up date.
- Expected reachability through the region's preferred channel.

The ranking is an outreach queue, not a replacement for MGX's strategic matching.

### 6.4 Generate personalized outreach

The system generates an adviser-ready outreach package containing:

- A concise explanation of why the company fits the active mandate.
- A sourced company fact that makes the message specific.
- A region-appropriate email, call opener or LinkedIn draft.
- A short call brief with likely owner and financial context.
- A recommended sequence and follow-up date.

All generated claims must be traceable to their source. Unverified financial estimates must be labelled or excluded.

### 6.5 Capture the outcome

The adviser records or imports the outcome of each call or message. AI may summarize the conversation and propose structured updates, but a human approves sensitive interpretations.

Required owner-intent states:

- Not contacted.
- Attempted; no connection.
- Unknown after contact.
- No current interest.
- Revisit at a specified time.
- Open to an introductory discussion.
- Open to an anonymous market test.
- Actively considering a transaction.
- Already represented or in a process.
- Do not contact.

Every status must retain date, source channel, confidence, adviser, notes and next action. Intent is temporal and must never be treated as a permanent yes/no attribute.

## 7. Regional channel playbooks

### 7.1 Nordics

Based on Mergero's experience, the default sequence is:

1. Personalized email.
2. LinkedIn DM or connection where appropriate.
3. Human call follow-up.

Messages should be concise, transparent and tied to a real buyer thesis. The system should optimize for replies and qualified conversations, not send volume.

### 7.2 DACH and Switzerland

Based on Mergero's experience, the default sequence is:

1. Human phone call in the owner's local language.
2. Personalized email following an interaction or valid outreach basis.
3. LinkedIn follow-up where appropriate.

The product should create prioritized call queues, local-language scripts, company briefs and follow-up drafts. It should not pretend that DACH can be scaled through unrestricted cold-email automation.

### 7.3 WhatsApp

WhatsApp may be offered only when there is permission, an existing relationship or another appropriate basis. It should be a follow-up channel rather than a cold mass-outreach channel.

## 8. Data acquisition strategy

### 8.1 Buy the commodity layer

The fastest credible route is a hybrid data stack:

- **Official registries:** Canonical legal entities, business identifiers and registry changes.
- **Licensed company-data providers:** Normalized financials, employee counts, owner/decision-maker contacts and refresh feeds.
- **Company websites and permitted public sources:** Business descriptions and corroborating contact details.
- **Mergero's own interactions:** Owner willingness, relationship history and next-contact timing.

Vainu is a strong candidate for Nordic company, financial and contact data. Dealfront or an equivalent DACH-focused provider is a candidate for German-speaking markets. Inven, Grata and SourceScrub are relevant benchmarks, but vendor selection should be based on a sample-quality test against Mergero's exact target segments.

### 8.2 Do not rely on unrestricted scraping

- Finland's PRH open data provides daily basic company information but not phone numbers or email addresses.
- Switzerland's Zefix provides official registry data but not a ready-made bulk owner-contact and financial dataset.
- German company registers provide filings and accounts, but not a simple outreach-ready contact universe.
- LinkedIn prohibits unauthorized scraping and automated messaging.

The prototype may crawl ordinary public company websites when permitted, but the production business case should assume licensed data for coverage, reliability and defensibility.

### 8.3 Vendor bake-off

Before choosing a production provider, test each candidate on the same stratified sample of Nordic and DACH target companies. Measure:

- Company coverage.
- Latest financial-period coverage.
- Owner/decision-maker identification accuracy.
- Valid email rate.
- Valid phone rate.
- Update latency.
- API and export usability.
- Permission to use data for the intended outreach workflow.
- Cost per usable, contactable company.

## 9. Customized GTM engineering architecture

The implementation should be modular so Mergero can replace vendors without rebuilding the workflow.

### 9.1 Data layer

- Provider connectors and scheduled refresh jobs.
- Canonical company, person, financial and source records.
- Entity resolution by national registration number and domain.
- Field-level provenance, timestamp and confidence.
- Cross-channel opt-out and suppression controls.

### 9.2 Campaign layer

- Buyer mandate and target criteria.
- Eligibility filters and prioritization scores.
- Regional sequence policy.
- Language and adviser assignment.
- Daily call and message queues.

### 9.3 Personalization layer

- Source-grounded company summary.
- Buyer-specific rationale.
- Email, call and LinkedIn drafts.
- Human approval rules.
- Prohibition on unsupported valuation or buyer claims.

### 9.4 Learning layer

The learning loop supports automated outreach rather than replacing data acquisition as the main value proposition. It measures which combination of company attributes, channel, message angle, timing and adviser produces:

- A successful connection.
- A reply.
- An owner conversation.
- Positive or future intent.
- A meeting.
- A mandate.

Recommendations should initially use transparent rules and reporting. Predictive models are justified only after Mergero has enough labelled outcomes.

### 9.5 MGX boundary

The GTM Engine should exchange:

- Buyer criteria from MGX or a neutral import format.
- Qualified company and contact records.
- Outreach history and owner-intent observations.
- Next actions and assigned adviser.

The prototype should implement CSV/JSON import and export. A direct MGX API integration is a later step because the hackathon brief does not promise API access.

## 10. Hackathon prototype

The demo should use real company data, but it should not contact real owners without an approved campaign.

### 10.1 Demonstration flow

1. Import one realistic buyer mandate.
2. Load real, lawfully sourced Nordic and DACH company samples.
3. Show which records are complete, stale or missing contacts/financials.
4. Enrich and deduplicate the companies.
5. Rank the best prospects for the mandate.
6. Produce a Nordic email-first package and a DACH call-first package.
7. Simulate a reply or call transcript.
8. Extract the owner's intent and next follow-up for human approval.
9. Export the qualified record in an MGX-ready format.

### 10.2 Minimum screens

- Coverage dashboard.
- Buyer mandate and criteria view.
- Ranked owner-outreach queue.
- Company profile with sources and freshness.
- Personalized outreach package.
- Interaction and intent timeline.
- Campaign performance dashboard.

## 11. Success metrics

### Data acquisition

- Percentage of eligible companies discovered.
- Percentage with current financials.
- Percentage with a verified owner or decision-maker.
- Valid email and phone rates.
- Cost per outreach-ready company.
- Time required to build a new market universe.

### Outreach

- Connection and reply rate by country and channel.
- Qualified owner conversations per 100 companies.
- Positive or future-intent classifications per 100 conversations.
- Meetings and mandates per 100 companies.
- Time and cost per qualified conversation.
- Opt-out, complaint and incorrect-contact rates.

The north-star metric is **new mandates per 100 eligible companies**, with qualified owner conversations as the leading indicator.

## 12. Compliance and trust requirements

- Maintain country- and channel-specific outreach policies reviewed by qualified counsel.
- Store provenance and a lawful-processing basis for personal contact data.
- Enforce a single suppression list across email, calls, LinkedIn and WhatsApp.
- Provide simple opt-out handling and retain evidence of the request.
- Require human approval for initial campaigns and sensitive owner-intent updates.
- Avoid automated LinkedIn scraping or messaging.
- Do not expose buyer identity unless permitted by the mandate.
- Do not send unsupported claims, fabricated personalization or unverified financial figures.
- Apply role-based access and audit logging because M&A activity is confidential.

## 13. Assumptions and questions to validate

### Current working assumptions

- Company, contact and outreach information is partial or scattered rather than uniformly complete.
- Mergero can provide sample mandate formats but not confidential client data.
- Live MGX API access is not guaranteed for the hackathon.
- Mergero wants humans to retain judgment and conduct calls.
- Real owner outreach during the hackathon requires separate approval.

### Questions for Mergero

1. Which company and contact-data providers are already licensed?
2. What percentage of the eligible Nordics and DACH universe already exists in Mergero systems?
3. Where do call, email, LinkedIn and WhatsApp histories live today?
4. Is owner intent structured or stored mainly in free-text notes?
5. Can MGX import or export company, contact and interaction records?
6. Which steps consume the most staff time: company discovery, contact acquisition, research, personalization, calling, follow-up or data entry?
7. What legal and compliance playbooks already exist for each country and channel?
8. What conversion metrics can Mergero currently provide by region and channel?
9. What exactly counts as a new mandate for hackathon evaluation?

## 14. Sources

### Mergero

- [Mergero homepage](https://mergero.com/)
- [About Mergero](https://mergero.com/about-us/)
- [MGX Deal Engine](https://mergero.com/mgx-deal-engine/)
- [For Professional Acquirers](https://mergero.com/for-acquirers/)
- [For Business Owners](https://mergero.com/for-owners/)
- [Sell-Side Advisory](https://mergero.com/services/sell-side-advisory/)
- [Selected transactions](https://mergero.com/deals/)
- [Empirica case study: AI Makes M&A Target Search More Efficient](https://www.empirica.fi/en/customers/mgx/)
- Mergero hackathon challenge brief supplied by the team.
- Team notes from direct Q&A with a Mergero representative.

### Company data and sourcing benchmarks

- [Vainu company and contact data](https://www.vainu.com/contact-data/)
- [Vainu Developer Hub](https://developers.vainu.com/)
- [Dealfront trigger events](https://help.dealfront.com/en/articles/6923178-trigger-events)
- [Inven](https://www.inven.ai/)
- [Grata](https://grata.com/)
- [SourceScrub](https://www.sourcescrub.com/)
- [Finnish PRH open data](https://www.prh.fi/en/companiesandorganisations/tietopalvelut/prhopendata.html)
- [Swiss Federal Office of Justice: Zefix](https://www.bj.admin.ch/de/handelsregister-zefix-und-regix)
- [LinkedIn User Agreement](https://www.linkedin.com/legal/user-agreement)

### Outreach regulation reference points

- [EU General Data Protection Regulation](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32016R0679)
- [German Act Against Unfair Competition, Section 7](https://www.gesetze-im-internet.de/englisch_uwg/englisch_uwg.html)
- [Swiss Federal Office of Communications: mass sending](https://www.bakom.admin.ch/en/when-is-mass-sending-allowed)
- [Finnish Data Protection Ombudsman decision on B2B electronic direct marketing](https://www.finlex.fi/fi/viranomaiset/tietosuojavaltuutettu/2020/702)

This document is a product and research brief, not legal advice.
