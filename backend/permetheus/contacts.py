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
from .provenance import SourceOut, require_source

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
    source: SourceOut | None = None
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


class ContactPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(None, min_length=1, max_length=300)
    title: str | None = Field(None, max_length=200)
    person_role: PersonRole = None
    contact_role: ContactRole = None
    email: str | None = None
    phone: str | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, value):
        return normalize_email(value) if value else None

    @field_validator("phone")
    @classmethod
    def _phone(cls, value):
        return normalize_phone(value) if value else None


class ContactVerification(BaseModel):
    basis: str = Field(min_length=5, max_length=2000)
    source_id: uuid.UUID | None = None


@router.patch("/contacts/{contact_id}", response_model=ContactOut)
def edit_contact(contact_id: uuid.UUID, body: ContactPatch, db: Session = Depends(get_db)):
    contact = get_or_404(db, Contact, contact_id)
    changes = body.model_dump(exclude_unset=True)
    email = changes.get("email")
    if email and db.scalar(select(Contact.id).where(Contact.company_id == contact.company_id, Contact.email == email, Contact.id != contact_id)):
        raise ApiError(409, "contact_exists", "A contact with this email already exists for the company")
    if any(key in changes and changes[key] != getattr(contact, key) for key in ("name", "email", "phone", "person_role", "contact_role")):
        contact.verification = Verification.unverified
    for key, value in changes.items():
        setattr(contact, key, value)
    record_activity(db, "contact.updated", "Contact updated; changed identity needs verification", contact.company_id)
    db.commit()
    return contact


@router.post("/contacts/{contact_id}/verify", response_model=ContactOut)
def verify_contact(contact_id: uuid.UUID, body: ContactVerification, db: Session = Depends(get_db)):
    contact = get_or_404(db, Contact, contact_id)
    if not contact.email and not contact.phone:
        raise ApiError(422, "missing_contact_channel", "Add an email address or phone number before verification")
    if body.source_id:
        require_source(db, body.source_id)
        contact.source_id = body.source_id
    contact.verification = Verification.verified
    record_activity(db, "contact.verified", "Contact association verified by operator", contact.company_id, basis=body.basis)
    db.commit()
    return contact
