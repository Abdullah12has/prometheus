def create(client, **body):
    return client.post("/api/companies", json=body)


def test_business_id_intake_is_idempotent_across_formats(client):
    r = create(client, name="Nokia Oyj", country="fi", business_id="0112038-9")
    assert r.status_code == 201 and r.json()["resolution"] == "created"
    company = r.json()["company"]
    assert company["identifiers"][0] | {"id": None} == {"id": None, "scheme": "business_id", "jurisdiction": "FI", "value": "0112038-9"}
    assert company["status"] == "provisional" and company["seller_intent"] == "unknown"

    again = create(client, name="Different Name", country="FI", business_id="FI01120389")
    assert again.status_code == 200 and again.json()["resolution"] == "matched_business_id"
    assert again.json()["company"]["id"] == company["id"]
    assert client.get("/api/companies").json()["total"] == 1


def test_same_business_id_in_other_jurisdiction_is_separate(client):
    assert create(client, name="A", country="FI", business_id="0112038-9").status_code == 201
    assert create(client, name="A", country="EE", business_id="01120389").status_code == 201


def test_name_only_intake_asks_operator_when_ambiguous(client):
    first = create(client, name="Acme Oy", country="FI").json()["company"]
    r = create(client, name="ACME", country="FI")
    assert r.status_code == 409 and r.json()["error"]["code"] == "ambiguous_company"
    assert [c["id"] for c in r.json()["error"]["details"]["candidates"]] == [first["id"]]
    assert create(client, name="ACME", country="SE").status_code == 201  # other jurisdiction
    assert create(client, name="ACME", country="FI", allow_new=True).status_code == 201


def test_website_intake_matches_domain_but_registry_id_wins(client):
    first = create(client, website="https://www.acme.fi/").json()["company"]
    assert first["name"] == "acme.fi" and first["domain"] == "acme.fi"
    r = create(client, website="acme.fi/contact")
    assert r.status_code == 200 and r.json()["resolution"] == "matched_website"

    # A registry ID is authoritative; a shared domain is only a possible duplicate.
    r = create(client, name="Acme Group Oy", website="acme.fi", country="FI", business_id="0112038-9")
    assert r.status_code == 201
    assert [c["id"] for c in r.json()["possible_duplicates"]] == [first["id"]]


def test_intake_validation(client):
    for body, fragment in [
        ({}, "at least one"),
        ({"business_id": "0112038-9"}, "country is required"),
        ({"business_id": "0112038-8", "country": "FI"}, "checksum"),
        ({"website": "http://10.0.0.1"}, "IP address"),
        ({"name": "X", "country": "Finland"}, "alpha-2"),
    ]:
        r = create(client, **body)
        assert r.status_code == 422, body
        assert r.json()["error"]["code"] == "validation_error"
        assert fragment in r.text
    # Seller intent cannot be set by intake or edit.
    assert create(client, name="X", seller_intent="interested").status_code == 422
    cid = create(client, name="X").json()["company"]["id"]
    assert client.patch(f"/api/companies/{cid}", json={"seller_intent": "interested"}).status_code == 422
    assert client.patch(f"/api/companies/{cid}", json={"name": None}).status_code == 422


def test_list_detail_update_and_search(client):
    cid = create(client, name="Acme Oy", website="acme.fi", country="FI", business_id="0112038-9").json()["company"]["id"]
    create(client, name="Other")
    r = client.patch(f"/api/companies/{cid}", json={"website": "https://www.acme.com", "status": "confirmed"})
    assert r.json()["domain"] == "acme.com" and r.json()["status"] == "confirmed"
    assert client.get("/api/companies", params={"q": "acme"}).json()["total"] == 1
    assert client.get("/api/companies", params={"q": "0112038-9"}).json()["items"][0]["id"] == cid
    assert client.get("/api/companies", params={"q": "FI01120389"}).json()["items"][0]["id"] == cid
    assert client.get("/api/companies", params={"status": "confirmed"}).json()["total"] == 1

    detail = client.get(f"/api/companies/{cid}").json()
    assert [j["kind"] for j in detail["jobs"]] == ["company.enrich"] and detail["jobs"][0]["state"] == "queued"
    assert {a["kind"] for a in detail["activities"]} == {"company.created", "company.updated"}
    assert client.get("/api/companies/00000000-0000-0000-0000-000000000000").json()["error"]["code"] == "not_found"


def test_identifier_conflict_between_companies(client):
    a = create(client, name="A", country="FI", business_id="0112038-9").json()["company"]["id"]
    b = create(client, name="B").json()["company"]["id"]
    r = client.post(f"/api/companies/{b}/identifiers", json={"country": "FI", "business_id": "01120389"})
    assert r.status_code == 409 and r.json()["error"]["details"]["company_id"] == a
    assert client.post(f"/api/companies/{a}/identifiers", json={"country": "FI", "business_id": "0112038-9"}).status_code == 200


def test_country_filter_keeps_pagination_and_search_within_country(client):
    for name, country in [("Alpine One", "CH"), ("Alpine Two", "CH"), ("Alpine Three", "DE")]:
        assert create(client, name=name, country=country).status_code == 201
    first = client.get("/api/companies", params={"country": "ch", "q": "Alpine", "limit": 1}).json()
    second = client.get("/api/companies", params={"country": "CH", "q": "Alpine", "limit": 1, "offset": 1}).json()
    assert first["total"] == second["total"] == 2
    assert first["items"][0]["country"] == second["items"][0]["country"] == "CH"
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert client.get("/api/companies", params={"country": "Germany"}).status_code == 422


def test_swiss_uid_dedup_and_search_accept_both_common_formats(client):
    company = create(client, name="Karara AG", country="CH", business_id="CHE-116.229.879").json()["company"]
    repeated = create(client, name="Karara", country="CH", business_id="CHE116229879")
    assert repeated.json()["company"]["id"] == company["id"]
    assert client.get("/api/companies", params={"q": "CHE116229879"}).json()["items"][0]["id"] == company["id"]


def test_delete_cancels_jobs_and_leaves_content_free_tombstone(client):
    cid = create(client, name="Secret Target Oy").json()["company"]["id"]
    client.post(f"/api/companies/{cid}/contacts", json={"name": "Owner", "email": "o@target.fi"})
    assert client.delete(f"/api/companies/{cid}").json() == {"id": cid, "deleted": True}
    assert client.get(f"/api/companies/{cid}").status_code == 404
    assert client.get("/api/jobs").json()[0]["state"] == "cancelled"
    activities = client.get("/api/activities").json()
    assert [a["kind"] for a in activities] == ["company.deleted"]
    assert "Secret Target" not in str(activities)
    assert client.get("/api/dashboard").json()["contacts"] == 0
