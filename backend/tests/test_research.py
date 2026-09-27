"""Enrichment/discovery handlers and research routes, against a file-backed
SQLite (separate connections per session, so a transaction held open across
network I/O would show up as a lock) with every network seam faked."""

import uuid
from datetime import date, datetime, timedelta, timezone
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select

from conftest import PASSWORD
from permetheus import acquisition, research, worker
from permetheus.app import create_app
from permetheus.config import Settings
from permetheus.db import init_db, make_sessionmaker
from permetheus.documents import Document
from permetheus.models import (
    Company, CompanyIdentifier, Evidence, FinancialObservation, Job, JobState, ReviewStatus, Source, SourceKind,
)
from permetheus.notes import Note
from permetheus.research_models import (
    JOB_KIND_DISCOVERY, JOB_KIND_ENRICH, DiscoveryRun, DiscoveryRunStatus, ResearchRun, ResearchRunStatus,
)

NOW = datetime.now(timezone.utc).isoformat()
SEARX = "http://127.0.0.1:9999"
REVENUE_QUOTE = "Acme Oy revenue in 2023 was EUR 1,200,000"


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    init_db(engine)
    settings = Settings(_env_file=None, database_url="sqlite://", root_dir=tmp_path, searxng_url=SEARX)
    yield make_sessionmaker(engine), settings
    engine.dispose()


class FakeLLM:
    configured = True

    def __init__(self, payload=None):
        self.payload = payload or {}
        self.inputs = []

    async def extract(self, text, instruction):
        self.inputs.append((text, instruction))
        return self.payload


def page(url, text, title="Page"):
    return acquisition.PageResult(url=url, status=200, title=title, text_excerpt=text, links=[], error=None, fetched_at=NOW)


def crawl(url, pages):
    return acquisition.WebsiteResearch(root_url=url, pages=pages, contacts=[], internal_links=[], external_links=[],
                                       robots_disallowed=[], errors=[], pages_fetched=len(pages), fetched_at=NOW)


class Net:
    """Fakes for every acquisition call the enrichment handler makes."""

    def __init__(self, monkeypatch, *, site_text="Acme Oy builds boats.", search_results=(), pages=None):
        self.crawls, self.searches = [], []
        self.pages = {"https://acme.fi": site_text, **(pages or {})}
        self.search_results = [acquisition.SearchResultItem(title=t, url=u, content=c, engine="x")
                               for t, u, c in search_results]
        monkeypatch.setattr(acquisition, "prh_search",
                            lambda **kw: acquisition.PRHSearchResult([], 0, 1, None, "https://prh.example/c", NOW))
        monkeypatch.setattr(acquisition, "search_web", self.search_web)
        monkeypatch.setattr(acquisition, "research_website", self.research_website)

    def search_web(self, query, base_url=acquisition.DEFAULT_SEARXNG_BASE_URL, **kw):
        self.searches.append((query, base_url))
        return acquisition.SearchResult(query, base_url, self.search_results, False, None, NOW)

    def research_website(self, url, max_pages=5, *, timeout=10.0, text_excerpt_limit=4000, before_fetch=None):
        self.crawls.append((url, max_pages))
        if before_fetch:
            before_fetch()
        return crawl(url, [page(url, self.pages[url])])


def seed(Session, *, website="https://acme.fi", name="Acme Oy"):
    with Session() as db:
        company = Company(name=name, name_normalized="acme", website=website, domain=website and "acme.fi")
        db.add(company)
        db.flush()
        db.add(Job(kind=JOB_KIND_ENRICH, company_id=company.id, idempotency_key=f"enrich:{uuid.uuid4()}",
                   payload={"company_id": str(company.id)}))
        db.commit()
        return company.id


def claim(Session):
    with Session() as db:
        return worker.claim_job(db)


def enrich(Session, settings, llm, job, fencing):
    with Session() as db:
        research.run_enrich(db, job=job, fencing=fencing, llm=llm, settings=settings)


REVENUE = {"financials": [{
    "metric": "revenue", "amount": "1200000", "currency": "EUR", "period_start": "2023-01-01",
    "period_end": "2023-12-31", "scope": "entity", "status": "reported", "quote": REVENUE_QUOTE,
}]}


def test_known_website_still_searches_configured_searxng_and_keeps_result_sources(env, monkeypatch):
    Session, settings = env
    news = "https://news.example/acme"
    net = Net(monkeypatch, search_results=[
        ("Acme Oy results", news, "Acme Oy grew"),
        ("Unrelated", "https://other.example/x", "nothing here"),
    ], pages={news: "Acme Oy published results today."})
    company_id = seed(Session)
    job, fencing = claim(Session)
    enrich(Session, settings, FakeLLM(), job, fencing)

    assert net.searches == [('"Acme Oy"', SEARX)]
    assert net.crawls == [("https://acme.fi", research.WEBSITE_CRAWL_MAX_PAGES), (news, 1)]  # irrelevant result skipped
    with Session() as db:
        kept = db.scalar(select(Source).where(Source.kind == SourceKind.search_result))
        assert kept.url == news
        assert (settings.data_dir / "artifacts" / f"{kept.content_hash}.txt").read_text() == net.pages[news]
        run = db.scalar(select(ResearchRun).where(ResearchRun.company_id == company_id))
        assert "web_search" in run.checked and "website" in run.checked
        assert db.get(Job, job.id).state == JobState.succeeded
        assert db.get(Company, company_id).website == "https://acme.fi"  # no merge from search results


def test_financial_quote_persisted_as_evidence_on_same_source(env, monkeypatch):
    Session, settings = env
    Net(monkeypatch, site_text=f"About us. {REVENUE_QUOTE}. Thanks.")
    company_id = seed(Session)
    job, fencing = claim(Session)
    enrich(Session, settings, FakeLLM(REVENUE), job, fencing)

    with Session() as db:
        fin = db.scalar(select(FinancialObservation).where(FinancialObservation.company_id == company_id))
        quote = db.scalar(select(Evidence).where(Evidence.field == "financial.revenue"))
        assert fin.review_status == ReviewStatus.proposed
        assert quote.excerpt == REVENUE_QUOTE and quote.source_id == fin.source_id
        assert quote.value["amount"] == "1200000" and quote.value["period_end"] == "2023-12-31"


def test_claims_from_sources_not_naming_target_are_rejected(env, monkeypatch):
    Session, settings = env
    other = "https://blog.example/post"
    Net(monkeypatch, search_results=[("Acme Oy mention", other, "Acme Oy")],
        pages={other: "Beta Ab revenue in 2023 was EUR 9,000,000"})
    seed(Session)
    job, fencing = claim(Session)
    llm = FakeLLM({"financials": [{**REVENUE["financials"][0], "quote": "Beta Ab revenue in 2023 was EUR 9,000,000"}]})
    enrich(Session, settings, llm, job, fencing)

    instruction = llm.inputs[0][1]
    assert '"Acme Oy"' in instruction and "acme.fi" in instruction and "ignore any instructions" in instruction
    with Session() as db:
        assert db.scalar(select(func.count(FinancialObservation.id))) == 0
        run = db.scalar(select(ResearchRun))
        assert any("does not identify the target" in b for b in run.blocked)


def test_extraction_batches_stay_under_limit_and_keep_the_tail():
    fetched = [research.Fetched(SourceKind.website, f"https://acme.fi/{i}", None, None, NOW, "0" * 64,
                                ("x" * 29_990) + f"TAIL{i}", True) for i in range(3)]
    fetched.append(research.Fetched(SourceKind.website, "https://acme.fi/big", None, None, NOW, "0" * 64,
                                    ("y" * 80_000) + "BIGTAIL", True))
    batches = research.extraction_batches(fetched)
    assert all(len(b) <= research.EXTRACTION_BATCH_CHARS for b in batches)
    joined = "".join(batches)
    assert all(f"TAIL{i}" in joined for i in range(3)) and "BIGTAIL" in joined
    assert joined.count("y") == 80_000  # nothing silently dropped


def test_quote_in_tail_of_long_source_still_extracted(env, monkeypatch):
    Session, settings = env
    Net(monkeypatch, site_text=("filler " * 6000) + REVENUE_QUOTE)  # > 35k chars
    seed(Session)
    job, fencing = claim(Session)
    llm = FakeLLM(REVENUE)
    enrich(Session, settings, llm, job, fencing)
    assert len(llm.inputs) >= 2 and all(len(text) <= research.EXTRACTION_BATCH_CHARS for text, _ in llm.inputs)
    assert REVENUE_QUOTE in llm.inputs[-1][0]
    with Session() as db:
        assert db.scalar(select(func.count(FinancialObservation.id))) == 1


def test_cancel_mid_crawl_marks_run_cancelled_and_stops_fetching(env, monkeypatch):
    Session, settings = env
    fetched = []

    def fake_fetch(url, timeout=10.0, **kw):
        if url.endswith("/robots.txt"):
            return acquisition.FetchResult(url, url, 404, "text/plain", {}, b"", "", [], False, NOW)
        fetched.append(url)
        if url == "https://acme.fi":
            with Session() as db:  # the operator cancels while this page is in flight
                research.cancel_job(job.id, db)
        html = '<html><body>Acme Oy <a href="/a">a</a><a href="/b">b</a></body></html>'
        return acquisition.FetchResult(url, url, 200, "text/html", {}, html.encode(), html, [], False, NOW)

    monkeypatch.setattr(acquisition, "prh_search",
                        lambda **kw: acquisition.PRHSearchResult([], 0, 1, None, "https://prh.example/c", NOW))
    monkeypatch.setattr(acquisition, "fetch_public_url", fake_fetch)
    searches = []
    monkeypatch.setattr(acquisition, "search_web", lambda *a, **k: searches.append(a))
    company_id = seed(Session)
    job, fencing = claim(Session)
    with pytest.raises(research.JobCancelled):
        enrich(Session, settings, FakeLLM(REVENUE), job, fencing)

    assert fetched == ["https://acme.fi"] and searches == []
    with Session() as db:
        run = db.scalar(select(ResearchRun).where(ResearchRun.company_id == company_id))
        assert run.status == ResearchRunStatus.cancelled and run.attempt == fencing
        assert run.checked == ["registry", "website"]
        cancelled_job = db.get(Job, job.id)
        assert cancelled_job.state == JobState.cancelled
        assert cancelled_job.payload["progress"]["phase"] == "cancelled"
        assert all(event["phase"] != "completed" for event in cancelled_job.payload["progress"]["events"])
        assert db.scalar(select(func.count(Source.id))) == 0


def test_job_api_exposes_live_progress_before_gather_returns(api, monkeypatch, tmp_path):
    client, Session = api
    Net(monkeypatch)
    company_id = seed(Session)
    job, fencing = claim(Session)
    entered, release = Event(), Event()

    def blocked_crawl(url, max_pages=5, *, timeout=10.0, text_excerpt_limit=4000, before_fetch=None):
        if before_fetch:
            before_fetch()
        entered.set()
        assert release.wait(5), "test did not release blocked website fetch"
        return crawl(url, [page(url, "Acme Oy builds boats.")])

    monkeypatch.setattr(acquisition, "research_website", blocked_crawl)
    failures = []

    settings = Settings(_env_file=None, database_url="sqlite://", root_dir=tmp_path, searxng_url=SEARX)

    def run():
        try:
            enrich(Session, settings, FakeLLM(), job, fencing)
        except Exception as exc:  # asserted in the test thread after it joins
            failures.append(exc)

    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5), "enrichment did not reach the website fetch"
        body = client.get("/api/jobs", params={"company_id": str(company_id)}).json()
        active = next(item for item in body if item["id"] == str(job.id))
        progress = active["progress"]
        assert progress["phase"] == "website"
        assert progress["detail"] == "Crawling the company website"
        assert progress["source_count"] == 0
        assert progress["events"][-1]["phase"] == "website"
    finally:
        release.set()
        thread.join(5)

    assert not thread.is_alive()
    assert failures == []
    completed = next(item for item in client.get("/api/jobs", params={"company_id": str(company_id)}).json()
                     if item["id"] == str(job.id))["progress"]
    assert completed["phase"] == "completed" and completed["source_count"] == 1
    assert {event["phase"] for event in completed["events"]} >= {
        "registry", "website", "search", "extraction", "saving", "completed",
    }


def test_retry_clears_old_enrichment_progress(api):
    client, Session = api
    company_id = seed(Session)
    with Session() as db:
        job = db.scalar(select(Job).where(Job.company_id == company_id))
        job.state = JobState.failed
        job.payload = {**job.payload, "progress": {"phase": "failed", "events": []}}
        db.commit()
        job_id = job.id

    retried = client.post(f"/api/jobs/{job_id}/retry")

    assert retried.status_code == 200
    assert retried.json()["state"] == "queued" and retried.json()["progress"] is None


def test_reclaimed_attempt_cannot_commit_and_its_run_is_superseded(env, monkeypatch):
    Session, settings = env
    net = Net(monkeypatch, site_text=REVENUE_QUOTE)
    original = net.research_website
    company_id = seed(Session)
    job, fencing = claim(Session)
    def slow_crawl(url, *a, before_fetch=None, **kw):
        # while attempt 1 is fetching, its lease expires and attempt 2 takes over
        with Session() as db:
            db.get(Job, job.id).lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
            assert worker.claim_job(db)[1] == fencing + 1
            newer = db.get(Job, job.id)
            payload = dict(newer.payload)
            payload["progress"] = {"phase": "registry", "detail": "new attempt owns progress",
                                   "updated_at": NOW, "source_count": 0, "events": []}
            newer.payload = payload
            db.commit()
        # Simulate a slow origin that returns without invoking the old attempt's heartbeat.
        return crawl(url, [page(url, REVENUE_QUOTE)])

    monkeypatch.setattr(acquisition, "research_website", slow_crawl)
    with pytest.raises(research.LeaseLost):
        enrich(Session, settings, FakeLLM(REVENUE), job, fencing)
    with Session() as db:
        assert db.scalar(select(func.count(Source.id))) == 0
        assert db.get(Job, job.id).payload["progress"]["detail"] == "new attempt owns progress"
        stale = db.scalar(select(ResearchRun).where(ResearchRun.attempt == fencing))
        assert stale.status == ResearchRunStatus.running  # nothing written by the stale attempt
        research.cancel_job(job.id, db)  # cancels the current attempt only; it has no run yet
        assert db.get(ResearchRun, stale.id).status == ResearchRunStatus.running

    with Session() as db:
        research.retry_job(job.id, db)
    monkeypatch.setattr(acquisition, "research_website", original)
    job2, fencing2 = claim(Session)
    enrich(Session, settings, FakeLLM(REVENUE), job2, fencing2)
    with Session() as db:
        runs = {r.attempt: r.status for r in db.scalars(select(ResearchRun).where(ResearchRun.company_id == company_id))}
        assert runs == {fencing: ResearchRunStatus.failed, fencing2: ResearchRunStatus.completed}


def test_non_200_html_is_not_kept_as_research_evidence(env):
    _, settings = env
    target = research.Target("Acme Oy", None, "acme.fi", "https://acme.fi", "FI")
    f = research.Findings()
    bad = acquisition.PageResult("https://acme.fi", 503, "Error", "Acme Oy revenue was EUR 1,000.", [], None, NOW)

    research._keep_pages(settings, f, crawl("https://acme.fi", [bad]), SourceKind.website, target, own_site=True)

    assert f.fetched == []
    assert any("HTTP 503" in error for error in f.errors)


def test_results_commit_with_job_success_and_reruns_do_not_duplicate_or_touch_reviewed(env, monkeypatch):
    Session, settings = env
    Net(monkeypatch, site_text=REVENUE_QUOTE)
    company_id = seed(Session)
    job, fencing = claim(Session)
    enrich(Session, settings, FakeLLM(REVENUE), job, fencing)
    with Session() as db:
        # a crash here (before the worker's own release) loses nothing: success is already committed
        assert db.get(Job, job.id).state == JobState.succeeded
        fin = db.scalar(select(FinancialObservation))
        fin.review_status = ReviewStatus.accepted
        db.add(Job(kind=JOB_KIND_ENRICH, company_id=company_id, idempotency_key="enrich:again",
                   payload={"company_id": str(company_id)}))
        db.commit()

    job2, fencing2 = claim(Session)
    enrich(Session, settings, FakeLLM(REVENUE), job2, fencing2)
    with Session() as db:
        assert db.scalar(select(func.count(FinancialObservation.id))) == 1
        assert db.scalar(select(func.count(Evidence.id)).where(Evidence.field == "financial.revenue")) == 1
        assert db.scalar(select(func.count(Source.id))) == 1
        assert db.scalar(select(FinancialObservation)).review_status == ReviewStatus.accepted
        run = db.scalar(select(ResearchRun).where(ResearchRun.job_id == job2.id))
        assert "required_missing: financial.ebitda" in run.missing
        assert "required_unconfirmed: owner_intent" in run.missing


def test_registry_id_owned_by_other_company_is_not_merged(env, monkeypatch):
    Session, settings = env
    Net(monkeypatch)
    record = acquisition._normalize_company(
        {"businessId": {"value": "1234567-1"}, "names": [{"name": "Acme Oy"}]}, source_url="https://prh.example/c",
        fetched_at=NOW)
    monkeypatch.setattr(acquisition, "prh_search",
                        lambda **kw: acquisition.PRHSearchResult([record], 1, 1, None, "https://prh.example/c", NOW))
    with Session() as db:
        other = Company(name="Acme Holding", name_normalized="acme holding")
        db.add(other)
        db.flush()
        db.add(CompanyIdentifier(company_id=other.id, scheme="business_id", jurisdiction="FI", value="1234567-1"))
        db.commit()
    company_id = seed(Session)
    job, fencing = claim(Session)
    enrich(Session, settings, FakeLLM(), job, fencing)
    with Session() as db:
        assert db.scalar(select(func.count(CompanyIdentifier.id)).where(CompanyIdentifier.company_id == company_id)) == 0
        run = db.scalar(select(ResearchRun).where(ResearchRun.company_id == company_id))
        assert any(b.startswith("registry_business_id_in_use") for b in run.blocked)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def prh_record(business_id, name):
    return acquisition._normalize_company({"businessId": {"value": business_id}, "names": [{"name": name}]},
                                          source_url="https://prh.example/c", fetched_at=NOW)


def test_discovery_checkpoints_pages_and_dedupes_business_ids(env, monkeypatch):
    Session, _ = env
    calls = []

    def fake_prh(**kw):
        calls.append(kw)
        if kw["page"] == 1:
            recs = [prh_record("1111111-1", "A Oy"), prh_record("1111111-1", "A Oy"), prh_record("2222222-2", "B Oy")]
            return acquisition.PRHSearchResult(recs, 250, 1, 2, "https://prh.example/c", NOW)
        if len(calls) == 2:
            raise acquisition.PRHError("prh_unreachable: boom")
        recs = [prh_record("2222222-2", "B Oy"), prh_record("3333333-3", "C Oy")]
        return acquisition.PRHSearchResult(recs, 250, 2, None, "https://prh.example/c", NOW)

    monkeypatch.setattr(acquisition, "prh_search", fake_prh)
    with Session() as db:
        run = DiscoveryRun(params={"business_id": None, "registration_start": "2026-09-01", "max_pages": 5,
                                   "max_companies": 100})
        db.add(run)
        db.flush()
        db.add(Job(kind=JOB_KIND_DISCOVERY, idempotency_key=f"d:{run.id}", payload={"discovery_run_id": str(run.id)}))
        db.commit()
        run_id = run.id

    job, fencing = claim(Session)
    with Session() as db, pytest.raises(acquisition.PRHError):
        research.run_discovery(db, job=job, fencing=fencing)
    with Session() as db:
        run = db.get(DiscoveryRun, run_id)
        assert (run.current_page, run.companies_imported, run.status) == (1, 2, DiscoveryRunStatus.running)
        assert run.errors == ["page 2: prh_unreachable: boom"]
        db.get(Job, job.id).lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)  # the worker died
        db.commit()

    job, fencing = claim(Session)
    with Session() as db:
        research.run_discovery(db, job=job, fencing=fencing)
    assert [c["page"] for c in calls] == [1, 2, 2]  # resumed at the checkpoint, page 1 never refetched
    with Session() as db:
        run = db.get(DiscoveryRun, run_id)
        assert (run.companies_imported, run.status) == (3, DiscoveryRunStatus.completed)
        assert db.scalar(select(func.count(CompanyIdentifier.id))) == 3
        assert db.get(Job, job.id).state == JobState.succeeded
        assert db.scalar(select(func.count(Job.id)).where(Job.kind == JOB_KIND_ENRICH)) == 3


def test_discovery_cancel_between_pages(env, monkeypatch):
    Session, _ = env
    pages = []

    def fake_prh(**kw):
        pages.append(kw["page"])
        with Session() as db:
            research.cancel_job(job.id, db)
        return acquisition.PRHSearchResult([prh_record(f"{kw['page']}111111-1"[:9], "X Oy")], 500, kw["page"],
                                           kw["page"] + 1, "https://prh.example/c", NOW)

    monkeypatch.setattr(acquisition, "prh_search", fake_prh)
    with Session() as db:
        run = DiscoveryRun(params={"max_pages": 5, "max_companies": 100})
        db.add(run)
        db.flush()
        db.add(Job(kind=JOB_KIND_DISCOVERY, idempotency_key=f"d:{run.id}", payload={"discovery_run_id": str(run.id)}))
        db.commit()
        run_id = run.id
    job, fencing = claim(Session)
    with Session() as db, pytest.raises(research.JobCancelled):
        research.run_discovery(db, job=job, fencing=fencing)
    assert pages == [1]
    with Session() as db:
        run = db.get(DiscoveryRun, run_id)
        assert run.status == DiscoveryRunStatus.cancelled and run.current_page == 0  # page 1 result discarded


def test_scheduled_discovery_is_bounded_to_recent_registrations(env):
    Session, _ = env
    from permetheus.research_models import DiscoverySchedule
    with Session() as db:
        db.add(DiscoverySchedule(enabled=True, hour_utc=3, window_days=7))
        db.commit()
        now = datetime(2026, 9, 27, 4, tzinfo=timezone.utc)
        job = research.maybe_enqueue_scheduled_discovery(db, now=now)
        assert research.maybe_enqueue_scheduled_discovery(db, now=now) is None  # once per day
        run = db.get(DiscoveryRun, uuid.UUID(job.payload["discovery_run_id"]))
        assert run.params["registration_start"] == "2026-09-20" and run.params["registration_end"] == "2026-09-27"
        assert (run.params["max_pages"], run.params["max_companies"]) == (5, 100)


def test_discovery_input_validates_real_dates_order_and_business_id():
    ok = research.DiscoveryRunIn(business_id="FI 0112038-9", registration_start="2024-02-29",
                                 registration_end="2024-03-01")
    assert ok.business_id == "0112038-9"
    for bad in ({"registration_start": "2023-02-29"}, {"registration_end": "2024-13-01"},
                {"registration_start": "2024-03-02", "registration_end": "2024-03-01"},
                {"business_id": "0112038-8"}):
        with pytest.raises(ValueError):
            research.DiscoveryRunIn(**bad)


def test_discovery_passes_business_id_to_prh(env, monkeypatch):
    Session, _ = env
    seen = []
    monkeypatch.setattr(acquisition, "prh_search", lambda **kw: seen.append(kw) or
                        acquisition.PRHSearchResult([], 0, 1, None, "https://prh.example/c", NOW))
    with Session() as db:
        run = DiscoveryRun(params=research.DiscoveryRunIn(business_id="0112038-9").model_dump())
        db.add(run)
        db.flush()
        db.add(Job(kind=JOB_KIND_DISCOVERY, idempotency_key=f"d:{run.id}", payload={"discovery_run_id": str(run.id)}))
        db.commit()
    job, fencing = claim(Session)
    with Session() as db:
        research.run_discovery(db, job=job, fencing=fencing)
    assert seen[0]["business_id"] == "0112038-9"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture
def api():
    app = create_app(Settings(_env_file=None, database_url="sqlite://", admin_password=PASSWORD))
    app.include_router(research.router)
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"password": PASSWORD})
        c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        yield c, app.state.sessionmaker


def test_coverage_labels_required_items_and_never_claims_completeness(api):
    c, Session = api
    with Session() as db:
        company = Company(name="Acme Oy", name_normalized="acme")
        db.add(company)
        db.flush()
        source = Source(kind=SourceKind.website, url="https://acme.fi", content_hash="0" * 64)
        db.add(source)
        db.flush()
        for metric, status in (("revenue", ReviewStatus.accepted), ("ebitda", ReviewStatus.proposed)):
            db.add(FinancialObservation(company_id=company.id, source_id=source.id, metric=metric, amount=1,
                                        currency="EUR", period_start=date(2023, 1, 1), period_end=date(2023, 12, 31),
                                        scope="entity", status="reported", review_status=status))
        db.add(ResearchRun(company_id=company.id, status=ResearchRunStatus.completed, checked=["registry"]))
        db.commit()
        company_id = company.id
    body = c.get(f"/api/companies/{company_id}/coverage").json()
    assert body["required"] == {"financial.revenue": "accepted", "financial.ebitda": "proposed_unreviewed",
                                "financial.employees": "missing", "owner_intent": "unconfirmed"}
    assert "never a claim" in body["scope_note"]


def test_cancel_and_retry_work_for_media_jobs_without_duplicates(api):
    c, Session = api
    with Session() as db:
        company = Company(name="Media test", name_normalized="media test")
        db.add(company)
        db.flush()
        note = Note(title="Retryable note", company_id=company.id, status="processing", progress=42,
                    transcript_raw="ASR text", transcript_corrected="Human correction",
                    transcript_segments=[{"start_sec": 0, "end_sec": 1, "text": "ASR text"}])
        source = Source(kind=SourceKind.document, url="local:test", title="Report", content_hash="a" * 64)
        db.add_all((note, source))
        db.flush()
        document = Document(company_id=company.id, source_id=source.id, title="Report", media_type="application/pdf",
                            artifact_path="documents/test.pdf", sha256="a" * 64, byte_size=10,
                            status="processing", progress=35, result=[{"metric": "revenue"}], warning="kept")
        db.add(document)
        db.flush()
        db.add(Job(kind="note.transcribe", idempotency_key=f"note.transcribe:{note.id}",
                   payload={"note_id": str(note.id)}, company_id=company.id,
                   state=JobState.running, attempts=5, last_error="boom"))
        db.add(Job(kind="document.extract", idempotency_key=f"document.extract:{document.id}",
                   payload={"document_id": str(document.id)}, company_id=company.id,
                   state=JobState.running, attempts=2, last_error="boom"))
        db.commit()
        note_id, document_id = note.id, document.id
        note_job_id = db.scalar(select(Job.id).where(Job.kind == "note.transcribe"))
        doc_job_id = db.scalar(select(Job.id).where(Job.kind == "document.extract"))

    assert c.post(f"/api/jobs/{note_job_id}/cancel").json()["state"] == "cancelled"
    cancelled_note = c.get(f"/api/notes/{note_id}").json()
    assert cancelled_note["status"] == "failed"
    assert cancelled_note["processing_error"] == "Processing cancelled; retry when ready"
    assert cancelled_note["transcript_raw"] == "ASR text"
    assert cancelled_note["transcript_corrected"] == "Human correction"
    note_retry = c.post(f"/api/jobs/{note_job_id}/retry")
    assert note_retry.status_code == 200 and note_retry.json()["state"] == "queued"
    assert note_retry.json()["attempts"] == 5 and note_retry.json()["id"] == str(note_job_id)
    queued_note = c.get(f"/api/notes/{note_id}").json()
    assert queued_note["status"] == "queued" and queued_note["processing_error"] is None

    assert c.post(f"/api/jobs/{doc_job_id}/cancel").json()["state"] == "cancelled"
    cancelled_doc = c.get(f"/api/documents/{document_id}")
    assert cancelled_doc.status_code == 200 and cancelled_doc.json()["status"] == "failed"
    assert cancelled_doc.json()["error"] == "Processing cancelled; retry when ready"
    assert c.post(f"/api/jobs/{doc_job_id}/cancel").status_code == 409
    doc_retry = c.post(f"/api/jobs/{doc_job_id}/retry")
    assert doc_retry.status_code == 200 and doc_retry.json()["state"] == "queued"
    assert doc_retry.json()["id"] == str(doc_job_id)
    queued_doc = c.get(f"/api/documents/{document_id}").json()
    assert queued_doc["status"] == "queued" and queued_doc["error"] is None
    assert queued_doc["result"] == [{"metric": "revenue"}] and queued_doc["warning"] == "kept"
    with Session() as db:
        assert db.scalar(select(func.count(Job.id))) == 2
        assert db.get(Job, note_job_id).payload == {"note_id": str(note_id), "retry_base": 5}
