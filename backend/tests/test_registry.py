import csv
from contextlib import nullcontext
import io
import json
import uuid
import zipfile
from collections import Counter

import pytest
from sqlalchemy import create_engine, event, func, select

from permetheus import registry, research, worker
from permetheus import registry_sources as rs
from permetheus.config import Settings
from permetheus.db import init_db, make_sessionmaker
from permetheus.models import Activity, Company, CompanyIdentifier, Evidence, Job, JobState, Source
from permetheus.research_models import JOB_KIND_ENRICH, ResearchRun, ResearchRunStatus

PRH_ACTIVE = {"businessId": {"value": "0112038-9"}, "names": [
    {"name": "Old Name Oy", "type": "1", "endDate": "2001-01-01"}, {"name": "Nokia Oyj", "type": "1", "endDate": None}],
    "mainBusinessLine": {"type": "26300", "descriptions": [{"languageCode": "3", "description": "Manufacture of equipment"}]},
    "website": {"url": "www.nokia.com"}, "companyForms": [{"type": "17", "descriptions": [], "endDate": None}],
    "addresses": [{"type": 1, "street": "Karakaari", "buildingNumber": "7", "postCode": "02610",
                   "postOffices": [{"city": "ESPOO"}]}], "registrationDate": "1896-01-01"}
PRH_ENDED = {"businessId": {"value": "0184194-4"}, "names": [{"name": "Gone Oy", "type": "1"}], "endDate": "2020-01-01"}


GLEIF_API_RECORD = {"id": "05NYH65HUDFHD5XPKR75", "attributes": {
    "lei": "05NYH65HUDFHD5XPKR75", "registration": {"status": "ISSUED"}, "entity": {
        "legalName": {"name": "Raytheon Deutschland GmbH"}, "status": "ACTIVE", "jurisdiction": "DE",
        "registeredAs": "HRB 118213", "registeredAt": {"id": "RA000304"},
        "legalForm": {"id": "2HBR"}, "category": "GENERAL", "creationDate": "1997-10-20T22:00:00Z",
        "legalAddress": {"addressLines": ["Kulturstr. 105"], "city": "Freising", "region": "DE-BY",
                         "country": "DE", "postalCode": "85356"}}}}
GLEIF_CSV_HEADERS = ["lei", "Entity.LegalName.Name", "Entity.Status", "Entity.LegalJurisdiction",
                     "Entity.RegistrationAuthority.RegistrationAuthorityEntityID",
                     "Entity.RegistrationAuthority.RegistrationAuthorityID",
                     "Registration.ValidationAuthority.ValidationAuthorityEntityID",
                     "Registration.ValidationAuthority.ValidationAuthorityID", "Entity.LegalForm.EntityLegalFormCode",
                     "Entity.EntityCategory", "Entity.EntityCreationDate", "Registration.RegistrationStatus",
                     "Entity.LegalAddress.AddressLine1", "Entity.LegalAddress.City", "Entity.LegalAddress.Region",
                     "Entity.LegalAddress.CountryCode", "Entity.LegalAddress.PostalCode"]
GLEIF_CSV_RECORD = {"lei": "05NYH65HUDFHD5XPKR75", "Entity.LegalName.Name": "Raytheon Deutschland GmbH",
                    "Entity.Status": "ACTIVE", "Entity.LegalJurisdiction": "DE",
                    "Entity.RegistrationAuthority.RegistrationAuthorityEntityID": "HRB 118213",
                    "Entity.RegistrationAuthority.RegistrationAuthorityID": "RA000304",
                    "Registration.ValidationAuthority.ValidationAuthorityEntityID": "Different validation record",
                    "Registration.ValidationAuthority.ValidationAuthorityID": "RA000999",
                    "Entity.LegalForm.EntityLegalFormCode": "2HBR", "Entity.EntityCategory": "GENERAL",
                    "Entity.EntityCreationDate": "1997-10-21T00:00:00+02:00",
                    "Registration.RegistrationStatus": "ISSUED", "Entity.LegalAddress.AddressLine1": "Kulturstr. 105",
                    "Entity.LegalAddress.City": "Freising", "Entity.LegalAddress.Region": "DE-BY",
                    "Entity.LegalAddress.CountryCode": "DE", "Entity.LegalAddress.PostalCode": "85356"}


def write_gleif_archive(data_dir, rows):
    directory = data_dir / "imports" / "gleif"
    directory.mkdir(parents=True)
    filename = "20260927-0000-gleif-goldencopy-lei2-golden-copy.csv.zip"
    path = directory / filename
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        with archive.open(filename.removesuffix(".zip"), "w") as raw:
            stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            writer = csv.DictWriter(stream, fieldnames=GLEIF_CSV_HEADERS)
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
    url = f"https://goldencopy.gleif.org/storage/golden-copy-files/2026/09/27/1/{filename}"
    (directory / "latest-metadata.json").write_text(json.dumps({"data": {
        "publish_date": "2026-09-27 00:00:00",
        "full_file": {"csv": {"url": url, "record_count": len(rows), "size": path.stat().st_size}},
    }}))
    return path, url


@pytest.fixture
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    init_db(engine)
    registry.init(engine)
    yield make_sessionmaker(engine)
    engine.dispose()


def entity(value="CHE-116.229.879", country="CH", name="Karara AG", **kw):
    return rs.Entity(country, "business_id", value, name, f"https://example.test/{value}",
                     {"legal_name": name, "uid": value, **kw.pop("fields", {})}, **kw)


def source_row(db):
    row = Source(kind="registry", url="https://lindas.admin.ch/query", title="t")
    db.add(row)
    db.flush()
    return row.id


def test_json_array_stream_parses_items_across_chunk_boundaries():
    items = [{"i": i, "text": "ä" * i} for i in range(50)]
    data = json.dumps(items).encode()
    assert [x for x, _ in rs.iter_json_array(io.BytesIO(data), chunk_size=7)] == items
    assert list(rs.iter_json_array(io.BytesIO(b" [ ] "))) == []
    with pytest.raises(ValueError):
        list(rs.iter_json_array(io.BytesIO(data[:-20]), chunk_size=7))


def test_source_record_normalization():
    fi, reason = rs.prh_entity(PRH_ACTIVE)
    assert reason is None and (fi.value, fi.name, fi.website) == ("0112038-9", "Nokia Oyj", "www.nokia.com")
    assert fi.industry == "Manufacture of equipment" and fi.fields["address"]["city"] == "ESPOO"
    assert rs.prh_entity(PRH_ENDED) == (None, "inactive")
    assert rs.prh_entity({**PRH_ACTIVE, "tradeRegisterStatus": "0"})[0].registry_status == "unregistered"
    assert rs.prh_entity({**PRH_ACTIVE, "tradeRegisterStatus": "1"})[0].registry_status == "registered"
    assert rs.prh_entity(PRH_ACTIVE)[0].registry_status is None
    assert rs.prh_entity({**PRH_ACTIVE, "businessId": {"value": "0112038-1"}}) == (None, "invalid_identifier")

    ch, _ = rs.zefix_entity({"s": "https://register.ld.admin.ch/zefix/company/1", "name": "Karara AG",
                             "uid": "CHE116229879", "form": "https://ld.admin.ch/ech/97/legalforms/0106",
                             "purpose": "Beteiligungen"})
    assert (ch.value, ch.fields["legal_form_code"], ch.description) == ("CHE-116.229.879", "0106", "Beteiligungen")
    assert rs.zefix_entity({"s": "x", "name": "No UID AG"}) == (None, "invalid_identifier")

    de, _ = rs.gleif_entity({"id": "984500550BE591A09022", "attributes": {"lei": "984500550BE591A09022", "entity": {
        "legalName": {"name": "Daub Trading GmbH"}, "registeredAs": "HRB 752754", "status": "ACTIVE",
        "legalAddress": {"addressLines": ["Grabenstraße 36"], "city": "Gerabronn", "country": "DE"}}}})
    assert (de.country, de.scheme, de.value, de.fields["registered_as"]) == ("DE", "lei", "984500550BE591A09022",
                                                                             "HRB 752754")


def test_persist_batch_dedupes_fills_only_empty_fields_and_never_duplicates_evidence(Session):
    with Session() as db:
        human = Company(name="Karara (reviewed)", name_normalized="karara", description="Human description")
        db.add(human)
        db.flush()
        db.add(CompanyIdentifier(company_id=human.id, scheme="business_id", jurisdiction="CH", value="CHE-116.229.879"))
        db.commit()
        src = source_row(db)
        batch = [entity(description="Registry purpose"), entity(),  # duplicate inside one batch
                 entity("CHE-116.229.891", name="Hedonia AG", website="hedonia.ch"),
                 entity("0112038-9", country="FI", name="Same value, other country")]
        assert registry.persist_batch(db, source="zefix_lindas", source_row_id=src, import_id=uuid.uuid4(),
                                      entities=batch) == (2, 1)
        db.commit()
        # Re-import of identical data: all matched, no new evidence rows.
        assert registry.persist_batch(db, source="zefix_lindas", source_row_id=src, import_id=uuid.uuid4(),
                                      entities=batch) == (0, 3)
        db.commit()
        db.expire_all()
        human = db.get(Company, human.id)
        assert (human.name, human.description, human.country, human.registry_status) == (
            "Karara (reviewed)", "Human description", "CH", "active")
        assert db.scalar(select(func.count()).select_from(Evidence)) == 3
        hedonia = db.scalar(select(Company).where(Company.name == "Hedonia AG"))
        assert (hedonia.country, hedonia.domain, hedonia.identifiers[0].source_id) == ("CH", "hedonia.ch", src)
        assert db.scalar(select(func.count()).select_from(CompanyIdentifier).where(
            CompanyIdentifier.value == "0112038-9", CompanyIdentifier.jurisdiction == "FI")) == 1
        # Changed registry fields add one new snapshot row for that company only.
        registry.persist_batch(db, source="zefix_lindas", source_row_id=src, import_id=uuid.uuid4(),
                               entities=[entity("CHE-116.229.891", name="Hedonia AG", fields={"purpose": "new"})])
        db.commit()
        assert db.scalar(select(func.count()).select_from(Evidence)) == 4


def start(Session, source="zefix_lindas"):
    with Session() as db:
        imp = registry.RegistryImport(source=source, country="CH", checkpoint={}, skip_reasons={}, errors=[])
        db.add(imp)
        db.flush()
        registry._queue_job(db, imp)
        db.commit()
        return imp.id, imp.job_id


def fake_adapter(pages, calls):
    def adapter(checkpoint, beat, errors, data_dir):
        calls.append(dict(checkpoint))
        for n in range(checkpoint.get("page", 0), len(pages)):
            beat()
            errors.append(f"page {n} slow") if n == 0 else None
            yield rs.Batch(pages[n], {"page": n + 1}, Counter({"inactive": 1}), seen=len(pages[n]) + 1,
                           exhausted=n == len(pages) - 1, total=10, progress=(n + 1) / len(pages), snapshot="2026-09-27")
    return adapter


def test_import_job_pauses_and_resumes_from_checkpoint(Session, monkeypatch, tmp_path):
    pages = [[entity("CHE-100.000.001", name="A AG"), entity("CHE-100.000.002", name="B AG")],
             [entity("CHE-100.000.003", name="C AG")], [entity("CHE-100.000.004", name="D AG")]]
    calls = []
    settings = Settings(_env_file=None, database_url="sqlite://", root_dir=tmp_path)
    import_id, job_id = start(Session)
    beats = {"n": 0}
    real_heartbeat = research._heartbeat

    def pausing_heartbeat(db, jid, fencing):
        beat = real_heartbeat(db, jid, fencing)
        def wrapped():
            beats["n"] += 1
            if beats["n"] == 2:  # operator pauses while page 2 is being fetched
                with Session() as other:
                    registry.pause_import(import_id, other)
            beat()
        return wrapped

    monkeypatch.setitem(rs.ADAPTERS, "zefix_lindas", fake_adapter(pages, calls))
    monkeypatch.setattr(research, "_heartbeat", pausing_heartbeat)
    assert registry.process_one(Session, settings)
    with Session() as db:
        imp = db.get(registry.RegistryImport, import_id)
        assert (imp.status, imp.checkpoint, imp.created, imp.processed) == ("paused", {"page": 1}, 2, 3)
        assert db.get(Job, job_id).state == JobState.cancelled
        assert imp.errors and "page 0 slow" in imp.errors[0]
        assert not registry.process_one(Session, settings)  # paused: nothing claimable
        registry.resume_import(import_id, db)

    monkeypatch.setattr(research, "_heartbeat", real_heartbeat)
    assert registry.process_one(Session, settings)
    assert calls[-1] == {"page": 1}
    with Session() as db:
        imp = db.get(registry.RegistryImport, import_id)
        assert (imp.status, imp.exhausted, imp.created, imp.skipped, imp.processed) == ("completed", True, 4, 3, 7)
        assert imp.skip_reasons == {"inactive": 3} and imp.source_total == 10
        assert db.get(Job, job_id).state == JobState.succeeded
        assert db.scalar(select(func.count()).select_from(Company).where(Company.country == "CH")) == 4
        assert db.scalar(select(func.count()).select_from(Source)) == 1  # one Source per import, even across resume
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == JOB_KIND_ENRICH)) == 0
        assert db.scalar(select(Activity).where(Activity.kind == "registry.import_completed"))


def test_app_shutdown_requeues_import_without_spending_retry_budget(Session, monkeypatch, tmp_path):
    settings = Settings(_env_file=None, database_url="sqlite://", root_dir=tmp_path)
    import_id, job_id = start(Session)

    def stopping(checkpoint, beat, errors, data_dir):
        yield rs.Batch([entity()], {"page": 1}, seen=1)
        registry.STOP.set()
        beat()
        pytest.fail("beat must raise on shutdown")

    monkeypatch.setitem(rs.ADAPTERS, "zefix_lindas", stopping)
    try:
        assert registry.process_one(Session, settings)
    finally:
        registry.STOP.clear()
    with Session() as db:
        imp, job = db.get(registry.RegistryImport, import_id), db.get(Job, job_id)
        assert (imp.status, imp.checkpoint, job.state, worker.attempts_used(job)) == ("queued", {"page": 1}, "queued", 0)


def test_import_failures_back_off_keep_checkpoint_then_fail(Session, monkeypatch, tmp_path):
    settings = Settings(_env_file=None, database_url="sqlite://", root_dir=tmp_path)
    import_id, job_id = start(Session)

    def broken(checkpoint, beat, errors, data_dir):
        yield rs.Batch([entity()], {"page": 1}, seen=1)
        raise rs.SourceError("LINDAS returned 503")

    monkeypatch.setitem(rs.ADAPTERS, "zefix_lindas", broken)
    for attempt in range(1, worker.MAX_ATTEMPTS + 1):
        assert registry.process_one(Session, settings)
        with Session() as db:
            job = db.get(Job, job_id)
            job.available_at = job.created_at
            db.commit()
    with Session() as db:
        imp = db.get(registry.RegistryImport, import_id)
        assert db.get(Job, job_id).state == JobState.failed
        assert imp.status == "failed" and imp.checkpoint == {"page": 1} and imp.created == 1
        assert any("503" in e for e in imp.errors)
        registry.resume_import(import_id, db)  # failed imports resume from the checkpoint
        assert db.get(Job, job_id).state == JobState.queued


def add_companies(db, country, n):
    ids = []
    for i in range(n):
        c = Company(name=f"{country} {i}", name_normalized=f"{country} {i}", country=country)
        db.add(c)
        db.flush()
        ids.append(c.id)
    db.commit()
    return ids


def test_campaign_feeds_bounded_batches_skips_researched_and_pauses(Session):
    with Session() as db:
        ch = add_companies(db, "CH", 5)
        add_companies(db, "FI", 3)
        db.add(ResearchRun(company_id=ch[0], status=ResearchRunStatus.completed))
        campaign = registry.EnrichmentCampaign(country="CH", max_in_flight=2)
        db.add(campaign)
        db.commit()
        cid = campaign.id
    assert registry.feed_enrichment(Session) == 2
    assert registry.feed_enrichment(Session) == 0  # in-flight cap reached
    with Session() as db:
        jobs = db.scalars(select(Job).where(Job.kind == JOB_KIND_ENRICH)).all()
        assert {j.company_id for j in jobs} <= set(ch[1:]) and len(jobs) == 2
        registry.pause_campaign(cid, db)
        assert registry._job_counts(db, cid)["cancelled"] == 2
    assert registry.feed_enrichment(Session) == 0  # paused: nothing fed
    with Session() as db:
        registry.resume_campaign(cid, db)
        for job in db.scalars(select(Job).where(Job.kind == JOB_KIND_ENRICH)):
            job.state = JobState.succeeded
        db.commit()
    assert registry.feed_enrichment(Session) == 2
    assert registry.feed_enrichment(Session) == 0
    with Session() as db:
        for job in db.scalars(select(Job).where(Job.kind == JOB_KIND_ENRICH)):
            job.state = JobState.succeeded
        db.commit()
    assert registry.feed_enrichment(Session) == 0
    with Session() as db:
        campaign = db.get(registry.EnrichmentCampaign, cid)
        assert campaign.enqueued == 4 and campaign.exhausted and campaign.status == "running"
        enriched = set(db.scalars(select(Job.company_id).where(Job.kind == JOB_KIND_ENRICH)))
        assert enriched == set(ch[1:])


def test_swiss_enrichment_never_queries_the_finnish_registry(monkeypatch, tmp_path):
    company = Company(name="Karara AG", name_normalized="karara", country="CH")
    company.identifiers = [CompanyIdentifier(scheme="business_id", jurisdiction="CH", value="CHE-116.229.879")]
    target = research.enrichment_target(company)
    assert (target.business_id, target.id_label) == ("CHE-116.229.879", "Swiss UID")
    monkeypatch.setattr(research, "resolve_registry", lambda *a: pytest.fail("PRH queried for a Swiss company"))
    settings = Settings(_env_file=None, database_url="sqlite://", root_dir=tmp_path, searxng_url=None)
    f = research.Findings()
    research._gather(target, f, llm=type("L", (), {"configured": False})(), settings=settings, beat=lambda: None)
    assert "registry" not in f.checked and any("PRH covers Finland only" in m for m in f.missing)
    assert "Swiss UID CHE-116.229.879" in research.extraction_instruction(target)


def test_prh_bulk_stream_resumes_from_record_checkpoint(tmp_path, monkeypatch):
    dest = tmp_path / "imports" / "prh"
    dest.mkdir(parents=True)
    records = [PRH_ACTIVE, PRH_ENDED, {**PRH_ACTIVE, "businessId": {"value": "0184194-4"}}]
    with zipfile.ZipFile(dest / "all_companies_20260927.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data_20260927.json", json.dumps(records))
    monkeypatch.setattr(rs, "_prh_download", lambda *a: pytest.fail("pinned file must not be re-downloaded"))
    batches = list(rs.prh_batches({"file": "all_companies_20260927.zip"}, lambda: None, [], tmp_path, size=2))
    assert [len(b.entities) for b in batches] == [1, 1] and batches[-1].exhausted and batches[-1].total == 3
    assert batches[0].skipped == Counter({"inactive": 1}) and batches[0].snapshot == "2026-09-27"
    resumed = list(rs.prh_batches(batches[0].checkpoint, lambda: None, [], tmp_path, size=2))
    assert [e.value for b in resumed for e in b.entities] == ["0184194-4"] and resumed[0].seen == 1
    with pytest.raises(rs.SourceError, match="archive changed"):
        list(rs.prh_batches({**batches[0].checkpoint, "sha256": "incorrect"}, lambda: None, [], tmp_path))


def test_gleif_bulk_csv_filters_resumes_and_matches_api_fields(Session, tmp_path):
    foreign = {**GLEIF_CSV_RECORD, "Entity.LegalAddress.CountryCode": "FR"}
    inactive = {**GLEIF_CSV_RECORD, "lei": "5493001KJTIIGC8Y1R12", "Entity.Status": "INACTIVE"}
    path, url = write_gleif_archive(tmp_path, [foreign, inactive, GLEIF_CSV_RECORD])
    errors = []
    batches = list(rs.gleif_batches({}, lambda: None, errors, tmp_path, size=2))
    assert len(batches) == 2 and batches[0].checkpoint["source"] == "gleif_csv"
    assert batches[0].checkpoint["file"] == path.name and batches[0].checkpoint["row"] == 2
    assert batches[0].source_url == url and batches[0].content_hash
    assert batches[0].total == 3 and batches[0].progress == 2 / 3
    assert batches[0].skipped == Counter({"other_country": 1, "inactive": 1})
    csv_entity = batches[1].entities[0]
    api_entity, reason = rs.gleif_entity(GLEIF_API_RECORD)
    assert reason is None and csv_entity == api_entity
    assert batches[1].checkpoint["row"] == 3 and batches[1].exhausted and batches[1].progress == 1

    resumed = list(rs.gleif_batches(batches[0].checkpoint, lambda: None, [], tmp_path, size=2))
    assert [entity.value for batch in resumed for entity in batch.entities] == [csv_entity.value]
    assert resumed[0].seen == 1 and resumed[0].exhausted
    with pytest.raises(rs.SourceError, match="archive changed"):
        list(rs.gleif_batches({**batches[0].checkpoint, "sha256": "incorrect"}, lambda: None, [], tmp_path))

    with Session() as db:
        src = source_row(db)
        assert registry.persist_batch(db, source="gleif_de", source_row_id=src,
                                      import_id=uuid.uuid4(), entities=[api_entity]) == (1, 0)
        db.commit()
        assert registry.persist_batch(db, source="gleif_de", source_row_id=src,
                                      import_id=uuid.uuid4(), entities=[csv_entity]) == (0, 1)
        db.commit()
        assert db.scalar(select(func.count()).select_from(Evidence)) == 1


def test_gleif_bulk_csv_absent_keeps_api_fallback(monkeypatch, tmp_path):
    class Response:
        status_code = 200
        def json(self):
            return {"data": [GLEIF_API_RECORD], "meta": {"pagination": {"total": 1},
                    "goldenCopy": {"publishDate": "2026-09-27T00:00:00Z"}}, "links": {}}

    monkeypatch.setattr(rs, "client", nullcontext)
    monkeypatch.setattr(rs, "request", lambda *a, **kw: Response())
    monkeypatch.setattr(rs.time, "sleep", lambda *_: None)
    batches = list(rs.gleif_batches({}, lambda: None, [], tmp_path))
    assert len(batches) == 1 and batches[0].entities[0].value == "05NYH65HUDFHD5XPKR75"
    assert batches[0].checkpoint["next"] is None and batches[0].exhausted


def test_registry_api_contract(client):
    sources = client.get("/api/registry/sources").json()
    assert {s["id"]: s["country"] for s in sources} == {"prh_bulk": "FI", "zefix_lindas": "CH", "gleif_de": "DE"}
    assert "not the German commercial register" in next(s for s in sources if s["id"] == "gleif_de")["coverage"]
    r = client.post("/api/registry/imports", json={"source": "gleif_de"})
    assert r.status_code == 202 and r.json()["status"] == "queued" and r.json()["job_state"] == "queued"
    import_id = r.json()["id"]
    assert client.post("/api/registry/imports", json={"source": "gleif_de"}).json()["id"] == import_id
    assert client.post("/api/registry/imports", json={"source": "other"}).status_code == 422
    assert client.post(f"/api/registry/imports/{import_id}/resume").status_code == 409
    paused = client.post(f"/api/registry/imports/{import_id}/pause").json()
    assert (paused["status"], paused["job_state"]) == ("paused", "cancelled")
    assert client.post(f"/api/registry/imports/{import_id}/resume").json()["job_state"] == "queued"
    campaign = client.post("/api/registry/enrichment", json={"country": "DE", "max_in_flight": 1})
    assert campaign.status_code == 202 and campaign.json()["jobs"]["queued"] == 0
    assert client.post("/api/registry/enrichment", json={"country": "DE"}).status_code == 200
    assert client.post("/api/registry/enrichment", json={"country": "SE"}).status_code == 422
    status = client.get("/api/registry/status").json()
    de = next(c for c in status["countries"] if c["country"] == "DE")
    assert de["companies"] == 0 and de["imports"][0]["id"] == import_id
    assert de["enrichment"]["campaign"]["id"] == campaign.json()["id"] and status["registry_worker"] is False
