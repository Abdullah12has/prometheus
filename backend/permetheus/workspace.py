"""Health, dashboard counts, connector status, jobs and activity feed."""

import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AliasPath, BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .auth import require_session
from .config import Settings
from .db import get_db
from .models import Activity, Company, Contact, Job, JobState, utcnow

public = APIRouter(prefix="/api", tags=["workspace"])
router = APIRouter(prefix="/api", tags=["workspace"], dependencies=[Depends(require_session)])


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: str
    state: JobState
    company_id: uuid.UUID | None
    attempts: int
    progress: dict[str, Any] | None = Field(default=None, validation_alias=AliasPath("payload", "progress"))
    available_at: datetime
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    company_id: uuid.UUID | None
    kind: str
    summary: str
    actor: str
    payload: dict[str, Any]
    created_at: datetime


class ConnectorOut(BaseModel):
    id: str
    label: str
    configured: bool
    implemented: bool
    missing: list[str]
    note: str


def database_ok(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False


@public.get("/health")
def health(db: Session = Depends(get_db)):
    ok = database_ok(db)
    return {"status": "ok" if ok else "degraded", "database": "ok" if ok else "unavailable"}


# (id, label, env fields, note). Implemented flags flip as each connector ships.
CONNECTORS = [
    ("registry_fi", "Finnish trade register", [], "PRH daily bulk import and targeted searches. Manage country imports in Companies."),
    ("registry_ch", "Swiss commercial register", [], "Zefix public linked data via LINDAS. Resumable country import in Companies."),
    ("registry_de", "German LEI records", [], "GLEIF public records for German-address entities with an LEI; a subset of German companies."),
    ("gmail", "Gmail", ["google_client_id", "google_client_secret"],
     "Configure Google OAuth credentials, then connect your mailbox in Outreach."),
    ("llm", "Language model", ["litellm_base_url", "litellm_api_key", "litellm_model"],
     "Set LITELLM_BASE_URL, LITELLM_API_KEY and LITELLM_MODEL."),
    ("web_search", "Web search", ["searxng_url"], "Set SEARXNG_URL to a local SearXNG instance with JSON output enabled."),
    ("phone", "Phone calling", ["twilio_account_sid", "twilio_auth_token", "twilio_from_number", "public_base_url"],
     "Deferred. Needs a funded carrier account, a voice number and public HTTPS ingress. Browser voice does not need this."),
]


def connector_status(settings: Settings) -> list[ConnectorOut]:
    out = []
    for cid, label, fields, note in CONNECTORS:
        missing = [f.upper() for f in fields if getattr(settings, f) is None]
        out.append(ConnectorOut(id=cid, label=label, configured=not missing, implemented=cid != "phone", missing=missing, note=note))
    missing = [name for name, path in (("ASR_BINARY", settings.asr_binary), ("ASR_MODEL_PATH", settings.asr_model_path), ("TTS_PYTHON", settings.tts_python)) if path is None or not path.is_file()]
    out.append(ConnectorOut(id="voice", label="Local browser voice", configured=not missing, implemented=True, missing=missing, note="Local speech recognition and saved voice agents; microphone permission is requested in the browser."))
    return out


@router.get("/settings/status")
def settings_status(request: Request, db: Session = Depends(get_db)):
    settings: Settings = request.app.state.settings
    from .mail import GmailAccount, Suppression
    account = db.scalar(select(GmailAccount).limit(1))
    stopped = db.scalar(select(Suppression.id).where(Suppression.kind == "global", Suppression.value == "*"))
    email = "stopped" if stopped else "approval_required" if account and not account.needs_reauth else "disconnected"
    return {
        "database": "ok" if database_ok(db) else "unavailable",
        "auth": {"configured": settings.admin_password is not None},
        "outbound_dispatch": {"email": email, "phone": "disabled"},
        "connectors": connector_status(settings),
    }


def _counts(db: Session, column) -> dict[str, int]:
    return {str(k): v for k, v in db.execute(select(column, func.count()).group_by(column))}


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    return {
        "companies": db.scalar(select(func.count()).select_from(Company)),
        "companies_by_status": _counts(db, Company.status),
        "companies_by_seller_intent": _counts(db, Company.seller_intent),
        "contacts": db.scalar(select(func.count()).select_from(Contact)),
        "jobs_by_state": _counts(db, Job.state),
        "activities_last_7_days": db.scalar(
            select(func.count()).select_from(Activity).where(Activity.created_at >= utcnow() - timedelta(days=7))
        ),
    }


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(company_id: uuid.UUID | None = None, state: JobState | None = None,
              limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    q = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if company_id:
        q = q.where(Job.company_id == company_id)
    if state:
        q = q.where(Job.state == state)
    return db.scalars(q).all()


@router.get("/activities", response_model=list[ActivityOut])
def list_activities(company_id: uuid.UUID | None = None, limit: int = Query(50, ge=1, le=200),
                    db: Session = Depends(get_db)):
    q = select(Activity).order_by(Activity.created_at.desc()).limit(limit)
    if company_id:
        q = q.where(Activity.company_id == company_id)
    return db.scalars(q).all()
