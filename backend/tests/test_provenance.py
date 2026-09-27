import pytest


@pytest.fixture
def ids(client):
    cid = client.post("/api/companies", json={"name": "Acme Oy"}).json()["company"]["id"]
    sid = client.post("/api/sources", json={"kind": "registry", "url": "https://registry.example/acme?id=1",
                                            "title": "Registry extract"}).json()["id"]
    return cid, sid


FIN = {"metric": "revenue", "period_start": "2025-01-01", "period_end": "2025-12-31", "scope": "entity"}


def test_financials_keep_exact_decimals_and_unknowns_null(client, ids):
    cid, sid = ids
    r = client.post(f"/api/companies/{cid}/financials",
                    json=FIN | {"source_id": sid, "amount": "1234567.89", "currency": "EUR", "status": "reported"})
    assert r.status_code == 201 and r.json()["amount"] == "1234567.8900"
    assert r.json()["source"]["id"] == sid
    r = client.post(f"/api/companies/{cid}/financials", json=FIN | {"source_id": sid, "metric": "ebitda", "status": "not_disclosed"})
    assert r.status_code == 201 and r.json()["amount"] is None

    for bad in [
        {"status": "reported"},  # value missing but claimed reported
        {"status": "not_disclosed", "amount": "0", "currency": "EUR"},
        {"status": "reported", "amount": "10"},  # currency missing
        {"status": "derived", "amount": "10", "currency": "EUR"},  # formula missing
        {"status": "reported", "amount": "NaN", "currency": "EUR"},
        {"status": "reported", "amount": "10", "currency": "EUR", "period_end": "2024-01-01"},
        {"status": "reported", "metric": "employees", "amount": "2.5"},
    ]:
        assert client.post(f"/api/companies/{cid}/financials", json=FIN | {"source_id": sid} | bad).status_code == 422, bad
    unknown_source = FIN | {"source_id": cid, "status": "not_disclosed"}
    assert client.post(f"/api/companies/{cid}/financials", json=unknown_source).json()["error"]["code"] == "unknown_source"


def test_evidence_links_source_span(client, ids):
    cid, sid = ids
    r = client.post(f"/api/companies/{cid}/evidence", json={
        "source_id": sid, "field": "employees", "value": 42, "excerpt": "Henkilöstö: 42", "locator": {"page": 2}})
    assert r.status_code == 201 and r.json()["review_status"] == "proposed"
    detail = client.get(f"/api/companies/{cid}").json()
    assert detail["evidence"][0]["source"]["url"] == "https://registry.example/acme?id=1"
    assert client.post("/api/sources", json={"kind": "website", "url": "file:///etc/passwd"}).status_code == 422
    assert client.post("/api/sources", json={"kind": "manual"}).status_code == 422


def test_seller_intent_changes_only_on_confirmed_authorized_statement(client, ids):
    cid, _ = ids
    stmt = {"speaker_name": "Owner", "stance": "interested", "statement": "Yes, send information.",
            "stated_at": "2026-09-01T10:00:00Z"}
    client.post(f"/api/companies/{cid}/intent-statements", json=stmt | {"speaker_authority": "owner"})
    assert client.get(f"/api/companies/{cid}").json()["seller_intent"] == "unknown"

    r = client.post(f"/api/companies/{cid}/intent-statements", json=stmt | {"speaker_authority": "unverified", "confirmed": True})
    assert r.status_code == 422

    client.post(f"/api/companies/{cid}/intent-statements", json=stmt | {"speaker_authority": "owner", "confirmed": True})
    assert client.get(f"/api/companies/{cid}").json()["seller_intent"] == "interested"
    # An older confirmed statement does not override a newer one.
    client.post(f"/api/companies/{cid}/intent-statements", json=stmt | {
        "speaker_authority": "owner", "confirmed": True, "stance": "not_interested", "stated_at": "2026-01-01T10:00:00Z"})
    assert client.get(f"/api/companies/{cid}").json()["seller_intent"] == "interested"


def test_contacts_normalize_and_dedupe(client, ids):
    cid, _ = ids
    r = client.post(f"/api/companies/{cid}/contacts",
                    json={"name": "Anna", "email": " Anna@Acme.FI ", "phone": "+358 40 123 4567", "person_role": "owner"})
    assert r.status_code == 201
    assert (r.json()["email"], r.json()["phone"], r.json()["contact_role"]) == ("anna@acme.fi", "+358401234567", "seller")
    dup = client.post(f"/api/companies/{cid}/contacts", json={"name": "A", "email": "anna@acme.fi"})
    assert dup.json()["error"]["code"] == "contact_exists"
    assert client.post(f"/api/companies/{cid}/contacts", json={"name": "B", "email": "nope"}).status_code == 422
    assert client.get("/api/dashboard").json()["contacts"] == 1
