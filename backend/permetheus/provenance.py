"""Sources, evidence spans, financial observations and seller-intent statements."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_session
from .db import get_db, get_or_404, record_activity
from .errors import ApiError
from .models import (
    Company, Evidence, FinancialMetric, FinancialObservation, FinancialScope, FinancialStatus,
    IntentStatement, ReviewStatus, SellerIntent, Source, SourceKind, SpeakerAuthority,
)

router = APIRouter(prefix="/api", tags=["provenance"], dependencies=[Depends(require_session)])

Strict = ConfigDict(extra="forbid")


class SourceIn(BaseModel):
    model_config = Strict
    kind: SourceKind
    url: str | None = Field(None, max_length=2048)
    title: str | None = Field(None, max_length=500)
    publisher: str | None = Field(None, max_length=300)
    fetched_at: datetime | None = None
    published_at: datetime | None = None
    content_hash: str | None = Field(None, pattern=r"^[a-f0-9]{64}$", description="SHA-256 hex")

    @field_validator("url")
    @classmethod
    def public_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        parts = urlsplit(v.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("url must be an absolute http(s) URL")
        if parts.username or parts.password:
            raise ValueError("url must not contain credentials")
        return v.strip()

    @model_validator(mode="after")
    def locatable(self):
        if not self.url and not self.title:
            raise ValueError("a source needs a url or a title")
        return self


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: SourceKind
    url: str | None
    title: str | None
    publisher: str | None
    fetched_at: datetime | None
    published_at: datetime | None
    content_hash: str | None
    created_at: datetime


class EvidenceIn(BaseModel):
    model_config = Strict
    source_id: uuid.UUID
    field: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$", description="Typed predicate, e.g. website or employees")
    value: Any = None
    excerpt: str = Field(min_length=1, max_length=10000, description="Exact supporting text span")
    locator: dict[str, Any] = Field(default_factory=dict, description="Page, offset or time span in the source")
    extraction_method: str = Field("manual", min_length=1, max_length=64)


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    field: str
    value: Any
    excerpt: str
    locator: dict[str, Any]
    extraction_method: str
    review_status: ReviewStatus
    source: SourceOut
    created_at: datetime


class FinancialIn(BaseModel):
    model_config = Strict
    source_id: uuid.UUID
    metric: FinancialMetric
    amount: Decimal | None = Field(None, max_digits=24, decimal_places=4, allow_inf_nan=False,
                                   description="Exact full amount in currency units; null when unknown")
    currency: str | None = Field(None, pattern=r"^[A-Z]{3}$")
    period_start: date
    period_end: date
    scope: FinancialScope
    status: FinancialStatus
    formula: str | None = Field(None, max_length=500, description="Required when status is derived")

    @model_validator(mode="after")
    def consistent(self):
        valueless = self.status in (FinancialStatus.not_disclosed, FinancialStatus.not_applicable)
        if valueless != (self.amount is None):
            raise ValueError("amount must be null exactly when status is not_disclosed or not_applicable")
        if self.metric == FinancialMetric.employees:
            if self.currency is not None:
                raise ValueError("employees has no currency")
            if self.amount is not None and (self.amount < 0 or self.amount != self.amount.to_integral_value()):
                raise ValueError("employees must be a non-negative whole number")
        elif self.amount is not None and self.currency is None:
            raise ValueError("currency is required with a monetary amount")
        if self.status == FinancialStatus.derived and not self.formula:
            raise ValueError("derived observations need a formula")
        if self.period_end < self.period_start:
            raise ValueError("period_end must not be before period_start")
        return self


class FinancialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    metric: FinancialMetric
    amount: Decimal | None
    currency: str | None
    period_start: date
    period_end: date
    scope: FinancialScope
    status: FinancialStatus
    formula: str | None
    review_status: ReviewStatus
    source: SourceOut
    created_at: datetime


class IntentIn(BaseModel):
    model_config = Strict
    speaker_name: str = Field(min_length=1, max_length=300)
    speaker_authority: SpeakerAuthority
    stance: SellerIntent
    statement: str = Field(min_length=1, max_length=10000, description="The exact words or message span")
    stated_at: datetime
    source_id: uuid.UUID | None = None
    confirmed: bool = Field(False, description="Advisor confirms this attributable statement")

    @model_validator(mode="after")
    def authority(self):
        if self.confirmed and self.speaker_authority == SpeakerAuthority.unverified:
            raise ValueError("only statements from an owner or authorized representative can be confirmed")
        return self


class IntentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    speaker_name: str
    speaker_authority: SpeakerAuthority
    stance: SellerIntent
    statement: str
    stated_at: datetime
    source_id: uuid.UUID | None
    confirmed: bool
    created_at: datetime


def require_source(db: Session, source_id: uuid.UUID) -> Source:
    source = db.get(Source, source_id)
    if source is None:
        raise ApiError(422, "unknown_source", "source_id does not reference an existing source")
    return source


@router.post("/sources", response_model=SourceOut, status_code=201)
def create_source(body: SourceIn, db: Session = Depends(get_db)):
    source = Source(**body.model_dump())
    db.add(source)
    db.commit()
    return source


@router.get("/sources/{source_id}", response_model=SourceOut)
def get_source(source_id: uuid.UUID, db: Session = Depends(get_db)):
    return get_or_404(db, Source, source_id)


@router.post("/companies/{company_id}/evidence", response_model=EvidenceOut, status_code=201)
def add_evidence(company_id: uuid.UUID, body: EvidenceIn, db: Session = Depends(get_db)):
    get_or_404(db, Company, company_id)
    require_source(db, body.source_id)
    item = Evidence(company_id=company_id, **body.model_dump())
    db.add(item)
    record_activity(db, "evidence.added", f"Evidence added for {body.field}", company_id, field=body.field)
    db.commit()
    db.refresh(item)
    return item


@router.post("/companies/{company_id}/financials", response_model=FinancialOut, status_code=201)
def add_financial(company_id: uuid.UUID, body: FinancialIn, db: Session = Depends(get_db)):
    get_or_404(db, Company, company_id)
    require_source(db, body.source_id)
    item = FinancialObservation(company_id=company_id, **body.model_dump())
    db.add(item)
    record_activity(db, "financial.added", f"{body.metric} for {body.period_end.year} recorded", company_id,
                    metric=body.metric, status=body.status)
    db.commit()
    db.refresh(item)
    return item


@router.post("/companies/{company_id}/intent-statements", response_model=IntentOut, status_code=201)
def add_intent(company_id: uuid.UUID, body: IntentIn, db: Session = Depends(get_db)):
    company = get_or_404(db, Company, company_id)
    if body.source_id:
        require_source(db, body.source_id)
    item = IntentStatement(company_id=company_id, **body.model_dump())
    db.add(item)
    db.flush()
    latest = db.scalar(
        select(IntentStatement).where(IntentStatement.company_id == company_id, IntentStatement.confirmed)
        .order_by(IntentStatement.stated_at.desc()).limit(1)
    )
    if latest is not None:
        company.seller_intent = latest.stance
    record_activity(db, "intent.recorded", "Seller intent statement recorded" + (" and confirmed" if body.confirmed else ""),
                    company_id, confirmed=body.confirmed)
    db.commit()
    return item
