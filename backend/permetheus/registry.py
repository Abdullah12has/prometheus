"""Country registry population: durable bulk imports and low-concurrency
enrichment campaigns.

- ``registry.import`` jobs run on their own loop (``background.registry_loop``
  or ``python -m permetheus.registry``), so an hours-long import never blocks
  enrichment or media jobs. Each batch is written with Core bulk inserts and
  its checkpoint in one fenced transaction; a crash or pause resumes after the
  last committed batch.
- Companies dedupe on the unique (jurisdiction, scheme, value) identifier.
  A matched company only gets empty columns filled; non-empty values, which a
  human may have reviewed, are never overwritten. The registry fields go to
  one ``registry_record`` Evidence row per company and snapshot, skipped when
  the latest one is identical, under one ``Source`` per import.
- Enrichment campaigns feed ordinary ``company.enrich`` jobs, keeping at most
  ``max_in_flight`` queued or running, from a (created_at, id) keyset.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Index, and_, exists, func, insert, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from . import registry_sources as sources
from . import research, worker
from .auth import require_session
from .config import Settings
from .db import get_db, get_or_404, record_activity
from .errors import ApiError
from .identity import normalize_name, normalize_website
from .models import (
    Base, Company, CompanyIdentifier, CompanyStatus, Evidence, IdMixin, Job, JobState, JsonType,
    ReviewStatus, SellerIntent, Source, SourceKind, enum_col, utcnow,
)
from .research_models import JOB_KIND_ENRICH, ResearchRun, ResearchRunStatus

log = logging.getLogger("permetheus.registry")

JOB_KIND_REGISTRY_IMPORT = "registry.import"
RECORD_FIELD = "registry_record"
MAX_ERRORS = 20
COUNTRIES = ("FI", "CH", "DE")

# Serves per-country counts and the enrichment keyset. Created at startup;
# ``create_all`` does not add indexes to an existing table.
COUNTRY_INDEX = Index("ix_companies_country_created_id", Company.country, Company.created_at, Company.id)


class ImportStatus(StrEnum):
    queued = "queued"
    running = "running"
    paused = "paused"
    completed = "completed"
    failed = "failed"


class CampaignStatus(StrEnum):
    running = "running"
    paused = "paused"
    completed = "completed"


class RegistryImport(IdMixin, Base):
    __tablename__ = "registry_imports"
    source: Mapped[str] = mapped_column(index=True)
    country: Mapped[str] = mapped_column()
    status: Mapped[ImportStatus] = mapped_column(enum_col(ImportStatus), default=ImportStatus.queued)
    job_id: Mapped[uuid.UUID | None] = mapped_column()
    source_row_id: Mapped[uuid.UUID | None] = mapped_column()
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    source_total: Mapped[int | None] = mapped_column(default=None)
    processed: Mapped[int] = mapped_column(default=0)
    created: Mapped[int] = mapped_column(default=0)
    matched: Mapped[int] = mapped_column(default=0)
    skipped: Mapped[int] = mapped_column(default=0)
    skip_reasons: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    progress: Mapped[float | None] = mapped_column(default=None)
    snapshot: Mapped[str | None] = mapped_column(default=None)
    exhausted: Mapped[bool] = mapped_column(default=False)
    errors: Mapped[list[str]] = mapped_column(JsonType, default=list)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class EnrichmentCampaign(IdMixin, Base):
    __tablename__ = "enrichment_campaigns"
    country: Mapped[str] = mapped_column(index=True)
    status: Mapped[CampaignStatus] = mapped_column(enum_col(CampaignStatus), default=CampaignStatus.running)
    max_in_flight: Mapped[int] = mapped_column(default=2)
    max_companies: Mapped[int | None] = mapped_column(default=None)
    enqueued: Mapped[int] = mapped_column(default=0)
    exhausted: Mapped[bool] = mapped_column(default=False)
    last_created_at: Mapped[datetime | None] = mapped_column(default=None)
    last_company_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


OPEN_IMPORT_INDEX = Index("uq_registry_import_open_source", RegistryImport.source, unique=True,
                         postgresql_where=RegistryImport.status != ImportStatus.completed,
                         sqlite_where=RegistryImport.status != ImportStatus.completed)
OPEN_CAMPAIGN_INDEX = Index("uq_enrichment_open_country", EnrichmentCampaign.country, unique=True,
                           postgresql_where=EnrichmentCampaign.status != CampaignStatus.completed,
                           sqlite_where=EnrichmentCampaign.status != CampaignStatus.completed)


STOP = threading.Event()  # set by background.stop so a long import thread exits promptly
_OWNED_IMPORTS: dict[uuid.UUID, tuple[int, sessionmaker, dict[str, Any]]] = {}
_OWNED_IMPORTS_LOCK = threading.Lock()


class Shutdown(Exception):
    pass


def requeue_owned_jobs() -> None:
    """Fenced-requeue this process's active imports from their committed checkpoint."""
    with _OWNED_IMPORTS_LOCK:
        owned = list(_OWNED_IMPORTS.items())
    for job_id, (fencing, Session, payload) in owned:
        import_id = (payload or {}).get("import_id")
        if not import_id:
            continue
        now = utcnow()
        try:
            with Session() as db:
                changed = db.execute(update(RegistryImport).where(
                    RegistryImport.id == uuid.UUID(import_id),
                    RegistryImport.status.in_((ImportStatus.queued, ImportStatus.running))
                ).values(status=ImportStatus.queued, updated_at=now)).rowcount
                if changed != 1:
                    db.rollback()
                    continue
                changed = db.execute(update(Job).where(
                    Job.id == job_id, Job.attempts == fencing, Job.state == JobState.running
                ).values(state=JobState.queued, lease_until=None, available_at=now, updated_at=now,
                         payload={**payload, "retry_base": fencing})).rowcount
                if changed != 1:
                    db.rollback()
                    continue
                db.commit()
        except Exception:
            log.exception("Could not requeue registry import job %s during shutdown", job_id)


def init(engine: Engine) -> None:
    COUNTRY_INDEX.create(engine, checkfirst=True)
    OPEN_IMPORT_INDEX.create(engine, checkfirst=True)
    OPEN_CAMPAIGN_INDEX.create(engine, checkfirst=True)


def _errors(existing: list[str], new: list[str]) -> list[str]:
    stamp = utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return [*existing, *(f"{stamp} {e}"[:500] for e in new)][-MAX_ERRORS:]


# ---------------------------------------------------------------------------
# Batch persistence
# ---------------------------------------------------------------------------

def _website(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    try:
        return normalize_website(value)
    except ValueError:
        return None, None


def persist_batch(db: Session, *, source: str, source_row_id: uuid.UUID, import_id: uuid.UUID,
                  entities: list[sources.Entity]) -> tuple[int, int]:
    """Write one batch without the ORM unit of work. Returns (created, matched).
    The caller commits, together with the checkpoint."""
    by_key = {(e.country, e.scheme, e.value): e for e in entities}
    if not by_key:
        return 0, 0
    now = utcnow()
    existing: dict[tuple[str, str, str], uuid.UUID] = {}
    for country, scheme in {(c, s) for c, s, _ in by_key}:
        values = [v for c, s, v in by_key if (c, s) == (country, scheme)]
        rows = db.execute(select(CompanyIdentifier.value, CompanyIdentifier.company_id).where(
            CompanyIdentifier.jurisdiction == country, CompanyIdentifier.scheme == scheme,
            CompanyIdentifier.value.in_(values)))
        existing.update({(country, scheme, v): cid for v, cid in rows})

    companies, identifiers, company_of = [], [], {}
    for key, e in by_key.items():
        if key in existing:
            company_of[key] = existing[key]
            continue
        cid = company_of[key] = uuid.uuid4()
        website, domain = _website(e.website)
        companies.append({
            "id": cid, "created_at": now, "updated_at": now, "name": e.name[:300],
            "name_normalized": normalize_name(e.name)[:300], "country": e.country,
            "industry": (e.industry or None) and e.industry[:300], "description": e.description,
            "website": website, "domain": domain, "registry_status": e.registry_status,
            "status": CompanyStatus.provisional, "seller_intent": SellerIntent.unknown,
        })
        identifiers.append({"id": uuid.uuid4(), "created_at": now, "company_id": cid, "scheme": e.scheme,
                            "jurisdiction": e.country, "value": e.value, "source_id": source_row_id})
    if companies:
        db.execute(insert(Company), companies)
        db.execute(insert(CompanyIdentifier), identifiers)

    matched_ids = [existing[k] for k in by_key if k in existing]
    if matched_ids:
        _fill_empty(db, {existing[k]: e for k, e in by_key.items() if k in existing}, now)

    method = f"registry_import:{source}"
    latest: dict[uuid.UUID, Any] = {}
    if matched_ids:
        rows = db.execute(select(Evidence.company_id, Evidence.value).where(
            Evidence.company_id.in_(matched_ids), Evidence.field == RECORD_FIELD,
            Evidence.extraction_method == method).order_by(Evidence.created_at))
        latest = {cid: value for cid, value in rows}
    evidence = [
        {"id": uuid.uuid4(), "created_at": now, "company_id": company_of[key], "source_id": source_row_id,
         "field": RECORD_FIELD, "value": e.fields, "excerpt": f"{e.name} ({e.scheme} {e.value})",
         "locator": {"import_id": str(import_id), "record_url": e.record_url},
         "extraction_method": method, "review_status": ReviewStatus.proposed}
        for key, e in by_key.items() if latest.get(company_of[key]) != e.fields
    ]
    if evidence:
        db.execute(insert(Evidence), evidence)
    return len(companies), len(matched_ids)


def _fill_empty(db: Session, matched: dict[uuid.UUID, sources.Entity], now: datetime) -> None:
    rows = db.execute(select(Company.id, Company.website, Company.industry, Company.description,
                             Company.registry_status, Company.country).where(Company.id.in_(list(matched))))
    for cid, website, industry, description, registry_status, country in rows:
        e = matched[cid]
        values: dict[str, Any] = {}
        if not website and e.website:
            values["website"], values["domain"] = _website(e.website)
            if not values["website"]:
                values = {}
        if not industry and e.industry:
            values["industry"] = e.industry[:300]
        if not description and e.description:
            values["description"] = e.description
        if not registry_status and e.registry_status:
            values["registry_status"] = e.registry_status
        if not country:
            values["country"] = e.country
        if values:
            db.execute(update(Company).where(Company.id == cid).values(updated_at=now, **values))


# ---------------------------------------------------------------------------
# Import job handler
# ---------------------------------------------------------------------------

def _source_row(db: Session, imp: RegistryImport, batch: sources.Batch) -> uuid.UUID:
    previous = db.get(Source, imp.source_row_id) if imp.source_row_id else None
    if previous is not None and (not batch.source_url or previous.url == batch.source_url):
        return previous.id
    info = sources.SOURCES[imp.source]
    row = Source(kind=SourceKind.registry, url=batch.source_url or info.url, publisher=info.publisher,
                 title=f"{info.label}, snapshot {batch.snapshot or 'unknown'}"[:500], fetched_at=utcnow(),
                 content_hash=batch.content_hash)
    db.add(row)
    db.flush()
    imp.source_row_id = row.id
    return row.id


def run_import(Session: sessionmaker, job: Job, fencing: int, settings: Settings) -> None:
    import_id = uuid.UUID(job.payload["import_id"])
    with Session() as db:
        imp = db.get(RegistryImport, import_id)
        if imp is None or imp.status == ImportStatus.completed:
            research._fence(db, job.id, fencing, finish=JobState.succeeded)
            db.commit()
            return
        imp.status, imp.started_at, imp.finished_at = ImportStatus.running, imp.started_at or utcnow(), None
        research._fence(db, job.id, fencing, renew=True)
        db.commit()

        renew = research._heartbeat(db, job.id, fencing)

        def beat() -> None:
            if STOP.is_set():
                raise Shutdown()
            renew()

        notes: list[str] = []
        adapter = sources.ADAPTERS[imp.source]
        try:
            for batch in adapter(dict(imp.checkpoint or {}), beat, notes, settings.data_dir):
                for attempt in (1, 2):
                    try:
                        source_row_id = _source_row(db, imp, batch)
                        created, matched = persist_batch(db, source=imp.source, source_row_id=source_row_id,
                                                         import_id=imp.id, entities=batch.entities)
                        break
                    except IntegrityError:
                        # A concurrent intake took an identifier; the retry sees it as matched.
                        db.rollback()
                        if attempt == 2:
                            raise
                imp.checkpoint = batch.checkpoint
                imp.processed = (batch.checkpoint["row"] if batch.checkpoint.get("source") == sources.GLEIF_FILE_SOURCE
                                 else imp.processed + batch.seen)
                imp.created += created
                imp.matched += matched
                imp.skipped += sum(batch.skipped.values())
                imp.skip_reasons = dict(Counter(imp.skip_reasons or {}) + batch.skipped)
                imp.source_total = batch.total if batch.total is not None else imp.source_total
                imp.progress, imp.snapshot = batch.progress, batch.snapshot or imp.snapshot
                if notes:
                    imp.errors, notes[:] = _errors(imp.errors or [], notes), []
                if batch.exhausted:
                    imp.status, imp.exhausted, imp.finished_at = ImportStatus.completed, True, utcnow()
                    record_activity(db, "registry.import_completed",
                                    f"{sources.SOURCES[imp.source].label}: {imp.created} added, "
                                    f"{imp.matched} matched, {imp.skipped} skipped", None, import_id=str(imp.id))
                research._fence(db, job.id, fencing, renew=True,
                                finish=JobState.succeeded if batch.exhausted else None)
                db.commit()
            if imp.status != ImportStatus.completed:
                raise sources.SourceError("source stream ended without a final batch")
        except research.JobCancelled:
            db.rollback()
            imp = db.get(RegistryImport, import_id, populate_existing=True)
            if imp.status == ImportStatus.running:
                imp.status = ImportStatus.paused
            research._fence(db, job.id, fencing, finish=JobState.cancelled)
            db.commit()
            raise
        except research.LeaseLost:
            db.rollback()
            raise
        except Shutdown:
            # App stopping: requeue from the checkpoint without spending the retry budget.
            db.rollback()
            imp = db.get(RegistryImport, import_id, populate_existing=True)
            if imp.status == ImportStatus.running:
                imp.status = ImportStatus.queued
            db.commit()
            worker._release(db, job.id, fencing, state=JobState.queued, available_at=utcnow(),
                            payload={**(job.payload or {}), "retry_base": fencing})
            raise
        except Exception as exc:
            db.rollback()
            imp = db.get(RegistryImport, import_id, populate_existing=True)
            imp.errors = _errors(imp.errors or [], [*notes, f"{type(exc).__name__}: {exc}"])
            db.commit()
            raise


def mark_abandoned(db: Session, job: Job) -> None:
    import_id = (job.payload or {}).get("import_id")
    imp = db.get(RegistryImport, uuid.UUID(import_id)) if import_id else None
    if imp is not None and imp.status in (ImportStatus.queued, ImportStatus.running):
        imp.status, imp.finished_at = ImportStatus.failed, utcnow()
        imp.errors = _errors(imp.errors or [], [job.last_error or "retry budget exhausted"])
        db.commit()


def process_one(Session: sessionmaker, settings: Settings) -> bool:
    """Claim and run one import job; same retry/backoff policy as ``worker``."""
    with Session() as db:
        claimed = worker.claim_job(db, kinds=(JOB_KIND_REGISTRY_IMPORT,))
    if claimed is None:
        return False
    job, fencing = claimed
    with _OWNED_IMPORTS_LOCK:
        _OWNED_IMPORTS[job.id] = (fencing, Session, dict(job.payload or {}))
    try:
        if STOP.is_set():
            requeue_owned_jobs()
            return True
        run_import(Session, job, fencing, settings)
    except (research.JobCancelled, research.LeaseLost, Shutdown):
        pass
    except Exception as exc:
        used = worker.attempts_used(job, fencing)
        log.warning("registry import job %s failed (attempt %s): %s", job.id, used, exc)
        with Session() as db:
            if used >= worker.MAX_ATTEMPTS:
                if worker._release(db, job.id, fencing, state=JobState.failed, last_error=str(exc)[:2000]):
                    mark_abandoned(db, db.get(Job, job.id))
            else:
                worker._release(db, job.id, fencing, state=JobState.queued, last_error=str(exc)[:2000],
                                available_at=utcnow() + timedelta(seconds=worker._backoff_seconds(used)))
    else:
        with Session() as db:
            worker._release(db, job.id, fencing, state=JobState.succeeded)
    finally:
        with _OWNED_IMPORTS_LOCK:
            owned = _OWNED_IMPORTS.get(job.id)
            if owned is not None and owned[0] == fencing:
                del _OWNED_IMPORTS[job.id]
    return True


# ---------------------------------------------------------------------------
# Enrichment campaigns
# ---------------------------------------------------------------------------

def _campaign_prefix(campaign_id: uuid.UUID) -> str:
    return f"{JOB_KIND_ENRICH}:campaign:{campaign_id.hex}:"


def _job_counts(db: Session, campaign_id: uuid.UUID) -> dict[str, int]:
    rows = db.execute(select(Job.state, func.count()).where(
        Job.idempotency_key.like(_campaign_prefix(campaign_id) + "%")).group_by(Job.state))
    counts = {s.value: 0 for s in JobState}
    counts.update({JobState(state).value: n for state, n in rows})
    return counts


def feed_campaign(db: Session, campaign: EnrichmentCampaign) -> int:
    """Top the campaign's queued+running jobs up to ``max_in_flight``."""
    counts = _job_counts(db, campaign.id)
    in_flight = counts["queued"] + counts["running"]
    room = campaign.max_in_flight - in_flight
    if campaign.max_companies is not None:
        room = min(room, campaign.max_companies - campaign.enqueued)
        if campaign.enqueued >= campaign.max_companies and in_flight == 0:
            campaign.status, campaign.finished_at = CampaignStatus.completed, utcnow()
            db.commit()
            return 0
    if room <= 0:
        return 0
    busy = exists().where(Job.company_id == Company.id, Job.kind == JOB_KIND_ENRICH,
                          Job.state.in_([JobState.queued, JobState.running]))
    researched = exists().where(ResearchRun.company_id == Company.id,
                                ResearchRun.status == ResearchRunStatus.completed)
    q = select(Company.id, Company.created_at).where(Company.country == campaign.country, ~busy, ~researched)
    if campaign.last_created_at is not None:
        q = q.where(or_(Company.created_at > campaign.last_created_at,
                        and_(Company.created_at == campaign.last_created_at, Company.id > campaign.last_company_id)))
    picked = db.execute(q.order_by(Company.created_at, Company.id).limit(room)).all()
    prefix = _campaign_prefix(campaign.id)
    for company_id, _ in picked:
        db.add(Job(kind=JOB_KIND_ENRICH, company_id=company_id, idempotency_key=f"{prefix}{company_id.hex}",
                   payload={"company_id": str(company_id), "campaign_id": str(campaign.id)}))
    if picked:
        campaign.last_company_id, campaign.last_created_at = picked[-1][0], picked[-1][1]
        campaign.enqueued += len(picked)
    # Caught up for now; companies imported later are picked up by the keyset.
    campaign.exhausted = len(picked) < room
    db.commit()
    return len(picked)


def feed_enrichment(Session: sessionmaker) -> int:
    fed = 0
    with Session() as db:
        ids = db.scalars(select(EnrichmentCampaign.id).where(EnrichmentCampaign.status == CampaignStatus.running)).all()
    for campaign_id in ids:
        with Session() as db:
            campaign = db.get(EnrichmentCampaign, campaign_id)
            try:
                fed += feed_campaign(db, campaign)
            except IntegrityError as exc:
                db.rollback()
                campaign = db.get(EnrichmentCampaign, campaign_id)
                campaign.last_error = f"enqueue conflict: {exc.orig}"[:500]
                db.commit()
    return fed


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/registry", tags=["registry"], dependencies=[Depends(require_session)])


class SourceOut(BaseModel):
    id: str
    country: str
    label: str
    publisher: str
    url: str
    license: str
    identifier: dict[str, str]
    coverage: str
    limits: str


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(pattern="^(prh_bulk|zefix_lindas|gleif_de)$")


class ImportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    source: str
    country: str
    status: ImportStatus
    job_id: uuid.UUID | None
    job_state: JobState | None = None
    source_total: int | None
    processed: int
    created: int
    matched: int
    skipped: int
    skip_reasons: dict[str, int]
    progress: float | None
    snapshot: str | None
    checkpoint: dict[str, Any]
    errors: list[str]
    exhausted: bool
    coverage: str = ""
    limits: str = ""
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CampaignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country: str = Field(pattern="^(FI|CH|DE)$")
    max_in_flight: int = Field(2, ge=1, le=5)
    max_companies: int | None = Field(None, ge=1)


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    country: str
    status: CampaignStatus
    max_in_flight: int
    max_companies: int | None
    enqueued: int
    exhausted: bool
    jobs: dict[str, int] = {}
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


def _import_out(db: Session, imp: RegistryImport) -> ImportOut:
    info = sources.SOURCES[imp.source]
    job_state = db.scalar(select(Job.state).where(Job.id == imp.job_id)) if imp.job_id else None
    checkpoint = {k: v for k, v in (imp.checkpoint or {}).items() if k != "next"}  # cursor URLs are long
    return ImportOut.model_validate(imp).model_copy(update={
        "job_state": job_state, "coverage": info.coverage, "limits": info.limits, "checkpoint": checkpoint})


def _campaign_out(db: Session, c: EnrichmentCampaign) -> CampaignOut:
    return CampaignOut.model_validate(c).model_copy(update={"jobs": _job_counts(db, c.id)})


def _queue_job(db: Session, imp: RegistryImport) -> None:
    job = db.get(Job, imp.job_id) if imp.job_id else None
    now = utcnow()
    if job is None:
        job = Job(kind=JOB_KIND_REGISTRY_IMPORT, idempotency_key=f"{JOB_KIND_REGISTRY_IMPORT}:{imp.id}",
                  payload={"import_id": str(imp.id)})
        db.add(job)
        db.flush()
        imp.job_id = job.id
    elif job.state in (JobState.failed, JobState.cancelled, JobState.succeeded):
        # Same row, fresh retry budget; attempts keep counting so fencing stays unique.
        job.payload = {**(job.payload or {}), "retry_base": job.attempts}
        job.state, job.lease_until, job.available_at, job.last_error = JobState.queued, None, now, None
    imp.status, imp.finished_at = ImportStatus.queued, None


@router.get("/sources", response_model=list[SourceOut])
def list_sources():
    return [SourceOut(**{k: getattr(s, k) for k in ("id", "country", "label", "publisher", "url", "license",
                                                     "coverage", "limits")},
                      identifier={"jurisdiction": s.country, "scheme": s.scheme}) for s in sources.SOURCES.values()]


@router.post("/imports", response_model=ImportOut, status_code=202)
def start_import(body: ImportIn, response: Response, db: Session = Depends(get_db)):
    active = db.scalar(select(RegistryImport).where(
        RegistryImport.source == body.source,
        RegistryImport.status != ImportStatus.completed)
        .order_by(RegistryImport.created_at.desc()).limit(1))
    if active is not None:
        response.status_code = 200
        return _import_out(db, active)
    imp = RegistryImport(source=body.source, country=sources.SOURCES[body.source].country, checkpoint={},
                         skip_reasons={}, errors=[])
    db.add(imp)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        response.status_code = 200
        return _import_out(db, db.scalar(select(RegistryImport).where(
            RegistryImport.source == body.source, RegistryImport.status != ImportStatus.completed)))
    _queue_job(db, imp)
    record_activity(db, "registry.import_queued", f"{sources.SOURCES[body.source].label} import queued", None,
                    import_id=str(imp.id))
    db.commit()
    return _import_out(db, imp)


@router.get("/imports", response_model=list[ImportOut])
def list_imports(source: str | None = None, limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    q = select(RegistryImport).order_by(RegistryImport.created_at.desc()).limit(limit)
    if source:
        q = q.where(RegistryImport.source == source)
    return [_import_out(db, i) for i in db.scalars(q)]


@router.get("/imports/{import_id}", response_model=ImportOut)
def get_import(import_id: uuid.UUID, db: Session = Depends(get_db)):
    return _import_out(db, get_or_404(db, RegistryImport, import_id))


@router.post("/imports/{import_id}/pause", response_model=ImportOut)
def pause_import(import_id: uuid.UUID, db: Session = Depends(get_db)):
    imp = get_or_404(db, RegistryImport, import_id)
    if imp.status not in (ImportStatus.queued, ImportStatus.running):
        raise ApiError(409, "conflict", f"Import is {imp.status.value}; only queued or running imports can be paused")
    # The running handler sees the cancelled job at its next lease renewal and
    # stops after the batch in hand; the committed checkpoint stays.
    db.execute(update(Job).where(Job.id == imp.job_id, Job.state.in_([JobState.queued, JobState.running]))
               .values(state=JobState.cancelled, lease_until=None, updated_at=utcnow()))
    imp.status = ImportStatus.paused
    record_activity(db, "registry.import_paused", "Registry import paused", None, import_id=str(imp.id))
    db.commit()
    return _import_out(db, imp)


@router.post("/imports/{import_id}/resume", response_model=ImportOut)
def resume_import(import_id: uuid.UUID, db: Session = Depends(get_db)):
    imp = get_or_404(db, RegistryImport, import_id)
    if imp.status not in (ImportStatus.paused, ImportStatus.failed):
        raise ApiError(409, "conflict", f"Import is {imp.status.value}; only paused or failed imports can be resumed")
    job = db.get(Job, imp.job_id) if imp.job_id else None
    if job is not None and job.state == JobState.running:
        raise ApiError(409, "conflict", "The paused batch is still finishing; retry in a moment")
    _queue_job(db, imp)
    record_activity(db, "registry.import_resumed", "Registry import resumed", None, import_id=str(imp.id))
    db.commit()
    return _import_out(db, imp)


@router.post("/enrichment", response_model=CampaignOut, status_code=202)
def start_campaign(body: CampaignIn, response: Response, db: Session = Depends(get_db)):
    active = db.scalar(select(EnrichmentCampaign).where(
        EnrichmentCampaign.country == body.country,
        EnrichmentCampaign.status.in_([CampaignStatus.running, CampaignStatus.paused])).limit(1))
    if active is not None:
        response.status_code = 200
        return _campaign_out(db, active)
    campaign = EnrichmentCampaign(**body.model_dump())
    db.add(campaign)
    record_activity(db, "registry.enrichment_started", f"Background enrichment started for {body.country}", None,
                    **body.model_dump())
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        response.status_code = 200
        return _campaign_out(db, db.scalar(select(EnrichmentCampaign).where(
            EnrichmentCampaign.country == body.country, EnrichmentCampaign.status != CampaignStatus.completed)))
    return _campaign_out(db, campaign)


@router.get("/enrichment", response_model=list[CampaignOut])
def list_campaigns(db: Session = Depends(get_db)):
    return [_campaign_out(db, c) for c in db.scalars(
        select(EnrichmentCampaign).order_by(EnrichmentCampaign.created_at.desc()).limit(50))]


PAUSED_MARK = "campaign_paused"


@router.post("/enrichment/{campaign_id}/pause", response_model=CampaignOut)
def pause_campaign(campaign_id: uuid.UUID, db: Session = Depends(get_db)):
    campaign = get_or_404(db, EnrichmentCampaign, campaign_id)
    if campaign.status != CampaignStatus.running:
        raise ApiError(409, "conflict", f"Campaign is {campaign.status.value}; only running campaigns can be paused")
    campaign.status = CampaignStatus.paused
    db.execute(update(Job).where(Job.idempotency_key.like(_campaign_prefix(campaign.id) + "%"),
                                 Job.state == JobState.queued)
               .values(state=JobState.cancelled, last_error=PAUSED_MARK, updated_at=utcnow()))
    db.commit()
    return _campaign_out(db, campaign)


@router.post("/enrichment/{campaign_id}/resume", response_model=CampaignOut)
def resume_campaign(campaign_id: uuid.UUID, db: Session = Depends(get_db)):
    campaign = get_or_404(db, EnrichmentCampaign, campaign_id)
    if campaign.status != CampaignStatus.paused:
        raise ApiError(409, "conflict", f"Campaign is {campaign.status.value}; only paused campaigns can be resumed")
    campaign.status = CampaignStatus.running
    db.execute(update(Job).where(Job.idempotency_key.like(_campaign_prefix(campaign.id) + "%"),
                                 Job.state == JobState.cancelled, Job.last_error == PAUSED_MARK)
               .values(state=JobState.queued, last_error=None, available_at=utcnow(), updated_at=utcnow()))
    db.commit()
    return _campaign_out(db, campaign)


@router.get("/status")
def registry_status(request: Request, db: Session = Depends(get_db)):
    countries = []
    for country in COUNTRIES:
        latest = []
        for sid, info in sources.SOURCES.items():
            if info.country != country:
                continue
            imp = db.scalar(select(RegistryImport).where(RegistryImport.source == sid)
                            .order_by(RegistryImport.created_at.desc()).limit(1))
            if imp is not None:
                latest.append(_import_out(db, imp))
        campaign = db.scalar(select(EnrichmentCampaign).where(EnrichmentCampaign.country == country)
                             .order_by(EnrichmentCampaign.created_at.desc()).limit(1))
        enrich_jobs = {s.value: 0 for s in JobState}
        enrich_jobs.update({JobState(s).value: n for s, n in db.execute(
            select(Job.state, func.count()).join(Company, Company.id == Job.company_id)
            .where(Job.kind == JOB_KIND_ENRICH, Company.country == country).group_by(Job.state))})
        countries.append({
            "country": country,
            "companies": db.scalar(select(func.count()).select_from(Company).where(Company.country == country)),
            "identifiers": {s: n for s, n in db.execute(
                select(CompanyIdentifier.scheme, func.count()).where(CompanyIdentifier.jurisdiction == country)
                .group_by(CompanyIdentifier.scheme))},
            "sources": [sid for sid, info in sources.SOURCES.items() if info.country == country],
            "imports": latest,
            "enrichment": {
                "campaign": _campaign_out(db, campaign) if campaign else None,
                "researched_companies": db.scalar(
                    select(func.count(func.distinct(ResearchRun.company_id)))
                    .join(Company, Company.id == ResearchRun.company_id)
                    .where(Company.country == country, ResearchRun.status == ResearchRunStatus.completed)),
                "jobs": enrich_jobs,
            },
        })
    return {"countries": countries, "registry_worker": request.app.state.settings.worker_enabled}


# ---------------------------------------------------------------------------
# Standalone runner: python -m permetheus.registry
# ---------------------------------------------------------------------------

def run_loop(settings: Settings | None = None) -> None:
    """Import jobs plus the enrichment feeder, for when the app's background
    worker is disabled. Enrichment itself runs in ``permetheus.worker``."""
    from .db import init_db, make_engine, make_sessionmaker
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    init(engine)
    Session = make_sessionmaker(engine)

    def feeder() -> None:
        while True:
            try:
                feed_enrichment(Session)
            except Exception:
                log.exception("enrichment feeder failed")
            time.sleep(15)

    threading.Thread(target=feeder, daemon=True).start()
    while True:
        try:
            if process_one(Session, settings):
                continue
        except Exception:
            log.exception("registry import queue unavailable")
        time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_loop()
