"""Persisted tables for the research/enrichment subsystem.

These share ``Base``/``metadata`` with ``permetheus.models``, so
``init_db()`` (``Base.metadata.create_all(engine)`` in ``permetheus.db``)
picks them up automatically -- no edit to ``models.py`` or ``db.py`` needed.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Date, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, IdMixin, JsonType, enum_col, utcnow

# Job.kind values this subsystem understands. "company.enrich" already exists
# in models/companies.py (auto-created on company intake); "discovery.run" is
# introduced here for the PRH bulk-import pipeline.
JOB_KIND_ENRICH = "company.enrich"
JOB_KIND_DISCOVERY = "discovery.run"


class ResearchRunStatus(StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ResearchRun(IdMixin, Base):
    """One coverage ledger per enrichment attempt: what was checked, what
    stayed unknown, what was blocked, and what went wrong -- never a
    completeness claim beyond what was actually verified."""

    __tablename__ = "research_runs"

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    # The job's fencing token (``Job.attempts``) when this run started. The
    # cancel route only marks the run of the attempt that currently holds the job.
    attempt: Mapped[int] = mapped_column(default=0)
    status: Mapped[ResearchRunStatus] = mapped_column(enum_col(ResearchRunStatus), default=ResearchRunStatus.running)
    checked: Mapped[list[str]] = mapped_column(JsonType, default=list)
    missing: Mapped[list[str]] = mapped_column(JsonType, default=list)
    blocked: Mapped[list[str]] = mapped_column(JsonType, default=list)
    errors: Mapped[list[str]] = mapped_column(JsonType, default=list)
    recommendations: Mapped[list[str]] = mapped_column(JsonType, default=list)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class DiscoveryRunStatus(StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class DiscoveryRun(IdMixin, Base):
    """Progress/checkpoint for one bounded PRH registry import. ``current_page``
    is the durable checkpoint: a crashed or reclaimed worker resumes the next
    unfetched page instead of restarting the whole import."""

    __tablename__ = "discovery_runs"

    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    status: Mapped[DiscoveryRunStatus] = mapped_column(enum_col(DiscoveryRunStatus), default=DiscoveryRunStatus.running)
    params: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    current_page: Mapped[int] = mapped_column(default=0)
    total_results: Mapped[int | None] = mapped_column(default=None)
    companies_seen: Mapped[int] = mapped_column(default=0)
    companies_imported: Mapped[int] = mapped_column(default=0)
    errors: Mapped[list[str]] = mapped_column(JsonType, default=list)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class DiscoverySchedule(IdMixin, Base):
    """Operator-configured daily discovery schedule. Treated as a singleton:
    callers read/update the first row, creating it on first use. Absent a row
    (or with ``enabled=False``), no discovery job is ever auto-enqueued."""

    __tablename__ = "discovery_schedules"

    enabled: Mapped[bool] = mapped_column(default=False)
    hour_utc: Mapped[int] = mapped_column(default=3)
    window_days: Mapped[int] = mapped_column(default=7)
    last_run_date: Mapped[date | None] = mapped_column(Date, default=None)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
