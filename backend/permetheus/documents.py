"""Private company-document intake and proposed financial evidence extraction."""

import asyncio
import hashlib
import json
import os
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy import ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .auth import require_session
from .db import get_db, get_or_404
from .errors import ApiError
from .models import (
    Base, Company, Evidence, FinancialMetric, FinancialObservation, FinancialScope,
    FinancialStatus, IdMixin, Job, JobState, JsonType, ReviewStatus, Source, SourceKind, utcnow,
)

router = APIRouter(prefix="/api", tags=["documents"], dependencies=[Depends(require_session)])
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_MULTIPART_OVERHEAD = 128 * 1024
MAX_PDF_PAGES = 200
MAX_EXTRACTED_TEXT = 4 * 1024 * 1024
LLM_BATCH_CHARS = 25_000
METRICS = {m.value for m in FinancialMetric}
CONCEPT_METRICS = {
    "Revenue": "revenue",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
    "RevenueFromContractsWithCustomers": "revenue",
    "RevenueFromSaleOfGoods": "revenue",
    "RevenueFromRenderingOfServices": "revenue",
    "SalesRevenueNet": "revenue",
    "SalesRevenueGoodsAndServices": "revenue",
    "Turnover": "revenue",
    "EBITDA": "ebitda",
    "EBIT": "ebit",
    "OperatingProfitLoss": "ebit",
    "ProfitLoss": "net_income",
    "NetIncomeLoss": "net_income",
    "CashAndCashEquivalents": "cash",
    "CashAndCashEquivalentsAtCarryingValue": "cash",
    "Borrowings": "debt",
    "BorrowingsCurrent": "debt",
    "BorrowingsNoncurrent": "debt",
    "LongTermBorrowings": "debt",
    "LongTermDebt": "debt",
    "LongTermDebtCurrent": "debt",
    "LongTermDebtNoncurrent": "debt",
    "Equity": "equity",
    "TotalEquity": "equity",
    "EquityAttributableToOwnersOfParent": "equity",
    "EquityIncludingPortionAttributableToNoncontrollingInterests": "equity",
    "StockholdersEquity": "equity",
    "NumberOfEmployees": "employees",
    "AverageNumberOfEmployees": "employees",
    "EmployeesNumber": "employees",
}
_DOT_TRANSFORMS = {"num-dot-decimal", "num-dot-decimal-apos", "numdotdecimal", "numdotdecimalin"}
_COMMA_TRANSFORMS = {"num-comma-decimal", "num-comma-decimal-apos", "numcommadecimal"}
_APOSTROPHE_TRANSFORMS = {"num-dot-decimal-apos", "num-comma-decimal-apos"}
_CURRENCY_RE = re.compile(r"[A-Z]{3}\Z")


class Document(IdMixin, Base):
    __tablename__ = "company_documents"

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    media_type: Mapped[str] = mapped_column(String(64))
    artifact_path: Mapped[str] = mapped_column(String(700))
    sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int]
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    progress: Mapped[int] = mapped_column(default=0)
    result: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    warning: Mapped[str | None] = mapped_column(String(300))
    error: Mapped[str | None] = mapped_column(String(300))


def _root(request: Request) -> Path:
    return Path(request.app.state.settings.data_dir).resolve()


def _document_dir(root: Path, company_id: uuid.UUID, document_id: uuid.UUID) -> Path:
    base = (root / "documents").resolve()
    path = (base / str(company_id) / str(document_id)).resolve()
    if not path.is_relative_to(base):
        raise ApiError(500, "storage_error", "Document storage path is invalid")
    return path


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


async def _body(request: Request) -> bytes:
    total = 0
    chunks = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD:
            raise ApiError(413, "document_too_large", "Document upload is limited to 25 MiB")
        chunks.append(chunk)
    return b"".join(chunks)


def _multipart(content_type: str, payload: bytes) -> tuple[dict[str, str], dict[str, tuple[str, str, bytes]]]:
    if not content_type.lower().startswith("multipart/form-data"):
        raise ApiError(415, "unsupported_media_type", "Upload a multipart PDF or XHTML document")
    message = BytesParser(policy=policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + payload
    )
    if not message.is_multipart():
        raise ApiError(400, "bad_request", "Malformed multipart body")
    fields, files = {}, {}
    for part in message.iter_parts():
        key = part.get_param("name", header="content-disposition")
        if not key:
            continue
        data = part.get_payload(decode=True) or b""
        if part.get_filename() is None:
            if key in fields:
                raise ApiError(400, "bad_request", "Multipart fields must be unique")
            fields[key] = data.decode("utf-8", "replace").strip()
        else:
            if key in files:
                raise ApiError(400, "bad_request", "Only one document file is accepted")
            files[key] = (Path(part.get_filename()).name[:500], part.get_content_type().lower(), data)
    return fields, files


def _validate_upload(media_type: str, data: bytes) -> str:
    if not data:
        raise ApiError(400, "empty_document", "Document is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ApiError(413, "document_too_large", "Document upload is limited to 25 MiB")
    if media_type == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise ApiError(415, "invalid_document", "File content is not a PDF")
        return ".pdf"
    if media_type in ("application/xhtml+xml", "application/ixbrl+xml"):
        if _has_xml_declarations(data):
            raise ApiError(422, "unsafe_xml", "DTD and entity declarations are not accepted")
        if re.search(br"<(?:[A-Za-z_][\w.-]*:)?html\b", data[:16_384], re.IGNORECASE) is None:
            raise ApiError(422, "invalid_document", "XHTML document root must be html")
        return ".xhtml"
    raise ApiError(415, "unsupported_media_type", "Only PDF and XHTML/iXBRL documents are accepted")


def _document_out(db: Session, row: Document) -> dict[str, Any]:
    job = db.scalar(select(Job).where(Job.idempotency_key == f"document.extract:{row.id}"))
    return {
        "id": str(row.id), "company_id": str(row.company_id), "title": row.title,
        "media_type": row.media_type, "sha256": row.sha256, "byte_size": row.byte_size,
        "status": row.status, "progress": row.progress, "result": row.result,
        "warning": row.warning, "error": row.error, "job_id": str(job.id) if job else None,
        "created_at": row.created_at.isoformat(),
        "financials": [
            {"id": str(item.id), "metric": item.metric.value, "amount": str(item.amount) if item.amount is not None else None,
             "currency": item.currency, "period_start": item.period_start.isoformat(),
             "period_end": item.period_end.isoformat(), "scope": item.scope.value,
             "status": item.status.value, "review_status": item.review_status.value,
             "source_id": str(item.source_id)}
            for item in db.scalars(select(FinancialObservation).where(FinancialObservation.source_id == row.source_id))
        ],
    }


@router.post("/companies/{company_id}/documents", status_code=202)
async def upload_document(company_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    company = get_or_404(db, Company, company_id)
    content_type = request.headers.get("content-type", "")
    fields, files = await asyncio.to_thread(_multipart, content_type, await _body(request))
    if set(files) != {"document"}:
        raise ApiError(400, "document_required", "Attach exactly one file in the 'document' field")
    title, media_type, data = next(iter(files.values()))
    suffix = _validate_upload(media_type, data)
    document_id = uuid.uuid4()
    folder = _document_dir(_root(request), company.id, document_id)
    _private_dir(folder.parent.parent)
    _private_dir(folder.parent)
    _private_dir(folder)
    path = folder / f"source{suffix}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        digest = hashlib.sha256(data).hexdigest()
        source = Source(kind=SourceKind.document, title=title or f"Company document{suffix}",
                        content_hash=digest, fetched_at=utcnow())
        db.add(source)
        db.flush()
        row = Document(id=document_id, company_id=company.id, source_id=source.id,
                       title=title or f"Company document{suffix}", media_type=media_type,
                       artifact_path=str(path.relative_to(_root(request))), sha256=digest, byte_size=len(data))
        db.add(row)
        db.add(Job(kind="document.extract", payload={"document_id": str(document_id)},
                   company_id=company.id, idempotency_key=f"document.extract:{document_id}",
                   state=JobState.queued))
        db.commit()
    except Exception:
        db.rollback()
        path.unlink(missing_ok=True)
        raise
    return _document_out(db, row)


@router.get("/companies/{company_id}/documents")
def list_documents(company_id: uuid.UUID, db: Session = Depends(get_db)):
    get_or_404(db, Company, company_id)
    rows = db.scalars(select(Document).where(Document.company_id == company_id).order_by(Document.created_at.desc()))
    return [_document_out(db, row) for row in rows]


@router.get("/documents/{document_id}")
def get_document(document_id: uuid.UUID, db: Session = Depends(get_db)):
    return _document_out(db, get_or_404(db, Document, document_id))


@router.get("/documents/{document_id}/source")
def get_document_source(document_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    row = get_or_404(db, Document, document_id)
    path = (_root(request) / row.artifact_path).resolve()
    if not path.is_relative_to((_root(request) / "documents").resolve()) or not path.is_file():
        raise ApiError(404, "not_found", "Document source file not found")
    return FileResponse(path, media_type=row.media_type, filename=f"document-{row.id}{path.suffix}")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _scope(context: ET.Element) -> FinancialScope:
    # A dimensionless context does not establish whether the filing is entity or group level.
    # Dimensional facts are filtered before storage; their dimension cannot be generalized.
    return FinancialScope.unknown


def _context_dates(context: ET.Element) -> tuple[date, date] | None:
    for node in context.iter():
        name = _local(node.tag)
        if name == "instant" and node.text:
            day = date.fromisoformat(node.text.strip())
            return day, day
    start = end = None
    for node in context.iter():
        if _local(node.tag) == "startDate" and node.text:
            start = date.fromisoformat(node.text.strip())
        elif _local(node.tag) == "endDate" and node.text:
            end = date.fromisoformat(node.text.strip())
    return (start, end) if start and end and start <= end else None


def _metric(name: str) -> str | None:
    return CONCEPT_METRICS.get(name.rsplit(":", 1)[-1])


def _has_xml_declarations(data: bytes) -> bool:
    """Detect DTD/entity declarations in UTF-8, UTF-16, or UTF-32 XML encodings."""
    try:
        if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            text = data.decode("utf-32")
        elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        elif data[:4] == b"<\x00\x00\x00":
            text = data.decode("utf-32-le")
        elif data[:4] == b"\x00\x00\x00<":
            text = data.decode("utf-32-be")
        elif data[:2] == b"<\x00":
            text = data.decode("utf-16-le")
        elif data[:2] == b"\x00<":
            text = data.decode("utf-16-be")
        else:
            text = data.decode("utf-8", "replace")
    except UnicodeError:
        return True
    return re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE) is not None


def _has_unsupported_segment(context: ET.Element) -> bool:
    for node in context.iter():
        if _local(node.tag) in ("explicitMember", "typedMember"):
            return True
        if _local(node.tag) in ("segment", "scenario") and (len(node) or (node.text or "").strip()):
            return True
    return False


def _parse_numeric(raw: str, transform: str | None) -> Decimal | None:
    value = raw.strip().replace("\u202f", " ")
    if not transform:
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)", value):
            return None
        try:
            return Decimal(value)
        except InvalidOperation:
            return None

    name = transform.rsplit(":", 1)[-1]
    allowed = _APOSTROPHE_TRANSFORMS if name in _APOSTROPHE_TRANSFORMS else set()
    separators = r"'´’′" if allowed else ""
    if name in _DOT_TRANSFORMS:
        if name == "numdotdecimalin":
            pattern = rf"(?:\d+|\d{{1,2}}(?:,\d{{2}})*,\d{{3}})(?:\.\d+)?"
        else:
            pattern = rf"[0-9, \u00a0{separators}]*?(?:\.[0-9 \u00a0]+)?"
        if not re.fullmatch(pattern, value) or not re.search(r"\d", value):
            return None
        if value.count(".") > 1:
            return None
        integer, dot, fraction = value.partition(".")
        integer_digits = re.sub(rf"[, \u00a0{separators}]", "", integer)
        fraction_digits = re.sub(r"[ \u00a0]", "", fraction)
        normalized = integer_digits + (("." + fraction_digits) if dot else "")
    elif name in _COMMA_TRANSFORMS:
        pattern = rf"[0-9. \u00a0{separators}]*?(?:,[0-9 \u00a0]+)?"
        if not re.fullmatch(pattern, value) or not re.search(r"\d", value):
            return None
        if value.count(",") > 1:
            return None
        integer, comma, fraction = value.partition(",")
        integer_digits = re.sub(rf"[. \u00a0{separators}]", "", integer)
        fraction_digits = re.sub(r"[ \u00a0]", "", fraction)
        normalized = integer_digits + (("." + fraction_digits) if comma else "")
    else:
        return None
    if not normalized or normalized == "." or normalized.endswith("."):
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _unit_currency(unit: ET.Element) -> tuple[str | None, bool]:
    measures = ["".join(m.itertext()).strip() for m in unit.iter() if _local(m.tag) == "measure"]
    if len(measures) != 1:
        return None, False
    measure = measures[0]
    if measure in ("xbrli:pure", "pure", "http://www.xbrl.org/2003/instance}pure"):
        return None, True
    match = re.fullmatch(r"(?:iso4217:|\{http://www\.xbrl\.org/2003/iso4217\})?([A-Z]{3})", measure)
    if match and (":" in measure or "iso4217" in measure or "{" in measure):
        return match.group(1), False
    return None, False


def _extract_ixbrl(data: bytes) -> list[dict[str, Any]]:
    findings, _ = _extract_ixbrl_detailed(data)
    return findings


def _extract_ixbrl_detailed(data: bytes) -> tuple[list[dict[str, Any]], str | None]:
    if _has_xml_declarations(data):
        raise ValueError("DTD and entity declarations are not accepted")
    root = ET.fromstring(data)
    if _local(root.tag).lower() != "html":
        raise ValueError("XHTML document root must be html")
    nodes = list(root.iter())
    if len(nodes) > 200_000:
        raise ValueError("XHTML has too many XML elements")
    contexts = {_attr(n, "id"): n for n in nodes if _local(n.tag) == "context" and _attr(n, "id")}
    units = {_attr(n, "id"): n for n in nodes if _local(n.tag) == "unit" and _attr(n, "id")}
    results, seen, skipped = [], set(), {}
    for node in nodes:
        node_kind = _local(node.tag)
        if node_kind not in ("nonFraction", "fraction"):
            continue
        def skip(reason: str) -> None:
            skipped[reason] = skipped.get(reason, 0) + 1

        if node_kind == "fraction":
            skip("fraction")
            continue
        metric = _metric(_attr(node, "name") or "")
        context_id = _attr(node, "contextRef")
        unit_id = _attr(node, "unitRef")
        context = contexts.get(context_id)
        unit = units.get(unit_id)
        if metric not in METRICS:
            skip("concept")
            continue
        if context is None:
            skip("context")
            continue
        if _has_unsupported_segment(context):
            skip("dimensional segment")
            continue
        if unit is None:
            skip("unit")
            continue
        if not "".join(node.itertext()).strip():
            skip("empty value")
            continue
        try:
            periods = _context_dates(context)
        except ValueError:
            skip("period")
            continue
        if periods is None:
            skip("period")
            continue
        format_name = _attr(node, "format")
        lexical = "".join(node.itertext()).strip()
        value = _parse_numeric(lexical, format_name)
        if value is None:
            skip("numeric format")
            continue
        try:
            scale = int(_attr(node, "scale") or "0")
            if abs(scale) > 100 or len(lexical) > 100:
                skip("scale/value bound")
                continue
            amount = value * (Decimal(10) ** scale)
        except (InvalidOperation, ValueError, OverflowError):
            skip("numeric value")
            continue
        sign = _attr(node, "sign")
        if sign not in (None, "", "+", "-"):
            skip("sign")
            continue
        if sign == "-":
            amount = -amount
        if (not amount.is_finite() or len(amount.as_tuple().digits) > 24
                or amount.adjusted() >= 20 or max(0, -amount.as_tuple().exponent) > 4):
            skip("numeric precision")
            continue
        currency, is_pure = _unit_currency(unit)
        if metric == "employees":
            if not is_pure:
                skip("employee unit")
                continue
        elif currency is None:
            skip("monetary unit")
            continue
        context_string = ET.tostring(context, encoding="unicode")
        scope = _scope(context)
        locator = {"format": "ixbrl", "concept": _attr(node, "name"), "context_id": context_id,
                   "unit_id": unit_id, "scale": _attr(node, "scale"), "sign": sign,
                   "numeric_format": format_name, "context": context_string}
        key = (metric, str(amount), currency, periods, context_string, locator["concept"], format_name)
        if key in seen:
            skip("duplicate")
            continue
        seen.add(key)
        results.append({
            "metric": metric, "amount": str(amount), "currency": currency,
            "period_start": periods[0].isoformat(), "period_end": periods[1].isoformat(),
            "scope": scope.value, "status": FinancialStatus.reported.value,
            "quote": lexical, "locator": locator,
        })
    warning = None
    if skipped:
        details = ", ".join(f"{count} {reason}" for reason, count in sorted(skipped.items()))
        warning = f"Preserved source; skipped unsupported iXBRL facts ({details})"[:300]
    return results, warning


def _attr(node: ET.Element, name: str) -> str | None:
    for key, value in node.attrib.items():
        if _local(key) == name:
            return value
    return None


def _pdf_pages(path: Path) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF extraction requires the pypdf package") from exc
    reader = PdfReader(str(path), strict=False)
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ValueError("PDF exceeds the 200-page processing limit")
    pages, total = [], 0
    for page in reader.pages:
        text = page.extract_text() or ""
        total += len(text)
        if total > MAX_EXTRACTED_TEXT:
            raise ValueError("Extracted PDF text exceeds the processing limit")
        pages.append(text)
    return pages


def _pdf_batches(pages: list[str]) -> list[list[tuple[int, str]]]:
    batches, current, size = [], [], 0
    for number, text in enumerate(pages, 1):
        if not text.strip():
            continue
        if current and size + len(text) > LLM_BATCH_CHARS:
            batches.append(current)
            current, size = [], 0
        if len(text) > LLM_BATCH_CHARS:
            # Oversized single pages are sent in disjoint slices with no invented page-level join.
            for start in range(0, len(text), LLM_BATCH_CHARS):
                batches.append([(number, text[start:start + LLM_BATCH_CHARS])])
            continue
        current.append((number, text))
        size += len(text)
    if current:
        batches.append(current)
    return batches


async def _extract_pdf(pages: list[str], llm, guard=None) -> tuple[list[dict[str, Any]], str | None]:
    if llm is None or not llm.configured:
        return [], "PDF text was retained; configure the language model to extract proposed financials"
    prompt = (
        'Extract only explicit financial values. Return JSON {"financials":[{"metric":"revenue|ebitda|ebit|net_income|cash|debt|equity|employees", '
        '"amount":"decimal string","currency":"ISO 4217 code or null","period_start":"YYYY-MM-DD", '
        '"period_end":"YYYY-MM-DD","scope":"entity|consolidated|unknown","quote":"exact quote", "page":1}]}. '
        "Do not calculate, estimate, infer missing periods, or invent values. Every quote must be copied exactly from one supplied page."
    )
    findings = []
    batches = _pdf_batches(pages)
    succeeded = 0
    for batch in batches:
        if guard is not None and not guard():
            break
        source = "\n".join(f"[PAGE {n}]\n{text}" for n, text in batch)
        try:
            parsed = await llm.extract(source, prompt)
        except Exception:
            continue
        succeeded += 1
        allowed = dict(batch)
        for row in parsed.get("financials", []) if isinstance(parsed, dict) else []:
            finding = _validate_pdf_finding(row, allowed)
            if finding:
                findings.append(finding)
    warning = None
    if batches and succeeded < len(batches):
        warning = f"Partial extraction: {succeeded} of {len(batches)} text batches processed"
    return findings, warning


def _validate_pdf_finding(row: Any, pages: dict[int, str]) -> dict[str, Any] | None:
    if not isinstance(row, dict) or row.get("metric") not in METRICS:
        return None
    quote, page = row.get("quote"), row.get("page")
    if not isinstance(page, int) or isinstance(page, bool) or page not in pages:
        return None
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 2000 or quote not in pages[page]:
        return None
    try:
        amount = Decimal(row.get("amount"))
        period_start = date.fromisoformat(row.get("period_start"))
        period_end = date.fromisoformat(row.get("period_end"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if (not amount.is_finite() or period_start > period_end or len(amount.as_tuple().digits) > 24
            or amount.adjusted() >= 20 or max(0, -amount.as_tuple().exponent) > 4):
        return None
    currency = row.get("currency")
    if currency is not None and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
        return None
    if (row["metric"] == "employees" and currency is not None) or (row["metric"] != "employees" and currency is None):
        return None
    scope = row.get("scope")
    if scope not in (FinancialScope.entity.value, FinancialScope.consolidated.value, FinancialScope.unknown.value):
        return None
    return {"metric": row["metric"], "amount": str(amount), "currency": currency,
            "period_start": period_start.isoformat(), "period_end": period_end.isoformat(),
            "scope": scope, "status": FinancialStatus.reported.value, "quote": quote,
            "locator": {"format": "pdf", "page": page, "quote": quote}}


def _artifact_path(data_root: Path, relative: str) -> Path:
    root = (data_root / "documents").resolve()
    path = (data_root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise FileNotFoundError("Stored document is unavailable")
    return path


def _persist_findings(db: Session, row: Document, source: Source, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Idempotently upsert facts without replacing reviewed observations or evidence."""
    prior_evidence = db.scalars(select(Evidence).where(Evidence.source_id == source.id)).all()
    evidence_by_key = {
        e.locator.get("fact_key"): e for e in prior_evidence
        if isinstance(e.locator, dict) and e.locator.get("fact_key")
    }
    prior_observations = db.scalars(
        select(FinancialObservation).where(FinancialObservation.source_id == source.id)
    ).all()
    observations_by_value = {}
    observations_by_id = {}
    for observation in prior_observations:
        value_key = (
            observation.metric.value, observation.amount, observation.currency,
            observation.period_start, observation.period_end, observation.scope.value,
        )
        observations_by_value.setdefault(value_key, observation)
        observations_by_id[str(observation.id)] = observation
    saved = []
    for item in findings:
        locator = dict(item["locator"])
        fact_key = _fact_key(item)
        locator["fact_key"] = fact_key
        metric = FinancialMetric(item["metric"])
        start, end = date.fromisoformat(item["period_start"]), date.fromisoformat(item["period_end"])
        amount = Decimal(item["amount"])
        value_key = (metric.value, amount, item["currency"], start, end, item["scope"])
        evidence = evidence_by_key.get(fact_key)
        linked_id = evidence.value.get("observation_id") if evidence and isinstance(evidence.value, dict) else None
        observation = observations_by_id.get(linked_id) if linked_id else None
        if observation is None and evidence is None:
            observation = observations_by_value.get(value_key)
        if observation is None:
            observation = FinancialObservation(
                id=uuid.uuid4(),
                company_id=row.company_id, source_id=source.id, metric=metric,
                amount=amount, currency=item["currency"], period_start=start, period_end=end,
                scope=FinancialScope(item["scope"]), status=FinancialStatus.reported,
                review_status=ReviewStatus.proposed,
            )
            db.add(observation)
            observations_by_value[value_key] = observation
            observations_by_id[str(observation.id)] = observation
        if evidence is None:
            evidence = Evidence(
                company_id=row.company_id, source_id=source.id, field=f"financial.{metric.value}",
                value={"amount": item["amount"], "currency": item["currency"], "period_start": item["period_start"],
                       "period_end": item["period_end"], "scope": item["scope"],
                       "observation_id": str(observation.id)},
                excerpt=item["quote"], locator=locator, extraction_method=locator["format"],
                review_status=ReviewStatus.proposed,
            )
            db.add(evidence)
            evidence_by_key[fact_key] = evidence
        elif isinstance(evidence.value, dict) and not evidence.value.get("observation_id"):
            evidence.value = {**evidence.value, "observation_id": str(observation.id)}
        saved.append({"metric": metric.value, "amount": item["amount"], "currency": item["currency"],
                      "period_start": item["period_start"], "period_end": item["period_end"],
                      "scope": item["scope"], "status": "reported",
                      "review_status": observation.review_status.value,
                      "evidence_review_status": evidence.review_status.value,
                      "excerpt": item["quote"], "locator": locator})
    row.result = saved
    return saved


def _fact_key(item: dict[str, Any]) -> str:
    identity = {key: item[key] for key in (
        "metric", "amount", "currency", "period_start", "period_end", "scope", "quote", "locator",
    )}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def process_document(document_id: uuid.UUID, sessionmaker, llm, data_root: Path,
                           job_guard=None) -> None:
    """Persistent-worker entrypoint; pass settings.data_dir as ``data_root``."""
    if job_guard is not None and not job_guard():
        return
    root = Path(data_root).resolve()
    with sessionmaker() as db:
        row = db.get(Document, document_id)
        if row is None:
            raise LookupError("Document no longer exists")
        if row.status == "completed":
            return
        row.status, row.progress, row.error, row.warning = "processing", 5, None, None
        db.commit()
        relative, media_type, source_id = row.artifact_path, row.media_type, row.source_id
    try:
        path = _artifact_path(root, relative)
        data = await asyncio.to_thread(path.read_bytes)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("Document exceeds the processing size limit")
        if media_type in ("application/xhtml+xml", "application/ixbrl+xml"):
            findings, warning = await asyncio.to_thread(_extract_ixbrl_detailed, data)
        elif media_type == "application/pdf":
            pages = await asyncio.to_thread(_pdf_pages, path)
            with sessionmaker() as db:
                row = db.get(Document, document_id)
                if row is not None:
                    row.progress = 35
                    db.commit()
            findings, warning = await _extract_pdf(pages, llm, job_guard)
        else:
            raise ValueError("Unsupported stored document type")
        if job_guard is not None and not job_guard():
            return
        with sessionmaker() as db:
            row, source = db.get(Document, document_id), db.get(Source, source_id)
            if row is None or source is None:
                return
            _persist_findings(db, row, source, findings)
            row.status, row.progress, row.warning = "completed", 100, warning
            db.commit()
    except Exception as exc:
        with sessionmaker() as db:
            row = db.get(Document, document_id)
            if row is not None:
                row.status, row.error = "failed", "Document parsing or extraction failed"
                db.commit()
        raise RuntimeError("Document extraction failed") from exc
