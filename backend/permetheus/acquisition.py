"""Isolated acquisition library for company discovery and enrichment.

No database access and no other backend module imports: this is the
foundation layer, built to run standalone while the rest of the backend is
designed in parallel. Every network-facing function returns plain
dataclasses that carry source provenance (the exact source URL used and the
time it was fetched) so a caller can judge coverage honestly instead of
assuming completeness.

Four callables:

- ``prh_search``: Finnish PRH v3 company registry search (name / business ID /
  registration-date window / pagination), verified live against
  https://avoindata.prh.fi/opendata-ytj-api/v3/schema?lang=en on 2026-09-27.
- ``fetch_public_url``: SSRF-safe fetch of an arbitrary public URL (company
  websites, registry pages). Validates DNS answers and connects to the
  validated address directly, so a DNS answer that changes between
  validation and connect (DNS rebinding) cannot redirect the connection.
- ``research_website``: bounded same-host crawl on top of
  ``fetch_public_url`` that extracts visible text, links and candidate
  contact points (unverified) while honouring robots.txt.
- ``search_web``: JSON client for an operator-configured local SearXNG
  instance.

``search_web`` talks to an operator-configured host (typically
``127.0.0.1``), not attacker-influenced input, so it does not go through the
public-address SSRF policy that ``fetch_public_url`` enforces -- that policy
would make a local SearXNG deployment unreachable by design.
"""

from __future__ import annotations

import ipaddress
from io import BytesIO
import json
import math
import re
import socket
import ssl
import threading
import time
import zlib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable
from http.client import HTTPConnection, HTTPException
from urllib.robotparser import RobotFileParser

import certifi

USER_AGENT = "Permetheus-Acquisition/0.1"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# PRH v3 registry search
# ---------------------------------------------------------------------------

PRH_BASE_URL = "https://avoindata.prh.fi/opendata-ytj-api/v3"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PRHError(Exception):
    """Raised for any non-2xx PRH response or transport failure."""

    def __init__(self, message: str, *, status: int | None = None, errorcode: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.errorcode = errorcode


@dataclass
class CompanyRecord:
    business_id: str | None
    name: str | None
    company_form: str | None
    registration_date: str | None
    end_date: str | None
    trade_register_status: str | None
    status: str | None
    website: str | None
    addresses: list[dict]
    main_business_line: str | None
    last_modified: str | None
    raw: dict
    source_url: str
    fetched_at: str


@dataclass
class PRHSearchResult:
    companies: list[CompanyRecord]
    total_results: int
    page: int
    next_page_hint: int | None
    source_url: str
    fetched_at: str


def _current_name(names: list[dict] | None) -> str | None:
    """The primary *current* legal name: the entry still in force (no
    ``endDate``). ``version`` is a per-name sequence number, not a currency
    flag -- a name with ``version == 1`` can still have ended, so picking on
    version alone (as an earlier revision of this function did) can return a
    name the registry no longer considers current."""
    for entry in names or []:
        if not entry.get("endDate"):
            return entry.get("name")
    return names[0]["name"] if names else None


def _normalize_company(raw: dict, *, source_url: str, fetched_at: str) -> CompanyRecord:
    website = (raw.get("website") or {}).get("url") or None
    forms = raw.get("companyForms") or []
    addresses = []
    for addr in raw.get("addresses") or []:
        offices = addr.get("postOffices") or []
        addresses.append(
            {
                "street": addr.get("street"),
                "post_code": addr.get("postCode"),
                "city": offices[0].get("city") if offices else None,
                "country": addr.get("country"),
            }
        )
    return CompanyRecord(
        business_id=(raw.get("businessId") or {}).get("value"),
        name=_current_name(raw.get("names")),
        company_form=forms[0].get("type") if forms else None,
        registration_date=raw.get("registrationDate"),
        end_date=raw.get("endDate"),
        trade_register_status=raw.get("tradeRegisterStatus"),
        status=raw.get("status"),
        website=website,
        addresses=addresses,
        main_business_line=(raw.get("mainBusinessLine") or {}).get("type"),
        last_modified=raw.get("lastModified"),
        raw=raw,
        source_url=source_url,
        fetched_at=fetched_at,
    )


def _read_bounded(fp, max_bytes: int) -> bytes:
    """Read a urllib response/error body up to ``max_bytes``, raising
    ``PRHError`` instead of buffering an unbounded upstream reply."""
    data = fp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise PRHError("response_too_large")
    return data


def prh_search(
    name: str | None = None,
    business_id: str | None = None,
    registration_start: str | None = None,
    registration_end: str | None = None,
    page: int = 1,
    *,
    base_url: str = PRH_BASE_URL,
    timeout: float = 15.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> PRHSearchResult:
    """Search PRH v3 ``/companies``.

    ``page`` follows the documented contract: PRH shows more than roughly
    100 results across multiple pages, but the API does not document an
    exact fixed page size or an explicit "last page" flag (observed live:
    a registration-date window paged 100 then 64 of 164 total, while a
    broad name search returned 73 on page 1 of ~686k total). ``next_page_hint``
    is therefore a hint derived from ``total_results``, not a guarantee --
    stop paging once a page returns zero companies.

    Raises ``PRHError`` on a non-2xx response or if PRH is unreachable.
    """
    if not isinstance(page, int) or page < 1:
        raise ValueError("page must be an int >= 1")
    for label, value in (("registration_start", registration_start), ("registration_end", registration_end)):
        if value is not None and not _DATE_RE.match(value):
            raise ValueError(f"{label} must be in YYYY-MM-DD format")

    params: dict[str, str] = {}
    if name:
        params["name"] = name
    if business_id:
        params["businessId"] = business_id
    if registration_start:
        params["registrationDateStart"] = registration_start
    if registration_end:
        params["registrationDateEnd"] = registration_end
    params["page"] = str(page)

    url = f"{base_url}/companies?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    fetched_at = _now()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = _read_bounded(response, max_bytes)
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = _read_bounded(exc, max_bytes)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise PRHError(f"prh_unreachable: {exc}") from exc

    if status != 200:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        message = payload.get("message") if isinstance(payload, dict) else None
        raise PRHError(
            message or f"PRH request failed with status {status}",
            status=status,
            errorcode=payload.get("errorcode") if isinstance(payload, dict) else None,
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PRHError("invalid_json_response", status=status) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("companies", []), list):
        raise PRHError("invalid_response_shape", status=status)

    companies_raw = payload.get("companies") or []
    total_results = payload.get("totalResults", len(companies_raw))
    companies = [_normalize_company(c, source_url=url, fetched_at=fetched_at) for c in companies_raw]
    next_page_hint = page + 1 if companies and page * 100 < total_results else None
    return PRHSearchResult(
        companies=companies,
        total_results=total_results,
        page=page,
        next_page_hint=next_page_hint,
        source_url=url,
        fetched_at=fetched_at,
    )


# ---------------------------------------------------------------------------
# SSRF-safe public URL fetch
# ---------------------------------------------------------------------------


class FetchError(Exception):
    """A bounded public error code; never carries upstream response bodies."""


DEFAULT_ALLOWED_CONTENT_TYPES = frozenset(
    {"text/html", "text/plain", "application/xhtml+xml", "application/json"}
)
_ALLOWED_SCHEME_PORTS = {"http": 80, "https": 443}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    content_type: str | None
    headers: dict[str, str]
    body: bytes
    text: str | None
    redirect_chain: list[str]
    truncated: bool
    fetched_at: str


def _canonical_hostname(host: str) -> str:
    if not isinstance(host, str) or not host or len(host) > 253:
        raise FetchError("invalid_hostname")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        normalized = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise FetchError("invalid_hostname") from exc
    labels = normalized.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in label)
        for label in labels
    ):
        raise FetchError("invalid_hostname")
    return normalized


def _assert_public_address(value: str) -> str:
    """Reject loopback/private/link-local/reserved/multicast v4/v6, including
    IPv4-mapped, 6to4, Teredo and NAT64 v6 forms that unwrap to a non-public v4
    address."""
    if "%" in value:
        raise FetchError("non_public_address")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise FetchError("invalid_dns_answer") from exc
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif (
            address.sixtofour is not None
            or address.teredo is not None
            or address in ipaddress.IPv6Network("64:ff9b::/96")
            or address in ipaddress.IPv6Network("64:ff9b:1::/48")
        ):
            raise FetchError("non_public_address")
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise FetchError("non_public_address")
    return str(address)


def _resolve_host(host: str, port: int) -> list[str]:
    """DNS resolution seam (patched in tests). Called exactly once per hop; the
    returned, validated address is what we connect to, so a second DNS answer
    handed out later (DNS rebinding) never affects this request."""
    return [answer[4][0] for answer in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


class _PinnedHTTPConnection(HTTPConnection):
    """Connects to a pre-validated IP address while still sending/verifying
    TLS against the original hostname (SNI + certificate hostname check)."""

    def __init__(self, host: str, port: int, address: str, tls: bool, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address
        self._tls = tls

    def connect(self) -> None:
        family = socket.AF_INET6 if ":" in self._address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect((self._address, self.port))
            if self._tls:
                context = ssl.create_default_context()
                # The OS bundle can lag behind current public CA roots. Retain verification.
                context.load_verify_locations(cafile=certifi.where())
                self.sock = context.wrap_socket(sock, server_hostname=self.host)
            else:
                self.sock = sock
        except BaseException:
            sock.close()
            raise


def _open_connection(host: str, port: int, address: str, tls: bool, timeout: float) -> _PinnedHTTPConnection:
    """Connection seam (patched in tests)."""
    return _PinnedHTTPConnection(host, port, address, tls, timeout)


def _validate_url(url: str) -> tuple[urllib.parse.SplitResult, str, int]:
    if not isinstance(url, str) or not url or len(url) > 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise FetchError("invalid_url")
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in _ALLOWED_SCHEME_PORTS
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise FetchError("unsupported_url")
    host = _canonical_hostname(parsed.hostname)
    default_port = _ALLOWED_SCHEME_PORTS[parsed.scheme]
    port = parsed.port if parsed.port is not None else default_port
    if port != default_port:
        raise FetchError("unsupported_port")
    return parsed, host, port


def _read_body_bounded(response, max_bytes: int, start: float, overall_timeout: float) -> tuple[bytes, bool]:
    """Read a response body in chunks, re-checking ``overall_timeout`` between
    chunks. A single ``response.read(max_bytes + 1)`` call bounds total bytes
    but not total time: a server that trickles one byte at a time can hold the
    connection open indefinitely without ever exceeding the per-recv socket
    timeout (slowloris). Chunking makes the *whole body read* subject to the
    same wall-clock budget as DNS/connect/redirect hops."""
    chunk_size = 65536
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        if time.monotonic() - start > overall_timeout:
            raise FetchError("overall_timeout_exceeded")
        chunk = response.read(min(chunk_size, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    body = b"".join(chunks)
    truncated = len(body) > max_bytes
    if truncated:
        body = body[:max_bytes]
    return body, truncated


def fetch_public_url(
    url: str,
    *,
    method: str = "GET",
    timeout: float = 10.0,
    max_redirects: int = 5,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allowed_content_types: frozenset[str] = DEFAULT_ALLOWED_CONTENT_TYPES,
    overall_timeout: float = 30.0,
    allow_cross_host_redirects: bool = True,
    before_redirect: Callable[[str], None] | None = None,
) -> FetchResult:
    """Fetch a public http(s) URL with SSRF, redirect, size, time and
    content-type bounds. Raises ``FetchError`` for any policy violation or
    transport failure.

    ``allow_cross_host_redirects=False`` raises
    ``FetchError("cross_host_redirect_blocked")`` on a redirect to a different
    hostname. The crawler needs this: it checked robots.txt only for the
    original host, so following a hop to another host would skip that host's
    robots policy."""
    if method not in ("GET", "HEAD"):
        raise FetchError("unsupported_method")

    start = time.monotonic()
    current_url = url
    redirect_chain: list[str] = []
    origin_host = _validate_url(url)[1]

    for _ in range(max_redirects + 1):
        if time.monotonic() - start > overall_timeout:
            raise FetchError("overall_timeout_exceeded")

        parsed, host, port = _validate_url(current_url)
        try:
            literal = ipaddress.ip_address(host)
            addresses = [str(literal)]
        except ValueError:
            try:
                addresses = list(_resolve_host(host, port))
            except socket.gaierror as exc:
                raise FetchError("dns_resolution_failed") from exc
        if not addresses or len(addresses) > 32:
            raise FetchError("invalid_dns_answer")
        validated = [_assert_public_address(address) for address in addresses]

        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query

        connection = _open_connection(host, port, validated[0], parsed.scheme == "https", timeout)
        try:
            try:
                connection.request(
                    method,
                    target,
                    headers={
                        # The pinned transport subclasses HTTPConnection even for TLS;
                        # its automatic Host would append :443 and cause canonical redirect loops.
                        "Host": f'[{host}]' if ':' in host else host,
                        "Accept-Encoding": "identity",
                        "User-Agent": USER_AGENT,
                        "Connection": "close",
                    },
                )
                with connection.getresponse() as response:
                    status = response.status
                    content_type_header = response.getheader("Content-Type")
                    location = response.getheader("Location")
                    encodings = response.headers.get_all("Content-Encoding", [])
                    encoding = ','.join(encodings).strip().lower()
                    if encoding not in ('', 'identity', 'gzip'):
                        raise FetchError("compressed_response_rejected")
                    declared = response.headers.get_all("Content-Length", [])
                    if len(declared) > 1 or (declared and not declared[0].isdigit()):
                        raise FetchError("invalid_response_length")
                    if declared and int(declared[0]) > max_bytes:
                        raise FetchError("response_too_large")
                    body, truncated = (
                        _read_body_bounded(response, max_bytes, start, overall_timeout)
                        if method != "HEAD"
                        else (b"", False)
                    )
                    if encoding == 'gzip' and method != 'HEAD':
                        try:
                            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                            body = decoder.decompress(body, max_bytes + 1)
                        except zlib.error as exc:
                            raise FetchError('invalid_compressed_response') from exc
                        if truncated or len(body) > max_bytes or decoder.unconsumed_tail:
                            raise FetchError('response_too_large')
                        if not decoder.eof or decoder.unused_data:
                            raise FetchError('invalid_compressed_response')
                    headers = {}
                    for name in ("Content-Type", "Content-Length", "Last-Modified", "ETag"):
                        value = response.getheader(name)
                        if value is not None:
                            headers[name.lower()] = value
            except ssl.SSLCertVerificationError as exc:
                raise FetchError("tls_certificate_invalid") from exc
            except TimeoutError as exc:
                raise FetchError("request_timed_out") from exc
            except (HTTPException, ssl.SSLError, OSError) as exc:
                raise FetchError("upstream_failed") from exc
        finally:
            connection.close()

        if status in _REDIRECT_STATUSES and location:
            if len(redirect_chain) >= max_redirects:
                raise FetchError("too_many_redirects")
            current_url = urllib.parse.urljoin(current_url, location)
            if not allow_cross_host_redirects and _validate_url(current_url)[1] != origin_host:
                raise FetchError("cross_host_redirect_blocked")
            if before_redirect is not None:
                before_redirect(current_url)
            redirect_chain.append(current_url)
            continue

        mime = (content_type_header or "").split(";")[0].strip().lower()
        if mime and mime not in allowed_content_types:
            raise FetchError("unsupported_content_type")
        text = None
        if mime in ("text/html", "text/plain", "application/xhtml+xml", "application/json"):
            charset = "utf-8"
            if content_type_header and "charset=" in content_type_header.lower():
                charset = content_type_header.lower().split("charset=")[-1].split(";")[0].strip() or "utf-8"
            try:
                text = body.decode(charset, errors="replace")
            except LookupError:
                text = body.decode("utf-8", errors="replace")
        return FetchResult(
            requested_url=url,
            final_url=current_url,
            status=status,
            content_type=mime or None,
            headers=headers,
            body=body,
            text=text,
            redirect_chain=redirect_chain,
            truncated=truncated,
            fetched_at=_now(),
        )

    raise FetchError("too_many_redirects")


# ---------------------------------------------------------------------------
# Website research: text / contact / link extraction
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9.+_-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w.-])")
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,18}\d(?!\w)")
# Plain digit runs matched by _PHONE_RE also match calendar dates
# ("2024-01-01", "31.12.2023", a bare "2024"); reject those shapes explicitly
# rather than reporting a date as a discovered phone number.
_DATE_LIKE_RES = (
    re.compile(r"^\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}$"),
    re.compile(r"^\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}$"),
    re.compile(r"^\d{4}$"),
)


def _looks_like_date(value: str) -> bool:
    return any(pattern.match(value) for pattern in _DATE_LIKE_RES)


def _valid_email(value: str) -> str | None:
    value = urllib.parse.unquote(value).strip(" \t\r\n<>\"'.,;:!?()[]{}")
    if not _EMAIL_RE.fullmatch(value) or value.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf")):
        return None
    domain = value.lower().rsplit("@", 1)[1]
    if value.lower().split("@", 1)[0] in {"firstname.lastname", "first.last", "etunimi.sukunimi", "vorname.nachname", "name.surname"}:
        return None
    if domain in {"example.com", "example.org", "example.net", "example.invalid", "example.test"} or domain.endswith((".invalid", ".example", ".test")):
        return None
    return value.lower()


def _emails_in_text(text: str) -> list[str]:
    normalized = re.sub(r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\})\s*", "@", text, flags=re.I)
    normalized = re.sub(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\})\s*", ".", normalized, flags=re.I)
    normalized = re.sub(r"(?<=[\w.+-])@\s+(?=[A-Za-z0-9-]+\.)", "@", normalized)
    return list(dict.fromkeys(email for match in _EMAIL_RE.findall(normalized)
                              if (email := _valid_email(match))))


def _phone_value(value: str) -> str | None:
    value = urllib.parse.unquote(value).split(";", 1)[0].strip()
    if not re.fullmatch(r"\+?[\d\s().-]+", value):
        return None
    value = re.sub(r"^(\+\d{1,3})\s*\(0\)", r"\1", value)
    digits = re.sub(r"\D", "", value)
    if not 7 <= len(digits) <= 15:
        return None
    return ("+" if value.lstrip().startswith("+") else "") + digits


def _contacts_in_text(text: str, source_url: str) -> list[ContactCandidate]:
    contacts = [ContactCandidate("email", email, source_url, "text") for email in _emails_in_text(text)]
    lowered = text.lower()
    phone_context = re.compile(r"(?:tel(?:ephone)?|phone|call|fax|puh(?:elin)?|puhelin|telefon|téléphone|tlf|telefonnummer)", re.I)
    billing_context = re.compile(r"(?:ovt|iban|bic|invoice|invoicing|billing|lasku|laskutus|rekening|rechnung)", re.I)
    for match in _PHONE_RE.finditer(text):
        raw = match.group().strip()
        digits = re.sub(r"\D", "", raw)
        context = lowered[max(0, match.start() - 48):match.end() + 24]
        preceding = lowered[max(0, match.start() - 45):match.start()]
        # A Finnish business ID is seven digits, a hyphen and a check digit.
        if re.fullmatch(r"\d{7}-\d", raw) or digits.startswith("0037") or _looks_like_date(raw):
            continue
        if billing_context.search(context):
            continue
        if re.search(r"(?:che|uid|vat|ust.?id(?:nr)?|hrb|tax id|register(?:nummer| number)?)\s*[:.\-]?\s*$", preceding):
            continue
        if re.search(r"\b(example|e\.g\.|esim\.?|muodossa|beispiel)\s*[:(]?\s*$", lowered[max(0, match.start()-35):match.start()]):
            continue
        # Plain numbers are too often business IDs, dates, or invoice data.
        if len(digits) < 9 or (not raw.startswith("+") and not phone_context.search(context)):
            continue
        value = _phone_value(raw)
        if value:
            contacts.append(ContactCandidate("phone", value, source_url, "text"))
    return contacts


def _cloudflare_email(encoded: str) -> str | None:
    try:
        raw = bytes.fromhex(encoded)
        key = raw[0]
        return _valid_email(bytes(byte ^ key for byte in raw[1:]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, IndexError):
        return None


def _organization_contacts(payload: object) -> list[tuple[str, str]]:
    """Read contact properties only from schema.org organization JSON-LD."""
    found: list[tuple[str, str]] = []
    def visit(value: object, inherited_schema: bool = False) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item, inherited_schema)
        elif isinstance(value, dict):
            context = value.get("@context")
            schema_context = inherited_schema or context in {"https://schema.org", "http://schema.org"}
            types = value.get("@type", [])
            if isinstance(types, str):
                types = [types]
            contact_entity = schema_context and any(t in {"Organization", "Corporation", "LocalBusiness",
                                                              "ProfessionalService", "NGO", "GovernmentOrganization",
                                                              "ContactPoint"} for t in types)
            if contact_entity:
                for key, kind in (("email", "email"), ("telephone", "phone")):
                    values = value.get(key, [])
                    if isinstance(values, str):
                        values = [values]
                    for item in values if isinstance(values, list) else []:
                        if isinstance(item, str):
                            if kind == "email":
                                found.extend((kind, email) for email in _emails_in_text(item))
                            elif (phone := _phone_value(item)):
                                found.append((kind, phone))
            for child in value.values():
                visit(child, schema_context)

    visit(payload)
    return list(dict.fromkeys(found))


def _page_contacts(text: str, source_url: str, extractor: _PageExtractor | None = None) -> list[ContactCandidate]:
    contacts = _contacts_in_text(text, source_url)
    if extractor is not None:
        for mail in extractor.mailto:
            for email in _emails_in_text(mail):
                contacts.append(ContactCandidate("email", email, source_url, "mailto_link"))
        for tel in extractor.tel:
            if phone := _phone_value(tel):
                contacts.append(ContactCandidate("phone", phone, source_url, "tel_link"))
        for raw in extractor.json_ld:
            try:
                values = _organization_contacts(json.loads(raw))
            except (json.JSONDecodeError, TypeError, RecursionError):
                continue
            contacts.extend(ContactCandidate(kind, value, source_url, "json_ld") for kind, value in values)
    return _unique_candidates(contacts)


def _unique_candidates(contacts: list[ContactCandidate]) -> list[ContactCandidate]:
    return list({(contact.kind, contact.value, contact.source_url, contact.source): contact
                 for contact in contacts}.values())


class _PageExtractor(HTMLParser):
    """Pulls visible text, the title, absolute links and mailto:/tel: contacts
    out of one HTML page. Script/style/noscript content is not text."""

    _SKIPPED_TAGS = frozenset({"script", "style", "noscript"})

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title: str | None = None
        self.links: list[str] = []
        self.mailto: list[str] = []
        self.tel: list[str] = []
        self._text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._base_seen = False
        self.json_ld: list[str] = []
        self._json_ld_depth = 0
        self._hidden_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        hidden = ("hidden" in attributes or "displaynone" in (attributes.get("class") or "").split()
                  or re.search(r"display\s*:\s*none", attributes.get("style") or "", re.I))
        if self._hidden_tags or hidden:
            if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
                self._hidden_tags.append(tag)
            return
        if tag == "script" and (attributes.get("type") or "").lower() == "application/ld+json":
            self._json_ld_depth += 1
            return
        if tag in self._SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "base" and not self._base_seen:
            href = attributes.get("href")
            if href:
                base = urllib.parse.urljoin(self.base_url, href)
                parsed = urllib.parse.urlsplit(base)
                if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password:
                    self.base_url, self._base_seen = base, True
            return
        if (encoded := attributes.get("data-cfemail")):
            if (email := _cloudflare_email(encoded)):
                self.mailto.append(email)
        if tag == "a":
            href = attributes.get("href")
            if not href:
                return
            href = href.strip()
            decoded = urllib.parse.unquote(href)
            if decoded.partition(":")[0].lower() in {"mailto", "tel"}:
                href = decoded
            scheme, _, address = href.partition(":")
            if scheme.lower() == "mailto":
                recipients = urllib.parse.unquote(address.split("?", 1)[0]).split(",")
                self.mailto.extend(item.strip() for item in recipients if item.strip())
            elif scheme.lower() == "tel":
                self.tel.append(urllib.parse.unquote(address))
            elif not href.startswith(("javascript:", "#")):
                absolute = urllib.parse.urljoin(self.base_url, href)
                if absolute.startswith(("http://", "https://")):
                    self.links.append(absolute)

    def handle_endtag(self, tag: str) -> None:
        if self._hidden_tags:
            if tag in self._hidden_tags:
                del self._hidden_tags[len(self._hidden_tags) - 1 - self._hidden_tags[::-1].index(tag):]
            return
        if tag == "script" and self._json_ld_depth:
            self._json_ld_depth -= 1
        elif tag in self._SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._hidden_tags:
            return
        if self._json_ld_depth:
            self.json_ld.append(data)
            return
        if self._skip_depth:
            return
        if self._in_title:
            self.title = (self.title or "") + data
            return
        stripped = data.strip()
        if stripped:
            self._text_parts.append(stripped)

    @property
    def text(self) -> str:
        return " ".join(" ".join(self._text_parts).split())


@dataclass
class ContactCandidate:
    kind: str  # "email" | "phone" -- discovered, not verified
    value: str
    source_url: str
    source: str  # "mailto_link" | "tel_link" | "text"


@dataclass
class PageResult:
    url: str
    status: int | None
    title: str | None
    text_excerpt: str
    links: list[str]
    error: str | None
    fetched_at: str


@dataclass
class WebsiteResearch:
    root_url: str
    pages: list[PageResult]
    contacts: list[ContactCandidate]
    internal_links: list[str]
    external_links: list[str]
    robots_disallowed: list[str]
    errors: list[str]
    pages_fetched: int
    fetched_at: str


def _load_robots(scheme_host: str, timeout: float) -> tuple[RobotFileParser | None, str | None]:
    """Fetch and parse robots.txt. Returns ``(parser, None)`` when the policy
    is known -- either a parsed robots.txt, or an unavailable 4xx response
    (RFC 9309 section 2.3.1.3). Rate limits remain a reason to stop. Returns ``(None, reason)`` when the
    policy could *not* be determined (DNS/connect/timeout failure, or any
    rate limit or server error): crawling a host without knowing its
    robots policy is not permitted, so the caller must skip that host with an
    explicit reason instead of silently defaulting to permissive."""
    try:
        result = fetch_public_url(
            scheme_host + "/robots.txt",
            timeout=timeout,
            allowed_content_types=DEFAULT_ALLOWED_CONTENT_TYPES,
        )
    except FetchError as exc:
        return None, f"{scheme_host}/robots.txt: robots_fetch_failed: {exc}"
    if 400 <= result.status < 500 and result.status != 429:
        parser = RobotFileParser()
        parser.set_url(scheme_host + "/robots.txt")
        parser.parse([])  # unavailable policy; actual page access may still be denied
        return parser, None
    if result.status == 200:
        parser = RobotFileParser()
        parser.set_url(scheme_host + "/robots.txt")
        parser.parse((result.text or "").splitlines())
        return parser, None
    return None, f"{scheme_host}/robots.txt: robots_fetch_failed: status_{result.status}"


def _research_priority(url: str) -> tuple[int, int]:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lower()
    if re.search(r"/(about|contact|team|company|investor|annual|financial|report|tilinpaatos|talous|yhteys|yhteystiedot|yritys|impressum|unternehmen|kontakt|kontaktuppgifter|ueber|über|legal|invoicing|contactez|coordonnees|coordonnées)", path):
        return 0, path.count("/")
    if re.search(r"blog|news|article|event|privacy|cookie|terms|tag|category", path):
        return 2, path.count("/")
    return 1, path.count("/")


def _web_pdf_text(body: bytes, limit: int) -> str:
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(body), strict=False)
    if len(reader.pages) > 60:
        raise ValueError("pdf_page_limit_exceeded")
    pieces, count = [], 0
    for number, page in enumerate(reader.pages, 1):
        text = " ".join((page.extract_text() or "").split())
        if not text.strip():
            continue
        pieces.append(f"[PDF page {number}] {text}"[:max(0, limit - count)])
        count += len(pieces[-1])
        if count >= limit:
            break
    if not pieces:
        raise ValueError("pdf_has_no_text")
    return "\n".join(pieces)


def research_website(
    url: str,
    max_pages: int = 5,
    *,
    timeout: float = 10.0,
    text_excerpt_limit: int = 4000,
    before_fetch: Callable[[], None] | None = None,
    include_documents: bool = False,
) -> WebsiteResearch:
    """Bounded same-site crawl starting at ``url``. Obeys robots.txt, follows
    same-host and www-alias redirects, stops at ``max_pages``
    fetched pages. Every email and phone number returned is *discovered*, not
    verified.

    ``before_fetch`` runs before every page fetch (not before robots.txt).
    Any exception it raises propagates out of the crawl, so a caller can use
    it to cancel between pages or renew a job lease."""
    if not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError("max_pages must be an int >= 1")
    parsed_root = urllib.parse.urlsplit(url)
    if parsed_root.scheme not in ("http", "https") or not parsed_root.hostname:
        raise ValueError("url must be an absolute http(s) URL")

    root_host = parsed_root.hostname.lower()
    scheme_host = f"{parsed_root.scheme}://{parsed_root.netloc}"
    robots, skip_reason = _load_robots(scheme_host, timeout)
    if robots is None:
        return WebsiteResearch(
            root_url=url,
            pages=[],
            contacts=[],
            internal_links=[],
            external_links=[],
            robots_disallowed=[],
            errors=[skip_reason],
            pages_fetched=0,
            fetched_at=_now(),
        )

    queue: list[str] = [urllib.parse.urldefrag(url)[0]]
    seen: set[str] = set()
    pages: list[PageResult] = []
    contacts: list[ContactCandidate] = []
    internal_links: set[str] = set()
    external_links: set[str] = set()
    robots_disallowed: list[str] = []
    errors: list[str] = []
    robots_by_origin = {scheme_host: (robots, None)}

    def check_redirect(target: str) -> None:
        parsed = urllib.parse.urlsplit(target)
        if (parsed.hostname or '').lower().removeprefix('www.') != root_host.removeprefix('www.'):
            raise FetchError('cross_host_redirect_blocked')
        origin = f'{parsed.scheme}://{parsed.netloc}'
        if origin not in robots_by_origin:
            robots_by_origin[origin] = _load_robots(origin, timeout)
        policy, problem = robots_by_origin[origin]
        if policy is None:
            raise FetchError(problem or 'robots_fetch_failed')
        if not policy.can_fetch(USER_AGENT, target):
            robots_disallowed.append(target)
            raise FetchError('robots_disallowed_redirect')

    while queue and len(pages) < max_pages:
        page_url = queue.pop(0)
        if page_url in seen:
            continue
        seen.add(page_url)

        if before_fetch is not None:
            before_fetch()
        try:
            check_redirect(page_url)
            result = fetch_public_url(page_url, timeout=timeout, before_redirect=check_redirect,
                                      allowed_content_types=DEFAULT_ALLOWED_CONTENT_TYPES | ({"application/pdf"} if include_documents else set()))
        except FetchError as exc:
            if str(exc) == 'robots_disallowed_redirect':
                continue
            errors.append(f"{page_url}: {exc}")
            pages.append(
                PageResult(
                    url=page_url,
                    status=None,
                    title=None,
                    text_excerpt="",
                    links=[],
                    error=str(exc),
                    fetched_at=_now(),
                )
            )
            continue

        if result.final_url != page_url:
            try:
                check_redirect(result.final_url)
            except FetchError:
                continue
        seen.add(result.final_url)

        if result.status != 200:
            error = f"page_http_{result.status}"
            errors.append(f"{result.final_url}: {error}")
            pages.append(PageResult(result.final_url, result.status, None, "", [], error, result.fetched_at))
            continue

        if include_documents and result.content_type == "application/pdf":
            try:
                text = _web_pdf_text(result.body, text_excerpt_limit)
                contacts.extend(_page_contacts(text, result.final_url))
                pages.append(PageResult(result.final_url, 200, urllib.parse.urlsplit(result.final_url).path.rsplit('/', 1)[-1], text, [], None, result.fetched_at))
            except Exception as exc:
                error = str(exc) if isinstance(exc, ValueError) else 'pdf_parse_failed'
                errors.append(f"{result.final_url}: {error[:160]}")
                pages.append(PageResult(result.final_url, 200, None, "", [], error[:160], result.fetched_at))
            continue

        if result.content_type not in ("text/html", "application/xhtml+xml") or result.text is None:
            pages.append(
                PageResult(
                    url=result.final_url,
                    status=result.status,
                    title=None,
                    text_excerpt="",
                    links=[],
                    error="non_html_content",
                    fetched_at=result.fetched_at,
                )
            )
            continue

        extractor = _PageExtractor(result.final_url)
        try:
            extractor.feed(result.text)
        except Exception as exc:  # malformed HTML must not abort the crawl
            errors.append(f"{page_url}: parse_error {exc}")
            continue

        text = extractor.text
        contacts.extend(_page_contacts(text, result.final_url, extractor))

        for link in extractor.links:
            link = urllib.parse.urldefrag(link)[0]
            link_host = urllib.parse.urlsplit(link).hostname
            if not link_host:
                continue
            if link_host.lower().removeprefix('www.') == root_host.removeprefix('www.'):
                internal_links.add(link)
                if len(pages) + 1 < max_pages and link not in seen and link not in queue:
                    if len(queue) < max_pages * 4 or _research_priority(link) < _research_priority(queue[-1]):
                        queue.append(link)
                        queue.sort(key=_research_priority)
                        del queue[max_pages * 4:]
            else:
                external_links.add(link)

        pages.append(
            PageResult(
                url=result.final_url,
                status=result.status,
                title=(extractor.title or "").strip() or None,
                text_excerpt=text[:text_excerpt_limit],
                links=extractor.links,
                error=None,
                fetched_at=result.fetched_at,
            )
        )

    deduped_contacts: list[ContactCandidate] = []
    seen_pairs: set[tuple[str, str]] = set()
    for contact in contacts:
        key = (contact.kind, contact.value)
        if key not in seen_pairs:
            seen_pairs.add(key)
            deduped_contacts.append(contact)

    return WebsiteResearch(
        root_url=url,
        pages=pages,
        contacts=deduped_contacts,
        internal_links=sorted(internal_links),
        external_links=sorted(external_links),
        robots_disallowed=robots_disallowed,
        errors=errors,
        pages_fetched=len(pages),
        fetched_at=_now(),
    )


# ---------------------------------------------------------------------------
# SearXNG local JSON client
# ---------------------------------------------------------------------------

DEFAULT_SEARXNG_BASE_URL = "http://127.0.0.1:8888"
_SEARCH_LOCK = threading.Lock()
_SEARCH_MIN_INTERVAL = 5.0
_SEARCH_FAILURE_COOLDOWN = 120.0
_SEARCH_LAST_REQUEST_AT: float | None = None
_SEARCH_COOLDOWN_UNTIL = 0.0


def _search_quota_failure(error: str | None) -> bool:
    lowered = (error or "").lower()
    return any(marker in lowered for marker in ("too many requests", "captcha", "429", "rate limit", "ratelimit"))


@dataclass
class SearchResultItem:
    title: str
    url: str
    content: str | None
    engine: str | None


@dataclass
class SearchResult:
    query: str
    base_url: str
    results: list[SearchResultItem]
    degraded: bool
    error: str | None
    fetched_at: str


def search_web(
    query: str,
    base_url: str = DEFAULT_SEARXNG_BASE_URL,
    *,
    timeout: float = 10.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> SearchResult:
    """Query an operator-configured local SearXNG instance's JSON API
    (``?format=json`` must be enabled on that instance). Never raises for a
    reachability problem: returns ``degraded=True`` with ``error`` set so
    search health stays distinct from "no results"."""
    fetched_at = _now()
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return SearchResult(query, base_url, [], True, "invalid_base_url", fetched_at)

    url = base_url.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    global _SEARCH_LAST_REQUEST_AT, _SEARCH_COOLDOWN_UNTIL
    # ponytail: one process-wide lock serializes SearXNG requests; use per-engine pacing if concurrency warrants it.
    with _SEARCH_LOCK:
        now = time.monotonic()
        if _SEARCH_COOLDOWN_UNTIL > now:
            remaining = math.ceil(_SEARCH_COOLDOWN_UNTIL - now)
            return SearchResult(
                query, base_url, [], True,
                f"searxng_cooldown: upstream quota or CAPTCHA failure; retry in {remaining} seconds",
                fetched_at,
            )
        if _SEARCH_LAST_REQUEST_AT is not None:
            wait = _SEARCH_MIN_INTERVAL - (now - _SEARCH_LAST_REQUEST_AT)
            if wait > 0:
                time.sleep(wait)
        _SEARCH_LAST_REQUEST_AT = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            error = f"searxng_unreachable: {exc}"
            if exc.code == 429:
                _SEARCH_COOLDOWN_UNTIL = time.monotonic() + _SEARCH_FAILURE_COOLDOWN
                error += "; search cooldown 120 seconds after HTTP 429"
            exc.close()
            return SearchResult(query, base_url, [], True, error, fetched_at)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return SearchResult(query, base_url, [], True, f"searxng_unreachable: {exc}", fetched_at)

        if len(body) > max_bytes:
            return SearchResult(query, base_url, [], True, "response_too_large", fetched_at)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return SearchResult(query, base_url, [], True, "invalid_json_response", fetched_at)
        if not isinstance(payload, dict):
            return SearchResult(query, base_url, [], True, "invalid_json_response", fetched_at)

        items = []
        for entry in payload.get("results", []) or []:
            if not isinstance(entry, dict) or not entry.get("url"):
                continue
            items.append(
                SearchResultItem(
                    title=entry.get("title") or entry["url"],
                    url=entry["url"],
                    content=entry.get("content"),
                    engine=entry.get("engine"),
                )
            )
        unresponsive = payload.get("unresponsive_engines")
        degraded = bool(unresponsive)
        error = f"unresponsive_engines: {unresponsive}" if degraded else None
        if not items and degraded and _search_quota_failure(error):
            _SEARCH_COOLDOWN_UNTIL = time.monotonic() + _SEARCH_FAILURE_COOLDOWN
            error += "; search cooldown 120 seconds after upstream quota or CAPTCHA failure"
        return SearchResult(query, base_url, items, degraded, error, fetched_at)
