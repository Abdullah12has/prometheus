import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from permetheus import deals, mail
from permetheus.app import create_app
from permetheus.config import Settings
from permetheus.deals import evaluate
from permetheus.errors import ApiError

PASSWORD = "correct horse"
NOW = datetime.now(timezone.utc)


def iso(delta_days: float = 0) -> str:
    return (NOW + timedelta(days=delta_days)).isoformat()


@pytest.fixture
def api():
    app = create_app(Settings(_env_file=None, database_url="sqlite://", admin_password=PASSWORD))
    paths = {getattr(r, "path", None) for r in app.routes}
    for module, path in ((deals, "/api/mandates"), (mail, "/api/outreach/drafts")):
        if path not in paths:
            app.include_router(module.router)
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"password": PASSWORD})
        c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        yield c


def ok(r, status=200):
    assert r.status_code == status, r.text
    return r.json()


def source(api, url="https://example.org/report"):
    return ok(api.post("/api/sources", json={"kind": "website", "url": url}), 201)["id"]


def review(api, kind, item_id):
    return ok(api.post(f"/api/{kind}/{item_id}/review", json={"status": "accepted", "reason": "Checked against source"}))


def company(api, name="Nordic Parts Oy", country="FI", revenue="12000000.00", industry="Manufacturing", accept=True):
    cid = ok(api.post("/api/companies", json={"name": name, "country": country, "allow_new": True}), 201)["company"]["id"]
    sid = source(api)
    if revenue:
        fin = ok(api.post(f"/api/companies/{cid}/financials", json={
            "source_id": sid, "metric": "revenue", "amount": revenue, "currency": "EUR", "period_start": "2025-01-01",
            "period_end": "2025-12-31", "scope": "entity", "status": "reported"}), 201)
        if accept:
            review(api, "financials", fin["id"])
    if industry:
        ev = ok(api.post(f"/api/companies/{cid}/evidence", json={
            "source_id": sid, "field": "industry", "value": industry, "excerpt": f"We are a {industry} company"}), 201)
        if accept:
            review(api, "evidence", ev["id"])
    return cid


def mandate(api, name="Buyer A", top=None, **criteria):
    body = {"buyer_name": name, "evidence_level": "buyer_confirmed", "confirmed_by": "Jane Buyer",
            "last_confirmed_at": iso(-1), "expires_at": iso(90), "identity_verified": True, "source_id": source(api),
            "criteria": {"countries": ["FI", "SE"], "industries": ["manufacturing"], "financial_currency": "EUR",
                         "revenue_min": "5000000", "revenue_max": "50000000", **criteria}, **(top or {})}
    return ok(api.post("/api/mandates", json=body), 201)


def confirmed_profile(api, cid, conditions):
    p = ok(api.post(f"/api/companies/{cid}/preferences", json={"conditions": conditions, "stated_by": "Owner"}), 201)
    return ok(api.post(f"/api/preferences/{p['id']}/confirm", json={
        "conditions_hash": p["conditions_hash"], "speaker_name": "Owner", "speaker_authority": "owner",
        "statement": "Yes, these are my conditions", "confirmed_at": iso(-0.01)}))


# ------------------------------------------------------------ pure evaluation

FACTS = {"country": {"value": "FI", "citations": ["company:x.country"]}}
PROFILE = {"id": "p", "version": 1, "confirmed": True}


def m_snap(**criteria):
    return {"id": "m", "version": 1, "buyer_name": "B", "evidence_level": "buyer_confirmed", "identity_verified": True,
            "source_id": "s", "financing_status": "unknown", "criteria": {"countries": ["FI"], **criteria}}


def test_unknown_hard_condition_stays_unresolved_despite_perfect_soft_score():
    conditions = [{"kind": "site_retention", "strength": "hard", "weight": 1},
                  {"kind": "retained_ownership", "strength": "soft", "weight": 5, "min_pct": "30"}]
    r = evaluate(FACTS, PROFILE, conditions, m_snap(max_rollover_pct="40"))
    assert r["fit_score"] == 1.0 and r["status"] == "research_needed"
    assert any("site stays open" in q for q in r["explanation"]["questions"])
    assert r["explanation"]["financing"] == "unknown (not inferred)"


def test_hard_fail_excludes_and_is_not_scored():
    conditions = [{"kind": "retained_ownership", "strength": "hard", "min_pct": "30"},
                  {"kind": "team_retention", "strength": "soft", "weight": 1}]
    r = evaluate(FACTS, PROFILE, conditions, m_snap(max_rollover_pct="0", team_commitment=True))
    assert r["status"] == "excluded" and r["fit_score"] is None
    assert [c["key"] for c in r["explanation"]["contrary"]] == ["retained_ownership"]
    assert r["explanation"]["contrary"][0]["citations"] == ["preference:p@v1.conditions.retained_ownership",
                                                            "mandate:m@v1.criteria.max_rollover_pct"]


def test_unconfirmed_strength_conflict_asks_owner_instead_of_excluding():
    r = evaluate(FACTS, PROFILE, [{"kind": "operating_control", "strength": "unknown"}],
                 m_snap(control_retention=False))
    assert r["status"] == "research_needed"
    assert "firm requirement" in r["explanation"]["questions"][0]


def test_money_in_other_currency_is_unknown_not_converted():
    facts = {**FACTS, "revenue": {"amount": "9000000", "currency": "SEK", "citations": ["financial:1"]}}
    r = evaluate(facts, PROFILE, [], m_snap(financial_currency="EUR", revenue_min="1000000"))
    assert r["checks"][-1]["result"] == "unknown" and "no FX" in r["checks"][-1]["detail"]
    assert r["status"] == "research_needed"
    cond = [{"kind": "minimum_proceeds", "strength": "hard", "amount": "5000000", "currency": "EUR"}]
    r = evaluate(FACTS, PROFILE, cond, m_snap(max_consideration="9000000", consideration_currency="USD"))
    assert r["checks"][-1]["result"] == "unknown"


def test_public_strategy_or_unconfirmed_profile_is_never_compatible():
    assert evaluate(FACTS, PROFILE, [], m_snap())["status"] == "compatible"
    assert evaluate(FACTS, PROFILE, [], {**m_snap(), "evidence_level": "public_strategy"})["status"] == "research_needed"
    assert evaluate(FACTS, {**PROFILE, "confirmed": False}, [], m_snap())["status"] == "research_needed"
    assert evaluate(FACTS, None, [], m_snap())["status"] == "research_needed"
    assert evaluate({}, PROFILE, [], m_snap())["status"] == "research_needed"  # country unknown


# ------------------------------------------------------------ API


def test_requires_session(api):
    api.cookies.clear()
    assert api.get("/api/mandates").status_code == 401


def test_mandate_verification_rules_and_version_history(api):
    base = {"buyer_name": "B", "criteria": {}, "expires_at": iso(30)}
    bad = [
        {**base, "evidence_level": "public_strategy"},  # needs source
        {**base, "evidence_level": "buyer_confirmed"},  # needs confirmer
        {**base, "evidence_level": "public_strategy", "source_id": source(api), "confirmed_by": "X",
         "last_confirmed_at": iso(-1)},  # public strategy cannot claim confirmation
        {**base, "evidence_level": "public_strategy", "source_id": source(api), "financing_status": "evidenced"},
        {**base, "evidence_level": "buyer_confirmed", "confirmed_by": "X", "last_confirmed_at": iso(-1),
         "criteria": {"revenue_min": "1"}},  # range without currency
        {**base, "evidence_level": "buyer_confirmed", "confirmed_by": "X", "last_confirmed_at": iso(-1),
         "criteria": {"employees_min": 10, "employees_max": 5}},
        {**base, "evidence_level": "buyer_confirmed", "confirmed_by": "X", "last_confirmed_at": iso(-1),
         "identity_verified": True},  # identity claim without a source
    ]
    for body in bad:
        assert api.post("/api/mandates", json=body).status_code == 422, body
    expired = {**base, "evidence_level": "buyer_confirmed", "confirmed_by": "X", "last_confirmed_at": iso(-1),
               "expires_at": iso(-1)}
    assert api.post("/api/mandates", json=expired).json()["error"]["code"] == "expired"

    m = mandate(api)
    assert m["version"] == 1 and m["active"] and m["financing_status"] == "unknown"
    patched = ok(api.patch(f"/api/mandates/{m['id']}", json={"criteria": {"max_rollover_pct": "20"}}))
    assert patched["version"] == 2 and patched["criteria"]["countries"] == ["FI", "SE"]  # nested merge
    assert api.patch(f"/api/mandates/{m['id']}", json={"criteria": {"revenue_max": "1"}}).status_code == 422
    assert api.patch(f"/api/mandates/{m['id']}", json={"bogus": 1}).status_code == 422
    assert ok(api.patch(f"/api/mandates/{m['id']}", json={"status": "active"}))["version"] == 2  # no-op
    detail = ok(api.get(f"/api/mandates/{m['id']}"))
    assert [v["version"] for v in detail["versions"]] == [2, 1]
    assert detail["versions"][0]["changed_fields"] == ["criteria"]
    ok(api.patch(f"/api/mandates/{m['id']}", json={"status": "paused"}))
    assert ok(api.get("/api/mandates?active_only=true")) == []


def test_preference_confirmation_is_bound_to_version_and_authority(api):
    cid = company(api)
    bad = [{"kind": "timeline", "strength": "hard"},  # missing within_months
           {"kind": "team_retention", "strength": "hard", "min_pct": "5"}]  # irrelevant param
    for cond in bad:
        assert api.post(f"/api/companies/{cid}/preferences", json={"conditions": [cond]}).status_code == 422
    dup = [{"kind": "team_retention", "strength": "hard"}] * 2
    assert api.post(f"/api/companies/{cid}/preferences", json={"conditions": dup}).status_code == 422

    p1 = ok(api.post(f"/api/companies/{cid}/preferences",
                     json={"conditions": [{"kind": "team_retention", "strength": "hard"}]}), 201)
    confirm = {"conditions_hash": "0" * 64, "speaker_name": "Owner", "speaker_authority": "owner",
               "statement": "confirmed", "confirmed_at": iso(-0.01)}
    assert api.post(f"/api/preferences/{p1['id']}/confirm", json=confirm).json()["error"]["code"] == "version_mismatch"
    confirm["conditions_hash"] = p1["conditions_hash"]
    assert api.post(f"/api/preferences/{p1['id']}/confirm",
                    json={**confirm, "speaker_authority": "unverified"}).status_code == 422
    done = ok(api.post(f"/api/preferences/{p1['id']}/confirm", json=confirm))
    assert done["confirmation"]["authority"] == "owner"
    assert api.post(f"/api/preferences/{p1['id']}/confirm", json=confirm).status_code == 409

    p2 = ok(api.post(f"/api/companies/{cid}/preferences",
                     json={"conditions": [{"kind": "brand_retention", "strength": "soft"}]}), 201)
    listing = ok(api.get(f"/api/companies/{cid}/preferences"))
    assert [p["version"] for p in listing["items"]] == [2, 1]
    assert listing["effective_profile_id"] == p1["id"]  # unconfirmed v2 is not authoritative
    assert p2["confirmation"] is None


def test_scenario_snapshot_overrides_without_mutating_profile(api):
    cid = company(api)
    mandate(api, "Minority Fund", max_rollover_pct="40", structures=["minority_investment"])
    profile = confirmed_profile(api, cid, [{"kind": "retained_ownership", "strength": "hard", "min_pct": "60"}])
    s = ok(api.post("/api/scenarios", json={
        "company_id": cid, "name": "Accept 30% retained",
        "conditions": [{"kind": "retained_ownership", "strength": "hard", "min_pct": "30"}],
        "facts": {"revenue": {"amount": "20000000", "currency": "EUR"}}}), 201)
    [res] = s["results"]
    assert res["baseline_status"] == "excluded" and res["status"] == "compatible"
    assert s["snapshot"]["facts"]["revenue"]["hypothetical"] is True
    assert any(c.startswith("scenario:override") for c in res["checks"][-1]["citations"])
    after = ok(api.get(f"/api/companies/{cid}/preferences"))["items"][0]
    assert after["conditions"] == profile["conditions"] and after["conditions_hash"] == profile["conditions_hash"]
    assert len(ok(api.get(f"/api/scenarios?company_id={cid}"))) == 1
    both = {"company_id": cid, "name": "x", "conditions": [{"kind": "team_retention", "strength": "soft"}],
            "remove_kinds": ["team_retention"]}
    assert api.post("/api/scenarios", json=both).status_code == 422


def test_match_run_persists_results_and_opportunities(api):
    cid = company(api)
    fit = mandate(api, "Fit Co", max_rollover_pct="40", site_commitment=True, team_commitment=True)
    silent = mandate(api, "Silent Co", max_rollover_pct="40")
    full = mandate(api, "Full Buyout Co", max_rollover_pct="0")
    too_big = mandate(api, "Big Co", revenue_min="100000000", revenue_max=None, max_rollover_pct="40",
                      site_commitment=True)
    confirmed_profile(api, cid, [
        {"kind": "retained_ownership", "strength": "hard", "min_pct": "30"},
        {"kind": "site_retention", "strength": "hard", "sites": ["Tampere"]},
        {"kind": "team_retention", "strength": "soft", "weight": 2}])

    run = ok(api.post("/api/match-runs", json={"company_id": cid}), 201)
    assert run["counts"] == {"compatible": 1, "research_needed": 1, "excluded": 2}
    by = {r["mandate_id"]: r for r in run["results"]}
    assert by[fit["id"]]["status"] == "compatible" and by[fit["id"]]["fit_score"] == 1.0
    assert by[silent["id"]]["status"] == "research_needed" and by[silent["id"]]["coverage"] == 0.0
    assert by[full["id"]]["status"] == "excluded" and by[too_big["id"]]["status"] == "excluded"
    revenue = next(c for c in by[fit["id"]]["checks"] if c["key"] == "revenue")
    assert revenue["result"] == "pass" and any(c.startswith("financial:") for c in revenue["citations"])
    assert run["results"][0]["mandate_id"] == fit["id"]  # compatible first
    assert run["snapshot"]["profile"]["confirmed"] is True

    opps = ok(api.get(f"/api/opportunities?company_id={cid}"))
    assert {o["mandate_id"] for o in opps} == {fit["id"], silent["id"]}  # excluded never become opportunities
    assert all(not o["stale"] for o in opps)
    ok(api.patch(f"/api/mandates/{fit['id']}", json={"criteria": {"site_commitment": False}}))
    stale = {o["mandate_id"]: o["stale"] for o in ok(api.get("/api/opportunities"))}
    assert stale[fit["id"]] is True

    rerun = ok(api.post("/api/match-runs", json={"company_id": cid}), 201)
    assert rerun["counts"]["excluded"] == 3
    opp = next(o for o in ok(api.get("/api/opportunities")) if o["mandate_id"] == fit["id"])
    assert opp["status"] == "excluded" and not opp["stale"]  # kept with history, marked excluded
    assert ok(api.get(f"/api/match-runs/{run['id']}"))["counts"]["compatible"] == 1  # old snapshot intact
    assert len(ok(api.get(f"/api/match-runs?company_id={cid}"))) == 2


def test_refresh_matches_only_creates_runs_for_changed_inputs(api):
    cid = company(api)
    buyer = mandate(api, "Buyer A")
    sessionmaker = api.app.state.sessionmaker

    assert deals.refresh_matches(sessionmaker) == 1
    first = ok(api.get(f"/api/match-runs?company_id={cid}"))
    assert len(first) == 1
    assert deals.refresh_matches(sessionmaker) == 0
    assert len(ok(api.get(f"/api/match-runs?company_id={cid}"))) == 1

    sid = source(api, "https://example.org/new-financials")
    financial = ok(api.post(f"/api/companies/{cid}/financials", json={
        "source_id": sid, "metric": "ebitda", "amount": "2000000", "currency": "EUR",
        "period_start": "2026-01-01", "period_end": NOW.date().isoformat(), "scope": "entity",
        "status": "reported"}), 201)
    review(api, "financials", financial["id"])
    assert deals.refresh_matches(sessionmaker) == 1

    ok(api.patch(f"/api/mandates/{buyer['id']}", json={"criteria": {"team_commitment": True}}))
    assert deals.refresh_matches(sessionmaker) == 1
    runs = ok(api.get(f"/api/match-runs?company_id={cid}"))
    assert len(runs) == 3
    assert len({run["snapshot_hash"] for run in runs}) == 3


def test_refresh_match_batches_consider_new_companies_without_owner_profiles(api):
    company_ids = [company(api, f"Target {n}", revenue=None, industry=None) for n in range(3)]
    mandate(api, "Buyer A")
    sessionmaker = api.app.state.sessionmaker

    first_count, cursor = deals.refresh_match_batch(sessionmaker, limit=2)
    assert first_count == 2
    assert cursor is not None

    last_count, cursor = deals.refresh_match_batch(sessionmaker, after_id=cursor, limit=2)
    assert last_count == 1
    assert cursor is None

    for company_id in company_ids:
        [run] = ok(api.get(f"/api/match-runs?company_id={company_id}"))
        assert run["counts"]["research_needed"] == 1


def test_match_runs_exclude_buyer_company_itself_manually_and_automatically(api):
    buyer_company = company(api, "Buyer Legal Entity")
    target_company = company(api, "Independent Target Oy")
    buyer = mandate(api, "Buyer A", top={"buyer_company_id": buyer_company})

    manual = api.post("/api/match-runs", json={"company_id": buyer_company, "mandate_ids": [buyer["id"]]})
    assert manual.status_code == 409 and manual.json()["error"]["code"] == "no_active_mandates"

    assert deals.refresh_matches(api.app.state.sessionmaker) == 1
    assert ok(api.get(f"/api/match-runs?company_id={buyer_company}")) == []
    [target_run] = ok(api.get(f"/api/match-runs?company_id={target_company}"))
    assert [result["mandate_id"] for result in ok(api.get(f"/api/match-runs/{target_run['id']}"))["results"]] == [buyer["id"]]


def test_proposed_facts_do_not_create_compatible_refresh_results(api):
    cid = company(api, accept=False)
    confirmed_profile(api, cid, [])
    mandate(api, "Buyer A")

    assert deals.refresh_matches(api.app.state.sessionmaker) == 1
    [run] = ok(api.get(f"/api/match-runs?company_id={cid}"))
    detail = ok(api.get(f"/api/match-runs/{run['id']}"))
    [result] = detail["results"]
    assert result["status"] == "research_needed"
    assert {check["key"] for check in result["checks"] if check["result"] == "unknown"} >= {"industry", "revenue"}


def test_refresh_matches_when_historical_comparables_change(api):
    cid = company(api)
    mandate(api, "Buyer A")
    confirmed_profile(api, cid, [])
    sessionmaker = api.app.state.sessionmaker

    assert deals.refresh_matches(sessionmaker) == 1
    [first] = ok(api.get(f"/api/match-runs?company_id={cid}"))
    first_detail = ok(api.get(f"/api/match-runs/{first['id']}"))
    assert first_detail["results"][0]["explanation"]["comparables"] == []

    ok(api.post("/api/historical-deals", json={**DEAL, "buyer_name": "Buyer A"}), 201)
    assert deals.refresh_matches(sessionmaker) == 1
    latest = ok(api.get(f"/api/match-runs?company_id={cid}"))[0]
    latest_detail = ok(api.get(f"/api/match-runs/{latest['id']}"))
    assert len(latest_detail["results"][0]["explanation"]["comparables"]) == 1


def test_match_run_requires_active_mandates(api):
    cid = company(api)
    assert api.post("/api/match-runs", json={"company_id": cid}).json()["error"]["code"] == "no_active_mandates"
    m = mandate(api)
    ok(api.patch(f"/api/mandates/{m['id']}", json={"status": "closed"}))
    r = api.post("/api/match-runs", json={"company_id": cid, "mandate_ids": [m["id"]]})
    assert r.json()["error"]["code"] == "inactive_mandates"


def test_outcomes_drafts_and_analytics(api):
    cid = company(api)
    mandate(api, "Fit Co", max_rollover_pct="40")
    confirmed_profile(api, cid, [{"kind": "retained_ownership", "strength": "hard", "min_pct": "30"}])
    ok(api.post("/api/match-runs", json={"company_id": cid}), 201)
    [opp] = ok(api.get("/api/opportunities"))
    url = f"/api/opportunities/{opp['id']}"

    assert api.post(f"{url}/outcomes", json={"milestone": "lost", "occurred_at": iso(-1),
                                             "reason_category": "timing", "reason_basis": "stated"}).status_code == 422
    assert api.post(f"{url}/outcomes", json={"milestone": "replied", "occurred_at": iso(-1), "reason_category":
                    "timing", "reason_basis": "stated", "evidence_excerpt": "x"}).status_code == 422
    assert api.post(f"{url}/outcomes", json={"milestone": "reached", "occurred_at": iso(1)}).status_code == 422
    ok(api.post(f"{url}/outcomes", json={"milestone": "reached", "occurred_at": iso(-3)}), 201)
    ok(api.post(f"{url}/outcomes", json={"milestone": "lost", "occurred_at": iso(-1), "response": "rejected",
                                         "reason_category": "timing", "reason_basis": "stated",
                                         "evidence_excerpt": "We are not buying this year"}), 201)
    ok(api.post(f"{url}/outcomes", json={"milestone": "lost", "occurred_at": iso(-2)}), 201)  # no reason
    assert ok(api.get("/api/opportunities"))[0]["latest_milestone"] == "lost"
    assert [e["milestone"] for e in ok(api.get(f"{url}/outcomes"))] == ["reached", "lost", "lost"]

    a = ok(api.get("/api/deals/analytics"))
    assert a["failure_reasons"] == {"lost_events": 2, "supported": {"timing": 1}, "without_supported_reason": 1}
    assert a["opportunities"]["by_latest_milestone"] == {"lost": 1} and a["outcome_events"] == 3
    assert "probabilit" not in str(a).replace("no success probabilities", "")

    assert api.post(f"{url}/drafts", json={}).json()["error"]["code"] == "disclosure_authorization_required"
    auth = {"authorized_by": "Owner", "authority": "owner", "scope": ["country", "industry"],
            "statement": "You may share country and industry with Fit Co", "authorized_at": iso(-0.01)}
    assert api.post(f"{url}/drafts", json={"authorization": {**auth, "authority": "unverified"}}).status_code == 422
    draft = ok(api.post(f"{url}/drafts", json={"authorization": auth}), 201)
    company_payload = draft["payload"]["company"]
    assert company_payload == {"name": "Undisclosed company", "country": "FI", "industry": "Manufacturing"}
    assert draft["status"] == "draft" and len(draft["content_hash"]) == 64


DEAL = {"buyer_name": "Fit Co", "target_name": "Old Target Oy", "status": "completed", "announced_on": "2024-01-10",
        "completed_on": "2024-03-01", "sector": "Manufacturing", "country": "FI", "structure": "majority_sale",
        "source_url": "https://news.example.org/deal", "disclosure_rights": "public", "as_of": "2024-03-02T00:00:00Z"}


def test_historical_deal_validation_and_crud(api):
    bad = [{**DEAL, "completed_on": None}, {**DEAL, "withdrawn_on": "2024-02-01"},
           {**DEAL, "completed_on": "2024-01-01"}, {**DEAL, "as_of": "2024-02-01T00:00:00Z"},
           {**DEAL, "value_amount": "100"}, {**DEAL, "source_url": "ftp://x"},
           {**DEAL, "as_of": iso(2)}, {k: v for k, v in DEAL.items() if k != "source_url"}]
    for body in bad:
        assert api.post("/api/historical-deals", json=body).status_code == 422, body
    d = ok(api.post("/api/historical-deals", json=DEAL), 201)
    assert d["value_amount"] is None
    assert api.patch(f"/api/historical-deals/{d['id']}", json={"status": "withdrawn"}).status_code == 422
    d = ok(api.patch(f"/api/historical-deals/{d['id']}", json={"value_amount": "5000000", "value_currency": "EUR"}))
    assert d["value_amount"] == "5000000.0000" or float(d["value_amount"]) == 5_000_000
    assert ok(api.get("/api/historical-deals?known_as_of=2024-03-01T00:00:00Z"))["total"] == 0
    assert ok(api.get("/api/historical-deals?buyer=fit"))["total"] == 1
    ok(api.delete(f"/api/historical-deals/{d['id']}"))
    assert ok(api.get("/api/historical-deals"))["total"] == 0


def test_historical_csv_import_is_all_or_nothing(api):
    header = "buyer_name,target_name,status,announced_on,completed_on,withdrawn_on,source_url,disclosure_rights,as_of"
    good = "Fit Co,T1,completed,2024-01-10,2024-03-01,,https://e.org/1,public,2024-03-02T00:00:00Z"
    withdrawn = "Fit Co,T2,withdrawn,2024-01-10,,2024-05-01,https://e.org/2,public,2024-05-02T00:00:00Z"
    broken = "Fit Co,T3,completed,2024-01-10,,,https://e.org/3,public,2024-03-02T00:00:00Z"
    post = lambda text, ct="text/csv": api.post("/api/historical-deals/import", content=text.encode(),
                                                 headers={"Content-Type": ct})

    r = post("\n".join([header, good, broken]))
    assert r.status_code == 422 and r.json()["error"]["details"][0]["row"] == 3
    assert ok(api.get("/api/historical-deals"))["total"] == 0
    assert post("buyer_name,nonsense\nx,y").json()["error"]["code"] == "invalid_csv_header"
    assert post(header).json()["error"]["code"] == "empty_csv"
    assert post("\n".join([header, good]), ct="application/json").status_code == 415
    assert post(header + "\n" + "x," * 600_000).status_code == 413

    res = ok(post("\n".join([header, good, withdrawn, good])), 201)
    assert res["imported"] == 2 and res["skipped_duplicate_rows"] == [4]
    assert ok(post("\n".join([header, good])), 201) == {"imported": 0, "skipped_duplicate_rows": [2], "ids": []}
    assert ok(api.get("/api/deals/analytics"))["historical_deals"]["by_status"] == {"completed": 1, "withdrawn": 1}


def test_replay_is_unavailable_without_records_then_uses_snapshots(api):
    r = ok(api.post("/api/simulations/replay", json={"as_of": iso(0)}))
    assert r["status"] == "unavailable" and len(r["reasons"]) == 2

    cid = company(api)
    mandate(api, "Fit Co", max_rollover_pct="40")
    confirmed_profile(api, cid, [{"kind": "retained_ownership", "strength": "hard", "min_pct": "30"}])
    ok(api.post("/api/match-runs", json={"company_id": cid}), 201)
    after_run = datetime.now(timezone.utc).isoformat()
    r = ok(api.post("/api/simulations/replay", json={"as_of": after_run}))
    assert r["status"] == "unavailable" and r["reasons"] == [
        "no historical deals known at as_of and no recorded outcomes for replayed results"]

    ok(api.post("/api/historical-deals", json=DEAL), 201)
    now = datetime.now(timezone.utc)
    later = {**DEAL, "target_name": "Future Target", "announced_on": None, "completed_on": now.date().isoformat(),
             "as_of": now.isoformat()}
    ok(api.post("/api/historical-deals", json=later), 201)
    r = ok(api.post("/api/simulations/replay", json={"as_of": after_run}))
    assert r["status"] == "completed" and r["counts"]["runs"] == 1 and r["counts"]["changed"] == 0
    [item] = r["runs"][0]["results"]
    assert item["saved_status"] == item["replayed_status"] == "compatible"
    # Deals recorded after as_of must not leak into the replay.
    assert [c["target_name"] for c in item["comparables_known"]] == ["Old Target Oy"]
    assert ok(api.post("/api/simulations/replay", json={"as_of": iso(-10)}))["status"] == "unavailable"
    assert api.post("/api/simulations/replay", json={"as_of": iso(1)}).status_code == 422


# ------------------------------------------------------------ review, identity, freshness, email drafts

AUTH = {"authorized_by": "Owner", "authority": "owner", "scope": ["country", "industry"],
        "statement": "You may share country and industry with this buyer", "authorized_at": iso(-0.01)}
RETAIN_30 = [{"kind": "retained_ownership", "strength": "hard", "min_pct": "30"}]


def test_proposed_evidence_is_cited_as_unknown_until_accepted(api):
    cid = company(api, accept=False)
    mandate(api, "Fit Co", max_rollover_pct="40")
    confirmed_profile(api, cid, RETAIN_30)
    [res] = ok(api.post("/api/match-runs", json={"company_id": cid}), 201)["results"]
    assert res["status"] == "research_needed"
    checks = {c["key"]: c for c in res["checks"]}
    for key, prefix in (("revenue", "financial:"), ("industry", "evidence:")):
        assert checks[key]["result"] == "unknown" and "proposed" in checks[key]["detail"]
        assert any(c.startswith(prefix) for c in checks[key]["citations"])
    [opp] = ok(api.get("/api/opportunities"))
    assert opp["stale_reasons"] == []
    # Proposed facts never reach a disclosure, even when in scope.
    scope = {**AUTH, "scope": ["country", "industry", "revenue"]}
    draft = ok(api.post(f"/api/opportunities/{opp['id']}/drafts", json={"authorization": scope}), 201)
    assert draft["payload"]["company"] == {"name": "Undisclosed company", "country": "FI"}

    detail = ok(api.get(f"/api/companies/{cid}"))
    review(api, "financials", detail["financials"][0]["id"])
    review(api, "evidence", detail["evidence"][0]["id"])
    [opp] = ok(api.get("/api/opportunities"))
    assert opp["stale"] and opp["stale_reasons"] == ["company_facts"]
    r = api.post(f"/api/opportunities/{opp['id']}/drafts", json={"authorization": AUTH})
    assert r.status_code == 409 and r.json()["error"]["details"]["reasons"] == ["company_facts"]
    [res] = ok(api.post("/api/match-runs", json={"company_id": cid}), 201)["results"]
    assert res["status"] == "compatible"


def test_unverified_buyer_identity_is_never_compatible(api):
    assert evaluate(FACTS, PROFILE, [], {**m_snap(), "identity_verified": False})["status"] == "research_needed"
    no_source = evaluate(FACTS, PROFILE, [], {**m_snap(), "source_id": None})
    assert no_source["status"] == "research_needed" and "buyer_identity" in no_source["explanation"]["summary"]
    cid = company(api)
    mandate(api, "Unverified Co", top={"identity_verified": False}, max_rollover_pct="40")
    confirmed_profile(api, cid, RETAIN_30)
    [res] = ok(api.post("/api/match-runs", json={"company_id": cid}), 201)["results"]
    assert res["status"] == "research_needed" and any("identity" in q for q in res["explanation"]["questions"])


def test_owner_profile_and_company_changes_make_opportunity_stale(api):
    cid = company(api)
    mandate(api, "Fit Co", max_rollover_pct="40")
    confirmed_profile(api, cid, RETAIN_30)
    ok(api.post("/api/match-runs", json={"company_id": cid}), 201)
    [opp] = ok(api.get("/api/opportunities"))
    assert not opp["stale"]
    ok(api.post(f"/api/companies/{cid}/preferences", json={"conditions": RETAIN_30 + [
        {"kind": "brand_retention", "strength": "soft"}]}), 201)
    assert not ok(api.get("/api/opportunities"))[0]["stale"]  # an unconfirmed version is not effective
    confirmed_profile(api, cid, [{"kind": "retained_ownership", "strength": "hard", "min_pct": "50"}])
    [opp] = ok(api.get("/api/opportunities"))
    assert opp["stale_reasons"] == ["owner_profile"]
    r = api.post(f"/api/opportunities/{opp['id']}/drafts", json={"authorization": AUTH})
    assert r.json()["error"]["code"] == "opportunity_stale"
    ok(api.post("/api/match-runs", json={"company_id": cid}), 201)
    ok(api.patch(f"/api/companies/{cid}", json={"name": "Renamed Parts Oy"}))
    assert ok(api.get("/api/opportunities"))[0]["stale_reasons"] == ["company_facts"]


def contact(api, company_id, email, role="buyer", verify=True):
    c = ok(api.post(f"/api/companies/{company_id}/contacts",
                    json={"name": "Jane Buyer", "contact_role": role, "email": email}), 201)
    if verify:
        ok(api.post(f"/api/contacts/{c['id']}/verify", json={"basis": "Confirmed via company switchboard"}))
    return c["id"]


def test_email_draft_is_scoped_bound_and_never_dispatched(api):
    cid = company(api)
    buyer = ok(api.post("/api/companies", json={"name": "Acquirer AB", "country": "SE", "allow_new": True}), 201)
    buyer = buyer["company"]["id"]
    other = ok(api.post("/api/companies", json={"name": "Other AB", "country": "SE", "allow_new": True}), 201)
    other = other["company"]["id"]
    named = mandate(api, "Acquirer AB", top={"buyer_company_id": buyer}, max_rollover_pct="40")
    anon = mandate(api, "Anonymous Fund", max_rollover_pct="40")
    confirmed_profile(api, cid, RETAIN_30)
    ok(api.post("/api/match-runs", json={"company_id": cid}), 201)
    opps = {o["mandate_id"]: o for o in ok(api.get("/api/opportunities"))}
    url = f"/api/opportunities/{opps[named['id']]['id']}"
    proposal = ok(api.post(f"{url}/drafts", json={"authorization": AUTH}), 201)
    basis = proposal["payload"]["basis"]
    assert basis["company_id"] == cid and basis["mandate_version"] == 1 and basis["profile_version"] == 1
    assert len(basis["facts_hash"]) == 64

    jane = contact(api, buyer, "jane@acquirer.example")
    blocked = {
        "contact_unverified": contact(api, buyer, "new@acquirer.example", verify=False),
        "contact_not_buyer_role": contact(api, buyer, "seller@acquirer.example", role="seller"),
        "contact_not_at_buyer_company": contact(api, other, "jane@other.example"),
    }
    for reason, contact_id in blocked.items():
        r = api.post(f"{url}/email-draft", json={"proposal_id": proposal["id"], "contact_id": contact_id})
        assert r.status_code == 409 and reason in r.json()["error"]["details"]["reasons"], (reason, r.text)
    anon_url = f"/api/opportunities/{opps[anon['id']]['id']}"
    anon_prop = ok(api.post(f"{anon_url}/drafts", json={"authorization": AUTH}), 201)
    r = api.post(f"{anon_url}/email-draft", json={"proposal_id": anon_prop["id"], "contact_id": jane})
    assert "mandate_without_buyer_company" in r.json()["error"]["details"]["reasons"]
    r = api.post(f"{anon_url}/email-draft", json={"proposal_id": proposal["id"], "contact_id": jane})
    assert r.json()["error"]["code"] == "proposal_mismatch"

    out = ok(api.post(f"{url}/email-draft", json={"proposal_id": proposal["id"], "contact_id": jane}), 201)
    draft = ok(api.get(f"/api/outreach/drafts/{out['mail_draft_id']}"))
    assert draft["status"] == "draft" and draft["approval"] is None and draft["dispatch"] is None
    assert draft["kind"] == "deal_brief" and draft["company_id"] == buyer and draft["recipients"] == ["jane@acquirer.example"]
    text = draft["subject"] + draft["body"]
    assert "Country: FI" in text and "Industry: Manufacturing" in text and "Undisclosed company" in text
    assert "Nordic" not in text and "12000000" not in text and "Revenue" not in text  # outside the authorized scope
    d = draft["disclosure"]
    assert d["proposal_content_hash"] == proposal["content_hash"] and d["basis"] == basis
    assert d["scope"] == ["country", "industry"] and d["contact_id"] == jane

    with api.app.state.sessionmaker() as db:
        row = db.get(mail.OutreachDraft, uuid.UUID(out["mail_draft_id"]))
        deals.validate_deal_disclosure(db, row.disclosure, row)
        deals.validate_deal_disclosure(db, {"generated": "missing_fields"})  # not a deal brief: no-op
        assert db.query(mail.Dispatch).count() == 0
        for disclosure, body, reason in ((row.disclosure, row.body + "\nRevenue: 12000000 EUR", "message_edited"),
                                         ({}, row.body, None)):
            row.body = body
            with pytest.raises(ApiError) as exc:
                deals.validate_deal_disclosure(db, disclosure, row)
            assert exc.value.status == 409 and (reason is None or reason in exc.value.details["reasons"])
        db.rollback()

    ok(api.patch(f"/api/mandates/{named['id']}", json={"criteria": {"team_commitment": True}}))
    with api.app.state.sessionmaker() as db:
        row = db.get(mail.OutreachDraft, uuid.UUID(out["mail_draft_id"]))
        with pytest.raises(ApiError) as exc:
            deals.validate_deal_disclosure(db, row.disclosure, row)
        assert "mandate_version" in exc.value.details["reasons"]
    r = api.post(f"{url}/email-draft", json={"proposal_id": proposal["id"], "contact_id": jane})
    assert r.status_code == 409 and "mandate_version" in r.json()["error"]["details"]["reasons"]
