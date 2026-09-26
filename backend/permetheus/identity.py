"""Pure normalization used for company identity resolution and input validation."""

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

LEGAL_SUFFIXES = {
    "oy", "oyj", "ab", "abp", "ky", "tmi", "ltd", "limited", "inc", "llc", "plc", "corp",
    "corporation", "co", "company", "gmbh", "ag", "as", "asa", "aps", "bv", "nv", "sa", "sarl",
    "srl", "spa",
}
COUNTRY = re.compile(r"^[A-Z]{2}$")
FI_BUSINESS_ID = re.compile(r"^(\d{6,7})-?(\d)$")
GENERIC_ID = re.compile(r"^[A-Z0-9./-]{2,64}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE = re.compile(r"^\+?[0-9]{6,15}$")
HOST = re.compile(r"^[a-z0-9.-]{1,253}$")


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).casefold()
    tokens = re.sub(r"[^\w\s]", " ", text).split()
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_country(value: str) -> str:
    code = value.strip().upper()
    if not COUNTRY.match(code):
        raise ValueError("country must be an ISO 3166-1 alpha-2 code")
    return code


def normalize_website(value: str) -> tuple[str, str]:
    """Return (canonical URL, registrable-ish domain). Rejects non-public or credentialed URLs."""
    raw = value.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError("website must use http or https")
    if parts.username or parts.password:
        raise ValueError("website must not contain credentials")
    host = (parts.hostname or "").rstrip(".")
    if _is_ip(host):
        raise ValueError("website must be a domain name, not an IP address")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("website has an invalid domain name") from exc
    if not HOST.match(host) or "." not in host or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("website must be a public domain name")
    domain = host.removeprefix("www.")
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{host}{path}", domain


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def normalize_business_id(country: str, value: str) -> str:
    compact = re.sub(r"\s", "", value).upper()
    if country != "FI":
        if not GENERIC_ID.match(compact):
            raise ValueError("business_id contains unsupported characters")
        return compact
    match = FI_BUSINESS_ID.match(compact.removeprefix("FI"))
    if not match:
        raise ValueError("Finnish business ID must look like 1234567-8")
    digits, check = match.group(1).zfill(7), int(match.group(2))
    remainder = sum(int(d) * w for d, w in zip(digits, (7, 9, 10, 5, 8, 4, 2))) % 11
    if remainder == 1 or (0 if remainder == 0 else 11 - remainder) != check:
        raise ValueError("Finnish business ID checksum is invalid")
    return f"{digits}-{check}"


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 320 or not EMAIL.match(email):
        raise ValueError("invalid email address")
    return email


def normalize_phone(value: str) -> str:
    phone = re.sub(r"[\s().-]", "", value)
    if not PHONE.match(phone):
        raise ValueError("phone must contain 6-15 digits, optionally prefixed with +")
    return phone
