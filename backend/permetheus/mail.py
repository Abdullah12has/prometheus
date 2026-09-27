"""Personal Gmail connection, tracked-thread sync and approval-gated outreach.

OAuth tokens are Fernet-encrypted with a key derived from SESSION_SECRET and never leave this module:
not to the browser, not to a model. Only threads Permetheus itself started are read and persisted.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, parseaddr
from enum import StrEnum
from html import unescape
from string import Formatter
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .auth import require_session
from .db import get_db, get_or_404, record_activity
from .errors import ApiError
from .identity import normalize_email
from .models import (
    Base, Company, CompanyStatus, Contact, FinancialMetric, FinancialObservation, IdMixin, IntentStatement,
    JsonType, ReviewStatus, SellerIntent, Source, SourceKind, SpeakerAuthority, Verification, enum_col, utcnow,
)

SCOPES = ("https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
OAUTH_COOKIE = "permetheus_oauth_state"
OAUTH_TTL = timedelta(minutes=10)
STALE_SENDING = timedelta(minutes=5)
MAX_HISTORY_PAGES = 50
MAX_RESYNC_THREADS = 500
TEMPLATE_FIELDS = {"company_name", "contact_name"}

router = APIRouter(prefix="/api", tags=["mail"], dependencies=[Depends(require_session)])
oauth_router = APIRouter(prefix="/oauth/google", tags=["mail"])


# ---------------------------------------------------------------- tables

class DraftStatus(StrEnum):
    draft = "draft"
    approved = "approved"
    sending = "sending"
    sent = "sent"
    delivery_unknown = "delivery_unknown"
    failed = "failed"


class ReplyIntent(StrEnum):
    interested = "interested"
    no = "no"
    optout = "optout"
    unclear = "unclear"


class GmailAccount(IdMixin, Base):
    __tablename__ = "gmail_accounts"
    email: Mapped[str] = mapped_column(String(320), unique=True)
    token_ciphertext: Mapped[str] = mapped_column(Text)
    scopes: Mapped[str] = mapped_column(String(500))
    history_id: Mapped[str | None] = mapped_column(String(32))
    needs_reauth: Mapped[bool] = mapped_column(default=False)
    last_sync_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(String(500))


class OAuthState(IdMixin, Base):
    __tablename__ = "gmail_oauth_states"
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    binding_hash: Mapped[str] = mapped_column(String(64))
    verifier_ciphertext: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(index=True)
    used_at: Mapped[datetime | None]


class Suppression(IdMixin, Base):
    __tablename__ = "outreach_suppressions"
    __table_args__ = (UniqueConstraint("kind", "value"),)
    kind: Mapped[str] = mapped_column(String(16))  # email | company | channel | global
    value: Mapped[str] = mapped_column(String(320))
    reason: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | optout


class Conversation(IdMixin, Base):
    __tablename__ = "outreach_conversations"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    gmail_thread_id: Mapped[str] = mapped_column(String(64), unique=True)
    subject: Mapped[str] = mapped_column(String(998))
    status: Mapped[str] = mapped_column(String(32), default="awaiting_reply")
    # Ingestion time, not the provider date: an approval older than this has not seen the reply.
    last_inbound_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class MailMessage(IdMixin, Base):
    __tablename__ = "outreach_messages"
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("outreach_conversations.id", ondelete="CASCADE"), index=True)
    gmail_message_id: Mapped[str] = mapped_column(String(64), unique=True)
    rfc_message_id: Mapped[str | None] = mapped_column(String(998))
    direction: Mapped[str] = mapped_column(String(8))  # inbound | outbound
    sender: Mapped[str] = mapped_column(String(320))
    recipients: Mapped[list[str]] = mapped_column(JsonType, default=list)
    subject: Mapped[str] = mapped_column(String(998))
    body_text: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime]
    proposed_intent: Mapped[ReplyIntent | None] = mapped_column(enum_col(ReplyIntent))
    proposed_reason: Mapped[str | None] = mapped_column(String(300))
    confirmed_intent: Mapped[ReplyIntent | None] = mapped_column(enum_col(ReplyIntent))
    confirmed_at: Mapped[datetime | None]


class Sequence(IdMixin, Base):
    __tablename__ = "outreach_sequences"
    name: Mapped[str] = mapped_column(String(200))
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JsonType)
    version: Mapped[int] = mapped_column(default=1)
    paused: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class TemplateApproval(IdMixin, Base):
    __tablename__ = "outreach_template_approvals"
    sequence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("outreach_sequences.id", ondelete="CASCADE"), index=True)
    sequence_version: Mapped[int]
    step_index: Mapped[int]
    template_hash: Mapped[str] = mapped_column(String(64))
    company_ids: Mapped[list[str]] = mapped_column(JsonType)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class Enrollment(IdMixin, Base):
    __tablename__ = "outreach_enrollments"
    sequence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("outreach_sequences.id", ondelete="CASCADE"), index=True)
    # A running enrollment keeps the steps it started with; editing the sequence does not migrate it.
    sequence_version: Mapped[int]
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JsonType)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("outreach_conversations.id", ondelete="SET NULL"))
    step_index: Mapped[int] = mapped_column(default=0)
    state: Mapped[str] = mapped_column(String(32), default="active")  # active | awaiting_review | stopped | done
    next_run_at: Mapped[datetime] = mapped_column(index=True)
    pending_draft_id: Mapped[uuid.UUID | None]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class OutreachDraft(IdMixin, Base):
    __tablename__ = "outreach_drafts"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("outreach_conversations.id", ondelete="SET NULL"))
    enrollment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("outreach_enrollments.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32), default="manual")  # manual | sequence | missing_fields
    recipients: Mapped[list[str]] = mapped_column(JsonType)
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    disclosure: Mapped[dict[str, Any]] = mapped_column(default=dict)
    version: Mapped[int] = mapped_column(default=1)
    status: Mapped[DraftStatus] = mapped_column(enum_col(DraftStatus), default=DraftStatus.draft)
    approved_hash: Mapped[str | None] = mapped_column(String(64))
    approved_version: Mapped[int | None]
    approved_at: Mapped[datetime | None]
    approval_expires_at: Mapped[datetime | None]
    template_approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("outreach_template_approvals.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Dispatch(IdMixin, Base):
    """Send ledger: one row per approved draft version, claimed before the provider is called."""
    __tablename__ = "outreach_dispatches"
    __table_args__ = (UniqueConstraint("draft_id", "draft_version"),)
    draft_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("outreach_drafts.id", ondelete="CASCADE"), index=True)
    draft_version: Mapped[int]
    content_hash: Mapped[str] = mapped_column(String(64))
    rfc_message_id: Mapped[str] = mapped_column(String(998), unique=True)
    state: Mapped[DraftStatus] = mapped_column(enum_col(DraftStatus))
    gmail_message_id: Mapped[str | None] = mapped_column(String(64))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(String(500))
    claimed_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]


# ---------------------------------------------------------------- helpers

def aware(dt: datetime | None) -> datetime | None:
    # SQLite drops tzinfo on read; every stored value is UTC.
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _secret(settings: Any, name: str) -> str | None:
    value = getattr(settings, name, None)
    return value.get_secret_value() if hasattr(value, "get_secret_value") else value


def _oauth_config(settings: Any) -> tuple[str, str, str]:
    cid, secret = getattr(settings, "google_client_id", None), _secret(settings, "google_client_secret")
    redirect = getattr(settings, "google_redirect_uri", None)
    if not (cid and secret and redirect):
        raise ApiError(503, "gmail_not_configured", "Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI")
    return cid, secret, redirect


def _fernet(settings: Any) -> Fernet:
    secret = _secret(settings, "session_secret")
    if not secret or len(secret) < 32:
        raise ApiError(503, "session_secret_missing", "Set SESSION_SECRET (32+ characters) before connecting Gmail")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"permetheus gmail tokens v1").derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def _web_url(settings: Any) -> str:
    return settings.web_origins.split(",")[0].strip().rstrip("/")


def _http(app: FastAPI) -> httpx.Client:
    # ponytail: one process-lifetime client; tests inject app.state.gmail_http with a MockTransport.
    if getattr(app.state, "gmail_http", None) is None:
        app.state.gmail_http = httpx.Client(timeout=httpx.Timeout(20, connect=5))
    return app.state.gmail_http


class GmailError(Exception):
    def __init__(self, status: int, reason: str, maybe_accepted: bool = False):
        super().__init__(f"{status} {reason}")
        # maybe_accepted: the request may have reached Google, so a send may have happened.
        self.status, self.reason, self.maybe_accepted = status, reason, maybe_accepted


def _reason(r: httpx.Response) -> str:
    try:
        err = r.json().get("error")
    except (ValueError, AttributeError):
        return "http_error"
    return err if isinstance(err, str) else str((err or {}).get("status") or r.status_code)


def _google(http: httpx.Client, method: str, url: str, token: str | None = None, **kw: Any) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = http.request(method, url, headers=headers, **kw)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        raise GmailError(0, "unreachable") from exc  # no request bytes left this machine
    except httpx.HTTPError as exc:
        raise GmailError(0, "no_response", maybe_accepted=True) from exc
    if r.status_code >= 400:
        raise GmailError(r.status_code, _reason(r), maybe_accepted=r.status_code >= 500)
    try:
        return r.json() if r.content else {}
    except ValueError as exc:
        raise GmailError(r.status_code, "invalid_response", maybe_accepted=True) from exc


def _gmail(app: FastAPI, token: str, method: str, path: str, **kw: Any) -> dict:
    return _google(_http(app), method, GMAIL_API + path, token=token, **kw)


def _unavailable(exc: GmailError) -> ApiError:
    if exc.status == 429:
        return ApiError(429, "gmail_rate_limited", "Gmail rate limit reached; retry later")
    return ApiError(503, "gmail_unavailable", f"Gmail request failed ({exc})")


def _tokens(app: FastAPI, account: GmailAccount) -> dict:
    try:
        return json.loads(_fernet(app.state.settings).decrypt(account.token_ciphertext.encode()))
    except InvalidToken as exc:
        raise ApiError(409, "gmail_reauth_required", "Stored Gmail tokens cannot be decrypted; reconnect Gmail") from exc


def _store_tokens(app: FastAPI, account: GmailAccount, tokens: dict) -> None:
    account.token_ciphertext = _fernet(app.state.settings).encrypt(json.dumps(tokens).encode()).decode()


def _connected(db: Session) -> GmailAccount:
    account = db.scalar(select(GmailAccount).limit(1))
    if account is None:
        raise ApiError(409, "gmail_not_connected", "Connect Gmail first")
    if account.needs_reauth:
        raise ApiError(409, "gmail_reauth_required", account.last_error or "Reconnect Gmail")
    return account


def access_token(app: FastAPI, db: Session, account: GmailAccount) -> str:
    tokens = _tokens(app, account)
    if tokens.get("expires_at", 0) > time.time() + 60:
        return tokens["access_token"]
    cid, secret, _ = _oauth_config(app.state.settings)
    try:
        fresh = _google(_http(app), "POST", TOKEN_URL, data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": cid, "client_secret": secret,
        })
    except GmailError as exc:
        if exc.reason != "invalid_grant":
            raise _unavailable(exc) from exc
        # Revoked, expired (7-day Testing limit) or password change: only a reconnect helps.
        account.needs_reauth, account.last_error = True, "Google rejected the refresh token; reconnect Gmail"
        db.commit()
        raise ApiError(409, "gmail_reauth_required", account.last_error) from exc
    tokens.update(access_token=fresh["access_token"], expires_at=time.time() + int(fresh.get("expires_in", 3600)))
    if fresh.get("refresh_token"):
        tokens["refresh_token"] = fresh["refresh_token"]
    _store_tokens(app, account, tokens)
    db.commit()
    return tokens["access_token"]


# ---------------------------------------------------------------- OAuth

@router.post("/gmail/connect")
def connect(request: Request, response: Response, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    cid, _, redirect = _oauth_config(settings)
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).hostname != urlsplit(redirect).hostname:
        raise ApiError(409, "oauth_host_mismatch", f"Open {_web_url(settings)} before connecting Gmail; the browser and OAuth callback must use the same hostname.")
    fernet = _fernet(settings)
    state, binding, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    db.execute(delete(OAuthState).where(OAuthState.expires_at <= utcnow()))
    db.add(OAuthState(state_hash=_sha(state), binding_hash=_sha(binding), expires_at=utcnow() + OAUTH_TTL,
                      verifier_ciphertext=fernet.encrypt(verifier.encode()).decode()))
    db.commit()
    # Lax, not Strict: the callback is a top-level navigation back from accounts.google.com.
    response.set_cookie(OAUTH_COOKIE, binding, max_age=int(OAUTH_TTL.total_seconds()), httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path=oauth_router.prefix)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "false", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return {"authorization_url": f"{AUTH_URL}?{query}", "expires_in": int(OAUTH_TTL.total_seconds())}


@oauth_router.get("/callback")
def callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
             db: Session = Depends(get_db)):
    app, settings = request.app, request.app.state.settings

    def done(result: str, reason: str | None = None, clear: bool = True) -> RedirectResponse:
        query = urlencode({"gmail": result, **({"reason": reason} if reason else {})})
        resp = RedirectResponse(f"{_web_url(settings)}/outreach?{query}", status_code=303)
        if clear:
            resp.delete_cookie(OAUTH_COOKIE, path=oauth_router.prefix)
        return resp

    binding = request.cookies.get(OAUTH_COOKIE)
    row = state and binding and db.scalar(select(OAuthState).where(OAuthState.state_hash == _sha(state)))
    # A forged callback must not clear the binding of a genuine in-flight connect.
    if not row or row.used_at or aware(row.expires_at) <= utcnow() or not hmac.compare_digest(row.binding_hash, _sha(binding)):
        return done("error", "invalid_state", clear=False)
    # One-time: the conditional update wins exactly once even under concurrent callbacks.
    if db.execute(update(OAuthState).where(OAuthState.id == row.id, OAuthState.used_at.is_(None))
                  .values(used_at=utcnow())).rowcount != 1:
        db.rollback()
        return done("error", "invalid_state", clear=False)
    db.commit()
    if error or not code:
        return done("error", "denied")

    cid, secret, redirect = _oauth_config(settings)
    http = _http(app)
    try:
        verifier = _fernet(settings).decrypt(row.verifier_ciphertext.encode()).decode()
        tokens = _google(http, "POST", TOKEN_URL, data={
            "code": code, "client_id": cid, "client_secret": secret, "redirect_uri": redirect,
            "grant_type": "authorization_code", "code_verifier": verifier,
        })
        granted = set(tokens.get("scope", "").split())
        if not set(SCOPES) <= granted or not tokens.get("refresh_token"):
            _google(http, "POST", REVOKE_URL, data={"token": tokens.get("refresh_token") or tokens.get("access_token", "")})
            return done("error", "scope_missing" if not set(SCOPES) <= granted else "no_refresh_token")
        profile = _google(http, "GET", GMAIL_API + "/profile", token=tokens["access_token"])
    except (GmailError, InvalidToken, KeyError):
        return done("error", "token_exchange_failed")

    email = profile["emailAddress"].lower()
    account = db.scalar(select(GmailAccount).limit(1))  # one mailbox per workspace
    if account is not None and account.email != email:
        # Thread IDs and Message-IDs only resolve in the mailbox that sent them; a switch would orphan them.
        if db.scalar(select(Conversation.id).limit(1)) or db.scalar(select(Dispatch.id).limit(1)):
            try:
                _google(http, "POST", REVOKE_URL, data={"token": tokens["refresh_token"]})
            except GmailError:
                pass  # never stored; Google access can also be removed in the account settings
            return done("error", "mailbox_mismatch")
        _revoke(app, account)
        db.delete(account)
        db.flush()
        account = None
    if account is None:
        account = GmailAccount(email=email, history_id=str(profile.get("historyId") or "") or None)
    # Reconnecting the same mailbox keeps its cursor, so replies since the last sync are still read.
    account.scopes, account.needs_reauth, account.last_error = " ".join(sorted(granted)), False, None
    _store_tokens(app, account, {"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
                                 "expires_at": time.time() + int(tokens.get("expires_in", 3600))})
    db.add(account)
    record_activity(db, "gmail.connected", "Gmail connected", None, email=email)
    db.commit()
    return done("connected")


@router.get("/gmail/status")
def gmail_status(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    secret = _secret(settings, "session_secret")
    configured = all((getattr(settings, "google_client_id", None), _secret(settings, "google_client_secret"),
                      getattr(settings, "google_redirect_uri", None), secret and len(secret) >= 32))
    account = db.scalar(select(GmailAccount).limit(1))
    return {
        "configured": configured,
        "connected": account is not None and not account.needs_reauth,
        "email": account and account.email,
        "scopes": account.scopes.split() if account else [],
        "required_scopes": list(SCOPES),
        "needs_reauth": bool(account and account.needs_reauth),
        "last_sync_at": account and account.last_sync_at,
        "last_error": account and account.last_error,
        "has_sync_cursor": bool(account and account.history_id),
    }


@router.post("/gmail/disconnect")
def disconnect(request: Request, db: Session = Depends(get_db)):
    account = db.scalar(select(GmailAccount).limit(1))
    if account is None:
        return {"connected": False, "revoked": False}
    revoked = _revoke(request.app, account)
    # Tokens are erased; the address and cursor stay so retained threads remain tied to this mailbox.
    account.token_ciphertext, account.needs_reauth = "", True
    account.last_error = f"Disconnected; reconnect {account.email} to resume"
    record_activity(db, "gmail.disconnected", "Gmail disconnected", None, revoked=revoked)
    db.commit()
    return {"connected": False, "revoked": revoked}


def _revoke(app: FastAPI, account: GmailAccount) -> bool:
    try:
        _google(_http(app), "POST", REVOKE_URL, data={"token": _tokens(app, account)["refresh_token"]})
        return True
    except (GmailError, ApiError, KeyError):
        return False  # erased locally regardless; Google access can also be removed in the account settings


# ---------------------------------------------------------------- reply parsing and classification

QUOTE_START = re.compile(r"^(on .+ wrote:|.+ kirjoitti:|-+ ?original message ?-+|-+ ?forwarded message ?-+|from: .+)$", re.I)
INTENT_RULES = (
    (ReplyIntent.optout, re.compile(r"unsubscribe|opt[- ]?out|remove me|stop (e-?mailing|contacting)|"
                                    r"do not (contact|e-?mail)|don'?t (contact|e-?mail)|älkää (lähettäkö|ottako)|poista(kaa)? minut", re.I)),
    (ReplyIntent.no, re.compile(r"not interested|no,? thank|not for sale|not selling|ei kiinnosta|"
                                r"emme ole kiinnostuneita|en ole kiinnostunut|ei ole myynnissä", re.I)),
    (ReplyIntent.interested, re.compile(r"\binterested\b|happy to (talk|chat|discuss|meet)|let'?s (talk|discuss|meet)|"
                                        r"tell me more|open to|kiinnostaa|kiinnostunut|kiinnostuneita", re.I)),
)


def new_reply_text(text: str) -> str:
    """Drop quoted history and forwarded blocks so only the new reply is stored and classified."""
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if QUOTE_START.match(stripped):
            break
        if not stripped.startswith(">"):
            kept.append(line)
    return "\n".join(kept).strip()


def classify_reply(text: str) -> tuple[ReplyIntent, str]:
    """Proposal only: a human confirms before seller intent or follow-ups change. Opt-out is checked first."""
    # ponytail: keyword rules; swap in a model call on the single new reply text if accuracy demands it.
    for intent, pattern in INTENT_RULES:
        if match := pattern.search(text):
            return intent, f"matched “{match.group(0)}”"
    return ReplyIntent.unclear, "no rule matched"


def _body_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):
        return ""
    if part.get_content_subtype() == "html":
        text = unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)))
    return new_reply_text(text)[:20000]


# ---------------------------------------------------------------- sync

def _history(app: FastAPI, token: str, start: str) -> tuple[list[tuple[str, str]], str]:
    found, params = [], {"startHistoryId": start, "historyTypes": "messageAdded"}
    for _ in range(MAX_HISTORY_PAGES):
        page = _gmail(app, token, "GET", "/history", params=params)
        found += [(a["message"]["id"], a["message"]["threadId"]) for h in page.get("history", []) for a in h.get("messagesAdded", [])]
        if not page.get("nextPageToken"):
            return found, str(page.get("historyId") or start)
        params = {**params, "pageToken": page["nextPageToken"]}
    raise GmailError(404, "history_too_long")  # bounded: fall back to a tracked-thread resync


def _thread_messages(app: FastAPI, token: str, tid: str) -> list[dict] | None:
    try:
        return _gmail(app, token, "GET", f"/threads/{tid}", params={"format": "minimal"}).get("messages", [])
    except GmailError as exc:
        if exc.status == 404:
            return None
        raise


def _resync(app: FastAPI, token: str, threads: list[str]) -> tuple[list[tuple[str, str]], str]:
    cursor = str(_gmail(app, token, "GET", "/profile")["historyId"])  # taken first so nothing falls between
    return [(m["id"], tid) for tid in threads for m in (_thread_messages(app, token, tid) or [])], cursor


def _stop_enrollments(db: Session, conversation_id: uuid.UUID, state: str, only_active: bool = False) -> None:
    states = ("active",) if only_active else ("active", "awaiting_review")
    db.execute(update(Enrollment).where(Enrollment.conversation_id == conversation_id, Enrollment.state.in_(states)).values(state=state))


def _suppress(db: Session, kind: str, value: str, reason: str, source: str = "manual") -> None:
    if not db.scalar(select(Suppression.id).where(Suppression.kind == kind, Suppression.value == value)):
        db.add(Suppression(kind=kind, value=value, reason=reason, source=source))


def _suppress_optout(db: Session, conv: Conversation, sender: str, reason: str) -> None:
    # "Remove us" from an assistant also covers the contact the thread was addressed to.
    contact = conv.contact_id and db.get(Contact, conv.contact_id)
    for email in {sender, (contact.email or "").strip().lower() if contact else ""} - {""}:
        _suppress(db, "email", email, reason, "optout")


def _ingest(db: Session, account: GmailAccount, conv: Conversation, gm: dict) -> MailMessage | None:
    if "DRAFT" in gm.get("labelIds", []):
        return None
    raw = gm["raw"]
    msg = message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default)
    sender = parseaddr(str(msg.get("From", "")))[1].lower()
    record = MailMessage(
        conversation_id=conv.id, gmail_message_id=gm["id"], rfc_message_id=(str(msg.get("Message-ID", "")).strip() or None),
        direction="outbound" if sender == account.email else "inbound", sender=sender[:320],
        recipients=[addr.lower() for _, addr in getaddresses([str(v) for v in msg.get_all("To", [])])],
        subject=str(msg.get("Subject", ""))[:998], body_text=_body_text(msg),
        sent_at=datetime.fromtimestamp(int(gm.get("internalDate", 0)) / 1000, timezone.utc),
    )
    db.add(record)
    if record.direction == "outbound":
        return record
    automatic = str(msg.get("Auto-Submitted", "no")).lower() != "no" or msg.get("X-Autoreply")
    record.proposed_intent, record.proposed_reason = (
        (ReplyIntent.unclear, "automatic reply") if automatic else classify_reply(record.body_text))
    conv.last_inbound_at = utcnow()
    if record.proposed_intent is ReplyIntent.optout:
        # Automatic and conservative: a false positive only stops mail to this thread's people.
        _suppress_optout(db, conv, sender, f"Opt-out reply in thread {conv.gmail_thread_id}")
        conv.status = "opted_out"
        _stop_enrollments(db, conv.id, "stopped")
    elif conv.status not in ("opted_out", "declined"):  # a later auto-reply never reopens a closed thread
        conv.status = "needs_review"
        _stop_enrollments(db, conv.id, "awaiting_review", only_active=True)
    record_activity(db, "outreach.reply", f"Reply received from {sender}", conv.company_id,
                    conversation_id=str(conv.id), proposed_intent=record.proposed_intent.value)
    return record


def _stored(db: Session, gmail_id: str) -> bool:
    return db.scalar(select(MailMessage.id).where(MailMessage.gmail_message_id == gmail_id)) is not None


def _lock_conversation(db: Session, thread_id: str) -> Conversation | None:
    # Row lock (PostgreSQL) serializes sync and send finalizers writing messages into the same thread.
    return db.scalar(select(Conversation).where(Conversation.gmail_thread_id == thread_id)
                     .with_for_update().execution_options(populate_existing=True))


def _ingest_found(app: FastAPI, db: Session, token: str, account: GmailAccount, found: list[tuple[str, str]]) -> int:
    ingested = 0
    for mid, tid in dict.fromkeys(found):
        if _stored(db, mid):
            continue
        # Looked up per message, not snapshotted, so a thread started by a send during this sync is tracked.
        # Unrelated mailbox content is dropped here, before any body is fetched.
        if db.scalar(select(Conversation.id).where(Conversation.gmail_thread_id == tid)) is None:
            continue
        try:
            gm = _gmail(app, token, "GET", f"/messages/{mid}", params={"format": "raw"})
        except GmailError as exc:
            if exc.status == 404:
                continue  # deleted since the history entry
            raise
        conv = _lock_conversation(db, tid)
        if conv is not None and not _stored(db, mid) and _ingest(db, account, conv, gm):
            ingested += 1
        db.commit()  # per message: releases the lock, and a later failure cannot undo a stored reply's side effects
    return ingested


@router.post("/gmail/sync")
def sync(request: Request, db: Session = Depends(get_db)):
    app = request.app
    account = _connected(db)
    token = access_token(app, db, account)
    resynced = complete = True
    try:
        try:
            if not account.history_id:
                raise GmailError(404, "no_cursor")
            found, cursor = _history(app, token, account.history_id)
            resynced = False
        except GmailError as exc:
            if exc.status != 404:
                raise
            # Expired cursor is not "no reply": re-read tracked threads, most recently active first.
            threads = list(db.scalars(select(Conversation.gmail_thread_id).order_by(Conversation.updated_at.desc(), Conversation.id)
                                      .limit(MAX_RESYNC_THREADS + 1)))
            complete = len(threads) <= MAX_RESYNC_THREADS
            found, cursor = _resync(app, token, threads[:MAX_RESYNC_THREADS])
        ingested = _ingest_found(app, db, token, account, found)
    except GmailError as exc:
        db.rollback()
        account.last_error = f"Sync failed: {exc}"[:500]
        db.commit()
        raise _unavailable(exc) from exc
    # ponytail: an over-limit resync keeps the old cursor and re-reads the newest threads each time; page the
    # resync with a stored offset if a workspace ever tracks more than MAX_RESYNC_THREADS threads.
    if complete:
        account.history_id = cursor
    account.last_sync_at = utcnow()
    account.last_error = None if complete else f"Resync read only the {MAX_RESYNC_THREADS} most recent threads; cursor not advanced"
    db.commit()
    tracked = db.scalar(select(func.count(Conversation.id)))
    return {"ingested": ingested, "resynced": resynced, "complete": complete, "tracked_threads": tracked}


# ---------------------------------------------------------------- drafts

def content_hash(d: OutreachDraft) -> str:
    payload = {
        "company_id": str(d.company_id), "contact_id": str(d.contact_id),
        "conversation_id": str(d.conversation_id) if d.conversation_id else None,
        "recipients": d.recipients, "subject": d.subject, "body": d.body, "disclosure": d.disclosure, "version": d.version,
    }
    return _sha(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


class DraftFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("recipients", check_fields=False)
    @classmethod
    def _recipients(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        emails = [normalize_email(e) for e in v]
        if len(set(emails)) != len(emails):
            raise ValueError("duplicate recipient")
        return emails

    @field_validator("subject", check_fields=False)
    @classmethod
    def _subject(cls, v: str | None) -> str | None:
        if v is not None and ("\r" in v or "\n" in v):
            raise ValueError("subject must be a single line")
        return v

    @field_validator("disclosure", check_fields=False)
    @classmethod
    def _disclosure(cls, v: dict | None) -> dict | None:
        if v is not None and len(json.dumps(v)) > 4000:
            raise ValueError("disclosure metadata is too large")
        return v


class DraftIn(DraftFields):
    company_id: uuid.UUID
    contact_id: uuid.UUID
    conversation_id: uuid.UUID | None = None
    recipients: list[str] = Field(min_length=1, max_length=10)
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20000)
    disclosure: dict[str, Any] = Field(default_factory=dict, description="e.g. buyer identity disclosed, AI assistance, sender role")


class DraftPatch(DraftFields):
    recipients: list[str] = Field(None, min_length=1, max_length=10)
    subject: str = Field(None, min_length=1, max_length=300)
    body: str = Field(None, min_length=1, max_length=20000)
    disclosure: dict[str, Any] = None


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    content_hash: str = Field(min_length=64, max_length=64)
    expires_in_minutes: int = Field(1440, ge=5, le=10080)


def _draft_out(db: Session, d: OutreachDraft) -> dict:
    last = db.scalar(select(Dispatch).where(Dispatch.draft_id == d.id).order_by(Dispatch.claimed_at.desc()).limit(1))
    return {
        "id": d.id, "company_id": d.company_id, "contact_id": d.contact_id, "conversation_id": d.conversation_id,
        "enrollment_id": d.enrollment_id, "kind": d.kind, "recipients": d.recipients, "subject": d.subject, "body": d.body,
        "disclosure": d.disclosure, "version": d.version, "status": d.status, "content_hash": content_hash(d),
        "approval": d.approved_hash and {
            "version": d.approved_version, "content_hash": d.approved_hash, "approved_at": d.approved_at,
            "expires_at": d.approval_expires_at, "template_approval_id": d.template_approval_id,
        },
        "dispatch": last and {
            "id": last.id, "state": last.state, "rfc_message_id": last.rfc_message_id, "gmail_message_id": last.gmail_message_id,
            "gmail_thread_id": last.gmail_thread_id, "error": last.error, "claimed_at": last.claimed_at, "finished_at": last.finished_at,
        },
        "created_at": d.created_at, "updated_at": d.updated_at,
    }


def _check_target(db: Session, company_id: uuid.UUID, contact_id: uuid.UUID, conversation_id: uuid.UUID | None) -> None:
    get_or_404(db, Company, company_id)
    if get_or_404(db, Contact, contact_id).company_id != company_id:
        raise ApiError(422, "contact_mismatch", "Contact does not belong to the company")
    if conversation_id and get_or_404(db, Conversation, conversation_id).company_id != company_id:
        raise ApiError(422, "conversation_mismatch", "Conversation does not belong to the company")


@router.post("/outreach/drafts", status_code=201)
def create_draft(body: DraftIn, db: Session = Depends(get_db)):
    _check_target(db, body.company_id, body.contact_id, body.conversation_id)
    draft = OutreachDraft(**body.model_dump())
    db.add(draft)
    record_activity(db, "outreach.draft_created", "Outreach draft created", body.company_id)
    db.commit()
    return _draft_out(db, draft)


@router.get("/outreach/drafts")
def list_drafts(status: DraftStatus | None = None, company_id: uuid.UUID | None = None,
                limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    q = select(OutreachDraft).order_by(OutreachDraft.updated_at.desc()).limit(limit)
    if status:
        q = q.where(OutreachDraft.status == status)
    if company_id:
        q = q.where(OutreachDraft.company_id == company_id)
    return [_draft_out(db, d) for d in db.scalars(q)]


@router.get("/outreach/drafts/{draft_id}")
def get_draft(draft_id: uuid.UUID, db: Session = Depends(get_db)):
    return _draft_out(db, get_or_404(db, OutreachDraft, draft_id))


@router.patch("/outreach/drafts/{draft_id}")
def patch_draft(draft_id: uuid.UUID, body: DraftPatch, db: Session = Depends(get_db)):
    draft = get_or_404(db, OutreachDraft, draft_id)
    editable = (DraftStatus.draft, DraftStatus.approved, DraftStatus.failed)
    if draft.status not in editable:
        raise ApiError(409, "draft_locked", f"A {draft.status} draft cannot be edited; reconcile or create a new draft")
    # Any edit is a new version and voids the approval, even if the text ends up identical. Conditional on the
    # version and status read, so an edit that lost a race with a send claim or another edit changes nothing.
    changed = db.execute(update(OutreachDraft).where(
        OutreachDraft.id == draft.id, OutreachDraft.version == draft.version, OutreachDraft.status.in_(editable),
    ).values(**body.model_dump(exclude_unset=True), version=draft.version + 1, status=DraftStatus.draft, approved_hash=None,
             approved_version=None, approved_at=None, approval_expires_at=None, template_approval_id=None,
             updated_at=utcnow())).rowcount
    if changed != 1:
        db.rollback()
        raise ApiError(409, "draft_conflict", "The draft changed or is being sent; reload it")
    db.commit()
    db.refresh(draft)
    return _draft_out(db, draft)


@router.post("/outreach/drafts/{draft_id}/approve")
def approve_draft(draft_id: uuid.UUID, body: ApproveIn, db: Session = Depends(get_db)):
    draft = get_or_404(db, OutreachDraft, draft_id)
    if draft.status not in (DraftStatus.draft, DraftStatus.approved):
        raise ApiError(409, "not_approvable", f"A {draft.status} draft cannot be approved")
    current = content_hash(draft)
    if body.version != draft.version or not hmac.compare_digest(body.content_hash, current):
        raise ApiError(409, "stale_preview", "The draft changed after it was previewed; review the current version",
                       {"version": draft.version, "content_hash": current})
    now = utcnow()
    # Conditional: an edit or send claim that committed after the read above wins, and this approves nothing.
    if db.execute(update(OutreachDraft).where(
        OutreachDraft.id == draft.id, OutreachDraft.version == draft.version,
        OutreachDraft.status.in_((DraftStatus.draft, DraftStatus.approved)),
    ).values(status=DraftStatus.approved, approved_hash=current, approved_version=draft.version, approved_at=now,
             approval_expires_at=now + timedelta(minutes=body.expires_in_minutes), template_approval_id=None,
             updated_at=now)).rowcount != 1:
        db.rollback()
        raise ApiError(409, "stale_preview", "The draft changed after it was previewed; review the current version")
    db.refresh(draft)
    record_activity(db, "outreach.draft_approved", "Outreach draft approved", draft.company_id,
                    draft_id=str(draft.id), version=draft.version, content_hash=current)
    db.commit()
    return _draft_out(db, draft)


# ---------------------------------------------------------------- send

def _suppression_hit(db: Session, draft: OutreachDraft) -> Suppression | None:
    keys = [("global", "*"), ("channel", "email"), ("company", str(draft.company_id))] + [("email", r) for r in draft.recipients]
    return db.scalar(select(Suppression).where(or_(*(and_(Suppression.kind == k, Suppression.value == v) for k, v in keys))).limit(1))


def _check_sendable(db: Session, draft: OutreachDraft, status: DraftStatus = DraftStatus.approved) -> None:
    # populate_existing: sessions keep objects across commits, and a recheck must see what others committed since.
    from .deals import validate_deal_disclosure
    validate_deal_disclosure(db, draft.disclosure, draft)
    now, fresh = utcnow(), {"populate_existing": True}
    if draft.status != status:
        raise ApiError(409, "not_approved", f"Draft is {draft.status}, not approved")
    if draft.approved_version != draft.version or draft.approved_hash != content_hash(draft):
        raise ApiError(409, "approval_invalid", "Approval does not match the current draft content")
    if aware(draft.approval_expires_at) <= now:
        raise ApiError(409, "approval_expired", "Approval expired; review and approve again")
    if draft.template_approval_id:
        ta = db.get(TemplateApproval, draft.template_approval_id, **fresh)
        if ta is None or ta.revoked_at or aware(ta.expires_at) <= now:
            raise ApiError(409, "approval_expired", "The template approval was revoked or expired")
    if hit := _suppression_hit(db, draft):
        code = "outreach_stopped" if hit.kind == "global" else "suppressed"
        raise ApiError(409, code, f"Blocked by {hit.kind} suppression: {hit.reason}")
    company = db.get(Company, draft.company_id, **fresh)
    if company is None or company.status is not CompanyStatus.confirmed:
        raise ApiError(409, "company_unconfirmed", "Confirm the company record before contacting it")
    verified = set(db.scalars(select(Contact.email).where(
        Contact.company_id == draft.company_id, Contact.verification == Verification.verified, Contact.email.is_not(None))))
    contact = db.get(Contact, draft.contact_id, **fresh)
    if contact is None or contact.verification is not Verification.verified or not set(draft.recipients) <= verified:
        raise ApiError(409, "contact_unverified", "Every recipient must be a verified contact of this company")
    conv = draft.conversation_id and db.get(Conversation, draft.conversation_id, **fresh)
    if draft.conversation_id and conv is None:
        raise ApiError(409, "conversation_changed", "The conversation no longer exists")
    if draft.template_approval_id:
        # A preauthorized template ("following up…") may only go to a thread nobody has answered.
        answered = conv and db.scalar(select(MailMessage.id).where(
            MailMessage.conversation_id == conv.id, MailMessage.direction == "inbound").limit(1))
        if not conv or conv.status != "awaiting_reply" or answered:
            raise ApiError(409, "conversation_changed", "The thread has a reply; template follow-ups need per-draft review")
    if conv:
        if conv.status in ("opted_out", "declined"):
            raise ApiError(409, "conversation_closed", f"Conversation is {conv.status}")
        if conv.last_inbound_at and aware(conv.last_inbound_at) > aware(draft.approved_at):
            raise ApiError(409, "conversation_changed", "A reply arrived after approval; review the thread first")


def _check_thread_synced(app: FastAPI, db: Session, token: str, conv: Conversation) -> None:
    """The DB only knows synced replies; ask Gmail whether the thread has anything we have not stored."""
    messages = _thread_messages(app, token, conv.gmail_thread_id)
    if messages is None:
        raise ApiError(409, "conversation_changed", "The Gmail thread no longer exists; review before sending")
    stored = set(db.scalars(select(MailMessage.gmail_message_id).where(MailMessage.conversation_id == conv.id)))
    if any(m["id"] not in stored and "DRAFT" not in m.get("labelIds", []) for m in messages):
        raise ApiError(409, "conversation_changed", "The thread has messages that are not synced yet; sync and review first")


def _message_id(draft: OutreachDraft, sender: str) -> str:
    # Deterministic per draft version so an uncertain send can be found again with rfc822msgid:.
    return f"<permetheus.{draft.id.hex}.v{draft.version}@{sender.rsplit('@', 1)[1]}>"


def _thread_refs(db: Session, conv: Conversation) -> list[str]:
    ids = db.scalars(select(MailMessage.rfc_message_id).where(MailMessage.conversation_id == conv.id,
                                                              MailMessage.rfc_message_id.is_not(None)).order_by(MailMessage.sent_at))
    return list(ids)[-20:]


def build_mime(draft: OutreachDraft, sender: str, message_id: str, refs: list[str]) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(draft.recipients), draft.subject
    msg["Date"], msg["Message-ID"] = formatdate(usegmt=True), message_id
    if refs:
        msg["In-Reply-To"], msg["References"] = refs[-1], " ".join(refs)
    msg.set_content(draft.body)
    return msg.as_bytes(policy=policy.SMTP)


def _enrollment_needs_review(db: Session, draft: OutreachDraft) -> None:
    if draft.enrollment_id:
        db.execute(update(Enrollment).where(Enrollment.id == draft.enrollment_id, Enrollment.state == "active")
                   .values(state="awaiting_review"))


def _finalize_sent(db: Session, draft: OutreachDraft, record: Dispatch, sender: str, gmail_id: str, thread_id: str) -> bool:
    """Record a send exactly once. False (and nothing written) if another finisher already resolved this attempt."""
    now = utcnow()
    if db.execute(update(Dispatch).where(
        Dispatch.id == record.id, Dispatch.state.in_((DraftStatus.sending, DraftStatus.delivery_unknown)),
    ).values(state=DraftStatus.sent, gmail_message_id=gmail_id, gmail_thread_id=thread_id, finished_at=now)).rowcount != 1:
        db.rollback()
        return False
    draft.status = DraftStatus.sent
    # Keyed by the thread Gmail actually used: a changed subject can start a new thread, and its replies must be tracked.
    conv = _lock_conversation(db, thread_id)
    if conv is None:
        conv = Conversation(company_id=draft.company_id, contact_id=draft.contact_id, gmail_thread_id=thread_id, subject=draft.subject)
        db.add(conv)
        db.flush()
    elif conv.status == "unclear":  # unreviewed replies, interest and closed threads keep their status
        conv.status = "awaiting_reply"
    draft.conversation_id = conv.id
    if not _stored(db, gmail_id):
        db.add(MailMessage(conversation_id=conv.id, gmail_message_id=gmail_id, rfc_message_id=record.rfc_message_id,
                           direction="outbound", sender=sender, recipients=draft.recipients, subject=draft.subject,
                           body_text=draft.body, sent_at=now))
    e = draft.enrollment_id and db.get(Enrollment, draft.enrollment_id, populate_existing=True, with_for_update=True)
    if e and e.pending_draft_id == draft.id:
        e.pending_draft_id, e.conversation_id, e.step_index = None, conv.id, e.step_index + 1
        if e.step_index >= len(e.steps):
            e.state = "done"
        else:
            e.next_run_at = now + timedelta(hours=e.steps[e.step_index]["delay_hours"])
    record_activity(db, "outreach.sent", f"Email sent to {', '.join(draft.recipients)}", draft.company_id,
                    draft_id=str(draft.id), rfc_message_id=record.rfc_message_id, gmail_message_id=gmail_id)
    return True


def dispatch(app: FastAPI, db: Session, draft_id: uuid.UUID) -> OutreachDraft:
    account = _connected(db)
    token = access_token(app, db, account)
    draft = get_or_404(db, OutreachDraft, draft_id)
    _check_sendable(db, draft)
    conv = db.get(Conversation, draft.conversation_id) if draft.conversation_id else None
    if conv:
        try:
            _check_thread_synced(app, db, token, conv)
        except GmailError as exc:
            raise _unavailable(exc) from exc
    # Claim: the conditional update locks the row and fails if the draft changed or another send won.
    claimed = db.execute(update(OutreachDraft).where(
        OutreachDraft.id == draft.id, OutreachDraft.status == DraftStatus.approved,
        OutreachDraft.version == draft.version, OutreachDraft.approved_hash == draft.approved_hash,
    ).values(status=DraftStatus.sending, updated_at=utcnow())).rowcount
    if claimed != 1:
        db.rollback()
        raise ApiError(409, "dispatch_conflict", "Draft changed or is already being sent")
    refs = _thread_refs(db, conv) if conv else []
    record = Dispatch(draft_id=draft.id, draft_version=draft.version, content_hash=draft.approved_hash,
                      rfc_message_id=_message_id(draft, account.email), state=DraftStatus.sending)
    db.add(record)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "already_dispatched", "This draft version already has a send attempt") from exc
    try:
        # Rechecked after the claim commit: a stop, suppression or reply committed before it still blocks.
        _check_sendable(db, draft, DraftStatus.sending)
    except ApiError as exc:
        record.state = draft.status = DraftStatus.failed  # nothing reached Gmail; a new version may be approved
        record.error, record.finished_at = f"Blocked at send time: {exc.code}", utcnow()
        _enrollment_needs_review(db, draft)
        db.commit()
        raise

    payload = {"raw": base64.urlsafe_b64encode(build_mime(draft, account.email, record.rfc_message_id, refs)).decode()}
    if conv:
        payload["threadId"] = conv.gmail_thread_id
    try:
        sent = _gmail(app, token, "POST", "/messages/send", json=payload)
        if not (sent.get("id") and sent.get("threadId")):
            raise GmailError(200, "response_without_message_id", maybe_accepted=True)
    except GmailError as exc:
        state = DraftStatus.delivery_unknown if exc.maybe_accepted else DraftStatus.failed
        record.state = draft.status = state
        record.error, record.finished_at = str(exc)[:500], utcnow()
        _enrollment_needs_review(db, draft)
        record_activity(db, f"outreach.{state.value}", f"Send {state.value}: {exc}", draft.company_id, draft_id=str(draft.id))
        db.commit()
        details = {"draft_id": str(draft.id), "dispatch_id": str(record.id)}
        if state is DraftStatus.delivery_unknown:
            raise ApiError(502, "delivery_unknown", "Gmail may have accepted the message; reconcile before any retry", details) from exc
        raise ApiError(502, "send_failed", f"Gmail did not accept the message ({exc})", details) from exc
    if _finalize_sent(db, draft, record, account.email, sent["id"], sent["threadId"]):
        db.commit()
    else:
        db.refresh(draft)  # a reconcile finished it first
    return draft


@router.post("/outreach/drafts/{draft_id}/send")
def send_draft(draft_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    return _draft_out(db, dispatch(request.app, db, draft_id))


@router.post("/outreach/reconcile")
def reconcile(request: Request, db: Session = Depends(get_db)):
    """Resolve uncertain sends by searching the mailbox for the recorded Message-ID. Never resends, never gives up."""
    app, now = request.app, utcnow()
    account = _connected(db)
    token = access_token(app, db, account)
    pending = db.scalars(select(Dispatch).where(or_(
        Dispatch.state == DraftStatus.delivery_unknown,
        and_(Dispatch.state == DraftStatus.sending, Dispatch.claimed_at < now - STALE_SENDING),  # crashed mid-send
    ))).all()
    results = []
    for record in pending:
        draft = db.get(OutreachDraft, record.draft_id)
        out = {"dispatch_id": record.id, "draft_id": draft.id}
        try:
            # Trash and spam included: a sent message the operator deleted still counts as sent.
            hits = _gmail(app, token, "GET", "/messages", params={
                "q": f"rfc822msgid:{record.rfc_message_id.strip('<>')}", "includeSpamTrash": "true"}).get("messages", [])
        except GmailError as exc:
            results.append({**out, "state": record.state, "error": str(exc)})
            continue
        if not hits:
            # A miss is not proof: search can lag or miss. The attempt stays unresolved and the draft stays locked.
            db.execute(update(Dispatch).where(Dispatch.id == record.id, Dispatch.state == DraftStatus.sending)
                       .values(state=DraftStatus.delivery_unknown))
            db.execute(update(OutreachDraft).where(OutreachDraft.id == draft.id, OutreachDraft.status == DraftStatus.sending)
                       .values(status=DraftStatus.delivery_unknown))
            db.commit()
            results.append({**out, "state": DraftStatus.delivery_unknown})
            continue
        tid = hits[0]["threadId"]
        if not _finalize_sent(db, draft, record, account.email, hits[0]["id"], tid):
            results.append({**out, "state": "already_resolved"})
            continue
        db.commit()
        results.append({**out, "state": DraftStatus.sent})
        try:
            # The thread was untracked until now, so sync skipped its replies; read them once here.
            _ingest_found(app, db, token, account, [(m["id"], tid) for m in _thread_messages(app, token, tid) or []])
        except GmailError as exc:
            db.rollback()
            results[-1]["error"] = f"Sent, but reading its thread failed: {exc}"
    return {"reconciled": results}


# ---------------------------------------------------------------- conversations

class ClassifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: uuid.UUID
    intent: ReplyIntent
    speaker_authority: SpeakerAuthority = SpeakerAuthority.unverified


def _message_out(m: MailMessage) -> dict:
    return {
        "id": m.id, "direction": m.direction, "sender": m.sender, "recipients": m.recipients, "subject": m.subject,
        "body_text": m.body_text, "sent_at": m.sent_at, "rfc_message_id": m.rfc_message_id,
        "proposed_intent": m.proposed_intent, "proposed_reason": m.proposed_reason,
        "confirmed_intent": m.confirmed_intent, "confirmed_at": m.confirmed_at,
    }


def _messages(db: Session, conv_id: uuid.UUID) -> list[MailMessage]:
    return list(db.scalars(select(MailMessage).where(MailMessage.conversation_id == conv_id).order_by(MailMessage.sent_at)))


def _latest_inbound(db: Session, conv_id: uuid.UUID) -> MailMessage | None:
    return db.scalar(select(MailMessage).where(MailMessage.conversation_id == conv_id, MailMessage.direction == "inbound")
                     .order_by(MailMessage.sent_at.desc()).limit(1))


def _conversation_out(db: Session, c: Conversation) -> dict:
    latest = _latest_inbound(db, c.id)
    return {
        "id": c.id, "company_id": c.company_id, "contact_id": c.contact_id, "gmail_thread_id": c.gmail_thread_id,
        "subject": c.subject, "status": c.status, "last_inbound_at": c.last_inbound_at, "updated_at": c.updated_at,
        "latest_reply": latest and _message_out(latest),
    }


@router.get("/outreach/conversations")
def list_conversations(company_id: uuid.UUID | None = None, status: str | None = None,
                       limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    q = select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
    if company_id:
        q = q.where(Conversation.company_id == company_id)
    if status:
        q = q.where(Conversation.status == status)
    return [_conversation_out(db, c) for c in db.scalars(q)]


@router.get("/outreach/conversations/{conversation_id}")
def get_conversation(conversation_id: uuid.UUID, db: Session = Depends(get_db)):
    conv = get_or_404(db, Conversation, conversation_id)
    drafts = db.scalars(select(OutreachDraft).where(OutreachDraft.conversation_id == conv.id).order_by(OutreachDraft.created_at))
    return {**_conversation_out(db, conv), "messages": [_message_out(m) for m in _messages(db, conv.id)],
            "drafts": [_draft_out(db, d) for d in drafts]}


@router.post("/outreach/conversations/{conversation_id}/classify")
def classify(conversation_id: uuid.UUID, body: ClassifyIn, db: Session = Depends(get_db)):
    conv = get_or_404(db, Conversation, conversation_id)
    msg = db.get(MailMessage, body.message_id)
    if msg is None or msg.conversation_id != conv.id or msg.direction != "inbound":
        raise ApiError(404, "not_found", "Inbound message not found in this conversation")
    sender_verified = db.scalar(select(Contact.id).where(
        Contact.company_id == conv.company_id, Contact.verification == Verification.verified,
        func.lower(func.trim(Contact.email)) == msg.sender).limit(1))
    if body.speaker_authority is not SpeakerAuthority.unverified and not sender_verified:
        raise ApiError(409, "speaker_unverified", "Only a reply from a verified contact of this company can carry authority")
    # Conditional: a second (or concurrent) classification would duplicate the evidence.
    if db.execute(update(MailMessage).where(MailMessage.id == msg.id, MailMessage.confirmed_intent.is_(None))
                  .values(confirmed_intent=body.intent, confirmed_at=utcnow())).rowcount != 1:
        db.rollback()
        raise ApiError(409, "already_classified", "This reply was already classified")
    conv.status = {"interested": "interested", "no": "declined", "optout": "opted_out", "unclear": "unclear"}[body.intent.value]
    contact = conv.contact_id and db.get(Contact, conv.contact_id)
    if body.intent is ReplyIntent.optout:
        _suppress_optout(db, conv, msg.sender, "Opt-out confirmed by operator")
    if body.intent in (ReplyIntent.optout, ReplyIntent.no):
        _stop_enrollments(db, conv.id, "stopped")
    if body.intent is ReplyIntent.interested:
        db.execute(update(Enrollment).where(Enrollment.conversation_id == conv.id, Enrollment.state == "awaiting_review")
                   .values(state="active"))
    if body.intent in (ReplyIntent.interested, ReplyIntent.no):
        source = Source(kind=SourceKind.email, title=msg.subject[:500], fetched_at=msg.sent_at, content_hash=_sha(msg.body_text))
        db.add(source)
        db.flush()
        stance = SellerIntent.interested if body.intent is ReplyIntent.interested else SellerIntent.not_interested
        # Credited to the contact only if the contact wrote it; a colleague or forged From is named by address.
        own = contact and (contact.email or "").strip().lower() == msg.sender
        db.add(IntentStatement(company_id=conv.company_id, source_id=source.id, speaker_name=(contact.name if own else msg.sender),
                               speaker_authority=body.speaker_authority, stance=stance, statement=msg.body_text[:5000],
                               stated_at=msg.sent_at, confirmed=True))
        # Company-level willingness moves only on an operator-confirmed statement from someone with authority.
        if body.speaker_authority is not SpeakerAuthority.unverified:
            db.get(Company, conv.company_id).seller_intent = stance
    record_activity(db, "outreach.classified", f"Reply classified as {body.intent.value}", conv.company_id,
                    message_id=str(msg.id), proposed=msg.proposed_intent and msg.proposed_intent.value)
    db.commit()
    return _conversation_out(db, conv)


FIELD_QUESTIONS = {
    "revenue": "Approximate revenue for the latest financial year",
    "ebitda": "Approximate EBITDA or operating profit for the latest financial year",
    "employees": "Current number of employees",
    "ownership": "Who owns the company and who would take part in a decision",
    "timing": "Your preferred timing",
    "structure": "What kind of transaction you have in mind (full sale, majority, minority or succession)",
}


def missing_fields(db: Session, company_id: uuid.UUID) -> list[str]:
    """Only asks; never fills. Values come back as owner-reported evidence for review."""
    known = set(db.scalars(select(FinancialObservation.metric).where(
        FinancialObservation.company_id == company_id, FinancialObservation.review_status == ReviewStatus.accepted,
        FinancialObservation.amount.is_not(None))))
    fields = [m.value for m in (FinancialMetric.revenue, FinancialMetric.ebitda, FinancialMetric.employees) if m not in known]
    if not db.scalar(select(IntentStatement.id).where(IntentStatement.company_id == company_id, IntentStatement.confirmed.is_(True),
                                                      IntentStatement.speaker_authority == SpeakerAuthority.owner)):
        fields.append("ownership")
    return fields + ["timing", "structure"]


def _re(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"[:300]


def _followup(db: Session, conv: Conversation, contact: Contact) -> OutreachDraft:
    fields = missing_fields(db, conv.company_id)
    questions = "\n".join(f"{i}. {FIELD_QUESTIONS[f]}" for i, f in enumerate(fields, 1))
    body = (f"Hello {contact.name},\n\nThank you for your reply. So that a first conversation is useful, could you share "
            f"a few details:\n\n{questions}\n\nShort answers are fine, and anything you prefer not to put in writing can wait "
            f"until we speak.\n")
    return OutreachDraft(company_id=conv.company_id, contact_id=contact.id, conversation_id=conv.id, kind="missing_fields",
                         recipients=[contact.email], subject=_re(conv.subject), body=body,
                         disclosure={"generated": "missing_fields", "fields": fields})


def _followup_contact(db: Session, conv: Conversation) -> Contact:
    latest = _latest_inbound(db, conv.id)
    if latest is None or latest.confirmed_intent is not ReplyIntent.interested:
        raise ApiError(409, "interest_not_confirmed", "Confirm an interested reply before asking for details")
    contact = conv.contact_id and db.get(Contact, conv.contact_id)
    if not contact or not contact.email:
        raise ApiError(409, "contact_missing", "Conversation has no contact with an email address")
    return contact


@router.post("/outreach/conversations/{conversation_id}/followup", status_code=201)
def create_followup(conversation_id: uuid.UUID, db: Session = Depends(get_db)):
    conv = get_or_404(db, Conversation, conversation_id)
    draft = _followup(db, conv, _followup_contact(db, conv))
    db.add(draft)
    db.commit()
    return _draft_out(db, draft)


# ---------------------------------------------------------------- suppression and stop switch

class SuppressionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["email", "company", "channel"]
    value: str = Field(min_length=1, max_length=320)
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def _normalize(self):
        if self.kind == "email":
            self.value = normalize_email(self.value)
        elif self.kind == "company":
            self.value = str(uuid.UUID(self.value))
        elif self.value != "email":
            raise ValueError("channel suppression supports: email")
        return self


@router.get("/outreach/suppressions")
def list_suppressions(db: Session = Depends(get_db)):
    return [{"id": s.id, "kind": s.kind, "value": s.value, "reason": s.reason, "source": s.source, "created_at": s.created_at}
            for s in db.scalars(select(Suppression).where(Suppression.kind != "global").order_by(Suppression.created_at.desc()))]


@router.post("/outreach/suppressions", status_code=201)
def add_suppression(body: SuppressionIn, db: Session = Depends(get_db)):
    _suppress(db, body.kind, body.value, body.reason)
    db.commit()
    return {"kind": body.kind, "value": body.value, "suppressed": True}


@router.delete("/outreach/suppressions/{suppression_id}")
def remove_suppression(suppression_id: uuid.UUID, db: Session = Depends(get_db)):
    s = get_or_404(db, Suppression, suppression_id)
    if s.source == "optout" or s.kind == "global":
        raise ApiError(409, "suppression_protected", "Opt-outs and the stop switch cannot be removed here")
    db.delete(s)
    db.commit()
    return {"id": suppression_id, "deleted": True}


class StopIn(BaseModel):
    stopped: bool


def _stopped(db: Session) -> bool:
    return db.scalar(select(Suppression.id).where(Suppression.kind == "global")) is not None


@router.get("/outreach/controls")
def controls(db: Session = Depends(get_db)):
    return {"stopped": _stopped(db)}


@router.post("/outreach/stop")
def stop_switch(body: StopIn, db: Session = Depends(get_db)):
    if body.stopped:
        _suppress(db, "global", "*", "All outbound stopped by operator")
    else:
        db.execute(delete(Suppression).where(Suppression.kind == "global"))
    record_activity(db, "outreach.stop_switch", f"Outbound {'stopped' if body.stopped else 'resumed'}", None)
    db.commit()
    return {"stopped": body.stopped}


# ---------------------------------------------------------------- sequences

def _placeholders(template: str) -> set[str]:
    fields = set()
    for _, name, spec, conversion in Formatter().parse(template):
        if name is not None:
            if spec or conversion:
                raise ValueError("template placeholders cannot use format specs or conversions")
            fields.add(name)
    return fields


class StepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["ask_interest", "request_missing_fields"]
    delay_hours: int = Field(0, ge=0, le=2160, description="Durable wait after the previous step is sent")
    subject: str | None = Field(None, max_length=300)
    body: str | None = Field(None, max_length=20000)
    approval: Literal["per_draft", "template"] = "per_draft"


class SequenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    steps: list[StepIn] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _validate(self):
        # Steps are a linear list, so loops and backwards transitions cannot be expressed.
        first = self.steps[0]
        if first.kind != "ask_interest" or first.approval != "per_draft":
            raise ValueError("step 0 must be ask_interest with per_draft approval: cold outreach needs an exact preview")
        for i, s in enumerate(self.steps):
            if s.kind == "ask_interest":
                # Later steps reply in the thread with "Re: <original subject>".
                if not s.body or (i == 0 and not s.subject):
                    raise ValueError(f"step {i}: ask_interest needs a body (and a subject on step 0)")
                if s.subject and ("\n" in s.subject or "\r" in s.subject):
                    raise ValueError(f"step {i}: subject must be a single line")
                if unknown := (_placeholders(s.subject or "") | _placeholders(s.body)) - TEMPLATE_FIELDS:
                    raise ValueError(f"step {i}: unknown placeholders {sorted(unknown)}; allowed {sorted(TEMPLATE_FIELDS)}")
            elif s.subject or s.body or s.approval != "per_draft":
                raise ValueError(f"step {i}: missing-field requests are generated and always reviewed per draft")
            if i and s.delay_hours == 0:
                raise ValueError(f"step {i}: follow-up steps need delay_hours > 0")
        return self


class TemplateApprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_index: int = Field(ge=1)
    company_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    expires_in_hours: int = Field(168, ge=1, le=720)


class EnrollIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_id: uuid.UUID
    contact_id: uuid.UUID


def _sequence_out(s: Sequence) -> dict:
    return {"id": s.id, "name": s.name, "steps": s.steps, "version": s.version, "paused": s.paused, "updated_at": s.updated_at}


def _enrollment_out(e: Enrollment) -> dict:
    return {"id": e.id, "sequence_id": e.sequence_id, "sequence_version": e.sequence_version, "company_id": e.company_id,
            "contact_id": e.contact_id, "conversation_id": e.conversation_id, "step_index": e.step_index,
            "steps": len(e.steps), "state": e.state, "next_run_at": e.next_run_at, "pending_draft_id": e.pending_draft_id}


def _template_hash(version: int, step: dict) -> str:
    return _sha(json.dumps({"version": version, "step": step}, sort_keys=True))


@router.get("/outreach/sequences")
def list_sequences(db: Session = Depends(get_db)):
    return [_sequence_out(s) for s in db.scalars(select(Sequence).order_by(Sequence.created_at))]


@router.post("/outreach/sequences", status_code=201)
def create_sequence(body: SequenceIn, db: Session = Depends(get_db)):
    seq = Sequence(name=body.name, steps=[s.model_dump() for s in body.steps])
    db.add(seq)
    db.commit()
    return _sequence_out(seq)


@router.patch("/outreach/sequences/{sequence_id}")
def update_sequence(sequence_id: uuid.UUID, body: SequenceIn, db: Session = Depends(get_db)):
    seq = get_or_404(db, Sequence, sequence_id)
    # A new version: template approvals are version-bound and running enrollments keep their snapshot.
    seq.name, seq.steps, seq.version = body.name, [s.model_dump() for s in body.steps], seq.version + 1
    db.commit()
    return _sequence_out(seq)


def _set_paused(db: Session, sequence_id: uuid.UUID, paused: bool) -> dict:
    seq = get_or_404(db, Sequence, sequence_id)
    seq.paused = paused
    db.commit()
    return _sequence_out(seq)


@router.post("/outreach/sequences/{sequence_id}/pause")
def pause_sequence(sequence_id: uuid.UUID, db: Session = Depends(get_db)):
    return _set_paused(db, sequence_id, True)


@router.post("/outreach/sequences/{sequence_id}/resume")
def resume_sequence(sequence_id: uuid.UUID, db: Session = Depends(get_db)):
    return _set_paused(db, sequence_id, False)


@router.post("/outreach/sequences/{sequence_id}/template-approvals", status_code=201)
def approve_template(sequence_id: uuid.UUID, body: TemplateApprovalIn, db: Session = Depends(get_db)):
    seq = get_or_404(db, Sequence, sequence_id)
    if body.step_index >= len(seq.steps) or seq.steps[body.step_index]["approval"] != "template":
        raise ApiError(422, "step_not_templatable", "Only steps configured with approval=template can be preauthorized")
    ta = TemplateApproval(sequence_id=seq.id, sequence_version=seq.version, step_index=body.step_index,
                          template_hash=_template_hash(seq.version, seq.steps[body.step_index]),
                          company_ids=sorted({str(c) for c in body.company_ids}),
                          expires_at=utcnow() + timedelta(hours=body.expires_in_hours))
    db.add(ta)
    record_activity(db, "outreach.template_approved", f"Template step {body.step_index} of {seq.name} approved", None,
                    sequence_id=str(seq.id), version=seq.version, companies=len(ta.company_ids))
    db.commit()
    return {"id": ta.id, "sequence_version": ta.sequence_version, "step_index": ta.step_index, "template_hash": ta.template_hash,
            "company_ids": ta.company_ids, "expires_at": ta.expires_at}


@router.delete("/outreach/template-approvals/{approval_id}")
def revoke_template(approval_id: uuid.UUID, db: Session = Depends(get_db)):
    get_or_404(db, TemplateApproval, approval_id).revoked_at = utcnow()
    db.commit()
    return {"id": approval_id, "revoked": True}


@router.post("/outreach/sequences/{sequence_id}/enrollments", status_code=201)
def enroll(sequence_id: uuid.UUID, body: EnrollIn, db: Session = Depends(get_db)):
    seq = get_or_404(db, Sequence, sequence_id)
    _check_target(db, body.company_id, body.contact_id, None)
    contact = db.get(Contact, body.contact_id)
    if contact.verification is not Verification.verified or not contact.email:
        raise ApiError(409, "contact_unverified", "Only verified contacts with an email address can be enrolled")
    if db.scalar(select(Enrollment.id).where(Enrollment.contact_id == contact.id, Enrollment.state.in_(("active", "awaiting_review")))):
        raise ApiError(409, "already_enrolled", "Contact already has a running sequence")
    e = Enrollment(sequence_id=seq.id, sequence_version=seq.version, steps=seq.steps, company_id=body.company_id,
                   contact_id=contact.id, next_run_at=utcnow() + timedelta(hours=seq.steps[0]["delay_hours"]))
    db.add(e)
    db.commit()
    return _enrollment_out(e)


@router.get("/outreach/enrollments")
def list_enrollments(sequence_id: uuid.UUID | None = None, db: Session = Depends(get_db)):
    q = select(Enrollment).order_by(Enrollment.created_at.desc())
    if sequence_id:
        q = q.where(Enrollment.sequence_id == sequence_id)
    return [_enrollment_out(e) for e in db.scalars(q)]


@router.post("/outreach/enrollments/{enrollment_id}/{action}")
def enrollment_action(enrollment_id: uuid.UUID, action: Literal["stop", "resume"], db: Session = Depends(get_db)):
    e = get_or_404(db, Enrollment, enrollment_id)
    if action == "stop":
        e.state = "stopped"
    elif e.state == "awaiting_review":
        e.state = "active"
    else:
        raise ApiError(409, "not_resumable", f"A {e.state} enrollment cannot be resumed")
    db.commit()
    return _enrollment_out(e)


def _valid_template_approval(db: Session, e: Enrollment) -> TemplateApproval | None:
    ta = db.scalar(select(TemplateApproval).where(
        TemplateApproval.sequence_id == e.sequence_id, TemplateApproval.sequence_version == e.sequence_version,
        TemplateApproval.step_index == e.step_index, TemplateApproval.revoked_at.is_(None), TemplateApproval.expires_at > utcnow(),
        TemplateApproval.template_hash == _template_hash(e.sequence_version, e.steps[e.step_index]),
    ).order_by(TemplateApproval.created_at.desc()).limit(1))
    return ta if ta and str(e.company_id) in ta.company_ids else None


def _clean(value: str) -> str:
    return " ".join(value.split())  # template values can never inject header lines


def _run_step(app: FastAPI, db: Session, e: Enrollment) -> dict:
    out = {"enrollment_id": e.id, "step_index": e.step_index}
    draft_id = uuid.uuid4()
    # Claim before drafting: a concurrent run-due waits on the row lock, then matches no row and skips.
    if db.execute(update(Enrollment).where(
        Enrollment.id == e.id, Enrollment.pending_draft_id.is_(None), Enrollment.state == "active",
        Enrollment.step_index == e.step_index,
    ).values(pending_draft_id=draft_id)).rowcount != 1:
        db.rollback()
        return {**out, "result": "skipped", "reason": "claimed_elsewhere"}
    db.refresh(e)
    step = e.steps[e.step_index]
    contact, company = db.get(Contact, e.contact_id), db.get(Company, e.company_id)
    conv = db.get(Conversation, e.conversation_id) if e.conversation_id else None
    out["kind"] = step["kind"]
    if e.step_index and conv is None:  # thread deleted under a running sequence
        e.state, e.pending_draft_id = "awaiting_review", None
        db.commit()
        return {**out, "result": "awaiting_review", "reason": "conversation_missing"}
    if step["kind"] == "request_missing_fields":
        try:
            draft = _followup(db, conv, _followup_contact(db, conv)) if conv else None
        except ApiError:
            draft = None
        if draft is None:
            e.state, e.pending_draft_id = "awaiting_review", None
            db.commit()
            return {**out, "result": "awaiting_review", "reason": "interest_not_confirmed"}
    else:
        values = {"company_name": _clean(company.name), "contact_name": _clean(contact.name)}
        draft = OutreachDraft(
            company_id=e.company_id, contact_id=contact.id, conversation_id=conv and conv.id, kind="sequence",
            recipients=[contact.email], subject=_re(conv.subject) if conv else step["subject"].format_map(values),
            body=step["body"].format_map(values),
            disclosure={"sequence_id": str(e.sequence_id), "sequence_version": e.sequence_version, "step_index": e.step_index})
    draft.id, draft.enrollment_id = draft_id, e.id
    db.add(draft)
    db.flush()
    # Preauthorization only covers in-thread follow-ups, never the first (cold) message.
    ta = step["approval"] == "template" and conv is not None and _valid_template_approval(db, e)
    if ta:
        draft.status, draft.approved_hash, draft.approved_version = DraftStatus.approved, content_hash(draft), draft.version
        draft.approved_at, draft.approval_expires_at, draft.template_approval_id = utcnow(), ta.expires_at, ta.id
    db.commit()
    if not ta:
        return {**out, "result": "draft_awaiting_review", "draft_id": draft.id}
    try:
        dispatch(app, db, draft.id)
    except ApiError as exc:
        db.rollback()
        e.state = "awaiting_review"
        db.commit()
        return {**out, "result": "blocked", "draft_id": draft.id, "error": exc.code}
    return {**out, "result": "sent", "draft_id": draft.id}


@router.post("/outreach/run-due")
def run_due(request: Request, limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    """Advance due enrollments. Waits are stored as next_run_at, so they survive restarts; a scheduler calls this."""
    if _stopped(db):
        return {"stopped": True, "results": []}
    due = db.scalars(select(Enrollment).join(Sequence, Sequence.id == Enrollment.sequence_id).where(
        Enrollment.state == "active", Enrollment.pending_draft_id.is_(None), Enrollment.next_run_at <= utcnow(),
        Sequence.paused.is_(False)).order_by(Enrollment.next_run_at).limit(limit)).all()
    return {"stopped": False, "results": [_run_step(request.app, db, e) for e in due]}
