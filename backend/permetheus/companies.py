"""Company intake with identity resolution, plus list/detail/update/delete."""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import require_session
from .contacts import ContactOut
from .db import get_db, get_or_404, record_activity
from .errors import ApiError
from .identity import normalize_business_id, normalize_country, normalize_name, normalize_website
from .models import (
    Activity, Company, CompanyIdentifier, CompanyStatus, Job, JobState, SellerIntent, utcnow,
)
from .provenance import EvidenceOut, FinancialOut, IntentOut
from .workspace import ActivityOut, JobOut

router = APIRouter(prefix="/api/companies", tags=["companies"], dependencies=[Depends(require_session)])

BUSINESS_ID = "business_id"
MAX_CANDIDATES = 10


class CompanyFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("country", check_fields=False)
    @classmethod
    def _country(cls, v: str | None) -> str | None:
        return normalize_country(v) if v else None

    @field_validator("website", check_fields=False)
    @classmethod
    def _website(cls, v: str | None) -> str | None:
        return normalize_website(v)[0] if v else None


class CompanyIn(CompanyFields):
    industry: str | None = Field(None, max_length=300)
    description: str | None = Field(None, max_length=20000)
    name: str | None = Field(None, min_length=1, max_length=300)
    website: str | None = Field(None, max_length=2048)
    country: str | None = Field(None, description="ISO 3166-1 alpha-2; required with business_id")
    business_id: str | None = Field(None, max_length=64)
    registry_status: str | None = Field(None, max_length=64)
    allow_new: bool = Field(False, description="Create even when name/website candidates exist")

    @model_validator(mode="after")
    def identity(self):
        if not (self.name or self.website or self.business_id):
            raise ValueError("provide at least one of name, website or business_id")
        if self.business_id:
            if not self.country:
                raise ValueError("country is required with business_id")
            self.business_id = normalize_business_id(self.country, self.business_id)
        return self


class CompanyPatch(CompanyFields):
    confirmation_basis: str | None = Field(None, min_length=5, max_length=2000)
    industry: str | None = Field(None, max_length=300)
    description: str | None = Field(None, max_length=20000)
    # name/status accept omission but reject explicit null.
    name: str = Field(None, min_length=1, max_length=300)
    website: str | None = Field(None, max_length=2048)
    country: str | None = None
    registry_status: str | None = Field(None, max_length=64)
    status: CompanyStatus = None


class IdentifierIn(CompanyFields):
    country: str
    business_id: str = Field(max_length=64)

    @model_validator(mode="after")
    def normalized(self):
        self.business_id = normalize_business_id(self.country, self.business_id)
        return self


class IdentifierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    scheme: str
    jurisdiction: str
    value: str


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    industry: str | None
    description: str | None
    country: str | None
    website: str | None
    domain: str | None
    registry_status: str | None
    status: CompanyStatus
    seller_intent: SellerIntent
    identifiers: list[IdentifierOut]
    last_verified_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CompanyDetail(CompanyOut):
    contacts: list[ContactOut]
    evidence: list[EvidenceOut]
    financials: list[FinancialOut]
    intent_statements: list[IntentOut]
    jobs: list[JobOut]
    activities: list[ActivityOut]


class CompanyPage(BaseModel):
    items: list[CompanyOut]
    total: int


class IntakeOut(BaseModel):
    resolution: Literal["created", "matched_business_id", "matched_website"]
    company: CompanyOut
    possible_duplicates: list[CompanyOut] = Field(
        default_factory=list, description="Name/website candidates that may be the same entity; review before outreach"
    )


def find_by_business_id(db: Session, country: str, value: str) -> Company | None:
    return db.scalar(
        select(Company).join(CompanyIdentifier).where(
            CompanyIdentifier.scheme == BUSINESS_ID,
            CompanyIdentifier.jurisdiction == country,
            CompanyIdentifier.value == value,
        )
    )


def find_candidates(db: Session, name: str | None, domain: str | None, country: str | None) -> tuple[str, list[Company]]:
    if domain:
        found = db.scalars(select(Company).where(Company.domain == domain).limit(MAX_CANDIDATES)).all()
        if found:
            return "website", list(found)
    if name:
        q = select(Company).where(Company.name_normalized == normalize_name(name))
        if country:
            # Same name in another jurisdiction is a different company.
            q = q.where(or_(Company.country == country, Company.country.is_(None)))
        return "name", list(db.scalars(q.limit(MAX_CANDIDATES)).all())
    return "none", []


@router.post("", response_model=IntakeOut, status_code=201, responses={
    200: {"model": IntakeOut, "description": "Existing company matched"},
    409: {"description": "ambiguous_company: candidates in error.details.candidates"},
})
def create_company(body: CompanyIn, response: Response, db: Session = Depends(get_db)):
    domain = normalize_website(body.website)[1] if body.website else None

    if body.business_id:
        # A registry identifier is authoritative: match it, never create a second record.
        existing = find_by_business_id(db, body.country, body.business_id)
        if existing:
            response.status_code = 200
            return IntakeOut(resolution="matched_business_id", company=existing)
        _, candidates = find_candidates(db, body.name, domain, body.country)
        candidates = [c for c in candidates if not any(i.jurisdiction == body.country for i in c.identifiers)]
    else:
        kind, candidates = find_candidates(db, body.name, domain, body.country)
        if candidates and not body.allow_new:
            if kind == "website" and len(candidates) == 1:
                response.status_code = 200
                return IntakeOut(resolution="matched_website", company=candidates[0])
            raise ApiError(409, "ambiguous_company", "Existing companies may match; choose one or resend with allow_new",
                           {"candidates": [CompanyOut.model_validate(c).model_dump(mode="json") for c in candidates]})

    name = body.name or domain or body.business_id
    company = Company(name=name, name_normalized=normalize_name(name), country=body.country,
                      website=body.website, domain=domain, registry_status=body.registry_status,
                      industry=body.industry, description=body.description)
    db.add(company)
    db.flush()
    if body.business_id:
        db.add(CompanyIdentifier(company_id=company.id, scheme=BUSINESS_ID, jurisdiction=body.country, value=body.business_id))
    # ponytail: jobs are persisted for the future worker; nothing executes them yet.
    db.add(Job(kind="company.enrich", company_id=company.id, idempotency_key=f"company.enrich:{company.id}",
               payload={"company_id": str(company.id)}))
    record_activity(db, "company.created", f"{name} added", company.id)
    try:
        db.commit()
    except IntegrityError:
        # Concurrent intake of the same business ID converges on the winner.
        db.rollback()
        existing = body.business_id and find_by_business_id(db, body.country, body.business_id)
        if not existing:
            raise
        response.status_code = 200
        return IntakeOut(resolution="matched_business_id", company=existing)
    db.refresh(company)
    return IntakeOut(resolution="created", company=company, possible_duplicates=candidates)


@router.get("", response_model=CompanyPage)
def list_companies(q: str | None = Query(None, max_length=200), status: CompanyStatus | None = None,
                   country: str | None = Query(None, pattern="^[A-Za-z]{2}$"),
                   limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    query = select(Company)
    if q and q.strip():
        term = q.strip()
        query = query.where(or_(
            Company.name_normalized.contains(normalize_name(term), autoescape=True),
            Company.domain.contains(term.lower(), autoescape=True),
            Company.id.in_(select(CompanyIdentifier.company_id).where(CompanyIdentifier.value == term.upper())),
        ))
    if status:
        query = query.where(Company.status == status)
    if country:
        query = query.where(Company.country == country.upper())
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(Company.created_at.desc(), Company.id.desc()).limit(limit).offset(offset)).all()
    return CompanyPage(items=items, total=total)


@router.get("/{company_id}", response_model=CompanyDetail)
def get_company(company_id: uuid.UUID, db: Session = Depends(get_db)):
    company = get_or_404(db, Company, company_id)
    jobs = db.scalars(select(Job).where(Job.company_id == company_id).order_by(Job.created_at.desc()).limit(20)).all()
    activities = db.scalars(
        select(Activity).where(Activity.company_id == company_id).order_by(Activity.created_at.desc()).limit(50)
    ).all()
    return CompanyDetail.model_validate(
        {**CompanyOut.model_validate(company).model_dump(), "contacts": company.contacts, "evidence": company.evidence,
         "financials": company.financials, "intent_statements": company.intent_statements,
         "jobs": jobs, "activities": activities}
    )


@router.patch("/{company_id}", response_model=CompanyOut)
def update_company(company_id: uuid.UUID, body: CompanyPatch, db: Session = Depends(get_db)):
    company = get_or_404(db, Company, company_id)
    changes = body.model_dump(exclude_unset=True)
    basis = changes.pop("confirmation_basis", None)
    if basis and changes.get("status") == CompanyStatus.confirmed:
        record_activity(db, "company.identity_confirmed", "Company identity confirmed", company.id, basis=basis)
        company.last_verified_at = utcnow()
    for key, value in changes.items():
        setattr(company, key, value)
    if "name" in changes:
        company.name_normalized = normalize_name(company.name)
    if "website" in changes:
        company.domain = normalize_website(company.website)[1] if company.website else None
    if changes:
        record_activity(db, "company.updated", "Company details updated", company.id, fields=sorted(changes))
    db.commit()
    db.refresh(company)
    return company


@router.post("/{company_id}/identifiers", response_model=IdentifierOut, status_code=201)
def add_identifier(company_id: uuid.UUID, body: IdentifierIn, response: Response, db: Session = Depends(get_db)):
    company = get_or_404(db, Company, company_id)
    owner = find_by_business_id(db, body.country, body.business_id)
    if owner and owner.id != company.id:
        raise ApiError(409, "identifier_conflict", "This business ID belongs to another company; merge is required",
                       {"company_id": str(owner.id)})
    if owner:
        response.status_code = 200
        return next(i for i in owner.identifiers if i.jurisdiction == body.country and i.value == body.business_id)
    ident = CompanyIdentifier(company_id=company.id, scheme=BUSINESS_ID, jurisdiction=body.country, value=body.business_id)
    db.add(ident)
    record_activity(db, "identifier.added", f"Business ID {body.business_id} added", company.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "identifier_conflict", "This business ID was just assigned to another company")
    return ident


@router.delete("/{company_id}")
def delete_company(company_id: uuid.UUID, db: Session = Depends(get_db)):
    company = get_or_404(db, Company, company_id)
    db.execute(update(Job).where(Job.company_id == company_id, Job.state.in_([JobState.queued, JobState.running]))
               .values(state=JobState.cancelled, updated_at=utcnow()))
    db.execute(update(Job).where(Job.company_id == company_id).values(company_id=None))
    db.execute(delete(Activity).where(Activity.company_id == company_id))
    db.delete(company)
    # Content-free tombstone: the deleted company's details are not retained.
    record_activity(db, "company.deleted", "Company record deleted", None, deleted_company_id=str(company_id))
    db.commit()
    return {"id": company_id, "deleted": True}
