"""Network adapters for country registry population. No database access.

Each source turns a JSON checkpoint into a stream of ``Batch``es; the caller
persists a batch and stores ``batch.checkpoint`` in the same transaction, so a
restart resumes after the last committed batch. Endpoints verified live on
2026-09-27:

- ``prh_bulk``: PRH open data ``/all_companies`` (daily zip, one JSON array,
  CC BY 4.0). Streamed from disk; never extracted or loaded whole.
- ``zefix_lindas``: Zefix open linked data on the LINDAS SPARQL endpoint
  (active commercial-register entities). IRI keyset paging, never OFFSET.
- ``gleif_de``: GLEIF LEI records with a German legal address and ACTIVE
  entity status, from a local Golden Copy CSV when available or API cursor
  paging otherwise. A subset: only entities with an LEI, not the register.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import json
import re
import time
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from io import TextIOWrapper
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .acquisition import USER_AGENT
from .identity import normalize_business_id

Beat = Callable[[], None]

PRH_BULK_URL = "https://avoindata.prh.fi/opendata-ytj-api/v3/all_companies"
PRH_RECORD_URL = "https://avoindata.prh.fi/opendata-ytj-api/v3/companies?businessId={}"
LINDAS_URL = "https://lindas.admin.ch/query"
ZEFIX_GRAPH = "https://lindas.admin.ch/foj/zefix"
GLEIF_URL = "https://api.gleif.org/api/v1/lei-records"
GLEIF_FIRST = (f"{GLEIF_URL}?filter%5Bentity.legalAddress.country%5D=DE&filter%5Bentity.status%5D=ACTIVE"
               "&page%5Bsize%5D=200&page%5Bcursor%5D=%2A")
GLEIF_FILE_SOURCE = "gleif_csv"
GLEIF_BULK_BATCH = 10_000
GLEIF_ARCHIVE = re.compile(r"^\d{8}-\d{4}-gleif-goldencopy-lei2-golden-copy\.csv\.zip$")

ZEFIX_PAGE = 5000
PRH_BATCH = 1000
MAX_TRIES = 6
UID = re.compile(r"^CHE(\d{3})(\d{3})(\d{3})$")
LEI = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")


class SourceError(Exception):
    """A source failed after in-handler retries; the job backs off and resumes."""


@dataclass
class Entity:
    country: str
    scheme: str
    value: str
    name: str
    record_url: str
    fields: dict[str, Any]
    industry: str | None = None
    description: str | None = None
    website: str | None = None
    registry_status: str | None = "active"


@dataclass
class Batch:
    entities: list[Entity]
    checkpoint: dict[str, Any]
    skipped: Counter = field(default_factory=Counter)
    seen: int = 0
    exhausted: bool = False
    total: int | None = None
    progress: float | None = None
    snapshot: str | None = None
    source_url: str | None = None
    content_hash: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class SourceInfo:
    id: str
    country: str
    label: str
    publisher: str
    url: str
    license: str
    scheme: str
    coverage: str
    limits: str


SOURCES = {s.id: s for s in (
    SourceInfo("prh_bulk", "FI", "Finnish Trade Register (PRH open data)", "PRH", PRH_BULK_URL, "CC BY 4.0",
               "business_id",
               "Daily bulk file of all companies on the Finnish Trade Register and pending companies. "
               "Records with an end date are skipped as inactive.",
               "No email or phone data. Websites only where the company registered one. Private traders "
               "outside the Trade Register are not included."),
    SourceInfo("zefix_lindas", "CH", "Swiss commercial register (Zefix open linked data)",
               "Federal Commercial Registry Office", LINDAS_URL,
               "Open government data; terms per FCRO legal bases", "business_id",
               "Daily baseline data for active legal entities in the Swiss commercial register: name, UID, "
               "legal form, seat, address and registered purpose.",
               "No websites, email, phone or financials. Deleted entities are not included."),
    SourceInfo("gleif_de", "DE", "German entities with an LEI (GLEIF)", "GLEIF", GLEIF_URL, "CC0 1.0", "lei",
               "Only entities that hold an LEI with a German legal address and ACTIVE status. This is a "
               "subset, not the German commercial register.",
               "The Handelsregister portal forbids systematic retrieval to build parallel registers and "
               "offers no open bulk file, so German companies without an LEI are not covered."),
)}


# ---------------------------------------------------------------------------
# HTTP with rate-aware backoff
# ---------------------------------------------------------------------------

def _sleep(seconds: float, beat: Beat) -> None:
    """Sleep in short steps, renewing the lease (and noticing a pause)."""
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        time.sleep(min(left, 15))
        beat()


def request(client: httpx.Client, method: str, url: str, beat: Beat, errors: list[str], **kw) -> httpx.Response:
    """Retry transport errors, 429 and 5xx with exponential backoff, honouring
    Retry-After. Other 4xx are returned so the caller can decide."""
    for attempt in range(MAX_TRIES):
        try:
            response = client.request(method, url, **kw)
        except httpx.HTTPError as exc:
            reason, wait = f"transport: {type(exc).__name__}", 2 ** attempt * 5
        else:
            if response.status_code != 429 and response.status_code < 500:
                return response
            retry_after = response.headers.get("Retry-After", "")
            wait = int(retry_after) if retry_after.isdigit() else 2 ** attempt * 5
            reason = f"http {response.status_code}"
        wait = min(wait, 300)
        errors.append(f"{reason}; retrying in {wait}s")
        _sleep(wait, beat)
    raise SourceError(f"{url.split('?')[0]}: gave up after {MAX_TRIES} tries ({errors[-1] if errors else ''})")


def client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(60, connect=15),
                        follow_redirects=False)


# ---------------------------------------------------------------------------
# Finland: PRH bulk file
# ---------------------------------------------------------------------------

def _prh_download(dest_dir: Path, beat: Beat, errors: list[str]) -> Path:
    """Fetch today's bulk zip into ``dest_dir``, reusing a complete local copy
    and resuming a partial one with HTTP Range."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with client() as c:
        head = request(c, "HEAD", PRH_BULK_URL, beat, errors)
        if head.status_code != 200:
            raise SourceError(f"PRH bulk HEAD returned {head.status_code}")
        disposition = head.headers.get("content-disposition", "")
        match = re.search(r'filename="?(all_companies_\d{8}\.zip)', disposition)
        name = match.group(1) if match else f"all_companies_{date.today():%Y%m%d}.zip"
        size = int(head.headers.get("content-length", 0))
        path = dest_dir / name
        if path.exists() and (not size or path.stat().st_size == size):
            return path
        part = path.with_suffix(".zip.part")
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        with c.stream("GET", PRH_BULK_URL, headers=headers) as response:
            if response.status_code not in (200, 206):
                raise SourceError(f"PRH bulk download returned {response.status_code}")
            mode = "ab" if response.status_code == 206 else "wb"
            written = 0
            with part.open(mode) as out:
                for chunk in response.iter_bytes(1 << 20):
                    out.write(chunk)
                    written += len(chunk)
                    if written % (16 << 20) < len(chunk):
                        beat()
        if size and part.stat().st_size != size:
            raise SourceError(f"PRH bulk download incomplete: {part.stat().st_size} of {size} bytes")
        part.rename(path)
        return path


def iter_json_array(stream, beat: Beat | None = None, chunk_size: int = 1 << 20) -> Iterator[tuple[dict, int]]:
    """Yield ``(item, bytes_read)`` from a binary stream holding one JSON
    array, holding at most about one chunk plus one item in memory."""
    decoder = json.JSONDecoder()
    text = codecs.getincrementaldecoder("utf-8")()
    buf, pos, read, started, eof = "", 0, 0, False, False
    while True:
        while pos < len(buf) and buf[pos] in " \t\r\n,":
            pos += 1
        if not started and pos < len(buf):
            if buf[pos] != "[":
                raise ValueError("expected a JSON array")
            started, pos = True, pos + 1
            continue
        if started and pos < len(buf) and buf[pos] == "]":
            return
        if pos < len(buf):
            try:
                item, end = decoder.raw_decode(buf, pos)
            except json.JSONDecodeError:
                if eof:
                    raise
            else:
                pos = end
                yield item, read
                continue
        if eof:
            if started:
                raise ValueError("JSON array is truncated")
            return
        if len(buf) - pos > 64 << 20:
            raise ValueError("JSON array item larger than 64 MiB; refusing to buffer further")
        chunk = stream.read(chunk_size)
        read += len(chunk)
        eof = not chunk
        buf = buf[pos:] + text.decode(chunk, final=eof)
        pos = 0
        if beat and chunk:
            beat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _described(entry: dict | None, lang: str = "3") -> str | None:
    descriptions = (entry or {}).get("descriptions") or []
    by_lang = {d.get("languageCode"): d.get("description") for d in descriptions}
    return by_lang.get(lang) or by_lang.get("1") or None


def prh_entity(raw: dict) -> tuple[Entity | None, str | None]:
    """Normalize one PRH bulk record, or return a skip reason."""
    if raw.get("endDate"):
        return None, "inactive"
    try:
        business_id = normalize_business_id("FI", (raw.get("businessId") or {}).get("value") or "")
    except ValueError:
        return None, "invalid_identifier"
    names = [n for n in raw.get("names") or [] if not n.get("endDate")]
    name = next((n["name"] for n in names if n.get("type") == "1" and n.get("name")), None) \
        or next((n["name"] for n in names if n.get("name")), None)
    if not name:
        return None, "missing_name"
    line = raw.get("mainBusinessLine") or {}
    forms = [f for f in raw.get("companyForms") or [] if not f.get("endDate")]
    address = next((a for a in raw.get("addresses") or [] if a.get("type") == 1), None) or {}
    offices = address.get("postOffices") or []
    website = (raw.get("website") or {}).get("url") or None
    fields = {
        "business_id": business_id, "legal_name": name, "euid": (raw.get("euId") or {}).get("value"),
        "company_form": _described(forms[0]) if forms else None,
        "registration_date": raw.get("registrationDate"),
        "main_business_line": {"code": line.get("type"), "description": _described(line)} if line else None,
        "website": website, "trade_register_status": raw.get("tradeRegisterStatus"), "status": raw.get("status"),
        "address": {"street": " ".join(p for p in (address.get("street"), address.get("buildingNumber")) if p) or None,
                    "post_code": address.get("postCode"), "city": offices[0].get("city") if offices else None}
        if address else None,
        "last_modified": raw.get("lastModified"),
    }
    return Entity("FI", "business_id", business_id, name, PRH_RECORD_URL.format(business_id), fields,
                  industry=fields["main_business_line"]["description"] if line else None, website=website,
                  registry_status={"0": "unregistered", "1": "registered", "2": "removed",
                                   "3": "pending", "4": "ceased"}.get(raw.get("tradeRegisterStatus"))), None


def prh_batches(checkpoint: dict, beat: Beat, errors: list[str], data_dir: Path,
                size: int = PRH_BATCH) -> Iterator[Batch]:
    dest = data_dir / "imports" / "prh"
    path = dest / checkpoint["file"] if checkpoint.get("file") else None
    if path is None or not path.exists():
        if path is not None:
            errors.append(f"checkpoint file {path.name} missing; restarting with the current bulk file")
        path, checkpoint = _prh_download(dest, beat, errors), {}
    digest = _sha256(path)
    if checkpoint.get("sha256") and checkpoint["sha256"] != digest:
        raise SourceError("PRH checkpoint archive changed; restore the pinned snapshot to resume")
    snapshot = re.sub(r"\D", "", path.name)
    with zipfile.ZipFile(path) as archive:
        members = [m for m in archive.infolist() if m.filename.endswith(".json")]
        if len(members) != 1:
            raise SourceError(f"PRH bulk zip has {len(members)} JSON members, expected 1")
        member = members[0]
        # ponytail: compressed JSON resumes by rereading its prefix; use a seekable snapshot if needed.
        done = checkpoint.get("records", 0)
        seen, entities, skipped = 0, [], Counter()
        with archive.open(member) as stream:
            for raw, read in iter_json_array(stream, beat):
                seen += 1
                if seen <= done:
                    continue
                entity, reason = prh_entity(raw)
                if entity:
                    entities.append(entity)
                else:
                    skipped[reason] += 1
                if seen - done >= size:
                    yield Batch(entities, {"file": path.name, "records": seen, "sha256": digest}, skipped,
                                seen=seen - done, progress=min(read / (member.file_size or 1), 1.0),
                                snapshot=f"{snapshot[:4]}-{snapshot[4:6]}-{snapshot[6:]}", source_url=PRH_BULK_URL)
                    done, entities, skipped = seen, [], Counter()
        yield Batch(entities, {"file": path.name, "records": seen, "sha256": digest}, skipped, seen=seen - done,
                    exhausted=True, total=seen, progress=1.0,
                    snapshot=f"{snapshot[:4]}-{snapshot[4:6]}-{snapshot[6:]}", source_url=PRH_BULK_URL)


# ---------------------------------------------------------------------------
# Switzerland: Zefix on LINDAS
# ---------------------------------------------------------------------------

ZEFIX_COUNT = (f"SELECT (COUNT(?s) AS ?n) WHERE {{ GRAPH <{ZEFIX_GRAPH}> "
               "{ ?s a <https://schema.ld.admin.ch/ZefixOrganisation> } }")
ZEFIX_PAGE_QUERY = """PREFIX schema: <http://schema.org/>
SELECT ?s ?name ?uid ?form ?muni ?street ?postal ?locality ?region ?purpose WHERE { GRAPH <%(graph)s> {
 { SELECT ?s WHERE { ?s a <https://schema.ld.admin.ch/ZefixOrganisation> FILTER(STR(?s) > %(after)s) }
   ORDER BY ?s LIMIT %(limit)d }
 OPTIONAL { ?s schema:legalName ?name }
 OPTIONAL { ?s schema:identifier ?idn . ?idn schema:name "CompanyUID" ; schema:value ?uid }
 OPTIONAL { ?s schema:additionalType ?form }
 OPTIONAL { ?s <https://schema.ld.admin.ch/municipality> ?muni }
 OPTIONAL { ?s schema:description ?purpose }
 OPTIONAL { ?s schema:address ?a . OPTIONAL { ?a schema:streetAddress ?street }
   OPTIONAL { ?a schema:postalCode ?postal } OPTIONAL { ?a schema:addressLocality ?locality }
   OPTIONAL { ?a schema:addressRegion ?region } }
} } ORDER BY ?s"""


def _sparql(c: httpx.Client, query: str, beat: Beat, errors: list[str]) -> list[dict]:
    response = request(c, "POST", LINDAS_URL, beat, errors, data={"query": query},
                       headers={"Accept": "application/sparql-results+json"})
    if response.status_code != 200:
        raise SourceError(f"LINDAS returned {response.status_code}: {response.text[:200]}")
    try:
        return [{k: v["value"] for k, v in row.items()} for row in response.json()["results"]["bindings"]]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError(f"LINDAS returned an invalid SPARQL result: {exc}") from exc


def zefix_entity(row: dict) -> tuple[Entity | None, str | None]:
    match = UID.match(row.get("uid") or "")
    if not match:
        return None, "invalid_identifier"
    if not row.get("name"):
        return None, "missing_name"
    uid = "CHE-{}.{}.{}".format(*match.groups())
    form = (row.get("form") or "").rsplit("/", 1)[-1] or None
    fields = {
        "uid": uid, "legal_name": row["name"], "legal_form_code": form,
        "municipality": row.get("muni"), "purpose": row.get("purpose"),
        "address": {"street": row.get("street"), "post_code": row.get("postal"), "city": row.get("locality"),
                    "canton": row.get("region")},
    }
    return Entity("CH", "business_id", uid, row["name"], row["s"], fields, description=row.get("purpose")), None


def zefix_batches(checkpoint: dict, beat: Beat, errors: list[str], data_dir: Path | None = None,
                  size: int = ZEFIX_PAGE) -> Iterator[Batch]:
    after = checkpoint.get("after", "")
    today = date.today().isoformat()
    with client() as c:
        total = checkpoint.get("total")
        if total is None:
            rows = _sparql(c, ZEFIX_COUNT, beat, errors)
            total = int(rows[0]["n"]) if rows else None
        seen_total = checkpoint.get("seen", 0)
        while True:
            beat()
            rows = _sparql(c, ZEFIX_PAGE_QUERY % {"graph": ZEFIX_GRAPH, "after": json.dumps(after), "limit": size},
                           beat, errors)
            first: dict[str, dict] = {}
            for row in rows:
                first.setdefault(row["s"], row)  # a subject with several values yields several rows
            entities, skipped = [], Counter()
            for row in first.values():
                entity, reason = zefix_entity(row)
                if entity:
                    entities.append(entity)
                else:
                    skipped[reason] += 1
            seen_total += len(first)
            exhausted = len(first) < size
            after = max(first, default=after)
            yield Batch(entities, {"after": after, "total": total, "seen": seen_total}, skipped, seen=len(first),
                        exhausted=exhausted, total=total, progress=min(seen_total / total, 1.0) if total else None,
                        snapshot=today, source_url=f"{LINDAS_URL} (graph {ZEFIX_GRAPH})")
            if exhausted:
                return
            time.sleep(1)  # polite pacing on a shared public endpoint


# ---------------------------------------------------------------------------
# Germany: GLEIF LEI subset
# ---------------------------------------------------------------------------

def gleif_entity(record: dict) -> tuple[Entity | None, str | None]:
    attrs = record.get("attributes") or {}
    lei = attrs.get("lei") or record.get("id") or ""
    entity = attrs.get("entity") or {}
    name = (entity.get("legalName") or {}).get("name")
    if not LEI.match(lei):
        return None, "invalid_identifier"
    if not name:
        return None, "missing_name"
    address = entity.get("legalAddress") or {}
    if address.get("country") != "DE" or entity.get("status") != "ACTIVE":
        return None, "outside_coverage"
    fields = {
        "lei": lei, "legal_name": name, "status": entity.get("status"), "jurisdiction": entity.get("jurisdiction"),
        "registered_as": entity.get("registeredAs"), "registration_authority": (entity.get("registeredAt") or {}).get("id"),
        "legal_form_code": (entity.get("legalForm") or {}).get("id"), "category": entity.get("category"),
        "creation_date": entity.get("creationDate"),
        "registration_status": (attrs.get("registration") or {}).get("status"),
        "address": {"street": " ".join(address.get("addressLines") or []) or None,
                    "post_code": address.get("postalCode"), "city": address.get("city"),
                    "country": address.get("country")},
    }
    return Entity("DE", "lei", lei, name, f"{GLEIF_URL}/{lei}", fields), None


def _gleif_csv_value(row: dict[str, str | None], *headers: str) -> str | None:
    for header in headers:
        value = row.get(header)
        if value and value.strip():
            return value.strip()
    return None


def _gleif_csv_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def gleif_csv_entity(row: dict[str, str | None]) -> tuple[Entity | None, str | None]:
    """Normalize one GLEIF Level 1 CSV row using the API entity field shape."""
    lei = _gleif_csv_value(row, "LEI", "lei") or ""
    name = _gleif_csv_value(row, "Entity.LegalName", "Entity.LegalName.Name")
    if not LEI.match(lei):
        return None, "invalid_identifier"
    if not name:
        return None, "missing_name"
    address_lines = [
        _gleif_csv_value(row, "Entity.LegalAddress.FirstAddressLine", "Entity.LegalAddress.AddressLine1"),
        *(_gleif_csv_value(row, f"Entity.LegalAddress.AdditionalAddressLine.{i}") for i in range(1, 4)),
    ]
    address_lines = [line for line in address_lines if line]
    authority = _gleif_csv_value(row, "Entity.RegistrationAuthority.RegistrationAuthorityID")
    fields = {
        "lei": lei,
        "legal_name": name,
        "status": _gleif_csv_value(row, "Entity.EntityStatus", "Entity.Status"),
        "jurisdiction": _gleif_csv_value(row, "Entity.LegalJurisdiction"),
        "registered_as": _gleif_csv_value(row, "Entity.RegistrationAuthority.RegistrationAuthorityEntityID"),
        "registration_authority": authority,
        "legal_form_code": _gleif_csv_value(row, "Entity.LegalForm.EntityLegalFormCode"),
        "category": _gleif_csv_value(row, "Entity.EntityCategory"),
        "creation_date": _gleif_csv_date(_gleif_csv_value(row, "Entity.EntityCreationDate", "Entity.CreationDate")),
        "registration_status": _gleif_csv_value(row, "Registration.RegistrationStatus"),
        "address": {
            "street": " ".join(address_lines) or None,
            "post_code": _gleif_csv_value(row, "Entity.LegalAddress.PostalCode"),
            "city": _gleif_csv_value(row, "Entity.LegalAddress.City"),
            "country": _gleif_csv_value(row, "Entity.LegalAddress.Country", "Entity.LegalAddress.CountryCode"),
        },
    }
    return Entity("DE", "lei", lei, name, f"{GLEIF_URL}/{lei}", fields), None


def _gleif_archive(data_dir: Path | None) -> tuple[Path, str, str, int] | None:
    """Return the already-downloaded latest official CSV archive, if present."""
    if data_dir is None:
        return None
    directory = data_dir / "imports" / "gleif"
    try:
        metadata = json.loads((directory / "latest-metadata.json").read_text())
        csv_info = metadata["data"]["full_file"]["csv"]
        url = csv_info["url"]
        filename = url.rsplit("/", 1)[-1]
        if (not url.startswith("https://goldencopy.gleif.org/storage/golden-copy-files/")
                or not GLEIF_ARCHIVE.fullmatch(filename)):
            return None
        path = directory / filename
        total = int(csv_info["record_count"])
        size = int(csv_info["size"])
        snapshot = str(metadata["data"]["publish_date"])[:10]
        if total < 1 or not snapshot or not path.is_file() or path.stat().st_size != size:
            return None
        return path, url, snapshot, total
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _gleif_csv_batches(path: Path, url: str, snapshot: str, total: int, checkpoint: dict,
                       beat: Beat, size: int) -> Iterator[Batch]:
    filename = path.name
    if not GLEIF_ARCHIVE.fullmatch(filename):
        raise SourceError("GLEIF checkpoint has an unsafe archive filename")
    row_done = int(checkpoint.get("row", 0))
    if size < 1 or total < 1 or row_done < 0 or row_done > total:
        raise SourceError("GLEIF CSV checkpoint or batch size is invalid")
    digest = _sha256(path)
    if checkpoint.get("sha256") and checkpoint["sha256"] != digest:
        raise SourceError("GLEIF checkpoint archive changed; restore the pinned snapshot to resume")
    pinned = {"source": GLEIF_FILE_SOURCE, "file": filename, "url": url, "snapshot": snapshot,
              "total": total, "sha256": digest}
    with zipfile.ZipFile(path) as archive:
        members = [member for member in archive.infolist() if member.filename.lower().endswith(".csv")]
        if len(members) != 1:
            raise SourceError(f"GLEIF archive has {len(members)} CSV members, expected 1")
        row_number, batch_seen = 0, 0
        entities, skipped = [], Counter()
        with TextIOWrapper(archive.open(members[0]), encoding="utf-8-sig", newline="") as stream:
            rows = csv.DictReader(stream)
            headers = set(rows.fieldnames or ())
            required = (("LEI", "lei"), ("Entity.LegalName", "Entity.LegalName.Name"),
                        ("Entity.EntityStatus", "Entity.Status"),
                        ("Entity.LegalAddress.Country", "Entity.LegalAddress.CountryCode"))
            if any(not headers.intersection(aliases) for aliases in required):
                raise SourceError("GLEIF CSV is missing required Level 1 columns")
            for row_number, row in enumerate(rows, 1):
                if row_number % 1000 == 0:
                    beat()
                if row_number <= row_done:
                    continue
                batch_seen += 1
                country = _gleif_csv_value(row, "Entity.LegalAddress.Country", "Entity.LegalAddress.CountryCode")
                status = _gleif_csv_value(row, "Entity.EntityStatus", "Entity.Status")
                if country != "DE":
                    skipped["other_country"] += 1
                elif status != "ACTIVE":
                    skipped["inactive"] += 1
                else:
                    entity, reason = gleif_csv_entity(row)
                    if entity:
                        entities.append(entity)
                    else:
                        skipped[reason] += 1
                if batch_seen >= size:
                    yield Batch(entities, {**pinned, "row": row_number}, skipped, seen=batch_seen,
                                total=total, progress=min(row_number / total, 1.0), snapshot=snapshot,
                                source_url=url, content_hash=digest)
                    row_done, batch_seen, entities, skipped = row_number, 0, [], Counter()
        if row_number != total:
            raise SourceError(f"GLEIF CSV row count {row_number} does not match metadata {total}")
        yield Batch(entities, {**pinned, "row": row_number}, skipped, seen=batch_seen, exhausted=True,
                    total=total, progress=1.0, snapshot=snapshot, source_url=url, content_hash=digest)


def gleif_batches(checkpoint: dict, beat: Beat, errors: list[str], data_dir: Path | None = None,
                  size: int = GLEIF_BULK_BATCH) -> Iterator[Batch]:
    if checkpoint.get("source") == GLEIF_FILE_SOURCE:
        filename = checkpoint.get("file")
        if not isinstance(filename, str) or not GLEIF_ARCHIVE.fullmatch(filename):
            raise SourceError("GLEIF checkpoint has an unsafe archive filename")
        path = (data_dir / "imports" / "gleif" / filename) if data_dir is not None else None
        if path is None or not path.is_file():
            raise SourceError("GLEIF checkpoint archive is missing; restore the pinned file to resume")
        source_url = str(checkpoint.get("url") or "")
        if not source_url.startswith("https://goldencopy.gleif.org/storage/golden-copy-files/"):
            raise SourceError("GLEIF checkpoint has an invalid archive URL")
        yield from _gleif_csv_batches(path, source_url,
                                      str(checkpoint.get("snapshot") or ""), int(checkpoint.get("total") or 0),
                                      checkpoint, beat, size)
        return
    archive = _gleif_archive(data_dir)
    if archive is not None:
        path, url, snapshot, total = archive
        yield from _gleif_csv_batches(path, url, snapshot, total, {"row": 0}, beat, size)
        return
    url = checkpoint.get("next") or GLEIF_FIRST
    seen_total, total = checkpoint.get("seen", 0), checkpoint.get("total")
    with client() as c:
        while url:
            beat()
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.netloc != "api.gleif.org"
                    or parsed.path != "/api/v1/lei-records"):
                raise SourceError("GLEIF returned a cursor outside its records endpoint")
            response = request(c, "GET", url, beat, errors, headers={"Accept": "application/vnd.api+json"})
            if response.status_code == 400 and url != GLEIF_FIRST:
                # A stale cursor: restart from the top. Identifier dedup makes the replay safe.
                errors.append("GLEIF cursor rejected (expired?); restarting from the first page")
                url, seen_total = GLEIF_FIRST, 0
                continue
            if response.status_code != 200:
                raise SourceError(f"GLEIF returned {response.status_code}: {response.text[:200]}")
            try:
                payload = response.json()
                records = payload["data"]
            except (ValueError, KeyError) as exc:
                raise SourceError(f"GLEIF returned invalid JSON:API: {exc}") from exc
            meta = payload.get("meta") or {}
            total = (meta.get("pagination") or {}).get("total", total)
            snapshot = ((meta.get("goldenCopy") or {}).get("publishDate") or "")[:10] or None
            entities, skipped = [], Counter()
            for record in records:
                entity, reason = gleif_entity(record)
                if entity:
                    entities.append(entity)
                else:
                    skipped[reason] += 1
            seen_total += len(records)
            url = (payload.get("links") or {}).get("next") if records else None
            yield Batch(entities, {"next": url, "seen": seen_total, "total": total}, skipped, seen=len(records),
                        exhausted=not url, total=total, progress=min(seen_total / total, 1.0) if total else None,
                        snapshot=snapshot, source_url=GLEIF_FIRST)
            time.sleep(1.1)  # GLEIF allows 60 requests per minute


ADAPTERS = {"prh_bulk": prh_batches, "zefix_lindas": zefix_batches, "gleif_de": gleif_batches}
