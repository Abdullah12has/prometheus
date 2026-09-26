import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_session
from .db import get_db, get_or_404, record_activity
from .errors import ApiError
from .identity import normalize_email, normalize_phone
from .models import Company, Contact, ContactRole, PersonRole, Verification
from .provenance import require_source

router = APIRouter(prefix="/api", tags=["contacts"], dependencies=[Depends(require_session)])


class ContactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    title: str | None = Field(None, max_length=200)
    person_role: PersonRole = PersonRole.other
    contact_role: ContactRole = ContactRole.seller
    email: str | None = None
    phone: str | None = None
    source_id: uuid.UUID | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        return normalize_email(v) if v else None

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        return normalize_phone(v) if v else None


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    company_id: uuid.UUID
    name: str
    title: str | None
    person_role: PersonRole
    contact_role: ContactRole
    email: str | None
    phone: str | None
    verification: Verification
    source_id: uuid.UUID | None
    created_at: datetime


@router.post("/companies/{company_id}/contacts", response_model=ContactOut, status_code=201)
def add_contact(company_id: uuid.UUID, body: ContactIn, db: Session = Depends(get_db)):
    get_or_404(db, Company, company_id)
    if body.source_id:
        require_source(db, body.source_id)
    if body.email and db.scalar(select(Contact.id).where(Contact.company_id == company_id, Contact.email == body.email)):
        raise ApiError(409, "contact_exists", "A contact with this email already exists for the company")
    contact = Contact(company_id=company_id, **body.model_dump())
    db.add(contact)
    record_activity(db, "contact.added", f"Contact {body.name} added", company_id)
    db.commit()
    return contact


@router.delete("/contacts/{contact_id}")
def delete_contact(contact_id: uuid.UUID, db: Session = Depends(get_db)):
    contact = get_or_404(db, Contact, contact_id)
    record_activity(db, "contact.deleted", "Contact removed", contact.company_id)
    db.delete(contact)
    db.commit()
    return {"id": contact_id, "deleted": True}
