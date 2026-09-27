"""Deals domain: owner preferences, buyer mandates, futures scenarios, matching, outcomes and deal history.

Every constraint evaluates to pass, fail or unknown. A hard fail excludes; an unresolved hard condition keeps a
result at research_needed no matter how well soft preferences score. Nothing here sends email or calls anyone:
an authorized brief becomes at most an unapproved mail draft, re-checked at send time by validate_deal_disclosure.
"""

import csv
import hashlib
import io
import json
import uuid
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator,
)
from sqlalchemy import Date, ForeignKey, String, Text, UniqueConstraint, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from .auth import require_session
from .db import get_db, get_or_404, record_activity
from .errors import ApiError
from .mail import OutreachDraft
from .models import (
    Base, Company, Contact, ContactRole, Evidence, FinancialMetric, FinancialObservation, FinancialStatus, IdMixin,
    JsonType, ReviewStatus, Source, SpeakerAuthority, Verification, enum_col, utcnow,
)

router = APIRouter(prefix="/api", tags=["deals"], dependencies=[Depends(require_session)])

Strict = ConfigDict(extra="forbid")
POLICY_VERSION = "constraints-v2"  # v2: only accepted evidence decides; unverified buyer identity blocks
MAX_CSV_BYTES, MAX_CSV_ROWS, MAX_ROW_ERRORS = 1_000_000, 2000, 50

Country = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
Ccy = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Amount = Annotated[Decimal, Field(ge=0, max_digits=24, decimal_places=4, allow_inf_nan=False)]
Signed = Annotated[Decimal, Field(max_digits=24, decimal_places=4, allow_inf_nan=False)]
Pct = Annotated[Decimal, Field(ge=0, le=100, max_digits=7, decimal_places=4, allow_inf_nan=False)]


class Strength(StrEnum):
    hard = "hard"        # non-negotiable
    soft = "soft"        # preference; only these are scored
    unknown = "unknown"  # owner has not said whether it is negotiable


class ConditionKind(StrEnum):
    retained_ownership = "retained_ownership"
    operating_control = "operating_control"
    site_retention = "site_retention"
    team_retention = "team_retention"
    brand_retention = "brand_retention"
    timeline = "timeline"
    structure = "structure"
    currency = "currency"
    minimum_proceeds = "minimum_proceeds"


class Structure(StrEnum):
    minority_investment = "minority_investment"
    majority_sale = "majority_sale"
    full_sale = "full_sale"


class MandateStatus(StrEnum):
    active = "active"
    paused = "paused"
    closed = "closed"


class EvidenceLevel(StrEnum):
    public_strategy = "public_strategy"  # e.g. a website says they acquire; weaker than a brief
    buyer_confirmed = "buyer_confirmed"  # current brief confirmed by an accountable buyer contact


class Financing(StrEnum):
    unknown = "unknown"
    buyer_stated = "buyer_stated"
    evidenced = "evidenced"


class MatchStatus(StrEnum):
    compatible = "compatible"            # potentially compatible; never a confirmed match or offer
    research_needed = "research_needed"  # no hard fail, but something blocking is unresolved
    excluded = "excluded"                # at least one hard condition fails


class DealStatus(StrEnum):
    announced = "announced"
    completed = "completed"
    withdrawn = "withdrawn"


class Milestone(StrEnum):
    reached = "reached"
    replied = "replied"
    qualified = "qualified"
    meeting = "meeting"
    nda = "nda"
    engagement_proposed = "engagement_proposed"
    mandate_signed = "mandate_signed"
    diligence = "diligence"
    offer = "offer"
    closed = "closed"
    lost = "lost"


class BuyerResponse(StrEnum):
    request_for_details = "request_for_details"
    interested = "interested"
    rejected = "rejected"
    outside_mandate = "outside_mandate"
    timing_budget_changed = "timing_budget_changed"
    already_known = "already_known"
    opt_out = "opt_out"
    unclear = "unclear"


class ReasonCategory(StrEnum):
    outside_mandate = "outside_mandate"
    valuation_gap = "valuation_gap"
    structure_mismatch = "structure_mismatch"
    timing = "timing"
    financing = "financing"
    owner_withdrew = "owner_withdrew"
    buyer_withdrew = "buyer_withdrew"
    competing_process = "competing_process"
    other = "other"


class ReasonBasis(StrEnum):
    stated = "stated"        # the party said it
    confirmed = "confirmed"  # later confirmed by the party


class DisclosureField(StrEnum):
    company_identity = "company_identity"
    country = "country"
    industry = "industry"
    revenue = "revenue"
    ebitda = "ebitda"
    employees = "employees"
    owner_conditions = "owner_conditions"


# ---------------------------------------------------------------- tables


class BuyerMandate(IdMixin, Base):
    __tablename__ = "buyer_mandates"
    buyer_name: Mapped[str] = mapped_column(String(300), index=True)
    status: Mapped[MandateStatus] = mapped_column(enum_col(MandateStatus), index=True)
    expires_at: Mapped[datetime] = mapped_column(index=True)
    version: Mapped[int] = mapped_column(default=1)
    data: Mapped[dict[str, Any]]  # validated MandateIn; history in buyer_mandate_versions
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class MandateVersion(IdMixin, Base):
    __tablename__ = "buyer_mandate_versions"
    __table_args__ = (UniqueConstraint("mandate_id", "version"),)
    mandate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyer_mandates.id", ondelete="CASCADE"), index=True)
    version: Mapped[int]
    data: Mapped[dict[str, Any]]
    changed_fields: Mapped[list[Any]] = mapped_column(JsonType, default=list)


class PreferenceProfile(IdMixin, Base):
    """One immutable version of an owner's conditions; only the confirmation fields are ever written later."""
    __tablename__ = "owner_preference_profiles"
    __table_args__ = (UniqueConstraint("company_id", "version"),)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    version: Mapped[int]
    conditions: Mapped[list[Any]] = mapped_column(JsonType)
    conditions_hash: Mapped[str] = mapped_column(String(64))
    stated_by: Mapped[str | None] = mapped_column(String(300))
    note: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    confirmed_at: Mapped[datetime | None]
    confirmed_by: Mapped[str | None] = mapped_column(String(300))
    confirmed_authority: Mapped[SpeakerAuthority | None] = mapped_column(enum_col(SpeakerAuthority))
    confirmation_statement: Mapped[str | None] = mapped_column(Text)
    confirmation_source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))


class Scenario(IdMixin, Base):
    __tablename__ = "futures_scenarios"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("owner_preference_profiles.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200))
    snapshot: Mapped[dict[str, Any]]
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    results: Mapped[list[Any]] = mapped_column(JsonType)


class MatchRun(IdMixin, Base):
    __tablename__ = "match_runs"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("owner_preference_profiles.id", ondelete="SET NULL"))
    policy_version: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]]
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    counts: Mapped[dict[str, Any]] = mapped_column(default=dict)


class MatchResult(IdMixin, Base):
    __tablename__ = "match_results"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("match_runs.id", ondelete="CASCADE"), index=True)
    mandate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyer_mandates.id", ondelete="CASCADE"), index=True)
    mandate_version: Mapped[int]
    status: Mapped[MatchStatus] = mapped_column(enum_col(MatchStatus))
    fit_score: Mapped[float | None]
    coverage: Mapped[float | None]
    checks: Mapped[list[Any]] = mapped_column(JsonType)
    explanation: Mapped[dict[str, Any]]


class Opportunity(IdMixin, Base):
    __tablename__ = "deal_opportunities"
    __table_args__ = (UniqueConstraint("company_id", "mandate_id"),)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    mandate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyer_mandates.id", ondelete="CASCADE"), index=True)
    match_result_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("match_results.id", ondelete="SET NULL"))
    status: Mapped[MatchStatus] = mapped_column(enum_col(MatchStatus), index=True)
    latest_milestone: Mapped[Milestone | None] = mapped_column(enum_col(Milestone))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class OutcomeEvent(IdMixin, Base):
    __tablename__ = "opportunity_outcomes"
    opportunity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deal_opportunities.id", ondelete="CASCADE"), index=True)
    milestone: Mapped[Milestone] = mapped_column(enum_col(Milestone))
    response: Mapped[BuyerResponse | None] = mapped_column(enum_col(BuyerResponse))
    reason_category: Mapped[ReasonCategory | None] = mapped_column(enum_col(ReasonCategory))
    reason_basis: Mapped[ReasonBasis | None] = mapped_column(enum_col(ReasonBasis))
    evidence_excerpt: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    note: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime]


class HistoricalDeal(IdMixin, Base):
    __tablename__ = "historical_deals"
    status: Mapped[DealStatus] = mapped_column(enum_col(DealStatus), index=True)
    buyer_name: Mapped[str] = mapped_column(String(300), index=True)
    buyer_company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    target_name: Mapped[str] = mapped_column(String(300))
    source_url: Mapped[str] = mapped_column(String(2048))
    announced_on: Mapped[date | None] = mapped_column(Date)
    # When this information became available: replay never sees a deal before its as_of.
    as_of: Mapped[datetime] = mapped_column(index=True)
    data: Mapped[dict[str, Any]]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ProposalDraft(IdMixin, Base):
    __tablename__ = "proposal_drafts"
    opportunity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deal_opportunities.id", ondelete="CASCADE"), index=True)
    mandate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyer_mandates.id", ondelete="CASCADE"))
    authorization: Mapped[dict[str, Any]]
    payload: Mapped[dict[str, Any]]
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="draft")


# ---------------------------------------------------------------- schemas


def utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo; everything is stored and compared as UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def not_future(dt: datetime | None, field: str) -> None:
    if dt is not None and utc(dt) > utcnow():
        raise ValueError(f"{field} must not be in the future")


class MandateCriteria(BaseModel):
    model_config = Strict
    countries: list[Country] = Field(default_factory=list, max_length=100, description="Empty = no geography criterion")
    industries: list[Label] = Field(default_factory=list, max_length=100)
    financial_currency: Ccy | None = Field(None, description="Currency of revenue/EBITDA ranges; no FX conversion")
    revenue_min: Amount | None = None
    revenue_max: Amount | None = None
    ebitda_min: Signed | None = None
    ebitda_max: Signed | None = None
    employees_min: int | None = Field(None, ge=0)
    employees_max: int | None = Field(None, ge=0)
    structures: list[Structure] = Field(default_factory=list, description="Accepted structures; empty = not stated")
    max_rollover_pct: Pct | None = Field(None, description="Max equity the seller may retain; 0 = buyer requires 100%")
    control_retention: bool | None = Field(None, description="Buyer accepts owner keeping operating control")
    site_commitment: bool | None = None
    team_commitment: bool | None = None
    brand_commitment: bool | None = None
    close_within_months: int | None = Field(None, ge=1, le=240)
    consideration_currency: Ccy | None = None
    max_consideration: Amount | None = None

    @model_validator(mode="after")
    def consistent(self):
        for m in ("revenue", "ebitda", "employees"):
            lo, hi = getattr(self, f"{m}_min"), getattr(self, f"{m}_max")
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(f"{m}_min exceeds {m}_max")
        money = ("revenue_min", "revenue_max", "ebitda_min", "ebitda_max")
        if any(getattr(self, f) is not None for f in money) and not self.financial_currency:
            raise ValueError("financial_currency is required with revenue/EBITDA ranges")
        if self.max_consideration is not None and not self.consideration_currency:
            raise ValueError("consideration_currency is required with max_consideration")
        return self


class MandateIn(BaseModel):
    model_config = Strict
    buyer_name: Name
    buyer_company_id: uuid.UUID | None = None
    contact_name: Name | None = Field(None, description="Accountable buyer contact")
    advisor: Name | None = None
    status: MandateStatus = MandateStatus.active
    criteria: MandateCriteria
    evidence_level: EvidenceLevel
    source_id: uuid.UUID | None = Field(None, description="Required for public_strategy")
    identity_verified: bool = Field(False, description="Required (with source_id) for a compatible result")
    confirmed_by: Name | None = Field(None, description="Required for buyer_confirmed")
    last_confirmed_at: AwareDatetime | None = None
    financing_status: Financing = Field(Financing.unknown, description="Never inferred; evidenced needs a source")
    financing_source_id: uuid.UUID | None = None
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def verification(self):
        if self.evidence_level == EvidenceLevel.buyer_confirmed:
            if not (self.confirmed_by and self.last_confirmed_at):
                raise ValueError("buyer_confirmed mandates need confirmed_by and last_confirmed_at")
        else:
            if not self.source_id:
                raise ValueError("public_strategy mandates need a source_id")
            if self.confirmed_by or self.last_confirmed_at:
                raise ValueError("public_strategy mandates cannot carry a buyer confirmation")
        if self.identity_verified and not self.source_id:
            raise ValueError("identity_verified needs the source_id that evidences the buyer's identity")
        if (self.financing_status == Financing.evidenced) != (self.financing_source_id is not None):
            raise ValueError("financing_source_id is required exactly when financing_status is evidenced")
        not_future(self.last_confirmed_at, "last_confirmed_at")
        return self


class MandateOut(MandateIn):
    id: uuid.UUID
    version: int
    active: bool
    created_at: datetime
    updated_at: datetime


class MandateVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    version: int
    data: dict[str, Any]
    changed_fields: list[str]
    created_at: datetime


class MandateDetail(MandateOut):
    versions: list[MandateVersionOut]


PARAMS: dict[str, set[str]] = {
    "retained_ownership": {"min_pct"}, "timeline": {"within_months"}, "structure": {"structures"},
    "currency": {"currencies"}, "minimum_proceeds": {"amount", "currency"},
}
OPTIONAL_PARAMS = {"site_retention": {"sites"}}
PARAM_NAMES = ("min_pct", "within_months", "structures", "currencies", "amount", "currency", "sites")


class Condition(BaseModel):
    model_config = Strict
    kind: ConditionKind
    strength: Strength
    weight: int = Field(1, ge=1, le=5, description="Relative weight; used only for soft conditions")
    min_pct: Pct | None = Field(None, description="retained_ownership: minimum equity the owner keeps")
    within_months: int | None = Field(None, ge=1, le=240, description="timeline: close within N months")
    structures: list[Structure] | None = Field(None, min_length=1)
    currencies: list[Ccy] | None = Field(None, min_length=1)
    amount: Amount | None = Field(None, description="minimum_proceeds amount")
    currency: Ccy | None = None
    sites: list[Label] | None = Field(None, min_length=1, max_length=50)
    note: str | None = Field(None, max_length=1000)

    @model_validator(mode="after")
    def shape(self):
        need = PARAMS.get(self.kind, set())
        allowed = need | OPTIONAL_PARAMS.get(self.kind, set())
        for name in PARAM_NAMES:
            present = getattr(self, name) is not None
            if name in need and not present:
                raise ValueError(f"{self.kind} requires {name}")
            if present and name not in allowed:
                raise ValueError(f"{name} does not apply to {self.kind}")
        return self


def unique_kinds(v: list[Condition]) -> list[Condition]:
    kinds = [c.kind for c in v]
    if len(kinds) != len(set(kinds)):
        raise ValueError("each condition kind may appear once")
    return v


class PreferenceIn(BaseModel):
    model_config = Strict
    conditions: list[Condition] = Field(max_length=len(ConditionKind))
    stated_by: Name | None = Field(None, description="Who expressed these conditions")
    note: str | None = Field(None, max_length=5000)
    source_id: uuid.UUID | None = None

    @field_validator("conditions")
    @classmethod
    def check_kinds(cls, v: list[Condition]) -> list[Condition]:
        return unique_kinds(v)


class ConfirmIn(BaseModel):
    model_config = Strict
    conditions_hash: str = Field(pattern=r"^[a-f0-9]{64}$", description="Hash of the exact version being confirmed")
    speaker_name: Name
    speaker_authority: SpeakerAuthority
    statement: str = Field(min_length=1, max_length=10000)
    source_id: uuid.UUID | None = None
    confirmed_at: AwareDatetime

    @model_validator(mode="after")
    def authority(self):
        if self.speaker_authority == SpeakerAuthority.unverified:
            raise ValueError("only an owner or authorized representative can confirm preferences")
        not_future(self.confirmed_at, "confirmed_at")
        return self


class Confirmation(BaseModel):
    confirmed_at: datetime
    confirmed_by: str
    authority: SpeakerAuthority
    statement: str
    source_id: uuid.UUID | None


class PreferenceOut(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID
    version: int
    conditions: list[Condition]
    conditions_hash: str
    stated_by: str | None
    note: str | None
    source_id: uuid.UUID | None
    confirmation: Confirmation | None
    created_at: datetime


class PreferenceList(BaseModel):
    company_id: uuid.UUID
    effective_profile_id: uuid.UUID | None = Field(description="Highest confirmed version; used by match runs")
    items: list[PreferenceOut]


class MoneyFact(BaseModel):
    model_config = Strict
    amount: Signed
    currency: Ccy


class FactOverrides(BaseModel):
    model_config = Strict
    country: Country | None = None
    industry: Label | None = None
    revenue: MoneyFact | None = None
    ebitda: MoneyFact | None = None
    employees: int | None = Field(None, ge=0)


class ScenarioIn(BaseModel):
    model_config = Strict
    company_id: uuid.UUID
    name: Label
    profile_id: uuid.UUID | None = Field(None, description="Defaults to the latest version, confirmed or not")
    conditions: list[Condition] = Field(default_factory=list, max_length=len(ConditionKind),
                                        description="Replace or add conditions by kind")
    remove_kinds: list[ConditionKind] = Field(default_factory=list)
    facts: FactOverrides | None = Field(None, description="Explicitly hypothetical company facts")
    mandate_ids: list[uuid.UUID] | None = Field(None, max_length=200, description="Default: all active mandates")

    @field_validator("conditions")
    @classmethod
    def check_kinds(cls, v: list[Condition]) -> list[Condition]:
        return unique_kinds(v)

    @model_validator(mode="after")
    def disjoint(self):
        if {c.kind for c in self.conditions} & set(self.remove_kinds):
            raise ValueError("a kind cannot be both overridden and removed")
        return self


class ScenarioOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    company_id: uuid.UUID
    profile_id: uuid.UUID | None
    name: str
    snapshot: dict[str, Any]
    snapshot_hash: str
    results: list[dict[str, Any]]
    created_at: datetime


class MatchRunIn(BaseModel):
    model_config = Strict
    company_id: uuid.UUID
    profile_id: uuid.UUID | None = Field(None, description="Default: highest confirmed version")
    mandate_ids: list[uuid.UUID] | None = Field(None, max_length=200, description="Default: all active mandates")


class MatchResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    mandate_id: uuid.UUID
    mandate_version: int
    status: MatchStatus
    fit_score: float | None
    coverage: float | None
    checks: list[dict[str, Any]]
    explanation: dict[str, Any]


class MatchRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    company_id: uuid.UUID
    profile_id: uuid.UUID | None
    policy_version: str
    snapshot_hash: str
    counts: dict[str, int]
    created_at: datetime


class MatchRunDetail(MatchRunOut):
    snapshot: dict[str, Any]
    results: list[MatchResultOut]


class OpportunityOut(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID
    mandate_id: uuid.UUID
    buyer_name: str
    status: MatchStatus
    latest_milestone: Milestone | None
    match_result_id: uuid.UUID | None
    fit_score: float | None
    coverage: float | None
    summary: str | None
    questions: list[str]
    stale: bool = Field(description="Mandate, company facts or effective owner profile changed since the result")
    stale_reasons: list[str]
    created_at: datetime
    updated_at: datetime


class OutcomeIn(BaseModel):
    model_config = Strict
    milestone: Milestone
    occurred_at: AwareDatetime
    response: BuyerResponse | None = None
    reason_category: ReasonCategory | None = Field(None, description="Only with milestone=lost, stated or confirmed")
    reason_basis: ReasonBasis | None = None
    evidence_excerpt: str | None = Field(None, max_length=10000)
    source_id: uuid.UUID | None = None
    note: str | None = Field(None, max_length=5000)

    @model_validator(mode="after")
    def supported(self):
        not_future(self.occurred_at, "occurred_at")
        if (self.reason_category is None) != (self.reason_basis is None):
            raise ValueError("reason_category and reason_basis go together")
        if self.reason_category is not None:
            if self.milestone != Milestone.lost:
                raise ValueError("a reason is recorded only for a lost milestone")
            if not (self.evidence_excerpt or self.source_id):
                raise ValueError("a reason needs an evidence_excerpt or source_id; no reply is not a reason")
        return self


class OutcomeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    opportunity_id: uuid.UUID
    milestone: Milestone
    response: BuyerResponse | None
    reason_category: ReasonCategory | None
    reason_basis: ReasonBasis | None
    evidence_excerpt: str | None
    source_id: uuid.UUID | None
    note: str | None
    occurred_at: datetime
    created_at: datetime


class HistoricalDealIn(BaseModel):
    model_config = Strict
    buyer_name: Name
    buyer_company_id: uuid.UUID | None = None
    target_name: Name
    target_company_id: uuid.UUID | None = None
    status: DealStatus
    announced_on: date | None = None
    completed_on: date | None = None
    withdrawn_on: date | None = None
    sector: Label | None = None
    country: Country | None = None
    structure: Structure | None = None
    stake_pct: Pct | None = None
    value_amount: Amount | None = Field(None, description="Null = undisclosed/unknown; never zero-filled")
    value_currency: Ccy | None = None
    source_url: str = Field(max_length=2048)
    source_title: str | None = Field(None, max_length=500)
    disclosure_rights: Literal["public", "licensed", "internal"]
    as_of: AwareDatetime = Field(description="When this information became available")

    @field_validator("source_url")
    @classmethod
    def http_url(cls, v: str) -> str:
        parts = urlsplit(v.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise ValueError("source_url must be an absolute http(s) URL without credentials")
        return v.strip()

    @model_validator(mode="after")
    def timeline(self):
        end = {DealStatus.completed: "completed_on", DealStatus.withdrawn: "withdrawn_on"}.get(self.status)
        for field in ("completed_on", "withdrawn_on"):
            if (field == end) != (getattr(self, field) is not None):
                raise ValueError(f"{field} is required exactly when status is {field.split('_')[0]}")
        if end and self.announced_on and getattr(self, end) < self.announced_on:
            raise ValueError(f"{end} is before announced_on")
        not_future(self.as_of, "as_of")
        known = utc(self.as_of).date()
        for field in ("announced_on", "completed_on", "withdrawn_on"):
            if getattr(self, field) and getattr(self, field) > known:
                raise ValueError(f"{field} is after as_of; information cannot predate its own events")
        if (self.value_amount is None) != (self.value_currency is None):
            raise ValueError("value_amount and value_currency go together")
        return self


class HistoricalDealOut(HistoricalDealIn):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class DealPage(BaseModel):
    items: list[HistoricalDealOut]
    total: int


class DisclosureAuthorization(BaseModel):
    model_config = Strict
    authorized_by: Name
    authority: SpeakerAuthority
    scope: list[DisclosureField] = Field(min_length=1)
    statement: str = Field(min_length=1, max_length=10000)
    authorized_at: AwareDatetime
    source_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def authority_ok(self):
        if self.authority == SpeakerAuthority.unverified:
            raise ValueError("disclosure must be authorized by an owner or authorized representative")
        not_future(self.authorized_at, "authorized_at")
        return self


class DraftIn(BaseModel):
    model_config = Strict
    authorization: DisclosureAuthorization | None = None


class DraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    opportunity_id: uuid.UUID
    mandate_id: uuid.UUID
    authorization: dict[str, Any]
    payload: dict[str, Any]
    content_hash: str
    status: str
    created_at: datetime


class EmailDraftIn(BaseModel):
    model_config = Strict
    proposal_id: uuid.UUID
    contact_id: uuid.UUID


class ReplayIn(BaseModel):
    model_config = Strict
    as_of: AwareDatetime
    company_id: uuid.UUID | None = None


# ---------------------------------------------------------------- helpers


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def validation_details(exc: ValidationError) -> list[dict[str, Any]]:
    return [{"loc": list(e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]


def merge(model: type[BaseModel], current: dict[str, Any], patch: dict[str, Any], nested: tuple[str, ...] = ()):
    """PATCH = shallow merge onto the stored record, then full revalidation so cross-field rules still hold."""
    unknown = sorted(set(patch) - set(model.model_fields))
    if unknown:
        raise ApiError(422, "validation_error", f"Unknown fields: {', '.join(unknown)}")
    merged = {**current, **patch}
    for key in nested:
        if isinstance(patch.get(key), dict) and isinstance(current.get(key), dict):
            merged[key] = {**current[key], **patch[key]}
    try:
        return model.model_validate(merged)
    except ValidationError as exc:
        raise ApiError(422, "validation_error", "Request validation failed", validation_details(exc))


def require(db: Session, model: type, id: uuid.UUID | None, field: str) -> None:
    if id is not None and db.get(model, id) is None:
        raise ApiError(422, "unknown_reference", f"{field} does not reference an existing {model.__name__}")


def is_active(m: BuyerMandate) -> bool:
    return m.status == MandateStatus.active and utc(m.expires_at) > utcnow()


def mandate_out(m: BuyerMandate) -> dict[str, Any]:
    return {**m.data, "id": m.id, "version": m.version, "active": is_active(m),
            "created_at": m.created_at, "updated_at": m.updated_at}


def mandate_snap(m: BuyerMandate) -> dict[str, Any]:
    return {**m.data, "id": str(m.id), "version": m.version}


def select_mandates(db: Session, ids: list[uuid.UUID] | None,
                    target_company_id: uuid.UUID | None = None) -> list[BuyerMandate]:
    if ids:
        mandates = [get_or_404(db, BuyerMandate, i) for i in dict.fromkeys(ids)]
        inactive = [str(m.id) for m in mandates if not is_active(m)]
        if inactive:
            raise ApiError(409, "inactive_mandates", "Only active, unexpired mandates can be compared",
                           {"mandate_ids": inactive})
    else:
        query = select(BuyerMandate).where(BuyerMandate.status == MandateStatus.active).order_by(BuyerMandate.created_at)
        mandates = [m for m in db.scalars(query) if is_active(m)]
    if target_company_id is not None:
        mandates = [m for m in mandates if str(m.data.get("buyer_company_id") or "") != str(target_company_id)]
    if not mandates:
        raise ApiError(409, "no_active_mandates", "No active, unexpired buyer mandates to compare against")
    return mandates


def profile_out(p: PreferenceProfile) -> dict[str, Any]:
    confirmation = p.confirmed_at and {
        "confirmed_at": p.confirmed_at, "confirmed_by": p.confirmed_by, "authority": p.confirmed_authority,
        "statement": p.confirmation_statement, "source_id": p.confirmation_source_id,
    }
    return {"id": p.id, "company_id": p.company_id, "version": p.version, "conditions": p.conditions,
            "conditions_hash": p.conditions_hash, "stated_by": p.stated_by, "note": p.note, "source_id": p.source_id,
            "confirmation": confirmation or None, "created_at": p.created_at}


def resolve_profile(db: Session, company_id: uuid.UUID, profile_id: uuid.UUID | None,
                    confirmed_only: bool) -> PreferenceProfile | None:
    if profile_id:
        profile = get_or_404(db, PreferenceProfile, profile_id)
        if profile.company_id != company_id:
            raise ApiError(422, "profile_company_mismatch", "profile_id belongs to another company")
        return profile
    query = select(PreferenceProfile).where(PreferenceProfile.company_id == company_id)
    if confirmed_only:
        query = query.where(PreferenceProfile.confirmed_at.is_not(None))
    return db.scalars(query.order_by(PreferenceProfile.version.desc()).limit(1)).first()


def profile_snap(p: PreferenceProfile | None) -> dict[str, Any] | None:
    return p and {"id": str(p.id), "version": p.version, "confirmed": p.confirmed_at is not None,
                  "conditions_hash": p.conditions_hash, "conditions": p.conditions}


def reviewed_first(db: Session, query, column):
    """Latest accepted row, else latest proposed. Rejected rows are never used."""
    return (db.scalars(query.where(column == ReviewStatus.accepted)).first()
            or db.scalars(query.where(column == ReviewStatus.proposed)).first())


def decided(fact: dict[str, Any] | None) -> bool:
    """Accepted evidence, the company record or an explicit scenario hypothesis; proposed evidence is not."""
    return bool(fact) and fact.get("review_status", ReviewStatus.accepted) == ReviewStatus.accepted


def company_facts(db: Session, company: Company) -> dict[str, Any]:
    """Accepted facts, or proposed ones marked as such (cited, but evaluated as unknown). Estimates stay unknown."""
    facts: dict[str, Any] = {}
    if company.country:
        facts["country"] = {"value": company.country, "citations": [f"company:{company.id}.country"]}
    ev = reviewed_first(db, select(Evidence).where(
        Evidence.company_id == company.id, Evidence.field == "industry").order_by(Evidence.created_at.desc()),
        Evidence.review_status)
    if ev and isinstance(ev.value, str) and ev.value.strip():
        facts["industry"] = {"value": ev.value.strip(), "review_status": ev.review_status,
                             "citations": [f"evidence:{ev.id}", ev.source.url or f"source:{ev.source_id}"]}
    for metric in (FinancialMetric.revenue, FinancialMetric.ebitda, FinancialMetric.employees):
        obs = reviewed_first(db, select(FinancialObservation).where(
            FinancialObservation.company_id == company.id, FinancialObservation.metric == metric,
            FinancialObservation.amount.is_not(None),
            FinancialObservation.scope.in_(["entity", "consolidated"]),
            FinancialObservation.status.in_([FinancialStatus.reported, FinancialStatus.derived]),
        ).order_by(FinancialObservation.period_end.desc(), FinancialObservation.created_at.desc()),
            FinancialObservation.review_status)
        if obs:
            facts[metric.value] = {
                "amount": str(obs.amount), "currency": obs.currency, "period_end": obs.period_end.isoformat(),
                "scope": obs.scope, "status": obs.status, "review_status": obs.review_status,
                "citations": [f"financial:{obs.id}", obs.source.url or f"source:{obs.source_id}"],
            }
    return facts


# ---------------------------------------------------------------- evaluation (pure, deterministic)

LABELS = {
    "retained_ownership": "retained ownership", "operating_control": "owner keeps operating control",
    "site_retention": "site stays open", "team_retention": "team retained", "brand_retention": "brand retained",
    "timeline": "timeline", "structure": "deal structure", "currency": "proceeds currency",
    "minimum_proceeds": "minimum proceeds",
}
BUYER_FIELD = {
    "retained_ownership": "max_rollover_pct", "operating_control": "control_retention",
    "site_retention": "site_commitment", "team_retention": "team_commitment", "brand_retention": "brand_commitment",
    "timeline": "close_within_months", "structure": "structures", "currency": "consideration_currency",
    "minimum_proceeds": "max_consideration",
}
STRENGTH = {"hard": "hard", "soft": "soft", "unknown": "unconfirmed"}


def _check(key: str, origin: str, strength: str, result: str, detail: str, citations: list[str],
           question: str | None = None) -> dict[str, Any]:
    blocking = (strength == "hard" and result == "unknown") or (strength == "unconfirmed" and result != "pass")
    return {"key": key, "origin": origin, "strength": strength, "result": result, "blocking": blocking,
            "detail": detail, "citations": citations, "question": question}


def mandate_checks(facts: dict[str, Any], m: dict[str, Any]) -> list[dict[str, Any]]:
    c, ref = m["criteria"], f"mandate:{m['id']}@v{m['version']}.criteria"
    out = []
    for key, field, fact_key in (("geography", "countries", "country"), ("industry", "industries", "industry")):
        allowed = c.get(field) or []
        if not allowed:
            continue
        fact = facts.get(fact_key)
        cites = [f"{ref}.{field}", *(fact["citations"] if fact else [])]
        if fact and not decided(fact):
            out.append(_check(key, "mandate", "hard", "unknown", f"company {fact_key} '{fact['value']}' is only "
                              f"proposed evidence, not a reviewed fact", cites, f"Review the proposed {fact_key} evidence"))
            continue
        if not fact:
            out.append(_check(key, "mandate", "hard", "unknown", f"company {fact_key} not evidenced", cites,
                              f"Research the company's {fact_key} (mandate requires one of {', '.join(allowed)})"))
            continue
        ok = fact["value"].casefold() in {a.casefold() for a in allowed}
        out.append(_check(key, "mandate", "hard", "pass" if ok else "fail",
                          f"{fact['value']} {'is' if ok else 'is not'} in {', '.join(allowed)}", cites))
    for metric in ("revenue", "ebitda", "employees"):
        lo, hi = c.get(f"{metric}_min"), c.get(f"{metric}_max")
        if lo is None and hi is None:
            continue
        fact, ccy = facts.get(metric), None if metric == "employees" else c.get("financial_currency")
        bounds = f"[{lo if lo is not None else '-'}, {hi if hi is not None else '-'}]{' ' + ccy if ccy else ''}"
        cites = [f"{ref}.{metric}_min/max", *(fact["citations"] if fact else [])]
        if fact and not decided(fact):
            out.append(_check(metric, "mandate", "hard", "unknown", f"{metric} {fact['amount']} is only proposed "
                              f"evidence, not a reviewed fact; mandate range {bounds}", cites,
                              f"Review the proposed {metric} observation"))
        elif not fact:
            out.append(_check(metric, "mandate", "hard", "unknown", f"{metric} not evidenced; mandate range {bounds}",
                              cites, f"Obtain sourced {metric} (mandate range {bounds})"))
        elif ccy and fact["currency"] != ccy:
            out.append(_check(metric, "mandate", "hard", "unknown",
                              f"{metric} reported in {fact['currency']}, mandate in {ccy}; no FX conversion", cites,
                              f"Obtain {metric} in {ccy} or an advisor-approved conversion"))
        else:
            v = Decimal(fact["amount"])
            ok = (lo is None or v >= Decimal(str(lo))) and (hi is None or v <= Decimal(str(hi)))
            out.append(_check(metric, "mandate", "hard", "pass" if ok else "fail",
                              f"{metric} {fact['amount']} (period {fact.get('period_end')}) vs range {bounds}", cites))
    return out


def condition_check(cond: dict[str, Any], m: dict[str, Any], cref: str) -> dict[str, Any]:
    kind, c = cond["kind"], m["criteria"]
    field, label, buyer_name = BUYER_FIELD[kind], LABELS[kind], m["buyer_name"]
    buyer = c.get(field)
    strength = STRENGTH[cond["strength"]]
    cites = [f"scenario:override.{kind}" if cond.get("override") else f"{cref}.conditions.{kind}",
             f"mandate:{m['id']}@v{m['version']}.criteria.{field}"]
    result, detail = None, None
    if buyer in (None, []):
        result, detail = "unknown", f"mandate is silent on {field}"
    elif kind == "retained_ownership":
        ok = Decimal(str(buyer)) >= Decimal(str(cond["min_pct"]))
        detail = f"owner keeps at least {cond['min_pct']}%; buyer allows up to {buyer}% rollover"
    elif kind == "timeline":
        ok = buyer <= cond["within_months"]
        detail = f"buyer closes within {buyer} months; owner wants within {cond['within_months']}"
    elif kind == "structure":
        common = sorted(set(buyer) & set(cond["structures"]))
        ok = bool(common)
        detail = f"shared structures: {', '.join(common)}" if ok else f"buyer {buyer} vs owner {cond['structures']}"
    elif kind == "currency":
        ok = buyer in cond["currencies"]
        detail = f"buyer pays in {buyer}; owner accepts {', '.join(cond['currencies'])}"
    elif kind == "minimum_proceeds":
        ccy = c.get("consideration_currency")
        if ccy != cond["currency"]:
            result, detail = "unknown", f"buyer ceiling in {ccy}, owner minimum in {cond['currency']}; no FX conversion"
        else:
            ok = Decimal(str(buyer)) >= Decimal(str(cond["amount"]))
            detail = f"buyer ceiling {buyer} {ccy} vs owner minimum {cond['amount']} (not a valuation or offer)"
    else:
        ok = buyer is True
        detail = f"buyer {'commits to' if ok else 'does not commit to'}: {label}"
    if result is None:
        result = "pass" if ok else "fail"
    question = None
    if result == "unknown":
        question = f"Ask {buyer_name} about {label} ({detail})"
    elif result == "fail" and strength == "unconfirmed":
        question = f"Ask the owner whether '{label}' is a firm requirement; {buyer_name} conflicts ({detail})"
    return _check(kind, "owner", strength, result, detail, cites, question)


def evaluate(facts: dict[str, Any], profile: dict[str, Any] | None, conditions: list[dict[str, Any]],
             m: dict[str, Any]) -> dict[str, Any]:
    cref = f"preference:{profile['id']}@v{profile['version']}" if profile else "preference:none"
    owner = [condition_check(cond, m, cref) for cond in conditions]
    checks = mandate_checks(facts, m) + owner
    buyer = m["buyer_name"]
    questions = [k["question"] for k in checks if k["question"]]
    blockers = [k["key"] for k in checks if k["blocking"]]
    if m["evidence_level"] == EvidenceLevel.public_strategy:
        blockers.append("mandate_confirmation")
        questions.append(f"Confirm {buyer}'s current mandate directly; only public strategy is recorded")
    if not (m.get("identity_verified") and m.get("source_id")):
        blockers.append("buyer_identity")
        questions.append(f"Verify {buyer}'s identity and attach the source that evidences the mandate")
    if profile is None:
        blockers.append("owner_conditions")
        questions.append("Record the owner's conditions and have the owner confirm them")
    elif not profile["confirmed"]:
        blockers.append("owner_confirmation")
        questions.append(f"Get owner confirmation of preference version {profile['version']}")

    hard_fails = [k for k in checks if k["strength"] == "hard" and k["result"] == "fail"]
    if hard_fails:
        status = MatchStatus.excluded
        summary = f"Excluded for {buyer}: {', '.join(k['key'] for k in hard_fails)} fails a hard condition"
        next_action = "None; excluded unless the failing hard condition is explicitly changed"
    elif blockers:
        status = MatchStatus.research_needed
        summary = f"Potentially compatible with {buyer}; unresolved: {', '.join(dict.fromkeys(blockers))}"
        next_action = questions[0]
    else:
        status = MatchStatus.compatible
        summary = f"Potentially compatible with {buyer}; not a confirmed match, interest or offer"
        next_action = "Advisor review before any buyer contact"

    # Only evidenced soft passes add score; unknown adds nothing and shows up as lower coverage.
    soft = [(k, cond.get("weight", 1)) for k, cond in zip(owner, conditions) if k["strength"] == "soft"]
    total = sum(w for _, w in soft)
    passed = sum(w for k, w in soft if k["result"] == "pass")
    known = sum(w for k, w in soft if k["result"] != "unknown")

    def lines(result: str) -> list[dict[str, Any]]:
        return [{"key": k["key"], "strength": k["strength"], "detail": k["detail"], "citations": k["citations"]}
                for k in checks if k["result"] == result]

    return {
        "status": status,
        "fit_score": round(passed / total, 4) if total and status != MatchStatus.excluded else None,
        "coverage": round(known / total, 4) if total else None,
        "checks": checks,
        "explanation": {
            "summary": summary, "supporting": lines("pass"), "contrary": lines("fail"), "unknown": lines("unknown"),
            "questions": questions, "next_action": next_action, "mandate_evidence": m["evidence_level"],
            "financing": m["financing_status"] if m["financing_status"] != Financing.unknown
            else "unknown (not inferred)",
        },
    }


def comparables(db: Session, m: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
    """Prior deals by the same buyer known at as_of: a hypothesis aid, not evidence of current demand."""
    same = func.lower(HistoricalDeal.buyer_name) == m["buyer_name"].lower()
    if m.get("buyer_company_id"):
        same = same | (HistoricalDeal.buyer_company_id == uuid.UUID(m["buyer_company_id"]))
    deals = db.scalars(select(HistoricalDeal).where(same, HistoricalDeal.as_of <= utc(as_of))
                       .order_by(HistoricalDeal.announced_on.desc()).limit(5))
    return [{"id": str(d.id), "target_name": d.target_name, "status": d.status, "announced_on": d.data["announced_on"],
             "structure": d.data["structure"], "source_url": d.source_url} for d in deals]


# ---------------------------------------------------------------- mandates


def check_mandate_refs(db: Session, body: MandateIn, check_expiry: bool) -> None:
    require(db, Company, body.buyer_company_id, "buyer_company_id")
    require(db, Source, body.source_id, "source_id")
    require(db, Source, body.financing_source_id, "financing_source_id")
    if check_expiry and utc(body.expires_at) <= utcnow():
        raise ApiError(422, "expired", "expires_at must be in the future")


@router.get("/mandates", response_model=list[MandateOut])
def list_mandates(active_only: bool = False, status: MandateStatus | None = None, db: Session = Depends(get_db)):
    query = select(BuyerMandate).order_by(BuyerMandate.created_at.desc())
    if status:
        query = query.where(BuyerMandate.status == status)
    return [mandate_out(m) for m in db.scalars(query) if not active_only or is_active(m)]


@router.post("/mandates", response_model=MandateOut, status_code=201)
def create_mandate(body: MandateIn, db: Session = Depends(get_db)):
    check_mandate_refs(db, body, check_expiry=True)
    data = body.model_dump(mode="json")
    m = BuyerMandate(buyer_name=body.buyer_name, status=body.status, expires_at=utc(body.expires_at), data=data)
    db.add(m)
    db.flush()
    db.add(MandateVersion(mandate_id=m.id, version=1, data=data, changed_fields=sorted(data)))
    record_activity(db, "mandate.created", f"Buyer mandate for {body.buyer_name} recorded", None, mandate_id=str(m.id))
    db.commit()
    return mandate_out(m)


@router.get("/mandates/{mandate_id}", response_model=MandateDetail)
def get_mandate(mandate_id: uuid.UUID, db: Session = Depends(get_db)):
    m = get_or_404(db, BuyerMandate, mandate_id)
    versions = db.scalars(select(MandateVersion).where(MandateVersion.mandate_id == m.id)
                          .order_by(MandateVersion.version.desc())).all()
    return {**mandate_out(m), "versions": versions}


@router.patch("/mandates/{mandate_id}", response_model=MandateOut)
def update_mandate(mandate_id: uuid.UUID, patch: dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    m = get_or_404(db, BuyerMandate, mandate_id)
    body = merge(MandateIn, m.data, patch, nested=("criteria",))
    data = body.model_dump(mode="json")
    changed = sorted(k for k in data if data[k] != m.data.get(k))
    if not changed:
        return mandate_out(m)
    check_mandate_refs(db, body, check_expiry="expires_at" in changed)
    m.version += 1
    m.data, m.buyer_name, m.status, m.expires_at = data, body.buyer_name, body.status, utc(body.expires_at)
    db.add(MandateVersion(mandate_id=m.id, version=m.version, data=data, changed_fields=changed))
    record_activity(db, "mandate.updated", f"Buyer mandate for {m.buyer_name} updated to v{m.version}", None,
                    mandate_id=str(m.id), fields=changed)
    db.commit()
    return mandate_out(m)


# ---------------------------------------------------------------- owner preferences


@router.get("/companies/{company_id}/preferences", response_model=PreferenceList)
def list_preferences(company_id: uuid.UUID, db: Session = Depends(get_db)):
    get_or_404(db, Company, company_id)
    items = db.scalars(select(PreferenceProfile).where(PreferenceProfile.company_id == company_id)
                       .order_by(PreferenceProfile.version.desc())).all()
    effective = next((p.id for p in items if p.confirmed_at is not None), None)
    return {"company_id": company_id, "effective_profile_id": effective, "items": [profile_out(p) for p in items]}


@router.post("/companies/{company_id}/preferences", response_model=PreferenceOut, status_code=201)
def create_preferences(company_id: uuid.UUID, body: PreferenceIn, db: Session = Depends(get_db)):
    get_or_404(db, Company, company_id)
    require(db, Source, body.source_id, "source_id")
    conditions = [c.model_dump(mode="json", exclude_none=True) for c in sorted(body.conditions, key=lambda c: c.kind)]
    version = (db.scalar(select(func.max(PreferenceProfile.version))
                         .where(PreferenceProfile.company_id == company_id)) or 0) + 1
    profile = PreferenceProfile(company_id=company_id, version=version, conditions=conditions,
                                conditions_hash=digest(conditions), stated_by=body.stated_by, note=body.note,
                                source_id=body.source_id)
    db.add(profile)
    record_activity(db, "preferences.proposed", f"Owner preference version {version} proposed", company_id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "version_conflict", "Another preference version was saved concurrently; retry")
    return profile_out(profile)


@router.post("/preferences/{profile_id}/confirm", response_model=PreferenceOut)
def confirm_preferences(profile_id: uuid.UUID, body: ConfirmIn, db: Session = Depends(get_db)):
    profile = get_or_404(db, PreferenceProfile, profile_id)
    require(db, Source, body.source_id, "source_id")
    if body.conditions_hash != profile.conditions_hash:
        raise ApiError(409, "version_mismatch", "conditions_hash does not match this preference version")
    if profile.confirmed_at is not None:
        raise ApiError(409, "already_confirmed", "This preference version is already confirmed")
    newer = db.scalar(select(func.count()).select_from(PreferenceProfile).where(
        PreferenceProfile.company_id == profile.company_id, PreferenceProfile.version > profile.version,
        PreferenceProfile.confirmed_at.is_not(None)))
    if newer:
        raise ApiError(409, "superseded", "A newer preference version is already confirmed")
    profile.confirmed_at, profile.confirmed_by = utc(body.confirmed_at), body.speaker_name
    profile.confirmed_authority, profile.confirmation_statement = body.speaker_authority, body.statement
    profile.confirmation_source_id = body.source_id
    record_activity(db, "preferences.confirmed", f"Owner preference version {profile.version} confirmed",
                    profile.company_id, authority=body.speaker_authority, conditions_hash=profile.conditions_hash)
    db.commit()
    return profile_out(profile)


# ---------------------------------------------------------------- futures scenarios


def hypothetical(key: str, value: Any) -> dict[str, Any]:
    if key in ("country", "industry"):
        fact = {"value": value}
    elif key == "employees":
        fact = {"amount": str(value), "currency": None}
    else:
        fact = {"amount": str(value["amount"]), "currency": value["currency"]}
    return {**fact, "hypothetical": True, "citations": [f"scenario:hypothetical.{key}"]}


@router.post("/scenarios", response_model=ScenarioOut, status_code=201)
def create_scenario(body: ScenarioIn, db: Session = Depends(get_db)):
    company = get_or_404(db, Company, body.company_id)
    profile = profile_snap(resolve_profile(db, company.id, body.profile_id, confirmed_only=False))
    mandates = [mandate_snap(m) for m in select_mandates(db, body.mandate_ids, company.id)]
    base_conditions = profile["conditions"] if profile else []
    effective = {c["kind"]: c for c in base_conditions}
    for c in body.conditions:
        effective[c.kind] = {**c.model_dump(mode="json", exclude_none=True), "override": True}
    for kind in body.remove_kinds:
        effective.pop(kind, None)
    conditions = [effective[k] for k in sorted(effective)]
    real_facts = company_facts(db, company)
    facts = {**real_facts, **{k: hypothetical(k, v) for k, v in
                              (body.facts.model_dump(mode="json", exclude_none=True) if body.facts else {}).items()}}
    results = []
    for m in mandates:
        baseline = evaluate(real_facts, profile, base_conditions, m)["status"]
        results.append({"mandate_id": m["id"], "mandate_version": m["version"], "buyer_name": m["buyer_name"],
                        "baseline_status": baseline, **evaluate(facts, profile, conditions, m)})
    snapshot = {"taken_at": utcnow().isoformat(), "policy_version": POLICY_VERSION,
                "company": {"id": str(company.id), "name": company.name}, "profile": profile,
                "overrides": body.model_dump(mode="json", include={"conditions", "remove_kinds", "facts"}),
                "effective_conditions": conditions, "facts": facts, "mandates": mandates}
    scenario = Scenario(company_id=company.id, profile_id=profile and uuid.UUID(profile["id"]), name=body.name,
                        snapshot=snapshot, snapshot_hash=digest(snapshot), results=results)
    db.add(scenario)
    record_activity(db, "scenario.created", f"Futures scenario '{body.name}' computed", company.id)
    db.commit()
    return scenario


@router.get("/scenarios", response_model=list[ScenarioOut])
def list_scenarios(company_id: uuid.UUID | None = None, limit: int = Query(50, ge=1, le=200),
                   db: Session = Depends(get_db)):
    query = select(Scenario).order_by(Scenario.created_at.desc()).limit(limit)
    if company_id:
        query = query.where(Scenario.company_id == company_id)
    return db.scalars(query).all()


# ---------------------------------------------------------------- match runs and opportunities


def upsert_opportunity(db: Session, company_id: uuid.UUID, mandate_id: uuid.UUID, result: MatchResult) -> None:
    opp = db.scalar(select(Opportunity).where(Opportunity.company_id == company_id,
                                              Opportunity.mandate_id == mandate_id))
    if opp is None:
        if result.status == MatchStatus.excluded:
            return
        opp = Opportunity(company_id=company_id, mandate_id=mandate_id)
        db.add(opp)
    # An existing opportunity that is now excluded keeps its outcome history but is marked excluded.
    opp.status, opp.match_result_id = result.status, result.id


def run_detail(db: Session, run: MatchRun) -> dict[str, Any]:
    results = db.scalars(select(MatchResult).where(MatchResult.run_id == run.id)).all()
    order = {MatchStatus.compatible: 0, MatchStatus.research_needed: 1, MatchStatus.excluded: 2}
    results = sorted(results, key=lambda r: (order[r.status], -(r.fit_score or 0), -(r.coverage or 0)))
    return {**MatchRunOut.model_validate(run).model_dump(), "snapshot": run.snapshot, "results": results}


@router.post("/match-runs", response_model=MatchRunDetail, status_code=201)
def create_match_run(body: MatchRunIn, db: Session = Depends(get_db)):
    company = get_or_404(db, Company, body.company_id)
    profile = profile_snap(resolve_profile(db, company.id, body.profile_id, confirmed_only=True))
    mandates = select_mandates(db, body.mandate_ids, company.id)
    facts, now = company_facts(db, company), utcnow()
    snaps = [mandate_snap(m) for m in mandates]
    snapshot = {"taken_at": now.isoformat(), "policy_version": POLICY_VERSION,
                "company": {"id": str(company.id), "name": company.name}, "facts": facts, "profile": profile,
                "mandates": snaps}
    run = MatchRun(company_id=company.id, profile_id=profile and uuid.UUID(profile["id"]),
                   policy_version=POLICY_VERSION, snapshot=snapshot, snapshot_hash=digest(snapshot))
    db.add(run)
    db.flush()
    counts: Counter[str] = Counter()
    for m, snap in zip(mandates, snaps):
        r = evaluate(facts, profile, profile["conditions"] if profile else [], snap)
        r["explanation"]["comparables"] = comparables(db, snap, now)
        result = MatchResult(run_id=run.id, mandate_id=m.id, mandate_version=m.version, **r)
        db.add(result)
        db.flush()
        upsert_opportunity(db, company.id, m.id, result)
        counts[r["status"]] += 1
    run.counts = {s.value: counts[s] for s in MatchStatus}
    record_activity(db, "match.run", f"Matched against {len(mandates)} mandates", company.id, run_id=str(run.id),
                    **run.counts)
    db.commit()
    return run_detail(db, run)


def refresh_matches(sessionmaker: sessionmaker[Session]) -> int:
    """Refresh company runs only when their accepted input or visible comparables changed."""
    # ponytail: scans the personal workspace; use change-triggered jobs if it grows beyond a few hundred companies.
    refreshed = 0
    with sessionmaker() as db:
        companies = db.scalars(select(Company).order_by(Company.id)).all()
        for company in companies:
            try:
                mandates = select_mandates(db, None, company.id)
            except ApiError as exc:
                if exc.code == "no_active_mandates":
                    continue
                raise
            profile = profile_snap(resolve_profile(db, company.id, None, confirmed_only=True))
            facts = company_facts(db, company)
            mandate_snaps = [mandate_snap(m) for m in mandates]
            basis = {"policy_version": POLICY_VERSION, "company": {"id": str(company.id), "name": company.name},
                     "facts": facts, "profile": profile, "mandates": mandate_snaps}
            latest = db.scalar(select(MatchRun).where(MatchRun.company_id == company.id)
                               .order_by(MatchRun.created_at.desc(), MatchRun.id.desc()).limit(1))
            changed = latest is None or any(latest.snapshot.get(key) != value for key, value in basis.items())
            if latest is not None and not changed:
                saved = {str(result.mandate_id): result.explanation.get("comparables", [])
                         for result in db.scalars(select(MatchResult).where(MatchResult.run_id == latest.id))}
                now = utcnow()
                current = {snap["id"]: comparables(db, snap, now) for snap in mandate_snaps}
                changed = saved != current
            if changed:
                create_match_run(MatchRunIn(company_id=company.id), db)
                refreshed += 1
    return refreshed


@router.get("/match-runs", response_model=list[MatchRunOut])
def list_match_runs(company_id: uuid.UUID | None = None, limit: int = Query(50, ge=1, le=200),
                    db: Session = Depends(get_db)):
    query = select(MatchRun).order_by(MatchRun.created_at.desc()).limit(limit)
    if company_id:
        query = query.where(MatchRun.company_id == company_id)
    return db.scalars(query).all()


@router.get("/match-runs/{run_id}", response_model=MatchRunDetail)
def get_match_run(run_id: uuid.UUID, db: Session = Depends(get_db)):
    return run_detail(db, get_or_404(db, MatchRun, run_id))


def saved_basis(db: Session, opp: Opportunity) -> dict[str, Any] | None:
    """The exact company, facts, owner profile and mandate versions the opportunity's result was computed from."""
    result = db.get(MatchResult, opp.match_result_id) if opp.match_result_id else None
    if result is None:
        return None
    run = db.get(MatchRun, result.run_id)
    profile = run.snapshot["profile"]
    return {"company_id": str(opp.company_id), "company_name": run.snapshot["company"]["name"],
            "facts_hash": digest(run.snapshot["facts"]), "profile_id": profile and profile["id"],
            "profile_version": profile and profile["version"], "conditions_hash": profile and profile["conditions_hash"],
            "mandate_id": str(opp.mandate_id), "mandate_version": result.mandate_version,
            "match_result_id": str(result.id), "snapshot_hash": run.snapshot_hash}


STALE_KEYS = {"company_name": "company_facts", "facts_hash": "company_facts", "profile_id": "owner_profile",
              "conditions_hash": "owner_profile", "mandate_version": "mandate_version"}


def stale_reasons(db: Session, opp: Opportunity, saved: dict[str, Any] | None = None) -> list[str]:
    """Saved basis vs the current mandate, current company facts and the effective (highest confirmed) profile."""
    saved = saved or saved_basis(db, opp)
    if saved is None:
        return ["no_match_result"]
    company, mandate = db.get(Company, opp.company_id), db.get(BuyerMandate, opp.mandate_id)
    profile = resolve_profile(db, company.id, None, confirmed_only=True)
    current = {"company_name": company.name, "facts_hash": digest(company_facts(db, company)),
               "profile_id": profile and str(profile.id), "conditions_hash": profile and profile.conditions_hash,
               "mandate_version": mandate.version}
    reasons = [reason for key, reason in STALE_KEYS.items() if saved[key] != current[key]]
    if not is_active(mandate):
        reasons.append("mandate_inactive")
    return list(dict.fromkeys(reasons))


def opportunity_out(db: Session, o: Opportunity) -> dict[str, Any]:
    # ponytail: recomputes company facts per row; batch them if opportunity lists grow past a few hundred.
    result = db.get(MatchResult, o.match_result_id) if o.match_result_id else None
    mandate = db.get(BuyerMandate, o.mandate_id)
    reasons = stale_reasons(db, o)
    return {"id": o.id, "company_id": o.company_id, "mandate_id": o.mandate_id, "buyer_name": mandate.buyer_name,
            "status": o.status, "latest_milestone": o.latest_milestone, "match_result_id": o.match_result_id,
            "fit_score": result and result.fit_score, "coverage": result and result.coverage,
            "summary": result and result.explanation["summary"],
            "questions": result.explanation["questions"] if result else [],
            "stale": bool(reasons), "stale_reasons": reasons, "created_at": o.created_at, "updated_at": o.updated_at}


@router.get("/opportunities", response_model=list[OpportunityOut])
def list_opportunities(company_id: uuid.UUID | None = None, mandate_id: uuid.UUID | None = None,
                       status: MatchStatus | None = None, limit: int = Query(100, ge=1, le=500),
                       db: Session = Depends(get_db)):
    query = select(Opportunity).order_by(Opportunity.updated_at.desc()).limit(limit)
    for column, value in ((Opportunity.company_id, company_id), (Opportunity.mandate_id, mandate_id),
                          (Opportunity.status, status)):
        if value is not None:
            query = query.where(column == value)
    return [opportunity_out(db, o) for o in db.scalars(query)]


@router.post("/opportunities/{opportunity_id}/outcomes", response_model=OutcomeOut, status_code=201)
def add_outcome(opportunity_id: uuid.UUID, body: OutcomeIn, db: Session = Depends(get_db)):
    opp = get_or_404(db, Opportunity, opportunity_id)
    require(db, Source, body.source_id, "source_id")
    event = OutcomeEvent(opportunity_id=opp.id, **{**body.model_dump(), "occurred_at": utc(body.occurred_at)})
    db.add(event)
    db.flush()
    opp.latest_milestone = db.scalar(select(OutcomeEvent.milestone).where(OutcomeEvent.opportunity_id == opp.id)
                                     .order_by(OutcomeEvent.occurred_at.desc(), OutcomeEvent.created_at.desc()).limit(1))
    record_activity(db, "opportunity.outcome", f"Opportunity milestone: {body.milestone}", opp.company_id,
                    opportunity_id=str(opp.id), milestone=body.milestone)
    db.commit()
    return event


@router.get("/opportunities/{opportunity_id}/outcomes", response_model=list[OutcomeOut])
def list_outcomes(opportunity_id: uuid.UUID, db: Session = Depends(get_db)):
    get_or_404(db, Opportunity, opportunity_id)
    return db.scalars(select(OutcomeEvent).where(OutcomeEvent.opportunity_id == opportunity_id)
                      .order_by(OutcomeEvent.occurred_at)).all()


@router.post("/opportunities/{opportunity_id}/drafts", response_model=DraftOut, status_code=201)
def create_draft(opportunity_id: uuid.UUID, body: DraftIn, db: Session = Depends(get_db)):
    """Buyer-specific brief limited to the owner's authorized scope. Stored as a draft; nothing is sent."""
    opp = get_or_404(db, Opportunity, opportunity_id)
    auth = body.authorization
    if auth is None:
        raise ApiError(409, "disclosure_authorization_required",
                       "A draft for a buyer needs the owner's specific disclosure authorization")
    require(db, Source, auth.source_id, "source_id")
    if opp.status == MatchStatus.excluded or opp.match_result_id is None:
        raise ApiError(409, "opportunity_excluded", "Excluded opportunities cannot be proposed")
    basis = saved_basis(db, opp)
    if reasons := stale_reasons(db, opp, basis):
        raise ApiError(409, "opportunity_stale", "Rerun matching before drafting a disclosure", {"reasons": reasons})
    result = db.get(MatchResult, opp.match_result_id)
    snapshot = db.get(MatchRun, result.run_id).snapshot
    mandate = next(m for m in snapshot["mandates"] if m["id"] == str(opp.mandate_id))
    facts, scope = snapshot["facts"], set(auth.scope)
    company: dict[str, Any] = {"name": snapshot["company"]["name"] if "company_identity" in scope else UNDISCLOSED}
    # Only reviewed facts are disclosed; proposed evidence never leaves the workspace.
    for key in ("country", "industry"):
        if key in scope and decided(facts.get(key)):
            company[key] = facts[key]["value"]
    for key in ("revenue", "ebitda", "employees"):
        if key in scope and decided(facts.get(key)):
            company[key] = {k: facts[key][k] for k in ("amount", "currency", "period_end")}
    if "owner_conditions" in scope and snapshot["profile"] and snapshot["profile"]["confirmed"]:
        company["owner_conditions"] = [{k: v for k, v in c.items() if k not in ("note", "override")}
                                       for c in snapshot["profile"]["conditions"] if c["strength"] != "unknown"]
    payload = {"recipient": mandate["buyer_name"], "basis": basis, "match_status": result.status, "company": company,
               "disclaimer": "Compatibility with stated criteria only; not an offer, valuation or guarantee."}
    authorization = auth.model_dump(mode="json")
    draft = ProposalDraft(opportunity_id=opp.id, mandate_id=opp.mandate_id, authorization=authorization,
                          payload=payload, content_hash=proposal_hash(payload, authorization))
    db.add(draft)
    record_activity(db, "proposal.drafted", f"Draft brief for {mandate['buyer_name']} created (not sent)",
                    opp.company_id, opportunity_id=str(opp.id), content_hash=draft.content_hash)
    db.commit()
    return draft


UNDISCLOSED = "Undisclosed company"
DEAL_BRIEF = "deal_brief"


def proposal_hash(payload: dict[str, Any], authorization: dict[str, Any]) -> str:
    return digest({"payload": payload, "authorization": authorization})


def one_line(text: str) -> str:
    return " ".join(str(text).split())


def scope_violations(payload: dict[str, Any], scope: list[str]) -> list[str]:
    company = payload["company"]
    out = [key for key in company if key != "name" and key not in scope]
    if company["name"] != UNDISCLOSED and DisclosureField.company_identity not in scope:
        out.append(DisclosureField.company_identity.value)
    return out


def brief_text(payload: dict[str, Any], contact_name: str) -> tuple[str, str]:
    """Plain-text email from the authorized payload only; nothing else about the company is read."""
    c = payload["company"]
    lines = [f"Company: {one_line(c['name'])}"]
    lines += [f"{key.capitalize()}: {one_line(c[key])}" for key in ("country", "industry") if key in c]
    for key in ("revenue", "ebitda", "employees"):
        if key in c:
            v = c[key]
            unit = f" {v['currency']}" if v.get("currency") else ""
            lines.append(f"{'EBITDA' if key == 'ebitda' else key.capitalize()}: {v['amount']}{unit} "
                         f"(period ending {v['period_end']})")
    if "owner_conditions" in c:
        lines.append("Owner conditions:")
        for cond in c["owner_conditions"]:
            params = ", ".join(f"{p}: {cond[p]}" for p in PARAM_NAMES if p in cond)
            lines.append(f"- {LABELS[cond['kind']]} ({cond['strength']}){f'; {params}' if params else ''}")
    subject = f"Confidential opportunity for {one_line(payload['recipient'])}: {one_line(c['name'])}"[:300]
    body = (f"Hello {one_line(contact_name)},\n\nWith reference to your current acquisition criteria, the owner of a "
            f"company has authorized us to share the following:\n\n" + "\n".join(lines) +
            f"\n\n{payload['disclaimer']}\n")
    return subject, body


def proposal_problems(db: Session, proposal: ProposalDraft, contact_id: uuid.UUID) -> list[str]:
    problems = []
    if proposal_hash(proposal.payload, proposal.authorization) != proposal.content_hash:
        problems.append("proposal_modified")
    opp = db.get(Opportunity, proposal.opportunity_id)
    if opp.status == MatchStatus.excluded:
        problems.append("opportunity_excluded")
    basis = proposal.payload.get("basis")
    if basis != saved_basis(db, opp):
        problems.append("match_result_superseded")
    problems += stale_reasons(db, opp, basis)
    problems += [f"out_of_scope:{k}" for k in scope_violations(proposal.payload, proposal.authorization["scope"])]
    buyer_company = db.get(BuyerMandate, opp.mandate_id).data.get("buyer_company_id")
    contact = db.get(Contact, contact_id)
    if not buyer_company:
        problems.append("mandate_without_buyer_company")
    if contact is None:
        return problems + ["contact_missing"]
    if str(contact.company_id) != buyer_company:
        problems.append("contact_not_at_buyer_company")
    if contact.contact_role != ContactRole.buyer:
        problems.append("contact_not_buyer_role")
    if contact.verification != Verification.verified:
        problems.append("contact_unverified")
    if not contact.email:
        problems.append("contact_without_email")
    return list(dict.fromkeys(problems))


def message_hash(subject: str, body: str) -> str:
    return digest({"subject": subject, "body": body})


@router.post("/opportunities/{opportunity_id}/email-draft", status_code=201)
def create_email_draft(opportunity_id: uuid.UUID, body: EmailDraftIn, db: Session = Depends(get_db)):
    """Turns an authorized proposal into an unapproved outreach draft. Never approves or sends."""
    opp = get_or_404(db, Opportunity, opportunity_id)
    proposal = get_or_404(db, ProposalDraft, body.proposal_id)
    if proposal.opportunity_id != opp.id:
        raise ApiError(422, "proposal_mismatch", "proposal_id belongs to another opportunity")
    if problems := proposal_problems(db, proposal, body.contact_id):
        raise ApiError(409, "disclosure_blocked", "This proposal cannot be emailed to this contact",
                       {"reasons": problems})
    contact = db.get(Contact, body.contact_id)
    subject, text = brief_text(proposal.payload, contact.name)
    auth = proposal.authorization
    disclosure = {"type": DEAL_BRIEF, "proposal_id": str(proposal.id), "proposal_content_hash": proposal.content_hash,
                  "opportunity_id": str(opp.id), "contact_id": str(contact.id), "basis": proposal.payload["basis"],
                  "scope": auth["scope"], "authorized_by": auth["authorized_by"], "authority": auth["authority"],
                  "authorized_at": auth["authorized_at"], "message_sha256": message_hash(subject, text)}
    draft = OutreachDraft(company_id=contact.company_id, contact_id=contact.id, kind=DEAL_BRIEF,
                          recipients=[contact.email], subject=subject, body=text, disclosure=disclosure)
    db.add(draft)
    record_activity(db, "proposal.email_drafted", f"Email draft of brief for {proposal.payload['recipient']} created "
                    "(awaiting approval)", opp.company_id, opportunity_id=str(opp.id), proposal_id=str(proposal.id))
    db.commit()
    return {"mail_draft_id": draft.id, "status": draft.status, "recipients": draft.recipients, "subject": subject,
            "body": text, "disclosure": disclosure}


def validate_deal_disclosure(db: Session, disclosure: dict[str, Any] | None, draft: Any = None) -> None:
    """Send-time gate for deal-brief mail drafts; raises ApiError(409) when the disclosure no longer holds.

    Pass the OutreachDraft as `draft` so an edited subject/body, contact or stripped disclosure is caught too.
    Returns silently for drafts that are not deal briefs.
    """
    is_brief = (disclosure or {}).get("type") == DEAL_BRIEF
    if not is_brief and getattr(draft, "kind", None) != DEAL_BRIEF:
        return
    try:
        proposal = db.get(ProposalDraft, uuid.UUID(disclosure["proposal_id"]))
        contact_id = uuid.UUID(disclosure["contact_id"])
    except (KeyError, TypeError, ValueError):
        raise ApiError(409, "deal_disclosure_invalid", "Deal disclosure metadata is missing or malformed")
    if proposal is None:
        raise ApiError(409, "deal_disclosure_invalid", "The authorized proposal no longer exists")
    problems = proposal_problems(db, proposal, contact_id)
    if (not is_brief or disclosure.get("proposal_content_hash") != proposal.content_hash
            or disclosure.get("basis") != proposal.payload.get("basis")):
        problems.append("disclosure_mismatch")
    if draft is not None:
        if disclosure.get("message_sha256") != message_hash(draft.subject, draft.body):
            problems.append("message_edited")
        contact = db.get(Contact, contact_id)
        if draft.contact_id != contact_id or not contact or draft.recipients != [contact.email]:
            problems.append("recipient_changed")
    if problems:
        raise ApiError(409, "deal_disclosure_invalid", "The deal disclosure is no longer valid; redraft it",
                       {"reasons": problems})


# ---------------------------------------------------------------- historical deals


def deal_out(d: HistoricalDeal) -> dict[str, Any]:
    return {**d.data, "id": d.id, "created_at": d.created_at, "updated_at": d.updated_at}


def deal_columns(body: HistoricalDealIn) -> dict[str, Any]:
    return {"status": body.status, "buyer_name": body.buyer_name, "buyer_company_id": body.buyer_company_id,
            "target_name": body.target_name, "source_url": body.source_url, "announced_on": body.announced_on,
            "as_of": utc(body.as_of), "data": body.model_dump(mode="json")}


def check_deal_refs(db: Session, body: HistoricalDealIn) -> None:
    require(db, Company, body.buyer_company_id, "buyer_company_id")
    require(db, Company, body.target_company_id, "target_company_id")


def deal_key(d: dict[str, Any]) -> tuple[str, ...]:
    return d["source_url"], d["buyer_name"].casefold(), d["target_name"].casefold(), d["status"]


@router.get("/historical-deals", response_model=DealPage)
def list_historical_deals(status: DealStatus | None = None, buyer: str | None = Query(None, max_length=300),
                          known_as_of: AwareDatetime | None = None, limit: int = Query(50, ge=1, le=500),
                          offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    query = select(HistoricalDeal)
    if status:
        query = query.where(HistoricalDeal.status == status)
    if buyer:
        query = query.where(func.lower(HistoricalDeal.buyer_name).contains(buyer.lower(), autoescape=True))
    if known_as_of:
        query = query.where(HistoricalDeal.as_of <= utc(known_as_of))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(HistoricalDeal.announced_on.desc(), HistoricalDeal.created_at.desc())
                       .limit(limit).offset(offset))
    return {"items": [deal_out(d) for d in items], "total": total}


@router.post("/historical-deals", response_model=HistoricalDealOut, status_code=201)
def create_historical_deal(body: HistoricalDealIn, db: Session = Depends(get_db)):
    check_deal_refs(db, body)
    deal = HistoricalDeal(**deal_columns(body))
    db.add(deal)
    db.commit()
    return deal_out(deal)


async def csv_body(request: Request) -> bytes:
    if request.headers.get("content-type", "").split(";")[0].strip() not in ("text/csv", "text/plain"):
        raise ApiError(415, "unsupported_media_type", "Send the CSV as text/csv")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_CSV_BYTES:
            raise ApiError(413, "payload_too_large", f"CSV exceeds {MAX_CSV_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/historical-deals/import", status_code=201)
def import_historical_deals(raw: bytes = Depends(csv_body), db: Session = Depends(get_db)):
    """All-or-nothing: any invalid row rejects the whole file. Exact duplicates are skipped and reported."""
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        header = set(reader.fieldnames or [])
        fields = set(HistoricalDealIn.model_fields)
        required = {n for n, f in HistoricalDealIn.model_fields.items() if f.is_required()}
        if header - fields or required - header:
            raise ApiError(422, "invalid_csv_header", "CSV header does not match the historical deal schema",
                           {"unknown": sorted(header - fields), "missing": sorted(required - header)})
        rows, errors = [], []
        for count, row in enumerate(reader, start=1):
            line = reader.line_num
            if count > MAX_CSV_ROWS:
                raise ApiError(413, "too_many_rows", f"CSV exceeds {MAX_CSV_ROWS} rows")
            if None in row:
                errors.append({"row": line, "errors": [{"loc": [], "message": "more cells than header columns"}]})
                continue
            try:
                body = HistoricalDealIn.model_validate({k: v.strip() for k, v in row.items() if v and v.strip()})
                check_deal_refs(db, body)
                rows.append((line, body))
            except ValidationError as exc:
                errors.append({"row": line, "errors": validation_details(exc)})
            except ApiError as exc:
                errors.append({"row": line, "errors": [{"loc": [], "message": exc.message}]})
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ApiError(422, "invalid_csv", f"Could not parse CSV: {exc}")
    if errors:
        raise ApiError(422, "invalid_csv_rows", f"{len(errors)} rows failed validation; nothing was imported",
                       errors[:MAX_ROW_ERRORS])
    if not rows:
        raise ApiError(422, "empty_csv", "CSV has no data rows")
    urls = {b.source_url for _, b in rows}
    seen = {deal_key(d) for d in db.scalars(select(HistoricalDeal.data).where(HistoricalDeal.source_url.in_(urls)))}
    imported, skipped = [], []
    for line, body in rows:
        key = deal_key(body.model_dump(mode="json"))
        if key in seen:
            skipped.append(line)
            continue
        seen.add(key)
        deal = HistoricalDeal(**deal_columns(body))
        db.add(deal)
        imported.append(deal)
    record_activity(db, "historical_deals.imported", f"{len(imported)} historical deals imported", None,
                    imported=len(imported), skipped=len(skipped))
    db.commit()
    return {"imported": len(imported), "skipped_duplicate_rows": skipped, "ids": [d.id for d in imported]}


@router.patch("/historical-deals/{deal_id}", response_model=HistoricalDealOut)
def update_historical_deal(deal_id: uuid.UUID, patch: dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    deal = get_or_404(db, HistoricalDeal, deal_id)
    body = merge(HistoricalDealIn, deal.data, patch)
    check_deal_refs(db, body)
    for key, value in deal_columns(body).items():
        setattr(deal, key, value)
    db.commit()
    return deal_out(deal)


@router.delete("/historical-deals/{deal_id}")
def delete_historical_deal(deal_id: uuid.UUID, db: Session = Depends(get_db)):
    db.delete(get_or_404(db, HistoricalDeal, deal_id))
    db.commit()
    return {"id": deal_id, "deleted": True}


# ---------------------------------------------------------------- analytics and replay


@router.get("/deals/analytics")
def deal_analytics(db: Session = Depends(get_db)):
    """Sample counts only. No rates, probabilities or causal attributions are computed."""
    def grouped(column) -> dict[str, int]:
        return {str(k): n for k, n in db.execute(select(column, func.count()).group_by(column)).all() if k is not None}

    deal_values = db.scalars(select(HistoricalDeal.data)).all()
    lost = db.scalar(select(func.count()).select_from(OutcomeEvent).where(OutcomeEvent.milestone == Milestone.lost))
    supported = {str(k): n for k, n in db.execute(
        select(OutcomeEvent.reason_category, func.count()).where(
            OutcomeEvent.milestone == Milestone.lost, OutcomeEvent.reason_category.is_not(None))
        .group_by(OutcomeEvent.reason_category)).all()}
    return {
        "historical_deals": {"total": len(deal_values), "by_status": grouped(HistoricalDeal.status),
                             "value_disclosed": sum(d["value_amount"] is not None for d in deal_values),
                             "value_undisclosed": sum(d["value_amount"] is None for d in deal_values)},
        "opportunities": {"total": db.scalar(select(func.count()).select_from(Opportunity)),
                          "by_status": grouped(Opportunity.status),
                          "by_latest_milestone": grouped(Opportunity.latest_milestone)},
        "outcome_events": db.scalar(select(func.count()).select_from(OutcomeEvent)),
        "failure_reasons": {"lost_events": lost, "supported": supported,
                            "without_supported_reason": lost - sum(supported.values())},
        "notes": ["Counts of recorded events only; no success probabilities or causal claims.",
                  "A reason counts only when stated or confirmed with evidence; no reply is never a reason."],
    }


@router.post("/simulations/replay")
def replay(body: ReplayIn, db: Session = Depends(get_db)):
    """Re-evaluate saved match snapshots taken at or before as_of with the current policy. Read-only."""
    as_of = utc(body.as_of)
    if as_of > utcnow():
        raise ApiError(422, "future_as_of", "as_of must not be in the future")
    query = select(MatchRun).where(MatchRun.created_at <= as_of).order_by(MatchRun.created_at)
    if body.company_id:
        query = query.where(MatchRun.company_id == body.company_id)
    runs = db.scalars(query).all()
    deals_known = db.scalar(select(func.count()).select_from(HistoricalDeal).where(HistoricalDeal.as_of <= as_of))
    results = db.scalars(select(MatchResult).where(MatchResult.run_id.in_([r.id for r in runs])))
    by_run: dict[uuid.UUID, list[MatchResult]] = {}
    for res in results:
        by_run.setdefault(res.run_id, []).append(res)
    opps = {(o.company_id, o.mandate_id): o for o in db.scalars(
        select(Opportunity).where(Opportunity.company_id.in_({r.company_id for r in runs})))}
    events: dict[uuid.UUID, list[OutcomeEvent]] = {}
    for e in db.scalars(select(OutcomeEvent).where(OutcomeEvent.opportunity_id.in_([o.id for o in opps.values()]))):
        events.setdefault(e.opportunity_id, []).append(e)

    replayed, counts = [], Counter()
    for run in runs:
        snap, items = run.snapshot, []
        profile = snap["profile"]
        for res in by_run.get(run.id, []):
            m = next(x for x in snap["mandates"] if x["id"] == str(res.mandate_id))
            now = evaluate(snap["facts"], profile, profile["conditions"] if profile else [], m)
            opp = opps.get((run.company_id, res.mandate_id))
            observed = sorted((e for e in events.get(opp.id, []) if utc(e.occurred_at) >= utc(run.created_at))
                              if opp else [], key=lambda e: e.occurred_at)
            items.append({"mandate_id": res.mandate_id, "saved_status": res.status, "replayed_status": now["status"],
                          "changed": now["status"] != res.status, "comparables_known": comparables(db, m, as_of),
                          "observed_milestones": [{"milestone": e.milestone, "occurred_at": e.occurred_at}
                                                  for e in observed]})
            counts["results"] += 1
            counts["changed"] += now["status"] != res.status
            counts["labeled"] += bool(observed)
        replayed.append({"run_id": run.id, "company_id": run.company_id, "created_at": run.created_at,
                         "saved_policy_version": run.policy_version, "results": items})
    counts.update(runs=len(runs), historical_deals_known=deals_known)
    reasons = []
    if not runs:
        reasons.append("no saved match-run snapshots at or before as_of")
    if not deals_known and not counts["labeled"]:
        reasons.append("no historical deals known at as_of and no recorded outcomes for replayed results")
    if reasons:
        return {"status": "unavailable", "as_of": as_of, "reasons": reasons, "counts": dict(counts)}
    return {"status": "completed", "as_of": as_of, "policy_version": POLICY_VERSION, "counts": dict(counts),
            "runs": replayed,
            "notes": ["Inputs are the saved snapshots; deals are filtered by their as_of.",
                      "Observed milestones are labels, not inputs. Unpursued results have no counterfactual."]}
