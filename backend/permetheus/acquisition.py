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
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable
from http.client import HTTPConnection, HTTPException
from urllib.robotparser import RobotFileParser

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
                    if any(value.strip().lower() != "identity" for value in encodings):
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
                    headers = {}
                    for name in ("Content-Type", "Content-Length", "Last-Modified", "ETag"):
                        value = response.getheader(name)
                        if value is not None:
                            headers[name.lower()] = value
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

_EMAIL_RE = re.compile(r"[A-Za-z0-9.+_-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,18}\d")
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

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "a":
            href = dict(attrs).get("href")
            if not href:
                return
            href = href.strip()
            if href.startswith("mailto:"):
                self.mailto.append(href[len("mailto:") :].split("?")[0])
            elif href.startswith("tel:"):
                self.tel.append(href[len("tel:") :])
            elif not href.startswith(("javascript:", "#")):
                absolute = urllib.parse.urljoin(self.base_url, href)
                if absolute.startswith(("http://", "https://")):
                    self.links.append(absolute)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
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
        return " ".join(self._text_parts)


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
    is known -- either a parsed robots.txt, or a confirmed 404 (no robots.txt
    published, permissive by convention). Returns ``(None, reason)`` when the
    policy could *not* be determined (DNS/connect/timeout failure, or any
    non-200/404 status such as a 5xx): crawling a host without knowing its
    robots policy is not permitted, so the caller must skip that host with an
    explicit reason instead of silently defaulting to permissive."""
    try:
        result = fetch_public_url(
            scheme_host + "/robots.txt",
            timeout=timeout,
            allowed_content_types=DEFAULT_ALLOWED_CONTENT_TYPES,
        )
    except FetchError as exc:
        return None, f"robots_fetch_failed: {exc}"
    if result.status == 404:
        parser = RobotFileParser()
        parser.set_url(scheme_host + "/robots.txt")
        parser.parse([])  # confirmed absent: permissive default, same as most crawlers
        return parser, None
    if result.status == 200:
        parser = RobotFileParser()
        parser.set_url(scheme_host + "/robots.txt")
        parser.parse((result.text or "").splitlines())
        return parser, None
    return None, f"robots_fetch_failed: status_{result.status}"


def research_website(
    url: str,
    max_pages: int = 5,
    *,
    timeout: float = 10.0,
    text_excerpt_limit: int = 4000,
    before_fetch: Callable[[], None] | None = None,
) -> WebsiteResearch:
    """Bounded same-host crawl starting at ``url``. Obeys robots.txt, follows
    only same-host links and same-host redirects, stops at ``max_pages``
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

    queue: list[str] = [url]
    seen: set[str] = set()
    pages: list[PageResult] = []
    contacts: list[ContactCandidate] = []
    internal_links: set[str] = set()
    external_links: set[str] = set()
    robots_disallowed: list[str] = []
    errors: list[str] = []

    while queue and len(pages) < max_pages:
        page_url = queue.pop(0)
        if page_url in seen:
            continue
        seen.add(page_url)

        if not robots.can_fetch(USER_AGENT, page_url):
            robots_disallowed.append(page_url)
            continue

        if before_fetch is not None:
            before_fetch()
        try:
            result = fetch_public_url(page_url, timeout=timeout, allow_cross_host_redirects=False)
        except FetchError as exc:
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

        if result.final_url != page_url and not robots.can_fetch(USER_AGENT, result.final_url):
            # ponytail: same-host redirect target checked after fetching; content is discarded, never used
            robots_disallowed.append(result.final_url)
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
        for email in _EMAIL_RE.findall(text):
            contacts.append(ContactCandidate("email", email.lower(), result.final_url, "text"))
        for mail in extractor.mailto:
            contacts.append(ContactCandidate("email", mail.lower(), result.final_url, "mailto_link"))
        for tel in extractor.tel:
            contacts.append(ContactCandidate("phone", tel.strip(), result.final_url, "tel_link"))
        for match in _PHONE_RE.findall(text):
            stripped = match.strip()
            if 7 <= sum(c.isdigit() for c in match) <= 15 and not _looks_like_date(stripped):
                contacts.append(ContactCandidate("phone", stripped, result.final_url, "text"))

        for link in extractor.links:
            link_host = urllib.parse.urlsplit(link).hostname
            if not link_host:
                continue
            if link_host.lower() == root_host:
                internal_links.add(link)
                if link not in seen and len(seen) + len(queue) < max_pages * 4:
                    queue.append(link)
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
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
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
    return SearchResult(query, base_url, items, degraded, error, fetched_at)
