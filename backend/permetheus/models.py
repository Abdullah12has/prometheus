import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CompanyStatus(StrEnum):
    provisional = "provisional"
    confirmed = "confirmed"


class SellerIntent(StrEnum):
    unknown = "unknown"
    interested = "interested"
    conditional = "conditional"
    not_now = "not_now"
    not_interested = "not_interested"


class SpeakerAuthority(StrEnum):
    owner = "owner"
    authorized_representative = "authorized_representative"
    unverified = "unverified"


class SourceKind(StrEnum):
    registry = "registry"
    website = "website"
    search_result = "search_result"
    document = "document"
    email = "email"
    call = "call"
    note = "note"
    owner_reported = "owner_reported"
    manual = "manual"


class ReviewStatus(StrEnum):
    proposed = "proposed"
    accepted = "accepted"
    rejected = "rejected"


class FinancialMetric(StrEnum):
    revenue = "revenue"
    ebitda = "ebitda"
    ebit = "ebit"
    net_income = "net_income"
    cash = "cash"
    debt = "debt"
    equity = "equity"
    employees = "employees"


class FinancialStatus(StrEnum):
    reported = "reported"
    derived = "derived"
    estimated = "estimated"
    not_disclosed = "not_disclosed"
    not_applicable = "not_applicable"


class FinancialScope(StrEnum):
    entity = "entity"
    consolidated = "consolidated"


class ContactRole(StrEnum):
    seller = "seller"
    buyer = "buyer"


class PersonRole(StrEnum):
    owner = "owner"
    founder = "founder"
    director = "director"
    executive = "executive"
    representative = "representative"
    employee = "employee"
    other = "other"


class Verification(StrEnum):
    unverified = "unverified"
    verified = "verified"


class JobState(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


def enum_col(cls: type[StrEnum]) -> Enum:
    return Enum(cls, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e])


JsonType = JSON().with_variant(JSONB(), "postgresql")
TZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: TZ, dict[str, Any]: JsonType}


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuthSession(IdMixin, Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(index=True)


class Company(IdMixin, Base):
    __tablename__ = "companies"
    name: Mapped[str] = mapped_column(String(300))
    name_normalized: Mapped[str] = mapped_column(String(300), index=True)
    industry: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(2))
    website: Mapped[str | None] = mapped_column(String(2048))
    # Domain is evidence, not identity: group companies can share one website.
    domain: Mapped[str | None] = mapped_column(String(253), index=True)
    registry_status: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[CompanyStatus] = mapped_column(enum_col(CompanyStatus), default=CompanyStatus.provisional)
    # Only changed by a confirmed statement from an owner or authorized representative.
    seller_intent: Mapped[SellerIntent] = mapped_column(enum_col(SellerIntent), default=SellerIntent.unknown)
    last_verified_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    identifiers: Mapped[list["CompanyIdentifier"]] = relationship(cascade="all, delete-orphan", lazy="selectin")
    contacts: Mapped[list["Contact"]] = relationship(cascade="all, delete-orphan")
    evidence: Mapped[list["Evidence"]] = relationship(cascade="all, delete-orphan")
    financials: Mapped[list["FinancialObservation"]] = relationship(cascade="all, delete-orphan")
    intent_statements: Mapped[list["IntentStatement"]] = relationship(cascade="all, delete-orphan")


class CompanyIdentifier(IdMixin, Base):
    __tablename__ = "company_identifiers"
    __table_args__ = (UniqueConstraint("jurisdiction", "scheme", "value"),)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    scheme: Mapped[str] = mapped_column(String(32))
    jurisdiction: Mapped[str] = mapped_column(String(2))
    value: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))


class Source(IdMixin, Base):
    __tablename__ = "sources"
    kind: Mapped[SourceKind] = mapped_column(enum_col(SourceKind))
    url: Mapped[str | None] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(String(500))
    publisher: Mapped[str | None] = mapped_column(String(300))
    fetched_at: Mapped[datetime | None]
    published_at: Mapped[datetime | None]
    content_hash: Mapped[str | None] = mapped_column(String(64))


class Evidence(IdMixin, Base):
    __tablename__ = "evidence"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    field: Mapped[str] = mapped_column(String(64))
    value: Mapped[Any] = mapped_column(JsonType, nullable=True)
    excerpt: Mapped[str] = mapped_column(Text)
    locator: Mapped[dict[str, Any]] = mapped_column(default=dict)
    extraction_method: Mapped[str] = mapped_column(String(64))
    review_status: Mapped[ReviewStatus] = mapped_column(enum_col(ReviewStatus), default=ReviewStatus.proposed)
    source: Mapped[Source] = relationship(lazy="joined")


class FinancialObservation(IdMixin, Base):
    __tablename__ = "financial_observations"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    metric: Mapped[FinancialMetric] = mapped_column(enum_col(FinancialMetric))
    # Null means unknown/not disclosed; never coerced to zero.
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    scope: Mapped[FinancialScope] = mapped_column(enum_col(FinancialScope))
    status: Mapped[FinancialStatus] = mapped_column(enum_col(FinancialStatus))
    formula: Mapped[str | None] = mapped_column(String(500))
    review_status: Mapped[ReviewStatus] = mapped_column(enum_col(ReviewStatus), default=ReviewStatus.proposed)
    source: Mapped[Source] = relationship(lazy="joined")


class IntentStatement(IdMixin, Base):
    __tablename__ = "intent_statements"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    speaker_name: Mapped[str] = mapped_column(String(300))
    speaker_authority: Mapped[SpeakerAuthority] = mapped_column(enum_col(SpeakerAuthority))
    stance: Mapped[SellerIntent] = mapped_column(enum_col(SellerIntent))
    statement: Mapped[str] = mapped_column(Text)
    stated_at: Mapped[datetime]
    confirmed: Mapped[bool] = mapped_column(default=False)


class Contact(IdMixin, Base):
    __tablename__ = "contacts"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(300))
    title: Mapped[str | None] = mapped_column(String(200))
    person_role: Mapped[PersonRole] = mapped_column(enum_col(PersonRole))
    contact_role: Mapped[ContactRole] = mapped_column(enum_col(ContactRole))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(20))
    verification: Mapped[Verification] = mapped_column(enum_col(Verification), default=Verification.unverified)


class Job(IdMixin, Base):
    __tablename__ = "jobs"
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    state: Mapped[JobState] = mapped_column(enum_col(JobState), default=JobState.queued, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    available_at: Mapped[datetime] = mapped_column(default=utcnow)
    lease_until: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Activity(IdMixin, Base):
    __tablename__ = "activities"
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(String(500))
    actor: Mapped[str] = mapped_column(String(64), default="operator")
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
