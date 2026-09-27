import sys
import types
import uuid

import pytest
from sqlalchemy import func, select

from permetheus import acquisition, buyers, deals, registry
from permetheus.acquisition import PageResult, WebsiteResearch
from permetheus.config import Settings
from permetheus.llm import ModelUnavailable
from permetheus.models import Company, Evidence, Job, JobState, utcnow

HOME = "https://www.nordcap.example/"
PAGES = {
    HOME: ("Nordcap", "Nordcap is a Nordic private equity firm investing in family-owned industrial companies."),
    "https://www.nordcap.example/contact": ("Contact", "Call us any time."),
    "https://www.nordcap.example/investment-strategy": (
        "Strategy", "We invest in industrial technology and business services companies in Sweden and Finland. "
        "We do not invest in real estate or gambling. Enterprise values of EUR 20-150 million."),
    "https://www.nordcap.example/portfolio": (
        "Portfolio", "Portfolio: Alfa Valves, Beta Logistics. In 2021 Nordcap acquired Gamma Tools from its founder."),
}
LINKS = [*PAGES, "https://www.nordcap.example/careers", "https://www.nordcap.example/privacy"]


def fake_site(url, max_pages=5, *, text_excerpt_limit=4000, before_fetch=None, timeout=10.0):
    before_fetch and before_fetch()
    url = url if url in PAGES else url + "/"
    urls = [url, "https://www.nordcap.example/contact"] if url == HOME else [url]
    pages = [PageResult(u, 200, PAGES[u][0], PAGES[u][1][:text_excerpt_limit], [], None, utcnow().isoformat())
             for u in urls[:max_pages] if u in PAGES]
    return WebsiteResearch(url, pages, [], LINKS, [], [], [], len(pages), utcnow().isoformat())


class FakeLLM:
    configured = True

    def __init__(self, payload):
        self.payload, self.inputs = payload, []

    async def extract(self, text, instruction):
        self.inputs.append(text)
        return self.payload


STRATEGY = "https://www.nordcap.example/investment-strategy"
PORTFOLIO = "https://www.nordcap.example/portfolio"
GOOD_PAYLOAD = {
    "classification": {"kind": "private_equity", "is_buyer": True, "reject_reason": None, "source_url": HOME,
                       "quote": "Nordcap is a Nordic private equity firm"},
    "facts": [
        {"field": "sector", "value": "Industrial technology", "source_url": STRATEGY,
         "quote": "We invest in industrial technology and business services companies"},
        {"field": "geography", "value": "Sweden", "source_url": STRATEGY, "quote": "companies in Sweden and Finland"},
        {"field": "geography", "value": "Finland", "source_url": STRATEGY, "quote": "companies in Sweden and Finland"},
        {"field": "exclusion", "value": "Real estate", "source_url": STRATEGY,
         "quote": "We do not invest in real estate or gambling"},
        {"field": "exclusion", "value": "Healthcare", "source_url": STRATEGY, "quote": "industrial technology"},
        {"field": "sector", "value": "Invented", "source_url": STRATEGY, "quote": "a quote that is not there"},
        {"field": "sector", "value": "Elsewhere", "source_url": "https://other.example/", "quote": "industrial"},
    ],
    "history": [
        {"target_name": "Alfa Valves", "kind": "portfolio", "date": None, "source_url": PORTFOLIO,
         "quote": "Portfolio: Alfa Valves, Beta Logistics"},
        {"target_name": "Gamma Tools", "kind": "acquisition", "date": "2021", "source_url": PORTFOLIO,
         "quote": "In 2021 Nordcap acquired Gamma Tools from its founder"},
        {"target_name": "Beta Logistics", "kind": "acquisition", "date": "2099", "source_url": PORTFOLIO,
         "quote": "Portfolio: Alfa Valves, Beta Logistics"},
        {"target_name": "Delta Oy", "kind": "acquisition", "date": None, "source_url": PORTFOLIO,
         "quote": "Portfolio: Alfa Valves"},
    ],
}


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, database_url="sqlite://", root_dir=tmp_path)


@pytest.fixture
def site(monkeypatch):
    calls = []
    monkeypatch.setattr(acquisition, "research_website",
                        lambda url, *a, **k: calls.append(url) or fake_site(url, *a, **k))
    return calls


def _drain(client, llm, settings, limit=20):
    for _ in range(limit):
        if not buyers.process_one(client.app.state.sessionmaker, llm, settings):
            return


def _create(client, **over):
    body = {"name": "Nordcap AB", "website": "https://www.nordcap.example", "country": "SE", "kind": "unknown", **over}
    return client.post("/api/buyers", json=body)


def test_create_reuses_company_by_domain_and_rejects_duplicate(client):
    with client.app.state.sessionmaker() as db:
        db.add(Company(name="Nordcap Holding", name_normalized="nordcap holding", domain="nordcap.example"))
        db.commit()
    r = _create(client)
    assert r.status_code == 201, r.text
    buyer = r.json()
    assert buyer["research_status"] == "queued" and buyer["sectors"] == [] and buyer["summary"] is None
    with client.app.state.sessionmaker() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 1
    assert _create(client, website="nordcap.example/").status_code == 409

    first = client.post(f"/api/buyers/{buyer['id']}/research").json()
    assert client.post(f"/api/buyers/{buyer['id']}/research").json() == first  # idempotent while active
    assert client.post("/api/buyers", json={"name": "X", "website": "x.example", "country": "US"}).status_code == 422
    client.headers.pop("X-CSRF-Token")
    assert _create(client, website="other.example").status_code == 403


def test_research_keeps_only_quoted_facts_and_respects_review(client, settings, site):
    buyer = _create(client).json()
    llm = FakeLLM(GOOD_PAYLOAD)
    _drain(client, llm, settings)

    assert STRATEGY in site and PORTFOLIO in site  # chosen links, not just the breadth-first crawl
    assert not any("careers" in u or "privacy" in u for u in site)
    assert "[source https://www.nordcap.example/investment-strategy]" in llm.inputs[0]

    d = client.get(f"/api/buyers/{buyer['id']}").json()
    assert d["research_status"] == "completed" and d["status"] == "profiled" and d["kind"] == "private_equity"
    assert d["sectors"] == ["Industrial technology"] and d["geographies"] == ["Sweden", "Finland"]
    assert d["exclusions"] == ["Real estate"]  # the unquoted "Healthcare" exclusion is not inferred
    for fact in d["facts"]:
        assert fact["excerpt"] in PAGES[fact["source_url"]][1] and fact["review_status"] == "proposed"
    history = {h["target_name"]: h for h in d["history"]}
    assert set(history) == {"Alfa Valves", "Gamma Tools"}
    assert history["Alfa Valves"]["status"] == "portfolio" and history["Alfa Valves"]["announced_on"] is None
    assert history["Gamma Tools"]["announced_on"] == "2021"
    failures = " ".join(d["failures"])
    for reason in ("quote_not_found", "unknown_source", "exclusion_not_explicit", "future_date", "target_not_in_quote"):
        assert reason in failures
    assert {s["url"] for s in d["sources"]} == set(PAGES)
    assert "preferences: unknown, no sourced fact" in d["gaps"]

    # Search covers sectors, geographies and exclusions; LIKE wildcards are literal.
    assert client.get("/api/buyers", params={"q": "industrial finland"}).json()["total"] == 1
    assert client.get("/api/buyers", params={"q": "real estate", "country": "SE", "kind": "private_equity"}).json()["total"] == 1
    assert client.get("/api/buyers", params={"q": "%"}).json()["total"] == 0
    assert client.get("/api/buyers", params={"country": "DK"}).json()["total"] == 0

    # Operator review wins over later research; re-research adds no duplicates.
    r = client.patch(f"/api/buyers/{buyer['id']}", json={"sectors": ["Industrial automation"]})
    assert r.status_code == 200 and r.json()["reviewed_fields"] == ["sectors"]
    client.post(f"/api/buyers/{buyer['id']}/research")
    _drain(client, llm, settings)
    d2 = client.get(f"/api/buyers/{buyer['id']}").json()
    assert d2["sectors"] == ["Industrial automation"] and len(d2["facts"]) == len(d["facts"])
    assert len(d2["history"]) == 2

    exclusion = next(f for f in d2['facts'] if f['field'] == 'exclusion')
    result = client.post(f"/api/evidence/{exclusion['id']}/review", json={'status': 'rejected', 'reason': 'Superseded strategy'})
    assert result.status_code == 200
    assert client.get(f"/api/buyers/{buyer['id']}").json()['exclusions'] == []
    assert client.get('/api/buyers', params={'q': 'real estate'}).json()['total'] == 0

    s = client.get("/api/buyers/summary").json()
    assert s["total"] == 1 and s["by_country"] == {"SE": 1} and s["research_jobs"]["succeeded"] == 1


def test_mandate_needs_sourced_criteria_and_stays_unverified(client, settings, site):
    assert buyers._country_codes(['Norway, Sweden, Denmark, Finland']) == ['DK', 'FI', 'NO', 'SE']
    assert buyers._country_codes(['Nordic countries']) == ['DK', 'FI', 'IS', 'NO', 'SE']
    assert buyers._country_codes(['DACH']) == ['AT', 'CH', 'DE']
    assert buyers._country_codes(['This is a strategy with no geographic limit']) == []
    buyer = _create(client).json()
    assert client.post(f"/api/buyers/{buyer['id']}/mandate").status_code == 422
    _drain(client, FakeLLM(GOOD_PAYLOAD), settings)
    r = client.post(f"/api/buyers/{buyer['id']}/mandate")
    assert r.status_code == 201
    m = client.get(f"/api/mandates/{r.json()['mandate_id']}").json()
    assert m["evidence_level"] == "public_strategy" and m["identity_verified"] is False
    assert m["criteria"]["countries"] == ["FI", "SE"] and m["criteria"]["industries"] == ["Industrial technology"]
    assert client.post(f"/api/buyers/{buyer['id']}/mandate").json() == r.json()


def test_advisor_is_excluded_with_reason_not_facts(client, settings, site):
    buyer = _create(client).json()
    payload = {**GOOD_PAYLOAD, "classification": {
        "kind": "unknown", "is_buyer": False, "reject_reason": "advisor", "source_url": HOME,
        "quote": "Nordcap is a Nordic private equity firm"}}
    _drain(client, FakeLLM(payload), settings)
    d = client.get(f"/api/buyers/{buyer['id']}").json()
    assert d["status"] == "excluded" and d["exclusion_reason"].startswith("advisor:")
    assert d["facts"] == [] and d["history"] == [] and d["sources"]


def test_unconfigured_model_keeps_sources_and_fails_honestly(client, settings, site):
    buyer = _create(client).json()
    _drain(client, types.SimpleNamespace(configured=False), settings)
    d = client.get(f"/api/buyers/{buyer['id']}").json()
    assert d["research_status"] == "failed" and "llm_not_configured" in d["gaps"]
    assert d["sources"] and d["facts"] == []


def test_partial_model_failure_keeps_facts_and_fails_job(client, settings, site, monkeypatch):
    buyer = _create(client).json()
    monkeypatch.setattr(buyers.worker, "MAX_ATTEMPTS", 1)
    monkeypatch.setattr(buyers, "BATCH_CHARS", 1)

    class PartialLLM:
        configured = True

        async def extract(self, text, instruction):
            if STRATEGY in text:
                return {"facts": [{"field": "sector", "value": "Industrial technology", "source_url": STRATEGY,
                                   "quote": "We invest in industrial technology and business services companies"}]}
            raise ModelUnavailable("temporary extraction failure")

    _drain(client, PartialLLM(), settings)

    detail = client.get(f"/api/buyers/{buyer['id']}").json()
    assert detail["research_status"] == "failed"
    assert detail["sectors"] == ["Industrial technology"]  # successful batch evidence remains useful
    assert "partial" in detail["research_error"].lower()
    assert detail["latest_job"]["state"] == "failed"
    assert "partial" in detail["latest_job"]["error"].lower()


def test_partial_model_failure_retries_and_keeps_evidence(client, settings, site, monkeypatch):
    buyer = _create(client).json()
    monkeypatch.setattr(buyers, "BATCH_CHARS", 1)
    monkeypatch.setattr(buyers.worker, "_backoff_seconds", lambda attempts: 0)

    class RecoveringLLM:
        configured = True

        def __init__(self):
            self.calls = 0

        async def extract(self, text, instruction):
            self.calls += 1
            if self.calls == 1:
                raise ModelUnavailable("temporary upstream failure")
            if STRATEGY in text:
                return {"facts": [{"field": "sector", "value": "Industrial technology", "source_url": STRATEGY,
                                   "quote": "We invest in industrial technology and business services companies"}]}
            return {}

    llm = RecoveringLLM()
    sessionmaker = client.app.state.sessionmaker
    assert buyers.process_one(sessionmaker, llm, settings)
    first = client.get(f"/api/buyers/{buyer['id']}").json()
    assert first["research_status"] == "queued"
    assert first["latest_job"]["state"] == "queued"
    assert first["sectors"] == ["Industrial technology"]

    assert buyers.process_one(sessionmaker, llm, settings)
    second = client.get(f"/api/buyers/{buyer['id']}").json()
    assert second["research_status"] == "completed"
    assert second["latest_job"]["state"] == "succeeded"
    assert second["sectors"] == ["Industrial technology"]
    assert len([fact for fact in second["facts"] if fact["field"] == "sector"]) == 1


def test_dns_fetch_failure_retries_on_next_attempt(client, settings, site, monkeypatch):
    buyer = _create(client).json()
    monkeypatch.setattr(buyers.worker, "_backoff_seconds", lambda attempts: 0)
    successful_fetch = acquisition.research_website
    calls = 0

    def dns_then_success(url, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return WebsiteResearch(url, [], [], [], [], [], ["robots_fetch_failed: dns_resolution_failed"], 0,
                                   utcnow().isoformat())
        return successful_fetch(url, *args, **kwargs)

    monkeypatch.setattr(acquisition, "research_website", dns_then_success)
    sessionmaker = client.app.state.sessionmaker
    llm = FakeLLM(GOOD_PAYLOAD)

    assert buyers.process_one(sessionmaker, llm, settings)
    first = client.get(f"/api/buyers/{buyer['id']}").json()
    assert first["research_status"] == "queued" and first["latest_job"]["state"] == "queued"
    assert any("dns_resolution_failed" in failure for failure in first["failures"])

    assert buyers.process_one(sessionmaker, llm, settings)
    second = client.get(f"/api/buyers/{buyer['id']}").json()
    assert second["research_status"] == "completed" and second["latest_job"]["state"] == "succeeded"


@pytest.fixture
def catalog(monkeypatch):
    calls = []
    sources = {
        "se_dir": {"id": "se_dir", "country": "SE", "label": "SE directory", "url": "https://dir.example/se",
                   "coverage": "members"},
        "fi_dir": {"id": "fi_dir", "country": "FI", "label": "FI directory", "url": "https://dir.example/fi",
                   "coverage": "members"},
        "de_dir": {"id": "de_dir", "country": "DE", "label": "DE directory", "url": "https://dir.example/de",
                   "coverage": "members"},
    }

    def discover(source_id, beat):
        calls.append(source_id)
        beat()
        if source_id == "fi_dir" and len(calls) == 2:
            registry.STOP.set()  # simulated shutdown after the first source committed
            beat()
        if source_id == "de_dir":
            raise RuntimeError("directory layout changed")
        return {"pages": 1, "errors": ["page 2 timed out"] if source_id == "se_dir" else [], "candidates": [
            {"name": "Nordcap AB", "website": "https://nordcap.example", "country": "SE", "kind": "private_equity",
             "source_url": "https://dir.example/se?p=1"},
            {"name": "Nordcap duplicate", "website": "http://www.nordcap.example/", "country": "SE",
             "kind": "private_equity", "source_url": "https://dir.example/se?p=1"},
            {"name": "Outside", "website": "https://outside.example", "country": "US", "kind": "unknown"},
        ] if source_id == "se_dir" else [
            {"name": "Suomi Family Office", "website": "suomifo.example", "country": "FI", "kind": "family_office",
             "source_url": "https://dir.example/fi"},
        ]}

    module = types.SimpleNamespace(SOURCES=sources, discover=discover)
    monkeypatch.setitem(sys.modules, "permetheus.buyer_sources", module)
    yield calls
    registry.STOP.clear()


def test_discovery_checkpoints_resumes_and_queues_research(client, settings, catalog, site):
    assert client.post("/api/buyers/discovery/runs", json={"countries": ["SE", "FI", "DE"]}).status_code == 202
    again = client.post("/api/buyers/discovery/runs", json={"countries": ["SE"]})
    assert again.status_code == 200  # one open run
    run_id = again.json()["id"]
    assert [s["id"] for s in client.get("/api/buyers/discovery/sources").json()] == ["se_dir", "fi_dir", "de_dir"]

    sm = client.app.state.sessionmaker
    assert buyers.process_one(sm, FakeLLM({}), settings)
    run = client.get("/api/buyers/discovery/runs").json()[0]
    assert (run["status"], run["source_index"], run["source_total"], run["created"], run["matched"]) == (
        "queued", 1, 3, 1, 1)  # shutdown kept the first source's checkpoint and candidates
    assert any("page 2 timed out" in e for e in run["errors"]) and any("country outside scope" in e for e in run["errors"])
    registry.STOP.clear()

    with sm() as db:  # drive discovery only; research jobs wait
        db.query(Job).filter(Job.kind == buyers.JOB_KIND_RESEARCH).update({"available_at": utcnow().replace(year=2100)})
        db.commit()
    assert buyers.process_one(sm, FakeLLM({}), settings)
    run = client.get("/api/buyers/discovery/runs").json()[0]
    assert run["id"] == run_id and run["status"] == "completed" and run["source_index"] == 3
    assert catalog == ["se_dir", "fi_dir", "fi_dir", "de_dir"]  # se_dir not re-fetched after resume
    assert any("directory layout changed" in e for e in run["errors"])

    items = client.get("/api/buyers").json()["items"]
    assert [(b["name"], b["kind"], b["research_status"]) for b in items] == [
        ("Nordcap AB", "private_equity", "queued"), ("Suomi Family Office", "family_office", "queued")]
    detail = client.get(f"/api/buyers/{items[0]['id']}").json()
    assert detail["discovered_via"][0]["url"] == "https://dir.example/se?p=1" and detail["source_count"] == 1
    with sm() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == buyers.JOB_KIND_RESEARCH)) == 2


def test_pause_and_resume_run(client, catalog):
    run = client.post("/api/buyers/discovery/runs", json={"countries": ["SE"]}).json()
    paused = client.post(f"/api/buyers/discovery/runs/{run['id']}/pause").json()
    assert paused["status"] == "paused" and paused["job_state"] == "cancelled"
    assert client.post(f"/api/buyers/discovery/runs/{run['id']}/pause").status_code == 409
    resumed = client.post(f"/api/buyers/discovery/runs/{run['id']}/resume").json()
    assert resumed["status"] == "queued" and resumed["job_state"] == "queued" and resumed["job_id"] == run["job_id"]


def test_sources_absent_until_catalog_supplied(client, monkeypatch):
    monkeypatch.setattr(buyers, "_catalog", lambda: None)
    assert client.get("/api/buyers/discovery/sources").json() == []
    assert client.post("/api/buyers/discovery/runs", json={"countries": ["SE"]}).status_code == 503


def test_validate_rejects_invalid_dates_and_foreign_sources():
    page = buyers.research.Fetched(buyers.SourceKind.website, HOME, None, None, utcnow().isoformat(), "x",
                                   "Acquired Omega GmbH on 2020-02-30. Acquired Sigma AG in 2019.", True)
    g = buyers.Gathered()
    buyers.validate({"history": [
        {"target_name": "Omega GmbH", "kind": "acquisition", "date": "2020-02-30", "source_url": HOME,
         "quote": "Acquired Omega GmbH on 2020-02-30"},
        {"target_name": "Sigma AG", "kind": "acquisition", "date": "2018", "source_url": HOME,
         "quote": "Acquired Sigma AG in 2019"},
        {"target_name": "Sigma AG", "kind": "merger", "date": None, "source_url": HOME, "quote": "Acquired Sigma AG"},
    ]}, [page], "Nordcap", g)
    assert g.history == []
    assert [f.split(":")[1].split()[0] for f in g.failures] == ["invalid_date", "date_not_in_quote", "unsupported_item"]
    assert buyers.link_score("https://x.example/portfolio") > buyers.link_score("https://x.example/news") > 0
    assert buyers.link_score("https://x.example/contact") == 0


def test_history_requires_transaction_language_and_quotes_date_precision():
    portfolio = buyers.research.Fetched(
        buyers.SourceKind.website, PORTFOLIO, None, None, utcnow().isoformat(), "x",
        "Portfolio: Beta Logistics and Delta Oy. Nordcap acquired Gamma Tools in 2021. Nordcap sold Sigma AG in 2022.",
        True,
    )
    g = buyers.Gathered()
    buyers.validate({"history": [
        {"target_name": "Beta Logistics", "kind": "acquisition", "date": None, "source_url": PORTFOLIO,
         "quote": "Portfolio: Beta Logistics and Delta Oy"},
        {"target_name": "Delta Oy", "kind": "exit", "date": None, "source_url": PORTFOLIO,
         "quote": "Portfolio: Beta Logistics and Delta Oy"},
        {"target_name": "Gamma Tools", "kind": "acquisition", "date": "2021-06-03", "source_url": PORTFOLIO,
         "quote": "Nordcap acquired Gamma Tools in 2021"},
        {"target_name": "Sigma AG", "kind": "exit", "date": "2022", "source_url": PORTFOLIO,
         "quote": "Nordcap sold Sigma AG in 2022"},
        {"target_name": "Delta Oy", "kind": "portfolio", "source_url": PORTFOLIO, "quote": "Delta Oy"},
    ]}, [portfolio], "Nordcap", g)

    assert [(h["target_name"], h["status"], h["announced_on"]) for h in g.history] == [
        ("Beta Logistics", "portfolio", None), ("Delta Oy", "portfolio", None),
        ("Gamma Tools", "acquisition", "2021"), ("Sigma AG", "exit", "2022"),
        ("Delta Oy", "portfolio", None),
    ]
    assert buyers._check_date("2021-06-03", "The deal closed on 2021-06-03.") == ("2021-06-03", None)


def test_non_200_html_is_not_kept_as_buyer_evidence(settings):
    crawl = WebsiteResearch(
        HOME, [PageResult(HOME, 503, "Maintenance", "Nordcap invests in companies.", [], None, utcnow().isoformat())],
        [], [], [], [], [], 1, utcnow().isoformat(),
    )
    gathered = buyers.Gathered()

    buyers._take(gathered, crawl, "www.nordcap.example", settings)

    assert gathered.pages == []
    assert any("HTTP 503" in failure for failure in gathered.failures)
